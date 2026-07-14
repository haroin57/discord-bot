"""Bot health snapshot for /health endpoint (SRE-C7).

Aggregates liveness/readiness from each subsystem without touching their
internal state. services/health_server.py calls get_health_snapshot()
inside the /health route handler.
"""

import os
import time
import logging
from typing import Any

import psutil

log = logging.getLogger(__name__)

_NPROC = os.cpu_count() or 1

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
        from services.oauth_manager import is_oauth_healthy, oauth_status_detail
        st: dict[str, Any] = {"ok": bool(is_oauth_healthy())}
        try:
            st.update(oauth_status_detail())
        except Exception:  # detail は best-effort — ok 判定は壊さない
            pass
        return st
    except ImportError:
        return {"ok": True, "reason": "oauth_module_unavailable"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def _check_mem0() -> dict[str, Any]:
    """Lightweight check — does NOT issue a real Mem0 query (would be expensive)."""
    try:
        from services import mem0  # noqa
        return {"ok": True}
    except ImportError as e:
        return {"ok": False, "reason": f"import_failed: {e}"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def _check_system() -> dict[str, Any]:
    """Host-level resource snapshot (SRE-C0c). Informational — the external
    Worker probe decides alert thresholds, not this endpoint. Only flags
    ``ok: False`` at near-outage levels (disk/mem essentially exhausted).
    """
    try:
        load1, _, _ = os.getloadavg()
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        swap = psutil.swap_memory()
        disk_pct = disk.percent
        mem_pct = mem.percent
        ok = disk_pct < 95 and mem_pct < 98
        return {
            "ok": ok,
            "cpu_load1": round(load1, 2),
            "cpu_load_pct": round(load1 / _NPROC * 100, 1),
            "mem_pct": mem_pct,
            "disk_pct": disk_pct,
            "swap_pct": swap.percent,
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def get_health_snapshot(bot=None) -> dict[str, Any]:
    """Compose a health dict for /health responses."""
    discord_st = _check_discord(bot)
    oauth_st = _check_oauth()
    mem0_st = _check_mem0()
    system_st = _check_system()
    overall_ok = discord_st["ok"] and oauth_st["ok"] and mem0_st["ok"] and system_st["ok"]
    return {
        "status": "ok" if overall_ok else "degraded",
        "uptime_s": uptime_seconds(),
        "subsystems": {
            "discord": discord_st,
            "oauth": oauth_st,
            "mem0": mem0_st,
            "system": system_st,
        },
    }


__all__ = ["get_health_snapshot", "uptime_seconds"]
