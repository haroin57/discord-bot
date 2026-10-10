from __future__ import annotations

import asyncio
import logging

import pytest

from services.query_expansion import QueryExpansionSingleFlight


async def wait_for_waiters(singleflight, expected):
    async def wait():
        while sum(flight.waiters for flight in singleflight._loops.get(asyncio.get_running_loop(), {}).values()) != expected:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 1)


def invocation(singleflight, factory, **overrides):
    arguments = {"user_id": "owner-a", "query": "synthetic exact query",
                 "configuration": {"model": "synthetic", "prompt": "version-one"},
                 "factory": factory}
    arguments.update(overrides)
    return singleflight.run(**arguments)


@pytest.mark.parametrize("consumers", [2, 4, 8])
async def test_concurrent_duplicates_share_only_one_execution(consumers):
    singleflight = QueryExpansionSingleFlight()
    release = asyncio.Event()
    calls = []
    original = (["rewritten"], "hypothetical", "keywords")

    async def factory():
        calls.append("called")
        await release.wait()
        return original

    tasks = [asyncio.create_task(invocation(singleflight, factory)) for _ in range(consumers)]
    try:
        await wait_for_waiters(singleflight, consumers)
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert len(calls) == 1
    assert results == [original] * consumers
    results[0][0].append("changed")
    assert results[1] == original
    assert original[0] == ["rewritten"]
    assert not singleflight._loops[asyncio.get_running_loop()]


@pytest.mark.parametrize("change", [
    {"user_id": "owner-b"},
    {"query": "synthetic exact query "},
    {"configuration": {"model": "other", "prompt": "version-one"}},
    {"configuration": {"model": "synthetic", "prompt": "version-two"}},
])
async def test_owner_exact_input_and_configuration_are_isolated(change):
    singleflight = QueryExpansionSingleFlight()
    release = asyncio.Event()
    calls = []

    async def factory():
        calls.append("called")
        await release.wait()
        return (["rewritten"], "hypothetical", "keywords")

    tasks = [asyncio.create_task(invocation(singleflight, factory)),
             asyncio.create_task(invocation(singleflight, factory, **change))]
    try:
        await wait_for_waiters(singleflight, 2)
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert len(calls) == 2


async def test_configuration_order_does_not_prevent_sharing():
    singleflight = QueryExpansionSingleFlight()
    release = asyncio.Event()
    calls = []

    async def factory():
        calls.append("called")
        await release.wait()
        return ([], "", "")

    tasks = [asyncio.create_task(invocation(singleflight, factory)), asyncio.create_task(invocation(
        singleflight, factory, configuration={"prompt": "version-one", "model": "synthetic"},
    ))]
    try:
        await wait_for_waiters(singleflight, 2)
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert len(calls) == 1


@pytest.mark.parametrize("result", [(["rewritten"], "hypothetical", "keywords"), ([], "", "")])
async def test_completed_or_failed_open_results_are_not_cached(result):
    singleflight = QueryExpansionSingleFlight()
    calls = []

    async def factory():
        calls.append("called")
        return result

    assert await invocation(singleflight, factory) == result
    assert await invocation(singleflight, factory) == result
    assert len(calls) == 2


async def test_cancelled_waiter_does_not_cancel_other_consumers():
    singleflight = QueryExpansionSingleFlight()
    release = asyncio.Event()
    drained = asyncio.Event()

    async def factory():
        try:
            await release.wait()
            return (["rewritten"], "hypothetical", "keywords")
        finally:
            drained.set()

    cancelled = asyncio.create_task(invocation(singleflight, factory))
    surviving = asyncio.create_task(invocation(singleflight, factory))
    await wait_for_waiters(singleflight, 2)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not drained.is_set()
    assert not surviving.done()
    release.set()
    assert await surviving == (["rewritten"], "hypothetical", "keywords")
    assert drained.is_set()


async def test_last_cancelled_waiter_cancels_and_drains_provider_work():
    singleflight = QueryExpansionSingleFlight()
    started = asyncio.Event()
    drained = asyncio.Event()

    async def factory():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    task = asyncio.create_task(invocation(singleflight, factory))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained.is_set()
    assert not singleflight._loops[asyncio.get_running_loop()]


