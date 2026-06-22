"""Session-boundary history compaction (ADR-0010 phase 3).

Mirrors the official claude-cli auto-compact, reverse-engineered from the
claude.exe embedded JS on 2026-06-12:

- the compact request is a plain messages call whose system prompt is
  "You are a helpful AI assistant tasked with summarizing conversations."
  and whose user turn is the conversation so far + a 9-section summary
  instruction template;
- the response's <analysis> block is dropped and the <summary> block body is
  kept (binary fn X45);
- the summary is wrapped in the "This session is being continued from a
  previous conversation that ran out of context." continuation preamble
  (binary fn X0$), with a pointer to the full transcript and a "Recent
  messages are preserved verbatim." note for the kept tail;
- the verbatim tail is chosen by walking sizes backwards until a budget is
  reached, falling back to the newest half when that would keep everything
  (binary fn o27);
- repeated failures trip a circuit breaker so a broken summarizer cannot
  add latency + cost to every turn (consecutiveFailures guard in the binary).

Deliberate deviation: the official CLI summarizes with the main-loop model,
but Fable 5 refuses exactly the long contexts compaction exists to shrink,
so the summary call uses HISTORY_COMPACT_MODEL (Opus 4.8) instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from config import (
    HISTORY_COMPACT_EFFORT,
    HISTORY_COMPACT_ENABLED,
    HISTORY_COMPACT_KEEP_CHARS,
    HISTORY_COMPACT_MODEL,
    HISTORY_COMPACT_TRIGGER_CHARS,
)
from services.claude_api.client import (
    CLI_DEFAULT_SYSTEM_PROMPT,
    CLI_IDENTITY_TEXT,
    MODEL_BETAS,
    THINKING_CONFIG,
    get_attribution_header_value,
    get_metadata,
)
from services.claude_api.status import _safe_status
from services.session import (
    append_jsonl,
    history_file_path,
    load_compaction_rows,
)

log = logging.getLogger(__name__)

# Cap on the flattened conversation fed to the summarizer; oldest text is cut
# first. ~600K chars ≈ 150-250K tokens, inside Opus's window.
MAX_SUMMARY_INPUT_CHARS = 600_000
# The official CLI's summary is long-form (9 mandatory sections).
MAX_SUMMARY_OUTPUT_TOKENS = 16384

# Circuit breaker (per process): after this many consecutive failures on a
# channel, skip compaction for the cooldown window instead of re-paying a
# failing long-context summary call on every turn.
MAX_CONSECUTIVE_COMPACT_FAILURES = 3
COMPACT_FAILURE_COOLDOWN_S = 30 * 60
_compact_failures: dict[int, int] = {}
_compact_blocked_until: dict[int, float] = {}

# Rapid-refill breaker (CLI consecutiveRapidRefills, audit #19): if proactive
# compaction fires on this many CONSECUTIVE requests for a channel, the window
# is refilling to the ceiling right after every compact (e.g. one giant tool
# result dominates the context) — compacting again is thrashing, not progress.
# Open the same cooldown breaker instead.
MAX_CONSECUTIVE_RAPID_REFILLS = 3
_rapid_refills: dict[int, int] = {}

# Boundary auto-compaction trigger, modelled on the CLI's et6(K)=ZQ(K)-Pe1.
# Fire near the effective context window minus the model's output budget and a
# headroom buffer for the compaction summary turn itself (≈ CLI Pe1).
_COMPACT_HEADROOM_TOKENS = 45_000
_COMPACT_FLOOR_TOKENS = 20_000


def effective_context_window() -> int:
    """Model context window in tokens: 1M with the context-1m beta, else 200K."""
    import config
    return 1_000_000 if config.CONTEXT_1M_ENABLED else 200_000


def compact_threshold_tokens() -> int:
    """Tokens at which boundary auto-compaction fires (CLI et6(K) port).

    Default: ``effective_window − CLAUDE_MAX_TOKENS − headroom`` (near-full, like
    the CLI). ``HISTORY_COMPACT_PCT_OVERRIDE`` (percent of the window) forces a
    flat ratio instead — set 50 to keep the old fixed 500K-on-1M behaviour when
    per-turn cost matters more than retained context.
    """
    import config
    win = effective_context_window()
    pct = getattr(config, "HISTORY_COMPACT_PCT_OVERRIDE", 0) or 0
    if pct > 0:
        return max(_COMPACT_FLOOR_TOKENS, int(win * pct / 100.0))
    reserve = int(getattr(config, "CLAUDE_MAX_TOKENS", 64000)) + _COMPACT_HEADROOM_TOKENS
    return max(_COMPACT_FLOOR_TOKENS, min(win - reserve, win - 1000))


def record_proactive_compaction(channel_id: int) -> None:
    """Note that an in-loop proactive compaction fired for this request."""
    n = _rapid_refills.get(channel_id, 0) + 1
    _rapid_refills[channel_id] = n
    if n >= MAX_CONSECUTIVE_RAPID_REFILLS:
        _compact_blocked_until[channel_id] = (
            time.monotonic() + COMPACT_FAILURE_COOLDOWN_S
        )
        _rapid_refills.pop(channel_id, None)
        log.warning(
            "[ch=%d] rapid-refill breaker tripped after %d consecutive "
            "proactive compactions (cooldown %ds)",
            channel_id, n, COMPACT_FAILURE_COOLDOWN_S,
        )


def reset_rapid_refill(channel_id: int) -> None:
    """A request finished without needing proactive compaction — reset."""
    _rapid_refills.pop(channel_id, None)

COMPACT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant tasked with summarizing conversations."
)

# Verbatim official-CLI compact instruction template (claude.exe, 2026-06-12),
# with one bot-specific "Additional Instructions" block appended — the
# template itself invites those.
COMPACT_INSTRUCTIONS = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
   - Note any security-relevant instructions or constraints the user stated (e.g., sensitive files or data to avoid, operations that must not be performed, credential or secret handling rules). These MUST be preserved verbatim in the summary so they continue to apply after compaction.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent. Preserve any security-relevant instructions or constraints verbatim so they remain in effect after compaction.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.
   If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]
   - [...]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Summary of the changes made to this file, if any]
      - [Important Code Snippet]
   - [File Name 2]
      - [Important Code Snippet]
   - [...]

4. Errors and fixes:
    - [Detailed description of error 1]:
      - [How you fixed the error]
      - [User feedback on the error if any]
    - [...]

5. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

6. All user messages:
    - [Detailed non tool use user message]
    - [...]

7. Pending Tasks:
   - [Task 1]
   - [Task 2]
   - [...]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]
</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response.

Additional Instructions:
This is a Discord conversation transcript (mostly Japanese) between admin users and the assistant 佐藤かなえ — it is not always a coding session. When summarizing:
- preserve who said what: keep the [発言者: ...] speaker labels attached to the key statements and requests;
- preserve decisions, promises, and pending requests made in conversation;
- preserve the relational/emotional thread of the conversation where it matters for continuity;
- write the summary in Japanese where the source conversation is Japanese, keeping code, file paths, and identifiers verbatim."""

