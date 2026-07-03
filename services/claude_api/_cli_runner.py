"""Shared plumbing for OAuth-subscription CLI runners (agy / codex).

Both runners follow the same recipe: strip provider API keys from the
environment so the CLI falls back to its OAuth credential store, TTL-cache an
availability probe, and run the binary as a detached subprocess with a hard
timeout. Only the answer-extraction strategy differs per CLI, so that stays
in each runner module. (P1-b of docs/2026-07-03-grand-refactor-plan.md.)
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable


def clean_env(strip_keys: tuple[str, ...]) -> dict:
    """Copy of os.environ without `strip_keys`.

    A present API key makes both CLIs bypass their OAuth credential store and
    bill the metered API instead — removal is what forces the subscription
    path, so this must be applied to every subprocess spawn.
    """
    env = dict(os.environ)
    for k in strip_keys:
        env.pop(k, None)
    return env


class AvailabilityCache:
    """Process-lifetime TTL cache around an async availability probe."""

    def __init__(self, ttl_s: float = 300.0) -> None:
        self._ttl_s = ttl_s
        self._entry: "tuple[float, bool] | None" = None

    async def get(self, probe: Callable[[], Awaitable[bool]]) -> bool:
        now = time.monotonic()
        if self._entry is not None and now - self._entry[0] < self._ttl_s:
            return self._entry[1]
        ok = await probe()
        self._entry = (now, ok)
        return ok

    def reset(self) -> None:
        self._entry = None


async def run_cli(
    argv: "list[str]",
    *,
    timeout_s: int,
    env: dict,
    cwd: "str | None",
    label: str,
) -> "tuple[int | None, str, str]":
    """Run a CLI detached and return (returncode, stdout, stderr) decoded.

    - stdin=DEVNULL: under MCP a piped stdin makes children block on input.
    - start_new_session: fresh POSIX session (mitigates agy's non-TTY
      stdout-loss bug and keeps signals away from the bot process group).
    - On timeout the process is killed and RuntimeError is raised.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
        cwd=cwd,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await asyncio.gather(proc.wait(), return_exceptions=True)
        raise RuntimeError(f"{label} timed out after {timeout_s}s")
    stdout = (stdout_b or b"").decode("utf-8", "replace")
    stderr = (stderr_b or b"").decode("utf-8", "replace")
    return proc.returncode, stdout, stderr
