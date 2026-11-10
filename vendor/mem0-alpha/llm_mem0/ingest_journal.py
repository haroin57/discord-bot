"""Durable ingestion/confirmation checkpoints, never best-effort indices."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from .settings import settings

_INIT_LOCK = threading.RLock()
_INITIALIZED = {}


def namespace() -> str:
    return hashlib.sha256(json.dumps([settings.CHROMA_MODE, settings.CHROMA_HOST,
        settings.CHROMA_PORT, settings.MEM0_COLLECTION_NAME]).encode()).hexdigest()


@contextmanager
def connection():
    directory = Path(settings.STATE_DIR)
    path = directory / "mem0_quality_journal.sqlite3"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        created = False
    identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
    if created:
        os.fsync(fd)
    os.close(fd)
    if created:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if path.is_symlink():
        raise ValueError("quality journal must not be a symlink")
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.execute("PRAGMA synchronous=FULL")
        with _INIT_LOCK:
            if _INITIALIZED.get(str(path)) != identity:
                db.execute("CREATE TABLE IF NOT EXISTS confirmation_events (namespace TEXT, fact_id TEXT, event_id TEXT, revision INTEGER NOT NULL, patch TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(namespace,fact_id,event_id))")
                db.execute("CREATE TABLE IF NOT EXISTS extraction_jobs (key TEXT PRIMARY KEY, input_hash TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0, signals TEXT, result TEXT, complete INTEGER NOT NULL DEFAULT 0)")
                db.commit()
                _INITIALIZED[str(path)] = identity
        yield db


def event_recorded(fact_id: str, event_id: str | None) -> bool:
    if not event_id:
        return False
    with connection() as db:
        row = db.execute("SELECT applied FROM confirmation_events WHERE namespace=? AND fact_id=? AND event_id=?",
                         (namespace(), fact_id, event_id)).fetchone()
    return bool(row and row[0])


def pending_events(fact_id: str) -> list[tuple]:
    with connection() as db:
        rows = db.execute("SELECT event_id,revision,patch FROM confirmation_events WHERE namespace=? AND fact_id=? AND applied=0 ORDER BY revision,event_id",
                          (namespace(), fact_id)).fetchall()
    return [(event, revision, json.loads(patch)) for event, revision, patch in rows]


def plan_event(fact_id: str, event_id: str, revision: int, patch: dict, *, applied=False):
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO confirmation_events VALUES (?,?,?,?,?,?)",
                   (namespace(), fact_id, event_id, revision, json.dumps(patch, allow_nan=False), int(applied)))


def finish_event(fact_id: str, event_id: str):
    with connection() as db:
        db.execute("UPDATE confirmation_events SET applied=1 WHERE namespace=? AND fact_id=? AND event_id=?",
                   (namespace(), fact_id, event_id))


def job_key(user_id: str, source_id: str) -> str:
    return hashlib.sha256(json.dumps([namespace(), str(user_id), source_id]).encode()).hexdigest()


@asynccontextmanager
async def job_lock(key: str):
    def open_lock():
        directory = Path(settings.STATE_DIR) / "extraction_job_locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.open(directory / (key + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fd = open_lock()  # Short open; cancellation cannot strand an executor FD.
    try:
        deadline = time.monotonic() + 120
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("extraction job is busy")
                await asyncio.sleep(0.02)
        yield
    finally:
        os.close(fd)


def read_job(key: str, input_hash: str):
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO extraction_jobs(key,input_hash) VALUES (?,?)", (key, input_hash))
        row = db.execute("SELECT input_hash,cursor,signals,result,complete FROM extraction_jobs WHERE key=?", (key,)).fetchone()
        if row[0] != input_hash:
            raise ValueError("request_id_input_mismatch")
    return {"cursor": row[1], "signals": json.loads(row[2]) if row[2] else None,
            "result": json.loads(row[3]) if row[3] else None, "complete": bool(row[4])}


def has_job(key: str) -> bool:
    if not (Path(settings.STATE_DIR) / "mem0_quality_journal.sqlite3").exists():
        return False
    with connection() as db:
        return db.execute("SELECT 1 FROM extraction_jobs WHERE key=?", (key,)).fetchone() is not None


def cache_signals(key: str, signals: dict):
    with connection() as db:
        db.execute("UPDATE extraction_jobs SET signals=? WHERE key=?",
                   (json.dumps(signals, ensure_ascii=False, allow_nan=False), key))


def advance_job(key: str, cursor: int, result: dict, *, complete: bool):
    with connection() as db:
        db.execute("UPDATE extraction_jobs SET cursor=?,signals=NULL,result=?,complete=? WHERE key=?",
                   (cursor, json.dumps(result, ensure_ascii=False, allow_nan=False), int(complete), key))
