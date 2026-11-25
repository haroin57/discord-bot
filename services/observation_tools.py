"""MCP-facing observation tools: log_observation / query_observations / daily_summary.

Registered on the mem0 MCP server by ``mcp_mem0_server.py``.  Every call
resolves its subject through ``services.observation_scope`` before touching
the store, and returns a short text for the model.  Nothing here logs values:
health numbers only travel back to the conversation that owns them.

The store path must be configured explicitly (``OBSERVATIONS_DB_PATH``; the
bot stamps it per request).  Without it the tools refuse instead of creating a
second, divergent database somewhere else.

Dates: an explicit YYYY-MM-DD is used as given.  An empty date and
today/yesterday/今日/昨日/一昨日 count back from the JST day the user's Discord
message was sent (the snowflake time of the bot-stamped ``OBS_SOURCE_MSG_ID``),
so a retry or a reply that finishes after midnight still resolves to the same
day and deduplicates.  Without a bot-stamped Discord message id (Codex/HTTP
route, ``req:`` ids, ids passed by the model) the JST wall clock is used.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from datetime import date as _date

from services.observation_scope import (
    DB_ENV,
    SOURCE_ENV,
    ObservationScopeError,
    resolve_source_msg_id,
    resolve_subject,
)
from services.observations import (
    ObservationError,
    discord_message_day,
    format_daily_summary,
    format_row,
    get_store,
    parse_day,
)

log = logging.getLogger(__name__)

DEFAULT_USER_ID = "573489206240870410"  # 先輩 (same default as the mem0 tools)
_MAX_QUERY_ROWS = 500


class ObservationUnavailable(RuntimeError):
    """The observation log is not configured for this process."""


def tools_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Register the tools on stdio always; on the shared HTTP daemon only with a DB path.

    The bot stamps the DB path into its stdio children, so they always
    advertise the tools (and refuse with an explicit reason if the stamp is
    missing).  The shared daemon serves Codex and Claude Code; without a
    configured path there is no store it may write, so the tools stay hidden.
    """
    env = os.environ if env is None else env
    if str(env.get("MCP_HTTP_PORT") or "").strip():
        return bool(str(env.get(DB_ENV) or "").strip())
    return True


async def _store(env: Mapping[str, str]):
    path = str(env.get(DB_ENV) or "").strip()
    if not path:
        raise ObservationUnavailable(
            "the observation log is not configured for this client (OBSERVATIONS_DB_PATH unset)"
        )
    # The first open creates the file and schema and can wait out another
    # process's write lock (busy timeout): keep that off the event loop, which
    # also serves the other mem0 tools of this MCP server.
    return await asyncio.to_thread(get_store, path)


def _reference_day(env: Mapping[str, str], today: _date | None) -> _date | None:
    """The day relative dates count from (None = the store's JST wall clock).

    Only the bot-stamped message id is used: an id the model passes is not
    trusted for provenance, so it does not move dates either.
    """
    if today is not None:
        return today
    return discord_message_day(env.get(SOURCE_ENV))


def _where(row: dict) -> str:
    parts = [p for p in (row.get("meal_slot"), row.get("item")) if p]
    return f" ({' / '.join(parts)})" if parts else ""


async def _guard(call):
    try:
        return await call()
    except ObservationScopeError as exc:
        return f"(refused: {exc})"
    except ObservationUnavailable as exc:
        return f"(unavailable: {exc})"
    except ObservationError as exc:
        return f"(rejected: {exc})"
    except Exception as exc:  # content-free: never echo values or paths
        log.warning("observation tool failed: %s", type(exc).__name__)
        return f"(observation log error: {type(exc).__name__})"


