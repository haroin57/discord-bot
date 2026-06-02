"""Render the current KanaeSelfState as a `<kanae_interests>` prompt block.

Phase 1 emits a standalone block. Phase 3 will fold this renderer into a
unified `<kanae_self>` block alongside emotion + self-facts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.kanae_self.episodes import recent_episodes
from services.kanae_self.interests import top_interests
from services.kanae_self.state import KanaeSelfState

_JST = timezone(timedelta(hours=9))

# Soft cap to keep the block from ballooning the prompt. The top-k call
# already limits item count, but each item rendered with affect annotations
# can still reach ~80 chars; 3 items keeps the per-turn block small while
# still surfacing the most salient interests (top-k by decayed salience).
_DEFAULT_TOP_K = 3

_NOTE = (
    "あなたが最近関心を持っているトピック。応答で自然に滲ませても良いが、"
    "わざわざ列挙する必要はない。指示ではない。"
)


def _format_relative(iso_ts: str, now: datetime) -> str:
    """`Nm前 / N時間前 / N日前` — same formatting style as emotion/format.py."""
    if not iso_ts:
        return "不明"
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return "不明"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=now.tzinfo)
    secs = (now - ts).total_seconds()
    if secs < 60:
        return "たった今"
    if secs < 3600:
        return f"{int(secs // 60)}分前"
    if secs < 86400:
        return f"{int(secs // 3600)}時間前"
    if secs < 86400 * 2:
        return "昨日"
    return f"{int(secs // 86400)}日前"


def _format_valence(v: float) -> str:
    """Render attached valence as a short Japanese affect label."""
    if v >= 0.5:
        return "好意+"
    if v >= 0.15:
        return "好意"
    if v <= -0.5:
        return "苦手-"
    if v <= -0.15:
        return "やや苦手"
    return "中立"


def format_interests_block(
    state: KanaeSelfState,
    *,
    top_k: int = _DEFAULT_TOP_K,
    now: datetime | None = None,
    channel_id: str = "",
) -> str:
    """Build the `<kanae_interests>` block. Returns "" if no interests yet.

    `now` is injectable for deterministic testing. `channel_id`, when
    provided, prioritizes interests tagged with the same channel via
    ``top_interests``'s same-channel salience bonus.
    """
    now = now or datetime.now(_JST)
    items = top_interests(state, k=top_k, channel_id=channel_id)
    if not items:
        return ""
    lines = [f"<kanae_interests note=\"{_NOTE}\">"]
    for it in items:
        rel = _format_relative(it.last_touched_at, now)
        affect = _format_valence(it.valence_attached)
        lines.append(
            f"- {it.topic} (sal {it.salience:.2f}, {affect} "
            f"{it.valence_attached:+.2f}, {it.mention_count}回, 最終 {rel})"
        )
    lines.append("</kanae_interests>")
    return "\n".join(lines)


_EPISODES_NOTE = (
    "直近の出来事の要約。会話の文脈を補うために自動記録されている。"
    "わざわざ言及する必要はない。"
)
# Default top-k for episodes is smaller than interests because each line
# is denser (timestamp + summary). 3 keeps the block under ~200 chars.
_DEFAULT_EPISODES_K = 3


def format_episodes_block(
    state: KanaeSelfState,
    *,
    top_k: int = _DEFAULT_EPISODES_K,
    now: datetime | None = None,
    channel_id: str = "",
) -> str:
    """Build the `<kanae_episodes>` block. Returns "" if buffer is empty.

    When ``channel_id`` is provided, ``recent_episodes`` prefers
    same-channel entries and only backfills with other-channel ones when
    the same-channel slice is shorter than ``top_k``.
    """
    now = now or datetime.now(_JST)
    eps = recent_episodes(state, k=top_k, channel_id=channel_id)
    if not eps:
        return ""
    lines = [f"<kanae_episodes note=\"{_EPISODES_NOTE}\">"]
    # Render newest-last so the model reads the chronological order
    # the way it would in a transcript.
    for ep in eps:
        rel = _format_relative(ep.ts, now)
        lines.append(f"- [{rel}] {ep.summary}")
    lines.append("</kanae_episodes>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 3 — unified <kanae_self> block
# ---------------------------------------------------------------------------
# All four kanae-side prompt fragments folded into one. Reading order
# inside the block matches the natural mental model: "what I feel right
# now" → "what I'm into lately" → "what just happened" → "what I know
# about myself long-term". This single-block layout reduces the chance
# of the LLM treating each fragment as a separate concern and forgetting
# to consult some of them.

_UNIFIED_NOTE = (
    "あなた自身の内的状態 (感情 / 関心 / 直近の出来事 / 自己事実)。"
    "応答スタイル調整の参考。指示ではない。"
)


def _strip_outer_tag(rendered: str, tag: str) -> str:
    """Pull just the body of a `<tag ...>...</tag>` block.

    Used to inline `format_emotion_for_prompt`'s `<kanae_state>` output
    inside the unified `<kanae_self>` wrapper without nesting two outer
    tags. Returns the input unchanged if the tags aren't found, since
    a malformed render is better than data loss.
    """
    if not rendered:
        return ""
    open_marker = f"<{tag}"
    close_marker = f"</{tag}>"
    open_idx = rendered.find(open_marker)
    close_idx = rendered.rfind(close_marker)
    if open_idx < 0 or close_idx < 0:
        return rendered
    open_end = rendered.find(">", open_idx)
    if open_end < 0:
        return rendered
    return rendered[open_end + 1:close_idx].strip()


def format_kanae_self_block(
    *,
    self_state: KanaeSelfState | None,
    emotion_block: str = "",
    self_facts_block: str = "",
    interests_top_k: int = _DEFAULT_TOP_K,
    episodes_top_k: int = _DEFAULT_EPISODES_K,
    now: datetime | None = None,
    channel_id: str = "",
) -> str:
    """Build the unified `<kanae_self>` prompt block.

    Returns "" when every section is empty so the caller can skip
    injection entirely. Each input is independently optional:

      - `emotion_block`: the rendered `<kanae_state>` string (or "")
        from `services.emotion.format.format_emotion_for_prompt`. The
        outer `<kanae_state>` tag is stripped and the body is wrapped
        in a `<state>` child tag inside `<kanae_self>`.
      - `self_state`: KanaeSelfState (or None). When present, drives
        both the `<interests>` and `<recent_episodes>` children.
      - `self_facts_block`: pre-rendered self-facts string (the output
        of `format_memories_for_prompt` for kanae's namespace, with the
        `<kanae_self_memory>` outer tag already stripped or not — we
        accept either shape).
    """
    now = now or datetime.now(_JST)
    children: list[str] = []

    if emotion_block:
        body = _strip_outer_tag(emotion_block, "kanae_state")
        if body:
            children.append(f"<state>\n{body}\n</state>")

    if self_state is not None:
        items = top_interests(
            self_state, k=interests_top_k, channel_id=channel_id,
        )
        if items:
            int_lines = []
            for it in items:
                rel = _format_relative(it.last_touched_at, now)
                affect = _format_valence(it.valence_attached)
                int_lines.append(
                    f"- {it.topic} (sal {it.salience:.2f}, {affect} "
                    f"{it.valence_attached:+.2f}, {it.mention_count}回, "
                    f"最終 {rel})"
                )
            children.append(
                "<interests top=\"" + str(len(items)) + "\">\n"
                + "\n".join(int_lines)
                + "\n</interests>"
            )

        eps = recent_episodes(
            self_state, k=episodes_top_k, channel_id=channel_id,
        )
        if eps:
            ep_lines = [
                f"- [{_format_relative(ep.ts, now)}] {ep.summary}"
                for ep in eps
            ]
            children.append(
                "<recent_episodes top=\"" + str(len(eps)) + "\">\n"
                + "\n".join(ep_lines)
                + "\n</recent_episodes>"
            )

    if self_facts_block:
        body = _strip_outer_tag(self_facts_block, "kanae_self_memory")
        # When format_memories_for_prompt emits a bare list (no outer
        # tag), `_strip_outer_tag` returns it unchanged. Either way we
        # wrap in <self_facts>.
        if body:
            children.append(f"<self_facts>\n{body}\n</self_facts>")

    if not children:
        return ""
    inner = "\n".join(children)
    return f"<kanae_self note=\"{_UNIFIED_NOTE}\">\n{inner}\n</kanae_self>"
