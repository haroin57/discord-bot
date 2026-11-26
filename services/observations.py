"""Numeric observation log (体重・食事 kcal/栄養素・歩数) kept OUTSIDE Mem0.

Mem0 facts are deduplicated, LLM-merged and conflict-archived; a time series of
measurements must not be (an older weight archived as "outdated" is still a
valid data point).  This module is the system of record for such numbers:

- one SQLite file (``observations.sqlite3``) under the bot STATE_DIR, WAL,
  ``busy_timeout``, a per-path thread lock and ``BEGIN IMMEDIATE`` writes
  (same pattern as ``services/work/store.py``);
- append-only: a correction is a NEW row whose ``supersedes`` names the row it
  replaces; rows are never updated in place.  Reads return the latest
  non-superseded rows ("heads").  A partial UNIQUE index allows at most one
  successor per row, so correction chains cannot fork.  A retraction is a
  correction row with ``value IS NULL``; a chain whose head is a retraction
  disappears from reads (the history stays for audit);
- idempotent on ``source_msg_id``: ``UNIQUE(source_msg_id, metric,
  observed_date, meal_slot, item, supersedes)``.  ``meal_slot`` / ``item`` /
  ``supersedes`` are ``NOT NULL DEFAULT ''`` because SQLite treats NULLs as
  distinct, which would silently disable the constraint.  ``observed_date`` is
  part of the key so one message can report two days ("昨日60.5、今日60.2");
  ``supersedes`` so a correction or a method-version recompute derived from
  the SAME source message can still append;
- ``observed_date`` is a JST calendar date.  An explicit YYYY-MM-DD is kept as
  given; an empty date and today/yesterday/今日/昨日/一昨日/おととい count back
  from the caller's reference day (``today=``; the observation tools pass the
  JST day the Discord message was sent, see ``discord_message_day``), else
  from the wall clock.  Dates after the reference day are rejected;
- every read and write is scoped by ``user_id``.  Who may act as which
  ``user_id`` is decided by the caller (see ``services/observation_scope.py``).

Estimates keep what was eaten (``inputs_json``) and how the number was derived
(``method``) so they can be recomputed later with a better method.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

JST = timezone(timedelta(hours=9))

BASES = ("user_stated", "label", "estimate")
MEAL_SLOTS = ("", "breakfast", "lunch", "dinner", "snack")
DB_FILENAME = "observations.sqlite3"
RETRACTED_METHOD = "retracted"
BUSY_TIMEOUT_S = 10.0  # how long one statement waits for another writer's lock

_UNIQUE_KEY = ("source_msg_id", "metric", "observed_date", "meal_slot", "item", "supersedes")
# The key of databases created before observed_date joined it (2026-10-16).
_UNIQUE_KEY_V1 = ("source_msg_id", "metric", "meal_slot", "item", "supersedes")
_COLUMNS = ("id", "user_id", "metric", "value", "value_low", "value_high", "unit", "basis",
            "observed_date", "meal_slot", "item", "inputs_json", "method", "source_msg_id",
            "supersedes", "created_at")

_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS observations (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    metric        TEXT NOT NULL,
    value         REAL,              -- NULL only on a retraction row
    value_low     REAL,
    value_high    REAL,
    unit          TEXT NOT NULL DEFAULT '',
    basis         TEXT NOT NULL CHECK (basis IN ('user_stated', 'label', 'estimate')),
    observed_date TEXT NOT NULL,       -- JST calendar date, YYYY-MM-DD
    meal_slot     TEXT NOT NULL DEFAULT '',
    item          TEXT NOT NULL DEFAULT '',
    inputs_json   TEXT NOT NULL DEFAULT '{{}}',
    method        TEXT NOT NULL DEFAULT '',
    source_msg_id TEXT NOT NULL,
    supersedes    TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    UNIQUE ({', '.join(_UNIQUE_KEY)})
)"""
_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS observations_user_metric_date"
    " ON observations (user_id, metric, observed_date)",
    "CREATE UNIQUE INDEX IF NOT EXISTS observations_one_successor"
    " ON observations (supersedes) WHERE supersedes != ''",
)
_SCHEMA = ";\n".join((_TABLE_SQL, *_INDEX_SQL)) + ";\n"


