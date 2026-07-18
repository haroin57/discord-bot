"""MCP subprocess pool + tool dispatch for the direct-API path.

Wraps the Python ``mcp`` SDK so the tool_loop can advertise MCP-provided
tools to the Anthropic API and dispatch calls back to the right server.
The CLI used to own this lifecycle; in the direct-API world we own it.

Lifecycle is managed via ``AsyncExitStack``: ``spawn_servers()`` returns
a context-manager-shaped wrapper; the tool_loop's entry function should
own that stack so a crash anywhere in the loop guarantees ``aclose()``
is called and child PIDs SIGTERMed.

Local stdio transports expose their exact ``asyncio.subprocess.Process`` to an
observer. Cleanup is restricted to those registered handles; this module never
discovers processes from command lines, argv, or environment data.
"""

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession

from services.claude_api.mcp_transport import ProcessObserver, managed_stdio_client
from services.claude_api.subprocess_runner import (
    await_before_deadline,
    build_allowlisted_env,
    deadline_after,
)

# SSE transport is still supported for remote MCP servers that expose it.
try:
    from mcp.client.sse import sse_client  # type: ignore

    _HAS_SSE_TRANSPORT = True
except ImportError:  # pragma: no cover
    sse_client = None  # type: ignore
    _HAS_SSE_TRANSPORT = False

# streamable_http is optional / version-dependent — guard the import.
try:
    from mcp.client.streamable_http import streamablehttp_client  # type: ignore

    _HAS_HTTP_TRANSPORT = True
except ImportError:  # pragma: no cover
    streamablehttp_client = None  # type: ignore
    _HAS_HTTP_TRANSPORT = False

log = logging.getLogger(__name__)


@dataclass
class McpServerHandle:
    """Live MCP server connection + its tool list."""

    key: str
    session: ClientSession
    tools: list[Any] = field(default_factory=list)  # mcp.Tool


async def _enter_task_affine_session(
    stack: AsyncExitStack,
    session: ClientSession,
) -> ClientSession:
    """Enter a ClientSession and retry its task-affine exit after cancellation.

    MCP's BaseSession enters an AnyIO TaskGroup in ``__aenter__``.  AnyIO
    requires its cancel scope to be exited from that same asyncio task, so the
    session cannot be closed in a shield helper task.  AsyncExitStack normally
    pops an exit callback before awaiting it; if that one await is cancelled,
    calling ``stack.aclose()`` again cannot retry the half-finished session
    exit.  Register a retrying callback around this task-affine context only.
    The first delivered cancellation is re-raised after the session has
    finished closing, allowing the surrounding stack to clean later contexts.
    """
    entered = await session.__aenter__()

    async def close_session(exc_type, exc, traceback) -> bool | None:
        first_cancel: asyncio.CancelledError | None = None
        while True:
            try:
                suppressed = await session.__aexit__(exc_type, exc, traceback)
            except asyncio.CancelledError as cancel_exc:
                if first_cancel is None:
                    first_cancel = cancel_exc
                continue
            if first_cancel is not None:
                raise first_cancel
            return suppressed

    stack.push_async_exit(close_session)
    return entered


