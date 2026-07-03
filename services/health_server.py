"""Minimal aiohttp server exposing GET /health on 127.0.0.1:3002.

Replaces the /health route that web_api.py (Socket.IO Web UI) used to serve
(Phase 1-1, docs/2026-07-04-radical-cloudflare-outsourcing-plan.md). The
external uptime Worker probes this endpoint every minute, so it must stay up
even though the rest of the Web API is gone.

Delegates the rich subsystem snapshot to services.health.get_health_snapshot()
and returns 503 when status != "ok". Runs in the bot's own event loop, started
from main_async.py alongside bot.start() via asyncio.gather().
"""

import asyncio
import logging
import time

from aiohttp import web

from config import WEB_API_HOST, WEB_API_PORT

log = logging.getLogger(__name__)


class HealthServer:
    """Tiny GET /health server (:3002). No auth, localhost-only by default."""

    def __init__(self, bot):
        self.bot = bot
        self.app = web.Application()
        self._runner: web.AppRunner | None = None
        # Fallback uptime if services.health snapshot ever fails hard.
        self._started_at: float = time.time()

    async def _handle_health(self, request: web.Request) -> web.Response:
        try:
            from services.health import get_health_snapshot
            snap = get_health_snapshot(bot=self.bot)
        except Exception as exc:  # pragma: no cover — health must never fail hard
            log.warning("get_health_snapshot failed, using minimal fallback: %s", exc)
            discord_ok = self.bot.is_ready() if self.bot else False
            uptime = int(time.time() - self._started_at) if self._started_at else 0
            snap = {
                "status": "ok" if discord_ok else "degraded",
                "uptime_s": uptime,
                "subsystems": {"discord": {"ok": discord_ok}},
            }
        status_code = 200 if snap.get("status") == "ok" else 503
        return web.json_response(snap, status=status_code)

    async def start(self, port: int = WEB_API_PORT, host: str = WEB_API_HOST) -> None:
        self.app.router.add_get("/health", self._handle_health)
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        log.info("HealthServer listening on %s:%d (/health)", host, port)
        # Keep running until cancelled.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        log.info("HealthServer stopped")


__all__ = ["HealthServer"]