class ObservationError(ValueError):
    """Invalid observation input (message is safe to show to the model)."""


class ObservationConflict(ObservationError):
    """The idempotency key is already used in a way this call cannot reuse."""


@dataclass(frozen=True)
class MetricSpec:
    unit: str
    agg: str  # "sum" (meal nutrients, summed over items/slots) | "last" (state of the day)
    low: float | None = None
    high: float | None = None
    label: str = ""
    meal: bool = False


METRICS: dict[str, MetricSpec] = {
    "weight": MetricSpec("kg", "last", 20, 300, "体重"),
    "body_fat": MetricSpec("%", "last", 1, 75, "体脂肪率"),
    "kcal": MetricSpec("kcal", "sum", 0, 5000, "食事エネルギー", meal=True),
    "protein": MetricSpec("g", "sum", 0, 500, "たんぱく質", meal=True),
    "fat": MetricSpec("g", "sum", 0, 500, "脂質", meal=True),
    "carbs": MetricSpec("g", "sum", 0, 1000, "炭水化物", meal=True),
    "steps": MetricSpec("歩", "last", 0, 150000, "歩数"),
}

_METRIC_ALIASES = {
    "体重": "weight", "body_weight": "weight", "weight_kg": "weight",
    "体脂肪": "body_fat", "体脂肪率": "body_fat", "bodyfat": "body_fat",
    "energy": "kcal", "calories": "kcal", "calorie": "kcal", "meal_kcal": "kcal",
    "カロリー": "kcal", "エネルギー": "kcal",
    "protein_g": "protein", "たんぱく質": "protein", "タンパク質": "protein",
    "fat_g": "fat", "脂質": "fat",
    "carbs_g": "carbs", "carbohydrate": "carbs", "carbohydrates": "carbs", "炭水化物": "carbs",
    "step": "steps", "step_count": "steps", "歩数": "steps",
}

_UNIT_ALIASES = {
    "kg": "kg", "㎏": "kg", "ｋｇ": "kg", "キロ": "kg", "キログラム": "kg",
    "kcal": "kcal", "ｋｃａｌ": "kcal", "キロカロリー": "kcal", "カロリー": "kcal",
    "g": "g", "ｇ": "g", "グラム": "g",
    "歩": "歩", "step": "歩", "steps": "歩",
    "%": "%", "％": "%",
}

_MEAL_SLOT_ALIASES = {
    "": "", "none": "",
    "breakfast": "breakfast", "朝": "breakfast", "朝食": "breakfast", "朝ごはん": "breakfast", "朝ご飯": "breakfast",
    "lunch": "lunch", "昼": "lunch", "昼食": "lunch", "昼ごはん": "lunch", "昼ご飯": "lunch", "ランチ": "lunch",
    "dinner": "dinner", "夜": "dinner", "夕食": "dinner", "夕飯": "dinner", "晩ごはん": "dinner",
    "晩ご飯": "dinner", "夜ごはん": "dinner", "夜ご飯": "dinner",
    "snack": "snack", "間食": "snack", "おやつ": "snack", "夜食": "snack",
}

_METRIC_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9:._#@/\-]{1,160}$")
_OBS_ID_RE = re.compile(r"^obs_[0-9a-f]{20}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_ITEM = 80
_MAX_INPUTS = 2000
_MAX_METHOD = 120
_MAX_HISTORY_DAYS = 3660

_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()
_STORES: dict[str, "ObservationStore"] = {}


# ---------------------------------------------------------------------------
# Normalization (pure)
# ---------------------------------------------------------------------------
def jst_today(now: float | None = None) -> date:
    return datetime.fromtimestamp(time.time() if now is None else now, JST).date()


def jst_date_of(timestamp: str | float | int) -> date | None:
    """JST calendar date of an ISO8601 string or Unix seconds (None if unparsable)."""
    try:
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            return datetime.fromtimestamp(float(timestamp), JST).date()
        text = str(timestamp).strip()
        if re.fullmatch(r".*[+-]\d{4}$", text):  # time.strftime("%z") -> +0900
            text = text[:-2] + ":" + text[-2:]
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST).date()


