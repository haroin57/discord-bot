"""Minimal aiohttp server exposing live and ready probes on localhost.

Replaces the /health route that web_api.py (Socket.IO Web UI) used to serve
(Phase 1-1, docs/2026-07-04-radical-cloudflare-outsourcing-plan.md). The
external uptime Worker probes this endpoint every minute, so it must stay up
even though the rest of the Web API is gone.

``/live`` proves that the HTTP event loop is serving requests and never probes
dependencies. ``/ready`` aggregates runtime dependencies and returns 503 when
they are degraded. ``/health`` remains a compatibility alias for ``/ready``.
"""

import asyncio
import logging
import time

from aiohttp import web

from config import WEB_API_PORT
from services import health as health_service

log = logging.getLogger(__name__)


class HealthServer:
    """Tiny probe server (:3002). No auth, localhost-only by default."""

    def __init__(self, bot):
        self.bot = bot
        self.app = web.Application()
        self.app.router.add_get("/live", self._handle_live)
        self.app.router.add_get("/ready", self._handle_ready)
        self.app.router.add_get("/health", self._handle_health)
        self._runner: web.AppRunner | None = None
        self._release_verification_task: asyncio.Task[None] | None = None
        self._readiness_task: asyncio.Task | None = None
        # Fallback uptime if services.health snapshot ever fails hard.
        self._started_at: float = time.time()

    async def _verify_release_in_background(self) -> None:
        release_sha, release_error = await asyncio.to_thread(
            health_service.initialize_release_verification
        )
        if release_sha is None:
            log.error("Release generation verification failed: %s", release_error)
            return
        log.info("Release generation verification passed: %s", release_sha)

    def _start_release_verification(self) -> None:
        if (
            self._release_verification_task is None
            or self._release_verification_task.done()
        ):
            self._release_verification_task = asyncio.create_task(
                self._verify_release_in_background(),
                name="release-generation-verification",
            )

    async def _handle_live(self, request: web.Request) -> web.Response:
        # Keep liveness independent from services.health and its psutil/runtime
        # imports. If aiohttp can execute this handler, the process is live.
        return web.json_response(
            {
                "status": "live",
                "uptime_s": int(time.time() - self._started_at),
            },
            status=200,
        )

    async def _handle_ready(self, request: web.Request) -> web.Response:
        try:
            # psutil and the Chroma heartbeat are synchronous.  Keep them away
            # from the bot event loop so a stopped dependency cannot stall the
            # Discord gateway heartbeat.
            # Coalesce overlapping probes only. A completed result is never
            # cached: the next request must see current dependency health.
            # Shield the shared worker from disconnected/cancelled clients;
            # cancelling to_thread cannot stop its synchronous network I/O.
            if self._readiness_task is None or self._readiness_task.done():
                self._readiness_task = asyncio.create_task(
                    asyncio.to_thread(
                        health_service.get_readiness_snapshot, bot=self.bot
                    ),
                    name="readiness-snapshot",
                )
                self._readiness_task.add_done_callback(self._observe_readiness)
            snap = await asyncio.shield(self._readiness_task)
        except Exception as exc:  # pragma: no cover — health must never fail hard
            log.exception("get_readiness_snapshot failed closed: %s", exc)
            uptime = int(time.time() - self._started_at) if self._started_at else 0
            snap = {
                "status": "degraded",
                "uptime_s": uptime,
                "subsystems": {
                    "readiness": {
                        "ok": False,
                        "reason": f"snapshot_failed:{type(exc).__name__}",
                    }
                },
            }
        status_code = 200 if snap.get("status") == "ok" else 503
        return web.json_response(snap, status=status_code)

    @staticmethod
    def _observe_readiness(task: asyncio.Task) -> None:
        # Retrieve failures even when every HTTP waiter disconnected. The
        # handler still receives the exception and returns a fail-closed 503.
        if not task.cancelled():
            task.exception()

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Backward-compatible readiness alias for existing external probes."""
        return await self._handle_ready(request)

    async def start(self, port: int = WEB_API_PORT, host: str = "127.0.0.1") -> None:
        if host != "127.0.0.1":
            raise ValueError("HealthServer is restricted to 127.0.0.1")
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        log.info("HealthServer listening on %s:%d (/live, /ready, /health)", host, port)
        # Deep generation verification hashes every manifested release file.
        # Start it only after bind so /live remains available during the scan.
        self._start_release_verification()
        # Keep running until cancelled.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._release_verification_task is not None:
            if not self._release_verification_task.done():
                self._release_verification_task.cancel()
            try:
                await self._release_verification_task
            except asyncio.CancelledError:
                pass
            self._release_verification_task = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        log.info("HealthServer stopped")


__all__ = ["HealthServer"]
