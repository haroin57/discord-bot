"""LRU session manager and durable Discord-to-Codex thread mapping."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import time
import tomllib
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config import (
    BOT_DIR,
    CODEX_APP_SERVER_APPROVAL_POLICY,
    CODEX_APP_SERVER_CWD,
    CODEX_APP_SERVER_DANGEROUSLY_BYPASS_APPROVALS_AND_SANDBOX,
    CODEX_APP_SERVER_INHERIT_MCP_SERVERS,
    CODEX_APP_SERVER_MAX_SESSIONS,
    CODEX_APP_SERVER_MEM0_MCP_URL,
    CODEX_APP_SERVER_PATH,
    CODEX_APP_SERVER_POST_TOOL_TIMEOUT_S,
    CODEX_APP_SERVER_SANDBOX,
    CODEX_APP_SERVER_STATE_PATH,
    CODEX_APP_SERVER_TIMEOUT_S,
    CODEX_APP_SERVER_USE_LEGACY_LANDLOCK,
    CODEX_BIN,
)
from services.codex_app_server.session import CodexSession, CodexTurnResult
from utils.atomic_write import atomic_write_json

log = logging.getLogger(__name__)

_STATE_VERSION = 1
_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9_@-]{1,120}$")


def _key(channel_id: int, user_id: int | None) -> str:
    return f"{int(channel_id)}:{int(user_id or 0)}"


def _toml_string(value: str) -> str:
    # JSON basic strings are also valid TOML basic strings for these URLs and
    # model slugs. This keeps quoting out of shell parsing because Popen gets an
    # argv list directly.
    return json.dumps(value, ensure_ascii=False)


def _safe_mcp_url(url: object) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    # Credentials must never be exposed in the app-server command line.
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    return value


def _global_codex_config() -> dict[str, Any]:
    codex_home = Path(
        os.environ.get("CODEX_HOME", "~/.codex"),
    ).expanduser()
    try:
        with (codex_home / "config.toml").open("rb") as handle:
            value = tomllib.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def configured_mcp_endpoints() -> dict[str, str]:
    """Return the bot's explicit, credential-free MCP endpoint allowlist."""
    endpoints: dict[str, str] = {}
    mem0_url = _safe_mcp_url(CODEX_APP_SERVER_MEM0_MCP_URL)
    if mem0_url:
        endpoints["kanae-mem0"] = mem0_url

    mcp_path = Path(BOT_DIR) / ".mcp.json"
    try:
        raw = json.loads(mcp_path.read_text(encoding="utf-8"))
        for name, config in (raw.get("mcpServers") or {}).items():
            if not isinstance(name, str) or not _MCP_NAME_RE.fullmatch(name):
                continue
            if not isinstance(config, dict):
                continue
            url = _safe_mcp_url(config.get("url"))
            if url:
                endpoints[name] = url
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Codex MCP config read failed; continuing without it: %s", exc)

    global_servers = _global_codex_config().get("mcp_servers") or {}
    if not isinstance(global_servers, dict):
        global_servers = {}
    for name in CODEX_APP_SERVER_INHERIT_MCP_SERVERS:
        if not _MCP_NAME_RE.fullmatch(name):
            log.warning("Ignoring unsafe inherited Codex MCP server name: %r", name)
            continue
        server_config = global_servers.get(name)
        if not isinstance(server_config, dict):
            log.warning("Inherited Codex MCP server is not configured: %s", name)
            continue
        url = _safe_mcp_url(server_config.get("url"))
        if not url:
            log.warning("Inherited Codex MCP server has no safe URL: %s", name)
            continue
        endpoints[name] = url
    return endpoints


def build_app_server_extra_args(model: str) -> list[str]:
    """Build explicit model + credential-free MCP config overrides.

    The running Discord service has a deliberately narrow PATH and also owns
    API keys for unrelated providers.  The client handles the environment;
    this function removes the separate hidden dependency on the user's global
    Codex model and attaches only URL-based MCP daemons whose credentials stay
    in their own systemd services.
    """
    # Config overrides are layered onto ~/.codex/config.toml. Reset the table
    # first so the Discord bot exposes only its audited URL endpoints instead
    # of inheriting unrelated personal/global MCP servers.
    args: list[str] = [
        "-c", "mcp_servers={}",
        "-c", "features.apps=false",
        "-c", "features.remote_plugin=false",
        "-c", "apps._default.enabled=false",
    ]
    if CODEX_APP_SERVER_USE_LEGACY_LANDLOCK:
        args.extend(["--enable", "use_legacy_landlock"])
    args.extend(["-c", f"model={_toml_string(model)}"])
    endpoints = configured_mcp_endpoints()

    for name, url in sorted(endpoints.items()):
        args.extend([
            "-c",
            f"mcp_servers.{name}.url={_toml_string(url)}",
        ])

    # Empty-table overrides merge with lower config layers, so explicitly
    # disable every personal/global server and plugin not owned by this bot.
    # Only names, never credentials or values, are read from the user config.
    global_config = _global_codex_config()
    for name in sorted((global_config.get("mcp_servers") or {}).keys()):
        if name not in endpoints and _MCP_NAME_RE.fullmatch(str(name)):
            args.extend(["-c", f"mcp_servers.{name}.enabled=false"])
    for name in sorted((global_config.get("plugins") or {}).keys()):
        if _PLUGIN_NAME_RE.fullmatch(str(name)):
            args.extend(["-c", f"plugins.{name}.enabled=false"])
    return args


