"""Who may read/write whose observations, decided outside the model.

Investigation (2026-10-15).  The model never chose the speaker safely before:
every mem0 tool takes a free ``user_id`` that defaults to 先輩's id, and the
mem0 MCP server had no idea whose turn it was.  The bot reaches the mem0 tools
through two transports, and each can carry the speaker without the model:

1. Claude route (``services.claude_api.entry.call_with_tools``): the mem0
   server is a stdio child spawned per request by a fresh ``McpPool`` inside
   the request's own ``AsyncExitStack`` (the Agent sub-agent reuses that pool).
   ``scope_mcp_configs`` stamps that one spawn's env with
   ``OBS_SPEAKER_USER_ID`` (Discord id of the message author),
   ``OBS_SOURCE_MSG_ID`` (``discord:<message id>`` from
   ``current_source_message_id``, or a per-request token) and
   ``OBSERVATIONS_DB_PATH`` (the bot's STATE_DIR, not the MCP ``.state``
   namespace the child otherwise uses).  The static config sets
   ``OBS_SPEAKER_USER_ID="-"``, so any spawn path that skips the stamping
   (mcp probes, eval workers, future callers) fails closed.
2. Codex route (prod default provider): Codex app-server talks to the shared
   streamable-HTTP daemon (``mcp-mem0-shared``, 127.0.0.1:8013).  The bot runs
   one app-server process per (channel, user) key, so it can attach a static
   ``X-Kanae-Obs-Speaker`` header to that process's MCP connection
   (``services.codex_app_server.manager.observation_speaker_args``).  No
   per-turn message id can be attached that way, so Codex-route writes are
   idempotent only when an explicit ``source_msg_id`` is passed.

``resolve_subject`` (server side) enforces: env speaker known -> only that
user; env speaker ``"-"``/invalid -> refuse; else header speaker -> only that
user; else (no bot in the loop: Claude Code sessions on the shared daemon or a
hand-started stdio server) -> only ``OBS_UNSCOPED_USER_IDS`` (default: 先輩's
id), so another user's health data is never reachable from those paths.

Limits: the header and the env are bindings against model confusion, not
authentication.  Any local process that can reach 127.0.0.1:8013 can claim a
speaker; the daemon is reachable only from this host.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Mapping
from contextvars import ContextVar

SPEAKER_ENV = "OBS_SPEAKER_USER_ID"
SOURCE_ENV = "OBS_SOURCE_MSG_ID"
DB_ENV = "OBSERVATIONS_DB_PATH"
UNSCOPED_ENV = "OBS_UNSCOPED_USER_IDS"
SPEAKER_HEADER = "X-Kanae-Obs-Speaker"
NO_SPEAKER = "-"
MEMORY_SERVER_KEY = "mem0"

_SNOWFLAKE = re.compile(r"^\d{15,22}$")

# Discord message id of the turn being answered; set by
# bot_pipeline.model.invoke_model around the provider call so entry.py can
# forward it without changing the provider call signature.
current_source_message_id: ContextVar[str | None] = ContextVar(
    "observation_source_message_id", default=None,
)


class ObservationScopeError(PermissionError):
    """The caller may not act on the requested user's observations."""


def _bound(requested: str, speaker: str, where: str) -> str:
    if not _SNOWFLAKE.match(speaker):
        raise ObservationScopeError(
            f"observation tools are unavailable here: the current speaker is unknown ({where})"
        )
    if requested and requested != speaker:
        # A privacy boundary, not an input error: no retry hint, or the model
        # would re-ask without user_id and present the speaker's own numbers
        # as the other person's.
        raise ObservationScopeError(
            "observations are private to each user: only the current speaker's own "
            "records can be read or written, and another user's are not available"
        )
    return speaker


def resolve_subject(requested: str | None, env: Mapping[str, str] | None = None,
                    *, default_user_id: str, header_speaker: str | None = None) -> str:
    """Return the user_id this call may act on, or raise ObservationScopeError.

    ``header_speaker`` is the ``X-Kanae-Obs-Speaker`` request header (HTTP
    transport only; ``None`` when absent).
    """
    env = os.environ if env is None else env
    requested = str(requested or "").strip()
    # No format check before the binding: a name like "父" is another person
    # too, and "invalid user_id" would invite the same retry without user_id
    # that _bound avoids.  The allow-list below already rejects any non-id.
    if SPEAKER_ENV in env:
        return _bound(requested, str(env.get(SPEAKER_ENV) or "").strip(), "process")
    if header_speaker is not None:
        return _bound(requested, str(header_speaker).strip(), "connection")
    allowed = {part.strip() for part in str(env.get(UNSCOPED_ENV) or default_user_id).split(",")
               if part.strip()}
    subject = requested or default_user_id
    if subject not in allowed:
        raise ObservationScopeError("this user's observations are not available from this client")
    return subject


def resolve_source_msg_id(explicit: str | None, env: Mapping[str, str] | None = None) -> tuple[str, bool]:
    """Return ``(source_msg_id, idempotent)``.

    The id injected by the bot wins over anything the model passes (the model
    never sees Discord message ids, and must not be able to forge provenance).
    Without either, a random id is used and retries are NOT deduplicated.
    """
    env = os.environ if env is None else env
    injected = str(env.get(SOURCE_ENV) or "").strip()
    if injected:
        return injected, True
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit, True
    return f"auto:{uuid.uuid4().hex}", False


def observations_db_path() -> str:
    """The bot's observation DB: ``OBSERVATIONS_DB_PATH`` or ``<STATE_DIR>/observations.sqlite3``."""
    explicit = os.environ.get(DB_ENV, "").strip()
    if explicit:
        return explicit
    import config

    from services.observations import DB_FILENAME

    return os.path.join(config.STATE_DIR, DB_FILENAME)


def scope_mcp_configs(
    configs: dict[str, dict],
    *,
    speaker_user_id: int | str | None,
    source_message_id: str | None = None,
    db_path: str | None = None,
) -> dict[str, dict]:
    """Copy ``configs`` with the mem0 child's env stamped for this request only."""
    cfg = configs.get(MEMORY_SERVER_KEY)
    if not isinstance(cfg, dict):
        return configs
    env = dict(cfg.get("env") or {})
    speaker = str(speaker_user_id).strip() if speaker_user_id is not None else ""
    env[SPEAKER_ENV] = speaker if _SNOWFLAKE.match(speaker) else NO_SPEAKER
    source = str(source_message_id or current_source_message_id.get() or "").strip()
    env[SOURCE_ENV] = f"discord:{source}" if source.isdigit() else f"req:{uuid.uuid4().hex}"
    env[DB_ENV] = db_path or observations_db_path()
    scoped = dict(configs)
    scoped[MEMORY_SERVER_KEY] = {**cfg, "env": env}
    return scoped


__all__ = [
    "DB_ENV", "NO_SPEAKER", "ObservationScopeError", "SOURCE_ENV", "SPEAKER_ENV",
    "SPEAKER_HEADER", "UNSCOPED_ENV", "current_source_message_id", "observations_db_path",
    "resolve_source_msg_id", "resolve_subject", "scope_mcp_configs",
]
