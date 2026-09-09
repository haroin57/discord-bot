"""Durable, speaker-aware memory batches with explicit acknowledgement.

Existing JSON input remains readable. Unowned old turns are retained, never
guessed into a new speaker's account. POSIX file locks serialize processes.
Delivery is at-least-once: an uncertain provider outcome still needs fact dedup.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from .atomic_write import atomic_write_json
from .settings import settings

BATCH_SIZE = settings.MEM0_BATCH_SIZE
MAX_TURN_CHARS = 128_000
_COMPLETED_REQUESTS = 2048
_batch_lock = threading.RLock()


def _batch_file(state_dir, channel_id) -> Path:
    channel = str(int(channel_id))
    directory = Path(state_dir) / "mem0_batch"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / f"{channel}.json"


@contextmanager
def _locked(state_dir, channel_id):
    path = _batch_file(state_dir, channel_id)
    with _batch_lock:
        fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _load_raw(state_dir, channel_id) -> dict:
    path = _batch_file(state_dir, channel_id)
    if not path.exists():
        return {"version": 2, "turns": [], "extracted_facts": [], "inflight": {}, "completed": []}
    # Fail explicitly instead of overwriting corrupted/unreadable input.
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("invalid memory batch object")
    for key, expected, default in (("turns", list, []), ("extracted_facts", list, []),
                                   ("inflight", dict, {}), ("completed", list, [])):
        data.setdefault(key, default)
        if not isinstance(data[key], expected):
            raise ValueError(f"invalid memory batch field: {key}")
    if any(not isinstance(t, dict) for t in data["turns"]):
        raise ValueError("invalid memory batch turn")
    for pending in data["inflight"].values():
        if (not isinstance(pending, dict) or not isinstance(pending.get("turns"), list)
                or not isinstance(pending.get("lease_until"), (int, float))
                or not all(isinstance(t, dict) for t in pending["turns"])
                or not all(k in pending for k in ("user_id", "token", "extracted_facts"))):
            raise ValueError("invalid memory batch lease")
    return data


def _save_raw(state_dir, channel_id, data):
    atomic_write_json(_batch_file(state_dir, channel_id), data)


def _owner(turn):
    return str(turn.get("user_id") or "")


def _request_key(turn):
    if turn.get("message_id"):
        return [_owner(turn), "message:" + str(turn["message_id"])]
    request = turn.get("request_id")
    return [_owner(turn), str(request)] if request else None


def _all_turns(data):
    yield from data["turns"]
    for pending in data["inflight"].values():
        yield from pending["turns"]


def append_turn(state_dir, channel_id, user_text: str, assistant_text: str, *,
                request_id: str | None = None, user_id: str | None = None,
                author_id: str | None = None, source_timestamp: str | None = None,
                message_id: str | None = None, force_flush: bool = False) -> int:
    """Persist full input; return this owner's outstanding turn count.

    Redelivered request IDs are ignored across pending and recently completed
    batches. Oversized input is rejected explicitly, not silently truncated.
    """
    if len(user_text) > MAX_TURN_CHARS or len(assistant_text) > MAX_TURN_CHARS:
        raise ValueError(f"memory turn exceeds {MAX_TURN_CHARS} characters")
    owner = str(user_id or "")
    turn = {"id": uuid.uuid4().hex, "user": user_text, "assistant": assistant_text,
            "ts": source_timestamp or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "user_id": owner, "author_id": str(author_id or ""),
            "message_id": str(message_id or ""),
            "force_flush": bool(force_flush),
            "request_id": str(request_id or "")}
    with _locked(state_dir, channel_id):
        data = _load_raw(state_dir, channel_id)
        key = _request_key(turn)
        duplicate = key is not None and (key in data["completed"] or
                    any(_request_key(t) == key for t in _all_turns(data)))
        if not duplicate:
            data["turns"].append(turn)
        # A prior replace can have succeeded while directory fsync failed.
        # Redelivery must re-establish durability before reporting success.
        _save_raw(state_dir, channel_id, data)
        return sum(_owner(t) == owner for t in _all_turns(data))


def build_batch_summary(turns: list[dict], turn_chars: int | None = None) -> str:
    """Render role-separated turns. Explicit MAX_TURN_CHARS preserves full text.

    The default prompt cap remains compatible with existing standalone callers;
    the durable bot recorder opts into full input and bounded extractor chunks.
    """
    if turn_chars is None:
        turn_chars = settings.MEM0_BATCH_TURN_CHARS
    turn_chars = max(0, int(turn_chars))
    return "\n".join(
        f"user: {(t.get('user') or '')[:turn_chars]}\n"
        f"assistant: {(t.get('assistant') or '')[:turn_chars]}"
        for t in (turns or [])
    )


def append_extracted_facts(state_dir, channel_id, facts: list[str]) -> None:
    """Retain legacy unowned facts without assigning them to a new speaker."""
    if facts:
        with _locked(state_dir, channel_id):
            data = _load_raw(state_dir, channel_id)
            data["extracted_facts"].extend(facts)
            _save_raw(state_dir, channel_id, data)


@dataclass(frozen=True)
class BatchLease:
    channel_id: int
    batch_id: str
    token: str
    user_id: str
    turns: list[dict]
    extracted_facts: list[str]

    def __iter__(self):
        yield self.turns
        yield self.extracted_facts


def claim_batch(state_dir, channel_id, *, user_id: str | None = None,
                lease_seconds: float = 300, max_turns: int = 50) -> BatchLease | None:
    """Lease an owner's oldest retry or pending input, never deleting it.

    None means the unowned legacy queue, not every speaker. One claim per owner
    may be active; different speakers proceed separately.
    """
    if not math.isfinite(lease_seconds) or lease_seconds <= 0 or max_turns <= 0:
        raise ValueError("lease_seconds and max_turns must be positive")
    owner, now = str(user_id or ""), time.time()
    with _locked(state_dir, channel_id):
        data = _load_raw(state_dir, channel_id)
        pending = [(bid, p) for bid, p in data["inflight"].items() if p["user_id"] == owner]
        if any(p["lease_until"] > now for _, p in pending):
            return None
        if pending:
            batch_id, payload = pending[0]
        else:
            turns = [t for t in data["turns"] if _owner(t) == owner][:max_turns]
            facts = list(data["extracted_facts"]) if not owner else []
            if not turns and not facts:
                return None
            selected = {id(t) for t in turns}
            data["turns"] = [t for t in data["turns"] if id(t) not in selected]
            if not owner:
                data["extracted_facts"] = []
            batch_id = uuid.uuid4().hex
            payload = {"user_id": owner, "turns": turns, "extracted_facts": facts}
            data["inflight"][batch_id] = payload
        payload.update(token=uuid.uuid4().hex, lease_until=now + lease_seconds)
        _save_raw(state_dir, channel_id, data)
        return BatchLease(int(channel_id), batch_id, payload["token"], owner,
                          payload["turns"], payload["extracted_facts"])


def _matches(data, lease):
    payload = data["inflight"].get(lease.batch_id)
    return payload if (payload and payload["token"] == lease.token and
                       payload["user_id"] == lease.user_id and
                       payload["lease_until"] > time.time()) else None


def renew_batch(state_dir, lease: BatchLease, *, lease_seconds: float = 300) -> bool:
    if not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    with _locked(state_dir, lease.channel_id):
        data = _load_raw(state_dir, lease.channel_id)
        payload = _matches(data, lease)
        if payload is None:
            return False
        payload["lease_until"] = time.time() + lease_seconds
        _save_raw(state_dir, lease.channel_id, data)
        return True


def ack_batch(state_dir, lease: BatchLease) -> bool:
    """Remove input only after successful ingestion by the current lease owner."""
    with _locked(state_dir, lease.channel_id):
        data = _load_raw(state_dir, lease.channel_id)
        payload = _matches(data, lease)
        if payload is None:
            return False
        for turn in payload["turns"]:
            key = _request_key(turn)
            if key and key not in data["completed"]:
                data["completed"].append(key)
        data["completed"] = data["completed"][-_COMPLETED_REQUESTS:]
        del data["inflight"][lease.batch_id]
        _save_raw(state_dir, lease.channel_id, data)
        return True


def retry_batch(state_dir, lease: BatchLease, error: str = "") -> bool:
    """Release a claim, retaining its stable batch ID and all source turns."""
    with _locked(state_dir, lease.channel_id):
        data = _load_raw(state_dir, lease.channel_id)
        payload = _matches(data, lease)
        if payload is None:
            return False
        payload.update(lease_until=0, last_error=str(error)[:300])
        _save_raw(state_dir, lease.channel_id, data)
        return True


def has_retry_batch(state_dir, channel_id, *, user_id: str) -> bool:
    with _locked(state_dir, channel_id):
        data = _load_raw(state_dir, channel_id)
        return any(p["user_id"] == str(user_id) and p["lease_until"] <= time.time()
                   for p in data["inflight"].values())


def has_ready_batch(state_dir, channel_id, *, user_id: str) -> bool:
    """A force-record request remains ready even while an older claim runs."""
    with _locked(state_dir, channel_id):
        data = _load_raw(state_dir, channel_id)
        turns = [t for t in data["turns"] if _owner(t) == str(user_id)]
        return (len(turns) >= max(1, settings.MEM0_BATCH_SIZE)
                or any(t.get("force_flush") for t in turns)
                or any(p["user_id"] == str(user_id) and p["lease_until"] <= time.time()
                       for p in data["inflight"].values()))


def batch_status(state_dir, channel_id) -> dict:
    """Content-free diagnostic counts, including unknown legacy ownership."""
    with _locked(state_dir, channel_id):
        data = _load_raw(state_dir, channel_id)
        return {"pending_turns": len(data["turns"]),
                "unowned_turns": sum(not _owner(t) for t in _all_turns(data)),
                "inflight_batches": len(data["inflight"]),
                "retry_batches": sum(p["lease_until"] <= time.time() for p in data["inflight"].values()),
                "unowned_facts": len(data["extracted_facts"])}


def pop_batch(state_dir, channel_id) -> BatchLease:
    """Compatibility lease of unowned input; call ack_batch after success.

    Tuple unpacking still works. Unlike a destructive pop, unacknowledged input
    remains recoverable after expiry. New callers should pass an explicit owner
    to claim_batch instead.
    """
    return claim_batch(state_dir, channel_id) or BatchLease(int(channel_id), "", "", "", [], [])
