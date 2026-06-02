"""Claude Code SDK client — free-code QueryEngine pattern.

No keyword routing: model decides tool usage. Retry with exponential backoff
and error classification (free-code/services/api/withRetry.ts).
"""

import asyncio
import logging
import os
import re
import tempfile
import time

try:
    import psutil  # for orphan claude CLI cleanup
except ImportError:  # pragma: no cover
    psutil = None

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ResultMessage,
    query,
)
from claude_code_sdk.types import (
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from config import (
    ADMIN_MAX_TURNS as _CONFIG_ADMIN_MAX_TURNS,
    ATTACHMENT_DIR,
    CLAUDE_CLI_ENV,
    CLAUDE_MAX_ATTEMPTS,
    CLAUDE_MAX_BACKOFF_S,
    CLAUDE_MAX_PARALLEL,
    CLAUDE_MODEL,
    CLAUDE_TIMEOUT,
    CLAUDE_TRANSIENT_MAX_ATTEMPTS,
    CWD,
    MCP_SERVERS,
    RESTRICTED_ANTHROPIC_API_KEY,
    RESTRICTED_CLAUDE_MAX_PARALLEL,
    RESTRICTED_SYSTEM_PROMPT,
    SETTINGS_PATH,
    STATE_DIR,
    SYSTEM_PROMPT,
)
from services.mcp_selector import (
    get_mcp_servers_by_keys,
    pick_mcp_servers,
)
from services.session import SessionInfo

log = logging.getLogger(__name__)

# Limit concurrent Claude CLI subprocesses. 2GB RAM can't host 32 × ~200MB.
claude_semaphore = asyncio.Semaphore(CLAUDE_MAX_PARALLEL)
# Separate, tighter pool for non-admin calls so a hostile user cannot
# starve the admin concurrency budget (H-3).
restricted_claude_semaphore = asyncio.Semaphore(RESTRICTED_CLAUDE_MAX_PARALLEL)

# Working directory that restricted (non-admin) callers are confined to.
# Anything outside this tree must not be reachable via the Read tool.
RESTRICTED_CWD = ATTACHMENT_DIR

# Defence-in-depth: even with allowed_tools=[] / mcp_servers={}, explicitly
# deny every dangerous built-in. If the SDK adds a new tool that bypasses
# allow-list semantics (it shouldn't, but be paranoid), this list catches it.
_RESTRICTED_DENY_LIST = [
    "Bash", "BashOutput", "KillBash",
    "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep",
    "WebFetch", "WebSearch",
    "Task", "Agent", "SlashCommand", "ExitPlanMode", "TodoWrite",
]

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
# Single source of truth: config.CLAUDE_MODEL (default 4-7, .env-overridable).
MODEL = CLAUDE_MODEL

# ---------------------------------------------------------------------------
# Timeout / turns
# ---------------------------------------------------------------------------
TIMEOUT = CLAUDE_TIMEOUT  # 10 min
RESTRICTED_MAX_TURNS = 8
# Sourced from config so it can be tuned via env. The previous hard-coded
# 999 let one CLI invocation hold ~500MB of node/MCP children for the full
# CLAUDE_TIMEOUT window — see services/claude_sdk.py review.
ADMIN_MAX_TURNS = _CONFIG_ADMIN_MAX_TURNS

# ---------------------------------------------------------------------------
# Stream stall detection thresholds (SRE-H5)
# ---------------------------------------------------------------------------
STREAM_STALL_THRESHOLD_S = 30   # warn after 30 s without a message
STREAM_IDLE_TIMEOUT_S = 120     # raise TimeoutError after 2 min of silence

# ---------------------------------------------------------------------------
# Session resume
# ---------------------------------------------------------------------------
SESSION_TTL_S = int(os.environ.get("SESSION_TTL_S", str(6 * 60 * 60)))
_session_timestamps: dict[int, float] = {}

# ---------------------------------------------------------------------------
# Retry — exponential backoff (free-code: withRetry)
# ---------------------------------------------------------------------------

# M5: per-channel retry budget — at most 3 transient retries per 60 s window.
# Bounded to 500 entries; oldest entry evicted when the limit is reached.
_retry_budget: dict[int, list[float]] = {}
_RETRY_BUDGET_MAX_CHANNELS = 500
_RETRY_BUDGET_WINDOW_S = 60
_RETRY_BUDGET_PER_WINDOW = 3

BASE_BACKOFF_S = 3
MAX_BACKOFF_S = CLAUDE_MAX_BACKOFF_S
DEFAULT_MAX_ATTEMPTS = CLAUDE_MAX_ATTEMPTS
TRANSIENT_MAX_ATTEMPTS = CLAUDE_TRANSIENT_MAX_ATTEMPTS

# ---------------------------------------------------------------------------
# Error classification (free-code: shouldRetry + getAssistantMessageFromError)
# ---------------------------------------------------------------------------
_RATE_LIMIT_PATTERNS = ("rate_limit", "rate-limit", "429", "too many requests", "ratelimiterror")
_OVERLOAD_PATTERNS = ("overloaded", "529", "server_overload")
_AUTH_PATTERNS = ("authentication failed", "401 unauthorized", "403 forbidden", "invalid api key", "token revoked", "not logged in")
_CONTEXT_PATTERNS = ("too long", "token limit", "exceed context limit", "max_tokens exceed")
_MAX_TURNS_PATTERNS = ("max_turns", "max turns", "turn limit")


def _classify_error(err_str: str, stderr_tail: str) -> str:
    combined = f"{err_str}\n{stderr_tail}".lower()
    if any(p in combined for p in _RATE_LIMIT_PATTERNS):
        return "rate_limit"
    if any(p in combined for p in _OVERLOAD_PATTERNS):
        return "overloaded"
    if any(p in combined for p in _AUTH_PATTERNS):
        return "auth"
    if any(p in combined for p in _CONTEXT_PATTERNS):
        return "context_overflow"
    if any(p in combined for p in _MAX_TURNS_PATTERNS):
        return "max_turns"
    # exit code 1 with partial response = likely max_turns or completed with tool use
    if "exit code 1" in combined.lower():
        return "cli_exit"
    return "unknown"


def _error_message(error_type: str) -> str:
    return {
        "rate_limit": "⏳ APIのレート制限に達しました。少し待ってから再試行してください。",
        "overloaded": "🔄 サーバーが混雑しています。しばらく待ってから再試行してください。",
        "auth": "🔑 認証エラーが発生しました。管理者に連絡してください。",
        "context_overflow": "📝 メッセージが長すぎます。短くして送り直してください。",
        "max_turns": "🔄 ターン上限に達しました。もう一度話しかけてください。",
        "cli_exit": "⚠️ 処理が中断されました。もう一度試してください。",
    }.get(error_type, "❌ 内部エラーが発生しました。再試行してください。")


def _is_transient(error_type: str) -> bool:
    return error_type in ("rate_limit", "overloaded")


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def _is_session_fresh(channel_id: int) -> bool:
    ts = _session_timestamps.get(channel_id)
    if ts is None:
        return False
    return (time.monotonic() - ts) < SESSION_TTL_S


def _record_session_time(channel_id: int) -> None:
    _session_timestamps[channel_id] = time.monotonic()


def _clear_session_time(channel_id: int) -> None:
    _session_timestamps.pop(channel_id, None)


# ---------------------------------------------------------------------------
# Stderr capture
# ---------------------------------------------------------------------------
def _read_stderr_buffer(fileobj) -> str:
    """Read captured stderr from a SpooledTemporaryFile."""
    if fileobj is None:
        return ""
    try:
        fileobj.flush()
        fileobj.seek(0)
        raw = fileobj.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return raw[-50000:] if len(raw) > 50000 else raw
    except Exception:
        return ""


def _read_stderr_file(path: str | None) -> str:
    """Legacy helper kept for tests — read a stderr file from disk by path."""
    if not path:
        return ""
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
        return content[-10000:] if len(content) > 10000 else content
    except Exception:
        return ""


def _redact_secrets(text: str) -> str:
    """Remove API keys / auth tokens from text before writing to disk or logs."""
    # ANTHROPIC_API_KEY=xxx or ANTHROPIC_AUTH_TOKEN=xxx
    text = re.sub(r"(ANTHROPIC_(?:API|AUTH)_(?:KEY|TOKEN))=\S+", r"\1=***", text)
    # Raw sk-ant-... bearer tokens
    text = re.sub(r"sk-ant-[A-Za-z0-9_-]+", "sk-ant-***", text)
    return text


def _cleanup_stderr(fileobj, path: str | None = None, *, keep: bool = False) -> None:
    try:
        if fileobj and not fileobj.closed:
            fileobj.close()
    except Exception:
        pass
    # Legacy path cleanup (no longer used since we switched to SpooledTemporaryFile)
    if keep or not path:
        return
    try:
        if os.path.isfile(path):
            os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Orphan claude CLI cleanup (free-code: withProcessGroup)
# ---------------------------------------------------------------------------
# Substring patterns matched against descendant cmdlines so we can also
# clean up MCP child processes that the Claude CLI spawned. Without this,
# `mcp-server-memory`, `playwright/mcp`, brave-search-mcp etc. can stay
# orphaned (we observed `memory-service` reaching SIGKILL during cleanup).
_MCP_CMDLINE_PATTERNS = (
    "claude-code",
    "/claude/bin/",
    "mcp-server-",
    "@modelcontextprotocol/server-",
    "@playwright/mcp",
    "playwright-mcp",
    "brave-search-mcp",
    "@upstash/context7-mcp",
    "context7-mcp",
    "x-scraper/mcp_server.py",
    "scholarly-mcp/server.py",
    "news-curator/mcp_server.py",
    "papermcp",
    "tavily-mcp",
)


def kill_orphan_claude_cli() -> int:
    """SIGTERM → SIGKILL any descendant claude CLI / MCP processes. Returns count killed."""
    if psutil is None:
        return 0
    killed = 0
    try:
        me = psutil.Process(os.getpid())
        targets = []
        for child in me.children(recursive=True):
            try:
                name = (child.name() or "").lower()
                cmdline = " ".join(child.cmdline() or []).lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            # Match the Node.js `claude` CLI itself …
            if name == "claude":
                targets.append(child)
                continue
            # … and any MCP child process whose cmdline mentions a known
            # MCP entrypoint. This is intentionally substring-based because
            # cmdlines vary (npx wrappers, .venv pythons, etc.).
            if any(pat in cmdline for pat in _MCP_CMDLINE_PATTERNS):
                targets.append(child)
        if not targets:
            return 0
        for p in targets:
            try:
                p.terminate()
            except psutil.NoSuchProcess:
                pass
        gone, alive = psutil.wait_procs(targets, timeout=2)
        for p in alive:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        killed = len(targets)
    except Exception as exc:
        log.debug("kill_orphan_claude_cli failed: %s", exc)
    return killed


# ---------------------------------------------------------------------------
# Discord status hint — summarize tool input for the status message
# ---------------------------------------------------------------------------
def _tool_input_hint(name: str, inp: dict | None) -> str:
    """Return a short human-readable hint for the Discord status message."""
    if not inp:
        return ""
    try:
        n = name.split("__")[-1] if "__" in name else name  # strip mcp prefix
        if n in ("Bash", "BashOutput"):
            return inp.get("command", "")
        if n in ("Read",):
            return inp.get("file_path", "")
        if n in ("Edit", "Write", "MultiEdit"):
            return inp.get("file_path", "")
        if n in ("Glob",):
            return inp.get("pattern", "")
        if n in ("Grep",):
            return inp.get("pattern", "")
        if n in ("WebFetch", "navigate"):
            return inp.get("url", "")
        if n in ("WebSearch", "brave_web_search", "brave_local_search"):
            return inp.get("query", "")
        if n in ("search_x",):
            return inp.get("query", "")
        if n in ("Agent",):
            return inp.get("description", "")
        # Generic: show first string value
        for v in inp.values():
            if isinstance(v, str) and v:
                return v
        return ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Safe status callback wrapper (SRE-C5)
# ---------------------------------------------------------------------------
async def _safe_status(cb, msg: str) -> None:
    """Invoke status_callback without letting exceptions propagate."""
    if cb is None:
        return
    try:
        await cb(msg)
    except Exception as e:
        log.debug("status_callback failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Main entry point — free-code pattern: no keyword routing
# ---------------------------------------------------------------------------
async def call_with_tools(
    prompt: str,
    channel_id: int,
    sessions: dict[int, "SessionInfo"],
    status_callback=None,
    restricted: bool = False,
    raw_user_text: str = "",
    is_thread: bool = False,
    has_image_attachment: bool = False,
    author_label: str = "",
) -> tuple[str, "SessionInfo | None", bool]:
    """Call Claude via SDK. Model decides tool usage (free-code QueryEngine pattern)."""
    # H-3: pick the correct concurrency slot so non-admins can't starve
    # the admin pool.
    sema = restricted_claude_semaphore if restricted else claude_semaphore
    async with sema:
        if restricted:
            # Non-admin callers operate on a strict **allow-list**:
            #   - No MCP servers at all (playwright / brave / github / x-scraper
            #     would otherwise let a user read local files or exfiltrate
            #     data via arbitrary URLs).
            #   - `permission_mode="default"` (not bypassPermissions) so the CLI
            #     itself refuses tool calls that aren't explicitly allowed.
            #   - Only Read, and only if the caller explicitly signaled that
            #     image attachments are present. The previous string-match on
            #     the prompt was bypassable by a non-admin user typing the
            #     literal sentinel "Use the Read tool to view this image".
            #   - cwd isolated to the shared attachment tmpdir to limit where a
            #     successful Read can reach.
            #   - If BOT_RESTRICTED_ANTHROPIC_API_KEY is configured, the CLI
            #     subprocess inherits that key via env — so non-admin calls
            #     burn a dedicated, separately-billed quota rather than the
            #     owner's OAuth subscription.
            # Always inject CLAUDE_CLI_ENV (ToolSearch off) so the CLI's
            # deferred-tool exit path can't strand us with result="". See
            # docs/2026-05-13-tool-deferred-incident.md for the root cause.
            restricted_env: dict[str, str] = {**CLAUDE_CLI_ENV}
            if RESTRICTED_ANTHROPIC_API_KEY:
                restricted_env["ANTHROPIC_API_KEY"] = RESTRICTED_ANTHROPIC_API_KEY
                # Unset OAuth so the CLI doesn't prefer it over the API key.
                restricted_env["ANTHROPIC_AUTH_TOKEN"] = ""
            # H3: allow_tools=[] unconditionally. The CLI Read tool does not
            # enforce cwd, so a non-admin user could read absolute paths outside
            # the attachment dir. Image content is passed to the model via the
            # prompt (<attached_images> block in PromptBuilder) so Read is not
            # needed. Future work: base64-embed image bytes in the prompt directly.
            if has_image_attachment:
                log.warning(
                    "ch=%d restricted call has image attachment; images won't be "
                    "processed by Read tool in restricted mode (H3)",
                    channel_id,
                )
            options = ClaudeCodeOptions(
                system_prompt=RESTRICTED_SYSTEM_PROMPT,
                permission_mode="default",
                cwd=RESTRICTED_CWD,
                max_turns=RESTRICTED_MAX_TURNS,
                model=MODEL,
                allowed_tools=[],
                disallowed_tools=_RESTRICTED_DENY_LIST,
                mcp_servers={},
                settings=None,  # don't inherit admin-level settings
                env=restricted_env,
            )
        else:
            # Admin mode: model decides what tools to use, but we hand the
            # SDK only the MCP servers this request is likely to need so we
            # don't pay the chromium / scholarly / web-mcp startup cost on
            # every reply.
            # M3: only use raw_user_text (never fall back to the full assembled
            # prompt). The prompt may contain forwarded message content or Mem0
            # memories that include MCP keywords, allowing injection attacks.
            chosen_mcp = pick_mcp_servers(raw_user_text or "")
            options = ClaudeCodeOptions(
                system_prompt=SYSTEM_PROMPT,
                permission_mode="bypassPermissions",
                cwd=CWD,
                max_turns=ADMIN_MAX_TURNS,
                model=MODEL,
                mcp_servers=chosen_mcp,
                settings=SETTINGS_PATH,
                env={**CLAUDE_CLI_ENV},
            )

        # Session resume — enabled for threads only. MCP server config is
        # stored per session so we can restore the exact same set on resume.
        # For non-threads / non-admin, resume stays disabled.
        if not restricted:
            info = sessions.get(channel_id)
            if info and _is_session_fresh(channel_id):
                needed_keys = set(chosen_mcp.keys())
                stored_keys = set(info.mcp_server_keys)
                if needed_keys <= stored_keys:
                    options.resume = info.session_id
                    options.mcp_servers = get_mcp_servers_by_keys(info.mcp_server_keys)
                    log.info("Resuming session %s for ch=%d (mcp=%s)",
                             info.session_id[:12], channel_id, stored_keys)
                else:
                    merged = stored_keys | needed_keys
                    info.mcp_server_keys = sorted(merged)
                    options.resume = info.session_id
                    options.mcp_servers = get_mcp_servers_by_keys(info.mcp_server_keys)
                    log.info("MCP merge for ch=%d: %s + %s → %s, resuming %s",
                             channel_id, sorted(stored_keys),
                             sorted(needed_keys - stored_keys),
                             info.mcp_server_keys, info.session_id[:12])
            else:
                options.resume = None
        else:
            options.resume = None

        _actually_resumed = options.resume is not None
        response_text: str | None = None
        # Defence-in-depth: if ResultMessage.result comes back empty (e.g. the
        # CLI's deferred-tool exit path), fall back to the last TextBlock we
        # observed in the stream so we don't ship "(No response from Claude)".
        stream_text: str = ""
        session_id: str | None = None
        # Track the MCP keys used for this invocation (for SessionInfo)
        active_mcp_keys: list[str] = list(options.mcp_servers.keys()) if options.mcp_servers else []
        max_attempts = DEFAULT_MAX_ATTEMPTS

        # Phase F3: allocate the stderr buffer once outside the retry loop;
        # truncate it at the start of each attempt and close it exactly
        # once after the loop. Previously the SpooledTemporaryFile was
        # created inside the loop body and a per-iteration `finally`
        # cleaned it up, so each retry re-allocated state.
        stderr_fileobj = tempfile.SpooledTemporaryFile(
            max_size=1024 * 1024, mode="w+b", prefix="claude_stderr_"
        )
        options.debug_stderr = stderr_fileobj
        # --strict-mcp-config: only use the MCP servers we explicitly pass via
        # mcp_servers=. Without this flag the CLI also loads the user-level
        # ~/.claude.json mcpServers, which contains stale SSE endpoints
        # (brave-search/papermcp/browser-rendering on localhost:801x) and
        # Anthropic builtin remote MCPs (claude.ai Drive/Gmail/Calendar) that
        # fail with mcp_unauthorized_no_token and push the CLI into exit-code-1.
        # This affects only the subprocess we spawn; `claude` invoked directly
        # by the user (e.g. 先輩 in a terminal) still sees the global config.
        options.extra_args = {
            "debug-to-stderr": None,
            "strict-mcp-config": None,
        }

        for attempt in range(TRANSIENT_MAX_ATTEMPTS):
            if attempt >= max_attempts:
                break

            # M5: per-channel retry budget — cap transient retries to
            # _RETRY_BUDGET_PER_WINDOW within a rolling 60 s window so a
            # flapping upstream cannot drive unbounded retries per channel.
            if attempt > 0:
                now = time.monotonic()
                recent = [
                    t for t in _retry_budget.get(channel_id, [])
                    if now - t < _RETRY_BUDGET_WINDOW_S
                ]
                if len(recent) >= _RETRY_BUDGET_PER_WINDOW:
                    log.warning(
                        "Retry budget exhausted for ch=%d, giving up", channel_id
                    )
                    _cleanup_stderr(stderr_fileobj)
                    return _error_message("unknown"), None, False
                recent.append(now)
                _retry_budget[channel_id] = recent
                # LRU eviction: drop oldest entries if dict too large
                if len(_retry_budget) > _RETRY_BUDGET_MAX_CHANNELS:
                    oldest_key = next(iter(_retry_budget))
                    del _retry_budget[oldest_key]

            stderr_path: str | None = None
            try:
                # Reset the reused stderr buffer to empty for this attempt.
                stderr_fileobj.seek(0)
                stderr_fileobj.truncate()

                timeout = TIMEOUT
                if attempt == 0 and options.resume:
                    timeout = min(TIMEOUT, 90)

                log.info("SDK attempt %d/%d (timeout=%ds, resume=%s)",
                         attempt + 1, max_attempts, timeout, bool(options.resume))

                turn_count = 0
                tool_count = 0
                t_start = time.monotonic()

                # SRE-H5: shared mutable cell for watchdog. Re-allocated on
                # every attempt so flags from a prior attempt cannot leak
                # into the next retry (review H-SEC-3 robustness note).
                last_msg_time_cell: list[float] = [time.monotonic()]
                _stall_warned: list[bool] = [False]
                _stream_idle: list[bool] = [False]
                _parent_task = asyncio.current_task()

                async def _stall_watchdog() -> None:
                    """Poll last_msg_time every 10 s; cancel parent on idle timeout."""
                    while True:
                        await asyncio.sleep(10)
                        idle = time.monotonic() - last_msg_time_cell[0]
                        if idle > STREAM_IDLE_TIMEOUT_S:
                            log.warning(
                                "[ch=%d] stream idle for %.0fs, cancelling",
                                channel_id, idle,
                            )
                            _stream_idle[0] = True
                            if _parent_task is not None:
                                _parent_task.cancel("stream idle")
                            return
                        if idle > STREAM_STALL_THRESHOLD_S and not _stall_warned[0]:
                            log.warning(
                                "[ch=%d] stream stall: no message for %.0fs",
                                channel_id, idle,
                            )
                            _stall_warned[0] = True

                _watchdog_task: asyncio.Task | None = None
                try:
                    _watchdog_task = asyncio.create_task(_stall_watchdog())

                    # SRE-C1: catch CancelledError to kill orphan subprocess
                    try:
                        async with asyncio.timeout(timeout):
                            async for msg in query(prompt=prompt, options=options):
                                last_msg_time_cell[0] = time.monotonic()
                                _stall_warned[0] = False

                                # --- SystemMessage ---
                                if isinstance(msg, SystemMessage):
                                    log.info("[ch=%d] 📡 system: subtype=%s",
                                             channel_id, msg.subtype)

                                # --- AssistantMessage (thinking / tool_use / text) ---
                                elif isinstance(msg, AssistantMessage):
                                    for block in msg.content:
                                        if isinstance(block, ThinkingBlock) and block.thinking:
                                            summary = block.thinking[:200].replace("\n", " ")
                                            # F1: demote per-block stream logs to DEBUG.
                                            # A 30-turn task with 5 tools/turn produced
                                            # 150+ log.info lines on the hot path; the
                                            # default logger writes synchronously.
                                            log.debug("[ch=%d] 💭 thinking: %s%s",
                                                      channel_id, summary,
                                                      "…" if len(block.thinking) > 200 else "")
                                            await _safe_status(status_callback, f"💭 {summary}")

                                        elif isinstance(block, ToolUseBlock):
                                            tool_count += 1
                                            # Summarize input (truncate large payloads)
                                            inp = str(block.input) if block.input else ""
                                            inp_summary = inp[:200] + ("…" if len(inp) > 200 else "")
                                            # F1: per-block log → DEBUG (was INFO).
                                            log.debug("[ch=%d] 🔧 tool_use #%d: %s(%s)",
                                                      channel_id, tool_count, block.name, inp_summary)
                                            # Discord status: tool name + short input hint
                                            hint = _tool_input_hint(block.name, block.input)
                                            await _safe_status(
                                                status_callback,
                                                f"🔧 {block.name}" + (f"  `{hint}`" if hint else "")
                                            )

                                        elif isinstance(block, TextBlock) and block.text:
                                            stream_text = block.text
                                            preview = block.text[:150].replace("\n", " ")
                                            # F1: per-block log → DEBUG (was INFO).
                                            log.debug("[ch=%d] 📝 text: %s%s",
                                                      channel_id, preview,
                                                      "…" if len(block.text) > 150 else "")
                                            await _safe_status(status_callback, f"📝 応答を生成中")

                                # --- UserMessage (tool results flowing back) ---
                                elif isinstance(msg, UserMessage):
                                    for block in (msg.content if isinstance(msg.content, list) else []):
                                        if isinstance(block, ToolResultBlock):
                                            status = "❌ error" if block.is_error else "✅ ok"
                                            content_str = str(block.content) if block.content else ""
                                            preview = content_str[:200].replace("\n", " ")
                                            # F1: per-block log → DEBUG (was INFO).
                                            log.debug("[ch=%d] 📨 tool_result [%s]: %s%s",
                                                      channel_id, status, preview,
                                                      "…" if len(content_str) > 200 else "")
                                            if block.is_error:
                                                await _safe_status(status_callback, f"❌ ツールエラー")
                                            else:
                                                await _safe_status(status_callback, f"📨 結果を受信")

                                # --- ResultMessage (final) ---
                                elif isinstance(msg, ResultMessage):
                                    result_text = getattr(msg, "result", None)
                                    if not result_text and stream_text:
                                        log.warning(
                                            "[ch=%d] ResultMessage.result empty (subtype=%s); "
                                            "falling back to stream TextBlock (%d chars)",
                                            channel_id,
                                            getattr(msg, "subtype", "?"),
                                            len(stream_text),
                                        )
                                        result_text = stream_text
                                    response_text = result_text or ""
                                    session_id = msg.session_id if hasattr(msg, "session_id") else None
                                    elapsed = time.monotonic() - t_start
                                    cost_str = f"${msg.total_cost_usd:.4f}" if msg.total_cost_usd else "n/a"
                                    usage_in = msg.usage.get("input_tokens", 0) if msg.usage else 0
                                    usage_out = msg.usage.get("output_tokens", 0) if msg.usage else 0
                                    log.info(
                                        "[ch=%d] ✅ done: turns=%d tools=%d "
                                        "elapsed=%.1fs api=%.1fs cost=%s "
                                        "tokens_in=%d tokens_out=%d error=%s",
                                        channel_id, msg.num_turns, tool_count,
                                        elapsed, msg.duration_api_ms / 1000,
                                        cost_str, usage_in, usage_out, msg.is_error,
                                    )
                    except asyncio.CancelledError:
                        # SRE-C1: kill orphan subprocess before propagating cancellation
                        killed = kill_orphan_claude_cli()
                        if killed:
                            log.warning(
                                "Killed %d orphan claude CLI process(es) on CancelledError",
                                killed,
                            )
                        raise
                finally:
                    # SRE-H5: always cancel watchdog when the stream block exits
                    if _watchdog_task is not None and not _watchdog_task.done():
                        _watchdog_task.cancel()
                        try:
                            await _watchdog_task
                        except (asyncio.CancelledError, TimeoutError):
                            pass

                break  # success

            except TimeoutError:
                stderr_tail = _read_stderr_buffer(stderr_fileobj)
                # Phase F3: don't close the buffer here — it's reused on
                # `continue` and closed once after the loop (or before
                # an early `return` below).
                if stderr_tail:
                    log.warning("SDK timeout (attempt %d) stderr tail:\n%s", attempt + 1, stderr_tail)

                # Ensure the CLI subprocess doesn't stay orphaned holding RAM.
                killed = kill_orphan_claude_cli()
                if killed:
                    log.warning("Killed %d orphan claude CLI process(es) after timeout", killed)

                if attempt == 0 and options.resume:
                    log.warning("Session resume timed out for ch=%d, retrying fresh", channel_id)
                    if channel_id in sessions:
                        del sessions[channel_id]
                    _clear_session_time(channel_id)
                    options.resume = None
                    await asyncio.sleep(BASE_BACKOFF_S)
                    continue
                _cleanup_stderr(stderr_fileobj)
                return f"⏰ タイムアウトしました（{TIMEOUT // 60}分制限）", None, _actually_resumed

            except Exception as e:
                stderr_tail = _read_stderr_buffer(stderr_fileobj)
                # See TimeoutError handler — single cleanup at loop exit.

                err_str = str(e)[:500]
                error_type = _classify_error(err_str, stderr_tail)

                # Save stderr for debugging — only on the first few attempts to
                # avoid flooding .state/ with hundreds of files under retry storms.
                # H2: redact secrets and write with mode 0o600 to prevent
                # ANTHROPIC_API_KEY / sk-ant-* from leaking to world-readable files.
                if stderr_tail and attempt < 3:
                    debug_path = os.path.join(STATE_DIR, f"stderr_ch{channel_id}.log")
                    fd = None
                    try:
                        fd = os.open(
                            debug_path,
                            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                            0o600,
                        )
                        with os.fdopen(fd, "w", encoding="utf-8") as fh:
                            fh.write(_redact_secrets(stderr_tail))
                        fd = None  # os.fdopen took ownership
                        log.info("Saved stderr to %s", debug_path)
                    except Exception:
                        if fd is not None:
                            try:
                                os.close(fd)
                            except OSError:
                                pass

                if stderr_tail:
                    log.error("SDK error (attempt %d, type=%s): %s\nstderr tail:\n%s",
                              attempt + 1, error_type, err_str, stderr_tail[-3000:])
                else:
                    log.error("SDK error (attempt %d, type=%s): %s",
                              attempt + 1, error_type, err_str, exc_info=True)

                # Extend retries for transient errors
                if _is_transient(error_type) and max_attempts < TRANSIENT_MAX_ATTEMPTS:
                    max_attempts = TRANSIENT_MAX_ATTEMPTS
                    log.info("Transient error (%s), extending to %d attempts", error_type, max_attempts)

                # Non-retryable errors
                if error_type in ("auth", "context_overflow", "max_turns", "cli_exit"):
                    if error_type == "cli_exit" and response_text:
                        log.info("CLI exited with partial response, returning it")
                        break
                    if error_type == "cli_exit" and options.resume:
                        log.warning("Session resume failed (cli_exit) for ch=%d, retrying fresh", channel_id)
                        if channel_id in sessions:
                            del sessions[channel_id]
                        _clear_session_time(channel_id)
                        options.resume = None
                        await asyncio.sleep(BASE_BACKOFF_S)
                        continue
                    if error_type == "context_overflow":
                        if channel_id in sessions:
                            del sessions[channel_id]
                        _clear_session_time(channel_id)
                        _cleanup_stderr(stderr_fileobj)
                        return _error_message(error_type), None, False
                    # auth / max_turns: session state is valid, keep it
                    _cleanup_stderr(stderr_fileobj)
                    return _error_message(error_type), None, _actually_resumed

                # Transient errors exhausted — keep session alive
                if attempt >= max_attempts - 1:
                    _cleanup_stderr(stderr_fileobj)
                    return _error_message(error_type), None, _actually_resumed

                backoff = min(BASE_BACKOFF_S * (2 ** attempt), MAX_BACKOFF_S)
                log.info("Retrying in %ds (type=%s, attempt %d/%d)", backoff, error_type, attempt + 1, max_attempts)
                await _safe_status(status_callback, f"🔄 リトライ中... ({attempt + 1}/{max_attempts})")
                await asyncio.sleep(backoff)

        # Phase F3: single cleanup after the retry loop exits normally.
        # Each `return` path inside the loop above already cleaned up before
        # returning, so we only need this for the success-break case.
        _cleanup_stderr(stderr_fileobj)

        if response_text is None:
            return "(No response from Claude)", None, False

        result_info: SessionInfo | None = None
        if session_id:
            _record_session_time(channel_id)
            result_info = SessionInfo(session_id=session_id, mcp_server_keys=active_mcp_keys)

        log.info("Response via %s (restricted=%s) len=%d",
                 MODEL, restricted, len(response_text))
        return response_text.strip() or "(No response from Claude)", result_info, _actually_resumed


# ---------------------------------------------------------------------------
# PR7 backend dispatcher (refactor/claude-api-unify)
# ---------------------------------------------------------------------------
# When CLAUDE_BACKEND=api, call_with_tools / _is_session_fresh /
# kill_orphan_claude_cli are routed to services.claude_api instead of the
# CLI-driven implementation above. PR8 flips the default to "api". PR9
# deletes claude_sdk.py outright.
#
# All three symbols must dispatch together (Codex v3 #8): if only
# call_with_tools is rerouted, the API path's _session_timestamps and
# MCP-child set are stranded from bot.py's _is_session_fresh and
# main_async.py's shutdown cleanup.
_legacy_call_with_tools = call_with_tools
_legacy_is_session_fresh = _is_session_fresh
_legacy_kill_orphan_claude_cli = kill_orphan_claude_cli


def _backend() -> str:
    # PR8 (refactor/claude-api-unify): flipped default from "sdk" to "api".
    # Rollback: set CLAUDE_BACKEND=sdk in the environment.
    return os.environ.get("CLAUDE_BACKEND", "api").lower()


async def call_with_tools(*args, **kwargs):
    if _backend() == "api":
        from services.claude_api import call_with_tools as _impl
        return await _impl(*args, **kwargs)
    return await _legacy_call_with_tools(*args, **kwargs)


def _is_session_fresh(channel_id: int) -> bool:
    if _backend() == "api":
        from services.claude_api import _is_session_fresh as _impl
        return _impl(channel_id)
    return _legacy_is_session_fresh(channel_id)


def kill_orphan_claude_cli() -> int:
    """Run BOTH cleanup paths during the migration window.

    Even when CLAUDE_BACKEND=api, the SDK-side cmdline scan stays useful
    because a previous SDK-mode run may have left CLI children behind.
    """
    n = _legacy_kill_orphan_claude_cli()
    if _backend() == "api":
        from services.claude_api import kill_orphan_claude_cli as _impl
        n += _impl()
    return n