async def _spawn_one(
    stack: AsyncExitStack,
    name: str,
    cfg: dict,
    *,
    process_observer: ProcessObserver | None = None,
) -> McpServerHandle:
    """Open transport, initialize session, list tools. Registers exits in stack."""
    startup_timeout = max(0.1, float(cfg.get("startup_timeout", 30.0)))
    deadline = deadline_after(startup_timeout)
    local_stack = AsyncExitStack()
    await local_stack.__aenter__()
    try:
        transport = cfg.get("type")
        if transport == "sse":
            if not _HAS_SSE_TRANSPORT:
                raise RuntimeError(
                    f"MCP SSE transport not available, cannot spawn '{name}'",
                )
            read, write = await await_before_deadline(
                local_stack.enter_async_context(
                    sse_client(  # type: ignore[misc]
                        cfg["url"],
                        headers=cfg.get("headers") or {},
                        timeout=cfg.get("timeout", 5),
                        sse_read_timeout=cfg.get("sse_read_timeout", 300),
                    )
                ),
                deadline,
            )
        elif transport == "http":
            if not _HAS_HTTP_TRANSPORT:
                raise RuntimeError(
                    f"MCP HTTP transport not available, cannot spawn '{name}'",
                )
            ctx = streamablehttp_client(  # type: ignore[misc]
                cfg["url"],
                headers=cfg.get("headers") or {},
            )
            transport_result = await await_before_deadline(
                local_stack.enter_async_context(ctx),
                deadline,
            )
            read, write = transport_result[0], transport_result[1]
        else:
            argv = [cfg["command"], *(cfg.get("args") or [])]
            observer_kwargs = (
                {"process_observer": process_observer}
                if process_observer is not None
                else {}
            )
            read, write = await await_before_deadline(
                local_stack.enter_async_context(
                    managed_stdio_client(
                        argv,
                        env=build_allowlisted_env(cfg.get("env") or {}),
                        cwd=cfg.get("cwd"),
                        deadline=deadline,
                        **observer_kwargs,
                    )
                ),
                deadline,
            )

        session = await await_before_deadline(
            _enter_task_affine_session(local_stack, ClientSession(read, write)),
            deadline,
        )
        await await_before_deadline(session.initialize(), deadline)
        tools_resp = await await_before_deadline(session.list_tools(), deadline)
    except BaseException as exc:
        # ClientSession owns an AnyIO TaskGroup whose cancel scope must be
        # exited by the same asyncio task that entered it.  Moving aclose() to
        # await_cleanup_task()'s helper task makes ClientSession.__aexit__ call
        # cancel_scope.cancel() from the wrong task.  On an ordinary startup
        # failure (for example, a stdio child exiting before initialize) that
        # cancels this request task and turns a recoverable MCP error into an
        # unhandled CancelledError at bot.on_message.
        #
        # AsyncExitStack continues unwinding its remaining callbacks when an
        # additional cancellation arrives.  Keep the task-affine stack in the
        # current task and, for a request that was already cancelled, preserve
        # the first cancellation reason after cleanup finishes.
        try:
            await local_stack.aclose()
        except BaseException as cleanup_exc:
            if isinstance(exc, asyncio.CancelledError):
                raise exc from cleanup_exc
            raise
        raise
    await stack.enter_async_context(local_stack.pop_all())
    return McpServerHandle(key=name, session=session, tools=tools_resp.tools)


async def spawn_servers(
    cfg: dict[str, dict],
    stack: AsyncExitStack,
    *,
    process_observer: ProcessObserver | None = None,
) -> dict[str, McpServerHandle]:
    """Spawn every server in ``cfg``; caller owns ``stack`` for cleanup."""
    handles: dict[str, McpServerHandle] = {}
    for name, server_cfg in cfg.items():
        try:
            if process_observer is None:
                handles[name] = await _spawn_one(stack, name, server_cfg)
            else:
                handles[name] = await _spawn_one(
                    stack,
                    name,
                    server_cfg,
                    process_observer=process_observer,
                )
            log.info("MCP spawned: %s (%d tools)", name, len(handles[name].tools))
        except Exception as exc:
            log.error("MCP spawn failed for %s: %s", name, exc)
            # Continue with the other servers; the user may still get useful
            # behavior with a subset (mirrors claude_sdk behavior, where the
            # CLI also tolerated single-server failures).
    return handles


def mcp_tools_to_anthropic(handles: dict[str, McpServerHandle]) -> list[dict]:
    """Convert MCP Tool definitions into the Anthropic tools-API schema."""
    out: list[dict] = []
    for h in handles.values():
        for tool in h.tools:
            qualified_name = f"mcp__{h.key}__{tool.name}"
            out.append(
                {
                    "name": qualified_name,
                    "description": getattr(tool, "description", "") or "",
                    "input_schema": (
                        getattr(tool, "inputSchema", None)
                        or {"type": "object", "properties": {}}
                    ),
                }
            )
    return out


