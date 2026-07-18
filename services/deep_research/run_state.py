"""Deep Research の run 単位ライフサイクルと待機タスク管理。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar
from uuid import uuid4

from services.claude_api.subprocess_runner import (
    get_process_group_fence,
    process_group_cleanup_complete,
    wait_or_terminate_process_group,
)

from .types import ResearchState


T = TypeVar("T")
_STACK_CLEANUP_MAX_ATTEMPTS = 3
_STACK_CLEANUP_BACKOFF_S = 0.05


class OwnedMcpCleanupError(RuntimeError):
    """Run-owned MCP resources remained after bounded cleanup retries."""


def _last_error_leaf(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[-1]
    return error


async def _await_despite_repeated_cancel(task: asyncio.Task[T]) -> T:
    """Keep a strongly referenced owned task alive through repeated cancel."""
    cancellations: list[asyncio.CancelledError] = []
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.cancelled():
                break
            cancellations.append(exc)
        except BaseException:
            if task.done():
                break
            raise

    try:
        result = task.result()
    except BaseException as cleanup_error:
        if cancellations:
            raise cancellations[0] from cleanup_error
        raise
    if cancellations:
        raise cancellations[0]
    return result


@dataclass
class ResearchRun:
    """1 回の ``/research`` に閉じた可変状態。

    承認 deadline と Discord 待機タスクを run に所属させる。プロセス全体を占有する
    busy flag は持たず、終了・中断時にこの run が所有するタスクだけを回収する。
    """

    state: ResearchState
    run_id: str = field(default_factory=lambda: uuid4().hex)
    approval_deadline: float | None = None
    _tasks: set[asyncio.Task] = field(default_factory=set, repr=False)
    _stack_cleanup_task: asyncio.Task | None = field(default=None, repr=False)
    _stack_cleanup_done: bool = field(default=False, repr=False)
    _stack_cleanup_required: bool = field(default=False, repr=False)
    _stack_cleanup_diagnostic: str | None = field(default=None, repr=False)
    _stack_cleanup_generation: int = field(default=0, repr=False)
    _stack_cleanup_attempt: int = field(default=0, repr=False)
    _stack_cleanup_last_error: str | None = field(default=None, repr=False)
    _stack_closed: bool = field(default=False, repr=False)
    _owned_mcp_processes: list[asyncio.subprocess.Process] = field(
        default_factory=list, repr=False
    )
    _task_cleanup_task: asyncio.Task | None = field(default=None, repr=False)

    def begin_approval(self, timeout_s: float) -> float:
        timeout = max(0.0, float(timeout_s))
        self.approval_deadline = time.monotonic() + timeout
        return timeout

    def approval_time_left(self) -> float | None:
        if self.approval_deadline is None:
            return None
        return max(0.0, self.approval_deadline - time.monotonic())

    def track_task(self, task: asyncio.Task) -> asyncio.Task:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def require_mcp_cleanup(self) -> None:
        self._stack_cleanup_required = True

    @property
    def cleanup_discardable(self) -> bool:
        return not self._stack_cleanup_required or self._stack_cleanup_done

    @property
    def cleanup_diagnostic(self) -> str | None:
        return self._stack_cleanup_diagnostic

    def register_mcp_process(self, process: asyncio.subprocess.Process) -> None:
        """Observer callback retaining the exact Process returned by spawn."""
        if self._stack_cleanup_task is not None or self._stack_cleanup_done:
            raise RuntimeError("MCP process registered after cleanup started")
        if not isinstance(process, asyncio.subprocess.Process):
            raise RuntimeError("MCP observer received a non-asyncio Process handle")
        fence = get_process_group_fence(process)
        if fence is None:
            raise RuntimeError("MCP process has no spawn-time ownership fence")
        if fence.pgid is not None and (
            fence.pgid != fence.pid
            or fence.session_id != fence.pid
            or fence.generation is None
        ):
            raise RuntimeError("MCP process has no reusable generation/session fence")
        if process.returncode is not None:
            raise RuntimeError("MCP process exited before run ownership registration")
        if not any(owned is process for owned in self._owned_mcp_processes):
            self._owned_mcp_processes.append(process)
            self._stack_cleanup_required = True

    async def _cleanup_owned_processes(self) -> None:
        errors: list[BaseException] = []
        for process in self._owned_mcp_processes:
            try:
                if not process_group_cleanup_complete(process):
                    await wait_or_terminate_process_group(process)
                # Explicitly retrieve the leader result even when the child
                # watcher already populated returncode before this generation.
                await process.wait()
                if not process_group_cleanup_complete(process):
                    raise RuntimeError(
                        f"process group for MCP pid {process.pid} remains live"
                    )
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("run-owned MCP process cleanup failed", errors)

    async def _close_stack_and_processes(
        self,
        cleanup_factory: Callable[[], Awaitable[None]],
        generation: int,
    ) -> None:
        errors: list[BaseException] = []
        for attempt in range(1, _STACK_CLEANUP_MAX_ATTEMPTS + 1):
            self._stack_cleanup_attempt = attempt
            owned_cleanup_completed = False
            if not self._stack_closed:
                try:
                    await cleanup_factory()
                    self._stack_closed = True
                except BaseException as exc:
                    errors.append(exc)
            try:
                await self._cleanup_owned_processes()
                owned_cleanup_completed = True
            except BaseException as exc:
                errors.append(exc)

            handles_clean = True
            try:
                handles_clean = all(
                    process_group_cleanup_complete(process)
                    for process in self._owned_mcp_processes
                )
            except BaseException as exc:
                errors.append(exc)
                handles_clean = False
            if self._stack_closed and owned_cleanup_completed and handles_clean:
                self._stack_cleanup_done = True
                self._stack_cleanup_diagnostic = None
                self._stack_cleanup_last_error = None
                return
            if attempt < _STACK_CLEANUP_MAX_ATTEMPTS:
                await asyncio.sleep(_STACK_CLEANUP_BACKOFF_S * attempt)

        live_pids: list[int] = []
        for process in self._owned_mcp_processes:
            try:
                if not process_group_cleanup_complete(process):
                    live_pids.append(process.pid)
            except BaseException:
                live_pids.append(process.pid)
        last_error = (
            _last_error_leaf(errors[-1])
            if errors
            else RuntimeError("owned process groups remain live")
        )
        self._stack_cleanup_last_error = f"{type(last_error).__name__}: {last_error}"
        self._stack_cleanup_diagnostic = (
            f"run={self.run_id} MCP cleanup exhausted "
            f"generation={generation} attempt={self._stack_cleanup_attempt} "
            f"stack_closed={self._stack_closed} live_pids={live_pids} "
            f"last_error={self._stack_cleanup_last_error}"
        )
        raise OwnedMcpCleanupError(self._stack_cleanup_diagnostic) from last_error

    async def close_owned_stack(
        self,
        cleanup_factory: Callable[[], Awaitable[None]],
    ) -> None:
        """Close stack and exact handles in one cancellation-resistant task."""
        if self._stack_cleanup_done:
            return
        cleanup_task = self._stack_cleanup_task
        if cleanup_task is None or cleanup_task.done():
            if cleanup_task is not None:
                # Retrieve a completed generation before replacing the strong
                # reference. Its original waiter still receives the same result.
                try:
                    cleanup_task.exception()
                except asyncio.CancelledError:
                    pass
            self._stack_cleanup_generation += 1
            generation = self._stack_cleanup_generation
            cleanup_task = asyncio.create_task(
                self._close_stack_and_processes(cleanup_factory, generation),
                name=(
                    f"deep-research-stack-cleanup:{self.run_id}:generation-{generation}"
                ),
            )
            self._stack_cleanup_task = cleanup_task
        await _await_despite_repeated_cancel(cleanup_task)

    async def _cleanup_tasks(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self.approval_deadline = None

    async def cleanup(self) -> None:
        """この run が所有する未完了タスクだけを cancel して例外まで回収する。"""
        if self._task_cleanup_task is None:
            self._task_cleanup_task = asyncio.create_task(
                self._cleanup_tasks(),
                name=f"deep-research-task-cleanup:{self.run_id}",
            )
        await _await_despite_repeated_cancel(self._task_cleanup_task)


class RunRegistry:
    """並行 run の索引。直列化はせず、active thread の参照数だけを管理する。"""

    def __init__(self) -> None:
        self.runs: dict[str, ResearchRun] = {}
        self.active_threads: set[int] = set()
        self._thread_refs: dict[int, int] = {}

    def create(self, query: str, *, run_id: str | None = None) -> ResearchRun:
        candidate = run_id or uuid4().hex
        while candidate in self.runs:
            candidate = uuid4().hex
        run = ResearchRun(
            state=ResearchState(query=query, clarified_query=query),
            run_id=candidate,
        )
        self.runs[run.run_id] = run
        return run

    def discard(self, run: ResearchRun) -> bool:
        """登録中の同一 object だけを破棄する。"""
        if self.runs.get(run.run_id) is not run:
            return False
        if not run.cleanup_discardable:
            return False
        del self.runs[run.run_id]
        return True

    def mark_thread(self, thread_id: int) -> None:
        self._thread_refs[thread_id] = self._thread_refs.get(thread_id, 0) + 1
        self.active_threads.add(thread_id)

    def unmark_thread(self, thread_id: int) -> None:
        refs = self._thread_refs.get(thread_id, 0) - 1
        if refs > 0:
            self._thread_refs[thread_id] = refs
            return
        self._thread_refs.pop(thread_id, None)
        self.active_threads.discard(thread_id)

    def is_active_thread(self, thread_id: int) -> bool:
        return thread_id in self.active_threads


DEFAULT_RUNS = RunRegistry()
