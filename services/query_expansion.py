"""Share concurrent, owner-scoped query expansion without retaining results."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import json
import logging
import weakref


log = logging.getLogger(__name__)
QueryRepresentations = tuple[list[str], str, str]


@dataclass
class _Flight:
    task: asyncio.Task[QueryRepresentations]
    waiters: int = 0


class QueryExpansionSingleFlight:
    def __init__(self, *, max_inflight: int = 64, max_waiters: int = 16,
                 max_query_bytes: int = 16_000):
        if min(max_inflight, max_waiters, max_query_bytes) < 1:
            raise ValueError("single-flight bounds must be positive")
        self._max_inflight = max_inflight
        self._max_waiters = max_waiters
        self._max_query_bytes = max_query_bytes
        self._loops: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, _Flight]
        ] = weakref.WeakKeyDictionary()

    async def run(self, *, user_id: str, query: str, configuration: dict,
                  factory: Callable[[], Awaitable[QueryRepresentations]]) -> QueryRepresentations:
        if (not user_id or len(query) > self._max_query_bytes
                or len(query.encode("utf-8")) > self._max_query_bytes):
            log.info("mem0_query_singleflight outcome=bypass reason=owner_or_query_bounds")
            return await factory()
        try:
            serialized = json.dumps(
                [user_id, query, configuration], sort_keys=True, ensure_ascii=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            log.info("mem0_query_singleflight outcome=bypass reason=configuration")
            return await factory()
        key = hashlib.sha256(serialized).hexdigest()
        loop = asyncio.get_running_loop()
        flights = self._loops.setdefault(loop, {})
        flight = flights.get(key)
        if flight is not None and flight.task.done():
            flights.pop(key)
            flight = None
        if flight is None:
            if len(flights) >= self._max_inflight:
                log.info("mem0_query_singleflight outcome=bypass reason=inflight_limit")
                return await factory()

            async def invoke() -> QueryRepresentations:
                return await factory()

            flight = _Flight(asyncio.create_task(invoke()))
            flights[key] = flight

            def finished(task):
                if flights.get(key) is flight:
                    flights.pop(key)

            flight.task.add_done_callback(finished)
            outcome = "leader"
        else:
            if flight.waiters >= self._max_waiters:
                log.info("mem0_query_singleflight outcome=bypass reason=waiter_limit")
                return await factory()
            outcome = "joined"
        flight.waiters += 1
        log.info("mem0_query_singleflight outcome=%s active=%d waiters=%d",
                 outcome, len(flights), flight.waiters)
        try:
            variants, hypothetical, keywords = await asyncio.shield(flight.task)
            return list(variants), hypothetical, keywords
        finally:
            flight.waiters -= 1
            if not flight.waiters:
                if flights.get(key) is flight:
                    flights.pop(key)
                if not flight.task.done():
                    flight.task.cancel()
                    await asyncio.gather(flight.task, return_exceptions=True)
