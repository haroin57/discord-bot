"""Native Python implementations of the built-in tools the bot exposes.

Tools advertised here are returned by ``builtin_tools_for_api(restricted)``
and dispatched by ``execute_builtin_tool(name, args, ctx)``. MCP tools live
in services/claude_api/mcp_client.py; tool_loop.py merges both sets and
routes calls to the right backend.

Restricted mode rejects every tool here at the policy gate
(services/claude_api/restricted.py), but the implementations still respect
the same defenses (cwd jail, O_NOFOLLOW, output caps, env allowlist) so a
gate regression cannot escalate.

Ports / inspiration:
- Read   → free-code/src/tools/FileReadTool/FileReadTool.tsx
- Write  → free-code/src/tools/FileWriteTool/FileWriteTool.tsx
- Edit   → free-code/src/tools/FileEditTool/FileEditTool.tsx
- Bash   → free-code/src/utils/Shell.ts:275-278 (spawn('/bin/bash', ['-c', cmd]))
- Glob   → free-code/src/tools/GlobTool/
- Grep   → free-code/src/tools/GrepTool/
"""

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Limits / config
# ---------------------------------------------------------------------------
READ_MAX_BYTES = 1_000_000  # 1 MB
READ_MAX_LINES = 2000
WRITE_MAX_BYTES = 1_000_000

GLOB_MAX_RESULTS = 1000
GREP_MAX_RESULTS = 1000

BASH_DEFAULT_TIMEOUT_S = float(os.environ.get("CLAUDE_BASH_TIMEOUT_S", "120"))
BASH_OUTPUT_CAP_BYTES = 100_000

# Whitelist of env vars that survive into Bash subprocesses. Anything else
# (ANTHROPIC_API_KEY, DISCORD_TOKEN, etc.) must not leak to spawned shells.
BASH_ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ", "SHELL")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------
@dataclass
class ToolContext:
    cwd: str
    restricted: bool
    channel_id: int


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, ToolContext], Awaitable[str]]


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------
def _resolve_under(cwd: str, path: str) -> str:
    """Resolve ``path`` (relative or absolute) and require it to stay under cwd.

    Raises ``ValueError`` if the resolved path escapes cwd. Uses
    ``os.path.realpath`` so symlinks cannot point outside.
    """
    cwd_real = os.path.realpath(cwd)
    candidate = path if os.path.isabs(path) else os.path.join(cwd_real, path)
    resolved = os.path.realpath(candidate)
    # The exact cwd is allowed; subpaths must use the separator boundary so
    # /tmp/foo is not accepted as "under" /tmp/foobar.
    if resolved != cwd_real and not resolved.startswith(cwd_real + os.sep):
        raise ValueError(
            f"path escapes cwd: {path!r} → {resolved!r} (cwd={cwd_real!r})",
        )
    return resolved


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
def _read_file_sync(path: str) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if st.st_size > READ_MAX_BYTES:
            raise ValueError(
                f"file too large: {st.st_size} bytes > {READ_MAX_BYTES} cap",
            )
        with os.fdopen(fd, "rb") as fh:
            fd = -1  # ownership transferred
            data = fh.read(READ_MAX_BYTES)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if len(lines) > READ_MAX_LINES:
        head = "".join(lines[:READ_MAX_LINES])
        return head + f"\n[truncated at {READ_MAX_LINES} lines]"
    return text


async def _tool_read(args: dict, ctx: ToolContext) -> str:
    raw = args.get("file_path")
    if not raw:
        raise ValueError("file_path is required")
    path = _resolve_under(ctx.cwd, raw)
    return await asyncio.to_thread(_read_file_sync, path)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
def _write_file_sync(path: str, content: str) -> int:
    data = content.encode("utf-8")
    if len(data) > WRITE_MAX_BYTES:
        raise ValueError(
            f"content too large: {len(data)} bytes > {WRITE_MAX_BYTES} cap",
        )
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        raise FileNotFoundError(f"parent directory missing: {parent}")
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    return len(data)


async def _tool_write(args: dict, ctx: ToolContext) -> str:
    raw_path = args.get("file_path")
    content = args.get("content")
    if not raw_path:
        raise ValueError("file_path is required")
    if content is None:
        raise ValueError("content is required")
    path = _resolve_under(ctx.cwd, raw_path)
    n = await asyncio.to_thread(_write_file_sync, path, content)
    return f"wrote {n} bytes to {path}"


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------
async def _tool_edit(args: dict, ctx: ToolContext) -> str:
    raw_path = args.get("file_path")
    old = args.get("old_string")
    new = args.get("new_string")
    replace_all = bool(args.get("replace_all"))
    if not raw_path or old is None or new is None:
        raise ValueError("file_path, old_string, new_string are required")
    if old == new:
        raise ValueError("old_string and new_string are identical")
    path = _resolve_under(ctx.cwd, raw_path)
    content = await asyncio.to_thread(_read_file_sync, path)
    occurrences = content.count(old)
    if occurrences == 0:
        raise ValueError(f"old_string not found in {path}")
    if not replace_all and occurrences > 1:
        raise ValueError(
            f"old_string occurs {occurrences} times; pass replace_all=True or "
            "include more surrounding context to make it unique",
        )
    updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
    n = await asyncio.to_thread(_write_file_sync, path, updated)
    return f"edited {path} ({occurrences} replacement{'s' if occurrences != 1 else ''}, {n} bytes)"


