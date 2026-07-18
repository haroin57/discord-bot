"""Shared plumbing for OAuth-subscription CLI runners (agy / codex).

Both runners follow the same recipe: strip provider API keys from the
environment so the CLI falls back to its OAuth credential store, TTL-cache an
availability probe, and run the binary as a detached subprocess with a hard
timeout. Only the answer-extraction strategy differs per CLI, so that stays
in each runner module. (P1-b of docs/2026-07-03-grand-refactor-plan.md.)
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from services.claude_api.subprocess_runner import (
    build_allowlisted_env,
    deadline_after,
    run_subprocess,
    SubprocessDeadlineExceeded,
)


def clean_env(strip_keys: tuple[str, ...]) -> dict:
    """Build the allowlisted child environment without `strip_keys`.

    A present API key makes both CLIs bypass their OAuth credential store and
    bill the metered API instead — removal is what forces the subscription
    path, so this must be applied to every subprocess spawn.
    """
    return build_allowlisted_env(remove=strip_keys)


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
    timeout_s: float,
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
    try:
        result = await run_subprocess(
            argv,
            deadline=deadline_after(timeout_s),
            env=env,
            cwd=cwd,
            label=label,
        )
    except SubprocessDeadlineExceeded as exc:
        raise RuntimeError(f"{label} timed out after {timeout_s}s") from exc
    return result.returncode, result.stdout, result.stderr
