"""Persistent KanaeSelfState — interests + episodes ring buffer.

Mirrors the load/save discipline of `services.emotion.state`: atomic
write via `utils.atomic_write_json`, mode 0o600, fail-open on missing or
corrupt files. The schema is forward-compatible — `from_dict` tolerates
missing keys so older state files keep loading after Phase 2/3 add new
fields.

The state is the *single source of truth* for two things:
  - Interests: topic → Interest map, decayed every turn
  - Episodes: ring buffer of recent conversation summaries (Phase 2)

Concurrency: shared with the bot hook only (no cron writer in Phase 1),
so we don't take a flock here. If a Phase 2/3 cron worker starts
mutating this file, wrap the load/mutate/save cycle in an flock similar
to `services.emotion.lock`.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from utils.atomic_write import atomic_write_json

log = logging.getLogger(__name__)

CURRENT_VERSION = 1
DEFAULT_STATE_FILENAME = "kanae_self.json"

# Caps to keep the state file bounded. Interests beyond MAX_INTERESTS get
# evicted by lowest salience; episodes beyond MAX_EPISODES drop oldest.
MAX_INTERESTS = 100
MAX_EPISODES = 10

# Salience bump applied at recall time to records whose channel_id matches
# the active channel. 0.5 lifts a same-channel salience-0.4 above an
# other-channel salience-0.85 but still lets a hot global topic (0.95+) win.
SAME_CHANNEL_BONUS = 0.5


@dataclass
class Interest:
    """A topic kanae has shown interest in, with its current salience.

    Salience decays exponentially each turn (see `interests.decay_and_upsert`).
    `valence_attached` and `arousal_attached` are EMA-tracked so a topic
    that's repeatedly discussed in a positive context drifts positive.
    """

    topic: str                                  # canonical lowercase
    salience: float = 0.3
    last_touched_at: str = ""                   # ISO-8601 with tz
    first_seen_at: str = ""
    mention_count: int = 1
    valence_attached: float = 0.0               # -1..+1
    arousal_attached: float = 0.0               # 0..1
    source_episode_ids: list[str] = field(default_factory=list)
    entity_refs: list[str] = field(default_factory=list)
    channel_id: str = ""                        # last channel where upserted

    @classmethod
    def from_dict(cls, d: dict) -> "Interest":
        if not isinstance(d, dict):
            return cls(topic="")
        topic = str(d.get("topic", "") or "").strip().lower()
        try:
            salience = float(d.get("salience", 0.3) or 0.3)
        except (TypeError, ValueError):
            salience = 0.3
        try:
            mention_count = int(d.get("mention_count", 1) or 1)
        except (TypeError, ValueError):
            mention_count = 1
        try:
            valence = float(d.get("valence_attached", 0.0) or 0.0)
        except (TypeError, ValueError):
            valence = 0.0
        try:
            arousal = float(d.get("arousal_attached", 0.0) or 0.0)
        except (TypeError, ValueError):
            arousal = 0.0
        sources = d.get("source_episode_ids") or []
        if not isinstance(sources, list):
            sources = []
        ents = d.get("entity_refs") or []
        if not isinstance(ents, list):
            ents = []
        return cls(
            topic=topic,
            salience=max(0.0, min(1.0, salience)),
            last_touched_at=str(d.get("last_touched_at", "") or ""),
            first_seen_at=str(d.get("first_seen_at", "") or ""),
            mention_count=max(0, mention_count),
            valence_attached=max(-1.0, min(1.0, valence)),
            arousal_attached=max(0.0, min(1.0, arousal)),
            source_episode_ids=[str(s) for s in sources][:5],
            entity_refs=[str(e) for e in ents][:8],
            channel_id=str(d.get("channel_id", "") or ""),
        )


@dataclass
class Episode:
    """A short summary of one conversational turn.

    Phase 1 ships the schema but doesn't populate it yet — Phase 2 wires
    `extract_kanae_signals` to emit `episode` and `episodes.append_episode`
    to push it onto the ring buffer.
    """

    id: str
    ts: str                                     # ISO-8601 JST
    channel_id: str = ""
    summary: str = ""                           # ≤60 字
    topics: list[str] = field(default_factory=list)
    valence_delta: float = 0.0
    importance: int = 1                         # 1..5

    @classmethod
    def from_dict(cls, d: dict) -> "Episode":
        if not isinstance(d, dict):
            return cls(id="", ts="")
        try:
            valence_delta = float(d.get("valence_delta", 0.0) or 0.0)
        except (TypeError, ValueError):
            valence_delta = 0.0
        try:
            importance = int(d.get("importance", 1) or 1)
        except (TypeError, ValueError):
            importance = 1
        topics = d.get("topics") or []
        if not isinstance(topics, list):
            topics = []
        return cls(
            id=str(d.get("id", "") or ""),
            ts=str(d.get("ts", "") or ""),
            channel_id=str(d.get("channel_id", "") or ""),
            summary=str(d.get("summary", "") or "")[:120],
            topics=[str(t).strip().lower() for t in topics if str(t).strip()][:3],
            valence_delta=max(-1.0, min(1.0, valence_delta)),
            importance=max(1, min(5, importance)),
        )


@dataclass
class KanaeSelfState:
    version: int = CURRENT_VERSION
    updated_at: str = ""
    interests: dict[str, Interest] = field(default_factory=dict)
    episodes: list[Episode] = field(default_factory=list)
    last_extract_at: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "interests": {k: asdict(v) for k, v in self.interests.items()},
            "episodes": [asdict(e) for e in self.episodes],
            "last_extract_at": self.last_extract_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KanaeSelfState":
        if not isinstance(d, dict):
            return cls()
        raw_interests = d.get("interests") or {}
        interests: dict[str, Interest] = {}
        if isinstance(raw_interests, dict):
            for k, v in raw_interests.items():
                topic_key = str(k).strip().lower()
                if not topic_key:
                    continue
                interest = Interest.from_dict(v)
                if interest.topic and interest.topic == topic_key:
                    interests[topic_key] = interest
        raw_eps = d.get("episodes") or []
        episodes: list[Episode] = []
        if isinstance(raw_eps, list):
            for e in raw_eps:
                ep = Episode.from_dict(e)
                if ep.id:
                    episodes.append(ep)
            episodes = episodes[-MAX_EPISODES:]
        return cls(
            version=int(d.get("version", CURRENT_VERSION) or CURRENT_VERSION),
            updated_at=str(d.get("updated_at", "") or ""),
            interests=interests,
            episodes=episodes,
            last_extract_at=str(d.get("last_extract_at", "") or ""),
        )


def load_state(path: str | os.PathLike) -> KanaeSelfState:
    """Read state from `path`. Missing or corrupt files yield a fresh default.

    Fail-open is intentional — a bot that can't load its self-model
    shouldn't refuse to respond. A blank state just rebuilds itself
    after a few turns.
    """
    p = Path(path)
    if not p.is_file():
        return KanaeSelfState()
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("kanae_self state load failed (%s); using default", exc)
        return KanaeSelfState()
    return KanaeSelfState.from_dict(data)


def save_state(state: KanaeSelfState, path: str | os.PathLike) -> None:
    """Atomically write state to `path` with mode 0o600."""
    atomic_write_json(path, state.to_dict())
