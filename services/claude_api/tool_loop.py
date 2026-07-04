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
import logging
import re
import time
import uuid
from dataclasses import dataclass

import anthropic

from config import (
    CLAUDE_MAX_TOKENS,
    DELIB_DISTILL_ENABLED,
    MAX_CONSECUTIVE_EMPTY_TURNS,
    MAX_FINAL_EMPTY_NUDGES,
    MAX_PAUSE_TURN_CONTINUES,
    MAX_TEXT_INVOKE_CORRECTIONS,
    MAX_TEXT_INVOKE_RESCUES,
    MAX_TOOL_ERROR_NUDGES,
    MCP_TOOL_RESULT_CHARS,
    PROACTIVE_COMPACT_ENABLED,
    PROACTIVE_COMPACT_TRIGGER_TOKENS,
    SERVER_SIDE_FALLBACK_ENABLED,
    SLOW_FIRST_BYTE_MS,
    TEXT_INVOKE_RESCUE_ENABLED,
    TOOL_CALL_TIMEOUT_S,
    TOOL_ERROR_MIN_REPLY_CHARS,
)
from services.claude_api.loop_guard import (  # ADR-0036
    RequestBudget,
    ToolLoopDetector,
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
from services.claude_api.errors import (
    classify_anthropic_error,
    coerce_tool_input,
    contains_text_tool_call,
    parse_text_tool_calls,
    resolve_tool_name,
    strip_text_tool_call,
    strip_degraded_opener_token,
)
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
    # Set to the fallback model id (e.g. "claude-opus-4-8") when the primary
    # model (Fable 5) returned stop_reason=refusal and the loop retried the
    # request on the fallback. None when no fallback happened.
    fallback_model: str | None = None
    # Number of leading ``history`` entries that belong to the PRIOR
    # transcript after the loop finished. Equal to the caller's
    # ``prior_turns`` unless an in-loop proactive compaction replaced the
    # prior window mid-request (audit #3) — entry.py slices new turns for
    # JSONL persistence at this index, not at len(prior_history).
    prior_len: int | None = None
    # 機能6: per-turn aggregated usage (input/cache_read/cache_creation/output
    # tokens). Surfaced to bot.py via SessionInfo.last_usage so /context can
    # show "how much room is left before the 500K compaction trigger".
    usage_total: dict | None = None
    # ADR-0036: set when the loop stopped at a resource guard rather than a
    # natural end_turn — one of "turn_ceiling" / "wallclock" / "output_tokens"
    # / "tool_loop". None on clean completion. The reply text and session are
    # still preserved (graceful checkpoint), so "続けて" resumes seamlessly.
    paused_reason: str | None = None


# Header the official CLI sets on every API request once a refusal fallback
# has occurred in the session (claude.exe fetch wrapper, gated on
# hasRefusalFallbackOccurred), so the API can tell fallback-leg traffic apart.
# Same semantics here: set on the in-turn retry leg (fallback_used below) and
# on every turn of a sticky-pinned session (entry.py passes it through
# extra_headers). ADR-0010.
REFUSAL_FALLBACK_HEADER = "x-is-refusal-fallback"


def _model_display(m: str) -> str:
    """Short human label for a model id, for the user-facing fallback note."""
    ml = (m or "").lower()
    if "opus-4-8" in ml:
        return "Opus 4.8"
    if "opus" in ml:
        return "Opus"
    if "fable" in ml:
        return "Fable 5"
    return m or "?"


def _fallback_alert(model: str) -> str:
    """The user-facing alert pushed to Discord when a refusal fell back.

    A blockquote + warning so it reads as a system alert, visually distinct
    from かなえ's reply. Single source of truth — used both for the live note
    (streaming) and the text prepend (non-streaming)."""
    return (
        f"> ⚠️ **Fable 5 が応答を拒否したため {_model_display(model)} に"
        "フォールバックしました**"
    )


# MAX_TEXT_INVOKE_CORRECTIONS / MAX_TEXT_INVOKE_RESCUES / the nudge caps now
# live in config.py (env-tunable). Corrective nudge appended as a user turn
# when a text-form tool call could not be rescued.
_TEXT_INVOKE_CORRECTION = (
    "⚠️ 直前のツール呼び出しが通常テキスト（<invoke …> の生 XML）として出力され、"
    "実行されませんでした。ツール呼び出しは地の文に書かず、正規の tool_use 形式で"
    "呼び出してください。ツール名は広告された完全名（例: "
    "mcp__brave-search__brave_news_search）を使ってください。同じツールを今すぐ"
    "正しい形式で呼び出し直してください。"
)
# (B) Nudge after a tool error when the model gives up with a thin reply.
_TOOL_ERROR_NUDGE = (
    "⚠️ 直前のツール実行がエラーになりました。エラー内容を踏まえて、別の方法で"
    "続行するか、それが無理なら何が起きたのかを説明してください。"
)
# (C) Last-ditch nudge before returning the "(no response)" sentinel.
_FINAL_EMPTY_NUDGE = (
    "応答が空のようです。ここまでで分かったことだけでも要約して返してください。"
)
# ADR-0036: soft-budget wrap-up nudge. Injected once into a tool_results user
# turn when RequestBudget crosses the soft threshold, so the model produces a
# clean hand-off summary before the hard limit forces a checkpoint.
_BUDGET_WRAP_UP_NUDGE = (
    "⏳ このリクエストの実行予算が上限に近づいています。ここまでの成果・現在の状態・"
    "残っているタスクを簡潔にまとめ、新しい重い作業は始めずに一旦区切ってください。"
)
# ADR-0036: loop-detector warn nudge. Injected once when the same tool is being
# called with the same arguments repeatedly (no apparent progress).
_TOOL_LOOP_WARN_NUDGE = (
    "⚠️ 同じツールを同じ引数で繰り返しているようです。進んでいないので、"
    "別のアプローチに切り替えるか、ここまでで分かったことを整理してください。"
)
# ADR-0036: subtext note appended to a graceful checkpoint reply so the user
# knows work was saved and can resume. NOT a NON_PERSISTABLE sentinel — the
# reply carries real prose + full tool context, so it persists and resumes.
_CHECKPOINT_NOTE = (
    "-# ⏸️ 作業が長くなったのでここまでを保存しました。「続けて」で同じ文脈から再開できます。"
)


def _build_checkpoint_result(
    *,
    text_across_turns: list[str],
    last_text: str,
    fallback_used: bool,
    fallback_note_sent_live: bool,
    current_model: str,
    active_mcp_keys: list[str],
    prior_len: int | None,
    usage_totals: dict,
    paused_reason: str,
) -> "AgentLoopResult":
    """Graceful checkpoint (ADR-0036): preserve work instead of discarding it.

    Unlike the pre-ADR-0036 max_turns path (synthetic_session_id=None + a
    NON_PERSISTABLE sentinel, which threw away the whole transcript), this
    returns a REAL session id and a non-sentinel reply. entry.py's existing
    persistence (set_session + JSONL slice) then saves the full structured
    transcript verbatim, so "続けて" resumes with all tool context intact.
    """
    accumulated = "\n\n".join(t for t in text_across_turns if t)
    body = accumulated or last_text
    if body:
        text = body + "\n\n" + _CHECKPOINT_NOTE
    else:
        # Model ran tools but emitted no prose — still give the user a handle.
        text = "⏸️ 作業をここまで保存しました。「続けて」で同じ文脈から再開できます。"
    if fallback_used and not fallback_note_sent_live and body:
        text = _fallback_alert(current_model) + "\n\n" + text
    return AgentLoopResult(
        text=text,
        synthetic_session_id=uuid.uuid4().hex,
        active_mcp_keys=active_mcp_keys,
        fallback_model=current_model if fallback_used else None,
        prior_len=prior_len,
        usage_total=dict(usage_totals),
        paused_reason=paused_reason,
    )


# Read-only tools whose repeated identical calls are less alarming (status
# checks, re-reads). Loop detector gives these extra slack. Distinct from
# CONCURRENCY_SAFE_TOOLS (which includes Agent — repeated identical subagent
# spawns ARE suspicious, so Agent is intentionally excluded here).
_LOOP_READONLY_TOOLS = frozenset({"Read", "Glob", "Grep"})


def _observe_response_headers(source) -> None:
    """Feed rate-limit headers from a response-carrying object (stream or
    SDK exception) into the shared pacing state. Best-effort: never raises.
    Single idiom for both the success and the error path (P1-a)."""
    try:
        resp = getattr(source, "response", None)
        if resp is not None and getattr(resp, "headers", None) is not None:
            observe_ratelimit(resp.headers)
    except Exception as exc:
        log.debug("ratelimit observe failed: %s", exc)


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


def _mark_cache_breakpoint(msg: dict) -> dict:
    """Return a copy of ``msg`` with a ttl:1h breakpoint on its last block."""
    msg = dict(msg)
    content = msg["content"]
    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content,
             "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        ]
    elif isinstance(content, list) and content:
        content = list(content)
        last_block = dict(content[-1])
        last_block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        content[-1] = last_block
        msg["content"] = content
    return msg


def _inject_cache_breakpoints(messages: list[dict]) -> list[dict]:
    """Mirrors the helper in __init__.py (see _build_cached_system above).

    PLACEMENT (CLI parity, binary fn Def — audit #8): breakpoints go on BOTH
    the last and the second-to-last user message. [-2] caches the stable
    prefix (system + all prior turns). [-1] additionally writes the newest
    user turn, so the NEXT request's read window covers this turn's reply +
    tool results instead of stopping at the previous turn — without it, every
    resume re-pays the latest exchange as cache_creation (one leg of the
    ADR-0010 cache_read=0 cascade). Budget: 2 system blocks + these 2 = the
    API's 4-breakpoint maximum.

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
    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if not user_indices:
        return messages
    messages = list(messages)
    targets = {user_indices[-1]}
    if len(user_indices) >= 2:
        targets.add(user_indices[-2])
    for target_idx in targets:
        messages[target_idx] = _mark_cache_breakpoint(messages[target_idx])
    return messages


def _block_to_dict(block) -> dict | None:
    """Convert an anthropic content block (Pydantic model) to a serializable dict.

    Returns ``None`` for blocks that MUST NOT be echoed back to the API on the
    next turn (server-side response metadata that would cause a 400 on replay).
    Callers must ``filter(None, ...)`` before persisting to history.
    """
    btype = getattr(block, "type", None)
    if btype == "fallback":
        # Server-side refusal fallback (ADR-0015): Anthropic includes a
        # ``fallback`` metadata block in the response when it silently switched
        # models mid-stream (Fable 5 refusal → Opus 4.8). This block carries
        # {to:{model:...}, citations:[...]} — response-only metadata that the
        # Messages API refuses on the way back ("messages.N.content.M.fallback
        # .citations: Extra inputs are not permitted", HTTP 400). Drop it so
        # the next turn's history replay doesn't poison the request.
        # rid 0679af30 / haroin 2026-07-02: regression.
        return None
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
    if btype == "server_tool_use":
        # Tool Search Tool（等の server-side tool）の呼び出しブロック。echo 必須
        # だが SDK の model_dump() は `caller: None` を含み、fallback.citations
        # 400（rid 0679af30）と同型の「レスポンス専用フィールド echo で 400」を
        # 踏むため明示的に最小 dict へ直列化する。
        out = {
            "type": "server_tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", None) or {},
        }
        caller = getattr(block, "caller", None)
        if caller is not None:
            out["caller"] = (
                caller.model_dump() if hasattr(caller, "model_dump") else caller
            )
        return out
    if btype in ("tool_search_tool_result", "advisor_tool_result"):
        # server-side tool の result ブロック（tool search / advisor）。
        # SDK model_dump の余剰フィールド 400 を避けて最小 dict へ。
        return {
            "type": btype,
            "tool_use_id": getattr(block, "tool_use_id", ""),
            "content": _serialize_search_result_content(
                getattr(block, "content", None)
            ),
        }
    # Unknown / future block type — best effort.
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return {"type": btype or "unknown"}


def _serialize_search_result_content(content) -> "list[dict] | dict":
    """tool_search_tool_result.content を echo 可能な最小 dict へ直列化する。

    成功時は tool_reference のリスト、エラー時は error オブジェクト。SDK の
    model_dump に頼らず必要フィールドだけ残す（余剰フィールド 400 防止）。
    """
    if content is None:
        return []
    # エラー型（BetaToolSearchToolResultError 等）は list ではなく単一オブジェクト
    if not isinstance(content, (list, tuple)):
        err_code = getattr(content, "error_code", None)
        if err_code is not None:
            return {"type": "tool_search_tool_result_error", "error_code": err_code}
        if isinstance(content, dict):
            return content
        return []
    out: list[dict] = []
    for item in content:
        itype = getattr(item, "type", None) or (
            item.get("type") if isinstance(item, dict) else None
        )
        if itype == "tool_reference":
            name = getattr(item, "tool_name", None) or (
                item.get("tool_name") if isinstance(item, dict) else ""
            )
            out.append({"type": "tool_reference", "tool_name": name or ""})
        elif itype == "text":
            text = getattr(item, "text", None) or (
                item.get("text") if isinstance(item, dict) else ""
            )
            out.append({"type": "text", "text": text or ""})
        elif isinstance(item, dict):
            out.append(item)
    return out


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


def _truncate_head_tail(text: str, cap: int) -> str:
    """Middle-out truncation: keep the head and tail, drop the middle.

    MCP 検索結果は先頭（上位ヒット）と末尾（ページネーション情報等）に価値が
    寄りやすいので、head-only の ``_cap_inline`` ではなく中抜きにする。
    """
    if cap <= 0 or len(text) <= cap:
        return text
    half = max(1, cap // 2)
    omitted = len(text) - cap
    return (
        text[:half]
        + f"\n...[{omitted} chars omitted by MCP_TOOL_RESULT_CHARS cap]...\n"
        + text[-half:]
    )


def _cap_mcp_result_content(
    content: "str | list[dict]", cap: int,
) -> "str | list[dict]":
    """Apply the MCP result char cap to a tool_result content payload.

    str はそのまま中抜き。list（text+image ブロック）は text ブロックにだけ
    総量 cap を按分して適用する（image ブロックは対象外）。
    """
    if cap <= 0:
        return content
    if isinstance(content, str):
        return _truncate_head_tail(content, cap)
    if isinstance(content, list):
        def _is_text(b: object) -> bool:
            return (
                isinstance(b, dict) and b.get("type") == "text"
                and isinstance(b.get("text"), str)
            )

        n_text = sum(1 for b in content if _is_text(b))
        total = sum(len(b["text"]) for b in content if _is_text(b))
        if not n_text or total <= cap:
            return content
        per_block = max(1, cap // n_text)
        return [
            {**b, "text": _truncate_head_tail(b["text"], per_block)}
            if _is_text(b) else b
            for b in content
        ]
    return content


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
        # CLI-aligned "Update Todos" block — mirrors Claude Code's terminal UI:
        #   ☐ pending  /  ☐ **in-progress** (emphasized)  /  ☒ ~~completed~~
        # The ☐/☒ glyphs are the exact ones the CLI binary uses; the checkbox
        # column conveys progress at a glance, so no numbering / count summary
        # (the CLI shows neither). For in-progress, prefer activeForm (the CLI
        # surfaces the present-tense form for the active item). Cap to the inline
        # budget so the Discord status message stays under 2000 chars.
        lines = ["📝 **Update Todos**"]
        for t in todos:
            if not isinstance(t, dict):
                continue
            status = t.get("status")
            if status == "completed":
                lines.append(f"  ☒ ~~{str(t.get('content', ''))}~~")
            elif status == "in_progress":
                active = t.get("activeForm") or t.get("content", "")
                lines.append(f"  ☐ **{str(active)}**")
            else:  # pending / unknown
                lines.append(f"  ☐ {str(t.get('content', ''))}")
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

# MAX_CONSECUTIVE_EMPTY_TURNS (max consecutive thinking-only "pause turns"
# before the loop stops nudging) is imported from config.py above. It bounds a
# model that keeps thinking without acting; the empty-content 400 guard relies
# on it. Re-exported via this module so callers/tests can import it from here.


def _sanitize_content_blocks(content):
    """Drop content blocks that the Messages API rejects on replay.

    ``fallback`` blocks are server-side response metadata (ADR-0015 refusal
    fallback); the API returns them in responses but refuses them on the way
    back with ``messages.N.content.M.fallback.citations: Extra inputs are not
    permitted`` (HTTP 400). This is the single choke-point for both fresh
    in-loop turns and JSONL-resumed history — anything that reaches the API
    passes through ``_normalize_history_for_api``. Non-list content (a plain
    string user turn) is returned unchanged.
    rid 0679af30 / haroin 2026-07-02: regression.
    """
    if not isinstance(content, list):
        return content
    return [b for b in content if not (isinstance(b, dict) and b.get("type") == "fallback")]


# server-side tool のブロック type（tool search / advisor）。無効時の replay
# strip と未対ペア判定の両方で使う。
_TOOL_SEARCH_BLOCK_TYPES = (
    "server_tool_use", "tool_search_tool_result", "advisor_tool_result",
)
_SERVER_TOOL_RESULT_TYPES = ("tool_search_tool_result", "advisor_tool_result")


def strip_tool_search_blocks(content):
    """Tool Search 関連ブロックを content から除去する（純関数）。

    Tool Search 無効時（フラグ OFF / ランタイム auto-off 後）、過去ターンの
    JSONL に残った ``server_tool_use`` / ``tool_search_tool_result`` を tools
    配列に tool search が無いリクエストへ echo すると 400 になり得るため、
    replay 前に落とす。有効時は呼ばない（echo は resume に必要）。
    """
    if not isinstance(content, list):
        return content
    return [
        b for b in content
        if not (isinstance(b, dict) and b.get("type") in _TOOL_SEARCH_BLOCK_TYPES)
    ]


def _has_unpaired_server_tool_use(content) -> bool:
    """content 内に result 未対の server_tool_use があるか。

    Tool Search の検索発行ターンでは assistant content に server_tool_use が
    載り、対応する tool_search_tool_result が同一 content に無いまま
    stop_reason=tool_use / pause_turn で止まることがある。この状態で user
    ターンを挟むと server 側の resume 検出（trailing server_tool_use）が壊れる。
    """
    server_ids: set[str] = set()
    result_ids: set[str] = set()
    for b in content or []:
        btype = getattr(b, "type", None) or (
            b.get("type") if isinstance(b, dict) else None
        )
        if btype == "server_tool_use":
            bid = getattr(b, "id", None) or (
                b.get("id") if isinstance(b, dict) else None
            )
            if bid:
                server_ids.add(bid)
        elif btype in _SERVER_TOOL_RESULT_TYPES:
            rid = getattr(b, "tool_use_id", None) or (
                b.get("tool_use_id") if isinstance(b, dict) else None
            )
            if rid:
                result_ids.add(rid)
    return bool(server_ids - result_ids)


def _normalize_history_for_api(
    history: list[dict], *, strip_tool_search: bool = False,
) -> list[dict]:
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
        content = _sanitize_content_blocks(h["content"])
        if strip_tool_search:
            content = strip_tool_search_blocks(content)
        if content == "" or content == []:
            content = _EMPTY_CONTENT_PLACEHOLDER
        out.append({"role": h["role"], "content": content})
    return out


# Tools that only read state and can safely run concurrently (CLI
# isConcurrencySafe, audit #12). When a turn's tool_use blocks are ALL in
# this set they are dispatched via asyncio.gather; anything else (writes,
# Bash, MCP tools with unknown side effects) keeps the sequential path.
#
# Agent is concurrency-safe because each subagent runs in a fresh context
# (history=[] inside run_subagent), takes no shared mutable state from the
# parent, and is intentionally designed to be fanned out — matching the
# CLI's Promise.all dispatch when the model fires multiple Agent() calls
# in one turn. Per-process throttling lives in agent_tool._subagent_sema
# (SUBAGENT_MAX_PARALLEL), not here.
CONCURRENCY_SAFE_TOOLS = frozenset({"Read", "Glob", "Grep", "Agent"})


def _validate_tool_input(schema, args: dict) -> str | None:
    """Validate ``args`` against an input_schema; return an error string or None.

    Mirrors the CLI's pre-dispatch zod validation (audit #11): a malformed
    call comes back as a ``<tool_use_error>InputValidationError: …</tool_use_error>``
    tool_result naming the offending field, so the model can self-correct in
    one turn instead of decoding an arbitrary handler traceback. Soft
    dependency — when jsonschema is unavailable or the schema itself is
    broken, validation is skipped (the handlers keep their own guards).
    """
    if not isinstance(schema, dict) or not schema:
        return None
    try:
        import jsonschema
    except ImportError:
        return None
    try:
        jsonschema.validate(args, schema)
    except jsonschema.ValidationError as e:
        path = ".".join(str(p) for p in e.absolute_path)
        loc = f" at '{path}'" if path else ""
        return f"{e.message}{loc}"
    except jsonschema.SchemaError:
        return None  # broken schema is our bug; never block the tool on it
    return None


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


# Last observed per-channel context footprint (input + cache_read +
# cache_creation of the most recent turn). entry.py reads this for the
# token-based boundary compaction trigger (audit #7) — real tokens, not the
# 2-4x-noisy char estimate.
_last_context_tokens: dict[int, int] = {}


def get_last_context_tokens(channel_id: int) -> int:
    """Most recent context-size observation for a channel, 0 if none yet."""
    return _last_context_tokens.get(channel_id, 0)


def _context_tokens(values: dict[str, int]) -> int:
    return (
        values["input_tokens"]
        + values["cache_read_input_tokens"]
        + values["cache_creation_input_tokens"]
    )


def _log_turn_usage(
    channel_id: int,
    turn: int,
    final_msg,
    totals: dict[str, int],
    *,
    model: str = "",
    request_id: str = "",
    duration_ms: int | None = None,
    ttft_ms: int | None = None,
    message_count: int = 0,
) -> None:
    """Emit per-turn usage if the SDK message exposes it.

    Stays silent for mocks / responses without ``usage`` so the agent loop
    keeps running. Bumps ``totals`` so a later call to ``_log_total_usage``
    can summarize multi-turn (tool_use) requests.

    ``request_id`` / ``duration_ms`` / ``ttft_ms`` / ``message_count`` mirror
    the CLI's tengu_api_success payload (audit #17) — request-id especially,
    since without it a refusal/error can never be correlated server-side.
    """
    usage = getattr(final_msg, "usage", None)
    if usage is None:
        return
    values = {field: _usage_number(usage, field) for field in _USAGE_FIELDS}
    for field, value in values.items():
        totals[field] += value
    totals["turns"] += 1
    _last_context_tokens[channel_id] = _context_tokens(values)
    stop_reason = getattr(final_msg, "stop_reason", None)
    log.info(
        "claude_api_usage channel=%d turn=%d model=%s input_tokens=%d "
        "cache_read_input_tokens=%d cache_creation_input_tokens=%d "
        "output_tokens=%d stop_reason=%s request_id=%s duration_ms=%s "
        "ttft_ms=%s message_count=%d",
        channel_id, turn + 1,
        model or "-",
        values["input_tokens"],
        values["cache_read_input_tokens"],
        values["cache_creation_input_tokens"],
        values["output_tokens"],
        stop_reason,
        request_id or "-",
        duration_ms if duration_ms is not None else "-",
        ttft_ms if ttft_ms is not None else "-",
        message_count,
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


# --- DIAGNOSTIC (temporary): capture the exact request that produced a refusal.
# stop_reason=refusal with category=None is not a normal safety refusal; this
# dumps the full system + messages + config + response headers for the live
# request so we can inspect what fable-5 actually rejected. Bounded per process,
# best-effort, never raises into the agent loop. Remove once root-caused.
_REFUSAL_CAPTURES = 0
_REFUSAL_CAPTURE_MAX = 15


def _capture_refusal_request(channel_id, turn, model, cached_system, messages,
                             betas, final_msg, resp_headers):
    global _REFUSAL_CAPTURES
    if _REFUSAL_CAPTURES >= _REFUSAL_CAPTURE_MAX:
        return
    try:
        import json as _json
        import os as _os
        import time as _time
        sd = getattr(final_msg, "stop_details", None)
        usage = getattr(final_msg, "usage", None)
        hdrs = {}
        try:
            for k, v in dict(resp_headers or {}).items():
                kl = k.lower()
                if kl.startswith("anthropic-ratelimit") or kl in (
                    "request-id", "retry-after", "anthropic-beta", "x-should-retry",
                ):
                    hdrs[k] = v
        except Exception:
            pass
        payload = {
            "channel_id": channel_id, "turn": turn, "model": model,
            "betas": list(betas or []),
            "stop_reason": getattr(final_msg, "stop_reason", None),
            "stop_details": {
                "category": getattr(sd, "category", None) if sd else None,
                "explanation": getattr(sd, "explanation", None) if sd else None,
            },
            "usage": {
                f: getattr(usage, f, None) if usage else None
                for f in ("input_tokens", "cache_read_input_tokens",
                          "cache_creation_input_tokens", "output_tokens")
            },
            "response_headers": hdrs,
            "system": cached_system,
            "messages": messages,
        }
        d = "/tmp/fable5_capture"
        _os.makedirs(d, exist_ok=True)
        ts = _time.strftime("%Y%m%dT%H%M%S")
        path = _os.path.join(d, f"bot_refusal_{channel_id}_{turn}_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False, indent=2)
        _os.chmod(path, 0o600)
        _REFUSAL_CAPTURES += 1
        log.warning("captured refusal request -> %s", path)
    except Exception as exc:  # noqa: BLE001
        log.debug("refusal capture failed: %s", exc)


@dataclass
class _RescuedToolUse:
    """A lightweight stand-in for an anthropic tool_use block, minted when a
    text-form ``<invoke>`` is rescued. ``_run_one_tool`` reads id/name/input via
    getattr, so this duck-types as the real block on the execution path."""
    id: str
    name: str
    input: dict
    type: str = "tool_use"


def _build_stream_kwargs(
    *,
    current_model: str,
    cached_system: "list[dict]",
    messages: "list[dict]",
    extra_betas,
    output_config_override,
    refusal_fallback_model: "str | None",
    tools_for_api,
    extra_headers,
    fallback_used: bool,
) -> dict:
    """Assemble the per-turn ``messages.stream`` kwargs (P3-b extraction).

    Body fields the CLI pairs with its betas (mitm capture 2026-05-15,
    docs/captures/A_one_shot/): output_config + context_management ride as
    top-level body fields.

    - output_config MUST be a direct kwarg, NOT nested in extra_body: the
      anthropic SDK 0.88.0 beta streaming path silently drops it from
      extra_body (verified by mitmproxy — effort never reached the wire),
      which made the server see a contradictory request and return
      stop_reason=refusal (ADR-0009). /effort override (機能6) takes
      precedence per request; falls through to env-driven OUTPUT_CONFIG.
    - Server-side model fallback (ADR-0015): when a refusal fallback model
      is in play, declare it via the ``fallbacks`` body field so the server
      switches mid-stream on a Fable refusal — no client-side second
      request. SDK doesn't model ``fallbacks``, so it rides extra_body.
    """
    max_tokens = (
        CLAUDE_MAX_TOKENS_OPUS if "opus" in (current_model or "").lower()
        else CLAUDE_MAX_TOKENS
    )
    stream_kwargs = dict(
        model=current_model,
        max_tokens=max_tokens,
        system=cached_system,
        messages=messages,
        betas=extra_betas,
        metadata=get_metadata(),
        thinking=THINKING_CONFIG,
        output_config=output_config_override or OUTPUT_CONFIG,
        extra_body={
            "context_management": CONTEXT_MANAGEMENT_CONFIG,
        },
    )
    if SERVER_SIDE_FALLBACK_ENABLED and refusal_fallback_model:
        stream_kwargs["extra_body"]["fallbacks"] = [
            {"model": refusal_fallback_model}
        ]
    if tools_for_api:
        stream_kwargs["tools"] = tools_for_api
    merged_headers = dict(extra_headers or {})
    if fallback_used:
        merged_headers[REFUSAL_FALLBACK_HEADER] = "true"
    if merged_headers:
        stream_kwargs["extra_headers"] = merged_headers
    return stream_kwargs


async def _run_one_tool(
    block,
    *,
    can_use_tool,
    execute_tool,
    tool_schemas: dict,
    status_callback,
    tool_invocation_callback,
    turn: int,
    evidence=None,  # services.claude_api.evidence.EvidenceIndex | None
) -> dict:
    """Execute one tool_use block → a tool_result dict（evidence/蒸留の wrapper）.

    実行本体は ``_run_one_tool_inner``。この wrapper が
      1. evidence index への台帳記録（seq は最初の await 前に同期採番 —
         asyncio.gather は生成順に最初の同期区間を走らせるため block 順が保たれる）
      2. result distillation（原文に対して蒸留 → 成功時 content 差し替え）
      3. MCP 結果の文字数 cap（蒸留失敗時の最終防壁。蒸留前の原文は evidence 側に残る）
    を一手に担う。normal / text-form rescue の両経路がここを通る。
    """
    seq = evidence.next_seq() if evidence is not None else 0
    result = await _run_one_tool_inner(
        block,
        can_use_tool=can_use_tool,
        execute_tool=execute_tool,
        tool_schemas=tool_schemas,
        status_callback=status_callback,
        tool_invocation_callback=tool_invocation_callback,
        turn=turn,
    )
    tool_name = getattr(block, "name", "")
    tool_input = getattr(block, "input", None) or {}
    content = result.get("content")
    is_error = bool(result.get("is_error"))

    if evidence is not None:
        from services.claude_api.distill import (
            distill_result,
            render_note_for_model,
            should_distill,
        )
        rec = await evidence.add(
            seq=seq, turn=turn, tool=tool_name, tool_input=tool_input,
            content=content, is_error=is_error,
        )
        if (
            DELIB_DISTILL_ENABLED
            and not is_error
            and should_distill(tool_name, content)
        ):
            note = await distill_result(tool_name, tool_input, content)
            if note is not None:
                evidence.attach_note(rec, note, distilled=True)
                content = render_note_for_model(note, rec.raw_path, rec.raw_chars)
                result = dict(result)
                result["content"] = content

    # MCP tool 結果のみ文字数 cap（builtin は各自の cap を持つ）。蒸留成功時は
    # note が cap 未満なので no-op、失敗時はこれが従来どおりの防壁になる。
    if tool_name.startswith("mcp__"):
        capped = _cap_mcp_result_content(
            result.get("content"), MCP_TOOL_RESULT_CHARS,
        )
        if capped is not result.get("content"):
            result = dict(result)
            result["content"] = capped
    return result


async def _run_one_tool_inner(
    block,
    *,
    can_use_tool,
    execute_tool,
    tool_schemas: dict,
    status_callback,
    tool_invocation_callback,
    turn: int,
) -> dict:
    """Execute one tool_use block → a tool_result dict.

    Shared by the normal tool-execution path and the text-form rescue path so
    pre-dispatch validation, can_use_tool gating, the timeout cap, and error
    shaping are identical for both. Was a nested closure in run_agent_loop;
    hoisted to module scope (2026-06-21) so the rescue helper can reuse it.
    """
    tool_id = getattr(block, "id", "")
    tool_name = getattr(block, "name", "")
    tool_input = getattr(block, "input", None) or {}

    # Notify the caller that a tool is about to run. bot.py renders this as a
    # single edit-overwriting status message so the transcript stays clean.
    if tool_invocation_callback is not None:
        try:
            await tool_invocation_callback(tool_name, tool_input)
        except Exception as exc:
            log.warning(
                "tool_invocation_callback failed (tool=%s turn=%d): %s",
                tool_name, turn, exc,
            )

    # Pre-dispatch schema validation (audit #11): a malformed call is answered
    # in the CLI's <tool_use_error> shape, naming the field, so the model can
    # fix it in one turn.
    validation_error = _validate_tool_input(
        tool_schemas.get(tool_name), tool_input,
    )
    if validation_error is not None:
        log.info(
            "tool %s input validation failed: %s",
            tool_name, validation_error,
        )
        await _safe_status(status_callback, "❌ ツール入力エラー")
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": (
                "<tool_use_error>InputValidationError: "
                f"{validation_error}</tool_use_error>"
            ),
            "is_error": True,
        }

    allowed, reason = can_use_tool(tool_name, tool_input)
    if not allowed:
        await _safe_status(status_callback, "❌ ツール拒否")
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": f"Tool {tool_name} denied: {reason}",
            "is_error": True,
        }

    try:
        t0 = time.monotonic()
        # Hard cap so a hung tool/MCP call can't wedge the turn (and the whole
        # channel) forever. On timeout we return an is_error tool_result, so the
        # loop can continue or wind down cleanly.
        output = await asyncio.wait_for(
            execute_tool(tool_name, tool_input),
            timeout=TOOL_CALL_TIMEOUT_S,
        )
        dt = time.monotonic() - t0
        # Anthropic tool_result content accepts either a string or a list of
        # content blocks (text + image). The Read tool's image branch returns
        # the latter; everything else returns a plain string. Anything weird
        # falls back to str() so we never wedge the API call.
        if isinstance(output, list):
            result_content: "str | list[dict]" = output
        elif isinstance(output, str):
            result_content = output
        else:
            result_content = str(output)
        # NOTE: MCP 結果の文字数 cap は wrapper（_run_one_tool）側で、蒸留の
        # 後に適用する（蒸留は原文に対して行いたいため）。
        await _safe_status(
            status_callback,
            f"📨 結果を受信 ({dt:.1f}s)",
        )
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": result_content,
        }
    except (asyncio.TimeoutError, TimeoutError):
        log.warning(
            "tool %s timed out after %ds", tool_name, TOOL_CALL_TIMEOUT_S,
        )
        await _safe_status(status_callback, "⏰ ツールがタイムアウト")
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": (
                f"<tool_use_error>Tool {tool_name} timed out after "
                f"{TOOL_CALL_TIMEOUT_S}s and was aborted.</tool_use_error>"
            ),
            "is_error": True,
        }
    except Exception as exc:
        log.warning("tool %s failed: %s", tool_name, exc)
        await _safe_status(status_callback, "❌ ツールエラー")
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": f"<tool_use_error>{exc}</tool_use_error>",
            "is_error": True,
        }


async def _attempt_text_invoke_rescue(
    turn_text: str,
    *,
    history: list[dict],
    tools_for_api: list[dict],
    tool_schemas: dict,
    can_use_tool,
    execute_tool,
    status_callback,
    tool_invocation_callback,
    channel_id: int,
    turn: int,
    evidence=None,
) -> "list[dict] | None":
    """Rescue tool calls the model emitted as plain text.

    Parses the ``<invoke>`` span(s) in ``turn_text``, resolves each name against
    the advertised tools, and — only if EVERY call resolves — rewrites the
    poisoned assistant turn in place to carry real tool_use blocks, executes
    them, and appends the matching tool_result user turn. Returns the
    tool_results on success, or ``None`` (nothing mutated beyond a no-op) when
    rescue is impossible so the caller falls back to a corrective re-prompt.

    All-or-nothing by design: a partially-rescued turn (one unresolved call)
    would leave an orphan tool_use / unanswered result, which is worse than a
    clean re-prompt.
    """
    parsed = parse_text_tool_calls(turn_text)
    if not parsed:
        return None
    advertised = [
        t.get("name") for t in (tools_for_api or [])
        if isinstance(t, dict) and t.get("name")
    ]
    resolved: list[tuple[str, dict]] = []
    for call in parsed:
        real = resolve_tool_name(call["name"], advertised)
        if real is None:
            return None  # unresolved / ambiguous → abort the whole rescue
        resolved.append((real, call["input"]))

    pseudo_blocks = [
        _RescuedToolUse(
            id=f"toolu_rescue_{uuid.uuid4().hex[:24]}",
            name=real,
            input=coerce_tool_input(raw, tool_schemas.get(real)),
        )
        for real, raw in resolved
    ]

    # History surgery (400-avoidance core): replace the poisoned assistant turn
    # in place so the raw <invoke> XML never persists and the turn legitimately
    # ends in tool_use blocks. entry.py slices new turns from `history` for JSONL
    # persistence, so replacing (not appending) keeps the XML off disk and avoids
    # an orphan tool_use on resume.
    clean_text = strip_text_tool_call(turn_text)
    new_content: list[dict] = []
    if clean_text.strip():
        new_content.append({"type": "text", "text": clean_text})
    new_content.extend(
        {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
        for b in pseudo_blocks
    )
    if history and history[-1].get("role") == "assistant":
        history[-1] = {"role": "assistant", "content": new_content}
    else:  # defensive — the assistant turn was just appended by the caller
        history.append({"role": "assistant", "content": new_content})

    # Execute via the shared path. Read-only batches run concurrently (audit
    # #12); anything else stays sequential.
    if len(pseudo_blocks) > 1 and all(
        b.name in CONCURRENCY_SAFE_TOOLS for b in pseudo_blocks
    ):
        tool_results = list(await asyncio.gather(*(
            _run_one_tool(
                b, can_use_tool=can_use_tool, execute_tool=execute_tool,
                tool_schemas=tool_schemas, status_callback=status_callback,
                tool_invocation_callback=tool_invocation_callback, turn=turn,
                evidence=evidence,
            ) for b in pseudo_blocks
        )))
    else:
        tool_results = [
            await _run_one_tool(
                b, can_use_tool=can_use_tool, execute_tool=execute_tool,
                tool_schemas=tool_schemas, status_callback=status_callback,
                tool_invocation_callback=tool_invocation_callback, turn=turn,
                evidence=evidence,
            )
            for b in pseudo_blocks
        ]
    # tool_result ids equal the tool_use ids written above → strict pairing.
    history.append({"role": "user", "content": tool_results})
    log.warning(
        "[ch=%d] turn=%d rescued %d text-form tool call(s): %s",
        channel_id, turn + 1, len(pseudo_blocks),
        [b.name for b in pseudo_blocks],
    )
    return tool_results


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
    incremental_text_callback=None,   # async (text:str)->None  (per-turn final)
    text_stream_callback=None,        # async (partial_turn_text:str)->None
    tool_invocation_callback=None,    # async (name:str, args:dict)->None
    scratch_stream_callback=None,     # async (section:str, content:str, op:str)->None
    scratch_finalize_callback=None,   # async ()->None  (Scratchpad block closed)
    progress=None,                    # mutable {"tokens": int} for the live spinner
    max_turns: int,
    stall_watchdog: StallWatchdog,
    api_client: anthropic.AsyncAnthropic,
    model: str,
    extra_betas: list[str],
    extra_headers: dict | None = None,
    active_mcp_keys: list[str] | None = None,
    refusal_fallback_model: str | None = None,
    proactive_compact=None,  # async () -> list[dict] | None (new prior window)
    prior_turns: int = 0,
    output_config_override: dict | None = None,  # /effort per-request override
    tool_search_enabled: bool = False,  # Tool Search Tool (defer_loading) 有効時 True
    evidence=None,  # services.claude_api.evidence.EvidenceIndex | None（熟考P1）
    budget_role: str = "admin",  # ADR-0036: "admin"/"restricted"/"subagent"
    request_budget=None,  # ADR-0036: RequestBudget override (else built from role)
    loop_detector=None,  # ADR-0036: ToolLoopDetector override (else built from role)
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
    # Refusal fallback state. When Fable 5 returns stop_reason=refusal (its
    # category=None content classifier — not a real safety stop for this private
    # deployment), retry the SAME request once on ``refusal_fallback_model``
    # (Opus 4.8), which does not share that classifier. The switch is surfaced
    # to the user (live note when streaming, prepended note otherwise) — never
    # silent.
    current_model = model
    fallback_used = False
    fallback_note_sent_live = False
    # Text-form tool-call guard. If the model emits a tool call as plain TEXT
    # (`<invoke …>`) with no real tool_use block, nothing executes; re-prompt
    # once for a proper call before giving up (2026-06-13 poison, see
    # docs/2026-06-13_tool_call_text_invoke_poison.md).
    text_invoke_corrections = 0
    # Text-form rescue counter (2026-06-21): each successful rescue (parse the
    # text <invoke> and run it as a real tool_use) increments this; bounded by
    # MAX_TEXT_INVOKE_RESCUES so a model that only ever text-forms can't spin.
    text_invoke_rescues = 0
    # (B) Did the most recently appended tool_results batch contain an error?
    # Drives the one-shot recovery nudge when the model then gives up.
    last_turn_had_tool_error = False
    error_recovery_nudges = 0
    # (C) Last-ditch summary nudge before returning "(no response)".
    final_empty_nudges = 0
    # In-loop proactive compaction state (audit #3, ADR-0012). ``prior_len``
    # tracks how many leading history entries belong to the prior transcript;
    # a mid-loop compaction replaces history[:prior_len] with the collapsed
    # window and updates it. At most one compaction per agent loop — repeated
    # refills within one request are thrashing (rapid-refill breaker territory),
    # not something more summarization fixes.
    prior_len = prior_turns
    proactive_compacted = False
    # name → input_schema for pre-dispatch validation (audit #11).
    tool_schemas = {
        t.get("name"): t.get("input_schema")
        for t in (tools_for_api or [])
        if isinstance(t, dict)
    }

    # Running output-token total across completed turns, surfaced live to the
    # bot.py spinner via the shared `progress` dict.
    _tok_base = 0

    # server-side tool (Tool Search) の pause_turn 再開回数。userターンを足さずに
    # そのまま再送する continue の暴走防止（MAX_PAUSE_TURN_CONTINUES で打ち切り）。
    pause_turn_continues = 0

    # ADR-0036: 予算 + ループ検知。回数上限は非常停止に格下げし、これらが実務上の
    # リミッタになる。呼び出し側が override を渡さなければ role から構築する。
    budget = request_budget or RequestBudget.for_role(budget_role)
    detector = loop_detector or ToolLoopDetector.for_role(
        budget_role, readonly_names=_LOOP_READONLY_TOOLS,
    )
    # soft 予算到達で1回だけ立てる。次の tool_results ターンに wrap-up nudge を混ぜる。
    wrap_up_requested = False
    wrap_up_injected = False

    async def _emit_checkpoint(paused_reason: str) -> AgentLoopResult:
        """ADR-0036: graceful checkpoint を組み立てて返す。streaming 時は本文が
        既にライブ配信済みなので、末尾の resume ノートだけを追加でライブ送信して
        ユーザーに「続けて」導線を見せる（fallback_alert と同じ流儀）。"""
        _log_total_usage(channel_id, usage_totals)
        result = _build_checkpoint_result(
            text_across_turns=text_across_turns,
            last_text=last_text,
            fallback_used=fallback_used,
            fallback_note_sent_live=fallback_note_sent_live,
            current_model=current_model,
            active_mcp_keys=active_mcp_keys,
            prior_len=prior_len,
            usage_totals=usage_totals,
            paused_reason=paused_reason,
        )
        if incremental_text_callback is not None:
            _accumulated = "\n\n".join(t for t in text_across_turns if t)
            _note = _CHECKPOINT_NOTE if _accumulated else result.text
            try:
                await incremental_text_callback(_note)
            except Exception as exc:  # noqa: BLE001 — 表示失敗で loop を殺さない
                log.warning("[ch=%d] checkpoint note stream failed: %s", channel_id, exc)
        return result

    for turn in range(max_turns):
        # ADR-0036: リクエスト予算のチェック（API 呼び出し前）。hard は即 checkpoint、
        # soft は wrap-up を1回予約。前ターンまでの出力トークンと経過時間を反映。
        _bstatus = budget.status()
        if _bstatus.state == "hard":
            log.warning(
                "[ch=%d] turn=%d request budget hard limit → checkpoint: %s",
                channel_id, turn, _bstatus.reason,
            )
            _paused = "wallclock" if "wallclock" in _bstatus.reason else "output_tokens"
            return await _emit_checkpoint(_paused)
        if _bstatus.state == "soft" and not wrap_up_requested:
            wrap_up_requested = True
            log.info(
                "[ch=%d] turn=%d request budget soft → wrap-up requested: %s",
                channel_id, turn, _bstatus.reason,
            )

        messages = _normalize_history_for_api(
            history, strip_tool_search=not tool_search_enabled,
        )
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
                prior_len=prior_len,
            )
        # Soft pacing — sleep up to a few seconds if last response told us
        # we're in the warning zone, so we don't trip the probabilistic throttle.
        await maybe_wait_before_request()

        stream_kwargs = _build_stream_kwargs(
            current_model=current_model,
            cached_system=cached_system,
            messages=messages,
            extra_betas=extra_betas,
            output_config_override=output_config_override,
            refusal_fallback_model=refusal_fallback_model,
            tools_for_api=tools_for_api,
            extra_headers=extra_headers,
            fallback_used=fallback_used,
        )

        # Live token stream for the CURRENT turn's visible text. Accumulated
        # across text_delta events so text_stream_callback always receives the
        # full prose-so-far (bot.py edits one draft message, throttled). Reset
        # each turn so a new turn starts a fresh partial.
        live_turn_text = ""
        # Server-side fallback detection (ADR-0015). The server signals a
        # mid-stream model switch via a content_block_start whose content_block
        # type is "fallback" (carrying from.model / to.model). SDK 0.88.0 does
        # not model this block, so we sniff the raw event defensively. Set to
        # the fallback target model id when seen.
        server_fallback_to: str | None = None
        # Per-attempt timing (audit #16/#17): dispatch → first stream event is
        # the TTFB leg the StallWatchdog can't see (its heartbeat starts on the
        # first event, so a slow first byte and a mid-stream stall look alike).
        request_start = time.monotonic()
        ttft_ms: int | None = None
        # Tracks the tool_use block currently being streamed (its input
        # arrives incrementally via "input_json" events, one block at a
        # time since the API streams content_blocks sequentially). Reset on
        # content_block_stop so a later text block after this tool doesn't
        # inherit a stale tool identity.
        cur_tool: dict | None = None
        try:
            async with api_client.beta.messages.stream(**stream_kwargs) as stream:
                async for event in stream:
                    stall_watchdog.heartbeat()
                    if ttft_ms is None:
                        ttft_ms = int((time.monotonic() - request_start) * 1000)
                        if ttft_ms > SLOW_FIRST_BYTE_MS:
                            log.warning(
                                "claude_api_slow_first_byte channel=%d turn=%d "
                                "ttft_ms=%d threshold_ms=%d model=%s",
                                channel_id, turn + 1, ttft_ms,
                                SLOW_FIRST_BYTE_MS, current_model,
                            )
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        btype = getattr(block, "type", None) if block else None
                        if btype == "fallback":
                            # Server-side model fallback fired mid-stream
                            # (ADR-0015). content_block.to.model is the model
                            # that actually produced this response.
                            _to = getattr(block, "to", None)
                            to_model = getattr(_to, "model", None)
                            if isinstance(_to, dict):
                                to_model = _to.get("model")
                            if isinstance(to_model, str) and to_model:
                                server_fallback_to = to_model
                                log.warning(
                                    "[ch=%d] turn=%d server-side fallback → %s",
                                    channel_id, turn + 1, to_model,
                                )
                            continue
                        if btype == "thinking":
                            await _safe_status(status_callback, "💭 thinking…")
                        elif btype == "text":
                            await _safe_status(status_callback, "📝 応答を生成中")
                        elif btype == "server_tool_use":
                            # Tool Search 等の server-side tool 呼び出し。実行は
                            # API 側なので client 処理は不要、進捗表示とログのみ。
                            name = getattr(block, "name", "?")
                            log.info(
                                "[ch=%d] turn=%d server_tool_use: name=%s",
                                channel_id, turn + 1, name,
                            )
                            await _safe_status(status_callback, "🔎 ツール検索中")
                        elif btype == "tool_use":
                            name = getattr(block, "name", "?")
                            tool_input = getattr(block, "input", None)
                            hint = _tool_input_hint(name, tool_input)
                            short_name = name.split("__", 2)[-1] if "__" in name else name
                            cur_tool = {"name": name, "short": short_name}
                            # journal にも tool 名 + input を残す (audit / debug 用).
                            # input は最大 300 文字でクリップ。MCP 通信失敗時の切り分けに使う。
                            log.info(
                                "[ch=%d] turn=%d tool_use: name=%s input=%s",
                                channel_id, turn + 1, name,
                                str(tool_input)[:300] if tool_input is not None else "",
                            )
                            label = f"🔧 {short_name}"
                            if hint:
                                label += f" `{hint[:60]}`"
                            await _safe_status(status_callback, label)
                        else:
                            cur_tool = None
                    elif etype == "input_json" and cur_tool is not None:
                        # Synthesized by the SDK's beta stream helper from
                        # input_json_delta events — event.snapshot is the
                        # jiter-parsed dict-so-far of the tool_use input, so
                        # no partial-JSON parsing is needed here.
                        snapshot = getattr(event, "snapshot", None) or {}
                        if isinstance(snapshot, dict):
                            hint = _tool_input_hint(cur_tool["name"], snapshot)
                            if hint:
                                label = f"🔧 {cur_tool['short']}"
                                label += f" `{hint[:60]}`"
                                await _safe_status(status_callback, label)
                            if (
                                cur_tool["short"] == "Scratchpad"
                                and scratch_stream_callback is not None
                            ):
                                section = snapshot.get("section")
                                content = snapshot.get("content")
                                op = snapshot.get("op")
                                if section and content:
                                    try:
                                        await scratch_stream_callback(section, content, op)
                                    except Exception as exc:
                                        log.debug(
                                            "scratch_stream_callback failed: %s", exc,
                                        )
                    elif etype == "content_block_stop":
                        if (
                            cur_tool is not None
                            and cur_tool["short"] == "Scratchpad"
                            and scratch_finalize_callback is not None
                        ):
                            try:
                                await scratch_finalize_callback()
                            except Exception as exc:
                                log.debug("scratch_finalize_callback failed: %s", exc)
                        cur_tool = None
                    elif etype == "message_delta":
                        # Live output-token count for the spinner: cumulative
                        # for this stream + already-completed turns.
                        if progress is not None:
                            _u = getattr(event, "usage", None)
                            if _u is not None:
                                progress["tokens"] = _tok_base + _usage_number(
                                    _u, "output_tokens",
                                )
                    elif etype == "content_block_delta" and text_stream_callback is not None:
                        delta = getattr(event, "delta", None)
                        if getattr(delta, "type", None) == "text_delta":
                            piece = getattr(delta, "text", "") or ""
                            if piece:
                                live_turn_text += piece
                                # bot.py owns throttling (≤1 edit / 1.5s) and
                                # delivery; never let a UI hiccup abort the loop.
                                try:
                                    await text_stream_callback(live_turn_text)
                                except Exception as exc:
                                    log.debug("text_stream_callback failed: %s", exc)
                final_msg = await stream.get_final_message()
                request_id = ""
                try:
                    _resp = getattr(stream, "response", None)
                    _hdrs = getattr(_resp, "headers", None) if _resp else None
                    if _hdrs is not None:
                        request_id = _hdrs.get("request-id", "") or ""
                except Exception:
                    pass
                _log_turn_usage(
                    channel_id, turn, final_msg, usage_totals,
                    # current_model, not the request arg: a mid-loop refusal
                    # fallback swaps models between turns and the log must
                    # show which one actually answered.
                    model=current_model,
                    request_id=request_id,
                    duration_ms=int((time.monotonic() - request_start) * 1000),
                    ttft_ms=ttft_ms,
                    message_count=len(messages),
                )
                # Fold this turn's output into the running spinner total so the
                # next turn's live count continues from here.
                _this_turn_output = _usage_number(
                    getattr(final_msg, "usage", None), "output_tokens",
                )
                _tok_base += _this_turn_output
                # ADR-0036: feed the output-token budget (checked at loop top).
                budget.add_output_tokens(_this_turn_output)
                if progress is not None:
                    progress["tokens"] = _tok_base
                if getattr(final_msg, "stop_reason", None) == "refusal":
                    try:
                        _resp = getattr(stream, "response", None)
                        _hdrs = getattr(_resp, "headers", None) if _resp else None
                        _capture_refusal_request(
                            channel_id, turn, model, cached_system, messages,
                            extra_betas, final_msg, _hdrs,
                        )
                    except Exception:
                        pass
                # Update the shared rate-limit state from the response so
                # the next turn (and the next call from entry.py) can pace
                # itself before sending. anthropic SDK exposes the raw
                # response via stream.response.headers.
                _observe_response_headers(stream)

                # Server-side fallback detection (ADR-0015), multiplexed so any
                # one signal counts: (1) the mid-stream `fallback` content block
                # sniffed above, (2) final_msg.model diverging from the model we
                # asked for (if the SDK propagated it), (3) the
                # x-is-refusal-fallback response header. When a server-side
                # fallback fired we treat it exactly like the client-side one:
                # mark fallback_used, switch current_model so the result carries
                # the real answering model, and surface the alert. The
                # client-side stop_reason==refusal retry below then does NOT
                # fire (the server already produced a good answer).
                if not fallback_used and refusal_fallback_model:
                    detected = server_fallback_to
                    final_model = getattr(final_msg, "model", None)
                    if (
                        not detected
                        and isinstance(final_model, str)
                        and final_model
                        and final_model != current_model
                    ):
                        detected = final_model
                    if not detected:
                        try:
                            _resp = getattr(stream, "response", None)
                            _hdrs = getattr(_resp, "headers", None) if _resp else None
                            if _hdrs is not None and _hdrs.get(
                                REFUSAL_FALLBACK_HEADER
                            ) == "true":
                                detected = refusal_fallback_model
                        except Exception:
                            pass
                    if detected:
                        fallback_used = True
                        current_model = detected
                        log.warning(
                            "[ch=%d] turn=%d server-side fallback confirmed "
                            "(answering model=%s)",
                            channel_id, turn + 1, detected,
                        )
                        if (
                            incremental_text_callback is not None
                            and not fallback_note_sent_live
                        ):
                            try:
                                await incremental_text_callback(
                                    _fallback_alert(current_model)
                                )
                                fallback_note_sent_live = True
                            except Exception as exc:
                                log.warning(
                                    "server fallback alert callback failed: %s", exc
                                )
        except anthropic.AnthropicError as exc:
            category = classify_anthropic_error(exc)
            log.error("agent loop API error turn=%d category=%s: %s",
                      turn, category, exc)
            # Also try to observe ratelimit headers from the failing response
            # so the next pre-check can refuse outright.
            _observe_response_headers(exc)
            # Caller (entry.py) owns retry/backoff; surface the exception.
            raise

        # Persist the assistant turn (in-memory only; disk write is caller's job)
        # filter(None, ...): _block_to_dict returns None for server-only
        # metadata blocks (e.g. ``fallback``) that would cause a 400 on replay.
        assistant_blocks = [
            d for d in (_block_to_dict(b) for b in (final_msg.content or [])) if d is not None
        ]
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

        # Text-form tool-call handling: the model wrote a tool call as plain
        # text (`<invoke …>`) instead of a real tool_use block, so nothing ran.
        # FIRST try to RESCUE it — parse the <invoke>, resolve the (possibly
        # abbreviated) tool name, rewrite this assistant turn in place to carry
        # real tool_use blocks, and execute them. If the call can't be parsed
        # or its name can't be resolved unambiguously, fall back to a corrective
        # re-prompt (bounded by MAX_TEXT_INVOKE_CORRECTIONS). The persist/replay
        # chokepoints strip the raw XML so it never poisons the transcript even
        # if both paths are exhausted.
        if not fired_tools_this_turn and contains_text_tool_call(turn_text):
            rescued_results = None
            if (
                TEXT_INVOKE_RESCUE_ENABLED
                and text_invoke_rescues < MAX_TEXT_INVOKE_RESCUES
            ):
                rescued_results = await _attempt_text_invoke_rescue(
                    turn_text,
                    history=history,
                    tools_for_api=tools_for_api,
                    tool_schemas=tool_schemas,
                    can_use_tool=can_use_tool,
                    execute_tool=execute_tool,
                    status_callback=status_callback,
                    tool_invocation_callback=tool_invocation_callback,
                    channel_id=channel_id,
                    turn=turn,
                    evidence=evidence,
                )
            if rescued_results is not None:
                text_invoke_rescues += 1
                consecutive_empty = 0
                last_turn_had_tool_error = any(
                    r.get("is_error") for r in rescued_results
                )
                continue
            if text_invoke_corrections < MAX_TEXT_INVOKE_CORRECTIONS:
                text_invoke_corrections += 1
                history.append({
                    "role": "user",
                    "content": [{"type": "text", "text": _TEXT_INVOKE_CORRECTION}],
                })
                consecutive_empty = 0
                log.warning(
                    "[ch=%d] turn=%d text-form tool call emitted as text → "
                    "corrective re-prompt (%d/%d)",
                    channel_id, turn + 1, text_invoke_corrections,
                    MAX_TEXT_INVOKE_CORRECTIONS,
                )
                await _safe_status(status_callback, "🔧 ツール呼び出しをやり直し中")
                continue

        # Belt-and-suspenders: if a text-form call slipped through (correction
        # budget exhausted), strip the raw XML so neither Discord nor the
        # accumulated answer shows it.
        if turn_text and contains_text_tool_call(turn_text):
            turn_text = strip_text_tool_call(turn_text)

        # Degraded opener tokens (course/court/sit/…) can co-occur with a REAL
        # tool_use block: then fired_tools_this_turn is non-empty and
        # contains_text_tool_call is False, so neither the rescue path nor the
        # strip above fires, and the bare token survives into Discord AND the
        # transcript (self-poison seed). Strip a token left isolated on its own
        # line, regardless of tool_use co-occurrence.
        if turn_text:
            turn_text = strip_degraded_opener_token(turn_text)

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

        # Refusal fallback: Fable 5's content classifier (stop_reason=refusal,
        # category=None) does not fire on Opus 4.8. Retry the SAME request once
        # on the fallback model. The refused assistant turn was just appended
        # above — pop it so the retry re-sends the identical pre-turn context.
        if (
            stop_reason == "refusal"
            and refusal_fallback_model
            and not fallback_used
            and current_model != refusal_fallback_model
        ):
            if history and history[-1].get("role") == "assistant":
                history.pop()
            fallback_used = True
            current_model = refusal_fallback_model
            consecutive_empty = 0
            log.warning(
                "[ch=%d] turn=%d Fable refusal → fallback to %s",
                channel_id, turn + 1, current_model,
            )
            await _safe_status(
                status_callback,
                f"⚠️ {_model_display(current_model)} にフォールバック中",
            )
            # Push the alert into the Discord conversation (persistent), not just
            # the transient status, so the user always sees that a fallback
            # happened. When not streaming, it is prepended to the reply below.
            if incremental_text_callback is not None:
                try:
                    await incremental_text_callback(_fallback_alert(current_model))
                    fallback_note_sent_live = True
                except Exception as exc:
                    log.warning("fallback alert callback failed: %s", exc)
            continue

        if stop_reason == "pause_turn":
            # Server-side tool（Tool Search 等）実行中の一時停止。assistant turn
            # は上で history に append 済みなので、user ターンを一切足さずに
            # そのまま再送すると server 側が trailing server_tool_use を検出して
            # resume する（公式仕様: "Continue." 等を足してはいけない）。
            pause_turn_continues += 1
            if pause_turn_continues <= MAX_PAUSE_TURN_CONTINUES:
                log.info(
                    "[ch=%d] turn=%d stop_reason=pause_turn → resume (%d/%d)",
                    channel_id, turn + 1, pause_turn_continues,
                    MAX_PAUSE_TURN_CONTINUES,
                )
                await _safe_status(status_callback, "🔎 サーバー側処理を継続中")
                continue
            log.warning(
                "[ch=%d] turn=%d pause_turn limit reached (%d) → give up",
                channel_id, turn + 1, MAX_PAUSE_TURN_CONTINUES,
            )
            last_text = "\n\n".join(text_across_turns)
            _log_total_usage(channel_id, usage_totals)
            return AgentLoopResult(
                text=last_text or "(no response)",
                synthetic_session_id=None,
                active_mcp_keys=active_mcp_keys,
                fallback_model=current_model if fallback_used else None,
                prior_len=prior_len,
                usage_total=dict(usage_totals),
            )

        if stop_reason in ("end_turn", "stop_sequence"):
            # (B) The previous turn's tool(s) errored and the model gave up with
            # an empty/near-empty reply — nudge once to recover or at least
            # explain. Gated by the thin-reply length so a real apology/answer
            # is never nudged.
            if (
                last_turn_had_tool_error
                and error_recovery_nudges < MAX_TOOL_ERROR_NUDGES
                and len("\n\n".join(text_across_turns).strip())
                < TOOL_ERROR_MIN_REPLY_CHARS
            ):
                error_recovery_nudges += 1
                last_turn_had_tool_error = False
                history.append({
                    "role": "user",
                    "content": [{"type": "text", "text": _TOOL_ERROR_NUDGE}],
                })
                log.warning(
                    "[ch=%d] turn=%d tool error + thin reply → recovery nudge "
                    "(%d/%d)",
                    channel_id, turn + 1, error_recovery_nudges,
                    MAX_TOOL_ERROR_NUDGES,
                )
                await _safe_status(status_callback, "🔧 エラーから復帰を試行中")
                continue
            last_text = "\n\n".join(text_across_turns)
            if fallback_used and not fallback_note_sent_live and last_text:
                last_text = (
                    _fallback_alert(current_model) + "\n\n"
                    + last_text
                )
            _log_total_usage(channel_id, usage_totals)
            return AgentLoopResult(
                text=last_text,
                synthetic_session_id=uuid.uuid4().hex,
                active_mcp_keys=active_mcp_keys,
                fallback_model=current_model if fallback_used else None,
                prior_len=prior_len,
                usage_total=dict(usage_totals),
            )
        if stop_reason != "tool_use":
            # max_tokens / refusal / unexpected — return whatever text we
            # accumulated up to this point. Falls back to the current
            # turn's text alone if for some reason we never appended.
            last_text = "\n\n".join(text_across_turns)
            if fallback_used and not fallback_note_sent_live and last_text:
                last_text = (
                    _fallback_alert(current_model) + "\n\n"
                    + last_text
                )
            _log_total_usage(channel_id, usage_totals)
            return AgentLoopResult(
                text=last_text or "(no response)",
                synthetic_session_id=None,
                active_mcp_keys=active_mcp_keys,
                fallback_model=current_model if fallback_used else None,
                prior_len=prior_len,
                usage_total=dict(usage_totals),
            )

        # Execute every tool_use block in this turn via the shared
        # module-level _run_one_tool (also used by the text-form rescue path).
        tool_blocks = [
            b for b in (final_msg.content or [])
            if getattr(b, "type", None) == "tool_use"
        ]
        _tool_kw = dict(
            can_use_tool=can_use_tool,
            execute_tool=execute_tool,
            tool_schemas=tool_schemas,
            status_callback=status_callback,
            tool_invocation_callback=tool_invocation_callback,
            turn=turn,
            evidence=evidence,
        )
        # Read-only batches run concurrently (CLI isConcurrencySafe, audit
        # #12); tool_result order still follows tool_use order because
        # asyncio.gather preserves input order.
        if len(tool_blocks) > 1 and all(
            getattr(b, "name", "") in CONCURRENCY_SAFE_TOOLS
            for b in tool_blocks
        ):
            tool_results = list(await asyncio.gather(
                *(_run_one_tool(b, **_tool_kw) for b in tool_blocks)
            ))
        else:
            tool_results = [
                await _run_one_tool(b, **_tool_kw) for b in tool_blocks
            ]

        if not tool_results:
            # Tool Search 検索ターン: client 側 tool_use はゼロだが、result 未対の
            # server_tool_use が残っている場合は server 側の検索がこれから走る。
            # 「続けてください。」を挟むと trailing server_tool_use の resume
            # プロトコルが壊れるので、user ターンを足さずにそのまま再送する。
            if _has_unpaired_server_tool_use(final_msg.content):
                pause_turn_continues += 1
                if pause_turn_continues <= MAX_PAUSE_TURN_CONTINUES:
                    log.info(
                        "[ch=%d] turn=%d unpaired server_tool_use → resume "
                        "without nudge (%d/%d)",
                        channel_id, turn + 1, pause_turn_continues,
                        MAX_PAUSE_TURN_CONTINUES,
                    )
                    await _safe_status(status_callback, "🔎 ツール検索中")
                    continue
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
            # (C) Pause-turn budget exhausted. Before surrendering to
            # "(no response)", make one last-ditch attempt to extract whatever
            # the model has worked out so far.
            if final_empty_nudges < MAX_FINAL_EMPTY_NUDGES:
                final_empty_nudges += 1
                # NB: consecutive_empty is deliberately NOT reset — if the model
                # answers the nudge with text it returns normally next turn; if
                # it only thinks again, consecutive_empty keeps climbing and the
                # spent nudge budget routes straight to "(no response)".
                history.append({
                    "role": "user",
                    "content": [{"type": "text", "text": _FINAL_EMPTY_NUDGE}],
                })
                log.warning(
                    "[ch=%d] turn=%d pause limit → final summary nudge (%d/%d)",
                    channel_id, turn + 1, final_empty_nudges,
                    MAX_FINAL_EMPTY_NUDGES,
                )
                await _safe_status(status_callback, "📝 まとめを依頼中")
                continue
            log.warning(
                "pause turn limit reached ch=%d turn=%d consecutive=%d → "
                "returning accumulated text",
                channel_id, turn + 1, consecutive_empty,
            )
            last_text = "\n\n".join(text_across_turns)
            if fallback_used and not fallback_note_sent_live and last_text:
                last_text = (
                    _fallback_alert(current_model) + "\n\n"
                    + last_text
                )
            _log_total_usage(channel_id, usage_totals)
            return AgentLoopResult(
                text=last_text or "(no response)",
                synthetic_session_id=None,
                active_mcp_keys=active_mcp_keys,
                fallback_model=current_model if fallback_used else None,
                prior_len=prior_len,
                usage_total=dict(usage_totals),
            )
        consecutive_empty = 0

        # ADR-0036: ループ検知。このターンで実行した各ツールを記録し、同一
        # (tool, 引数) の反復（進捗ゼロの堂々巡り）を検出する。break は work を
        # 保存してから抜けるため、tool_results を append した後に離脱する。
        _loop_break = False
        _loop_warn = False
        for _tb in tool_blocks:
            _sig = detector.record(
                getattr(_tb, "name", "") or "",
                getattr(_tb, "input", None),
            )
            if _sig.action == "break":
                _loop_break = True
                log.warning(
                    "[ch=%d] turn=%d tool loop detected → checkpoint: %s",
                    channel_id, turn + 1, _sig.reason,
                )
            elif _sig.action == "warn":
                _loop_warn = True
                log.info(
                    "[ch=%d] turn=%d tool loop warn: %s",
                    channel_id, turn + 1, _sig.reason,
                )

        # tool_results ユーザーターンに nudge を混ぜる（別の user ターンを足すと
        # 連続 user ターンで 400 になるため、同一ターンの text ブロックとして注入）。
        _content = list(tool_results)
        if _loop_warn and not _loop_break:
            _content.append({"type": "text", "text": _TOOL_LOOP_WARN_NUDGE})
        if wrap_up_requested and not wrap_up_injected:
            _content.append({"type": "text", "text": _BUDGET_WRAP_UP_NUDGE})
            wrap_up_injected = True
        history.append({"role": "user", "content": _content})
        # (B) Remember whether this batch errored so the end_turn branch can
        # nudge once if the model then gives up with a thin reply.
        last_turn_had_tool_error = any(
            r.get("is_error") for r in tool_results
        )

        if _loop_break:
            return await _emit_checkpoint("tool_loop")

        # Sticky 昇格: このターンで実際に呼ばれた MCP サーバーを active_mcp_keys
        # に合流させる。entry.py が SessionInfo.mcp_server_keys へ永続化する
        # ので、Tool Search で発見された deferred サーバーは次ターンから
        # hot set（非 defer 広告）に昇格する。compact collapse で履歴中の
        # tool_reference が消えても発見済みツールを忘れない。
        _used_servers = set()
        for _b in tool_blocks:
            _bname = getattr(_b, "name", "") or ""
            if _bname.startswith("mcp__") and _bname.count("__") >= 2:
                _used_servers.add(_bname.split("__", 2)[1])
        _new_servers = _used_servers - set(active_mcp_keys)
        if _new_servers:
            active_mcp_keys = sorted(set(active_mcp_keys) | _new_servers)
            log.info(
                "[ch=%d] turn=%d MCP sticky promote: +%s → %s",
                channel_id, turn + 1, sorted(_new_servers), active_mcp_keys,
            )

        # In-loop proactive compaction (audit #3, ADR-0012): the CLI folds the
        # conversation BEFORE it overflows (proactive floor with a 20% buffer);
        # the bot used to compact only at the session boundary, leaving a long
        # in-loop turn to ride straight into a Fable refusal. When the last
        # turn's real context footprint crossed the floor, replace the prior
        # window with the freshly compacted one and continue the same loop.
        # Model-window-based trigger (CLI et6(K) port), unified with the
        # boundary trigger so the in-loop fold doesn't pre-empt it at a lower
        # flat constant. HISTORY_COMPACT_PCT_OVERRIDE dials both.
        from services.claude_api.compaction import compact_threshold_tokens
        if (
            PROACTIVE_COMPACT_ENABLED
            and proactive_compact is not None
            and not proactive_compacted
            and _context_tokens({
                f: _usage_number(getattr(final_msg, "usage", None), f)
                for f in _USAGE_FIELDS
            }) > compact_threshold_tokens()
        ):
            try:
                new_prior = await proactive_compact()
            except Exception as exc:  # noqa: BLE001 — never kill the loop
                log.warning(
                    "[ch=%d] in-loop proactive compaction failed: %s",
                    channel_id, exc,
                )
                new_prior = None
            proactive_compacted = True
            if new_prior is not None:
                log.info(
                    "[ch=%d] in-loop proactive compaction: prior %d → %d entries",
                    channel_id, prior_len, len(new_prior),
                )
                history[:prior_len] = new_prior
                prior_len = len(new_prior)

    # Reached the emergency turn ceiling (ADR-0036). This is now a rare backstop
    # — RequestBudget / ToolLoopDetector normally stop the loop first. Graceful
    # checkpoint instead of the old synthetic_session_id=None sentinel that
    # discarded the whole transcript: preserve work + let "続けて" resume.
    log.warning(
        "agent loop hit emergency turn ceiling max_turns=%d for ch=%d → checkpoint",
        max_turns, channel_id,
    )
    return await _emit_checkpoint("turn_ceiling")


def kill_orphan_claude_cli() -> int:
    """API-side cleanup: SIGTERM/SIGKILL MCP child processes.

    Replaces services/claude_sdk.py:kill_orphan_claude_cli for the direct-API
    path. The name is preserved for back-compat (main_async.py shutdown still
    imports this symbol).
    """
    return kill_orphan_mcp_processes()
