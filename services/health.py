"""Bot health snapshot for /health endpoint (SRE-C7).

Aggregates liveness/readiness from each subsystem without touching their
internal state. web_api.py calls get_health_snapshot() inside the
/health route handler.
"""

import time
import logging
from typing import Any

log = logging.getLogger(__name__)

_started_at = time.monotonic()


def uptime_seconds() -> int:
    return int(time.monotonic() - _started_at)


def _check_discord(bot) -> dict[str, Any]:
    if bot is None:
        return {"ok": False, "reason": "bot_not_initialized"}
    try:
        is_ready = bool(bot.is_ready())
        latency = bot.latency if is_ready else None
        return {"ok": is_ready, "gateway_latency_ms": int(latency * 1000) if latency else None}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def _check_oauth() -> dict[str, Any]:
    try:
        from services.oauth_manager import is_oauth_healthy
        return {"ok": bool(is_oauth_healthy())}
    except ImportError:
        return {"ok": True, "reason": "oauth_module_unavailable"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def _check_mem0() -> dict[str, Any]:
    """Lightweight check — does NOT issue a real Mem0 query (would be expensive)."""
    try:
        from services import mem0_service  # noqa
        return {"ok": True}
    except ImportError as e:
        return {"ok": False, "reason": f"import_failed: {e}"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def get_health_snapshot(bot=None) -> dict[str, Any]:
    """Compose a health dict for /health responses."""
    discord_st = _check_discord(bot)
    oauth_st = _check_oauth()
    mem0_st = _check_mem0()
    overall_ok = discord_st["ok"] and oauth_st["ok"] and mem0_st["ok"]
    return {
        "status": "ok" if overall_ok else "degraded",
        "uptime_s": uptime_seconds(),
        "subsystems": {
            "discord": discord_st,
            "oauth": oauth_st,
            "mem0": mem0_st,
        },
    }


__all__ = ["get_health_snapshot", "uptime_seconds"]
