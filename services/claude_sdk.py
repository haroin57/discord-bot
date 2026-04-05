"""Claude Code SDK client — free-code QueryEngine pattern.

No keyword routing: model decides tool usage. Retry with exponential backoff
and error classification (free-code/services/api/withRetry.ts).
"""

import asyncio
import logging
import os
import tempfile
import time

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ResultMessage,
    query,
)
from claude_code_sdk.types import ThinkingBlock, ToolUseBlock

from config import CLAUDE_TIMEOUT, CWD, MCP_SERVERS, SETTINGS_PATH, SYSTEM_PROMPT

log = logging.getLogger(__name__)

claude_semaphore = asyncio.Semaphore(1)  # max 1 CLI at a time

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
MODEL = "claude-opus-4-6"

# ---------------------------------------------------------------------------
# Timeout / turns
# ---------------------------------------------------------------------------
TIMEOUT = CLAUDE_TIMEOUT  # 10 min
RESTRICTED_MAX_TURNS = 8
ADMIN_MAX_TURNS = 25

# ---------------------------------------------------------------------------
# Session resume
# ---------------------------------------------------------------------------
SESSION_TTL_S = 30 * 60
_session_timestamps: dict[int, float] = {}

# ---------------------------------------------------------------------------
# Retry — exponential backoff (free-code: withRetry)
# ---------------------------------------------------------------------------
BASE_BACKOFF_S = 3
MAX_BACKOFF_S = 30
DEFAULT_MAX_ATTEMPTS = 3
TRANSIENT_MAX_ATTEMPTS = 5

# ---------------------------------------------------------------------------
# Error classification (free-code: shouldRetry + getAssistantMessageFromError)
# ---------------------------------------------------------------------------
_RATE_LIMIT_PATTERNS = ("rate_limit", "rate-limit", "429", "too many requests", "ratelimiterror")
_OVERLOAD_PATTERNS = ("overloaded", "529", "server_overload")
_AUTH_PATTERNS = ("authentication failed", "401 unauthorized", "403 forbidden", "invalid api key", "token revoked", "not logged in")
_CONTEXT_PATTERNS = ("too long", "token limit", "exceed context limit", "max_tokens exceed")


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
    return "unknown"


