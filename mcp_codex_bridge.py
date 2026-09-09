"""Native stdio MCP bridge for the bot's per-user authorized tool catalog.

The actor owns all AnyIO sessions in one task, including their teardown.
Tool schemas are lazy; merely starting a Codex thread does not spawn Chromium.
"""
from __future__ import annotations
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import stat
import sys


def load_config(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('MCP bridge configuration must be a private owned file')
        payload = json.load(stream)
    if payload.get('version') != 1 or not isinstance(payload.get('servers'), dict):
        raise ValueError('Invalid MCP bridge configuration')
    return payload


async def run_actor(queue, configs, *, timeout_s=160):
    """Keep transport entry/exit in one task; bound calls after lazy startup."""
    from services.claude_api.mcp_client import McpPool
    async with AsyncExitStack() as stack:
        pool = McpPool(stack=stack, available_configs=configs)
        while True:
            try:
                method, name, arguments, future = await queue.get()
            except asyncio.CancelledError:
                # Consume actor shutdown before exiting the persistent AnyIO
                # contexts; passing cancellation into every nested exit can
                # retry an already-closed ClientSession in the legacy pool.
                break
            if future.done():
                continue
            try:
                key = name if method == 'discover' else name.split('__', 2)[1]
                # Startup already has its own bounded, task-affine lifecycle.
                # Never put a persistent AnyIO session inside a per-call scope.
                handle = await pool.ensure_spawned(key)
                if method == 'discover':
                    result = json.dumps([tool.model_dump() for tool in handle.tools], ensure_ascii=False)
                else:
                    async with asyncio.timeout(timeout_s):
                        result = await pool.call_tool(name, arguments)
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                break
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)


class ServerActors:
    """Independent servers overlap; each task-affine server stays serial.

    Lazy actor creation does not spawn transports until an actual tool call.
    The shared admission limit bounds total queued/in-flight requests, not
    merely the queue length for each server.
    """

    def __init__(self, configs, *, timeout_s=160, request_timeout_s=175):
        self.configs = configs
        self.timeout_s = timeout_s
        self.request_timeout_s = request_timeout_s
        self.actors = {}
        self.admission = asyncio.Semaphore(32)
        self.closed = False

    def _finished(self, key, queue, task):
        # Retrieve startup/cleanup exceptions and fail queued callers promptly.
        # A terminated actor must never leave a reusable dead queue behind.
        if not task.cancelled():
            task.exception()
        if self.actors.get(key) == (queue, task):
            self.actors.pop(key, None)
        while not queue.empty():
            _, _, _, future = queue.get_nowait()
            if not future.done():
                if self.closed:
                    future.cancel()
                else:
                    future.set_exception(RuntimeError('MCP server actor stopped'))

    async def submit(self, method, name, arguments):
        key = name if method == 'discover' else name.split('__', 2)[1]
        if key not in self.configs:
            raise ValueError('Server not authorized')
        future = asyncio.get_running_loop().create_future()
        try:
            async with asyncio.timeout(self.request_timeout_s), self.admission:
                if self.closed:
                    raise RuntimeError('MCP actors closed')
                actor = self.actors.get(key)
                if actor is not None and actor[1].done():
                    self._finished(key, *actor)
                    actor = None
                if actor is None:
                    queue = asyncio.Queue(maxsize=32)
                    task = asyncio.create_task(
                        run_actor(queue, {key: self.configs[key]}, timeout_s=self.timeout_s),
                        name=f'codex-mcp:{key}',
                    )
                    actor = self.actors[key] = (queue, task)
                    task.add_done_callback(lambda done, k=key, q=queue: self._finished(k, q, done))
                await actor[0].put((method, name, arguments, future))
                return await future
        finally:
            if not future.done():
                future.cancel()

    async def close(self):
        self.closed = True
        actors = list(self.actors.values())
        self.actors.clear()
        for queue, task in actors:
            task.cancel()
            while not queue.empty():
                _, _, _, future = queue.get_nowait()
                future.cancel()
        await asyncio.gather(*(task for _, task in actors), return_exceptions=True)