_ANALYSIS_RE = re.compile(r"<analysis>[\s\S]*?</analysis>")
_SUMMARY_RE = re.compile(r"<summary>([\s\S]*?)</summary>")

CONTINUATION_PREAMBLE = (
    "This session is being continued from a previous conversation that ran "
    "out of context. The summary below covers the earlier portion of the "
    "conversation."
)


def extract_summary(text: str) -> str:
    """Reduce the summarizer response to its usable body (binary fn X45).

    Drops the <analysis> scratchpad, unwraps <summary> into "Summary:\\n…",
    and squeezes blank-line runs — same transforms as the official CLI.
    String slicing (not re.sub with the body as replacement) so backslashes
    in code snippets survive.
    """
    out = _ANALYSIS_RE.sub("", text or "")
    m = _SUMMARY_RE.search(out)
    if m:
        body = (m.group(1) or "").strip()
        out = out[: m.start()] + "Summary:\n" + body + out[m.end():]
    out = re.sub(r"\n\n+", "\n", out)
    return out.strip()


def wrap_summary(
    summary_text: str,
    transcript_path: str | None,
    kept_verbatim: bool,
) -> str:
    """Continuation wrapper around an extracted summary (binary fn X0$).

    No "Continue the conversation…" leg: in this bot a fresh user turn always
    follows the summary, so the model never has to self-resume.
    """
    out = f"{CONTINUATION_PREAMBLE}\n{extract_summary(summary_text)}"
    if transcript_path:
        out += (
            "\nIf you need specific details from before compaction (like "
            "exact code snippets, error messages, or content you generated), "
            f"read the full transcript at: {transcript_path}"
        )
    if kept_verbatim:
        out += "\nRecent messages are preserved verbatim."
    return out


