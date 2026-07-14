"""on_message parallel pre-processing (P2-a).

Launches every context-gathering task for one incoming message and returns
the in-flight bundle as a ContextTasks. Awaiting/merging stays in bot.py —
the join order there is interleaved with attachment handling and prompt
assembly and is pinned by the characterization suite.

Collaborator *functions* (fetch/thread/link/mem0 search...) are passed in by
bot.py at call time rather than imported here: tests patch them as bot-module
attributes (patch("bot.fetch_thread_context") etc.), and call-time injection
means the patched object is what actually runs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import discord

log = logging.getLogger(__name__)


@dataclass
class ContextTasks:
    """In-flight context-gathering tasks for one message.

    ``reply`` resolves to (context_str, ref_attachments, is_trusted);
    the string tasks resolve to prompt-ready blocks; the optional tasks are
    None when their gate (message length / admin / feature flag) was closed.
    """

    will_resume: bool
    reply: "asyncio.Task"
    thread: "asyncio.Task"
    links: "asyncio.Task"
    cross_thread: "asyncio.Task"
    mem0: "asyncio.Task | None"
    invest_kb: "asyncio.Task | None"
    history_search: "asyncio.Task | None"
    history_embed: "asyncio.Task | None"


def launch_context_tasks(
    *,
    bot,
    message: "discord.Message",
    prompt: str,
    raw_user_text: str,
    caller_is_admin: bool,
    mem0_user_id: str,
    sanitize,
    cap_text,
    get_session,
    is_session_fresh,
    fetch_thread_context,
    fetch_channel_context,
    resolve_message_links,
    resolve_channel_mention_contexts,
    search_memories_smart,
    should_use_memory_llm_mode,
    admin_user_ids,
    max_user_message_chars: int,
    mem0_min_query_length: int,
    mem0_smart_search: bool,
    mem0_recall_limit: int,
    mem0_recall_limit_restricted: int,
    mem0_history_search_enabled: bool,
    mem0_history_search_limit: int,
    mem0_history_embed_enabled: bool,
    mem0_history_embed_limit: int,
    will_resume_override: bool | None = None,
) -> ContextTasks:
    """Fire all pre-processing tasks for ``message`` in parallel."""
    is_thread = isinstance(message.channel, discord.Thread)
    # If we expect to resume an existing session in this channel, skip
    # fetching thread/channel context — Claude already has it in its context
    # window. Saves tokens on every resumed turn. 機能1: resume 可否は
    # (channel, user) 単位で判定する。同一チャンネルに複数 admin がいても、
    # 相手のセッションを自分の resume 判定に使わない。
    if will_resume_override is None:
        will_resume = (
            caller_is_admin
            and get_session(message.channel.id, message.author.id) is not None
            and is_session_fresh(message.channel.id, message.author.id)
        )
    else:
        # Provider-owned session stores are authoritative for their own route.
        # In particular, a Codex app-server thread must not inherit Claude's
        # freshness decision or the channel transcript is replayed on every
        # native thread resume.
        will_resume = caller_is_admin and bool(will_resume_override)

    async def _fetch_reply():
        if not (message.reference and message.reference.message_id):
            return "", [], False
        try:
            # Gateway reply payloads normally include referenced_message as
            # ``reference.resolved``. Use it before cache/REST so the same
            # metadata that admitted a reply ping also supplies its context.
            ref_msg = getattr(message.reference, "resolved", None)
            if not isinstance(ref_msg, discord.Message):
                ref_msg = message.reference.cached_message
            if ref_msg is None:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
            # For non-admins, refuse to inline admin messages — they may
            # contain command output / secrets the attacker should not see.
            if not caller_is_admin and ref_msg.author.id in admin_user_ids:
                return "", [], False
            body = sanitize(
                cap_text(ref_msg.content or "(no text)", max_user_message_chars)
            )
            author = sanitize(ref_msg.author.display_name)
            # An admin replying to admin- or bot-authored content is trusted
            # context (own past instructions, kanae's prior output,
            # b0nchichi's messages). Place it OUTSIDE <untrusted_user_input>.
            # External authors stay inside.
            is_trusted = caller_is_admin and (
                (bot.user is not None and ref_msg.author.id == bot.user.id)
                or ref_msg.author.id in admin_user_ids
            )
            if is_trusted:
                ctx = (
                    f"<replied_to_trusted_message author={author!r}>\n"
                    f"{body}\n"
                    f"</replied_to_trusted_message>\n\n"
                )
            else:
                ctx = (
                    f"<replied_to_message author={author!r}>\n"
                    f"{body}\n"
                    f"</replied_to_message>\n\n"
                )
            return ctx, list(ref_msg.attachments), is_trusted
        except Exception as e:
            log.warning("Failed to fetch replied-to message: %s", e)
            return "", [], False

    async def _fetch_thread():
        if will_resume:
            return ""
        if is_thread:
            return await fetch_thread_context(
                message.channel, message, caller_is_admin=caller_is_admin,
            )
        return await fetch_channel_context(
            message.channel, message, caller_is_admin=caller_is_admin,
        )

    reply_task = asyncio.create_task(_fetch_reply())
    thread_task = asyncio.create_task(_fetch_thread())
    link_task = asyncio.create_task(
        resolve_message_links(bot, prompt, requester=message.author, caller_is_admin=caller_is_admin)
    )
    # Cross-thread context from channel mentions (<#id>)
    cross_thread_task = asyncio.create_task(
        resolve_channel_mention_contexts(
            bot, prompt,
            current_channel_id=message.channel.id,
            requester=message.author,
            caller_is_admin=caller_is_admin,
        )
    )

    # Always recall Mem0 memories so Claude has long-term context, even on
    # resumed sessions. Skip only for very short messages (stickers, "はい",
    # "うん", etc.) where embedding search won't match.
    mem0_task: "asyncio.Task | None" = None
    invest_kb_task: "asyncio.Task | None" = None
    history_search_task: "asyncio.Task | None" = None
    history_embed_task: "asyncio.Task | None" = None
    if len(raw_user_text) >= mem0_min_query_length:
        mem0_llm_mode = should_use_memory_llm_mode(
            raw_user_text, enabled=mem0_smart_search,
        )
        # Admins always rewrite+rerank so a multi-fact topic (the 日経VI
        # strategy ≈ 6 memories) surfaces, not only on explicit recall asks;
        # the Haiku round-trip runs concurrently with prompt prep and is
        # dwarfed by the main response. They also get a wider recall window.
        # Non-admins keep the cheaper, narrower path.
        if caller_is_admin and mem0_smart_search:
            mem0_llm_mode = True
        _recall_limit = (
            mem0_recall_limit if caller_is_admin else mem0_recall_limit_restricted
        )
        mem0_task = asyncio.create_task(
            search_memories_smart(
                raw_user_text, mem0_user_id, limit=_recall_limit,
                rewrite=mem0_llm_mode, rerank=mem0_llm_mode,
                include_core=True,
                current_channel_id=str(message.channel.id),
            )
        )

        # invest_kb recall — neutral long-form investment knowledge stored in
        # a separate LanceDB side-store. Gated by a cheap keyword check so
        # chitchat doesn't spend Haiku budget on rewrite/HyDE/rerank.
        # Admin-only: the KB itself is neutral but the recall latency would
        # slow down non-admin restricted calls for no benefit.
        if caller_is_admin:
            from config import (
                INVEST_KB_ENABLED, INVEST_KB_LIMIT, INVEST_KB_QUERY_GATE,
            )
            if INVEST_KB_ENABLED:
                from services.invest_kb import is_investment_query, search_invest_kb
                gate = INVEST_KB_QUERY_GATE
                if gate == "always" or (gate == "auto" and is_investment_query(raw_user_text)):
                    invest_kb_task = asyncio.create_task(
                        search_invest_kb(raw_user_text, limit=INVEST_KB_LIMIT)
                    )

        # Per-turn literal-keyword recall over this channel's raw JSONL
        # transcript — rescues exact terms (proper nouns mentioned once) that
        # Mem0 curation dropped. Admin-only: the transcript has no author id,
        # so surfacing it to a non-admin could leak admin lines.
        if caller_is_admin and mem0_history_search_enabled:
            from services.mem0 import history_index
            history_search_task = asyncio.create_task(
                asyncio.to_thread(
                    history_index.search_history,
                    raw_user_text, message.channel.id,
                    mem0_history_search_limit,
                )
            )

        # Phase 5: semantic (vector) recall over the same raw transcript, run
        # in parallel and merged with the keyword hits downstream. Self-gated
        # (default OFF), admin-only (transcript may hold other admins' lines).
        if caller_is_admin and mem0_history_embed_enabled:
            from services.mem0 import history_embed
            history_embed_task = asyncio.create_task(
                asyncio.to_thread(
                    history_embed.search_semantic,
                    raw_user_text, message.channel.id,
                    mem0_history_embed_limit,
                )
            )

    return ContextTasks(
        will_resume=will_resume,
        reply=reply_task,
        thread=thread_task,
        links=link_task,
        cross_thread=cross_thread_task,
        mem0=mem0_task,
        invest_kb=invest_kb_task,
        history_search=history_search_task,
        history_embed=history_embed_task,
    )
