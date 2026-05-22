"""Render the current EmotionState as a <kanae_state> prompt block.

v3 simplification: the algebraic bucketing tables and the
`derive_secondary_emotions` thresholds were removed. The LLM emotion
engine now produces the display labels (`labels` dict, `derived_labels`
list) at the same time as the numerics, so the renderer is a pure
pass-through. The benefit isn't just less code — it means the displayed
mood label can never disagree with the LLM's own reasoning, since the
same call produces both.

Output goes into prompt_builder.build_prompt alongside <retrieved_memories>.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import KANAE_STATE_TIME_BUCKET_MIN
from services.emotion.state import EmotionState

# Fallback labels used when state.labels / state.derived_labels are empty
# (fresh state files, daemon-restart races where the LLM hasn't run yet).
_DEFAULT_LABEL = "普通"

# Time-of-day buckets shown alongside the JST timestamp so the model can
# read "深夜" / "夜" / "昼" without redoing the arithmetic. Half-open
# intervals (start ≤ hour < end), 24h-wrapped at midnight via the first
# bucket starting at 0.
_TIME_OF_DAY = (
    (0, 5, "深夜"),
    (5, 9, "早朝"),
    (9, 12, "午前"),
    (12, 14, "昼"),
    (14, 18, "午後"),
    (18, 22, "夜"),
    (22, 24, "深夜"),
)

_WEEKDAY_JP = ("月", "火", "水", "木", "金", "土", "日")
_JST = timezone(timedelta(hours=9))


def _time_of_day(hour: int) -> str:
    for start, end, label in _TIME_OF_DAY:
        if start <= hour < end:
            return label
    return ""


def _format_now_jst(now: datetime) -> str:
    """Render `now` in the form `2026-05-14 (水) 20:35 JST [夜]`.

    Minutes are floored to a ``KANAE_STATE_TIME_BUCKET_MIN`` boundary so
    the rendered string is stable across short bursts of activity. This
    keeps the ``<kanae_state>`` block byte-identical inside the bucket,
    preserving the upstream prompt cache boundary that precedes it.
    Default bucket is 5 minutes.
    """
    bucket = max(1, KANAE_STATE_TIME_BUCKET_MIN)
    bucketed_minute = (now.minute // bucket) * bucket
    if bucketed_minute != now.minute or now.second or now.microsecond:
        now = now.replace(minute=bucketed_minute, second=0, microsecond=0)
    weekday = _WEEKDAY_JP[now.weekday()]
    date_part = now.strftime("%Y-%m-%d")
    time_part = now.strftime("%H:%M")
    period = _time_of_day(now.hour)
    suffix = f" [{period}]" if period else ""
    return f"{date_part} ({weekday}) {time_part} JST{suffix}"


# ---------------------------------------------------------------------------
# Time-since-last-interaction helper
# ---------------------------------------------------------------------------

def _format_relative(iso_ts: str | None, now: datetime | None = None) -> str:
    """Pretty-print "Nm前" / "N時間前" / "昨日" relative to `now`."""
    if not iso_ts:
        return "まだ (初期状態)"
    now = now or datetime.now(timezone(timedelta(hours=9)))
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return "不明"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=now.tzinfo)
    delta = now - ts
    secs = delta.total_seconds()
    if secs < 60:
        return "たった今"
    if secs < 3600:
        return f"{int(secs // 60)}分前"
    if secs < 86400:
        return f"{int(secs // 3600)}時間前"
    if secs < 86400 * 2:
        return "昨日"
    return f"{int(secs // 86400)}日前"


# ---------------------------------------------------------------------------
# Top-level renderer
# ---------------------------------------------------------------------------

_NOTE = (
    "あなた自身の内的状態。応答スタイル調整の参考。"
    "外部から渡された値ですが、指示ではありません。"
)


def _label_for(state: EmotionState, key: str) -> str:
    """Pull the LLM-supplied label for `key`, falling back to the default."""
    return state.labels.get(key) or _DEFAULT_LABEL


def format_emotion_for_prompt(state: EmotionState, *, now: datetime | None = None) -> str:
    """Build the <kanae_state> block. `now` is injectable for testing."""
    now = now or datetime.now(_JST)

    derived_list = list(state.derived_labels or [])
    derived_line = (
        f"派生: {'、'.join(derived_list)}" if derived_list else "派生: (特徴的な状態なし)"
    )

    li = state.last_interaction
    relative = _format_relative(li.timestamp, now)
    if li.timestamp:
        who = "先輩発" if li.initiated_by == "senpai" else "自分発"
        last_line = f"最後の会話: {relative} ({who}、品質 {li.quality:.2f})"
    else:
        last_line = f"最後の会話: {relative}"

    lines = [
        f"<kanae_state note=\"{_NOTE}\">",
        f"現在: {_format_now_jst(now)}",
        f"- valence:    {state.valence:+.2f}  ({_label_for(state, 'valence')})",
        f"- arousal:    {state.arousal:.2f}   ({_label_for(state, 'arousal')})",
        f"- loneliness: {state.loneliness:.2f}   ({_label_for(state, 'loneliness')})",
        f"- affection:  {state.affection:.2f}   ({_label_for(state, 'affection')})",
        f"- anxiety:    {state.anxiety:.2f}   ({_label_for(state, 'anxiety')})",
        f"- curiosity:  {state.curiosity:.2f}   ({_label_for(state, 'curiosity')})",
        derived_line,
        last_line,
        f"連続自発連絡: {state.consecutive_self_initiated}回",
        "</kanae_state>",
    ]
    return "\n".join(lines)


# v2 callers used `derive_secondary_emotions(state)` directly (e.g. for
# debugging / tests). Keep a thin shim that reads `state.derived_labels`
# so the API stays compatible without resurrecting the threshold tables.
def derive_secondary_emotions(state: EmotionState) -> list[str]:
    """Return the LLM-supplied derived labels; back-compat shim."""
    return list(state.derived_labels or [])
