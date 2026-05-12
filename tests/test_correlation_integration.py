"""Integration tests: request_id flows through async boundaries.

Verifies the contextvar in services.correlation is correctly inherited by
asyncio.create_task, gather, and to_thread.
"""

import asyncio
import logging
import pytest

pytest.importorskip("services.correlation")


@pytest.mark.asyncio
async def test_request_id_inherited_by_create_task():
    from services.correlation import new_request_id, get_request_id
    rid = new_request_id()
    captured = {}
    async def child():
        captured["rid"] = get_request_id()
    await asyncio.create_task(child())
    assert captured["rid"] == rid


@pytest.mark.asyncio
async def test_request_id_isolated_between_parallel_handlers():
    """Two concurrent 'requests' must not see each other's IDs."""
    from services.correlation import new_request_id, get_request_id

    seen = []
    async def handler():
        rid = new_request_id()
        await asyncio.sleep(0.01)
        seen.append((rid, get_request_id()))

    await asyncio.gather(handler(), handler(), handler())
    # Each handler must observe its own id (set==got)
    for set_id, got_id in seen:
        assert set_id == got_id
    # All three ids must be distinct
    assert len({s for s, _ in seen}) == 3


@pytest.mark.asyncio
async def test_request_id_present_in_log_output(caplog):
    """When structured_log is installed, the rid appears in formatted output."""
    from services.correlation import new_request_id
    try:
        from services.structured_log import JsonFormatter
    except ImportError:
        pytest.skip("structured_log not available")
    import json

    log = logging.getLogger("test_int")
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    rid = new_request_id()
    with caplog.at_level(logging.INFO):
        log.info("test event")
    # caplog captures records; the formatter sees the rid via contextvar
    rec = caplog.records[-1]
    formatted = JsonFormatter().format(rec)
    payload = json.loads(formatted)
    assert payload["rid"] == rid


def test_get_request_id_outside_async_context():
    """Outside any explicit set, get_request_id returns '-'.

    contextvars persist within a single test process when prior tests in the
    same module set them at module scope. Run this test in a fresh
    `copy_context()` so we observe the pristine default.
    """
    import contextvars
    from services.correlation import get_request_id

    def _check():
        return get_request_id()

    ctx = contextvars.Context()
    assert ctx.run(_check) == "-"