async def serve(payload: dict) -> None:
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    os.environ['OAUTH_NO_REFRESH'] = '1'
    os.environ['BOT_STATE_DIR'] = payload['state_dir']
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
    from services.claude_api.mcp_client import STATIC_MCP_TOOL_SCHEMAS

    server = Server('discord-tools')
    configs = payload['servers']
    actors = ServerActors(configs)
    scratchpad: dict[str, str] = {}

    async def submit(method, name, arguments):
        return await actors.submit(method, name, arguments)

    @server.list_tools()
    async def list_tools():
        tools = []
        for key in sorted(configs):
            for spec in STATIC_MCP_TOOL_SCHEMAS.get(key, []):
                tools.append(Tool(name=spec['name'], description=spec.get('description', ''), inputSchema=spec['input_schema']))
        tools.extend([
            Tool(name='Scratchpad', description='Per-conversation working notes for multi-step tasks. Write progress, findings and decisions; not private chain-of-thought.',
                 inputSchema={'type':'object','properties':{'op':{'type':'string','enum':['read','write','append']},'section':{'type':'string'},'content':{'type':'string'}},'required':['op']}),
            Tool(name='discover_tools', description='Discover current tool schemas on an authorized server. Available servers: ' + ', '.join(sorted(configs)),
                 inputSchema={'type':'object','properties':{'server':{'type':'string','enum':sorted(configs)}},'required':['server']}),
            Tool(name='call_tool', description='Call a tool discovered with discover_tools, including tools absent from the cached schema catalog.',
                 inputSchema={'type':'object','properties':{'server':{'type':'string','enum':sorted(configs)},'tool':{'type':'string'},'arguments':{'type':'object'}},'required':['server','tool','arguments']}),
        ])
        return tools

    @server.call_tool()
    async def call_tool(name, arguments):
        from services.mcp_control import filter_enabled_mcp_keys
        args = arguments or {}
        if not isinstance(args, dict):
            raise ValueError('Tool arguments must be an object')
        if name == 'Scratchpad':
            op = args.get('op')
            if op != 'read':
                section = str(args.get('section', '')).strip()[:100]
                if not section or op not in {'write', 'append'}:
                    raise ValueError('Invalid scratchpad operation')
                content = str(args.get('content', ''))
                if op == 'append':
                    content = scratchpad.get(section, '') + '\n' + content
                if len(content) > 4000 or len(scratchpad) >= 20 and section not in scratchpad:
                    raise ValueError('Scratchpad budget exceeded')
                if sum(len(value) for key, value in scratchpad.items() if key != section) + len(content) > 24000:
                    raise ValueError('Scratchpad budget exceeded')
                scratchpad[section] = content
            return [TextContent(type='text', text='\n\n'.join(f'## {key}\n{value}' for key, value in scratchpad.items()) or '(empty)')]
        if name in {'discover_tools', 'call_tool'}:
            key = args.get('server')
            tool_name = args.get('tool', '')
            tool_args = args.get('arguments') or {}
        else:
            pieces = name.split('__', 2)
            if len(pieces) != 3 or pieces[0] != 'mcp':
                raise ValueError('Unknown tool')
            _, key, tool_name = pieces
            tool_args = args
        if key not in configs or key not in filter_enabled_mcp_keys(set(configs)):
            raise ValueError('Server not authorized or disabled')
        if not isinstance(tool_args, dict):
            raise ValueError('Tool arguments must be an object')
        if name != 'discover_tools' and (not isinstance(tool_name, str) or not tool_name):
            raise ValueError('Tool name is required')
        if name == 'discover_tools':
            text = await submit('discover', key, {})
        else:
            text = await submit('call', f'mcp__{key}__{tool_name}', tool_args)
        return [TextContent(type='text', text=text)]

    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await actors.close()


if __name__ == '__main__':
    asyncio.run(serve(load_config(Path(sys.argv[1]))))