# Static MCP tool schemas that we advertise on every admin API request even
# when the upstream MCP process isn't spawned yet. The actual process is
# spawned lazily by McpPool.call_tool() when Claude emits a tool_use for
# one of these names. Mem0 sits here so Kanae can record durable facts
# without selector heuristics having to detect "I should remember this"
# first; the WEB-tier servers (x-scraper / brave-search) were added
# 2026-05-16 after a turn-30 exhaustion incident where
# pick_mcp_servers() dropped WEB tier on a short follow-up message, the
# tools fell out of advertised_tools, and Kanae burned the whole loop
# searching for tools that no longer existed in her schema list.
STATIC_MCP_TOOL_SCHEMAS: dict[str, list[dict]] = {
    "mem0": [
        {
            "name": "mcp__mem0__search",
            "description": (
                "Search shared Mem0 long-term memory. Results carry metadata "
                "(category, importance 1-5, tier Live/Hot/Warm/Cold, "
                "mention_count) — prefer high-importance fresh facts. "
                "smart=true enables Haiku query-rewrite + rerank (slower, "
                "use only for explicit recall asks). "
                "【いつ呼ぶか】自動注入された記憶に無い過去の事実・好み・"
                "出来事を確認したいとき、「覚えてる？」系の明示的な想起依頼の"
                "ときに呼ぶ。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                    "user_id": {"type": "string"},
                    "smart": {"type": "boolean", "default": False},
                },
                "required": ["query"],
            },
        },
        {
            "name": "mcp__mem0__add",
            "description": (
                "Store a conversation turn so Mem0 can extract durable "
                "long-term facts (auto-classified into category / importance "
                "/ tier). "
                "【いつ呼ぶか】ユーザーが「覚えて」と言ったとき、または今後の"
                "会話で参照すべき新事実（好み・予定・決定事項）が出たときに"
                "呼ぶ。雑談の逐語保存には使わない（自動バッチ保存が別にある）。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "user_message": {"type": "string"},
                    "assistant_message": {"type": "string"},
                    "user_id": {"type": "string"},
                    "context": {"type": "string"},
                },
                "required": ["user_message", "assistant_message"],
            },
        },
    ],
}


_STATIC_SCHEMA_JSON = Path(__file__).with_name("static_mcp_schemas.json")


def _load_static_schemas_from_disk() -> None:
    """Merge the on-disk WEB-tier schema dump into STATIC_MCP_TOOL_SCHEMAS.

    The dump is produced by scripts/dump_mcp_tool_schemas.py against the
    live MCP servers, so the in-Python source of truth doesn't have to
    hand-mirror multi-hundred-line enum schemas (brave-search alone is
    ~800 lines). Missing or malformed dump is non-fatal: those servers
    just won't be statically advertised, matching pre-2026-05-16
    behavior.
    """
    if not _STATIC_SCHEMA_JSON.is_file():
        return
    try:
        with _STATIC_SCHEMA_JSON.open(encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "static_mcp_schemas.json load failed (%s); WEB MCP tools "
            "will fall back to keyword-gated advertisement",
            exc,
        )
        return
    if not isinstance(payload, dict):
        log.warning("static_mcp_schemas.json: expected dict at top level")
        return
    for server_key, tools in payload.items():
        # 空リストは「dump 失敗の残骸」でしかないので無視する。extend([]) でも
        # 実害はないが、将来 setdefault 側の意味論が変わっても正常スキーマを
        # 空で確定させないよう明示的に弾いておく。
        if not isinstance(tools, list) or not tools:
            continue
        bucket = STATIC_MCP_TOOL_SCHEMAS.setdefault(server_key, [])
        # name dedupe: Python 内ハードコード（例: mem0 の enriched description）
        # が優先。dump にも同名 tool が入っていても二重広告しない。
        existing = {t.get("name") for t in bucket}
        bucket.extend(t for t in tools if t.get("name") not in existing)


_load_static_schemas_from_disk()


def static_mcp_tools_to_anthropic(keys) -> list[dict]:
    """Return Anthropic tool schemas for the given static MCP keys.

    Unlike ``mcp_tools_to_anthropic``, this does not require the MCP
    server to be spawned — the schemas are baked in. Spawn happens at
    tool_use time via ``McpPool.call_tool()``.
    """
    from services.mcp_control import filter_enabled_mcp_keys

    out: list[dict] = []
    for key in filter_enabled_mcp_keys(keys):
        out.extend(dict(tool) for tool in STATIC_MCP_TOOL_SCHEMAS.get(key, []))
    return out


def _parse_qualified_tool_name(qualified_name: str) -> tuple[str, str]:
    """Split ``mcp__server__tool`` into ``(server, tool_name)``."""
    try:
        _, server, tool_name = qualified_name.split("__", 2)
    except ValueError as exc:
        raise ValueError(
            f"malformed MCP tool name {qualified_name!r}, want mcp__server__tool",
        ) from exc
    return server, tool_name


