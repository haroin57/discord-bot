"""Dependency container for slash-command modules (P2-d).

Commands live outside bot.py but need a few of its runtime objects. They are
injected explicitly here instead of importing bot (which would be a circular
import) or reaching into module globals (which P2.5 is eliminating).

Services-layer functions (mem0, session, deep_research, ...) are NOT injected
— command modules import those directly; only bot.py-owned state and helpers
go through this container.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class CommandDeps:
    bot: Any
    is_admin: Callable[[Any], bool]
    track_task: Callable[..., Any]
    send_segmented: Callable[..., Any]
    get_http_session: Callable[[], Any]
    # (channel_id, user_id) -> override value. Live references to bot.py's
    # dicts until P2.5 moves them into an OverrideState.
    model_overrides: dict
    effort_overrides: dict
    # /tier (2026-07-08): "deliberate"（熟考固定）| "chat"（会話固定）。
    # 未設定はルーター判定（既定は明示マーカーのみ）。
    tier_overrides: dict
    # (channel_id) -> per-channel asyncio.Semaphore(1). The same FIFO gate
    # on_message uses to serialise turns; free-code slash commands (/commit etc.)
    # acquire it so their tool-enabled turn can't race a concurrent channel reply
    # (ADR-0048). Optional/last-field so existing constructors stay valid.
    get_channel_sema: Callable[[int], Any] | None = None