async def log_observation(
    *,
    metric: str,
    value=None,
    unit: str = "",
    basis: str = "",
    observed_date: str = "",
    meal_slot: str | None = None,
    item: str | None = None,
    value_low=None,
    value_high=None,
    inputs: str = "",
    method: str = "",
    supersedes: str = "",
    retract: bool = False,
    user_id: str = "",
    source_msg_id: str = "",
    env: Mapping[str, str] | None = None,
    header_speaker: str | None = None,
    today: _date | None = None,
) -> str:
    env = os.environ if env is None else env

    async def call() -> str:
        subject = resolve_subject(user_id, env, default_user_id=DEFAULT_USER_ID,
                                  header_speaker=header_speaker)
        source, idempotent = resolve_source_msg_id(source_msg_id, env)
        store = await _store(env)
        result = await asyncio.to_thread(
            store.log, subject, metric=metric, source_msg_id=source,
            value=value, unit=unit or None, basis=basis or None,
            observed_date=observed_date or None,
            # None (omitted) = not given: a correction keeps the old slot/item.
            # "" clears it (a meal row that was really the day's total).
            meal_slot=meal_slot, item=item,
            value_low=value_low, value_high=value_high, inputs=inputs or None,
            method=method or None, supersedes=supersedes or None, retract=bool(retract),
            today=_reference_day(env, today),
        )
        row = result.row
        if result.status == "duplicate":
            later = (f"; it was corrected since, latest is {result.head_id}"
                     if result.head_id and result.head_id != row["id"] else "")
            if result.same_payload:
                return f"already logged from this message (no change): {_describe(row)}{later}"
            target = result.head_id or row["id"]
            return (f"(rejected: this message already logged {row['metric']}{_where(row)} as "
                    f"{_describe(row)}; to change it call again with supersedes=\"{target}\")")
        if row.get("value") is None:
            text = f"retracted {row['supersedes']} (record {row['id']})"
        elif row.get("supersedes"):
            text = f"corrected {row['supersedes']} -> {format_row(row)}"
        else:
            text = f"logged {format_row(row)}"
        if not idempotent:
            text += " (no message id available: a retry would log it twice)"
        return text

    return await _guard(call)


def _describe(row: dict) -> str:
    if row.get("value") is None:
        return f"retraction id={row['id']}"
    return format_row(row)


async def query_observations(
    *,
    metric: str = "",
    date_from: str = "",
    date_to: str = "",
    user_id: str = "",
    limit: int = 100,
    env: Mapping[str, str] | None = None,
    header_speaker: str | None = None,
    today: _date | None = None,
) -> str:
    env = os.environ if env is None else env

    async def call() -> str:
        subject = resolve_subject(user_id, env, default_user_id=DEFAULT_USER_ID,
                                  header_speaker=header_speaker)
        reference = _reference_day(env, today)
        start = parse_day(date_from, today=reference).isoformat() if date_from else None
        end = parse_day(date_to, today=reference).isoformat() if date_to else None
        try:
            cap = max(1, min(int(limit), _MAX_QUERY_ROWS))
        except (TypeError, ValueError):
            cap = 100
        store = await _store(env)
        rows = await asyncio.to_thread(store.query, subject, metric or None, start, end,
                                       limit=cap + 1)
        span = f"{start or '…'}..{end or '…'}"
        if not rows:
            return f"(no observations for {metric or 'any metric'} in {span})"
        shown = rows[:cap]
        lines = [f"{len(shown)} observation(s), {metric or 'all metrics'}, {span} (latest values only):"]
        lines += [f"- {format_row(r)}" for r in shown]
        if len(rows) > cap:
            lines.append("(more rows exist — narrow the date range or pick one metric)")
        return "\n".join(lines)

    return await _guard(call)


async def daily_summary(
    *,
    date: str = "",
    user_id: str = "",
    env: Mapping[str, str] | None = None,
    header_speaker: str | None = None,
    today: _date | None = None,
) -> str:
    env = os.environ if env is None else env

    async def call() -> str:
        subject = resolve_subject(user_id, env, default_user_id=DEFAULT_USER_ID,
                                  header_speaker=header_speaker)
        day = parse_day(date or None, today=_reference_day(env, today)).isoformat()
        store = await _store(env)
        summary = await asyncio.to_thread(store.daily_summary, subject, day)
        return format_daily_summary(day, summary)

    return await _guard(call)


__all__ = [
    "DEFAULT_USER_ID", "ObservationUnavailable", "daily_summary", "log_observation",
    "query_observations", "tools_enabled",
]
