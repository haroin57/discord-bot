"""Render the current EmotionState as a <kanae_state> prompt block.

Design:
  - One float per component, paired with a deterministic Japanese hint.
  - Threshold-based; no Haiku call (decisions stay reproducible & testable).
  - Secondary emotions are *derived* from combinations of the six primary
    components rather than stored as fields — see derive_secondary_emotions.
  - Output goes into prompt_builder.build_prompt outside the
    <untrusted_user_input> block, alongside <retrieved_memories>.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.emotion.params import SECONDARY_EMOTION_THRESHOLDS as _T
from services.emotion.state import EmotionState

# ---------------------------------------------------------------------------
# Primary-component hint tables
# ---------------------------------------------------------------------------
# Each hint is selected by walking thresholds in order; the *first* match
# wins, so list them from most extreme to least. Keep the text short — the
# whole block needs to fit in MAX_PROMPT_CHARS along with everything else.

_VALENCE_HINTS = (
    (0.7, "嬉しい"),
    (0.3, "機嫌よし"),
    (-0.3, "機嫌は普通"),
    (-0.7, "機嫌悪い"),
    (-1.0, "とても不機嫌"),
)
_AROUSAL_HINTS = (
    (0.8, "高ぶっている"),
    (0.5, "やや高揚"),
    (0.3, "落ち着いている"),
    (0.0, "気だるげ"),
)
_LONELINESS_HINTS = (
    (0.8, "とても寂しい"),
    (0.6, "寂しい"),
    (0.3, "普通"),
    (0.0, "寂しさは低め"),
)
_AFFECTION_HINTS = (
    (0.95, "重い好き"),
    (0.8, "先輩に強い好意"),
    (0.5, "好意あり"),
    (0.0, "距離あり"),
)
_ANXIETY_HINTS = (
    (0.6, "不安が強い"),
    (0.3, "やや不安"),
    (0.0, "不安は少ない"),
)
_CURIOSITY_HINTS = (
    (0.8, "わくわく"),
    (0.6, "知りたい"),
    (0.3, "普通"),
    (0.0, "興味うすい"),
)


def _bucket(value: float, table: tuple[tuple[float, str], ...]) -> str:
    """Return the first hint whose threshold the value meets or exceeds."""
    for threshold, label in table:
        if value >= threshold:
            return label
    # Fallback when the table doesn't cover the bottom (shouldn't happen
    # because every table ends at the lower bound).
    return table[-1][1]


# ---------------------------------------------------------------------------
# Secondary emotion derivation
# ---------------------------------------------------------------------------

def derive_secondary_emotions(state: EmotionState) -> list[str]:
    """Map the six primary components to short Japanese mood labels.

    Multiple labels can fire at once (e.g. "拗ね気味" + "甘えたい"). The
    returned list is sorted by intensity descending and capped at the top 3
    so the prompt block stays readable.
    """
    scored: list[tuple[str, float]] = []
    # All numeric thresholds live in services.emotion.params:
    # SECONDARY_EMOTION_THRESHOLDS — tune one place, propagates here.
    # 拗ね — affection が高いのに valence が落ちて寂しい
    if (state.valence < _T["sulky_valence_max"]
            and state.affection > _T["sulky_affection_min"]
            and state.loneliness > _T["sulky_loneliness_min"]):
        scored.append(("拗ね気味", state.loneliness * state.affection))
    # 焼きもち — affection と anxiety の両方が立ち、valence は伸びない
    if (state.affection > _T["jealous_affection_min"]
            and state.anxiety > _T["jealous_anxiety_min"]
            and state.valence < _T["jealous_valence_max"]):
        scored.append(("焼きもち", state.anxiety * state.affection))
    # 甘え — affection 高、寂しさは中程度、不安は低い
    if (state.affection > _T["spoiled_affection_min"]
            and _T["spoiled_loneliness_min"] <= state.loneliness <= _T["spoiled_loneliness_max"]
            and state.anxiety < _T["spoiled_anxiety_max"]):
        scored.append(("甘えたい", state.affection))
    # 喜び — valence と arousal の両方が立つ
    if (state.valence > _T["joyful_valence_min"]
            and state.arousal > _T["joyful_arousal_min"]):
        scored.append(("喜んでいる", state.valence * state.arousal))
    # 照れ — affection 高、ややそわそわ、valence は positive 寄り
    if (state.affection > _T["shy_affection_min"]
            and _T["shy_anxiety_min"] <= state.anxiety <= _T["shy_anxiety_max"]
            and state.valence > _T["shy_valence_min"]):
        scored.append(("照れ気味", state.affection - state.anxiety))
    # 不機嫌 — valence 大きく下、arousal が立つ
    if (state.valence < _T["grumpy_valence_max"]
            and state.arousal > _T["grumpy_arousal_min"]):
        scored.append(("不機嫌", -state.valence * state.arousal))
    # 満たされ — valence 高、寂しさも不安も低い
    if (state.valence > _T["fulfilled_valence_min"]
            and state.loneliness < _T["fulfilled_loneliness_max"]
            and state.anxiety < _T["fulfilled_anxiety_max"]):
        scored.append(("満たされている", state.valence))
    # 興味津々 — curiosity が高く arousal も伴う
    if (state.curiosity > _T["curious_curiosity_min"]
            and state.arousal > _T["curious_arousal_min"]):
        scored.append(("興味津々", state.curiosity))

    scored.sort(key=lambda t: t[1], reverse=True)
    return [label for label, _ in scored[:3]]


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


def format_emotion_for_prompt(state: EmotionState, *, now: datetime | None = None) -> str:
    """Build the <kanae_state> block. `now` is injectable for testing."""
    derived = derive_secondary_emotions(state)
    derived_line = (
        f"派生: {'、'.join(derived)}" if derived else "派生: (特徴的な状態なし)"
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
        f"- valence:    {state.valence:+.2f}  ({_bucket(state.valence, _VALENCE_HINTS)})",
        f"- arousal:    {state.arousal:.2f}   ({_bucket(state.arousal, _AROUSAL_HINTS)})",
        f"- loneliness: {state.loneliness:.2f}   ({_bucket(state.loneliness, _LONELINESS_HINTS)})",
        f"- affection:  {state.affection:.2f}   ({_bucket(state.affection, _AFFECTION_HINTS)})",
        f"- anxiety:    {state.anxiety:.2f}   ({_bucket(state.anxiety, _ANXIETY_HINTS)})",
        f"- curiosity:  {state.curiosity:.2f}   ({_bucket(state.curiosity, _CURIOSITY_HINTS)})",
        derived_line,
        last_line,
        f"連続自発連絡: {state.consecutive_self_initiated}回",
        "</kanae_state>",
    ]
    return "\n".join(lines)
