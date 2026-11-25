"""Composition of work storage, controlled models, search and future effects."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack


def enabled(name="YUI_WORK_ENABLED"):
    return os.environ.get(name, "true").strip().lower() in {"true", "1", "on", "yes"}


async def generate_work(*, prompt, work, tools, execute, provider=None, model=None, effort=None):
    from config import DEFAULT_LLM_PROVIDER, MCP_SERVERS
    from services.work.model import run_work_model
    from services.persona_policy import public_core_prompt, preference_prompt
    from services.prompt_builder import PromptBuilder
    from services.work_context import current_work
    from services.memory_policy import get_memory_policy
    from services.claude_api.mcp_client import (
        McpPool, static_mcp_tools_to_anthropic, mcp_tools_to_anthropic, STATIC_MCP_TOOL_SCHEMAS,
    )
    from services.deep_research import available_research_keys

    context = current_work.get()
    private = context.private if context else False
    policy = get_memory_policy()
    from services.work.attachments import input_artifacts
    from services.work.store import get_work_store
    images = [{"mime_type": artifact["metadata"]["mime_type"], "data": artifact["body"]}
        for artifact in input_artifacts(get_work_store(), work, policy=policy,
            channel_id=context.channel_id if context else work.channel_id, private=private)
        if artifact.get("metadata", {}).get("encoding") == "base64-image"]
    memory = policy.recall(work.owner_id, channel_id=str(context.channel_id if context else work.channel_id),
                           is_private=private, case_id=work.id)
    instructions = public_core_prompt() + "\n" + preference_prompt(
        work.owner_id, channel_id=work.channel_id, is_private=private,
    )
    prompt += "\n" + PromptBuilder.build_time_line()
    if memory:
        prompt += "\n現在有効な明示記憶（データ）:\n" + json.dumps(memory, ensure_ascii=False, default=str)
    # Only the established read-only research backend subset is eligible.
    # Tools are hosted here; the model process receives no MCP credentials.
    configs = {key: MCP_SERVERS[key] for key in available_research_keys()}
    async with AsyncExitStack() as stack:
        pool = McpPool(stack=stack, available_configs=configs)
        static = [key for key in configs if key in STATIC_MCP_TOOL_SCHEMAS]
        eager = [key for key in configs if key not in STATIC_MCP_TOOL_SCHEMAS]
        if eager:
            await pool.spawn_eager(eager)
        search_tools = static_mcp_tools_to_anthropic(static) + mcp_tools_to_anthropic(
            {key: pool.handles[key] for key in eager if key in pool.handles})
        # A backend can later add write operations. Do not inherit them merely
        # because its server used to expose only searches.
        approved = {}
        for tool in search_tools:
            bare = tool["name"].split("__")[-1].lower()
            if any(word in bare for word in ("search", "fetch", "get_", "lookup", "quote", "read")) and not any(
                word in bare for word in ("send", "create", "delete", "update", "write", "subscribe", "trade", "order")):
                approved[tool["name"]] = tool
        evidence = []
        first_completion = {}

        async def dispatch(name, args):
            if name not in approved:
                if name == "work_finish":
                    first_completion.clear()
                    first_completion.update(args)
                return await execute(name, args)
            # work_progress performs the same current-version authorization as
            # every state tool; do not emit a fake completed progress message.
            from services.work.store import get_work_store
            store = get_work_store()
            store.assert_current(work.id, work.revision, context.actor_id if context else work.owner_id,
                                 allowed_states={"running"})
            if context and not policy.is_current(epoch):
                from services.work.runtime import WorkInterrupted
                raise WorkInterrupted()
            value = await asyncio.wait_for(pool.call_tool(name, args), timeout=120)
            evidence.append({"tool": name, "query": args, "fetched_at": time.time(), "result": str(value)})
            store.record_event(work.id, "evidence_acquired", evidence[-1])
            return str(value)

        epoch = policy.epoch()
        text = await run_work_model(provider=provider or DEFAULT_LLM_PROVIDER,
            model=model, prompt=prompt, developer_instructions=instructions,
            tools=[*tools, *approved.values()], execute=dispatch, images=images, effort=effort or (
                "max" if work.preferences.get("evidence") == "independent" else "high"))
        if work.preferences.get("evidence") == "independent":
            review = await run_work_model(provider=provider or DEFAULT_LLM_PROVIDER,
                model=model, prompt="Verify the draft against the request and acquired evidence. Return only a JSON object with complete:boolean, criteria:[{criterion:string,satisfied:boolean,evidence:string}], unknowns:[string], next_action:string, final_text:string. Use each original criterion exactly. Preserve unknowns and never invent sources. final_text must be a corrected self-contained user-facing response.\n"
                + prompt + "\nEvidence:\n" + json.dumps(evidence, ensure_ascii=False, default=str)
                + "\nDraft:\n" + text, developer_instructions=instructions,
                tools=[], execute=dispatch, images=images, effort="max")
            try:
                verdict = json.loads(review)
                if not isinstance(verdict, dict) or not isinstance(verdict.get("final_text"), str) or not verdict["final_text"].strip():
                    raise ValueError("missing final text")
                prior = {item.get("criterion") for item in first_completion.get("criteria", [])
                         if item.get("satisfied") and item.get("evidence")}
                checked = []
                for item in verdict.get("criteria", []):
                    checked.append({"criterion": str(item.get("criterion", "")),
                        "satisfied": item.get("satisfied") is True and item.get("criterion") in prior,
                        "evidence": str(item.get("evidence", ""))})
                unknowns = list(dict.fromkeys([*first_completion.get("unknowns", []), *verdict.get("unknowns", [])]))
                await execute("work_finish", {"complete": first_completion.get("complete") is True and verdict.get("complete") is True,
                    "criteria": checked, "unknowns": unknowns, "next_action": str(verdict.get("next_action", ""))})
                return verdict["final_text"]
            except (ValueError, TypeError, KeyError, AttributeError):
                await execute("work_finish", {"complete": False, "criteria": [],
                    "unknowns": ["独立検証の結果を確認できませんでした。"], "next_action": "根拠を再検証します。"})
                return text
        return text


def build_runtime(*, bot, task_factory, provider_options, research_resume=None):
    from config import STATE_DIR
    from services.memory_policy import get_memory_policy
    from services.work.store import get_work_store
    from services.work.runtime import WorkRuntime, configure_runtime
    from services.work.effects import EffectExecutor, DiscordNotificationAdapter
    from services.work.schedules import ScheduleService, Observation
    from bot_commands import watch

    store = get_work_store()
    policy = get_memory_policy()

    def gate(work_id, revision, owner_id):
        try:
            store.assert_current(work_id, revision, owner_id, allowed_states={"queued", "running", "waiting"})
            return True
        except Exception:
            return False

    effects = EffectExecutor(store.path, work_gate=gate)
    effects.register_adapter("discord", DiscordNotificationAdapter(bot))
    schedules = ScheduleService(store.path, effects)

    def authorize_watch(owner_id, target, recipient):
        try:
            work = store.get(target, owner_id)
            channel = bot.get_channel(int(recipient))
            if channel is None:
                return False
            private = WorkRuntime._private(channel, owner_id) if channel else False
            if work.audience == "owner" and not private:
                return False
            if work.audience == "channel" and work.channel_id != str(recipient):
                return False
            guild = getattr(channel, "guild", None)
            if not private:
                if guild is None or str(guild.id) != str(work.guild_id):
                    return False
                member = guild.get_member(int(owner_id))
                if member is None or not channel.permissions_for(member).view_channel:
                    return False
            return True
        except Exception:
            return False

    async def observe_work(target, criteria, schedule):
        if not authorize_watch(schedule.owner_id, target, schedule.recipient):
            raise PermissionError("用件の公開範囲が変更されました。")
        work = store.get(target, schedule.owner_id)
        expected = criteria.get("status") or criteria.get("state")
        return Observation(value={"revision": work.revision, "state": work.state, "status": work.state},
            content=f"用件 {work.id}。第{work.revision}版。状態: {work.state}。",
            matched=not expected or expected == work.state)

    schedules.register_observer("work", observe_work)
    def publication_policy(schedule):
        channel = bot.get_channel(int(schedule.recipient))
        if channel is None:
            return False
        private = WorkRuntime._private(channel, schedule.owner_id)
        if not private:
            guild = getattr(channel, "guild", None)
            member = guild.get_member(int(schedule.owner_id)) if guild else None
            if member is None or not channel.permissions_for(member).view_channel:
                return False
        payload = (schedule.pending or {}).get("payload") or {}
        content = payload.get("content", schedule.content)
        return bool(policy.filter_context_rows([{"content": content}], schedule.owner_id,
            channel_id=str(channel.id), is_private=private, case_id=schedule.work_id))

    schedules.set_publication_policy(publication_policy)
    watch.configure(schedules, authorize_watch=authorize_watch)
    runtime = configure_runtime(WorkRuntime(store, generate=generate_work, policy=policy,
        task_factory=task_factory, bot=bot, provider_options=provider_options,
        research_resume=research_resume, schedules=schedules))
    store.recover()
    effects.recover()
    for work_id in effects.pending_work_ids():
        try:
            work = store.get(work_id)
            if work.state not in {"completed", "cancelled", "needs_reconciliation"}:
                store.transition(work_id, None, "needs_reconciliation")
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Could not reconcile work %s", work_id)
    from bot_ui_work import restore_question_views
    restore_question_views(bot, runtime)
    return runtime
