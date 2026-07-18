"""Tests for services/job_state.py (P0 job lifecycle / queue tracking)."""

import asyncio

import pytest

from services.job_state import (
    JobPhase,
    JobState,
    cancel_channel_jobs,
    live_jobs,
    queue_position,
    register_job,
    unregister_job,
)

CH = 777


@pytest.fixture(autouse=True)
def _clean():
    import services.job_state as js
    js._jobs.clear()
    yield
    js._jobs.clear()


def _job(rid, channel=CH, phase=JobPhase.QUEUED):
    j = JobState(rid=rid, channel_id=channel, user_id=1)
    j.phase = phase
    return j


def test_queue_position_counts_live_predecessors():
    a, b, c = _job("a"), _job("b"), _job("c")
    register_job(a)
    register_job(b)
    register_job(c)
    assert queue_position(a) == 1
    assert queue_position(b) == 2
    assert queue_position(c) == 3


def test_done_predecessors_do_not_count():
    a, b = _job("a", phase=JobPhase.DONE), _job("b")
    register_job(a)
    register_job(b)
    # a finished -> b is effectively first in line
    assert queue_position(b) == 1


def test_unregister_removes_and_cleans_channel():
    import services.job_state as js
    a = _job("a")
    register_job(a)
    unregister_job(a)
    assert CH not in js._jobs


def test_live_jobs_excludes_terminal():
    a, b = _job("a", phase=JobPhase.TOOL_RUNNING), _job("b", phase=JobPhase.FAILED)
    register_job(a)
    register_job(b)
    live = live_jobs(CH)
    assert a in live and b not in live


@pytest.mark.asyncio
async def test_cancel_channel_jobs_cancels_live_tasks():
    reasons = []

    async def _sleep():
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError as exc:
            reasons.append(exc.args)
            raise

    t1 = asyncio.create_task(_sleep())
    t2 = asyncio.create_task(_sleep())
    await asyncio.sleep(0)  # let them start

    a = _job("a")
    a.task = t1
    b = _job("b", phase=JobPhase.CALLING_CLAUDE)
    b.task = t2
    done = _job("c", phase=JobPhase.DONE)
    done.task = asyncio.create_task(_sleep())
    register_job(a)
    register_job(b)
    register_job(done)

    n = cancel_channel_jobs(CH)
    assert n == 2  # only the two live jobs
    await asyncio.sleep(0)
    assert t1.cancelled() and t2.cancelled()
    assert reasons == [("user abort",), ("user abort",)]
    # the DONE job's task was left alone
    done.task.cancel()  # cleanup


def test_cancel_with_no_jobs_returns_zero():
    assert cancel_channel_jobs(999999) == 0