def build_app_server_global_args() -> list[str]:
    """Return global CLI flags that must precede the app-server subcommand."""
    if CODEX_APP_SERVER_DANGEROUSLY_BYPASS_APPROVALS_AND_SANDBOX:
        return ["--dangerously-bypass-approvals-and-sandbox"]
    return []


class CodexSessionManager:
    def __init__(
        self,
        *,
        state_path: str = CODEX_APP_SERVER_STATE_PATH,
        max_sessions: int = CODEX_APP_SERVER_MAX_SESSIONS,
    ) -> None:
        self.state_path = state_path
        self.max_sessions = max(1, int(max_sessions))
        self._state = self._load_state()
        self._sessions: OrderedDict[str, CodexSession] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._channel_locks: dict[int, asyncio.Lock] = {}
        self._active_keys: set[str] = set()
        self._state_lock = asyncio.Lock()

    def _load_state(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
            if data.get("version") != _STATE_VERSION:
                return {}
            sessions = data.get("sessions") or {}
            if not isinstance(sessions, dict):
                return {}
            result: dict[str, dict[str, Any]] = {}
            for key, value in sessions.items():
                if (
                    isinstance(key, str)
                    and isinstance(value, dict)
                    and isinstance(value.get("thread_id"), str)
                    and value["thread_id"].strip()
                ):
                    result[key] = dict(value)
            return result
        except (OSError, ValueError, TypeError):
            return {}

    async def _save_state(self) -> None:
        async with self._state_lock:
            # Serialize an immutable snapshot. Other channel/user turns can
            # update ``_state`` while the blocking fsync runs in a worker.
            payload = {
                "version": _STATE_VERSION,
                "sessions": copy.deepcopy(self._state),
            }
            await asyncio.to_thread(
                atomic_write_json,
                self.state_path,
                payload,
                mode=0o600,
            )

    def has_thread(self, channel_id: int, user_id: int | None) -> bool:
        return _key(channel_id, user_id) in self._state

    def has_live_session(self, channel_id: int, user_id: int | None) -> bool:
        session = self._sessions.get(_key(channel_id, user_id))
        return bool(session is not None and session.is_alive)

    def context(self, channel_id: int, user_id: int | None) -> dict[str, Any] | None:
        value = self._state.get(_key(channel_id, user_id))
        return dict(value) if value else None

    async def run_turn(
        self,
        channel_id: int,
        user_id: int | None,
        prompt: Any,
        *,
        model: str,
        effort: str,
        developer_instructions: str,
        resume_fallback_context: str = "",
        **callbacks: Any,
    ) -> CodexTurnResult:
        key = _key(channel_id, user_id)
        channel_key = int(channel_id)
        channel_lock = self._channel_locks.setdefault(channel_key, asyncio.Lock())
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with channel_lock, lock:
            session = self._sessions.get(key)
            requested_instructions_hash = hashlib.sha256(
                developer_instructions.encode("utf-8", "replace")
            ).hexdigest()
            current_instructions_hash = str(
                getattr(session, "_manager_instructions_hash", "") or ""
            )
            instructions_changed = bool(
                session is not None
                and session.is_alive
                and current_instructions_hash
                and current_instructions_hash != requested_instructions_hash
            )
            model_changed = bool(
                session is not None
                and session.is_alive
                and getattr(session, "resolved_model", "")
                and getattr(session, "resolved_model", "") != model
            )
            if (
                session is not None
                and session.is_alive
                and (model_changed or instructions_changed)
            ):
                # Re-resume the same durable thread through a fresh app-server
                # so thread/resume applies a newly requested model or updated
                # developer instructions. turn/start alone cannot replace the
                # thread-level developerInstructions contract.
                if session.thread_id:
                    self._state.setdefault(key, {})["thread_id"] = session.thread_id
                log.info(
                    "Restarting Codex app-server for thread contract change "
                    "key=%s model_changed=%s instructions_changed=%s",
                    key,
                    model_changed,
                    instructions_changed,
                )
                await session.close()
                self._sessions.pop(key, None)
                session = None
            if session is None or not session.is_alive:
                if session is not None:
                    await session.close()
                stored = self._state.get(key) or {}
                session = CodexSession(
                    cwd=CODEX_APP_SERVER_CWD,
                    codex_bin=CODEX_BIN,
                    stored_thread_id=stored.get("thread_id"),
                    global_args=build_app_server_global_args(),
                    extra_args=build_app_server_extra_args(model),
                    sandbox=CODEX_APP_SERVER_SANDBOX,
                    approval_policy=CODEX_APP_SERVER_APPROVAL_POLICY,
                    turn_timeout_s=CODEX_APP_SERVER_TIMEOUT_S,
                    post_tool_timeout_s=CODEX_APP_SERVER_POST_TOOL_TIMEOUT_S,
                    child_env={"PATH": CODEX_APP_SERVER_PATH},
                )
                self._sessions[key] = session
            self._sessions.move_to_end(key)

            try:
                self._active_keys.add(key)
                try:
                    result = await session.run_turn(
                        prompt,
                        model=model,
                        effort=effort,
                        developer_instructions=developer_instructions,
                        resume_fallback_context=resume_fallback_context,
                        **callbacks,
                    )
                    setattr(
                        session,
                        "_manager_instructions_hash",
                        requested_instructions_hash,
                    )
                finally:
                    self._active_keys.discard(key)
            except BaseException:
                # Preserve the durable thread id so a fresh app-server can
                # resume after a crash/auth repair, but retire the possibly
                # wedged process immediately.
                if session.thread_id:
                    state = self._state.setdefault(key, {})
                    state["thread_id"] = session.thread_id
                    state["updated_at"] = time.time()
                    await self._save_state()
                await session.close()
                self._sessions.pop(key, None)
                raise

            self._state[key] = {
                "thread_id": result.thread_id,
                "model": result.model,
                "model_provider": result.model_provider,
                "sandbox": result.sandbox,
                "approval_policy": result.approval_policy,
                "updated_at": time.time(),
                "token_usage_last": result.token_usage_last,
                "token_usage_total": result.token_usage_total,
                "model_context_window": result.model_context_window,
                "compacted": result.compacted,
            }
            await self._save_state()
            await self._evict_if_needed()
            return result

    async def _evict_if_needed(self) -> None:
        while len(self._sessions) > self.max_sessions:
            candidate = next(
                (
                    candidate_key
                    for candidate_key in self._sessions
                    if candidate_key not in self._active_keys
                ),
                None,
            )
            if candidate is None:
                # Temporary overflow is safer than terminating an in-flight
                # Discord turn. The next completed turn retries eviction.
                return
            session = self._sessions.pop(candidate)
            await session.close()
            log.info("Evicted idle Codex app-server session key=%s", candidate)

    async def clear_channel(self, channel_id: int) -> int:
        channel_key = int(channel_id)
        channel_lock = self._channel_locks.setdefault(channel_key, asyncio.Lock())
        async with channel_lock:
            prefix = f"{channel_key}:"
            keys = {
                key for key in set(self._state) | set(self._sessions)
                if key.startswith(prefix)
            }
            for key in keys:
                lock = self._locks.setdefault(key, asyncio.Lock())
                async with lock:
                    session = self._sessions.pop(key, None)
                    if session is not None:
                        await session.close()
                    self._state.pop(key, None)
                    self._active_keys.discard(key)
                self._locks.pop(key, None)
            if keys:
                await self._save_state()
            return len(keys)

    async def clear_thread(self, channel_id: int, user_id: int | None) -> bool:
        channel_key = int(channel_id)
        key = _key(channel_key, user_id)
        channel_lock = self._channel_locks.setdefault(channel_key, asyncio.Lock())
        async with channel_lock:
            if key not in self._state and key not in self._sessions:
                return False
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                session = self._sessions.pop(key, None)
                if session is not None:
                    await session.close()
                self._state.pop(key, None)
                self._active_keys.discard(key)
            self._locks.pop(key, None)
            await self._save_state()
            return True

    async def close_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            try:
                await session.close()
            except Exception:
                log.debug("Codex session close failed", exc_info=True)


_manager: CodexSessionManager | None = None


def get_manager() -> CodexSessionManager:
    global _manager
    if _manager is None:
        _manager = CodexSessionManager()
    return _manager


async def clear_codex_channel(channel_id: int) -> int:
    return await get_manager().clear_channel(channel_id)


async def clear_codex_thread(channel_id: int, user_id: int | None) -> bool:
    return await get_manager().clear_thread(channel_id, user_id)


async def close_all_codex_sessions() -> None:
    if _manager is not None:
        await _manager.close_all()
