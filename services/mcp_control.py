"""Persistent MCP enablement and bounded health probes.

The bot does not keep MCP transports connected between model turns. This
module therefore separates persistent enablement, conversation selection,
and the latest temporary probe result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from utils.atomic_write import atomic_write_json

log = logging.getLogger(__name__)

_STATE_FILENAME = "mcp_control.json"
_STATE_VERSION = 1


@dataclass(frozen=True)
class McpProbeResult:
    key: str
    status: str
    tool_names: tuple[str, ...]
    checked_at: str
    error: str | None = None

    @property
    def tool_count(self) -> int:
        return len(self.tool_names)


@dataclass(frozen=True)
class McpMutation:
    target: str
    enabled: bool
    changed_keys: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.changed_keys)


async def _spawn_for_probe(stack: AsyncExitStack, key: str, cfg: dict):
    """Late import avoids a cycle with mcp_client.McpPool filtering."""

    from services.claude_api.mcp_client import _spawn_one

    return await _spawn_one(stack, key, cfg)


def _safe_probe_error(exc: BaseException) -> str:
    """Return an operator-safe error without secret-bearing exception text."""

    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError)):
        return "connection failed"
    return f"probe failed ({type(exc).__name__})"


class McpControlService:
    """MCP configuration view with persistent disabled-key overrides."""

    def __init__(
        self,
        servers: Mapping[str, dict] | None = None,
        *,
        state_path: str | Path | None = None,
        probe_timeout: float = 10.0,
    ) -> None:
        if servers is None:
            from config import MCP_SERVERS

            servers = MCP_SERVERS
        self._servers = dict(servers)
        self._explicit_state_path = Path(state_path) if state_path is not None else None
        self.probe_timeout = probe_timeout
        self._disabled: set[str] = set()
        self._loaded_path: Path | None = None
        self._probes: dict[str, McpProbeResult] = {}
        self._lock = threading.RLock()

    @property
    def server_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._servers))

    def _state_path(self) -> Path:
        if self._explicit_state_path is not None:
            return self._explicit_state_path
        from config import STATE_DIR

        return Path(STATE_DIR) / _STATE_FILENAME

    def _ensure_loaded(self) -> None:
        path = self._state_path()
        with self._lock:
            if self._loaded_path == path:
                return
            disabled: set[str] = set()
            try:
                with path.open(encoding="utf-8") as f:
                    payload = json.load(f)
                raw = payload.get("disabled", []) if isinstance(payload, dict) else []
                if isinstance(raw, list):
                    disabled = {str(key) for key in raw}
            except FileNotFoundError:
                pass
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                log.warning("MCP control state load failed: %s", type(exc).__name__)
            self._disabled = disabled
            self._loaded_path = path

    def _save(self) -> None:
        atomic_write_json(
            self._state_path(),
            {"version": _STATE_VERSION, "disabled": sorted(self._disabled)},
        )

    def disabled_keys(self) -> frozenset[str]:
        self._ensure_loaded()
        with self._lock:
            return frozenset(self._disabled)

    def is_enabled(self, key: str) -> bool:
        return key not in self.disabled_keys()

    def filter_keys(self, keys: Iterable[str]) -> set[str]:
        disabled = self.disabled_keys()
        return {key for key in keys if key not in disabled}

    def filter_configs(self, configs: Mapping[str, dict]) -> dict[str, dict]:
        disabled = self.disabled_keys()
        return {key: cfg for key, cfg in configs.items() if key not in disabled}

    def set_enabled(self, target: str, enabled: bool) -> McpMutation:
        target = (target or "").strip()
        if target != "all" and target not in self._servers:
            raise KeyError(target)

        self._ensure_loaded()
        with self._lock:
            targets = set(self._servers) if target == "all" else {target}
            before = set(self._disabled)
            if enabled:
                self._disabled.difference_update(targets)
            else:
                self._disabled.update(targets)
            changed = tuple(sorted(before ^ self._disabled))
            if changed:
                self._save()
            return McpMutation(target=target, enabled=enabled, changed_keys=changed)

    def _tier(self, key: str) -> str:
        from config import (
            MCP_SERVERS_BASE,
            MCP_SERVERS_DOCS,
            MCP_SERVERS_HEAVY,
            MCP_SERVERS_INSHI,
            MCP_SERVERS_IOT,
            MCP_SERVERS_MEMORY,
            MCP_SERVERS_PAPER,
            MCP_SERVERS_REALESTATE,
            MCP_SERVERS_WEB,
        )

        tiers = (
            ("base", MCP_SERVERS_BASE),
            ("web", MCP_SERVERS_WEB),
            ("heavy", MCP_SERVERS_HEAVY),
            ("memory", MCP_SERVERS_MEMORY),
            ("paper", MCP_SERVERS_PAPER),
            ("docs", MCP_SERVERS_DOCS),
            ("realestate", MCP_SERVERS_REALESTATE),
            ("iot", MCP_SERVERS_IOT),
            ("inshi", MCP_SERVERS_INSHI),
        )
        return next((name for name, bucket in tiers if key in bucket), "other")

    @staticmethod
    def _static_tool_names(key: str) -> tuple[str, ...]:
        from services.claude_api.mcp_client import STATIC_MCP_TOOL_SCHEMAS

        names = {
            str(tool.get("name", ""))
            for tool in STATIC_MCP_TOOL_SCHEMAS.get(key, [])
            if tool.get("name")
        }
        return tuple(sorted(names))

    def tool_names(self, key: str) -> tuple[str, ...]:
        if key not in self._servers:
            raise KeyError(key)
        probe = self._probes.get(key)
        if probe is not None and probe.status == "reachable":
            return probe.tool_names
        return self._static_tool_names(key)

    def list_servers(self, *, selected_keys: Iterable[str] = ()) -> list[dict]:
        selected = set(selected_keys)
        disabled = self.disabled_keys()
        rows: list[dict] = []
        for key in sorted(self._servers):
            cfg = self._servers[key]
            probe = self._probes.get(key)
            static_names = self._static_tool_names(key)
            rows.append({
                "key": key,
                "transport": cfg.get("type") or "stdio",
                "tier": self._tier(key),
                "enabled": key not in disabled,
                "selected": key in selected,
                "probe_status": probe.status if probe is not None else "unprobed",
                "checked_at": probe.checked_at if probe is not None else None,
                "error": probe.error if probe is not None else None,
                "tool_count": probe.tool_count if probe is not None else len(static_names),
            })
        return rows

    async def probe(self, key: str, *, timeout: float | None = None) -> McpProbeResult:
        if key not in self._servers:
            raise KeyError(key)
        checked_at = datetime.now(timezone.utc).isoformat()
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(timeout or self.probe_timeout):
                    handle = await _spawn_for_probe(stack, key, self._servers[key])
                    names = tuple(sorted(
                        str(tool.name)
                        for tool in handle.tools
                        if getattr(tool, "name", None)
                    ))
            result = McpProbeResult(
                key=key, status="reachable", tool_names=names, checked_at=checked_at,
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            result = McpProbeResult(
                key=key,
                status="failed",
                tool_names=(),
                checked_at=checked_at,
                error=_safe_probe_error(exc),
            )
        self._probes[key] = result
        return result

    def probe_payload(self, key: str) -> dict | None:
        probe = self._probes.get(key)
        return asdict(probe) if probe is not None else None


_default_service: McpControlService | None = None
_default_lock = threading.Lock()


def get_mcp_control() -> McpControlService:
    global _default_service
    if _default_service is None:
        with _default_lock:
            if _default_service is None:
                _default_service = McpControlService()
    return _default_service


def filter_enabled_mcp_keys(keys: Iterable[str]) -> set[str]:
    return get_mcp_control().filter_keys(keys)


def filter_enabled_mcp_configs(configs: Mapping[str, dict]) -> dict[str, dict]:
    return get_mcp_control().filter_configs(configs)


def _reset_default_for_test(service: McpControlService | None = None) -> None:
    global _default_service
    _default_service = service