DISCORD_EPOCH_MS = 1_420_070_400_000
_DISCORD_SOURCE_RE = re.compile(r"^discord:(\d{15,22})$")
_CLOCK_SKEW_S = 600


def discord_message_day(source_msg_id: str | None, *, now: float | None = None) -> date | None:
    """JST day a Discord message was sent, from a ``discord:<snowflake>`` source id.

    The reference day for relative dates: a retry after midnight, or a reply
    finished after midnight, still means the day the user wrote the message.
    None for any other id, or when the snowflake's time is more than 10 minutes
    ahead of the wall clock (the caller then falls back to the wall clock).
    """
    match = _DISCORD_SOURCE_RE.match(str(source_msg_id or "").strip())
    if not match:
        return None
    sent = ((int(match.group(1)) >> 22) + DISCORD_EPOCH_MS) / 1000
    if sent > (time.time() if now is None else now) + _CLOCK_SKEW_S:
        return None
    return datetime.fromtimestamp(sent, JST).date()


def normalize_metric(raw: str) -> str:
    text = str(raw or "").strip()
    key = _METRIC_ALIASES.get(text, _METRIC_ALIASES.get(text.lower(), text.lower()))
    if not _METRIC_RE.match(key):
        raise ObservationError(
            "metric must be one of " + ", ".join(METRICS) + " (or a snake_case name)"
        )
    return key


def metric_spec(metric: str) -> MetricSpec | None:
    return METRICS.get(metric)


def normalize_unit(metric: str, raw: str | None) -> str:
    spec = METRICS.get(metric)
    text = str(raw or "").strip()
    unit = _UNIT_ALIASES.get(text, _UNIT_ALIASES.get(text.lower(), text))
    if spec is not None:
        if not unit:
            return spec.unit
        if unit != spec.unit:
            raise ObservationError(f"{metric} must be recorded in {spec.unit} (got {text!r}); convert first")
        return unit
    if not unit or len(unit) > 16 or _CONTROL_RE.search(unit):
        raise ObservationError("unit is required for custom metrics (max 16 chars)")
    return unit


def normalize_meal_slot(metric: str, raw: str | None) -> str:
    text = str(raw or "").strip()
    slot = _MEAL_SLOT_ALIASES.get(text, _MEAL_SLOT_ALIASES.get(text.lower()))
    if slot is None:
        raise ObservationError("meal_slot must be one of breakfast, lunch, dinner, snack (or empty)")
    spec = METRICS.get(metric)
    if slot and spec is not None and not spec.meal:
        raise ObservationError(f"meal_slot is only for meal metrics, not {metric}")
    return slot


