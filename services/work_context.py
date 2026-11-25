"""Request-local identity for durable work and privacy-safe conversational turns.

This module intentionally imports neither Discord nor provider implementations.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkContext:
    work_id: str
    owner_id: str
    actor_id: str
    revision: int
    channel_id: str
    private: bool = False


current_work: ContextVar[WorkContext | None] = ContextVar("yui_current_work", default=None)


@dataclass(frozen=True)
class ConversationPolicy:
    owner_id: str
    channel_id: str
    private: bool
    epoch: int = 0
    rebuild: bool = False
    source_message_id: str | None = None


current_conversation: ContextVar[ConversationPolicy | None] = ContextVar(
    "yui_conversation_policy", default=None,
)
