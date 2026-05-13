"""Per-conversation interaction analyzer.

Run after each admin (haroin) message → assistant reply pair. Asks Haiku
to classify the exchange into one of the event types in `EVENT_DELTAS` so
the feedback loop has a concrete delta to apply.

Failure mode is fail-soft: if Haiku is unavailable or returns garbage we
return a neutral `short_reply` so the emotion state still ticks but
without overcommitting to a guessed event.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

_KNOWN_EVENTS = frozenset({
    "senpai_initiated", "long_good_chat", "read_ignore", "short_reply",
    "praised", "conflict", "sweet_words",
})

_FALLBACK = {
    "event_type": "short_reply",
    "intensity": 0.5,
    "topics": [],
    "intimacy_level": 0.5,
    "senpai_engagement": 0.5,
    "conflict_detected": False,
}

_PROMPT = """以下の会話 1 ターンを分析して JSON 1行で返してください。

直近文脈: {history}

ユーザー(先輩): {user_text}
アシスタント(かなえ): {assistant_text}

判定:
- event_type: senpai_initiated / long_good_chat / short_reply / praised / sweet_words / conflict / read_ignore のどれか
- intensity: 0.0-1.0
- topics: 1-3個の短い日本語キーワード(配列)
- intimacy_level: 親密さ 0.0-1.0
- senpai_engagement: 先輩の関与度 0.0-1.0
- conflict_detected: bool

出力: JSON 1行のみ。コードブロックや前置きは禁止。
"""


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n]


async def analyze_interaction(
    user_text: str, assistant_text: str, history_tail: str = "",
) -> dict:
    """Classify one user↔assistant turn into an event delta dict.

    Always returns a dict with the same shape as `_FALLBACK`. Validates
    event_type against `_KNOWN_EVENTS` and clamps numerics to [0,1].
    """
    try:
        from services.mem0.client import get_anthropic_client
        client = await get_anthropic_client()
    except Exception as exc:
        log.warning("emotion analyzer: get_anthropic_client failed: %s", exc)
        return dict(_FALLBACK)
    if client is None:
        return dict(_FALLBACK)

    prompt = _PROMPT.format(
        history=_truncate(history_tail, 500),
        user_text=_truncate(user_text, 500),
        assistant_text=_truncate(assistant_text, 500),
    )

    try:
        resp = await client.messages.create(
            model=os.environ.get("EMOTION_ANALYZER_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
    except Exception as exc:
        log.warning("emotion analyzer: Haiku call failed: %s", exc)
        return dict(_FALLBACK)

    # Strip code fences if Haiku decorates despite the instruction.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("emotion analyzer: JSON parse failed: %s (text=%r)", exc, text[:200])
        return dict(_FALLBACK)
    if not isinstance(data, dict):
        return dict(_FALLBACK)

    return _normalize(data)


def _normalize(data: dict) -> dict:
    """Coerce Haiku's output into the strict fallback shape."""
    event_type = data.get("event_type")
    if event_type not in _KNOWN_EVENTS:
        event_type = "short_reply"
    try:
        intensity = max(0.0, min(1.0, float(data.get("intensity", 0.5))))
    except (TypeError, ValueError):
        intensity = 0.5
    try:
        intimacy = max(0.0, min(1.0, float(data.get("intimacy_level", 0.5))))
    except (TypeError, ValueError):
        intimacy = 0.5
    try:
        engagement = max(0.0, min(1.0, float(data.get("senpai_engagement", 0.5))))
    except (TypeError, ValueError):
        engagement = 0.5
    topics = data.get("topics") or []
    if not isinstance(topics, list):
        topics = []
    topics = [str(t)[:40] for t in topics if t][:3]
    return {
        "event_type": event_type,
        "intensity": intensity,
        "topics": topics,
        "intimacy_level": intimacy,
        "senpai_engagement": engagement,
        "conflict_detected": bool(data.get("conflict_detected", False)),
    }
