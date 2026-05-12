import asyncio
import contextvars

import pytest

from services.correlation import get_request_id, new_request_id, set_request_id


def test_new_request_id_returns_hex_string():
    rid = new_request_id()
    assert isinstance(rid, str)
    assert len(rid) == 8
    int(rid, 16)  # raises ValueError if not hex


def test_get_returns_dash_when_unset():
    # Run in a fresh context so prior tests don't bleed in
    ctx = contextvars.copy_context()

    def _check():
        from services import correlation  # noqa: PLC0415
        correlation._REQUEST_ID.set("-")  # reset to default
        assert get_request_id() == "-"

    ctx.run(_check)


def test_request_id_propagates_through_async_call():
    async def _inner():
        return get_request_id()

    async def _run():
        rid = new_request_id()
        propagated = await _inner()
        assert propagated == rid

    asyncio.run(_run())


def test_request_ids_isolated_between_concurrent_tasks():
    results: dict[str, str] = {}

    async def task_a():
        set_request_id("aaaaaaaa")
        await asyncio.sleep(0)  # yield to let task_b run
        results["a"] = get_request_id()

    async def task_b():
        set_request_id("bbbbbbbb")
        await asyncio.sleep(0)
        results["b"] = get_request_id()

    async def _run():
        # Each asyncio.Task gets its own copy of the context
        await asyncio.gather(
            asyncio.ensure_future(task_a()),
            asyncio.ensure_future(task_b()),
        )

    asyncio.run(_run())
    assert results["a"] == "aaaaaaaa"
    assert results["b"] == "bbbbbbbb"
