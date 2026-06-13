"""Per-message job lifecycle tracking (P0 UX overhaul).

A JobState is registered the moment a message passes the dispatch filters —
BEFORE the channel semaphore is acquired — so queued work is visible to the
user ("⏳ 待機中(2番目)") and /abort can cancel queued jobs, not just the
one currently running. bot.py owns the phase transitions; this module only
holds the registry and the queries on it.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum


class JobPhase(str, Enum):
    QUEUED = "queued"
    PREPROCESSING = "preprocessing"
    RECALLING = "recalling"
    CALLING_CLAUDE = "calling_claude"
    TOOL_RUNNING = "tool_running"
    FINALIZING = "finalizing"
    DONE = "done"
    FAILED = "failed"


_LIVE_PHASES = frozenset(
    p for p in JobPhase if p not in (JobPhase.DONE, JobPhase.FAILED)
)


@dataclass
class JobState:
    rid: str
    channel_id: int
    user_id: int
    task: asyncio.Task | None = None
    phase: JobPhase = JobPhase.QUEUED
    created_at: float = field(default_factory=time.monotonic)


# channel_id -> rid -> JobState, in registration (FIFO) order.
_jobs: dict[int, OrderedDict[str, JobState]] = {}


def register_job(job: JobState) -> None:
    _jobs.setdefault(job.channel_id, OrderedDict())[job.rid] = job


def unregister_job(job: JobState) -> None:
    chan = _jobs.get(job.channel_id)
    if chan is not None:
        chan.pop(job.rid, None)
        if not chan:
            _jobs.pop(job.channel_id, None)


def queue_position(job: JobState) -> int:
    """1-based queue position: 1 means running now / next in line."""
    chan = _jobs.get(job.channel_id)
    if chan is None:
        return 1
    pos = 1
    for rid, other in chan.items():
        if rid == job.rid:
            break
        if other.phase in _LIVE_PHASES:
            pos += 1
    return pos


def live_jobs(channel_id: int) -> list[JobState]:
    """Live (queued or running) jobs in the channel, FIFO order."""
    chan = _jobs.get(channel_id)
    if not chan:
        return []
    return [j for j in chan.values() if j.phase in _LIVE_PHASES]


def cancel_channel_jobs(channel_id: int) -> int:
    """Cancel every live job (queued AND running) in the channel.

    Returns the number of tasks actually cancelled.
    """
    cancelled = 0
    for job in live_jobs(channel_id):
        if job.task is not None and not job.task.done():
            job.task.cancel()
            cancelled += 1
    return cancelled
