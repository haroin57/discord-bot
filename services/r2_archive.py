"""Durable Cloudflare R2 archive for transcripts, state, and conversation images.

The Discord hot path never talks to R2 directly.  It first copies an object
into a local, fsync-backed spool under ``.state/r2_archive``.  A background
worker uploads queued objects with Wrangler.  A Cloudflare outage therefore
cannot make a local transcript write or Discord reply fail, and temporary
rendered images remain available after their original files are deleted.

Object layout (schema v1)::

    <prefix>/history/events/<channel>/YYYY/MM/DD/<timestamp>-<id>.json
    <prefix>/history/transcripts/<channel>.jsonl
    <prefix>/images/<channel>/YYYY/MM/DD/<message>/<direction>/<hash>-<name>
    <prefix>/state/latest/<relative .state path>
    <prefix>/state/manifests/YYYY/MM/DD/<timestamp>.json

Historical, whole-tree disaster-recovery archives remain owned by
``scripts/snapshot_state.sh`` under ``snapshots/``.  This service adds the
searchable/per-file layer and does not delete remote objects.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import config
from utils.atomic_write import atomic_write_json
from utils.image_validator import identify_image_mime

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_lock = threading.RLock()
_flush_lock = threading.Lock()
_state_sync_lock = threading.Lock()
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_TRANSIENT_NAMES = {"bot.pid", "spinner_emoji.txt"}
_TRANSIENT_SUFFIXES = {".lock", ".wal", ".shm", "-wal", "-shm"}
_EXCLUDED_TOP_DIRS = {"r2_archive", "eval", "agy_workdir", "codex_workdir"}


@dataclass(frozen=True)
class R2ObjectRef:
    bucket: str
    key: str
    kind: str
    sha256: str
    size: int
    content_type: str
    created_at: str
    channel_id: int | None = None
    message_id: int | None = None
    direction: str | None = None

    @property
    def uri(self) -> str:
        return f"r2://{self.bucket}/{self.key}"


def is_enabled() -> bool:
    return bool(getattr(config, "R2_ARCHIVE_ENABLED", False))


def _bucket() -> str:
    value = str(getattr(config, "R2_ARCHIVE_BUCKET", "kanae-state-backup")).strip()
    if not value or "/" in value:
        raise ValueError("R2_ARCHIVE_BUCKET must be a non-empty bucket name")
    return value


def _prefix() -> str:
    value = str(getattr(config, "R2_ARCHIVE_PREFIX", "discord-bot/v1")).strip(" /")
    if not value or ".." in value.split("/"):
        raise ValueError("R2_ARCHIVE_PREFIX must be a safe object prefix")
    return value


def _spool_dir() -> Path:
    return Path(config.STATE_DIR) / "r2_archive"


def _pending_dir() -> Path:
    return _spool_dir() / "pending"


def _catalog_path() -> Path:
    return _spool_dir() / "catalog.jsonl"


def _state_index_path() -> Path:
    return _spool_dir() / "state_index.json"


@contextmanager
def _process_lock(name: str):
    """Non-blocking inter-process lock for worker/CLI coexistence."""
    root = _spool_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f"{_safe_component(name, fallback='operation')}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_component(value: str, *, fallback: str = "object", limit: int = 120) -> str:
    safe = _SAFE_COMPONENT_RE.sub("_", value.strip()).strip("._-")
    return (safe or fallback)[:limit]


def _content_type(path: Path) -> str:
    try:
        with path.open("rb") as f:
            head = f.read(512)
        image_type = identify_image_mime(head, allow_svg=True)
        if image_type:
            return image_type
    except OSError:
        pass
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _sqlite_backup_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(fd)
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
        dst = sqlite3.connect(tmp_name)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append_catalog(record: dict[str, Any]) -> None:
    path = _catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _queue_payload(
    *,
    key: str,
    kind: str,
    content_type: str,
    source: Path | None = None,
    data: bytes | None = None,
    sqlite_backup: bool = False,
    metadata: dict[str, Any] | None = None,
) -> R2ObjectRef:
    if not is_enabled():
        raise RuntimeError("R2 archive is disabled")
    if (source is None) == (data is None):
        raise ValueError("exactly one of source or data is required")

    queue_id = f"{time.time_ns()}-{uuid.uuid4().hex}"
    pending = _pending_dir()
    payload_path = pending / f"{queue_id}.payload"
    manifest_path = pending / f"{queue_id}.json"
    created_at = _iso_now()

    with _lock:
        pending.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source is not None:
            if sqlite_backup:
                _sqlite_backup_atomic(source, payload_path)
            else:
                _copy_file_atomic(source, payload_path)
        else:
            payload_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{payload_path.name}.", dir=pending)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(data or b"")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, payload_path)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

        digest = _sha256_file(payload_path)
        size = payload_path.stat().st_size
        meta = dict(metadata or {})
        ref = R2ObjectRef(
            bucket=_bucket(),
            key=key,
            kind=kind,
            sha256=digest,
            size=size,
            content_type=content_type,
            created_at=created_at,
            channel_id=meta.get("channel_id"),
            message_id=meta.get("message_id"),
            direction=meta.get("direction"),
        )
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "queue_id": queue_id,
            "bucket": ref.bucket,
            "key": ref.key,
            "kind": kind,
            "content_type": content_type,
            "payload": payload_path.name,
            "sha256": digest,
            "size": size,
            "created_at": created_at,
            "attempts": 0,
            "next_attempt_at": 0.0,
            "metadata": meta,
        }
        atomic_write_json(manifest_path, manifest, indent=None)
        _append_catalog({**asdict(ref), "uri": ref.uri, "metadata": meta})
        return ref


def _queue_json(
    key: str,
    payload: Any,
    *,
    kind: str,
    metadata: dict[str, Any] | None = None,
) -> R2ObjectRef:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _queue_payload(
        key=key,
        kind=kind,
        content_type="application/json; charset=utf-8",
        data=data,
        metadata=metadata,
    )


def archive_history_entry(channel_id: int, entry: dict[str, Any]) -> R2ObjectRef | None:
    """Stage one transcript event as independently addressable JSON."""
    if not is_enabled():
        return None
    copied = dict(entry)
    copied.setdefault("channel_id", channel_id)
    copied.setdefault("archive_event_id", uuid.uuid4().hex)
    timestamp = str(copied.get("timestamp") or _iso_now())
    now = _utc_now()
    stamp = _safe_component(timestamp.replace(":", "").replace("+", "_"), limit=40)
    event_id = _safe_component(str(copied["archive_event_id"]), limit=40)
    key = (
        f"{_prefix()}/history/events/{channel_id}/"
        f"{now:%Y/%m/%d}/{stamp}-{event_id}.json"
    )
    return _queue_json(
        key,
        copied,
        kind="history_event",
        metadata={"channel_id": channel_id, "role": copied.get("role")},
    )


def archive_image(
    path: str | os.PathLike[str],
    *,
    channel_id: int,
    message_id: int,
    direction: str,
    author_id: int | None = None,
    source: str = "discord",
) -> R2ObjectRef | None:
    """Stage a conversation image and an adjacent JSON metadata object."""
    if not is_enabled():
        return None
    image_path = Path(path)
    if not image_path.is_file():
        raise FileNotFoundError(image_path)
    content_type = _content_type(image_path)
    if not content_type.startswith("image/"):
        raise ValueError(f"not an image: {image_path}")
    direction = _safe_component(direction, fallback="unknown", limit=24)
    now = _utc_now()
    digest = _sha256_file(image_path)
    filename = _safe_component(image_path.name, fallback=f"image{image_path.suffix}")
    key = (
        f"{_prefix()}/images/{channel_id}/{now:%Y/%m/%d}/{message_id}/"
        f"{direction}/{digest[:20]}-{filename}"
    )
    metadata = {
        "channel_id": channel_id,
        "message_id": message_id,
        "direction": direction,
        "author_id": author_id,
        "source": source,
        "original_name": image_path.name,
    }
    ref = _queue_payload(
        key=key,
        kind="image",
        content_type=content_type,
        source=image_path,
        metadata=metadata,
    )
    _queue_json(
        f"{key}.metadata.json",
        {**asdict(ref), "uri": ref.uri, "metadata": metadata},
        kind="image_metadata",
        metadata=metadata,
    )
    return ref


def archive_images(
    paths: Iterable[str | os.PathLike[str]],
    *,
    channel_id: int,
    message_id: int,
    direction: str,
    author_id: int | None = None,
    source: str = "discord",
) -> list[R2ObjectRef]:
    refs: list[R2ObjectRef] = []
    if not is_enabled():
        return refs
    for path in dict.fromkeys(str(p) for p in paths):
        try:
            ref = archive_image(
                path,
                channel_id=channel_id,
                message_id=message_id,
                direction=direction,
                author_id=author_id,
                source=source,
            )
            if ref:
                refs.append(ref)
        except (OSError, ValueError) as exc:
            log.warning("R2 image staging failed for %s: %s", path, exc)
    return refs


def _state_path_excluded(relative: Path) -> bool:
    if not relative.parts:
        return True
    if relative.parts[0] in _EXCLUDED_TOP_DIRS:
        return True
    name = relative.name
    if name in _TRANSIENT_NAMES or any(name.endswith(s) for s in _TRANSIENT_SUFFIXES):
        return True
    if ".bak" in name or ".broken." in name or "_flush_backup_" in name:
        return True
    return False


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _load_state_index() -> dict[str, str]:
    try:
        data = json.loads(_state_index_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _state_object_key(relative: Path) -> tuple[str, str]:
    rel = relative.as_posix()
    if relative.parts and relative.parts[0] == "history":
        name = _safe_component(relative.name, fallback="transcript.jsonl", limit=180)
        group = "cleared" if ".cleared-" in name else "transcripts"
        return f"{_prefix()}/history/{group}/{name}", "history_transcript"
    return f"{_prefix()}/state/latest/{rel}", "state_file"


def sync_state_once() -> dict[str, Any]:
    """Stage changed ``.state`` files and a manifest; never deletes R2 data."""
    if not is_enabled():
        return {"enabled": False, "changed": 0, "deleted": 0, "errors": []}
    if not _state_sync_lock.acquire(blocking=False):
        return {
            "enabled": True,
            "changed": 0,
            "deleted": 0,
            "errors": [],
            "skipped": "state sync already running",
        }
    try:
        with _process_lock("state-sync") as acquired:
            if not acquired:
                return {
                    "enabled": True,
                    "changed": 0,
                    "deleted": 0,
                    "errors": [],
                    "skipped": "state sync already running in another process",
                }
            return _sync_state_once_locked()
    finally:
        _state_sync_lock.release()


def _sync_state_once_locked() -> dict[str, Any]:
    root = Path(config.STATE_DIR)
    previous = _load_state_index()
    current: dict[str, str] = {}
    changed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if _state_path_excluded(relative):
            continue
        rel = relative.as_posix()
        try:
            fingerprint = _fingerprint(path)
        except OSError as exc:
            errors.append({"path": rel, "error": str(exc)})
            continue
        current[rel] = fingerprint
        if previous.get(rel) == fingerprint:
            continue
        key, kind = _state_object_key(relative)
        try:
            ref = _queue_payload(
                key=key,
                kind=kind,
                content_type=_content_type(path),
                source=path,
                sqlite_backup=path.suffix.lower() in _SQLITE_SUFFIXES,
                metadata={"state_path": rel, "fingerprint": fingerprint},
            )
            changed.append({
                "path": rel,
                "key": ref.key,
                "sha256": ref.sha256,
                "size": ref.size,
            })
        except Exception as exc:  # one corrupt/locked file must not stop the scan
            errors.append({"path": rel, "error": str(exc)})
            current.pop(rel, None)  # retry this file on the next scan

    deleted = sorted(set(previous) - set(current))
    generated_at = _iso_now()
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": generated_at,
        "state_root": str(root),
        "changed": changed,
        "deleted_since_previous_manifest": deleted,
        "errors": errors,
        "remote_deletes_performed": False,
    }
    stamp = _utc_now()
    manifest_name = stamp.strftime("%Y%m%dT%H%M%S.%fZ")
    _queue_json(
        f"{_prefix()}/state/manifests/{stamp:%Y/%m/%d}/{manifest_name}.json",
        manifest,
        kind="state_manifest",
    )
    _queue_json(
        f"{_prefix()}/state/latest-manifest.json",
        manifest,
        kind="state_manifest_latest",
    )
    atomic_write_json(_state_index_path(), current)
    return {
        "enabled": True,
        "changed": len(changed),
        "deleted": len(deleted),
        "errors": errors,
        "manifest": manifest_name,
    }


def _wrangler_binary() -> str:
    configured = str(getattr(config, "R2_ARCHIVE_WRANGLER_BIN", "")).strip()
    candidates = [
        configured,
        shutil.which("wrangler") or "",
        str(Path.home() / ".volta" / "bin" / "wrangler"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError("wrangler executable not found")


def _wrangler_env() -> dict[str, str]:
    env = os.environ.copy()
    volta_bin = str(Path.home() / ".volta" / "bin")
    env["PATH"] = f"{volta_bin}:{env.get('PATH', '')}"
    return env


def _run_wrangler(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    command = [_wrangler_binary(), *args]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout or float(getattr(config, "R2_ARCHIVE_COMMAND_TIMEOUT_S", 300)),
        check=False,
        env=_wrangler_env(),
    )


def _load_queue_manifest(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def flush_pending_once(*, limit: int | None = None) -> dict[str, Any]:
    """Upload one bounded batch from the durable spool."""
    if not is_enabled():
        return {"enabled": False, "uploaded": 0, "failed": 0, "pending": 0}
    if not _flush_lock.acquire(blocking=False):
        status = pending_status()
        return {
            "enabled": True,
            "uploaded": 0,
            "failed": 0,
            "pending": status["pending"],
            "skipped": "flush already running",
        }
    try:
        with _process_lock("flush") as acquired:
            if not acquired:
                status = pending_status()
                return {
                    "enabled": True,
                    "uploaded": 0,
                    "failed": 0,
                    "pending": status["pending"],
                    "skipped": "flush already running in another process",
                }
            return _flush_pending_once_locked(limit=limit)
    finally:
        _flush_lock.release()


def _flush_pending_once_locked(*, limit: int | None = None) -> dict[str, Any]:
    limit = limit or int(getattr(config, "R2_ARCHIVE_FLUSH_BATCH", 20))
    pending_dir = _pending_dir()
    pending_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    uploaded = 0
    failed = 0
    now = time.time()

    for manifest_path in sorted(pending_dir.glob("*.json"))[:limit]:
        manifest = _load_queue_manifest(manifest_path)
        if not manifest:
            failed += 1
            continue
        if float(manifest.get("next_attempt_at", 0.0) or 0.0) > now:
            continue
        payload_name = Path(str(manifest.get("payload", ""))).name
        payload_path = pending_dir / payload_name
        if not payload_path.is_file():
            manifest["last_error"] = "spool payload missing"
            manifest["attempts"] = int(manifest.get("attempts", 0)) + 1
            atomic_write_json(manifest_path, manifest, indent=None)
            failed += 1
            continue
        args = [
            "r2", "object", "put",
            f"{manifest['bucket']}/{manifest['key']}",
            "--file", str(payload_path),
            "--content-type", str(manifest.get("content_type") or "application/octet-stream"),
            "--remote",
        ]
        try:
            result = _run_wrangler(args)
        except (OSError, subprocess.SubprocessError) as exc:
            result = None
            error = str(exc)
        else:
            error = (result.stderr or result.stdout or "wrangler upload failed").strip()
        if result is not None and result.returncode == 0:
            try:
                _append_catalog({
                    "kind": "delivery",
                    "key": manifest.get("key"),
                    "source_kind": manifest.get("kind"),
                    "uploaded_at": _iso_now(),
                })
            except OSError as exc:
                log.warning("R2 delivery catalog update failed: %s", exc)
            try:
                manifest_path.unlink()
                payload_path.unlink()
            except OSError as exc:
                log.warning("R2 spool cleanup failed for %s: %s", manifest.get("key"), exc)
            uploaded += 1
            continue
        attempts = int(manifest.get("attempts", 0)) + 1
        manifest["attempts"] = attempts
        manifest["last_error"] = error[-1000:]
        manifest["next_attempt_at"] = time.time() + min(3600.0, 5.0 * (2 ** min(attempts, 9)))
        atomic_write_json(manifest_path, manifest, indent=None)
        failed += 1
        log.warning("R2 upload deferred key=%s attempt=%d: %s", manifest.get("key"), attempts, error[-300:])

    return {
        "enabled": True,
        "uploaded": uploaded,
        "failed": failed,
        "pending": len(list(pending_dir.glob("*.json"))),
    }


def pending_status() -> dict[str, Any]:
    manifests = list(_pending_dir().glob("*.json")) if _pending_dir().exists() else []
    payload_bytes = 0
    for path in _pending_dir().glob("*.payload") if _pending_dir().exists() else ():
        try:
            payload_bytes += path.stat().st_size
        except OSError:
            pass
    return {
        "enabled": is_enabled(),
        "bucket": _bucket() if is_enabled() else None,
        "prefix": _prefix() if is_enabled() else None,
        "pending": len(manifests),
        "pending_bytes": payload_bytes,
    }


async def archive_worker() -> None:
    """Continuously sync state and drain the local R2 spool."""
    if not is_enabled():
        log.info("R2 archive disabled")
        return
    flush_interval = max(1.0, float(getattr(config, "R2_ARCHIVE_FLUSH_INTERVAL_S", 5)))
    state_interval = max(60.0, float(getattr(config, "R2_ARCHIVE_STATE_INTERVAL_S", 3600)))
    next_state_sync = 0.0
    log.info("R2 archive worker started: bucket=%s prefix=%s", _bucket(), _prefix())
    while True:
        try:
            now = time.monotonic()
            if now >= next_state_sync:
                report = await asyncio.to_thread(sync_state_once)
                log.info(
                    "R2 state scan staged changed=%d deleted=%d errors=%d",
                    report.get("changed", 0), report.get("deleted", 0), len(report.get("errors", [])),
                )
                next_state_sync = time.monotonic() + state_interval
            await asyncio.to_thread(flush_pending_once)
            await asyncio.sleep(flush_interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("R2 archive worker iteration failed: %s", exc, exc_info=True)
            await asyncio.sleep(min(60.0, flush_interval * 2))


def normalize_object_key(value: str) -> str:
    value = value.strip()
    marker = f"r2://{_bucket()}/"
    if value.startswith("r2://"):
        if not value.startswith(marker):
            raise ValueError("R2 URI references a different bucket")
        value = value[len(marker):]
    value = value.lstrip("/")
    prefix = _prefix()
    if not (value == prefix or value.startswith(f"{prefix}/")):
        raise ValueError("object key is outside the configured archive prefix")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("invalid R2 object key")
    return value


def fetch_object(value: str, destination: str | os.PathLike[str]) -> Path:
    """Fetch one archived object through Wrangler into ``destination``."""
    if not is_enabled():
        raise RuntimeError("R2 archive is disabled")
    key = normalize_object_key(value)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Wrangler refuses to overwrite in some versions.  Callers may reserve a
    # secure tempfile name first, so remove that empty placeholder immediately
    # before the authenticated download.
    try:
        if target.exists() and target.stat().st_size == 0:
            target.unlink()
    except OSError:
        pass
    result = _run_wrangler([
        "r2", "object", "get", f"{_bucket()}/{key}",
        "--file", str(target), "--remote",
    ])
    if result.returncode != 0:
        try:
            target.unlink()
        except OSError:
            pass
        detail = (result.stderr or result.stdout or "wrangler download failed").strip()
        raise RuntimeError(detail[-1000:])
    os.chmod(target, 0o600)
    return target


def recent_images(channel_id: int, *, limit: int = 10) -> list[dict[str, Any]]:
    """Return newest locally catalogued image references for a channel."""
    limit = max(1, min(int(limit), 50))
    try:
        with _catalog_path().open(encoding="utf-8") as f:
            tail = deque(f, maxlen=5000)
    except OSError:
        return []
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    delivered = {
        row.get("key")
        for line in tail
        for row in [_parse_catalog_line(line)]
        if row.get("kind") == "delivery" and row.get("key")
    }
    for line in reversed(tail):
        row = _parse_catalog_line(line)
        key = row.get("key")
        if row.get("kind") != "image" or row.get("channel_id") != channel_id or not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        row["upload_status"] = "uploaded" if key in delivered else "pending"
        results.append(row)
        if len(results) >= limit:
            break
    return results


def _parse_catalog_line(line: str) -> dict[str, Any]:
    try:
        row = json.loads(line)
        return row if isinstance(row, dict) else {}
    except json.JSONDecodeError:
        return {}
