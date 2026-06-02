"""Persistent emotion-session daemon.

Holds one Claude CLI process open for the bot's lifetime, communicating
via stdin/stdout in stream-json mode. Multi-turn capability on a single
process was verified against CLI 2.1.92 with the flags

    claude --print --input-format stream-json --output-format stream-json
           --verbose --model haiku --resume <SID>

(verbose is required by the CLI when using --output-format stream-json).

The daemon is shared between conversation feedback (every user turn) and
the 30-minute silence tick; both await `daemon.query(prompt)` serialized
via an asyncio.Lock. CLI startup cost (2-5s) is paid once at bot boot
instead of every turn, which is the main reason this daemon exists.

Lifecycle:
    daemon = EmotionSessionDaemon("emotion", STATE_DIR, model="haiku",
                                  cwd=REPO_DIR)
    await daemon.start()           # on_ready
    result = await daemon.query(prompt)
    await daemon.stop()            # bot shutdown

Crash / hang recovery: every query is bounded by `query_timeout`; on
timeout or broken pipe the process is killed and the next query re-spawns
with `--resume`. The persistent JSON state files (`kanae_emotion.json` /
`kanae_daily.json` / `kanae_daily_summary.txt`) are the source of truth,
so the in-CLI working memory loss is recoverable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from services.emotion.session import load_sessions, reset_session

log = logging.getLogger(__name__)

# Per-query timeout — covers LLM inference plus any startup work.
DEFAULT_QUERY_TIMEOUT_S = 60.0

# Bound on how long we wait for the CLI's `type:system, subtype:init`
# handshake to arrive after spawning.
DEFAULT_STARTUP_TIMEOUT_S = 30.0

# Backoff before successive spawn retries. The last value is reused
# indefinitely so we never give up — Claude CLI being unavailable is
# generally transient (rate-limit, network blip).
RESTART_BACKOFF_S = (1.0, 2.0, 5.0, 10.0)

# After this many consecutive failures we rotate the session-id (the
# session file may have been corrupted) and let `--session-id` create a
# fresh session on the next attempt.
SESSION_ROTATION_THRESHOLD = 3

# A turn that comes back from the CLI but whose text isn't valid JSON is
# almost always a refusal ("I will not respond...") — and once that lands
# in a resumed session it poisons every subsequent turn. Rotate the
# session after this many consecutive unparseable results so the daemon
# self-heals instead of looping on the refusal forever (the bug behind the
# 364 dead turns observed 2026-05-29).
PARSE_FAILURE_ROTATION_THRESHOLD = 2

# Proactively rotate the session every N successful turns. The emotion
# prompt is self-contained (it re-embeds journal / daily / mem0 context
# each turn), so the resumed CLI transcript holds no irreplaceable state —
# but it does accrete, and a long enough haiku transcript starts reading
# the repeated prompts as one escalating request and refuses. Cutting a
# fresh session well before that point keeps every turn a clean single-shot.
SESSION_ROTATION_TURN_INTERVAL = 20


class EmotionSessionDaemon:
    """Long-lived Claude CLI process for the emotion engine.

    Concurrency:
      - All `query()` calls take `self._lock`. Conversation feedback and
        the silence tick contend on the same lock; the silence tick can
        check `self.is_conversation_active` to skip rather than queue.
      - The lock also serializes lifecycle ops (start / stop / restart)
        with queries so we never write to a stdin we just closed.
    """

    def __init__(
        self,
        session_key: str,
        state_dir: str | Path,
        *,
        model: str = "haiku",
        cwd: str | Path | None = None,
        query_timeout: float = DEFAULT_QUERY_TIMEOUT_S,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_S,
    ):
        self.session_key = session_key
        self.state_dir = Path(state_dir)
        self.model = model
        self.cwd = str(cwd) if cwd else None
        self.query_timeout = query_timeout
        self.startup_timeout = startup_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._consecutive_failures = 0
        # Unparseable-result streak (refusals); drives self-heal rotation.
        self._parse_failures = 0
        # Successful turns on the current session; drives proactive rotation.
        self._query_count = 0
        self._stderr_drain_task: asyncio.Task | None = None
        self.is_conversation_active = False

    # ────────────────────────── public surface ─────────────────────────

    async def start(self) -> None:
        """Spawn the CLI process if not already running. Idempotent."""
        async with self._lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            await self._spawn_with_retry()

    async def stop(self, timeout: float = 5.0) -> None:
        """Gracefully shut down. Closes stdin, waits, kills on timeout."""
        async with self._lock:
            await self._terminate(timeout)

    async def query(self, prompt: str, *, timeout: float | None = None) -> dict | None:
        """Send `prompt` as a user message, return the parsed JSON output.

        The CLI emits a stream of `type:assistant` / `type:rate_limit_event`
        / etc. messages and finally one `type:result` per turn. We extract
        the `result.result` text (the model's final assistant message) and
        attempt to JSON-parse it — emotion engine prompts ask for a strict
        JSON object as the only output.

        Returns the parsed dict, or None if anything went wrong (parse
        error, timeout, daemon crash). Never raises.
        """
        async with self._lock:
            return await self._query_locked(prompt, timeout or self.query_timeout)

    @property
    def is_alive(self) -> bool:
        """True if the underlying CLI process is still running."""
        return self._proc is not None and self._proc.returncode is None

    # ───────────────────────── locked impls ─────────────────────────────

    async def _query_locked(self, prompt: str, timeout: float) -> dict | None:
        # Ensure the daemon is alive (lazily restart after a crash).
        if not self.is_alive:
            ok = await self._spawn_with_retry()
            if not ok:
                return None

        proc = self._proc
        assert proc is not None and proc.stdin is not None

        # Write one stream-json user message terminated by a newline. The
        # CLI parses one message per line.
        msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        }
        try:
            proc.stdin.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, AttributeError) as exc:
            log.warning("daemon %s: stdin write failed: %s", self.session_key, exc)
            await self._terminate(timeout=2.0)
            self._consecutive_failures += 1
            return None

        try:
            result_msg = await asyncio.wait_for(self._read_until_result(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("daemon %s: query timeout after %ss; killing for restart",
                        self.session_key, timeout)
            await self._terminate(timeout=2.0)
            self._consecutive_failures += 1
            return None

        if result_msg is None:
            # Hit EOF before a result — process probably crashed mid-turn.
            log.warning("daemon %s: EOF before result", self.session_key)
            await self._terminate(timeout=2.0)
            self._consecutive_failures += 1
            return None

        self._consecutive_failures = 0
        parsed = self._parse_result(result_msg)
        if parsed is None:
            # CLI produced a turn but its text wasn't valid JSON — almost
            # always a refusal that will poison every resumed turn after it.
            # Count it and rotate the session before it cements into a loop.
            self._parse_failures += 1
            if self._parse_failures >= PARSE_FAILURE_ROTATION_THRESHOLD:
                log.warning(
                    "daemon %s: %d consecutive unparseable results "
                    "(likely refusal); rotating session",
                    self.session_key, self._parse_failures,
                )
                reset_session(self.state_dir, self.session_key)
                await self._terminate(timeout=2.0)
                self._parse_failures = 0
                self._query_count = 0
            return None

        # Healthy turn. Clear the refusal streak and count it toward the
        # proactive rotation budget so the transcript never grows unbounded.
        self._parse_failures = 0
        self._query_count += 1
        if self._query_count >= SESSION_ROTATION_TURN_INTERVAL:
            log.info(
                "daemon %s: rotating session after %d turns to stop "
                "context accretion", self.session_key, self._query_count,
            )
            reset_session(self.state_dir, self.session_key)
            await self._terminate(timeout=2.0)
            self._query_count = 0
        return parsed

    async def _read_until_result(self) -> dict | None:
        """Drain stream-json lines until `type:result` (one turn complete)."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return None
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Stream-json should always be one JSON per line; non-JSON
                # would be a CLI bug. Skip and keep reading.
                continue
            if obj.get("type") == "result":
                return obj

    @staticmethod
    def _parse_result(result_msg: dict) -> dict | None:
        """Pull the model's JSON out of `result_msg.result` (a string).

        Emotion-engine prompts ask the model for a single JSON object as
        the entire response. We tolerate optional ```json``` code fences
        because models occasionally add them despite instructions.
        """
        text = result_msg.get("result", "")
        if not isinstance(text, str):
            return None
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```", 2)
            if len(parts) >= 2:
                text = parts[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("daemon: failed to parse result JSON: %s", text[:200])
            return None
        return data if isinstance(data, dict) else None

    # ───────────────────────── spawn / terminate ────────────────────────

    async def _spawn_with_retry(self) -> bool:
        """Try spawn with backoff; rotate session-id after repeated failures."""
        for attempt in range(len(RESTART_BACKOFF_S) + 1):
            try:
                await self._spawn()
                return True
            except Exception as exc:
                self._consecutive_failures += 1
                log.warning("daemon %s: spawn attempt %d failed: %s",
                            self.session_key, attempt, exc)
                if self._consecutive_failures >= SESSION_ROTATION_THRESHOLD:
                    # Likely a corrupted session file — generate a fresh
                    # UUID so the next attempt is a clean --session-id
                    # rather than a doomed --resume.
                    log.warning("daemon %s: rotating session-id after %d failures",
                                self.session_key, self._consecutive_failures)
                    reset_session(self.state_dir, self.session_key)
                if attempt >= len(RESTART_BACKOFF_S):
                    return False
                await asyncio.sleep(RESTART_BACKOFF_S[attempt])
        return False

    async def _spawn(self) -> None:
        """Start the CLI, choosing --resume vs --session-id based on session file.

        Claude CLI stores session transcripts under
        ``~/.claude/projects/<cwd-encoded>/<session-id>.jsonl``. If that
        file exists, we can `--resume` and the CLI rehydrates the
        conversation. If not (first-run for this session-id, or rotated
        after a corrupt session), we use `--session-id` to create it.

        The CLI doesn't emit its `system/init` message until *after* it
        receives the first user message on stdin — there's no useful
        "ready" handshake to wait for. We just verify the process didn't
        die immediately and let the first query drain past init in its
        normal read loop.
        """
        sessions = load_sessions(self.state_dir)
        session_id = sessions[self.session_key]
        resume = self._session_file_exists(session_id)

        proc = await self._launch(session_id, resume=resume)
        # Give the CLI ~0.5s to keel over if it didn't like the args.
        await asyncio.sleep(0.5)
        if proc.returncode is not None and resume:
            # Resume failed — file existed but CLI rejected it (corrupt /
            # incompatible). Fall back to fresh --session-id.
            log.info("daemon %s: --resume exited (rc=%s), retrying with --session-id",
                     self.session_key, proc.returncode)
            await self._collect_stderr(proc)
            proc = await self._launch(session_id, resume=False)
            await asyncio.sleep(0.5)
        if proc.returncode is not None:
            err = await self._collect_stderr(proc)
            raise RuntimeError(
                f"daemon spawn: CLI exited immediately "
                f"(rc={proc.returncode}, stderr={err[:300]!r})"
            )

        self._proc = proc
        # stderr must be drained continuously — if the kernel pipe buffer
        # fills (Claude CLI writes diagnostic lines there), the CLI blocks
        # on write and stops emitting stdout, hanging our query reads.
        self._stderr_drain_task = asyncio.create_task(
            self._drain_stderr(proc), name=f"daemon-{self.session_key}-stderr"
        )
        log.info("daemon %s spawned (pid=%s sid=%s model=%s)",
                 self.session_key, proc.pid, session_id[:8], self.model)

    @staticmethod
    async def _collect_stderr(proc: asyncio.subprocess.Process) -> str:
        """Drain any pending stderr from a dead process for the error message."""
        if proc.stderr is None:
            return ""
        try:
            return (await proc.stderr.read()).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _session_file_exists(self, session_id: str) -> bool:
        """Check whether Claude CLI already has a transcript for this session.

        The CLI lays out sessions at
        ``$HOME/.claude/projects/<cwd-encoded>/<session_id>.jsonl`` where
        `<cwd-encoded>` is the absolute cwd path with `/` replaced by `-`.
        We only need a positive existence check; the CLI itself owns the
        file format.
        """
        cwd_str = self.cwd or os.getcwd()
        # /home/ubuntu/discord-bot → -home-ubuntu-discord-bot
        encoded = cwd_str.replace("/", "-")
        candidate = Path(
            os.path.expanduser("~/.claude/projects")
        ) / encoded / f"{session_id}.jsonl"
        return candidate.is_file()

    async def _launch(self, session_id: str, *, resume: bool) -> asyncio.subprocess.Process:
        cmd = [
            "claude",
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--model", self.model,
        ]
        cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
        # Credential isolation (2026-06-01 incident ②): this CLI shares the
        # bot's process env, so without an override it reads/writes the SAME
        # ~/.claude/.credentials.json as the bot's direct-API OAuth manager.
        # The CLI rotates that file on its own schedule, invalidating the
        # refresh token the bot holds in memory → `invalid_grant` (18:52 rotate
        # → 18:53 invalid_grant). Point the emotion CLI at its OWN config dir so
        # the two credential chains can never collide.
        #
        # Activation is gated on that dir already holding credentials: it needs
        # a one-time `CLAUDE_CONFIG_DIR=<dir> claude login` (see
        # docs/2026-06-01-claude-api-stall-incident.md). Until then we keep the
        # shared dir and warn loudly rather than silently breaking the (non-
        # critical) emotion engine on deploy — the isolation switches on by
        # itself the moment that login is done.
        env = dict(os.environ)
        emotion_cfg_dir = os.environ.get(
            "EMOTION_CLAUDE_CONFIG_DIR", "/home/ubuntu/.claude-emotion"
        )
        if os.path.exists(os.path.join(emotion_cfg_dir, ".credentials.json")):
            env["CLAUDE_CONFIG_DIR"] = emotion_cfg_dir
        else:
            log.warning(
                "emotion CLI: %s has no .credentials.json — falling back to the "
                "SHARED ~/.claude credential. OAuth self-poisoning risk persists "
                "until you run: CLAUDE_CONFIG_DIR=%s claude login",
                emotion_cfg_dir, emotion_cfg_dir,
            )
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.cwd,
        )

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Continuously read stderr to keep the kernel pipe buffer drained."""
        if proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                # Surface non-empty stderr at debug; emotion runtime is
                # noisy at warn already, and most CLI stderr is benign
                # ("ts log line written" style noise).
                msg = line.decode("utf-8", errors="replace").rstrip()
                if msg:
                    log.debug("daemon %s stderr: %s", self.session_key, msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("daemon %s stderr drain ended: %s", self.session_key, exc)

    async def _terminate(self, timeout: float) -> None:
        proc = self._proc
        self._proc = None
        drain_task = self._stderr_drain_task
        self._stderr_drain_task = None
        if proc is None or proc.returncode is not None:
            if drain_task is not None and not drain_task.done():
                drain_task.cancel()
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("daemon %s: did not exit in %ss, killing",
                        self.session_key, timeout)
            await self._kill(proc)
        if drain_task is not None and not drain_task.done():
            drain_task.cancel()
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):
                pass

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        try:
            await proc.wait()
        except Exception:
            pass
