"""Claude CLI wrapper for emotion/daily subsystem.

All LLM calls go through `claude_cli()` which handles:
  - Session resume vs first-run detection
  - subprocess timeout
  - JSON output parsing
  - Fail-safe on broken sessions (auto-reset)
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from services.emotion.session import load_sessions, reset_session

log = logging.getLogger(__name__)

CLI_TIMEOUT = 90  # seconds


def claude_cli(
    prompt: str,
    *,
    session_key: str,
    state_dir: str | Path,
    model: str = "sonnet",
    timeout: int = CLI_TIMEOUT,
    cwd: str | Path | None = None,
) -> dict | None:
    """Call Claude CLI with session resume. Returns parsed JSON or None on failure.

    On the first call for a session_key the session ID won't exist in Claude's
    internal store, so `--resume` will fail. We detect this and fall back to
    `--session-id` (create mode). On subsequent calls `--resume` works.

    Returns the parsed inner JSON (from the model's text output), or None if
    anything goes wrong.
    """
    state_dir = Path(state_dir)
    sessions = load_sessions(state_dir)
    session_id = sessions[session_key]

    # Try --resume first
    result = _run_cli(prompt, session_id, resume=True, model=model,
                      timeout=timeout, cwd=cwd)

    if result is None:
        # Resume failed — could be first run or corrupted session.
        # Try --session-id (create new session with this ID).
        log.info("cli: --resume failed for %s, trying --session-id", session_key)
        result = _run_cli(prompt, session_id, resume=False, model=model,
                          timeout=timeout, cwd=cwd)

    if result is None:
        # Both failed — reset session ID and try once more with a fresh ID
        log.warning("cli: both resume and session-id failed for %s, resetting", session_key)
        session_id = reset_session(state_dir, session_key)
        result = _run_cli(prompt, session_id, resume=False, model=model,
                          timeout=timeout, cwd=cwd)

    return result


def _run_cli(
    prompt: str,
    session_id: str,
    *,
    resume: bool,
    model: str,
    timeout: int,
    cwd: str | Path | None,
) -> dict | None:
    """Low-level subprocess call. Returns parsed JSON dict or None."""
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model]
    if resume:
        cmd.extend(["--resume", session_id])
    else:
        cmd.extend(["--session-id", session_id])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired:
        log.warning("cli: timeout after %ds (session=%s)", timeout, session_id[:8])
        return None
    except FileNotFoundError:
        log.error("cli: claude not found on PATH")
        return None

    if proc.returncode != 0:
        log.warning("cli: exit=%d stderr=%s", proc.returncode, proc.stderr[:300])
        return None

    return _parse_output(proc.stdout)


def _parse_output(stdout: str) -> dict | None:
    """Extract the model's JSON response from Claude CLI's JSON wrapper."""
    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("cli: outer JSON parse failed: %s", stdout[:200])
        return None

    if not isinstance(outer, dict):
        return None

    inner = outer.get("result", "")
    if not inner:
        log.warning("cli: no .result field")
        return None

    inner = inner.strip()
    # Strip code fences
    if inner.startswith("```"):
        parts = inner.split("```", 2)
        if len(parts) >= 2:
            inner = parts[1]
        if inner.startswith("json"):
            inner = inner[4:]
        inner = inner.rsplit("```", 1)[0].strip()

    try:
        data = json.loads(inner)
    except json.JSONDecodeError:
        log.warning("cli: inner JSON parse failed: %s", inner[:300])
        return None

    if not isinstance(data, dict):
        log.warning("cli: inner JSON is not a dict")
        return None

    return data
