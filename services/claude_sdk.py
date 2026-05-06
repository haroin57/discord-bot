"""Claude Code SDK client — free-code QueryEngine pattern.

No keyword routing: model decides tool usage. Retry with exponential backoff
and error classification (free-code/services/api/withRetry.ts).
"""

import asyncio
import logging
import os
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
    CLAUDE_MAX_ATTEMPTS,
    CLAUDE_MAX_BACKOFF_S,
    CLAUDE_MAX_PARALLEL,
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
    pick_mcp_servers,
)

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
MODEL = "claude-opus-4-6"

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
# Session resume
# ---------------------------------------------------------------------------
SESSION_TTL_S = 30 * 60
_session_timestamps: dict[int, float] = {}

# ---------------------------------------------------------------------------
# Retry — exponential backoff (free-code: withRetry)
# ---------------------------------------------------------------------------
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
            #   - Only Read, and only if the prompt actually references an image.
            #   - cwd isolated to the shared attachment tmpdir to limit where a
            #     successful Read can reach.
            #   - If BOT_RESTRICTED_ANTHROPIC_API_KEY is configured, the CLI
            #     subprocess inherits that key via env — so non-admin calls
            #     burn a dedicated, separately-billed quota rather than the
            #     owner's OAuth subscription.
            has_images = "Use the Read tool to view this image" in prompt
            restricted_env: dict[str, str] = {}
            if RESTRICTED_ANTHROPIC_API_KEY:
                restricted_env["ANTHROPIC_API_KEY"] = RESTRICTED_ANTHROPIC_API_KEY
                # Unset OAuth so the CLI doesn't prefer it over the API key.
                restricted_env["ANTHROPIC_AUTH_TOKEN"] = ""
            options = ClaudeCodeOptions(
                system_prompt=RESTRICTED_SYSTEM_PROMPT,
                permission_mode="default",
                cwd=RESTRICTED_CWD,
                max_turns=RESTRICTED_MAX_TURNS,
                model=MODEL,
                allowed_tools=(["Read"] if has_images else []),
                disallowed_tools=_RESTRICTED_DENY_LIST,
                mcp_servers={},
                settings=None,  # don't inherit admin-level settings
                env=restricted_env,
            )
        else:
            # Admin mode: model decides what tools to use, but we hand the
            # SDK only the MCP servers this request is likely to need so we
            # don't pay the chromium / scholarly / web-mcp startup cost on
            # every reply. raw_user_text falls back to prompt if missing.
            chosen_mcp = pick_mcp_servers(raw_user_text or prompt)
            options = ClaudeCodeOptions(
                system_prompt=SYSTEM_PROMPT,
                permission_mode="bypassPermissions",
                cwd=CWD,
                max_turns=ADMIN_MAX_TURNS,
                model=MODEL,
                mcp_servers=chosen_mcp,
                settings=SETTINGS_PATH,
            )

        # Session resume disabled — MCP server configuration varies per
        # request (pick_mcp_servers selects servers based on user text),
        # but CLI resume restores the original session's MCP servers.
        # Mismatched servers (e.g. claude.ai proxy servers from a previous
        # session) cause auth errors and immediate exit code 1.
        # KV cache benefits are handled by prompt caching (cache_control)
        # on the direct API path instead.
        options.resume = None

        response_text: str | None = None
        session_id: str | None = None
        max_attempts = DEFAULT_MAX_ATTEMPTS

        for attempt in range(TRANSIENT_MAX_ATTEMPTS):
            if attempt >= max_attempts:
                break

            stderr_fileobj = None
            stderr_path: str | None = None
            try:
                # In-memory buffer that spills to disk only when >1MB.
                # Avoids creating a new tempfile per attempt.
                stderr_fileobj = tempfile.SpooledTemporaryFile(
                    max_size=1024 * 1024, mode="w+b", prefix="claude_stderr_"
                )
                options.debug_stderr = stderr_fileobj
                options.extra_args = {"debug-to-stderr": None}

                timeout = TIMEOUT
                if attempt == 0 and options.resume:
                    timeout = min(TIMEOUT, 90)

                log.info("SDK attempt %d/%d (timeout=%ds, resume=%s)",
                         attempt + 1, max_attempts, timeout, bool(options.resume))

                turn_count = 0
                tool_count = 0
                t_start = time.monotonic()

                async with asyncio.timeout(timeout):
                    async for msg in query(prompt=prompt, options=options):
                        # --- SystemMessage ---
                        if isinstance(msg, SystemMessage):
                            log.info("[ch=%d] 📡 system: subtype=%s",
                                     channel_id, msg.subtype)

                        # --- AssistantMessage (thinking / tool_use / text) ---
                        elif isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, ThinkingBlock) and block.thinking:
                                    summary = block.thinking[:200].replace("\n", " ")
                                    log.info("[ch=%d] 💭 thinking: %s%s",
                                             channel_id, summary,
                                             "…" if len(block.thinking) > 200 else "")
                                    if status_callback:
                                        await status_callback(f"💭 {summary}")

                                elif isinstance(block, ToolUseBlock):
                                    tool_count += 1
                                    # Summarize input (truncate large payloads)
                                    inp = str(block.input) if block.input else ""
                                    inp_summary = inp[:200] + ("…" if len(inp) > 200 else "")
                                    log.info("[ch=%d] 🔧 tool_use #%d: %s(%s)",
                                             channel_id, tool_count, block.name, inp_summary)
                                    # Discord status: tool name + short input hint
                                    if status_callback:
                                        hint = _tool_input_hint(block.name, block.input)
                                        await status_callback(
                                            f"🔧 {block.name}" + (f"  `{hint}`" if hint else "")
                                        )

                                elif isinstance(block, TextBlock) and block.text:
                                    preview = block.text[:150].replace("\n", " ")
                                    log.info("[ch=%d] 📝 text: %s%s",
                                             channel_id, preview,
                                             "…" if len(block.text) > 150 else "")
                                    if status_callback:
                                        await status_callback(f"📝 応答を生成中")

                        # --- UserMessage (tool results flowing back) ---
                        elif isinstance(msg, UserMessage):
                            for block in (msg.content if isinstance(msg.content, list) else []):
                                if isinstance(block, ToolResultBlock):
                                    status = "❌ error" if block.is_error else "✅ ok"
                                    content_str = str(block.content) if block.content else ""
                                    preview = content_str[:200].replace("\n", " ")
                                    log.info("[ch=%d] 📨 tool_result [%s]: %s%s",
                                             channel_id, status, preview,
                                             "…" if len(content_str) > 200 else "")
                                    if status_callback:
                                        if block.is_error:
                                            await status_callback(f"❌ ツールエラー")
                                        else:
                                            await status_callback(f"📨 結果を受信")

                        # --- ResultMessage (final) ---
                        elif isinstance(msg, ResultMessage):
                            response_text = msg.result if hasattr(msg, "result") else str(msg)
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
                break  # success

            except TimeoutError:
                stderr_tail = _read_stderr_buffer(stderr_fileobj)
                _cleanup_stderr(stderr_fileobj)
                stderr_fileobj = None
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
                return f"⏰ タイムアウトしました（{TIMEOUT // 60}分制限）", None

            except Exception as e:
                stderr_tail = _read_stderr_buffer(stderr_fileobj)
                _cleanup_stderr(stderr_fileobj)
                stderr_fileobj = None

                err_str = str(e)[:500]
                error_type = _classify_error(err_str, stderr_tail)

                # Save stderr for debugging — only on the first few attempts to
                # avoid flooding .state/ with hundreds of files under retry storms.
                if stderr_tail and attempt < 3:
                    debug_path = os.path.join(STATE_DIR, f"stderr_ch{channel_id}.log")
                    try:
                        with open(debug_path, "w", encoding="utf-8") as fh:
                            fh.write(stderr_tail)
                        log.info("Saved stderr to %s", debug_path)
                    except Exception:
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
                    # cli_exit with partial response: return what we got
                    if error_type == "cli_exit" and response_text:
                        log.info("CLI exited with partial response, returning it")
                        break
                    # Resume failure (e.g. stale session with incompatible MCP
                    # servers): clear session and retry fresh instead of giving up.
                    if error_type == "cli_exit" and options.resume:
                        log.warning("Session resume failed (cli_exit) for ch=%d, retrying fresh", channel_id)
                        if channel_id in sessions:
                            del sessions[channel_id]
                        _clear_session_time(channel_id)
                        options.resume = None
                        await asyncio.sleep(BASE_BACKOFF_S)
                        continue
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
                _cleanup_stderr(stderr_fileobj)

        if response_text is None:
            return "(No response from Claude)", None

        if session_id:
            _record_session_time(channel_id)

        log.info("Response via %s (restricted=%s) len=%d",
                 MODEL, restricted, len(response_text))
        return response_text.strip() or "(No response from Claude)", session_id
