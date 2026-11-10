"""Task-affine MCP ownership for concurrently running research assistants.

Each server has an actor that opens, calls and closes its own MCP session.
Researchers share a facade rather than AnyIO context managers. Different
servers remain concurrent while calls to one server remain serialized.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import logging

from services.claude_api.mcp_client import McpPool, _parse_qualified_tool_name

log = logging.getLogger(__name__)


class ResearchMcpPool:
    def __init__(self, *, stack, available_configs, process_observer=None, timeout_s=160):
        from services.mcp_control import filter_enabled_mcp_configs
        self.available_configs = filter_enabled_mcp_configs(available_configs)
        self.process_observer = process_observer
        self.timeout_s = timeout_s
        self.handles = {}
        self._actors = {}
        self._closed = False
        stack.push_async_callback(self.aclose)

    async def _actor(self, name, queue):
        try:
            async with AsyncExitStack() as stack:
                pool = McpPool(stack=stack, available_configs={name: self.available_configs[name]},
                               process_observer=self.process_observer)
                while True:
                    try:
                        method, tool, arguments, future = await queue.get()
                    except asyncio.CancelledError:
                        break
                    if future.done():
                        continue
                    try:
                        # Startup owns persistent AnyIO scopes and therefore
                        # stays outside the individual call's timeout scope.
                        handle = await pool.ensure_spawned(name)
                        self.handles[name] = handle
                        if method == 'start':
                            result = handle
                        else:
                            async with asyncio.timeout(self.timeout_s):
                                result = await pool.call_tool(tool, arguments)
                        if not future.done():
                            future.set_result(result)
                    except asyncio.CancelledError:
                        if not future.done():
                            future.cancel()
                        # Consume shutdown cancellation before unwinding the
                        # actor's nested ClientSession/transport contexts.
                        break
                    except Exception as exc:
                        if not future.done():
                            future.set_exception(exc)
        finally:
            while not queue.empty():
                *_, future = queue.get_nowait()
                if not future.done():
                    future.cancel()

    async def _submit(self, name, method, tool='', arguments=None):
        if self._closed:
            raise RuntimeError('Research MCP pool is closed')
        if name not in self.available_configs:
            raise KeyError(f'Research MCP server is not authorized: {name}')
        if name not in self._actors:
            queue = asyncio.Queue(maxsize=32)
            task = asyncio.create_task(self._actor(name, queue), name=f'research-mcp-{name}')
            self._actors[name] = (queue, task)
        queue, task = self._actors[name]
        if task.done():
            raise RuntimeError(f'Research MCP server actor stopped: {name}')
        future = asyncio.get_running_loop().create_future()
        try:
            await queue.put((method, tool, arguments or {}, future))
            return await future
        finally:
            if not future.done():
                future.cancel()

    async def ensure_spawned(self, name):
        return await self._submit(name, 'start')

    async def spawn_eager(self, names):
        for name in names:
            try:
                await self.ensure_spawned(name)
            except Exception as exc:
                log.warning('Research MCP warmup failed for %s: %s', name, type(exc).__name__)

    async def call_tool(self, qualified_name, args):
        name, _ = _parse_qualified_tool_name(qualified_name)
        return await self._submit(name, 'call', qualified_name, args)

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        tasks = [task for _, task in self._actors.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first scheduling step never enters its
        # finally block. Resolve requests queued before that actor started.
        for queue, _ in self._actors.values():
            while not queue.empty():
                *_, future = queue.get_nowait()
                if not future.done():
                    future.cancel()
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise RuntimeError('Research MCP actor cleanup failed') from result