def normalize_item(raw: str | None) -> str:
    if raw is not None and not isinstance(raw, str):
        raise ObservationError("item must be text: one food name per row")
    text = _CONTROL_RE.sub(" ", raw or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _MAX_ITEM:
        raise ObservationError(f"item must be at most {_MAX_ITEM} characters")
    return text


def normalize_basis(raw: str | None) -> str:
    basis = str(raw or "user_stated").strip().lower()
    if basis not in BASES:
        raise ObservationError("basis must be user_stated, label or estimate")
    return basis


_RELATIVE_DAYS = {"": 0, "today": 0, "今日": 0, "yesterday": 1, "昨日": 1, "一昨日": 2, "おととい": 2}


def parse_day(raw: str | date | None, *, today: date | None = None) -> date:
    """YYYY-MM-DD, or today/yesterday/今日/昨日/一昨日 counted back from ``today``
    (the reference day; default: the JST wall clock), without range checks."""
    if isinstance(raw, date):
        return raw
    today = today or jst_today()
    text = str(raw or "").strip().lower()
    if text in _RELATIVE_DAYS:
        return today - timedelta(days=_RELATIVE_DAYS[text])
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ObservationError("dates must be YYYY-MM-DD (JST) or today/yesterday") from exc


def normalize_date(raw: str | date | None, *, today: date | None = None) -> str:
    """An observation date: parse_day plus 'not in the future, not absurdly old'."""
    today = today or jst_today()
    parsed = parse_day(raw, today=today)
    if parsed > today:
        raise ObservationError("observed_date is in the future (JST)")
    if (today - parsed).days > _MAX_HISTORY_DAYS:
        raise ObservationError("observed_date is too old")
    return parsed.isoformat()


def _finite(name: str, raw) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ObservationError(f"{name} must be a number")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ObservationError(f"{name} must be a number") from exc
    if not math.isfinite(value):
        raise ObservationError(f"{name} must be finite")
    return value


def normalize_values(metric: str, value, low, high) -> tuple[float, float | None, float | None]:
    v, lo, hi = _finite("value", value), _finite("value_low", low), _finite("value_high", high)
    if v is None:
        raise ObservationError("value is required (pass retract=true to withdraw a row)")
    if (lo is None) != (hi is None):
        raise ObservationError("value_low and value_high must be given together")
    if lo is not None and not (lo <= v <= hi):
        raise ObservationError("value_low <= value <= value_high is required")
    spec = METRICS.get(metric)
    if spec is not None:
        for name, number in (("value", v), ("value_low", lo), ("value_high", hi)):
            if number is None:
                continue
            if (spec.low is not None and number < spec.low) or (spec.high is not None and number > spec.high):
                raise ObservationError(
                    f"{name}={number:g}{spec.unit} is outside the plausible range for {metric}; check the unit"
                )
    elif v < 0:
        raise ObservationError("value must be >= 0")
    return v, lo, hi


def normalize_inputs(raw) -> str:
    if raw is None or raw == "":
        return "{}"
    if isinstance(raw, (dict, list)):
        payload = raw
    else:
        text = str(raw).strip()
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {"text": text}
        if not isinstance(payload, (dict, list)):
            payload = {"text": text}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(encoded) > _MAX_INPUTS:
        raise ObservationError(f"inputs must be at most {_MAX_INPUTS} characters")
    return encoded


def normalize_method(raw: str | None) -> str:
    text = _CONTROL_RE.sub(" ", str(raw or "")).strip()
    if len(text) > _MAX_METHOD:
        raise ObservationError(f"method must be at most {_MAX_METHOD} characters")
    return text


def validate_source_msg_id(raw: str) -> str:
    text = str(raw or "").strip()
    if not _SOURCE_RE.match(text):
        raise ObservationError("source_msg_id is required ([A-Za-z0-9:._#@/-], max 160)")
    return text


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def default_db_path() -> str:
    explicit = os.environ.get("OBSERVATIONS_DB_PATH", "").strip()
    if explicit:
        return explicit
    state_dir = os.environ.get("BOT_STATE_DIR", "").strip()
    if not state_dir:
        from config import STATE_DIR  # lazy: keeps this module importable without config

        state_dir = STATE_DIR
    return os.path.join(state_dir, DB_FILENAME)


def _path_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.Lock())


def get_store(path: str | os.PathLike | None = None) -> "ObservationStore":
    resolved = str(Path(path or default_db_path()).expanduser().resolve())
    with _PATH_LOCKS_GUARD:
        store = _STORES.get(resolved)
    if store is None:
        store = ObservationStore(resolved)
        with _PATH_LOCKS_GUARD:
            store = _STORES.setdefault(resolved, store)
    return store


def _unique_keys(conn: sqlite3.Connection) -> set[tuple[str, ...]]:
    """Column tuples of the table's UNIQUE constraints."""
    keys = set()
    for index in conn.execute("PRAGMA index_list(observations)").fetchall():
        if index["unique"] and index["origin"] == "u":
            columns = conn.execute(f'PRAGMA index_info("{index["name"]}")').fetchall()
            keys.add(tuple(column["name"] for column in columns))
    return keys


