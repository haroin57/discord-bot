"""Top-level daemon — wires tailer + parser + aggregator + a post callback."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from services.vscode_sync.aggregator import EventAggregator
from services.vscode_sync.parser import parse_event
from services.vscode_sync.tailer import JSONLTailer

log = logging.getLogger(__name__)

PostCallback = Callable[[str], Awaitable[None]]

_POLL_INTERVAL_S = 1.0

_state: dict[str, object] = {
    "running": False,
    "stop_event": None,
}


def is_running() -> bool:
    return bool(_state.get("running"))


async def stop_daemon() -> None:
    ev = _state.get("stop_event")
    if isinstance(ev, asyncio.Event):
        ev.set()


async def run_daemon(
    *,
    post_callback: PostCallback,
    projects_root: str = "/home/ubuntu/.claude/projects",
    flush_interval: float = 2.0,
    idle_notify_seconds: int = 300,
    poll_interval: float = _POLL_INTERVAL_S,
) -> None:
    """Long-running daemon. Returns when stop_daemon() is called or cancelled."""
    if _state.get("running"):
        log.warning("vscode_sync: run_daemon called while already running; refusing")
        return

    stop_event = asyncio.Event()
    _state["running"] = True
    _state["stop_event"] = stop_event
    log.info(
        "vscode_sync starting (root=%s flush=%.1fs idle=%ss)",
        projects_root, flush_interval, idle_notify_seconds,
    )

    tailer = JSONLTailer(Path(projects_root))
    tailer.prime()

    aggregator = EventAggregator(flush_callback=post_callback, flush_interval=flush_interval)
    for path in list(tailer._offsets.keys()):  # noqa: SLF001
        sid = path.stem
        if sid:
            aggregator.mark_session_started(sid)

    last_seen: dict[str, float] = {}
    ended: set[str] = set()
    session_meta: dict[str, tuple[str, Optional[str]]] = {}

    try:
        while not stop_event.is_set():
            try:
                lines = tailer.poll()
            except Exception:
                log.exception("vscode_sync: tailer.poll() raised")
                lines = []

            now = time.monotonic()
            for tl in lines:
                events = parse_event(tl.raw)
                for ev in events:
                    aggregator.push(ev)
                    last_seen[ev.session_id] = now
                    session_meta[ev.session_id] = (ev.cwd, ev.git_branch)
                    if ev.session_id in ended:
                        ended.discard(ev.session_id)

            for sid, ts in list(last_seen.items()):
                if sid in ended:
                    continue
                if now - ts >= idle_notify_seconds:
                    ended.add(sid)
                    cwd, branch = session_meta.get(sid, ("?", None))
                    short = sid[:8]
                    branch_s = f" ({branch})" if branch else ""
                    minutes = max(1, idle_notify_seconds // 60)
                    await post_callback(
                        f"-# ■ `{short}` ended (idle {minutes}m)"
                        f" @ `{cwd}`{branch_s}"
                    )

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        log.info("vscode_sync cancelled")
        raise
    finally:
        try:
            await aggregator.flush_now()
        except Exception:
            log.exception("vscode_sync: final flush failed")
        _state["running"] = False
        _state["stop_event"] = None
        log.info("vscode_sync stopped")
