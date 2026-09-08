"""Bot liveness and readiness snapshots for the operational HTTP endpoints.

Aggregates liveness/readiness from each subsystem without touching their
internal state. services/health_server.py calls get_health_snapshot()
inside the /health route handler.
"""

import json
import hashlib
import logging
import math
import os
import pwd
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psutil

log = logging.getLogger(__name__)

_NPROC = os.cpu_count() or 1

_started_at = time.monotonic()
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def _verify_release_content(
    generation: Path, payload: dict[str, Any], expected_uid: int
) -> str | None:
    manifest = generation / "release-content.jsonl"
    manifest_stat = manifest.lstat()
    if not stat.S_ISREG(manifest_stat.st_mode) or manifest_stat.st_uid != expected_uid:
        return "release_content_manifest_invalid"
    if (
        stat.S_IMODE(manifest_stat.st_mode) & 0o022
        or manifest_stat.st_size > 32_000_000
    ):
        return "release_content_manifest_invalid"
    manifest_bytes = manifest.read_bytes()
    if (
        payload.get("content_manifest_sha256")
        != hashlib.sha256(manifest_bytes).hexdigest()
    ):
        return "release_content_manifest_digest_mismatch"

    expected: dict[str, tuple[int, str]] = {}
    for raw_line in manifest_bytes.decode("utf-8").splitlines():
        record = json.loads(raw_line)
        if not isinstance(record, dict) or set(record) != {"path", "mode", "sha256"}:
            return "release_content_manifest_invalid"
        relative = record["path"]
        relative_path = Path(relative) if isinstance(relative, str) else Path("/")
        if (
            not isinstance(relative, str)
            or relative_path.is_absolute()
            or relative_path.parts[:1] not in {("src",), ("venv",)}
            or ".." in relative_path.parts
            or relative in expected
            or not isinstance(record["mode"], int)
            or not isinstance(record["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None
        ):
            return "release_content_manifest_invalid"
        expected[relative] = (record["mode"], record["sha256"])

    actual: dict[str, tuple[int, str]] = {}
    for subtree in (generation / "src", generation / "venv"):
        for current, directories, files in os.walk(subtree, followlinks=False):
            current_path = Path(current)
            for name in [*directories, *files]:
                object_stat = (current_path / name).lstat()
                if object_stat.st_uid != expected_uid:
                    return "release_content_owner_invalid"
                if (
                    not stat.S_ISLNK(object_stat.st_mode)
                    and stat.S_IMODE(object_stat.st_mode) & 0o022
                ):
                    return "release_content_writable"
            for name in files:
                object_path = current_path / name
                object_stat = object_path.lstat()
                if stat.S_ISREG(object_stat.st_mode):
                    actual[object_path.relative_to(generation).as_posix()] = (
                        stat.S_IMODE(object_stat.st_mode),
                        hashlib.sha256(object_path.read_bytes()).hexdigest(),
                    )
    if not actual or actual != expected:
        return "release_content_mismatch"
    return None


def _resolve_release_sha() -> tuple[str | None, str | None]:
    """Read an immutable deploy identity without granting runtime Git access."""

    configured_path = os.environ.get("BOT_RELEASE_METADATA_PATH", "").strip()
    configured_root = os.environ.get("BOT_RELEASE_ROOT", "").strip()
    configured_owner = os.environ.get("BOT_RELEASE_METADATA_OWNER", "").strip()
    if not configured_path:
        return None, "release_metadata_path_unset"
    if not configured_root:
        return None, "release_root_unset"
    if not configured_owner:
        return None, "release_metadata_owner_unset"

    path = Path(configured_path)
    root = Path(configured_root)
    if not path.is_absolute() or not root.is_absolute():
        return None, "release_metadata_path_not_absolute"
    current = root / "current"
    if path != current / "release.json":
        return None, "release_metadata_path_outside_current"
    try:
        expected_uid = pwd.getpwnam(configured_owner).pw_uid
    except KeyError:
        return None, "release_metadata_owner_unknown"

    try:
        root_stat = root.lstat()
        generations = root / "generations"
        generations_stat = generations.lstat()
        current_stat = current.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or not stat.S_ISDIR(
            generations_stat.st_mode
        ):
            return None, "release_root_invalid"
        if not stat.S_ISLNK(current_stat.st_mode):
            return None, "release_current_not_symlink"
        if (
            root_stat.st_uid != expected_uid
            or generations_stat.st_uid != expected_uid
            or current_stat.st_uid != expected_uid
        ):
            return None, "release_metadata_owner_invalid"
        if (
            stat.S_IMODE(root_stat.st_mode) & 0o022
            or stat.S_IMODE(generations_stat.st_mode) & 0o022
        ):
            return None, "release_root_writable"

        target = os.readlink(current)
        target_match = re.fullmatch(r"generations/([0-9a-f]{40})", target)
        if target_match is None:
            return None, "release_current_target_invalid"
        target_sha = target_match.group(1)
        generation = root / "generations" / target_sha
        generation_stat = generation.lstat()
        source_stat = (generation / "src").lstat()
        venv_stat = (generation / "venv").lstat()
        if not all(
            stat.S_ISDIR(item.st_mode)
            for item in (generation_stat, source_stat, venv_stat)
        ):
            return None, "release_generation_invalid"
        if any(
            item.st_uid != expected_uid
            for item in (generation_stat, source_stat, venv_stat)
        ):
            return None, "release_metadata_owner_invalid"
        if any(
            stat.S_IMODE(item.st_mode) & 0o022
            for item in (generation_stat, source_stat, venv_stat)
        ):
            return None, "release_generation_writable"

        for marker in (
            generation / ".release-sha",
            generation / "src/.release-source-sha",
        ):
            marker_stat = marker.lstat()
            if (
                not stat.S_ISREG(marker_stat.st_mode)
                or marker_stat.st_uid != expected_uid
            ):
                return None, "release_generation_marker_invalid"
            if stat.S_IMODE(marker_stat.st_mode) & 0o022 or marker_stat.st_size > 128:
                return None, "release_generation_marker_invalid"
            if marker.read_text(encoding="ascii").strip() != target_sha:
                return None, "release_generation_marker_invalid"

        resolved = path.resolve(strict=True)
        expected_metadata = generation / "release.json"
        if resolved != expected_metadata:
            return None, "release_metadata_generation_mismatch"
        file_stat = resolved.lstat()
        if not stat.S_ISREG(file_stat.st_mode):
            return None, "release_metadata_not_regular"
        if file_stat.st_uid != expected_uid:
            return None, "release_metadata_owner_invalid"
        if stat.S_IMODE(file_stat.st_mode) & 0o022:
            return None, "release_metadata_writable"
        if file_stat.st_size > 4096:
            return None, "release_metadata_too_large"
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        if path.resolve(strict=True) != resolved:
            return None, "release_generation_changed"
    except FileNotFoundError:
        return None, "release_metadata_missing"
    except PermissionError:
        return None, "release_metadata_unreadable"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "release_metadata_invalid"

    if (
        not isinstance(payload, dict)
        or payload.get("format") != "discord-bot-release-v2"
    ):
        return None, "release_metadata_format_invalid"
    release_sha = payload.get("release_sha")
    if not isinstance(release_sha, str):
        return None, "release_sha_invalid"
    if not _FULL_SHA.fullmatch(release_sha):
        return None, "release_sha_invalid"
    if release_sha != target_sha or payload.get("generation") != release_sha:
        return None, "release_metadata_generation_mismatch"
    try:
        content_error = _verify_release_content(generation, payload, expected_uid)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "release_content_manifest_invalid"
    if content_error is not None:
        return None, content_error
    return release_sha, None


_RELEASE_STATE_LOCK = threading.Lock()
_RELEASE_SHA: str | None = None
_RELEASE_ERROR: str | None = "release_verification_pending"
_RELEASE_VERIFICATION_STARTED = False


def initialize_release_verification() -> tuple[str | None, str | None]:
    """Run the full immutable-generation verification once.

    HealthServer invokes this function in a worker thread only after its
    loopback listener has been bound.  Keeping the default full verification
    inside ``_resolve_release_sha`` preserves the direct-call security contract
    without blocking module import or the liveness endpoint.
    """

    global _RELEASE_ERROR, _RELEASE_SHA, _RELEASE_VERIFICATION_STARTED
    with _RELEASE_STATE_LOCK:
        if _RELEASE_VERIFICATION_STARTED:
            return _RELEASE_SHA, _RELEASE_ERROR
        _RELEASE_VERIFICATION_STARTED = True

    try:
        release_sha, release_error = _resolve_release_sha()
    except Exception as exc:  # fail closed on an unexpected verifier defect
        log.exception("release generation verification failed unexpectedly")
        release_sha = None
        release_error = f"release_verification_failed:{type(exc).__name__}"

    with _RELEASE_STATE_LOCK:
        _RELEASE_SHA = release_sha
        _RELEASE_ERROR = release_error
        return _RELEASE_SHA, _RELEASE_ERROR


def uptime_seconds() -> int:
    return int(time.monotonic() - _started_at)


def _check_discord(bot) -> dict[str, Any]:
    if bot is None:
        return {"ok": False, "reason": "bot_not_initialized"}
    try:
        is_ready = bool(bot.is_ready())
        accepting_jobs = getattr(bot, "accepting_jobs", None)
        accepting = (
            bool(accepting_jobs()) if callable(accepting_jobs) else True
        )
        latency = bot.latency if is_ready else None
        # discord.py の latency は初回 heartbeat ack 前に float("nan") を返す。
        # ready な gateway の latency 不明は「不明」であって障害ではない。
        latency_ms = (
            int(latency * 1000)
            if isinstance(latency, (int, float)) and math.isfinite(latency)
            else None
        )
        status = {
            "ok": is_ready and accepting,
            "gateway_latency_ms": latency_ms,
        }
        if is_ready and not accepting:
            status["reason"] = "shutdown_draining"
        return status
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
        return {"ok": False, "reason": "oauth_module_unavailable"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def _check_mem0() -> dict[str, Any]:
    """Probe the configured Chroma service without issuing an embedding query.

    Importing the Mem0 package does not prove that its storage process is
    reachable.  The default server-mode deployment therefore requires a valid
    Chroma heartbeat.  Embedded mode remains available for recovery and checks
    that the configured storage directory exists.
    """
    try:
        mode = os.environ.get("CHROMA_MODE", "server").lower()
        if mode == "embedded":
            from config import STATE_DIR

            storage_path = os.environ.get(
                "MEM0_EMBEDDED_PATH", str(STATE_DIR / "mem0_db")
            )
            if not os.path.isdir(storage_path):
                return {"ok": False, "reason": "embedded_store_missing"}
            return {"ok": True, "mode": "embedded"}
        if mode != "server":
            return {"ok": False, "reason": f"unsupported_chroma_mode:{mode}"}

        host = os.environ.get("CHROMA_HOST", "127.0.0.1")
        port = int(os.environ.get("CHROMA_PORT", "8765"))
        timeout = float(os.environ.get("HEALTH_MEM0_TIMEOUT_SECONDS", "2"))
        if timeout <= 0:
            return {"ok": False, "reason": "invalid_timeout"}
        url = f"http://{host}:{port}/api/v2/heartbeat"
        request = Request(url, headers={"Accept": "application/json"})
        # host and port are trusted operator configuration, not request input.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            if response.status != 200:
                return {"ok": False, "reason": f"heartbeat_http_{response.status}"}
            payload = json.loads(response.read())
        if not isinstance(payload, dict) or not any(
            "heartbeat" in str(key).lower() for key in payload
        ):
            return {"ok": False, "reason": "invalid_heartbeat_payload"}
        return {"ok": True, "mode": "server"}
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        return {"ok": False, "reason": f"heartbeat_failed:{type(e).__name__}"}
    except (ValueError, json.JSONDecodeError) as e:
        return {"ok": False, "reason": f"heartbeat_invalid:{type(e).__name__}"}
    except Exception as e:
        log.exception("unexpected Mem0 readiness failure")
        return {"ok": False, "reason": f"unexpected:{type(e).__name__}"}


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


def _check_release() -> dict[str, Any]:
    with _RELEASE_STATE_LOCK:
        release_sha = _RELEASE_SHA
        release_error = _RELEASE_ERROR
    if release_sha is None:
        return {"ok": False, "sha": None, "reason": release_error}
    return {"ok": True, "sha": release_sha}


def get_liveness_snapshot() -> dict[str, Any]:
    """Return process-only liveness without touching external dependencies."""
    return {
        "status": "live",
        "uptime_s": uptime_seconds(),
    }


def get_readiness_snapshot(bot=None) -> dict[str, Any]:
    """Compose the dependency readiness snapshot used by ``/ready``."""
    discord_st = _check_discord(bot)
    oauth_st = _check_oauth()
    mem0_st = _check_mem0()
    system_st = _check_system()
    release_st = _check_release()
    from services.codex_app_server import readiness as codex_readiness
    claude_required = codex_readiness.claude_required()
    codex_st = codex_readiness.check() if os.environ.get('DEFAULT_LLM_PROVIDER', '').lower() == 'codex' else None
    if not claude_required:
        oauth_st['required'] = False
    overall_ok = (
        discord_st["ok"]
        and (oauth_st["ok"] or not claude_required)
        and (codex_st is None or codex_st['ok'])
        and mem0_st["ok"]
        and system_st["ok"]
        and release_st["ok"]
    )
    return {
        "status": "ok" if overall_ok else "degraded",
        "uptime_s": uptime_seconds(),
        "build_sha": release_st["sha"],
        "release_sha": release_st["sha"],
        "subsystems": {
            "discord": discord_st,
            "oauth": oauth_st,
            "mem0": mem0_st,
            "system": system_st,
            "release": release_st,
            **({'codex': codex_st} if codex_st is not None else {}),
        },
    }


def get_health_snapshot(bot=None) -> dict[str, Any]:
    """Compatibility alias for callers that still consume ``/health``."""
    return get_readiness_snapshot(bot=bot)


__all__ = [
    "get_health_snapshot",
    "get_liveness_snapshot",
    "get_readiness_snapshot",
    "initialize_release_verification",
    "uptime_seconds",
]