def _row_chars(row: dict) -> int:
    """Approximate replay weight of a JSONL row in characters."""
    total = 0
    content = row.get("content")
    if isinstance(content, str):
        total += len(content)
    api_messages = row.get("api_messages")
    if isinstance(api_messages, list) and api_messages:
        try:
            total += len(json.dumps(api_messages, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            pass
    return total


def estimate_replay_chars(messages: list[dict]) -> int:
    """Cheap size gate for an already-built replay message list (entry.py)."""
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            try:
                total += len(json.dumps(content, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                pass
    return total


def compute_keep_boundary(rows: list[dict], keep_chars: int) -> int:
    """Index of the first row of the verbatim tail (binary fn o27).

    Walks backwards accumulating row sizes until ``keep_chars`` is covered,
    then extends the tail back to the nearest top-level user row so the
    preserved leg starts on a clean user turn. When that would keep (nearly)
    everything, keeps the newest half instead — the official CLI's guard
    against a no-op compact.
    """
    n = len(rows)
    if n == 0:
        return 0
    acc = 0
    k = 0
    for i in range(n - 1, -1, -1):
        acc += _row_chars(rows[i])
        k += 1
        if acc >= keep_chars:
            break
    boundary = n - k
    while boundary > 0 and rows[boundary].get("role") != "user":
        boundary -= 1
    if boundary <= 1:
        boundary = n // 2
        while boundary > 0 and rows[boundary].get("role") != "user":
            boundary -= 1
    return boundary


def _flatten_rows_for_summary(rows: list[dict], max_chars: int) -> str:
    """Render rows as labelled plain text for the summarizer input.

    Assistant rows flatten to their final display text — tool_use internals
    add bulk without aiding the 9-section summary. Oldest text is cut first
    when over budget (the newest part matters most for sections 8/9).
    """
    parts: list[str] = []
    for row in rows:
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        author = row.get("author")
        if isinstance(author, str) and author:
            label = f"[発言者: {author}]"
        elif row.get("role") == "assistant":
            label = "[assistant (かなえ)]"
        else:
            label = "[user]"
        parts.append(f"{label}\n{content}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text


def _record_compact_failure(channel_id: int) -> None:
    fails = _compact_failures.get(channel_id, 0) + 1
    _compact_failures[channel_id] = fails
    if fails >= MAX_CONSECUTIVE_COMPACT_FAILURES:
        _compact_blocked_until[channel_id] = (
            time.monotonic() + COMPACT_FAILURE_COOLDOWN_S
        )
        log.warning(
            "[ch=%d] compaction circuit breaker tripped after %d failures "
            "(cooldown %ds)", channel_id, fails, COMPACT_FAILURE_COOLDOWN_S,
        )


async def compact_channel_history(
    channel_id: int,
    api_client,
    status_callback=None,
    *,
    force: bool = False,
) -> bool:
    """Summarize the older replay window into a compact-summary JSONL row.

    Returns True when a summary row was appended — the caller should reload
    the replay window. False means nothing happened (disabled, breaker open,
    below trigger, or the summary call failed); the caller continues with the
    raw history, and the refusal-fallback path downstream keeps the
    conversation alive either way.

    ``force=True`` skips the char-based trigger gate — used by the token-based
    paths (audit #3/#7), where the caller already observed real usage tokens
    above the proactive floor (chars under-/over-estimate tokens by 2-4x on
    JP/code transcripts, so the char gate must not veto a token decision).
    """
    if not HISTORY_COMPACT_ENABLED:
        log.info("[ch=%d] compaction skipped: disabled via env", channel_id)
        return False
    blocked_until = _compact_blocked_until.get(channel_id, 0.0)
    if blocked_until > time.monotonic():
        log.info(
            "[ch=%d] compaction skipped: circuit breaker open for %.0fs more "
            "(consecutive_failures=%d)",
            channel_id, blocked_until - time.monotonic(),
            _compact_failures.get(channel_id, 0),
        )
        return False

    rows = await asyncio.to_thread(load_compaction_rows, channel_id)
    total_chars = sum(_row_chars(r) for r in rows)
    if not force and total_chars <= HISTORY_COMPACT_TRIGGER_CHARS:
        log.debug(
            "[ch=%d] compaction below trigger: chars=%d <= %d",
            channel_id, total_chars, HISTORY_COMPACT_TRIGGER_CHARS,
        )
        return False
    log.info(
        "[ch=%d] compaction trigger: chars=%d trigger=%d force=%s rows=%d",
        channel_id, total_chars, HISTORY_COMPACT_TRIGGER_CHARS, force,
        len(rows),
    )

    boundary = compute_keep_boundary(rows, HISTORY_COMPACT_KEEP_CHARS)
    old_rows, keep_rows = rows[:boundary], rows[boundary:]
    if not old_rows:
        log.info(
            "[ch=%d] compaction skipped: keep boundary=%d leaves nothing to "
            "summarize (keep_chars=%d)",
            channel_id, boundary, HISTORY_COMPACT_KEEP_CHARS,
        )
        return False
    old_chars = sum(_row_chars(r) for r in old_rows)
    keep_chars = sum(_row_chars(r) for r in keep_rows)
    log.info(
        "[ch=%d] compaction boundary: summarize_rows=%d (%d chars) "
        "keep_rows=%d (%d chars) keep_budget=%d",
        channel_id, len(old_rows), old_chars, len(keep_rows), keep_chars,
        HISTORY_COMPACT_KEEP_CHARS,
    )

    flat = _flatten_rows_for_summary(old_rows, MAX_SUMMARY_INPUT_CHARS)
    if not flat.strip():
        log.info(
            "[ch=%d] compaction skipped: flattened transcript is empty "
            "(old_rows had no string content)", channel_id,
        )
        return False
    log.info(
        "[ch=%d] compaction input: flat_chars=%d (cap=%d, truncated=%s)",
        channel_id, len(flat), MAX_SUMMARY_INPUT_CHARS,
        old_chars > MAX_SUMMARY_INPUT_CHARS,
    )

    await _safe_status(status_callback, "📚 過去ログを圧縮中")
    log.info(
        "[ch=%d] compaction start: rows=%d (summarize=%d keep=%d) chars=%d "
        "model=%s effort=%s max_tokens=%d",
        channel_id, len(rows), len(old_rows), len(keep_rows), total_chars,
        HISTORY_COMPACT_MODEL, HISTORY_COMPACT_EFFORT,
        MAX_SUMMARY_OUTPUT_TOKENS,
    )
    call_start = time.monotonic()
    try:
        # Recursive Claude call: Opus 4.8 at the top effort tier. The summary
        # is the lossy bottleneck for every future turn of this channel, so
        # we spend max reasoning here (it fires rarely, only past the trigger)
        # via the same effort beta + output_config the main loop uses, just
        # dialled to "max" instead of "medium". Thinking stays adaptive.
        #
        # The request MUST be subscriber-shaped like the main loop: metadata
        # (device/account user_id) + the CLI 4-block system prefix
        # (attribution, identity, CLI default prompt). Without these the call
        # lands in the anonymous-OAuth bucket and is answered with a bare 429
        # regardless of actual quota (observed 2026-06-13: every compaction
        # 429'd while same-minute main-loop calls returned 200 — compaction
        # had never succeeded in production).
        compact_messages = [{
            "role": "user",
            "content": (
                "Here is the conversation transcript to summarize:\n\n"
                f"{flat}\n\n{COMPACT_INSTRUCTIONS}"
            ),
        }]
        compact_system = [
            {"type": "text",
             "text": get_attribution_header_value(compact_messages)},
            {"type": "text", "text": CLI_IDENTITY_TEXT},
            {"type": "text", "text": CLI_DEFAULT_SYSTEM_PROMPT,
             "cache_control": {"type": "ephemeral", "ttl": "1h",
                               "scope": "global"}},
            {"type": "text", "text": COMPACT_SYSTEM_PROMPT},
        ]
        resp = await api_client.beta.messages.create(
            model=HISTORY_COMPACT_MODEL,
            max_tokens=MAX_SUMMARY_OUTPUT_TOKENS,
            system=compact_system,
            messages=compact_messages,
            betas=MODEL_BETAS,
            metadata=get_metadata(),
            thinking=THINKING_CONFIG,
            output_config={"effort": HISTORY_COMPACT_EFFORT},
        )
        text = "".join(
            getattr(block, "text", "")
            for block in (resp.content or [])
            if getattr(block, "type", "") == "text"
        )
        usage = getattr(resp, "usage", None)
        log.info(
            "[ch=%d] compaction summary response: duration_ms=%d "
            "request_id=%s input_tokens=%s output_tokens=%s "
            "stop_reason=%s text_chars=%d",
            channel_id,
            int((time.monotonic() - call_start) * 1000),
            getattr(resp, "_request_id", None) or "-",
            getattr(usage, "input_tokens", None) if usage else "-",
            getattr(usage, "output_tokens", None) if usage else "-",
            getattr(resp, "stop_reason", None),
            len(text or ""),
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal by design
        log.warning(
            "[ch=%d] compaction summary call failed after %dms "
            "(consecutive_failures=%d): %s",
            channel_id, int((time.monotonic() - call_start) * 1000),
            _compact_failures.get(channel_id, 0) + 1, exc,
        )
        _record_compact_failure(channel_id)
        return False

    # Same validity bar as the official CLI ("response did not contain valid
    # text content"): require an actual <summary> block, not just prose.
    if "<summary>" not in (text or ""):
        log.warning(
            "[ch=%d] compaction failed: no <summary> block in response "
            "(len=%d, head=%r)",
            channel_id, len(text or ""), (text or "")[:120],
        )
        _record_compact_failure(channel_id)
        return False
    log.info(
        "[ch=%d] compaction extraction: raw_chars=%d has_analysis=%s",
        channel_id, len(text), "<analysis>" in text,
    )

    wrapped = wrap_summary(
        text,
        transcript_path=history_file_path(channel_id),
        kept_verbatim=bool(keep_rows),
    )
    row = {
        "role": "user",
        "content": wrapped,
        "is_compact_summary": True,
        "keep_tail_rows": len(keep_rows),
        "compact_meta": {
            "model": HISTORY_COMPACT_MODEL,
            "summarized_rows": len(old_rows),
            "source_chars": total_chars,
        },
    }
    await asyncio.to_thread(append_jsonl, channel_id, row)
    _compact_failures.pop(channel_id, None)
    _compact_blocked_until.pop(channel_id, None)
    log.info(
        "[ch=%d] compaction done: summary_chars=%d keep_tail_rows=%d "
        "source_chars=%d compression=%.1f%% total_duration_ms=%d",
        channel_id, len(wrapped), len(keep_rows), total_chars,
        (len(wrapped) + keep_chars) / total_chars * 100 if total_chars else 0.0,
        int((time.monotonic() - call_start) * 1000),
    )
    return True
