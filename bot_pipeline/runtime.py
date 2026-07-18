"""Composition-root ownership for long-lived and per-turn resources."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any


async def _cancel_and_await(task: asyncio.Task | None) -> None:
    if task is None:
        return
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class RuntimeResources:
    """Own background tasks, browser state, and active live renderers."""

    def __init__(
        self,
        *,
        background_tasks: set[asyncio.Task],
        close_browser: Callable[[], Awaitable[None]],
        active_tasks: Callable[[], list[asyncio.Task]] | None = None,
        drain_timeout_s: float = 0,
        force_cancel_timeout_s: float = 30,
    ) -> None:
        self._background_tasks = background_tasks
        self._close_browser = close_browser
        self._active_tasks = active_tasks or (lambda: [])
        self._drain_timeout_s = max(0.0, float(drain_timeout_s))
        self._force_cancel_timeout_s = max(0.0, float(force_cancel_timeout_s))
        self._renderers: dict[int, Any] = {}
        self._jobs: dict[int, tuple[Any, Callable[[Any], None]]] = {}
        self._dispatch_tasks: set[asyncio.Task] = set()
        self._close_task: asyncio.Task | None = None
        self._accepting_jobs = True
        self._closing = False
        self._closed = False

    @property
    def accepting_jobs(self) -> bool:
        """Return whether a newly admitted Discord turn may be registered."""
        return self._accepting_jobs and not self._closing and not self._closed

    @property
    def active_job_count(self) -> int:
        """Return the current number of unfinished owned Discord job tasks."""
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        return sum(
            1
            for task in self._owned_job_tasks()
            if task is not current and not task.done()
        )

    def begin_drain(self) -> bool:
        """Atomically stop new job admission while existing jobs stay usable."""
        if not self._accepting_jobs:
            return False
        self._accepting_jobs = False
        return True

    def register_dispatch_task(self, task: asyncio.Task | None) -> bool:
        """Own an on_message dispatch from its first instruction until done."""
        if task is None or not self.accepting_jobs:
            return False
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)
        return True

    def _owned_job_tasks(self) -> list[asyncio.Task]:
        """Return registered dispatch/jobs regardless of their phase label."""
        tasks: list[asyncio.Task] = []
        seen: set[int] = set()
        candidates = [*self._dispatch_tasks, *self._active_tasks()]
        candidates.extend(
            getattr(job, "task", None) for job, _unregister in self._jobs.values()
        )
        for task in candidates:
            if not isinstance(task, asyncio.Task) or id(task) in seen:
                continue
            seen.add(id(task))
            tasks.append(task)
        return tasks

    def _ensure_open(self) -> None:
        if self._closing or self._closed:
            raise RuntimeError("runtime resources are closing")

    def register_task(self, task: asyncio.Task) -> asyncio.Task:
        """Register one owned task, rejecting producers after shutdown starts."""
        try:
            self._ensure_open()
        except RuntimeError:
            if not task.done():
                task.cancel()
            raise
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def create(self, coro: Any, *, name: str | None = None) -> asyncio.Task:
        """Create and register one task without a shutdown registration race."""
        try:
            self._ensure_open()
        except RuntimeError:
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            raise
        task = asyncio.create_task(coro, name=name)
        return self.register_task(task)

    def register_renderer(self, renderer: Any) -> None:
        self._ensure_open()
        self._renderers[id(renderer)] = renderer

    def register_job(
        self,
        job: Any,
        *,
        register: Callable[[Any], None],
        unregister: Callable[[Any], None],
    ) -> None:
        """Register an active JobState behind the common shutdown gate."""
        if not self.accepting_jobs:
            raise RuntimeError("runtime resources are closing and not accepting jobs")
        register(job)
        self._jobs[id(job)] = (job, unregister)

    def unregister_job(self, job: Any) -> None:
        owned = self._jobs.pop(id(job), None)
        if owned is not None:
            owned[1](owned[0])

    async def release_renderer(self, renderer: Any | None) -> None:
        if renderer is None:
            return
        self._renderers.pop(id(renderer), None)
        try:
            await _cancel_and_await(getattr(renderer, "tool_notice_task", None))
            notice_message = getattr(renderer, "tool_notice_msg", None)
            if notice_message is not None:
                try:
                    await notice_message.delete()
                except Exception:
                    pass
                clear_notice = getattr(renderer, "clear_tool_notice_msg", None)
                if clear_notice is not None:
                    clear_notice()
        finally:
            rendered_images = getattr(renderer, "rendered_images", None)
            if rendered_images is not None:
                for path in list(rendered_images):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                rendered_images.clear()
            message_ids = getattr(renderer, "rendered_image_message_ids", None)
            if message_ids is not None:
                message_ids.clear()

    async def _close(self) -> None:
        current = asyncio.current_task()
        try:
            # Stop admission first, but keep lower-level resources open while
            # already accepted Discord turns finish. Active turns may create
            # late renderer/background tasks during this grace period.
            deadline = asyncio.get_running_loop().time() + self._drain_timeout_s
            while True:
                active = [
                    task
                    for task in self._owned_job_tasks()
                    if task is not current and not task.done()
                ]
                if not active:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                _done, pending = await asyncio.wait(active, timeout=remaining)
                if pending:
                    break

            # The grace period is over. Fence every producer before forcing
            # any remaining turn down so cleanup cannot race a late resource.
            self._closing = True
            force_deadline = (
                asyncio.get_running_loop().time() + self._force_cancel_timeout_s
            )
            while True:
                active = {
                    task
                    for task in self._owned_job_tasks()
                    if task is not current and not task.done()
                }
                background = {
                    task
                    for task in self._background_tasks
                    if task is not current and not task.done()
                }
                owned = active | background
                if not owned:
                    break
                for task in owned:
                    task.cancel("shutdown")
                remaining = force_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                done, pending = await asyncio.wait(owned, timeout=remaining)
                if done:
                    await asyncio.gather(*done, return_exceptions=True)
                if pending:
                    # A task suppressing cancellation must not hold the entire
                    # service until systemd sends SIGKILL. No new producers can
                    # register after _closing was set above.
                    for task in pending:
                        task.cancel("shutdown")
                    break

            # Active dispatch tasks normally unregister in their turn-level
            # finally. Remove any completed/orphaned registrations before
            # lower-level resources are closed.
            while self._jobs:
                _job_id, (job, unregister) = self._jobs.popitem()
                unregister(job)

            while self._renderers:
                for renderer in list(self._renderers.values()):
                    await self.release_renderer(renderer)
            await self._close_browser()
        finally:
            self._closed = True

    async def close(self) -> None:
        """Drain accepted jobs, then close every owned resource exactly once."""
        if self._close_task is None:
            # This synchronous gate closes the admission race between close()
            # and the cleanup task's first tick. Existing jobs remain allowed
            # to create their owned resources until the drain deadline.
            self.begin_drain()
            self._close_task = asyncio.create_task(
                self._close(),
                name="bot-runtime-close",
            )
        first_cancel: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError as exc:
                if first_cancel is None:
                    first_cancel = exc
                # A caller can be cancelled repeatedly while browser cleanup
                # is in progress. The owned close task must still be drained.
                if self._close_task.done():
                    break
        # Retrieve and propagate a cleanup failure even after cancellation.
        self._close_task.result()
        if first_cancel is not None:
            raise first_cancel
