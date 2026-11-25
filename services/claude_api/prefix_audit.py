"""Bounded, process-local request prefix diagnostics; never log prompt content.

Byte measurements are NOT token counts or provider cache-hit estimates. Opaque
fingerprints compare consecutive logical requests, excluding cache annotations
and transport credentials. An HMAC key is regenerated on process restart.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict

log = logging.getLogger(__name__)


def _block(value):
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if key != "cache_control"}
    return value


def _components(payload: dict):
    yield "settings", {key: payload.get(key) for key in (
        "model", "thinking", "output_config", "betas", "extra_body", "max_tokens",
    )}
    # Deferred tools are excluded from the provider prefix; label the rest by
    # name so a changed hot set is attributable (names are not content).
    for tool in payload.get("tools", []):
        if isinstance(tool, dict) and not tool.get("defer_loading"):
            yield f"tools:{tool.get('name') or tool.get('type')}", _block(tool)
    system = payload.get("system", [])
    for index, block in enumerate(system if isinstance(system, list) else [system]):
        yield f"system[{index}]", _block(block)
    for index, message in enumerate(payload.get("messages", [])):
        yield f"messages[{index}].role", message.get("role")
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        for block_index, block in enumerate(blocks or []):
            yield f"messages[{index}].content[{block_index}]", _block(block)


class PrefixAuditor:
    def __init__(self, *, capacity: int = 128, ttl: float = 3600, clock=time.monotonic):
        self.capacity = max(1, capacity)
        self.ttl = ttl
        self.clock = clock
        self._key = secrets.token_bytes(32)
        self._entries: OrderedDict[object, tuple[float, list]] = OrderedDict()
        self._lock = threading.Lock()

    def observe(self, scope: object, payload: dict) -> dict:
        current = []
        for label, value in _components(payload):
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            current.append((label, hmac.digest(self._key, raw, hashlib.sha256), len(raw)))
        now = self.clock()
        with self._lock:
            previous = self._entries.pop(scope, None)
            self._entries[scope] = (now, current)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)
        old = previous[1] if previous and now - previous[0] < self.ttl else []
        shared = 0
        count = 0
        for before, after in zip(old, current):
            if before[:2] != after[:2]:
                break
            shared += after[2]
            count += 1
        changed = (current[count][0] if count < len(current) else "end") if old else "cold"
        tools = [tool for tool in payload.get("tools", []) if isinstance(tool, dict)]
        return {
            "event": "context_prefix_audit", "cold": not bool(old),
            "logical_payload_bytes": sum(item[2] for item in current),
            "common_prefix_bytes": shared, "common_components": count,
            "first_changed_component": changed,
            "messages": len(payload.get("messages", [])),
            "hot_tools": sum(not tool.get("defer_loading") for tool in tools),
            "deferred_tools": sum(bool(tool.get("defer_loading")) for tool in tools),
        }


_auditor = PrefixAuditor()


def audit_request_prefix(channel_id: int, payload: dict) -> None:
    if os.environ.get("CONTEXT_PREFIX_AUDIT", "0") != "1":
        return
    try:
        result = _auditor.observe(channel_id, payload)
        log.info("[ch=%d] %s", channel_id, json.dumps(result, sort_keys=True))
    except Exception:
        # Observability must not block a response or log the sensitive payload.
        log.warning("context prefix audit unavailable")
