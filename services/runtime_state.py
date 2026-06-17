"""Cross-restart persistence of in-memory runtime state (機能2, 2026-06-17).

A bot restart used to wipe every in-memory ledger: session freshness, the
Opus sticky-model pin, per-user cooldowns, and daily quota. This module is a
thin façade: each owning module exposes ``dump_state()`` / ``load_state(data)``
and registers here implicitly via ``_providers()``. We snapshot them to
``.state/runtime_state.json`` on a timer and at graceful shutdown, and restore
on startup.

Deliberately NOT persisted:
  - claude_sdk._retry_budget — a 60-second *monotonic* window. monotonic resets
    on restart (so it can't be serialized meaningfully), and any restart takes
    long enough that a 60s budget would have expired anyway. Persisting it is
    pure noise, so it's left to reset.

Concurrency note: ``dump_state()`` of the async-owned session store iterates its
dicts. We therefore COLLECT on the event-loop thread (a plain function with no
``await`` is atomic w.r.t. other coroutines) and only push the file write to a
worker thread — never iterate those dicts off-thread, which would race the loop
mutating them ("dict changed size during iteration").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from config import RUNTIME_STATE_PATH
from utils.atomic_write import atomic_write_json

log = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 60


def _providers() -> dict:
    """name -> (dump_fn, load_fn). Lazy imports avoid an import cycle."""
    from services import rate_limit
    from services.claude_api import session_store

    return {
        "rate_limit": (rate_limit.dump_state, rate_limit.load_state),
        "session": (session_store.dump_state, session_store.load_state),
    }


def _collect() -> dict:
    """Snapshot every provider. MUST run on the event-loop thread (see module
    docstring) — it touches dicts the loop mutates, but holds no await so no
    other coroutine interleaves."""
    out: dict = {}
    for name, (dump, _load) in _providers().items():
        try:
            out[name] = dump()
        except Exception as exc:  # one bad provider must not lose the rest
            log.warning("runtime_state dump %s failed: %s", name, exc)
    return out


def save_all_sync() -> None:
    """Collect + write synchronously (atexit / tests / non-loop callers)."""
    try:
        atomic_write_json(RUNTIME_STATE_PATH, _collect())
    except Exception as exc:
        log.warning("runtime_state save failed: %s", exc)


def load_all() -> None:
    """Restore all provider state from disk. No-op when the file is absent."""
    try:
        with open(RUNTIME_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    except OSError as exc:
        log.warning("runtime_state load failed: %s", exc)
        return
    if not isinstance(data, dict):
        return
    restored = []
    for name, (_dump, load) in _providers().items():
        if name in data:
            try:
                load(data[name])
                restored.append(name)
            except Exception as exc:
                log.warning("runtime_state load %s failed: %s", name, exc)
    if restored:
        log.info("runtime_state restored: %s", ", ".join(restored))


async def periodic_flush(stop: asyncio.Event, interval: float = FLUSH_INTERVAL_S) -> None:
    """Flush every ``interval`` seconds until ``stop`` is set, then flush once
    more (the shutdown flush). Collect on-loop, write off-thread."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass  # periodic tick
        data = _collect()
        try:
            await asyncio.to_thread(atomic_write_json, RUNTIME_STATE_PATH, data)
        except Exception as exc:
            log.warning("runtime_state flush failed: %s", exc)
    log.info("runtime_state final flush complete")
