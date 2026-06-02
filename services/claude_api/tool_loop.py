"""Agentic tool-use loop driving the direct-API path.

One turn = stream a response from the API, observe ``tool_use`` blocks the
model emitted, gate them through ``can_use_tool``, execute approved ones,
package the results as ``tool_result`` blocks, and feed them back in the
next turn. Stops when the model emits ``end_turn`` / ``stop_sequence`` or
``max_turns`` is reached.

Differs from services/claude_sdk.py's ``call_with_tools`` (which delegates
to the ``claude`` CLI internally) by owning the loop ourselves: we see
every tool_use block, we route to either a builtin or MCP, and we synthesize
a session_id so SessionInfo round-trips cleanly even without a CLI session.

Notes:
- ``history`` is mutated in place; the caller (entry.py) is responsible for
  any disk persistence. tool_use / tool_result blocks are NOT written to
  JSONL (Codex v2 #5).
- ``api_client`` must be ``anthropic.AsyncAnthropic``; the sync client cannot
  yield streaming events.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass

import anthropic

from config import (
    CLAUDE_MAX_TOKENS,
    MAX_TOOL_RESULT_CHARS,
    STREAM_FIRST_EVENT_MAX_RETRIES,
    STREAM_FIRST_EVENT_TIMEOUT_S,
)

from services.claude_api.client import (
    CLAUDE_MAX_TOKENS_OPUS,
    CLI_DEFAULT_SYSTEM_PROMPT,
    CLI_IDENTITY_TEXT,
    CONTEXT_MANAGEMENT_CONFIG,
    OUTPUT_CONFIG,
    THINKING_CONFIG,
    get_attribution_header_value,
    get_metadata,
)
from services.claude_api.errors import classify_anthropic_error
from services.claude_api.mcp_client import kill_orphan_mcp_processes
from services.claude_api.ratelimit import (
    maybe_wait_before_request,
    observe as observe_ratelimit,
    should_block,
)
from services.claude_api.stall import StallWatchdog
from services.claude_api.status import _safe_status, _tool_input_hint

log = logging.getLogger(__name__)


@dataclass
class AgentLoopResult:
    text: str
    synthetic_session_id: str | None
    active_mcp_keys: list[str]


def _build_cached_system(system_prompt: str, messages: list[dict]) -> list[dict]:
    """Build the 4-block system prompt the CLI sends on /v1/messages.

    Captured from real claude-cli 2.1.92 (mitmproxy, 2026-05-13):

        system[0] = attribution header value (no cache)
        system[1] = "You are Claude Code, Anthropic's official CLI for
                     Claude." (no cache)
        system[2] = CLI's 11700-char default agent system prompt
                    (cache_control={"type":"ephemeral","scope":"global"})
        system[3] = caller-supplied prompt — for us this is the
                    concatenation of ~/.claude/CLAUDE.md (global persona)
                    + discord-bot/CLAUDE.md (Discord-specific override).
                    No cache; this block changes per session.

    The CLI default prompt at system[2] is what subscriber-tier gating
    appears to inspect; the caller's persona prompt at system[3] is the
    behavior override that wins on conflicts (Anthropic prompts give
    later system text more weight when they contradict).
    """
    attribution = get_attribution_header_value(messages)
    return [
        {"type": "text", "text": attribution},
        {"type": "text", "text": CLI_IDENTITY_TEXT},
        {"type": "text", "text": CLI_DEFAULT_SYSTEM_PROMPT,
         # 1h TTL so the static prefix (tools + this block) survives the
         # SESSION_TTL_S (30-min) gap and is cache-read on resume. MUST be
         # >= the messages breakpoint TTL: Anthropic requires longer-TTL
         # cache_control to appear BEFORE shorter ones (tools→system→messages).
         "cache_control": {"type": "ephemeral", "scope": "global", "ttl": "1h"}},
        {"type": "text", "text": system_prompt},
    ]


def _inject_cache_breakpoints(messages: list[dict]) -> list[dict]:
    """Mirrors the helper in __init__.py (see _build_cached_system above)."""
    if len(messages) < 4:
        return messages
    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(user_indices) < 2:
        return messages
    target_idx = user_indices[-2]
    messages = list(messages)
    msg = dict(messages[target_idx])
    content = msg["content"]
    # 1h TTL so the prior_history prefix (the dominant ~90K-token turn=1
    # write) survives the 30-min SESSION_TTL_S gap and is cache-read on
    # resume instead of rewritten. Must match the system breakpoint's 1h
    # (longer-TTL-before-shorter ordering rule).
    _bp = {"type": "ephemeral", "ttl": "1h"}
    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": _bp},
        ]
    elif isinstance(content, list):
        content = list(content)
        last_block = dict(content[-1])
        last_block["cache_control"] = _bp
        content[-1] = last_block
        msg["content"] = content
    messages[target_idx] = msg
    return messages


def _block_to_dict(block) -> dict:
    """Convert an anthropic content block (Pydantic model) to a serializable dict."""
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if btype == "tool_use":
        out = {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", None) or {},
        }
        # CLI capture (mitmproxy 2026-05-13) showed tool_use blocks
        # round-tripping with a "caller" field present. Preserve it if
        # the SDK exposed it so 2nd-turn requests are byte-shape-equal
        # to what the CLI emits.
        caller = getattr(block, "caller", None)
        if caller is not None:
            # caller may be a Pydantic model with .model_dump
            if hasattr(caller, "model_dump"):
                out["caller"] = caller.model_dump()
            else:
                out["caller"] = caller
        return out
    if btype == "thinking":
        # Anthropic round-trips thinking content; the API REQUIRES the
        # signature field on the way back, or it returns HTTP 400
        # "thinking.signature: Field required" on the next turn.
        out = {
            "type": "thinking",
            "thinking": getattr(block, "thinking", ""),
        }
        sig = getattr(block, "signature", None)
        if sig is not None:
            out["signature"] = sig
        return out
    if btype == "redacted_thinking":
        # Same story for redacted_thinking blocks — keep the data field
        # so we can echo it back next turn.
        out = {"type": "redacted_thinking"}
        data = getattr(block, "data", None)
        if data is not None:
            out["data"] = data
        return out
    # Unknown / future block type — best effort.
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return {"type": btype or "unknown"}


def _join_final_text(final_message) -> str:
    parts = []
    for block in getattr(final_message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts).strip()


# Cap on the inline body of any tool-invocation notice. Discord caps each
# message at 2000 chars; we hold ~100 chars of headroom for the wrapper
# (header, code fences, "truncated" marker). Anything bigger is sliced —
# the user sees the head of a long Bash pipeline or Write payload, and
# the model still operates on the full input.
_TOOL_DETAIL_INLINE_CAP = 1900
_TOOL_DETAIL_FIELD_CAP = 700  # for Edit's old/new pair (two fields share the budget)


# Hallucination detection: when assistant prose claims a file was written /
# saved / created (and names a plausible file path) but no Write or Edit
# tool_use block fired in that turn, log a WARN so we can audit. Kept as a
# pure observation hook for now — no auto-retry, no synthetic correction
# message. False positives are tolerated; the trace is meant to surface
# patterns that warrant prompt tweaks.
_HALLUC_VERBS = (
    "書きました", "書き出しました", "書き込みました", "保存しました",
    "作成しました", "作りました", "出力しました", "残しました", "記録しました",
    "wrote to", "wrote out", "saved to", "saved as", "created file",
    "written to",
)
_HALLUC_VERB_RE = re.compile(
    r"(?P<verb>" + "|".join(re.escape(v) for v in _HALLUC_VERBS) + r")",
    re.IGNORECASE,
)
_HALLUC_PATH_RE = re.compile(
    r"(?P<path>[\w./\\~-]+\."
    r"(?:md|py|txt|json|yaml|yml|toml|sh|rst|csv|tsv|html|css|js|ts|tsx|jsx|"
    r"go|rs|java|kt|swift|c|cpp|h|hpp|sql|env|ini|cfg|log))\b"
)
# Max distance (chars) between the claim verb and the cited file path. The
# two may appear in either order ("wrote to foo.md" en / "foo.md に書きました"
# jp), so we check both directions.
_HALLUC_MAX_DISTANCE = 160
_HALLUC_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def _detect_write_hallucination(
    turn_text: str,
    fired_tools: set[str],
    *,
    channel_id: int,
    turn: int,
) -> None:
    """Log a WARN if prose claims file writing without a Write/Edit fire."""
    if not turn_text:
        return
    if fired_tools & _HALLUC_WRITE_TOOLS:
        return
    verbs = list(_HALLUC_VERB_RE.finditer(turn_text))
    if not verbs:
        return
    paths = list(_HALLUC_PATH_RE.finditer(turn_text))
    if not paths:
        return
    # Pair the closest verb / path within _HALLUC_MAX_DISTANCE chars (either
    # order). Word order varies by language: "wrote to foo.md" (en) puts the
    # verb first; "foo.md に書きました" (jp) puts the path first.
    best: tuple[int, "re.Match", "re.Match"] | None = None
    for v in verbs:
        for p in paths:
            dist = max(v.start(), p.start()) - min(v.end(), p.end())
            if dist < 0:
                dist = 0
            if dist > _HALLUC_MAX_DISTANCE:
                continue
            if best is None or dist < best[0]:
                best = (dist, v, p)
    if best is None:
        return
    _, v, p = best
    lo = min(v.start(), p.start())
    hi = max(v.end(), p.end())
    snippet = turn_text[lo:hi]
    if len(snippet) > 200:
        snippet = snippet[:200] + "..."
    log.warning(
        "hallucination_suspect ch=%d turn=%d verb=%r path=%r snippet=%r "
        "fired_tools=%s",
        channel_id, turn + 1,
        v.group("verb"), p.group("path"), snippet,
        sorted(fired_tools),
    )


def _cap_inline(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + "\n...(truncated)"


def _format_tool_invocation_for_display(tool_name: str, tool_input: dict) -> str | None:
    """Build a Discord-friendly notice of an about-to-execute tool call.

    Returns ``None`` when there is nothing meaningful to render (empty
    input). The string is meant to be posted as-is via
    ``incremental_text_callback``.
    """
    tool_input = tool_input or {}

    if tool_name == "Bash":
        cmd = str(tool_input.get("command", "")).rstrip()
        if not cmd:
            return None
        timeout_hint = tool_input.get("timeout")
        header = "🔧 **Bash 実行**"
        if timeout_hint:
            header += f" (timeout={timeout_hint}s)"
        return f"{header}\n```bash\n{_cap_inline(cmd, _TOOL_DETAIL_INLINE_CAP)}\n```"

    if tool_name == "Read":
        fp = tool_input.get("file_path", "")
        if not fp:
            return None
        return f"🔧 **Read** `{fp}`"

    if tool_name == "Write":
        fp = tool_input.get("file_path", "")
        content = str(tool_input.get("content", ""))
        if not fp:
            return None
        return (
            f"🔧 **Write** `{fp}`\n"
            f"```\n{_cap_inline(content, _TOOL_DETAIL_INLINE_CAP)}\n```"
        )

    if tool_name == "Edit":
        fp = tool_input.get("file_path", "")
        old = str(tool_input.get("old_string", ""))
        new = str(tool_input.get("new_string", ""))
        if not fp:
            return None
        old_p = _cap_inline(old, _TOOL_DETAIL_FIELD_CAP)
        new_p = _cap_inline(new, _TOOL_DETAIL_FIELD_CAP)
        # Prefix each line so multi-line old/new still reads as a diff in
        # Discord's code-block syntax-highlighter.
        old_lines = "\n".join("- " + ln for ln in old_p.splitlines() or [""])
        new_lines = "\n".join("+ " + ln for ln in new_p.splitlines() or [""])
        replace_all = " (replace_all)" if tool_input.get("replace_all") else ""
        return (
            f"🔧 **Edit** `{fp}`{replace_all}\n"
            f"```diff\n{old_lines}\n{new_lines}\n```"
        )

    if tool_name == "Glob":
        pat = tool_input.get("pattern", "")
        if not pat:
            return None
        return f"🔧 **Glob** `{pat}`"

    if tool_name == "Grep":
        pat = tool_input.get("pattern", "")
        glob_filter = tool_input.get("glob")
        if not pat:
            return None
        if glob_filter:
            return f"🔧 **Grep** `{pat}` (in `{glob_filter}`)"
        return f"🔧 **Grep** `{pat}`"

    if tool_name == "TodoWrite":
        todos = tool_input.get("todos") or []
        if not isinstance(todos, list):
            return None
        counts = {"pending": 0, "in_progress": 0, "completed": 0}
        for t in todos:
            if isinstance(t, dict):
                s = t.get("status")
                if s in counts:
                    counts[s] += 1
        header = (
            f"📝 **TodoWrite** {len(todos)}件 "
            f"(pending={counts['pending']} "
            f"in_progress={counts['in_progress']} "
            f"completed={counts['completed']})"
        )
        # Render the list with simple markers; cap to inline budget so the
        # Discord status message stays under 2000 chars even with long todos.
        lines = [header]
        for i, t in enumerate(todos, 1):
            if not isinstance(t, dict):
                continue
            marker = {
                "pending": "[ ]",
                "in_progress": "[~]",
                "completed": "[x]",
            }.get(t.get("status"), "[?]")
            content = str(t.get("content", ""))
            lines.append(f"{i}. {marker} {content}")
        body = "\n".join(lines)
        return _cap_inline(body, _TOOL_DETAIL_INLINE_CAP)

    # MCP / unknown tools: render the input as JSON so the user can see
    # what's being passed. Useful for mem0_add, x-scraper, etc.
    if tool_input:
        import json as _json
        try:
            args_str = _json.dumps(tool_input, ensure_ascii=False)
        except (TypeError, ValueError):
            args_str = str(tool_input)
        return (
            f"🔧 **{tool_name}**\n"
            f"```json\n{_cap_inline(args_str, _TOOL_DETAIL_INLINE_CAP)}\n```"
        )
    return f"🔧 **{tool_name}**"


# Placeholder substituted for empty message content. The Messages API
# rejects messages with empty content (400 'must have non-empty content').
# Empty content arises from a thinking-only pause turn (tool_results==[]) or
# from resumed JSONL history that captured such a turn from an older run.
# Wording explicitly forbids narration-only resumption: the model must call
# a tool in the next turn rather than just continuing prose.
_EMPTY_CONTENT_PLACEHOLDER = [{
    "type": "text",
    "text": "(前ターンで tool_use が無かったため再開要求。必要なら今すぐツールを呼んでください。宣言だけで終わらせないこと)",
}]

# Max consecutive thinking-only "pause turns" (stop_reason==tool_use but zero
# tool_use blocks) before the loop stops nudging and returns accumulated text.
# Bounds the loop so a model that keeps thinking without acting can't spin.
#
# 2026-05-30: lowered 2 → 1 after rid=8e6df64c (a nudged turn went silent 602s
# upstream). RESTORED to 2 on 2026-06-02: that lowering was a misdiagnosis.
# MAX=1 means the FIRST thinking-only pause turn returns accumulated text
# immediately WITHOUT nudging the model to call a tool — which truncated
# legitimate continuations (rid=decc2aeb msg3: turn=1 returned 823 chars of
# half-finished prose, abandoning the work the model was about to do). And it
# never actually fixed the hang: the 8e6df64c-class upstream stall recurs on
# *normal* turns (rid=c9b86f7b hung on turn-3, not a nudge turn), so crippling
# the nudge bought nothing. The hang is now caught directly and ~5x faster by
# the first-event timeout in the stream loop below, so the nudge is safe to
# restore. With MAX=2 a single benign pause turn gets one nudge ("call a tool
# now"); only a model that thinks-without-acting twice in a row is cut off.
MAX_CONSECUTIVE_EMPTY_TURNS = 2


def _truncate_tool_result(content):
    """Head+tail truncate an oversized string tool result.

    A single huge tool result (``journalctl`` dump, large ``Read``/``Grep``) is
    the dominant driver of agent-loop history bloat → 413 / 7.6M-token
    context_overflow (2026-06-01 incident ①). Cap string results at
    ``MAX_TOOL_RESULT_CHARS``, keeping the head (usually the most relevant) and
    a smaller tail, with an elision marker so the model knows content was
    dropped and can re-query a narrower slice. List results (the ``Read`` tool's
    image content blocks) are passed through untouched. ``cap<=0`` disables.
    """
    cap = MAX_TOOL_RESULT_CHARS
    if cap <= 0 or not isinstance(content, str) or len(content) <= cap:
        return content
    head = int(cap * 0.7)
    tail = cap - head
    elided = len(content) - cap
    return (
        content[:head]
        + f"\n\n…[{elided:,} chars elided — tool result capped at {cap:,}; "
        "re-run with a narrower query/range to see the rest]…\n\n"
        + content[-tail:]
    )


# Tool calls the model wrote as TEXT instead of as a native tool_use block.
# Opus 4.8 intermittently emits the function-calling XML
# (``<function_calls><invoke name=...><parameter ...>``) inline in its prose
# rather than as a structured tool_use block — observed rid=decc2aeb
# (2026-06-02): a brave-search / x-scraper call leaked verbatim into the Discord
# reply, and because the API parsed zero tool_use blocks the turn looked like a
# "thinking-only pause" and the loop stopped. The tools were advertised
# (tools=23 included brave-search), so this is a model-format quirk, not an
# advertising gap — minutes later the same model called the same tool natively.
# This text is NEVER executed by the API, so it must (a) never reach the user
# and (b) trigger a corrective nudge so the model re-issues it as real tool_use.
_TEXT_TOOL_CALL_MARKER = re.compile(
    r"<\s*/?\s*(?:antml:)?(?:function_calls|invoke)\b",
    re.IGNORECASE,
)
_TEXT_TOOL_CALL_RE = re.compile(
    # A whole <function_calls>…</function_calls> wrapper (across newlines)…
    r"<\s*(?:antml:)?function_calls\b[^>]*>.*?<\s*/\s*(?:antml:)?function_calls\s*>"
    # …or a standalone <invoke …>…</invoke> block…
    r"|<\s*(?:antml:)?invoke\b[^>]*>.*?<\s*/\s*(?:antml:)?invoke\s*>"
    # …or any leftover/unclosed call marker (stream truncated mid-tag).
    r"|<\s*/?\s*(?:antml:)?(?:function_calls|invoke|parameter)\b[^>]*>?",
    re.IGNORECASE | re.DOTALL,
)


def _strip_text_tool_calls(text: str) -> "tuple[str, bool]":
    """Strip text-format tool-call XML from model prose.

    Returns ``(cleaned_text, had_text_tool_call)``. ``cleaned_text`` is safe to
    show the user and to accumulate; ``had_text_tool_call`` tells the caller to
    parse+execute the call (so the model's intent isn't lost).
    """
    if not text or not _TEXT_TOOL_CALL_MARKER.search(text):
        return text, False
    cleaned = _TEXT_TOOL_CALL_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, True


# Parse the text-format tool call back into (name, input) so the loop can
# EXECUTE it instead of just nudging-and-giving-up. Nudging alone is symptom-
# hiding: if 4.8 re-emits the text format on the next turn (consecutive=2), the
# loop hits MAX_CONSECUTIVE_EMPTY_TURNS and returns a truncated, tool-less reply
# (docs/2026-06-02-msg3-tag-leak-model-revert-rootcause.md root cause 2).
_INVOKE_RE = re.compile(
    r"<\s*(?:antml:)?invoke\b[^>]*?\bname\s*=\s*\"([^\"]+)\"[^>]*>(.*?)"
    r"<\s*/\s*(?:antml:)?invoke\s*>",
    re.IGNORECASE | re.DOTALL,
)
_PARAM_RE = re.compile(
    r"<\s*(?:antml:)?parameter\b[^>]*?\bname\s*=\s*\"([^\"]+)\"[^>]*>(.*?)"
    r"<\s*/\s*(?:antml:)?parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _parse_text_tool_calls(text: str) -> "list[tuple[str, dict]]":
    """Extract ``[(tool_name, tool_input), ...]`` from text-format tool calls.

    Each ``<parameter>`` value is JSON-decoded when possible (so ``"12"`` →
    ``12``, ``"true"`` → ``True``, ``["a"]`` → list) to match typed tool input
    schemas, falling back to the raw string otherwise. Returns ``[]`` when
    nothing parses (caller then falls back to a corrective nudge).
    """
    calls: "list[tuple[str, dict]]" = []
    for m in _INVOKE_RE.finditer(text or ""):
        name = m.group(1).strip()
        params: dict = {}
        for pm in _PARAM_RE.finditer(m.group(2)):
            raw = pm.group(2).strip()
            try:
                params[pm.group(1).strip()] = json.loads(raw)
            except (ValueError, TypeError):
                params[pm.group(1).strip()] = raw
        if name:
            calls.append((name, params))
    return calls


def _normalize_history_for_api(history: list[dict]) -> list[dict]:
    """Strip extra fields, leaving only {role, content} for the API.

    Also guards against empty content (``""`` or ``[]``), which the Messages
    API rejects with 400 invalid_request. Empty content is REPLACED (not
    dropped) with a placeholder so tool_use/tool_result pairing stays intact.
    Both the in-loop path and the JSONL resume path funnel through here, so
    this is the single structural defense against the empty-content 400.
    Strict ``== ""`` / ``== []`` comparison (not a falsy check) so a future
    ``0``/``False`` content is left untouched.
    """
    out: list[dict] = []
    for h in history:
        content = h["content"]
        if content == "" or content == []:
            content = _EMPTY_CONTENT_PLACEHOLDER
        out.append({"role": h["role"], "content": content})
    return out


_USAGE_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def _usage_number(usage, field: str) -> int:
    if usage is None:
        return 0
    if isinstance(usage, dict):
        value = usage.get(field, 0)
    else:
        value = getattr(usage, field, 0)
    return value if isinstance(value, int) else 0


def _log_turn_usage(channel_id: int, turn: int, final_msg, totals: dict[str, int]) -> None:
    """Emit per-turn usage if the SDK message exposes it.

    Stays silent for mocks / responses without ``usage`` so the agent loop
    keeps running. Bumps ``totals`` so a later call to ``_log_total_usage``
    can summarize multi-turn (tool_use) requests.
    """
    usage = getattr(final_msg, "usage", None)
    if usage is None:
        return
    values = {field: _usage_number(usage, field) for field in _USAGE_FIELDS}
    for field, value in values.items():
        totals[field] += value
    totals["turns"] += 1
    log.info(
        "claude_api_usage channel=%d turn=%d input_tokens=%d "
        "cache_read_input_tokens=%d cache_creation_input_tokens=%d "
        "output_tokens=%d stop_reason=%s",
        channel_id, turn + 1,
        values["input_tokens"],
        values["cache_read_input_tokens"],
        values["cache_creation_input_tokens"],
        values["output_tokens"],
        getattr(final_msg, "stop_reason", None),
    )


def _log_total_usage(channel_id: int, totals: dict[str, int]) -> None:
    """Emit a tool-loop-wide summary when the request spanned >=2 turns."""
    if totals.get("turns", 0) <= 1:
        return
    log.info(
        "claude_api_usage_total channel=%d turns=%d input_tokens=%d "
        "cache_read_input_tokens=%d cache_creation_input_tokens=%d output_tokens=%d",
        channel_id, totals["turns"], totals["input_tokens"],
        totals["cache_read_input_tokens"], totals["cache_creation_input_tokens"],
        totals["output_tokens"],
    )


async def run_agent_loop(
    prompt: str,
    channel_id: int,
    *,
    history: list[dict],
    system_prompt: str,
    tools_for_api: list[dict],
    can_use_tool,  # (name, input) -> (bool, reason)
    execute_tool,  # async (name, input) -> str
    status_callback=None,
    incremental_text_callback=None,   # async (text:str)->None
    tool_invocation_callback=None,    # async (name:str, args:dict)->None
    max_turns: int,
    stall_watchdog: StallWatchdog,
    api_client: anthropic.AsyncAnthropic,
    model: str,
    extra_betas: list[str],
    extra_headers: dict | None = None,
    active_mcp_keys: list[str] | None = None,
) -> AgentLoopResult:
    """Run the streaming + tool-use loop. See module docstring for invariants.

    ``incremental_text_callback`` lets bot.py stream prose to Discord as
    soon as each turn emits it, instead of buffering until end_turn. When
    set, every turn's text block (if non-empty) is forwarded to the
    callback the moment that turn finishes, and the returned
    ``AgentLoopResult.text`` is left empty so the caller knows there's
    nothing extra to send. When ``None`` (legacy behaviour) the text is
    accumulated and returned at end_turn as a single string.

    ``tool_invocation_callback`` is a separate channel for "what tool is
    about to fire", carrying ``(name, input)`` per tool_use block. bot.py
    treats this as a single, edit-overwriting status message that gets
    deleted at end-of-response — so the final Discord transcript shows
    only the assistant's prose, not the scaffolding.
    """
    history.append({"role": "user", "content": prompt})
    active_mcp_keys = list(active_mcp_keys or [])
    last_text = ""
    # Accumulator for text blocks emitted across *all* turns when no
    # incremental callback is wired. The model often interleaves narration
    # with tool_use blocks — e.g. it writes its full review of an image
    # and *then* calls Mem0 to save a fact about it. That first turn's
    # stop_reason is "tool_use", which used to drop the prose, leaving
    # only whatever fragment the final end_turn turn emitted (often 0-2
    # tokens of acknowledgement). Concatenating across turns preserves
    # the user-facing answer. When incremental_text_callback is provided,
    # turns are streamed live instead and this buffer stays empty.
    text_across_turns: list[str] = []
    usage_totals: dict[str, int] = {field: 0 for field in _USAGE_FIELDS}
    usage_totals["turns"] = 0
    # Cumulative write-tool fires across the whole agent loop. Without this,
    # the hallucination detector misfires on a common pattern: turn N runs
    # Write, turn N+1 emits the user-facing "書きました ... path" sentence.
    # Per-turn fired_tools is empty on turn N+1, so the detector sees an
    # apparent claim with no backing tool call.
    fired_tools_cumulative: set[str] = set()
    # Consecutive thinking-only pause turns (stop_reason==tool_use but zero
    # tool_use blocks). Initialized OUTSIDE the loop so the cap accumulates;
    # reset to 0 whenever a turn yields real tool_results.
    consecutive_empty = 0

    for turn in range(max_turns):
        # Record current turn on the watchdog so a stall-cancel can tell the
        # user how deep we got ("最後のturn=N") instead of just "timed out".
        try:
            stall_watchdog.set_turn(turn)
        except AttributeError:
            # Older watchdog stub (test mocks without set_turn) — diagnostic
            # plumbing is best-effort, never break the loop on it.
            pass
        messages = _normalize_history_for_api(history)
        messages = _inject_cache_breakpoints(messages)
        cached_system = _build_cached_system(system_prompt, messages)

        # Refuse outright if observed rate-limit state says any attempt is futile.
        blocked, reason = should_block()
        if blocked:
            log.warning("ratelimit pre-check refused request: %s", reason)
            return AgentLoopResult(
                text=f"⏳ {reason}",
                synthetic_session_id=None,
                active_mcp_keys=active_mcp_keys,
            )
        # Soft pacing — sleep up to a few seconds if last response told us
        # we're in the warning zone, so we don't trip the probabilistic throttle.
        await maybe_wait_before_request()

        # Body fields the CLI pairs with its betas. Re-instated 2026-05-15
        # after a fresh mitm capture (docs/captures/A_one_shot/) showed
        # claude-cli 2.1.92 *does* send output_config + context_management
        # as top-level body fields — the earlier "extra_body: null"
        # reading was a misinterpretation of the Python SDK's wire format
        # (extra_body is the SDK's own merge slot, not the field name
        # the server sees). Without these, effort-2025-11-24 has nowhere
        # to read its dial from, so the model behaves as if effort were
        # unset.
        max_tokens = (
            CLAUDE_MAX_TOKENS_OPUS if "opus" in (model or "").lower()
            else CLAUDE_MAX_TOKENS
        )
        stream_kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=cached_system,
            messages=messages,
            betas=extra_betas,
            metadata=get_metadata(),
            thinking=THINKING_CONFIG,
            extra_body={
                "output_config": OUTPUT_CONFIG,
                "context_management": CONTEXT_MANAGEMENT_CONFIG,
            },
        )
        if tools_for_api:
            stream_kwargs["tools"] = tools_for_api
        if extra_headers:
            stream_kwargs["extra_headers"] = extra_headers

        # First-event (time-to-first-token) guarded stream consumption. An
        # upstream hang manifests as HTTP 200 + ZERO SSE events: the POST
        # succeeds but `message_start` never arrives. Bound only the wait for
        # the FIRST event and re-POST the turn, rather than waiting out the full
        # inter-event idle_timeout (600s observed in rid=c9b86f7b). After the
        # first event lands, inter-event gaps are owned by the StallWatchdog so
        # legitimate extended-thinking pauses are not cut off.
        first_event_attempt = 0
        while True:
            try:
                async with api_client.beta.messages.stream(**stream_kwargs) as stream:
                    stream_iter = stream.__aiter__()
                    got_first_event = False
                    while True:
                        try:
                            next_event = stream_iter.__anext__()
                            if not got_first_event:
                                event = await asyncio.wait_for(
                                    next_event, STREAM_FIRST_EVENT_TIMEOUT_S
                                )
                            else:
                                event = await next_event
                        except StopAsyncIteration:
                            break
                        got_first_event = True
                        stall_watchdog.heartbeat()
                        etype = getattr(event, "type", None)
                        if etype == "content_block_start":
                            block = getattr(event, "content_block", None)
                            btype = getattr(block, "type", None) if block else None
                            if btype == "thinking":
                                await _safe_status(status_callback, "💭 thinking…")
                            elif btype == "text":
                                await _safe_status(status_callback, "📝 応答を生成中")
                            elif btype == "tool_use":
                                name = getattr(block, "name", "?")
                                hint = _tool_input_hint(name, getattr(block, "input", None))
                                short_name = name.split("__", 2)[-1] if "__" in name else name
                                label = f"🔧 {short_name}"
                                if hint:
                                    label += f" `{hint[:60]}`"
                                await _safe_status(status_callback, label)
                    final_msg = await stream.get_final_message()
                    _log_turn_usage(channel_id, turn, final_msg, usage_totals)
                    # Update the shared rate-limit state from the response so
                    # the next turn (and the next call from entry.py) can pace
                    # itself before sending. anthropic SDK exposes the raw
                    # response via stream.response.headers.
                    try:
                        resp = getattr(stream, "response", None)
                        if resp is not None and getattr(resp, "headers", None) is not None:
                            observe_ratelimit(resp.headers)
                    except Exception as exc:
                        log.debug("ratelimit observe failed: %s", exc)
                break
            except asyncio.TimeoutError:
                # No SSE event within the first-event window. Abandon this
                # stream (the `async with` exit closes the dangling HTTP
                # connection) and re-POST the same turn. Heartbeat so the
                # StallWatchdog's idle timer doesn't also fire mid-retry.
                first_event_attempt += 1
                stall_watchdog.heartbeat()
                if first_event_attempt > STREAM_FIRST_EVENT_MAX_RETRIES:
                    log.error(
                        "first-event timeout exhausted ch=%d turn=%d "
                        "(%ds × %d attempts) → returning accumulated text",
                        channel_id, turn, STREAM_FIRST_EVENT_TIMEOUT_S,
                        first_event_attempt,
                    )
                    _log_total_usage(channel_id, usage_totals)
                    accumulated = "\n\n".join(text_across_turns)
                    return AgentLoopResult(
                        text=accumulated or (
                            "⏰ 応答が来ませんでした（初動タイムアウト）。"
                            "もう一度試してください。"
                        ),
                        synthetic_session_id=None,
                        active_mcp_keys=active_mcp_keys,
                    )
                log.warning(
                    "first-event timeout ch=%d turn=%d after %ds → re-POST "
                    "(retry %d/%d)",
                    channel_id, turn, STREAM_FIRST_EVENT_TIMEOUT_S,
                    first_event_attempt, STREAM_FIRST_EVENT_MAX_RETRIES,
                )
                await _safe_status(status_callback, "🔄 応答が来ないので再接続中")
                continue
            except anthropic.AnthropicError as exc:
                category = classify_anthropic_error(exc)
                log.error("agent loop API error turn=%d category=%s: %s",
                          turn, category, exc)
                # Also try to observe ratelimit headers from the failing
                # response so the next pre-check can refuse outright.
                try:
                    resp = getattr(exc, "response", None)
                    if resp is not None and getattr(resp, "headers", None) is not None:
                        observe_ratelimit(resp.headers)
                except Exception:
                    pass
                # Caller (entry.py) owns retry/backoff; surface the exception.
                raise

        # Persist the assistant turn (in-memory only; disk write is caller's job)
        assistant_blocks = [_block_to_dict(b) for b in (final_msg.content or [])]
        history.append({"role": "assistant", "content": assistant_blocks})

        # Capture any prose this turn emitted before deciding how to exit.
        # We do this on *every* turn (not just the final one) so a model
        # that wrote its full answer in turn N and then tool_use'd Mem0
        # in turn N+1 still surfaces the prose to the user.
        raw_turn_text = _join_final_text(final_msg)
        # Remove any tool call the model wrote as TEXT (function-calling XML)
        # before it is shown live or accumulated — that text is never executed
        # by the API and leaked to Discord in rid=decc2aeb. The RAW form is kept
        # so the text-format call can be parsed + executed below instead of lost.
        turn_text, had_text_tool_call = _strip_text_tool_calls(raw_turn_text)

        # Collect tool names that fired in this turn so the hallucination
        # detector below can compare claimed writes vs actual tool_use
        # blocks. MCP names are kept whole (e.g. mcp__mem0__add) — only
        # builtin write tools are relevant to the regex below, so the
        # comparison still works without prefix stripping.
        fired_tools_this_turn: set[str] = set()
        for _b in final_msg.content or []:
            if getattr(_b, "type", None) == "tool_use":
                fired_tools_this_turn.add(getattr(_b, "name", ""))
        fired_tools_cumulative |= fired_tools_this_turn
        _detect_write_hallucination(
            turn_text,
            fired_tools_cumulative,
            channel_id=channel_id,
            turn=turn,
        )

        if turn_text:
            if incremental_text_callback is not None:
                # Live-stream this turn's prose to the caller. The
                # callback owns delivery (Discord channel.send, splitting
                # at 2000-char boundaries, etc.) and is wrapped so a
                # failure can never abort the agent loop.
                try:
                    await incremental_text_callback(turn_text)
                except Exception as exc:
                    log.warning(
                        "incremental_text_callback failed (turn=%d): %s",
                        turn, exc,
                    )
            # Always accumulate — even when the callback already delivered
            # the prose live. The accumulated text is what entry.py writes
            # to the JSONL transcript so the next turn's resume sees what
            # the model said. bot.py uses ``_live_sent_any`` to decide
            # whether to skip the final reply, avoiding double-send.
            # Previously this branch only accumulated when no callback was
            # wired, which caused 30+ turns of streamed prose to never
            # land on disk (2026-05-15 postmortem).
            text_across_turns.append(turn_text)

        stop_reason = getattr(final_msg, "stop_reason", None)
        if stop_reason in ("end_turn", "stop_sequence"):
            last_text = "\n\n".join(text_across_turns)
            _log_total_usage(channel_id, usage_totals)
            return AgentLoopResult(
                text=last_text,
                synthetic_session_id=uuid.uuid4().hex,
                active_mcp_keys=active_mcp_keys,
            )
        if stop_reason != "tool_use":
            # max_tokens / refusal / unexpected — return whatever text we
            # accumulated up to this point. Falls back to the current
            # turn's text alone if for some reason we never appended.
            last_text = "\n\n".join(text_across_turns)
            _log_total_usage(channel_id, usage_totals)
            return AgentLoopResult(
                text=last_text or "(no response)",
                synthetic_session_id=None,
                active_mcp_keys=active_mcp_keys,
            )

        # Execute every tool_use block in this turn.
        tool_results: list[dict] = []
        for block in final_msg.content or []:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_id = getattr(block, "id", "")
            tool_name = getattr(block, "name", "")
            tool_input = getattr(block, "input", None) or {}

            # Notify the caller that a tool is about to run. bot.py
            # renders this as a single edit-overwriting status message
            # so the transcript stays clean — the final Discord history
            # only retains the assistant's prose (incremental_text path)
            # and is free of "🔧 Bash 実行 ..." scaffolding.
            if tool_invocation_callback is not None:
                try:
                    await tool_invocation_callback(tool_name, tool_input)
                except Exception as exc:
                    log.warning(
                        "tool_invocation_callback failed (tool=%s turn=%d): %s",
                        tool_name, turn, exc,
                    )

            allowed, reason = can_use_tool(tool_name, tool_input)
            if not allowed:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"Tool {tool_name} denied: {reason}",
                    "is_error": True,
                })
                await _safe_status(status_callback, "❌ ツール拒否")
                continue

            try:
                t0 = time.monotonic()
                output = await execute_tool(tool_name, tool_input)
                dt = time.monotonic() - t0
                # Anthropic tool_result content accepts either a string or
                # a list of content blocks (text + image). The Read tool's
                # image branch returns the latter; everything else still
                # returns a plain string. Anything weird falls back to str()
                # so we never wedge the API call.
                if isinstance(output, list):
                    result_content: "str | list[dict]" = output
                elif isinstance(output, str):
                    result_content = _truncate_tool_result(output)
                else:
                    result_content = _truncate_tool_result(str(output))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_content,
                })
                await _safe_status(
                    status_callback,
                    f"📨 結果を受信 ({dt:.1f}s)",
                )
            except Exception as exc:
                log.warning("tool %s failed: %s", tool_name, exc)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"Error: {exc}",
                    "is_error": True,
                })
                await _safe_status(status_callback, "❌ ツールエラー")

        # 4.8 sometimes writes the tool call as TEXT instead of a native
        # tool_use block. Rather than nudge-and-give-up (which truncates the
        # reply once the model repeats the format and hits the consecutive
        # limit), parse the call out of the raw prose and EXECUTE it, feeding
        # the result back so the turn's work is not lost. Only falls through to
        # the nudge below when nothing parses.
        if not tool_results and had_text_tool_call:
            parsed_calls = _parse_text_tool_calls(raw_turn_text)
            if parsed_calls:
                log.warning(
                    "executing %d text-format tool call(s) ch=%d turn=%d "
                    "(model emitted them as text; running them so the turn "
                    "is not lost)",
                    len(parsed_calls), channel_id, turn + 1,
                )
                result_lines: list[str] = []
                for tool_name, tool_input in parsed_calls:
                    if tool_invocation_callback is not None:
                        try:
                            await tool_invocation_callback(tool_name, tool_input)
                        except Exception as exc:
                            log.warning(
                                "tool_invocation_callback failed (text-call %s): %s",
                                tool_name, exc,
                            )
                    allowed, reason = can_use_tool(tool_name, tool_input)
                    if not allowed:
                        result_lines.append(f"[{tool_name}] 拒否: {reason}")
                        await _safe_status(status_callback, "❌ ツール拒否")
                        continue
                    try:
                        t0 = time.monotonic()
                        output = await execute_tool(tool_name, tool_input)
                        dt = time.monotonic() - t0
                        text_out = output if isinstance(output, str) else str(output)
                        result_lines.append(
                            f"[{tool_name}] 結果:\n{_truncate_tool_result(text_out)}"
                        )
                        await _safe_status(status_callback, f"📨 結果を受信 ({dt:.1f}s)")
                    except Exception as exc:
                        log.warning("text-format tool %s failed: %s", tool_name, exc)
                        result_lines.append(f"[{tool_name}] エラー: {exc}")
                        await _safe_status(status_callback, "❌ ツールエラー")
                feedback = (
                    "前ターンのツール呼び出しがテキスト形式 (<invoke>) だったため、"
                    "こちらで代理実行しました。結果は以下です。これを使って回答を"
                    "続けてください。追加でツールが必要なら、次は必ず正式な tool_use "
                    "ブロックで呼んでください (テキスト形式は実行されません)。\n\n"
                    + "\n\n".join(result_lines)
                )
                history.append({
                    "role": "user",
                    "content": [{"type": "text", "text": feedback}],
                })
                consecutive_empty = 0
                continue

        if not tool_results:
            # Pause turn: stop_reason was tool_use but the model emitted only
            # thinking/text and zero tool_use blocks (seen under effort=max +
            # interleaved-thinking). Appending {"role":"user","content":[]}
            # here was the direct cause of the empty-content 400.
            consecutive_empty += 1
            if consecutive_empty < MAX_CONSECUTIVE_EMPTY_TURNS:
                log.info(
                    "pause turn (%s) ch=%d turn=%d consecutive=%d → nudging",
                    "text-format tool call" if had_text_tool_call
                    else "thinking-only, no tool_use",
                    channel_id, turn + 1, consecutive_empty,
                )
                # MAX_CONSECUTIVE_EMPTY_TURNS=2 (restored 2026-06-02): one nudge
                # is allowed before giving up. Two distinct causes land here —
                # the model thought without acting, or it wrote a tool call as
                # text (stripped from turn_text above) — so the nudge is
                # tailored. Both forbid narration-only continuation: the prior
                # "続けてください。" let the model keep thinking (rid=8e6df64c).
                if had_text_tool_call:
                    nudge = (
                        "前ターンでツール呼び出しを <function_calls> / <invoke> の"
                        "プレーンテキスト形式で書きましたが、その形式は実行されません。"
                        "同じ呼び出しを正式な tool_use ブロックとして今すぐ実行してください。"
                        "説明や宣言だけで終わらせないこと。"
                    )
                else:
                    nudge = (
                        "前ターンは思考のみで tool_use ブロックがありませんでした。"
                        "説明や考察ではなく、必要なツール (Read / Grep / Bash / Edit 等) を"
                        "次のターンで必ず呼んでください。宣言だけで終わらせるのは禁止です。"
                    )
                history.append({
                    "role": "user",
                    "content": [{"type": "text", "text": nudge}],
                })
                continue
            log.warning(
                "pause turn limit reached ch=%d turn=%d consecutive=%d → "
                "returning accumulated text",
                channel_id, turn + 1, consecutive_empty,
            )
            last_text = "\n\n".join(text_across_turns)
            _log_total_usage(channel_id, usage_totals)
            return AgentLoopResult(
                text=last_text or "(no response)",
                synthetic_session_id=None,
                active_mcp_keys=active_mcp_keys,
            )
        consecutive_empty = 0
        history.append({"role": "user", "content": tool_results})

    # Reached max_turns.
    log.warning("agent loop exhausted max_turns=%d for ch=%d", max_turns, channel_id)
    _log_total_usage(channel_id, usage_totals)
    accumulated = "\n\n".join(text_across_turns)
    return AgentLoopResult(
        text=accumulated or last_text or "🔄 ターン上限に達しました。もう一度話しかけてください。",
        synthetic_session_id=None,
        active_mcp_keys=active_mcp_keys,
    )


def kill_orphan_claude_cli() -> int:
    """API-side cleanup: SIGTERM/SIGKILL MCP child processes.

    Replaces services/claude_sdk.py:kill_orphan_claude_cli for the direct-API
    path. The name is preserved for back-compat (main_async.py shutdown still
    imports this symbol).
    """
    return kill_orphan_mcp_processes()