async def test_provider_exception_is_shared_then_removed():
    singleflight = QueryExpansionSingleFlight()
    release = asyncio.Event()
    calls = []

    async def failing():
        calls.append("called")
        await release.wait()
        raise ValueError("synthetic provider failure")

    tasks = [asyncio.create_task(invocation(singleflight, failing)) for _ in range(2)]
    try:
        await wait_for_waiters(singleflight, 2)
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert len(calls) == 1
    assert all(isinstance(result, ValueError) for result in results)
    assert not singleflight._loops[asyncio.get_running_loop()]

    async def recovered():
        return (["recovered"], "", "")

    assert await invocation(singleflight, recovered) == (["recovered"], "", "")


@pytest.mark.parametrize("bounds,overrides", [
    ({"max_inflight": 1}, {"query": "other"}),
    ({"max_waiters": 1}, {}),
    ({"max_query_bytes": 4}, {"query": "青葉"}),
    ({}, {"user_id": ""}),
    ({}, {"configuration": {"not_json": object()}}),
])
async def test_capacity_and_invalid_key_bounds_fall_back_without_blocking(bounds, overrides):
    singleflight = QueryExpansionSingleFlight(**bounds)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def pending():
        calls.append("pending")
        started.set()
        await release.wait()
        return ([], "", "")

    task = asyncio.create_task(invocation(singleflight, pending, query="q"))
    await asyncio.wait_for(started.wait(), 1)

    async def immediate():
        calls.append("immediate")
        return (["fallback"], "", "")

    arguments = {"query": "q", **overrides}
    try:
        assert await asyncio.wait_for(invocation(singleflight, immediate, **arguments), 1) == (["fallback"], "", "")
    finally:
        release.set()
        await task
    assert calls == ["pending", "immediate"]


@pytest.mark.parametrize("name", ["max_inflight", "max_waiters", "max_query_bytes"])
def test_nonpositive_bounds_are_rejected(name):
    with pytest.raises(ValueError):
        QueryExpansionSingleFlight(**{name: 0})


def test_completed_event_loops_do_not_share_tasks():
    singleflight = QueryExpansionSingleFlight()
    calls = []

    async def factory():
        calls.append(asyncio.get_running_loop())
        return (["fresh"], "", "")

    assert asyncio.run(invocation(singleflight, factory)) == (["fresh"], "", "")
    assert asyncio.run(invocation(singleflight, factory)) == (["fresh"], "", "")
    assert len(calls) == 2
    assert calls[0] is not calls[1]


async def test_observations_do_not_log_query_owner_or_configuration(caplog):
    singleflight = QueryExpansionSingleFlight()

    async def factory():
        return ([], "", "")

    with caplog.at_level(logging.INFO, logger="services.query_expansion"):
        await invocation(singleflight, factory, user_id="private-owner",
                         query="private-query", configuration={"prompt": "private-prompt"})
    assert "mem0_query_singleflight outcome=leader" in caplog.text
    assert "private-owner" not in caplog.text
    assert "private-query" not in caplog.text
    assert "private-prompt" not in caplog.text


@pytest.mark.parametrize("enabled", [False, True])
async def test_legacy_search_only_shares_llm_stages(monkeypatch, enabled):
    from functools import partial
    from services.mem0 import search

    monkeypatch.setenv("MEM0_QUERY_SINGLEFLIGHT_ENABLED", str(int(enabled)))
    monkeypatch.setattr(search, "_QUERY_SINGLEFLIGHT", QueryExpansionSingleFlight())
    release = asyncio.Event()
    started = asyncio.Event()
    helpers = []
    graph_reads = []

    async def graph(query, user_id):
        graph_reads.append((query, user_id))
        return [f"fresh alias {len(graph_reads)}"]

    async def helper(name, result, query):
        helpers.append((name, query))
        if len(helpers) == (3 if enabled else 6):
            started.set()
        await release.wait()
        return result

    monkeypatch.setattr(search, "_expand_query_via_graph", graph)
    monkeypatch.setattr(search, "_expand_query_via_morpho", lambda query: "morphological")
    for name, result in (("_rewrite_query", ["rewritten"]),
                         ("_generate_hypothetical_fact", "hypothetical"),
                         ("_extract_keywords_via_llm", "keywords")):
        monkeypatch.setattr(search, name, partial(helper, name, result))
    tasks = [asyncio.create_task(search._expand_queries("exact query", "owner", rewrite=True)) for _ in range(2)]
    try:
        await asyncio.wait_for(started.wait(), 1)
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert len(helpers) == (3 if enabled else 6)
    assert graph_reads == [("exact query", "owner")] * 2
    assert [result[1] for result in results] == ["fresh alias 1", "fresh alias 2"]
    assert [result[-3:] for result in results] == [["rewritten", "hypothetical", "keywords"]] * 2