async def _dispatch_tool_call(
    handle: McpServerHandle, tool_name: str, args: dict
) -> str:
    """Call a tool on a live handle and stringify the response content blocks.

    Anthropic's ``tool_result.content`` is a string, so TextContent /
    ImageContent / EmbeddedResource blocks are flattened to text here.
    """
    result = await handle.session.call_tool(tool_name, args)

    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(getattr(block, "text", "") or "")
        elif block_type == "image":
            mime = getattr(block, "mimeType", "?")
            data = getattr(block, "data", "") or ""
            parts.append(f"[image: {mime}, {len(data)} bytes base64]")
        else:
            # Unknown / embedded resource — fall back to repr.
            parts.append(str(block))
    return "\n".join(parts) or "(empty)"


async def call_mcp_tool(
    handles: dict[str, McpServerHandle],
    qualified_name: str,
    args: dict,
) -> str:
    """Dispatch a ``mcp__server__tool`` call to the right session.

    Deprecated for direct use inside the agent loop — prefer
    :class:`McpPool` which adds auto-spawn fallback. Kept for backwards
    compatibility with tests and any caller that already manages handles.
    """
    server, tool_name = _parse_qualified_tool_name(qualified_name)
    handle = handles.get(server)
    if handle is None:
        raise KeyError(f"MCP server '{server}' is not spawned")
    return await _dispatch_tool_call(handle, tool_name, args)


@dataclass
class McpPool:
    """Pool of MCP server handles with on-demand spawn support.

    Keyword-matched servers can be spawned eagerly via :meth:`spawn_eager`
    (warmup); any other configured server is spawned the moment Claude
    actually tries to call one of its tools via :meth:`call_tool`. The
    caller's :class:`AsyncExitStack` owns all spawned subprocesses, so
    every handle is torn down when the conversation finishes.

    The per-server ``_spawn_locks`` use a double-checked-locking pattern:
    two concurrent ``ensure_spawned("mem0")`` calls block on the same lock
    and only one ``_spawn_one`` call actually runs.
    """

    stack: AsyncExitStack
    available_configs: dict[str, dict]
    handles: dict[str, McpServerHandle] = field(default_factory=dict)
    _spawn_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    process_observer: ProcessObserver | None = None

    def __post_init__(self) -> None:
        # Snapshot the persistent override at pool construction. A command that
        # changes enablement does not mutate an already-running model turn.
        from services.mcp_control import filter_enabled_mcp_configs

        self.available_configs = filter_enabled_mcp_configs(self.available_configs)

    async def ensure_spawned(self, name: str) -> McpServerHandle:
        """Return an existing handle or lazy-spawn the configured server."""
        if name in self.handles:
            return self.handles[name]
        lock = self._spawn_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if name in self.handles:  # double-checked locking
                return self.handles[name]
            cfg = self.available_configs.get(name)
            if cfg is None:
                raise KeyError(
                    f"MCP server '{name}' not configured "
                    f"(available: {list(self.available_configs)})"
                )
            if self.process_observer is None:
                handle = await _spawn_one(self.stack, name, cfg)
            else:
                handle = await _spawn_one(
                    self.stack,
                    name,
                    cfg,
                    process_observer=self.process_observer,
                )
            log.info(
                "MCP spawned (lazy): %s (%d tools)",
                name,
                len(handle.tools),
            )
            self.handles[name] = handle
            return handle

    async def spawn_eager(self, names: list[str]) -> None:
        """Warmup: spawn the listed servers up-front. Failures are non-fatal."""
        for n in names:
            try:
                await self.ensure_spawned(n)
            except Exception as exc:
                log.error("MCP spawn (eager) failed for %s: %s", n, exc)

    async def call_tool(self, qualified_name: str, args: dict) -> str:
        """Dispatch ``mcp__server__tool``; lazy-spawn the server if needed."""
        server, tool_name = _parse_qualified_tool_name(qualified_name)
        handle = await self.ensure_spawned(server)
        return await _dispatch_tool_call(handle, tool_name, args)


def kill_orphan_mcp_processes() -> int:
    """Compatibility no-op; global MCP process discovery is prohibited.

    Exact observer handles are owned by ``managed_stdio_client`` and their
    ``AsyncExitStack``. A synchronous process-wide sweep cannot prove that an
    unregistered process belongs to this caller, so it must not signal one.
    """
    return 0