def _migrate_unique_key(conn: sqlite3.Connection) -> None:
    """Rebuild a table created with the v1 key (no observed_date) under the current key.

    The new key only adds a column, so every v1 row stays unique; rows and ids
    are copied unchanged.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _UNIQUE_KEY_V1 in _unique_keys(conn):  # another process may have done it
            columns = ", ".join(_COLUMNS)
            conn.execute("ALTER TABLE observations RENAME TO observations_v1")
            conn.execute(_TABLE_SQL)
            conn.execute(f"INSERT INTO observations ({columns}) SELECT {columns} FROM observations_v1")
            conn.execute("DROP TABLE observations_v1")  # drops the v1 indexes with it
            for statement in _INDEX_SQL:
                conn.execute(statement)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@dataclass(frozen=True)
class LogResult:
    status: str  # "inserted" | "duplicate"
    row: dict
    same_payload: bool = True
    head_id: str = ""  # latest row of the chain (differs from row["id"] once corrected)


def _payload_key(row: dict) -> tuple:
    return (row.get("value"), row.get("value_low"), row.get("value_high"), row.get("unit"),
            row.get("basis"), row.get("observed_date"))


class ObservationStore:
    """Append-only, user-scoped observation table in its own SQLite file."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Health data: create the file 0600 before SQLite does (WAL/SHM files
        # inherit the database file's permissions).
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        self._lock = _path_lock(self.path)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                if _UNIQUE_KEY_V1 in _unique_keys(conn):
                    _migrate_unique_key(conn)
            finally:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
            finally:
                conn.close()

    # -- writes -------------------------------------------------------------
    def log(
        self,
        user_id: str,
        *,
        metric: str,
        source_msg_id: str,
        value=None,
        unit: str | None = None,
        basis: str | None = None,
        observed_date: str | date | None = None,
        meal_slot: str | None = None,
        item: str | None = None,
        value_low=None,
        value_high=None,
        inputs=None,
        method: str | None = None,
        supersedes: str | None = None,
        retract: bool = False,
        today: date | None = None,
    ) -> LogResult:
        """Append one observation (or a correction/retraction of one).

        ``None`` means "not given": a correction inherits unit, basis, date,
        meal slot, item, inputs and method from the row it supersedes.
        ``today`` is the reference day for relative/empty dates and the latest
        allowed date (default: the JST wall clock).
        """
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ObservationError("user_id is required")
        source = validate_source_msg_id(source_msg_id)
        metric = normalize_metric(metric)
        target_id = str(supersedes or "").strip()
        if target_id and not _OBS_ID_RE.match(target_id):
            raise ObservationError("supersedes must be an observation id (obs_...)")
        if retract and not target_id:
            raise ObservationError("retract requires supersedes=<observation id>")
        with self._write() as conn:
            target = None
            if target_id:
                row = conn.execute("SELECT * FROM observations WHERE id=?", (target_id,)).fetchone()
                # Another user's row is reported exactly like a missing one.
                if row is None or row["user_id"] != user_id:
                    raise ObservationError(f"observation {target_id} not found")
                target = dict(row)
                if target["metric"] != metric:
                    raise ObservationError("a correction must keep the metric of the row it supersedes")

            def inherit(given, key):
                return target[key] if (given is None and target is not None) else given

            unit_n = normalize_unit(metric, inherit(unit, "unit"))
            basis_n = normalize_basis(inherit(basis or None, "basis"))
            date_raw = inherit(observed_date if observed_date not in ("", None) else None, "observed_date")
            date_n = normalize_date(date_raw, today=today)
            slot_n = normalize_meal_slot(metric, inherit(meal_slot, "meal_slot"))
            item_n = normalize_item(inherit(item, "item"))
            # inputs/method describe how the old number was obtained; carry them
            # over only while the basis stays the same.
            same_basis = target is not None and target["basis"] == basis_n
            inputs_given = inputs if inputs not in (None, "") else None
            inputs_n = (target["inputs_json"] if (inputs_given is None and same_basis)
                        else normalize_inputs(inputs_given))
            method_n = normalize_method(method if (method or not same_basis) else target["method"])
            if retract:
                v = lo = hi = None
                method_n = RETRACTED_METHOD
            else:
                if value in (None, "") and target is not None and target["value"] is not None:
                    # A correction of only the date / slot / item keeps the number.
                    value = target["value"]
                    if value_low in (None, "") and value_high in (None, ""):
                        value_low, value_high = target["value_low"], target["value_high"]
                v, lo, hi = normalize_values(metric, value, value_low, value_high)
                if basis_n == "estimate" and (inputs_n == "{}" or not method_n):
                    raise ObservationError(
                        "basis=estimate needs inputs (what/how much was eaten as the user said) "
                        "and method (estimation method/version) so it can be recomputed"
                    )

            existing = conn.execute(
                "SELECT * FROM observations WHERE source_msg_id=? AND metric=? AND observed_date=?"
                " AND meal_slot=? AND item=? AND supersedes=?",
                (source, metric, date_n, slot_n, item_n, target_id),
            ).fetchone()
            candidate = {"value": v, "value_low": lo, "value_high": hi, "unit": unit_n,
                         "basis": basis_n, "observed_date": date_n}
            if existing is not None:
                if existing["user_id"] != user_id:
                    raise ObservationConflict("this source message is already used for another user")
                existing_row = dict(existing)
                return LogResult("duplicate", existing_row,
                                 _payload_key(existing_row) == _payload_key(candidate),
                                 self._head_id(conn, existing_row["id"], user_id))
            if target is not None:
                newer = conn.execute(
                    "SELECT id FROM observations WHERE supersedes=? AND user_id=?",
                    (target_id, user_id),
                ).fetchone()
                if newer is not None:
                    head = self._head_id(conn, newer["id"], user_id)
                    raise ObservationError(
                        f"{target_id} was already corrected; supersede the latest row {head} instead"
                    )
            row = {
                "id": "obs_" + uuid.uuid4().hex[:20],
                "user_id": user_id,
                "metric": metric,
                **candidate,
                "meal_slot": slot_n,
                "item": item_n,
                "inputs_json": inputs_n,
                "method": method_n,
                "source_msg_id": source,
                "supersedes": target_id,
                "created_at": time.time(),
            }
            columns = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            conn.execute(f"INSERT INTO observations ({columns}) VALUES ({marks})", tuple(row.values()))
            return LogResult("inserted", row, True, row["id"])

    @staticmethod
    def _head_id(conn: sqlite3.Connection, start: str, user_id: str) -> str:
        current, seen = start, {start}
        while True:
            nxt = conn.execute(
                "SELECT id FROM observations WHERE supersedes=? AND user_id=?", (current, user_id),
            ).fetchone()
            if nxt is None or nxt["id"] in seen:
                return current
            current = nxt["id"]
            seen.add(current)

    # -- reads --------------------------------------------------------------
    # Heads = rows nobody supersedes.  A retraction head (value IS NULL) hides
    # its whole chain.
    _HEADS_SQL = (
        "SELECT o.* FROM observations o"
        " WHERE o.user_id = ?"
        " AND NOT EXISTS (SELECT 1 FROM observations s"
        "                 WHERE s.supersedes = o.id AND s.user_id = o.user_id)"
        " AND o.value IS NOT NULL"
    )

    def query(
        self,
        user_id: str,
        metric: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        *,
        limit: int = 500,
    ) -> list[dict]:
        """Latest non-superseded, non-retracted rows of ``user_id`` (date ascending)."""
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ObservationError("user_id is required")
        sql = self._HEADS_SQL
        params: list = [user_id]
        if metric:
            sql += " AND o.metric = ?"
            params.append(normalize_metric(metric))
        if date_from:
            sql += " AND o.observed_date >= ?"
            params.append(_iso_date(date_from))
        if date_to:
            sql += " AND o.observed_date <= ?"
            params.append(_iso_date(date_to))
        sql += " ORDER BY o.observed_date, o.created_at, o.id LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        with self._read() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def history(self, user_id: str, observation_id: str) -> list[dict]:
        """The correction chain containing ``observation_id`` (oldest first), user-scoped."""
        with self._read() as conn:
            row = conn.execute("SELECT * FROM observations WHERE id=? AND user_id=?",
                               (observation_id, user_id)).fetchone()
            if row is None:
                return []
            chain = [dict(row)]
            while chain[0]["supersedes"]:
                prev = conn.execute("SELECT * FROM observations WHERE id=? AND user_id=?",
                                    (chain[0]["supersedes"], user_id)).fetchone()
                if prev is None:
                    break
                chain.insert(0, dict(prev))
            current = chain[-1]["id"]
            while True:
                nxt = conn.execute("SELECT * FROM observations WHERE supersedes=? AND user_id=?",
                                   (current, user_id)).fetchone()
                if nxt is None:
                    break
                chain.append(dict(nxt))
                current = nxt["id"]
            return chain

    def user_ids(self) -> list[str]:
        with self._read() as conn:
            return [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM observations ORDER BY user_id")]

    def find_head(self, user_id: str, metric: str, observed_date: str, meal_slot: str, item: str) -> list[dict]:
        sql = (self._HEADS_SQL + " AND o.metric=? AND o.observed_date=? AND o.meal_slot=? AND o.item=?")
        with self._read() as conn:
            return [dict(r) for r in conn.execute(sql, (user_id, metric, observed_date, meal_slot, item))]

    def daily_summary(self, user_id: str, day: str) -> dict[str, "MetricDay"]:
        day_iso = _iso_date(day)
        return aggregate_days(self.query(user_id, None, day_iso, day_iso)).get(day_iso, {})


def _iso_date(raw) -> str:
    try:
        return date.fromisoformat(str(raw).strip()).isoformat()
    except ValueError as exc:
        raise ObservationError("dates must be YYYY-MM-DD") from exc


# ---------------------------------------------------------------------------
# Aggregation (pure)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MetricDay:
    metric: str
    unit: str
    value: float
    low: float | None
    high: float | None
    rows: int
    has_estimate: bool
    slots: tuple[tuple[str, float], ...] = ()
    ignored_slot_totals: int = 0
    # sum-metrics only: the user stated a whole-day total (meal_slot='' and
    # item='').  It is the day's value; the per-meal rows are then shown as a
    # breakdown (``breakdown_total``) instead of being added on top of it.
    stated_total: bool = False
    breakdown_total: float | None = None


def _agg_kind(metric: str, rows: list[dict]) -> str:
    spec = METRICS.get(metric)
    if spec is not None:
        return spec.agg
    return "sum" if any(r.get("meal_slot") or r.get("item") for r in rows) else "last"


def _latest(rows: list[dict]) -> dict:
    return max(rows, key=lambda r: (r.get("created_at") or 0, r.get("id") or ""))


def _bounds(rows: list[dict]) -> tuple[float | None, float | None]:
    """Summed low/high over ``rows`` (exact rows count as low=high=value)."""
    if not any(r.get("value_low") is not None for r in rows):
        return None, None
    low = sum(float(r["value_low"] if r.get("value_low") is not None else r["value"]) for r in rows)
    high = sum(float(r["value_high"] if r.get("value_high") is not None else r["value"]) for r in rows)
    return low, high


def aggregate_metric_day(metric: str, rows: list[dict]) -> MetricDay | None:
    """Aggregate head rows of one metric on one day.

    last-metrics (weight, body fat, steps): the most recently logged row.
    sum-metrics (meal kcal / nutrients):
      - within a meal slot, item rows win over a slot-level total row (an item
        breakdown plus the slot total would otherwise double count);
      - a row with neither meal_slot nor item is the user's stated whole-day
        total; the latest such row is the day's value and the meal rows are
        reported as a breakdown, never added to it.
    """
    rows = [r for r in rows if r.get("value") is not None]
    if not rows:
        return None
    unit = rows[-1].get("unit") or ""
    if _agg_kind(metric, rows) == "last":
        latest = _latest(rows)
        return MetricDay(metric, unit, float(latest["value"]), latest.get("value_low"),
                         latest.get("value_high"), len(rows), latest.get("basis") == "estimate")
    day_totals = [r for r in rows if not r.get("meal_slot") and not r.get("item")]
    parts = [r for r in rows if r.get("meal_slot") or r.get("item")]
    by_slot: dict[str, list[dict]] = {}
    for r in parts:
        by_slot.setdefault(r.get("meal_slot") or "", []).append(r)
    chosen: list[dict] = []
    slots: list[tuple[str, float]] = []
    ignored = 0
    for slot in sorted(by_slot, key=lambda s: MEAL_SLOTS.index(s) if s in MEAL_SLOTS else 99):
        slot_rows = by_slot[slot]
        items = [r for r in slot_rows if r.get("item")]
        picked = items or slot_rows
        ignored += len(slot_rows) - len(picked)
        chosen.extend(picked)
        slots.append((slot, sum(float(r["value"]) for r in picked)))
    breakdown = sum(v for _, v in slots)
    if day_totals:
        total = _latest(day_totals)
        low, high = total.get("value_low"), total.get("value_high")
        return MetricDay(metric, unit, float(total["value"]), low, high, len(rows),
                         total.get("basis") == "estimate", tuple(slots), ignored,
                         stated_total=True, breakdown_total=breakdown if parts else None)
    low, high = _bounds(chosen)
    return MetricDay(metric, unit, breakdown, low, high, len(rows),
                     any(r.get("basis") == "estimate" for r in chosen), tuple(slots), ignored)


def aggregate_days(rows: list[dict]) -> dict[str, dict[str, MetricDay]]:
    grouped: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        grouped.setdefault(r["observed_date"], {}).setdefault(r["metric"], []).append(r)
    out: dict[str, dict[str, MetricDay]] = {}
    for day, metrics in grouped.items():
        for metric, metric_rows in metrics.items():
            agg = aggregate_metric_day(metric, metric_rows)
            if agg is not None:
                out.setdefault(day, {})[metric] = agg
    return out


def _fmt(number: float) -> str:
    return f"{number:,.1f}".rstrip("0").rstrip(".") if abs(number) < 1000 else f"{number:,.0f}"


def format_row(row: dict) -> str:
    unit = row.get("unit") or ""
    value = f"{_fmt(row['value'])}{unit}"
    if row.get("value_low") is not None:
        value = f"~{value} ({_fmt(row['value_low'])}–{_fmt(row['value_high'])})"
    where = " / ".join(x for x in (row.get("meal_slot"), row.get("item")) if x)
    basis = row.get("basis", "")
    if basis == "estimate" and row.get("method"):
        basis += f": {row['method']}"
    parts = [row["observed_date"], row["metric"], value, f"[{basis}]"]
    if where:
        parts.append(f"({where})")
    parts.append(f"id={row['id']}")
    return " ".join(parts)


_SLOT_LABELS = {"": "区分なし", "breakfast": "朝食", "lunch": "昼食", "dinner": "夕食", "snack": "間食"}


def format_daily_summary(day: str, summary: dict[str, MetricDay]) -> str:
    if not summary:
        return f"(no observations on {day})"
    lines = [f"{day} の観測ログ集計:"]
    for metric in sorted(summary, key=lambda m: (list(METRICS).index(m) if m in METRICS else 99, m)):
        agg = summary[metric]
        label = METRICS[metric].label if metric in METRICS else metric
        text = f"- {label}: {_fmt(agg.value)}{agg.unit}"
        if agg.low is not None:
            text += f" (推定幅 {_fmt(agg.low)}–{_fmt(agg.high)})"
        if agg.slots and (agg.stated_total or any(s for s, _ in agg.slots)):
            text += " [" + ", ".join(f"{_SLOT_LABELS.get(s, s)} {_fmt(v)}" for s, v in agg.slots) + "]"
        notes = [f"{agg.rows}件"]
        if agg.stated_total:
            notes.append("本人申告の1日合計")
            if agg.breakdown_total is not None:
                notes.append(f"食事別の記録の合計は{_fmt(agg.breakdown_total)}{agg.unit}")
        if agg.has_estimate:
            notes.append("推定値を含む")
        if agg.ignored_slot_totals:
            notes.append(f"品目内訳のある区分の合計行{agg.ignored_slot_totals}件は除外")
        lines.append(text + " (" + ", ".join(notes) + ")")
    return "\n".join(lines)


__all__ = [
    "BASES", "JST", "MEAL_SLOTS", "METRICS", "LogResult", "MetricDay", "MetricSpec",
    "ObservationConflict", "ObservationError", "ObservationStore", "aggregate_days",
    "aggregate_metric_day", "default_db_path", "discord_message_day", "format_daily_summary",
    "format_row", "get_store", "jst_date_of", "jst_today", "normalize_date", "normalize_meal_slot",
    "normalize_metric", "normalize_unit", "parse_day",
]
