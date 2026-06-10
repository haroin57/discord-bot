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

import logging
import re
import time
import uuid
from dataclasses import dataclass

import anthropic

from config import CLAUDE_MAX_TOKENS

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
        # ttl:1h matches the real CLI (mitm capture: system[2] carries
        # {ephemeral, ttl:1h, scope:global}). Without ttl the block falls
        # back to the 5-minute default and the cached prefix expires between
        # turns, forcing a full re-write (observed as cache_read=0).
        {"type": "text", "text": CLI_DEFAULT_SYSTEM_PROMPT,
         "cache_control": {"type": "ephemeral", "ttl": "1h", "scope": "global"}},
        # The CLI also caches system[3] (the persona prompt) with ttl:1h.
        {"type": "text", "text": system_prompt,
         "cache_control": {"type": "ephemeral", "ttl": "1h"}},
    ]


def _inject_cache_breakpoints(messages: list[dict]) -> list[dict]:
    """Mirrors the helper in __init__.py (see _build_cached_system above).

    PLACEMENT: the breakpoint goes on the second-to-last user message
    (user_indices[-2]), not the last. The last user message is the volatile new
    input that changes every request, so a breakpoint there would write a fresh
    entry that the next turn never reads. Targeting [-2] caches the stable prefix
    (system + all prior turns) and leaves only the newest turn uncached.

    TTL: the breakpoint carries ttl:1h, matching the ttl:1h value the real
    claude-cli uses on its message breakpoint (mitm capture cli_resume.json) and
    the ttl:1h on system[2]/[3] in _build_cached_system. The default 5-minute
    TTL is wrong for this workload: Discord turns are routinely spaced >5 min
    apart, so a 5-min breakpoint expires between user turns and the entire
    replayed conversation history is re-written to cache every resume (observed
    as cache_read pinned to system-block size while cache_creation re-pays the
    full history — up to ~227k tokens — on each turn). ttl:1h keeps the history
    prefix warm across the gap so the next turn reads it at ~0.1x instead.
    Requires the extended-cache-ttl beta, which MODEL_BETAS already sends.
    """
    if len(messages) < 4:
        return messages
    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(user_indices) < 2:
        return messages
    target_idx = user_indices[-2]
    messages = list(messages)
    msg = dict(messages[target_idx])
    content = msg["content"]
    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content,
             "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        ]
    elif isinstance(content, list):
        content = list(content)
        last_block = dict(content[-1])
        last_block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
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
_EMPTY_CONTENT_PLACEHOLDER = [{"type": "text", "text": "(続けてください)"}]

# Max consecutive thinking-only "pause turns" (stop_reason==tool_use but zero
# tool_use blocks) before the loop stops nudging and returns accumulated text.
# Bounds the loop so a model that keeps thinking without acting can't spin.
MAX_CONSECUTIVE_EMPTY_TURNS = 2


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
    stop_reason = getattr(final_msg, "stop_reason", None)
    log.info(
        "claude_api_usage channel=%d turn=%d input_tokens=%d "
        "cache_read_input_tokens=%d cache_creation_input_tokens=%d "
        "output_tokens=%d stop_reason=%s",
        channel_id, turn + 1,
        values["input_tokens"],
        values["cache_read_input_tokens"],
        values["cache_creation_input_tokens"],
        values["output_tokens"],
        stop_reason,
    )
    # On a model refusal, surface stop_details (category: cyber|bio|None +
    # explanation) so refusals are diagnosable instead of silently becoming
    # "(no response)" (ADR-0008). The bot was previously blind to WHY fable-5
    # refused. getattr-guarded so older SDKs / mocks without the field are safe.
    if stop_reason == "refusal":
        sd = getattr(final_msg, "stop_details", None)
        log.warning(
            "claude_api_refusal channel=%d turn=%d category=%s explanation=%r",
            channel_id, turn + 1,
            getattr(sd, "category", None),
            getattr(sd, "explanation", None),
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
            # output_config MUST be a direct kwarg, NOT nested in extra_body.
            # anthropic SDK 0.88.0's beta streaming path silently DROPS
            # output_config when it's inside extra_body (verified by mitmproxy:
            # effort never reached the wire), while context_management survives.
            # With effort gone but the effort-2025-11-24 beta header still set,
            # the server saw a contradictory request and returned
            # stop_reason=refusal. Direct kwarg puts effort on the wire (ADR-0009).
            output_config=OUTPUT_CONFIG,
            extra_body={
                "context_management": CONTEXT_MANAGEMENT_CONFIG,
            },
        )
        if tools_for_api:
            stream_kwargs["tools"] = tools_for_api
        if extra_headers:
            stream_kwargs["extra_headers"] = extra_headers

        try:
            async with api_client.beta.messages.stream(**stream_kwargs) as stream:
                async for event in stream:
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
        except anthropic.AnthropicError as exc:
            category = classify_anthropic_error(exc)
            log.error("agent loop API error turn=%d category=%s: %s",
                      turn, category, exc)
            # Also try to observe ratelimit headers from the failing response
            # so the next pre-check can refuse outright.
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
        turn_text = _join_final_text(final_msg)

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
                    result_content = output
                else:
                    result_content = str(output)
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

        if not tool_results:
            # Pause turn: stop_reason was tool_use but the model emitted only
            # thinking/text and zero tool_use blocks (seen under effort=max +
            # interleaved-thinking). Appending {"role":"user","content":[]}
            # here was the direct cause of the empty-content 400.
            consecutive_empty += 1
            if consecutive_empty < MAX_CONSECUTIVE_EMPTY_TURNS:
                log.info(
                    "pause turn (thinking-only, no tool_use) ch=%d turn=%d "
                    "consecutive=%d → nudging continuation",
                    channel_id, turn + 1, consecutive_empty,
                )
                history.append({
                    "role": "user",
                    "content": [{"type": "text", "text": "続けてください。"}],
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
