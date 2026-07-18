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
import time
import uuid

import anthropic
import httpx

from config import (
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
    STREAM_BYTE_IDLE_TIMEOUT_S,
    STREAM_NONSTREAM_FALLBACK_ENABLED,
    STREAM_NONSTREAM_TIMEOUT_S,
    STREAM_STALL_MAX_RETRIES,
    STREAM_STALL_RETRY_S,
    TEXT_INVOKE_RESCUE_ENABLED,
    TOOL_CALL_TIMEOUT_S,
    TOOL_ERROR_MIN_REPLY_CHARS,
)
from services.claude_api.loop_guard import (  # ADR-0036
    RequestBudget,
    ToolLoopDetector,
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
from services.claude_api.message_codec import (
    _EMPTY_CONTENT_PLACEHOLDER,
    _SERVER_TOOL_RESULT_TYPES,
    _TOOL_SEARCH_BLOCK_TYPES,
    _block_to_dict,
    _has_unpaired_server_tool_use,
    _join_final_text,
    _normalize_history_for_api,
    _response_blocks_to_dict,
    _sanitize_content_blocks,
    _serialize_search_result_content,
    strip_tool_search_blocks,
)
from services.claude_api.loop_contracts import (
    AUTO_RESUME_DIRECTIVE,
    AgentLoopResult,
    _AUTO_RESUME_NOTE,
    _CHECKPOINT_NOTE,
    _build_checkpoint_result,
    _fallback_alert,
    _model_display,
)
from services.claude_api.loop_observability import (
    _HALLUC_MAX_DISTANCE,
    _HALLUC_PATH_RE,
    _HALLUC_VERBS,
    _HALLUC_VERB_RE,
    _HALLUC_WRITE_TOOLS,
    _USAGE_FIELDS,
    _context_tokens,
    _detect_write_hallucination,
    _last_context_tokens,
    _log_total_usage,
    _log_turn_usage,
    _observe_response_headers,
    _usage_number,
    get_last_context_tokens,
)
from services.claude_api.ratelimit import (
    maybe_wait_before_request,
    should_block,
)
from services.claude_api.request_shape import (
    CLAUDE_MAX_TOKENS,
    CLAUDE_MAX_TOKENS_OPUS,
    CLI_DEFAULT_SYSTEM_PROMPT,
    CLI_IDENTITY_TEXT,
    CONTEXT_MANAGEMENT_CONFIG,
    OUTPUT_CONFIG,
    REFUSAL_FALLBACK_HEADER,
    THINKING_CONFIG,
    _build_cached_system,
    _build_stream_kwargs_impl,
    _inject_cache_breakpoints,
    _mark_cache_breakpoint,
    get_attribution_header_value,
    get_metadata,
)
from services.claude_api.stall import StallWatchdog
from services.claude_api.status import _safe_status, _tool_input_hint
from services.claude_api.tool_display import (
    _TOOL_DETAIL_FIELD_CAP,
    _TOOL_DETAIL_INLINE_CAP,
    _cap_inline,
    _format_tool_invocation_for_display,
)
from services.claude_api.tool_execution import (
    _cap_mcp_result_content,
    _RescuedToolUse,
    _run_one_tool_inner as _run_one_tool_inner_impl,
    _truncate_head_tail,
    _validate_tool_input,
)

log = logging.getLogger(__name__)


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
# Read-only tools whose repeated identical calls are less alarming (status
# checks, re-reads). Loop detector gives these extra slack. Distinct from
# CONCURRENCY_SAFE_TOOLS (which includes Agent — repeated identical subagent
# spawns ARE suspicious, so Agent is intentionally excluded here).
_LOOP_READONLY_TOOLS = frozenset({"Read", "Glob", "Grep"})


async def _idle_guarded(stream, idle_s: float):
    """Yield SSE events from ``stream``, raising ``TimeoutError`` if no event
    arrives within ``idle_s`` seconds.

    ADR-0038: the anthropic beta stream can wedge after the server accepts the
    POST (HTTP 200) yet delivers zero events — an upstream stall the
    StallWatchdog only ever answered by cancelling the whole task, throwing
    away even an already-finished draft (2026-07-05 incident). Surfacing the
    stall as a catchable ``TimeoutError`` at the consumption site lets the tool
    loop recover in place (re-issue a clean pre-first-byte stall, else
    checkpoint). Cancelling the pending ``__anext__`` is safe: the caller
    abandons this stream — its ``async with`` closes the HTTP connection — and
    either sends a fresh request or returns a graceful checkpoint.
    """
    ait = stream.__aiter__()
    while True:
        try:
            event = await asyncio.wait_for(ait.__anext__(), idle_s)
        except StopAsyncIteration:
            return
        yield event


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
        # Fixed operator-only directory for local refusal diagnostics.
        d = "/tmp/fable5_capture"  # nosec B108
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
    """Compatibility wrapper preserving ``tool_loop`` config patch points."""
    return _build_stream_kwargs_impl(
        current_model=current_model,
        cached_system=cached_system,
        messages=messages,
        extra_betas=extra_betas,
        output_config_override=output_config_override,
        refusal_fallback_model=refusal_fallback_model,
        tools_for_api=tools_for_api,
        extra_headers=extra_headers,
        fallback_used=fallback_used,
        server_side_fallback_enabled=SERVER_SIDE_FALLBACK_ENABLED,
        refusal_fallback_header=REFUSAL_FALLBACK_HEADER,
    )


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
    """Compatibility wrapper preserving the tool-loop timeout patch point."""
    return await _run_one_tool_inner_impl(
        block,
        can_use_tool=can_use_tool,
        execute_tool=execute_tool,
        tool_schemas=tool_schemas,
        status_callback=status_callback,
        tool_invocation_callback=tool_invocation_callback,
        turn=turn,
        _tool_call_timeout_s=TOOL_CALL_TIMEOUT_S,
    )


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
    thinking_stream_callback=None,    # async (partial_thinking_text:str)->None
    thinking_finalize_callback=None,  # async ()->None  (thinking block closed)
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
    should_auto_resume=None,  # ADR-0040: (paused_reason:str)->bool — entry.py の
    # 自動継続ポリシー。checkpoint 時に True を返すと「続けて」導線の代わりに
    # 自動継続ノートを出す（実際の再実行は entry.py 側が行う）。
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
        ユーザーに導線を見せる（fallback_alert と同じ流儀）。ADR-0040: entry.py
        が自動継続する場合は「続けて」の代わりに自動継続ノートを出す。"""
        _log_total_usage(channel_id, usage_totals)
        will_resume = False
        if should_auto_resume is not None:
            try:
                will_resume = bool(should_auto_resume(paused_reason))
            except Exception as exc:  # noqa: BLE001 — ポリシー故障で checkpoint を殺さない
                log.warning(
                    "[ch=%d] should_auto_resume(%s) failed: %s",
                    channel_id, paused_reason, exc,
                )
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
            auto_resume_planned=will_resume,
        )
        if incremental_text_callback is not None:
            _accumulated = "\n\n".join(t for t in text_across_turns if t)
            _live_note = _AUTO_RESUME_NOTE if will_resume else _CHECKPOINT_NOTE
            _note = _live_note if _accumulated else result.text
            try:
                await incremental_text_callback(_note)
            except Exception as exc:  # noqa: BLE001 — 表示失敗で loop を殺さない
                log.warning("[ch=%d] checkpoint note stream failed: %s", channel_id, exc)
        return result

    # ADR-0038: total clean-TTFB stream-stall retries across all turns. A
    # clean stall re-issues the same request via `continue` (see the stream
    # TimeoutError handler below); capped so a persistently dead upstream falls
    # through to a graceful checkpoint instead of looping forever.
    stall_retries = 0
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
        # Live thinking stream for the CURRENT thinking block (計画/検証 preview).
        # thinking_delta events carry the extended-thinking text; accumulate so
        # thinking_stream_callback always gets the block-so-far. Reset per block.
        live_thinking = ""
        in_thinking = False
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
            # Byte-layer stall detection (CLI-parity 2026-07-11): httpx's read
            # timeout is the max gap between BYTES, and SSE pings are bytes —
            # so a slow-but-alive upstream (long prefill keeps pinging) never
            # trips it, only a genuinely dead connection does. This mirrors
            # the CLI 2.1.170 byte watchdog (180s firstParty, oh_=180000),
            # its PRIMARY detector. Per-request override only: the shared
            # client keeps its 600s scalar because non-streaming calls
            # receive zero bytes until generation completes and a 180s read
            # cap would kill them. NOT added to stream_kwargs — those are
            # reused by the fallback create(**stream_kwargs, timeout=1800)
            # and a second timeout kwarg would TypeError.
            _stream_timeout = httpx.Timeout(
                connect=15.0,
                read=float(STREAM_BYTE_IDLE_TIMEOUT_S),
                write=60.0,
                pool=60.0,
            )
            async with api_client.beta.messages.stream(
                **stream_kwargs, timeout=_stream_timeout,
            ) as stream:
                # ADR-0038: idle-guard each event so a wedged upstream raises a
                # catchable TimeoutError here instead of tripping the outer
                # StallWatchdog's task-cancel. The heartbeat still feeds the
                # watchdog backstop for stalls this guard can't see. Since the
                # byte layer above owns "dead connection" at 180s, this event
                # guard (300s) only fires when bytes flow but no event arrives.
                async for event in _idle_guarded(stream, STREAM_STALL_RETRY_S):
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
                            in_thinking = True
                            live_thinking = ""
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
                        if in_thinking:
                            in_thinking = False
                            if thinking_finalize_callback is not None:
                                try:
                                    await thinking_finalize_callback()
                                except Exception as exc:
                                    log.debug(
                                        "thinking_finalize_callback failed: %s", exc,
                                    )
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
                    elif etype == "content_block_delta":
                        # NB: no text_stream_callback gate on the branch itself —
                        # thinking capture must work in deliberate tier too, where
                        # text_stream_callback is None but thinking_stream_callback
                        # is wired for the 計画/検証 preview.
                        delta = getattr(event, "delta", None)
                        _dtype = getattr(delta, "type", None)
                        if _dtype == "text_delta" and text_stream_callback is not None:
                            piece = getattr(delta, "text", "") or ""
                            if piece:
                                live_turn_text += piece
                                # bot.py owns throttling (≤1 edit / 1.5s) and
                                # delivery; never let a UI hiccup abort the loop.
                                try:
                                    await text_stream_callback(live_turn_text)
                                except Exception as exc:
                                    log.debug("text_stream_callback failed: %s", exc)
                        elif (
                            _dtype == "thinking_delta"
                            and thinking_stream_callback is not None
                        ):
                            piece = getattr(delta, "thinking", "") or ""
                            if piece:
                                live_thinking += piece
                                # Same throttle discipline as prose; a UI hiccup
                                # must never abort the tool loop.
                                try:
                                    await thinking_stream_callback(live_thinking)
                                except Exception as exc:
                                    log.debug(
                                        "thinking_stream_callback failed: %s", exc,
                                    )
                final_msg = await asyncio.wait_for(
                    stream.get_final_message(), STREAM_STALL_RETRY_S,
                )
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
                        # Clear the fallback target now that we've pinned it as
                        # the answering model. Otherwise the next tool-loop turn
                        # rebuilds stream_kwargs with current_model=<fallback> AND
                        # fallbacks=[<fallback>] still attached — and the fallback
                        # model (Opus 4.8) rejects the `fallbacks` body field with
                        # 400 invalid_request, hanging the turn at "整形中"
                        # (2026-07-14: Fable 5 server-side fallback → tool_use).
                        refusal_fallback_model = None
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
        except (
            TimeoutError, httpx.ReadTimeout, anthropic.APITimeoutError,
        ) as _stall_exc:
            # ADR-0038 + byte layer (2026-07-11): the upstream stream is
            # wedged. Recover here instead of letting the StallWatchdog cancel
            # the whole task. Three detection layers land here, distinguished
            # for the passive prod measurement (byte-vs-event trip ratio tells
            # us whether pings actually flow during a long prefill):
            #   - httpx.ReadTimeout  → BYTE layer: ≥STREAM_BYTE_IDLE_TIMEOUT_S
            #     with zero bytes (pings included) — a genuinely dead
            #     connection; immediate re-POST is correct.
            #   - TimeoutError       → EVENT layer: bytes flowed (httpx quiet)
            #     but no SSE event for STREAM_STALL_RETRY_S.
            #   - APITimeoutError    → REQUEST leg: the SDK wrapped a timeout
            #     before headers arrived (connect/TTFB) — pre-first-event by
            #     definition, same recovery family (CLI treats its stale-
            #     connection case identically: retry streaming, then fall
            #     back). Caught here BEFORE the anthropic.AnthropicError
            #     handler below by except-clause ordering.
            # A clean pre-first-byte stall (no prose streamed this turn,
            # so nothing was appended to `history` — the user turn was appended
            # once before the loop) is re-issued as the SAME request via
            # `continue`; it cannot duplicate visible output. Once prose has
            # streamed, or the retry budget is spent, preserve the work with a
            # graceful checkpoint ("続けて" resumes) rather than dying.
            if isinstance(_stall_exc, httpx.ReadTimeout):
                _stall_layer = "byte"
            elif isinstance(_stall_exc, anthropic.APITimeoutError):
                _stall_layer = "request"
            else:
                _stall_layer = "event"
            # Deliberately NO watchdog heartbeat here (ADR-0045): the CLI's
            # async-agent backstop is a hard 600s no-progress ceiling that is
            # NOT fed during recovery legs — a recovery chain that can't
            # finish inside the ceiling is killed as "stream watchdog did not
            # recover", by design. Ordering: byte 180 < event 300 = fallback
            # 300 < backstop 600.
            _produced = bool(live_turn_text.strip())
            if not _produced and stall_retries < STREAM_STALL_MAX_RETRIES:
                stall_retries += 1
                _backoff = min(2 ** stall_retries, 8)
                log.warning(
                    "[ch=%d] turn=%d stream stall (layer=%s, %r) → retry %d/%d "
                    "after %ds",
                    channel_id, turn + 1, _stall_layer, _stall_exc,
                    stall_retries, STREAM_STALL_MAX_RETRIES, _backoff,
                )
                await _safe_status(
                    status_callback,
                    f"⏳ 応答待ち・再試行中 ({stall_retries}/{STREAM_STALL_MAX_RETRIES})",
                )
                await asyncio.sleep(_backoff)
                continue
            # CLI-parity (2026-07-09): streaming retry budget exhausted on a
            # clean pre-first-byte stall. The Claude Code CLI falls back to a
            # single NON-streaming request here ("falling back to non-streaming
            # mode") rather than dying — a blocking messages.create sidesteps the
            # per-chunk SSE idle watchdog, keeps the prompt+cache intact, and
            # needs no second API call. On success we set final_msg and fall
            # through to the shared post-processing below (which appends the
            # assistant turn and delivers the prose via incremental_text_callback,
            # exactly like the streaming path — no double-send). On failure, or
            # once prose has already streamed, we preserve work with a checkpoint.
            if not _produced and STREAM_NONSTREAM_FALLBACK_ENABLED:
                log.warning(
                    "[ch=%d] turn=%d stream stall exhausted (retries=%d) → "
                    "non-streaming fallback",
                    channel_id, turn + 1, stall_retries,
                )
                await _safe_status(status_callback, "🐢 非ストリーミングで取得中…")
                _ns_final = None
                try:
                    _ns_final = await asyncio.wait_for(
                        # SDK-level timeout stays deliberately generous (1800s)
                        # so the SDK's "large max_tokens without streaming"
                        # guard never trips; the REAL deadline is the outer
                        # STREAM_NONSTREAM_TIMEOUT_S (300s — CLI parity: fL9
                        # passes _ef() = API_TIMEOUT_MS || 300000ms local as
                        # its per-request timeout, 2.1.170). This leg runs
                        # under the 600s backstop unfed, exactly like the
                        # CLI's fallback under its async-agent watchdog.
                        api_client.beta.messages.create(
                            **stream_kwargs, timeout=1800,
                        ),
                        STREAM_NONSTREAM_TIMEOUT_S,
                    )
                except Exception as _ns_exc:  # noqa: BLE001 - best-effort fallback
                    # %r, not %s: str(TimeoutError()) == "" — the bare %s
                    # produced "non-streaming fallback failed:  → checkpoint"
                    # with the cause invisible (observed 2026-07-11).
                    log.warning(
                        "[ch=%d] non-streaming fallback failed: %r → checkpoint",
                        channel_id, _ns_exc,
                    )
                if _ns_final is not None:
                    # The recovery WAIT ran unfed (hard ceiling), but a
                    # SUCCEEDED fallback is genuine progress — the CLI feeds
                    # its backstop on the resulting yielded message (ADR-0045).
                    # Heartbeat here so the unfed retry→fallback idle can't
                    # cancel a fully-received, fully-paid response during the
                    # post-processing / Discord delivery that follows.
                    stall_watchdog.heartbeat()
                    final_msg = _ns_final
                    if ttft_ms is None:
                        ttft_ms = int((time.monotonic() - request_start) * 1000)
                    _log_turn_usage(
                        channel_id, turn, final_msg, usage_totals,
                        model=current_model, request_id="",
                        duration_ms=int((time.monotonic() - request_start) * 1000),
                        ttft_ms=ttft_ms, message_count=len(messages),
                    )
                    _this_turn_output = _usage_number(
                        getattr(final_msg, "usage", None), "output_tokens",
                    )
                    _tok_base += _this_turn_output
                    budget.add_output_tokens(_this_turn_output)
                    if progress is not None:
                        progress["tokens"] = _tok_base
                    log.info(
                        "[ch=%d] turn=%d non-streaming fallback succeeded "
                        "(stop_reason=%s, output_tokens=%d)",
                        channel_id, turn + 1,
                        getattr(final_msg, "stop_reason", None), _this_turn_output,
                    )
                    # fall through to shared post-processing below.
                else:
                    return await _emit_checkpoint("stream_stall")
            else:
                if _produced:
                    # Fold the partial prose already shown to the user into the
                    # transcript so the checkpoint isn't missing this turn's head.
                    text_across_turns.append(live_turn_text)
                log.warning(
                    "[ch=%d] turn=%d stream stall (layer=%s, %r, produced=%s, "
                    "retries=%d) → graceful checkpoint",
                    channel_id, turn + 1, _stall_layer, _stall_exc, _produced,
                    stall_retries,
                )
                return await _emit_checkpoint("stream_stall")
        except anthropic.AnthropicError as exc:
            category = classify_anthropic_error(exc)
            log.error("agent loop API error turn=%d category=%s: %s",
                      turn, category, exc)
            # Also try to observe ratelimit headers from the failing response
            # so the next pre-check can refuse outright.
            _observe_response_headers(exc)
            # Caller (entry.py) owns retry/backoff; surface the exception.
            raise

        # Preserve the fallback boundary while removing only the pre-boundary
        # blocks the continuation schema forbids.
        assistant_blocks = _response_blocks_to_dict(final_msg.content)
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
                # Rescue executes real tools too — same backstop deferral as
                # the main tool phase (ADR-0045).
                stall_watchdog.tool_begin()
                try:
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
                finally:
                    stall_watchdog.tool_end()
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
            # Same reason as the server-side path above: once current_model IS
            # the fallback model, stop declaring `fallbacks` on subsequent turns
            # or the fallback model 400s on the unsupported body field.
            refusal_fallback_model = None
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
        # tool_begin/tool_end (ADR-0045): the whole-task backstop defers
        # while tools are in flight (CLI async-agent parity) — tool legs own
        # their deadline via TOOL_CALL_TIMEOUT_S, and a sequential multi-tool
        # phase can legitimately exceed the backstop's 600s.
        stall_watchdog.tool_begin()
        try:
            # free-code parity (toolOrchestration.partitionToolCalls): 連続する
            # concurrency-safe ブロックをバッチ並列（上限10）、非 safe は単体直列。
            # 従来の「全ブロックが safe のときだけ全並列」は混在ターンで全直列だった。
            tool_results = []
            _safe_batch: list = []

            async def _flush_batch() -> None:
                if not _safe_batch:
                    return
                if len(_safe_batch) > 1:
                    _sem = asyncio.Semaphore(10)

                    async def _capped(blk):
                        async with _sem:
                            return await _run_one_tool(blk, **_tool_kw)

                    tool_results.extend(await asyncio.gather(
                        *(_capped(b) for b in list(_safe_batch))
                    ))
                else:
                    tool_results.append(
                        await _run_one_tool(_safe_batch[0], **_tool_kw)
                    )
                _safe_batch.clear()

            for _blk in tool_blocks:
                if getattr(_blk, "name", "") in CONCURRENCY_SAFE_TOOLS:
                    _safe_batch.append(_blk)
                else:
                    await _flush_batch()
                    tool_results.append(await _run_one_tool(_blk, **_tool_kw))
            await _flush_batch()
        finally:
            stall_watchdog.tool_end()

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
        # Phase 3 (ADR-0037): fire on the HARD ceiling, NOT the boundary
        # trigger. Boundary compaction handles the normal case at the turn
        # boundary; this in-loop fold is the last-resort mid-turn breaker
        # for a single huge turn (giant tool_result) -- kept per external
        # review to avoid an overflow/crash, but gated higher so it rarely
        # pre-empts the boundary fold.
        from services.claude_api.compaction import proactive_hard_threshold_tokens
        if (
            PROACTIVE_COMPACT_ENABLED
            and proactive_compact is not None
            and not proactive_compacted
            and _context_tokens({
                f: _usage_number(getattr(final_msg, "usage", None), f)
                for f in _USAGE_FIELDS
            }) > proactive_hard_threshold_tokens()
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