# ---------------------------------------------------------------------------
# Bash
# ---------------------------------------------------------------------------
def _bash_env() -> dict:
    return {k: v for k, v in os.environ.items() if k in BASH_ENV_ALLOWLIST}


async def _tool_bash(args: dict, ctx: ToolContext) -> str:
    cmd = args.get("command")
    if not cmd or not isinstance(cmd, str):
        raise ValueError("command is required (string)")
    timeout_s = float(args.get("timeout", BASH_DEFAULT_TIMEOUT_S))
    timeout_s = min(timeout_s, BASH_DEFAULT_TIMEOUT_S)  # cap to default

    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-c", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=ctx.cwd,
        env=_bash_env(),
        start_new_session=True,  # isolate process group for clean termination
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        # Kill the whole process group
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (OSError, ProcessLookupError):
            pass
        raise TimeoutError(f"command timed out after {timeout_s}s")

    out = (stdout or b"").decode("utf-8", errors="replace")[:BASH_OUTPUT_CAP_BYTES]
    err = (stderr or b"").decode("utf-8", errors="replace")[:BASH_OUTPUT_CAP_BYTES]
    pieces = []
    if out:
        pieces.append(out)
    if err:
        pieces.append(f"--- stderr ---\n{err}")
    pieces.append(f"--- exit code: {proc.returncode} ---")
    return "\n".join(pieces)


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------
def _glob_sync(cwd: str, pattern: str) -> list[str]:
    base = Path(cwd)
    results: list[str] = []
    for p in base.rglob(pattern):
        results.append(str(p))
        if len(results) >= GLOB_MAX_RESULTS:
            break
    return results


async def _tool_glob(args: dict, ctx: ToolContext) -> str:
    pattern = args.get("pattern")
    if not pattern:
        raise ValueError("pattern is required")
    results = await asyncio.to_thread(_glob_sync, ctx.cwd, pattern)
    if not results:
        return f"(no matches for {pattern!r})"
    return "\n".join(results)


# ---------------------------------------------------------------------------
# Grep
# ---------------------------------------------------------------------------
async def _grep_via_rg(cwd: str, pattern: str, glob: str | None) -> str:
    cmd = ["rg", "-n", "--no-heading", "--color=never", pattern]
    if glob:
        cmd.extend(["-g", glob])
    cmd.append(".")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    out = (stdout or b"").decode("utf-8", errors="replace")
    lines = out.splitlines()
    if len(lines) > GREP_MAX_RESULTS:
        lines = lines[:GREP_MAX_RESULTS] + [f"[truncated at {GREP_MAX_RESULTS} matches]"]
    return "\n".join(lines) if lines else "(no matches)"


def _grep_python(cwd: str, pattern: str, glob: str | None) -> str:
    rx = re.compile(pattern)
    results: list[str] = []
    base = Path(cwd)
    iterator = base.rglob(glob) if glob else base.rglob("*")
    for p in iterator:
        if not p.is_file():
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if rx.search(line):
                        results.append(f"{p}:{lineno}:{line.rstrip()}")
                        if len(results) >= GREP_MAX_RESULTS:
                            return "\n".join(results) + f"\n[truncated at {GREP_MAX_RESULTS}]"
        except OSError:
            continue
    return "\n".join(results) if results else "(no matches)"


async def _tool_grep(args: dict, ctx: ToolContext) -> str:
    pattern = args.get("pattern")
    if not pattern:
        raise ValueError("pattern is required")
    glob = args.get("glob")
    if shutil.which("rg"):
        return await _grep_via_rg(ctx.cwd, pattern, glob)
    return await asyncio.to_thread(_grep_python, ctx.cwd, pattern, glob)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
BUILTIN_TOOLS: dict[str, ToolDef] = {
    "Read": ToolDef(
        name="Read",
        description="Read a UTF-8 text file from disk under the working directory.",
        input_schema={
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
        handler=_tool_read,
    ),
    "Write": ToolDef(
        name="Write",
        description="Write content to a file under the working directory (0600).",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        },
        handler=_tool_write,
    ),
    "Edit": ToolDef(
        name="Edit",
        description="Replace a substring inside an existing file.",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
        handler=_tool_edit,
    ),
    "Bash": ToolDef(
        name="Bash",
        description="Run a command via /bin/bash -c with a per-call timeout.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number"},
            },
            "required": ["command"],
        },
        handler=_tool_bash,
    ),
    "Glob": ToolDef(
        name="Glob",
        description="Find files matching a glob pattern under the working directory.",
        input_schema={
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
        handler=_tool_glob,
    ),
    "Grep": ToolDef(
        name="Grep",
        description="Search for a regex inside files under the working directory.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string"},
            },
            "required": ["pattern"],
        },
        handler=_tool_grep,
    ),
}


def builtin_tools_for_api(restricted: bool) -> list[dict]:
    """Return the tools-API schema list for the requested permission level.

    Restricted mode currently advertises NO built-in tools (the policy
    gate would deny them anyway; advertising them just wastes context).
    """
    if restricted:
        return []
    return [
        {
            "name": td.name,
            "description": td.description,
            "input_schema": td.input_schema,
        }
        for td in BUILTIN_TOOLS.values()
    ]


async def execute_builtin_tool(name: str, args: dict, ctx: ToolContext) -> str:
    td = BUILTIN_TOOLS.get(name)
    if td is None:
        raise KeyError(f"unknown builtin tool: {name}")
    return await td.handler(args, ctx)
