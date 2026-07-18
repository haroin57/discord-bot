"""Stable result and checkpoint contracts for the Claude tool loop."""

import uuid
from dataclasses import dataclass


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
    (streaming) and the text prepend (non-streaming).
    """
    return (
        f"> ⚠️ **Fable 5 が応答を拒否したため {_model_display(model)} に"
        "フォールバックしました**"
    )


_CHECKPOINT_NOTE = (
    "-# ⏸️ 作業が長くなったのでここまでを保存しました。「続けて」で同じ文脈から再開できます。"
)

_AUTO_RESUME_NOTE = (
    "-# ⏳ 作業が長くなったので一旦区切りました。続きを自動で実行します…"
)

AUTO_RESUME_DIRECTIVE = (
    "（自動継続）直前の応答は長さ上限で一旦区切られました。保存済みの文脈から"
    "作業をそのまま続けてください。挨拶・状況の再説明・完了済み作業のやり直しは"
    "不要です。残りを終えたら通常どおり結果をまとめてください。"
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
    auto_resume_planned: bool = False,
) -> AgentLoopResult:
    """Build a graceful checkpoint that preserves resumable loop state."""
    accumulated = "\n\n".join(t for t in text_across_turns if t)
    body = accumulated or last_text
    if body:
        note = _AUTO_RESUME_NOTE if auto_resume_planned else _CHECKPOINT_NOTE
        text = body + "\n\n" + note
    else:
        text = (
            "⏳ 作業をここまで保存しました。続きを自動で実行します…"
            if auto_resume_planned
            else "⏸️ 作業をここまで保存しました。「続けて」で同じ文脈から再開できます。"
        )
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