def _error_message(error_type: str) -> str:
    return {
        "rate_limit": "⏳ APIのレート制限に達しました。少し待ってから再試行してください。",
        "overloaded": "🔄 サーバーが混雑しています。しばらく待ってから再試行してください。",
        "auth": "🔑 認証エラーが発生しました。管理者に連絡してください。",
        "context_overflow": "📝 メッセージが長すぎます。短くして送り直してください。",
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
def _read_stderr_file(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
        return content[-10000:] if len(content) > 10000 else content
    except Exception:
        return ""


def _cleanup_stderr(fileobj, path: str | None) -> None:
    try:
        if fileobj and not fileobj.closed:
            fileobj.close()
    except Exception:
        pass
    try:
        if path and os.path.isfile(path):
            os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main entry point — free-code pattern: no keyword routing
# ---------------------------------------------------------------------------
async def call_with_tools(
    prompt: str,
    channel_id: int,
    sessions: dict[int, str],
    status_callback=None,
    restricted: bool = False,
    raw_user_text: str = "",
) -> tuple[str, str | None]:
    """Call Claude via SDK. Model decides tool usage (free-code QueryEngine pattern)."""
    async with claude_semaphore:
        if restricted:
            has_images = "Use the Read tool to view this image" in prompt
            disallowed = [
                "Bash", "Write", "Edit", "Glob", "Grep",
                "WebFetch", "WebSearch", "Agent",
            ]
            if not has_images:
                disallowed.append("Read")
            options = ClaudeCodeOptions(
                system_prompt=SYSTEM_PROMPT,
                permission_mode="bypassPermissions",
                cwd=CWD,
                max_turns=RESTRICTED_MAX_TURNS,
                model=MODEL,
                disallowed_tools=disallowed,
                mcp_servers=MCP_SERVERS,
                settings=SETTINGS_PATH,
            )
        else:
            # Full mode: model decides what tools to use
            options = ClaudeCodeOptions(
                system_prompt=SYSTEM_PROMPT,
                permission_mode="bypassPermissions",
                cwd=CWD,
                max_turns=ADMIN_MAX_TURNS,
                model=MODEL,
                mcp_servers=MCP_SERVERS,
                settings=SETTINGS_PATH,
            )

        # Session resume — only if fresh
        if channel_id in sessions and _is_session_fresh(channel_id):
            options.resume = sessions[channel_id]
        elif channel_id in sessions:
            log.info("Skipping stale session resume for channel %d", channel_id)

        response_text: str | None = None
        session_id: str | None = None
        max_attempts = DEFAULT_MAX_ATTEMPTS

        for attempt in range(TRANSIENT_MAX_ATTEMPTS):
            if attempt >= max_attempts:
                break

            stderr_fileobj = None
            stderr_path: str | None = None
            try:
                fd, stderr_path = tempfile.mkstemp(prefix="claude_stderr_")
                stderr_fileobj = os.fdopen(fd, "wb")
                options.debug_stderr = stderr_fileobj
                options.extra_args = {"debug-to-stderr": None}

                timeout = TIMEOUT
                if attempt == 0 and options.resume:
                    timeout = min(TIMEOUT, 90)

                log.info("SDK attempt %d/%d (timeout=%ds, resume=%s)",
                         attempt + 1, max_attempts, timeout, bool(options.resume))

                async with asyncio.timeout(timeout):
                    async for msg in query(prompt=prompt, options=options):
                        if isinstance(msg, AssistantMessage) and status_callback:
                            for block in msg.content:
                                if isinstance(block, ThinkingBlock) and block.thinking:
                                    summary = block.thinking[:80].replace("\n", " ")
                                    await status_callback(f"💭 {summary}")
                                elif isinstance(block, ToolUseBlock):
                                    await status_callback(f"🔧 {block.name}")
                        elif isinstance(msg, ResultMessage):
                            response_text = msg.result if hasattr(msg, "result") else str(msg)
                            session_id = msg.session_id if hasattr(msg, "session_id") else None
                break  # success

            except TimeoutError:
                _cleanup_stderr(stderr_fileobj, None)
                stderr_fileobj = None
                stderr_tail = _read_stderr_file(stderr_path)
                if stderr_tail:
                    log.warning("SDK timeout (attempt %d) stderr tail:\n%s", attempt + 1, stderr_tail)

                if attempt == 0 and options.resume:
                    log.warning("Session resume timed out for ch=%d, retrying fresh", channel_id)
                    if channel_id in sessions:
                        del sessions[channel_id]
                    _clear_session_time(channel_id)
                    options.resume = None
                    await asyncio.sleep(BASE_BACKOFF_S)
                    continue
                return f"⏰ タイムアウトしました（{TIMEOUT // 60}分制限）", None

            except Exception as e:
                _cleanup_stderr(stderr_fileobj, None)
                stderr_fileobj = None
                stderr_tail = _read_stderr_file(stderr_path)

                err_str = str(e)[:500]
                error_type = _classify_error(err_str, stderr_tail)

                if stderr_tail:
                    log.error("SDK error (attempt %d, type=%s): %s\nstderr tail:\n%s",
                              attempt + 1, error_type, err_str, stderr_tail)
                else:
                    log.error("SDK error (attempt %d, type=%s): %s",
                              attempt + 1, error_type, err_str, exc_info=True)

                # Extend retries for transient errors
                if _is_transient(error_type) and max_attempts < TRANSIENT_MAX_ATTEMPTS:
                    max_attempts = TRANSIENT_MAX_ATTEMPTS
                    log.info("Transient error (%s), extending to %d attempts", error_type, max_attempts)

                # Non-retryable errors
                if error_type in ("auth", "context_overflow"):
                    return _error_message(error_type), None

                # Clear broken session
                if channel_id in sessions:
                    del sessions[channel_id]
                    log.info("Cleared broken session for channel %d", channel_id)
                _clear_session_time(channel_id)
                options.resume = None

                if attempt >= max_attempts - 1:
                    return _error_message(error_type), None

                backoff = min(BASE_BACKOFF_S * (2 ** attempt), MAX_BACKOFF_S)
                log.info("Retrying in %ds (type=%s, attempt %d/%d)", backoff, error_type, attempt + 1, max_attempts)
                if status_callback:
                    await status_callback(f"🔄 リトライ中... ({attempt + 1}/{max_attempts})")
                await asyncio.sleep(backoff)

            finally:
                _cleanup_stderr(stderr_fileobj, stderr_path)

        if response_text is None:
            return "(No response from Claude)", None

        if session_id:
            _record_session_time(channel_id)

        log.info("Response via %s (restricted=%s)", MODEL, restricted)
        return response_text.strip() or "(No response from Claude)", session_id
