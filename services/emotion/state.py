"""Persistent emotion state — load/save with atomic file replacement.

Mirrors the uuid-tmp + os.replace pattern from services/session.py:119
(`save_sessions`) so concurrent writers can't corrupt the JSON. State files
are written with mode 0o600 because conversation-derived affection levels
shouldn't be world-readable on a multi-user host.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from services.emotion.params import INITIAL_STATE

log = logging.getLogger(__name__)

CURRENT_VERSION = 1


@dataclass
class LastInteraction:
    timestamp: str | None = None         # ISO-8601 of the last message we saw
    quality: float = 0.5                  # 0..1, blended intimacy proxy
    initiated_by: str = "senpai"          # "senpai" | "kanae"
    topics: list[str] = field(default_factory=list)
    duration_minutes: float = 0.0
    senpai_responsiveness: float = 0.5    # 0..1, response speed/volume proxy


# `Momentum` was dropped in 2026-05-13 cleanup — it was schema-only with no
# consumer. If a trend signal is ever needed again, derive it on-demand from
# the daily emotion journal rather than storing yet another field nobody
# updates.


@dataclass
class EmotionState:
    """Snapshot of かなえ's emotional state at a point in time.

    Floats are stored as-is; bounds are enforced by `transition.clamp` at
    write-time, not on the field itself, so an in-progress calculation can
    briefly hold an out-of-range value without raising.
    """

    version: int = CURRENT_VERSION
    updated_at: str = ""                  # ISO-8601 with timezone

    valence: float = INITIAL_STATE["valence"]
    arousal: float = INITIAL_STATE["arousal"]
    loneliness: float = INITIAL_STATE["loneliness"]
    affection: float = INITIAL_STATE["affection"]
    curiosity: float = INITIAL_STATE["curiosity"]
    anxiety: float = INITIAL_STATE["anxiety"]

    last_interaction: LastInteraction = field(default_factory=LastInteraction)
    consecutive_self_initiated: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EmotionState":
        """Build a state from a (possibly partial) dict.

        Missing keys fall back to INITIAL_STATE / dataclass defaults so we
        can load older state files after extending the schema without
        crashing on KeyError.
        """
        if not isinstance(d, dict):
            return cls()
        kwargs: dict = {}
        # Scalar floats with INITIAL_STATE fallbacks. `dominance` and
        # `satisfaction` were removed in 2026-05-13 cleanup; if an old state
        # file still carries them they're silently dropped here.
        for k in ("valence", "arousal", "loneliness", "affection",
                  "curiosity", "anxiety"):
            v = d.get(k)
            try:
                kwargs[k] = float(v) if v is not None else INITIAL_STATE[k]
            except (TypeError, ValueError):
                kwargs[k] = INITIAL_STATE[k]
        kwargs["version"] = int(d.get("version", CURRENT_VERSION))
        kwargs["updated_at"] = str(d.get("updated_at", "") or "")
        kwargs["consecutive_self_initiated"] = int(d.get("consecutive_self_initiated", 0) or 0)

        # Old state files may still carry a "momentum" object — silently
        # ignored. from_dict's tolerance ensures we don't break on it.
        li = d.get("last_interaction") or {}
        if isinstance(li, dict):
            try:
                quality = float(li.get("quality", 0.5) or 0.5)
            except (TypeError, ValueError):
                quality = 0.5
            try:
                duration = float(li.get("duration_minutes", 0.0) or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            try:
                resp = float(li.get("senpai_responsiveness", 0.5) or 0.5)
            except (TypeError, ValueError):
                resp = 0.5
            topics = li.get("topics") or []
            if not isinstance(topics, list):
                topics = []
            kwargs["last_interaction"] = LastInteraction(
                timestamp=li.get("timestamp"),
                quality=quality,
                initiated_by=str(li.get("initiated_by", "senpai") or "senpai"),
                topics=[str(t) for t in topics][:8],
                duration_minutes=duration,
                senpai_responsiveness=resp,
            )
        return cls(**kwargs)


def load_state(path: str | os.PathLike) -> EmotionState:
    """Read state from `path`. Missing/corrupt files yield a fresh default.

    Fail-open is intentional: a bot that can't load its mood file should
    keep running with a blank slate, not crash and stop responding.
    """
    p = Path(path)
    if not p.is_file():
        return EmotionState()
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("emotion state load failed (%s); using default", exc)
        return EmotionState()
    return EmotionState.from_dict(data)


def save_state(state: EmotionState, path: str | os.PathLike) -> None:
    """Atomically write state to `path` with mode 0o600.

    Same uuid-tmp + os.replace pattern as services/session.py:save_sessions
    so two concurrent writers (cron tick + bot hook) cannot interleave and
    leave a half-written JSON.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(tmp_path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        fd = None  # fdopen took ownership
        os.replace(tmp_path, p)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        # Best-effort cleanup so a failed write doesn't leave .tmp turds.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
