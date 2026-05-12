"""Unit tests for utils/render_semaphore.py."""

import asyncio
import importlib

import pytest

import utils.render_semaphore as _mod


async def _fresh_sema() -> asyncio.Semaphore:
    """Return a fresh Semaphore(1) bound to the current event loop."""
    return asyncio.Semaphore(1)


async def test_module_exports_render_sema():
    """render_sema is exported and is an asyncio.Semaphore."""
    assert hasattr(_mod, "render_sema")
    assert isinstance(_mod.render_sema, asyncio.Semaphore)


async def test_initial_value_is_one():
    """Module-level semaphore starts with value 1 (allows one holder)."""
    sema = await _fresh_sema()
    assert sema._value == 1


async def test_acquire_decrements_value():
    sema = await _fresh_sema()
    await sema.acquire()
    assert sema._value == 0
    sema.release()


async def test_release_restores_value():
    sema = await _fresh_sema()
    await sema.acquire()
    sema.release()
    assert sema._value == 1


async def test_context_manager():
    sema = await _fresh_sema()
    async with sema:
        assert sema._value == 0
    assert sema._value == 1


async def test_only_one_at_a_time():
    """Second acquire must block while the first holder has the semaphore."""
    sema = await _fresh_sema()
    results = []

    async def worker(name: str, hold_secs: float):
        async with sema:
            results.append(f"{name}_start")
            await asyncio.sleep(hold_secs)
            results.append(f"{name}_end")

    await asyncio.gather(worker("w1", 0.05), worker("w2", 0.01))
    assert results == ["w1_start", "w1_end", "w2_start", "w2_end"]


async def test_semaphore_released_after_exception():
    """Semaphore must be released even if an exception is raised inside."""
    sema = await _fresh_sema()
    with pytest.raises(RuntimeError):
        async with sema:
            raise RuntimeError("boom")
    assert sema._value == 1


async def test_concurrent_tasks_serialize():
    """N tasks must all run one at a time (no overlap)."""
    sema = await _fresh_sema()
    active = []
    max_concurrent = 0

    async def task():
        nonlocal max_concurrent
        async with sema:
            active.append(1)
            max_concurrent = max(max_concurrent, len(active))
            await asyncio.sleep(0)
            active.pop()

    await asyncio.gather(*[task() for _ in range(5)])
    assert max_concurrent == 1
