"""Apply explicit memory changes to provider sessions and reconstructed prompts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from services.work_context import ConversationPolicy, current_conversation


def assert_current_delivery():
    """Check immediately before each Discord publication, including rich output."""
    context = current_conversation.get()
    if context is not None:
        from services.memory_policy import get_memory_policy
        if not get_memory_policy().is_current(context.epoch):
            raise RuntimeError("Memory or audience changed before publication")


class ConversationPolicyRegistry:
    """Persistent privacy provenance for native state and derived summaries."""

    def __init__(self, path, policy):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS conversation_policy (owner TEXT, channel TEXT, epoch INTEGER, audience TEXT, PRIMARY KEY(owner,channel))")

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def begin(self, owner_id, channel_id, private, source_message_id=None):
        owner, channel = str(owner_id), str(channel_id)
        with self._connect() as conn:
            row = conn.execute("SELECT epoch,audience FROM conversation_policy WHERE owner=? AND channel=?", (owner, channel)).fetchone()
        epoch = self.policy.epoch()
        audience = self.policy.audience_key(owner, channel, private)
        rebuild = not row or row[0] != epoch or row[1] != audience
        return ConversationPolicy(owner, channel, private, epoch, rebuild, source_message_id)

    def commit(self, context):
        if not self.policy.is_current(context.epoch):
            raise RuntimeError("記憶の訂正後に古い会話状態を保存できません。")
        audience = self.policy.audience_key(context.owner_id, context.channel_id, context.private)
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO conversation_policy VALUES (?,?,?,?)",
                         (context.owner_id, context.channel_id, context.epoch, audience))


async def retire_native_context(channel_id, owner_id, sessions=None):
    """Retire both providers before constructing a new memory-safe prompt."""
    from services.codex_app_server.manager import clear_codex_thread
    from services.session import clear_session
    from services.claude_api.session_store import _clear_session_time

    await clear_codex_thread(int(channel_id), int(owner_id))
    clear_session(int(channel_id), int(owner_id))
    # Provider API replay snapshots are channel-shared in the legacy store.
    # Invalidate that stored prompt context, then rebuild from filtered records.
    _clear_session_time(int(channel_id), int(owner_id))


def filter_rows(rows):
    context = current_conversation.get()
    if context is None:
        return rows
    from services.memory_policy import get_memory_policy

    policy = get_memory_policy()
    # One reconstruction uses one fresh revision/audience. Do not retain this
    # snapshot across calls: corrections and expirations invalidate the next
    # reconstruction, and assert_current_delivery checks in-flight publication.
    epoch = policy.epoch()
    audience = policy.audience_key(context.owner_id, context.channel_id, context.private)
    filtered = []
    for row in rows:
        row = dict(row)
        reusable = row.get("memory_epoch") == epoch and row.get("audience_key") == audience
        if not reusable:
            if row.get("is_compact_summary") or row.get("role") == "assistant":
                continue
            row.pop("context_snapshot", None)
            row.pop("api_messages", None)
        filtered.append(row)
    return policy.filter_context_rows(filtered, context.owner_id,
        channel_id=context.channel_id, is_private=context.private)


def filter_memories(rows):
    context = current_conversation.get()
    if context is None:
        return rows
    from services.memory_policy import get_memory_policy

    policy = get_memory_policy()
    rows = policy.filter_memories(rows, context.owner_id, channel_id=context.channel_id,
                                 is_private=context.private)
    explicit = policy.recall(context.owner_id, channel_id=context.channel_id,
                             is_private=context.private)
    seen = {str(row.get("id")) for row in rows}
    return rows + [row for row in explicit if str(row.get("id")) not in seen]


def sanitize_derived(text: str, *, recorded_epoch=None) -> str:
    context = current_conversation.get()
    if context is None:
        return text
    from services.memory_policy import get_memory_policy

    return get_memory_policy().sanitize_context(text, context.owner_id,
        channel_id=context.channel_id, is_private=context.private,
        context_epoch=recorded_epoch)


def persona_prompt(*, restricted=False, legacy=""):
    """Public persona never reads administrator relationship/identity files."""
    context = current_conversation.get()
    if context is None:
        return legacy
    from services.persona_policy import public_core_prompt, preference_prompt

    return public_core_prompt() + "\n\n" + preference_prompt(context.owner_id,
        channel_id=context.channel_id, is_private=context.private)
