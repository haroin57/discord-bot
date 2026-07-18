"""Provider invocation and Discord callback wiring for one model turn."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelConfig:
    scratchpad_stream_enabled: bool
    plan_stream_enabled: bool
    math_presentation_mode: str
    deliberation_router_enabled: bool
    spinner_mode: str
    admin_user_ids: set[int]


@dataclass(frozen=True)
class ModelDeps:
    live_renderer: Any
    track_task: Callable[..., Any]
    split_message: Callable[[str], list[str]]
    speaker_label: Callable[[int], str]
    provider_label: Callable[[str], str]
    call_with_tools: Callable[..., Any]
    register_renderer: Callable[[Any], None] | None = None
    delivery_callback: Callable[[Any], Any] | None = None


@dataclass(frozen=True)
class ModelInput:
    bot: Any
    message: Any
    status: Any
    job: Any
    prompt: str
    sessions: Any
    provider: str
    caller_is_admin: bool
    is_thread: bool
    raw_user_text: str
    image_paths: list[str]
    text_attachments: list[tuple[str, str]]
    arbitrary_attachments: list[dict]
    forced_model: str | None
    forced_effort: str | None
    active_skill_prompt: str
    tier_overrides: dict[tuple[int, int], str]


@dataclass(frozen=True)
class ModelResult:
    response_text: str
    session_info: Any
    actually_resumed: bool
    renderer: Any
    tier: str | None
    route_judgment: bool


async def _install_permission_handler(data: ModelInput, config: ModelConfig) -> None:
    from services.claude_api.ask_bridge import judge_approval, set_ask_handler

    async def permission_ask(
        tool_name: str,
        detail: str,
        rule: str,
    ) -> bool:
        approval_message = await data.message.channel.send(
            f"🔐 **ツール承認待ち** `{tool_name}`（rule: `{rule}`）\n"
            f"```\n{detail[:800]}\n```\n"
            "✅/❌ のリアクション、またはこのメッセージへの返信で"
            "指示してください（時間制限なし）",
        )
        await approval_message.add_reaction("✅")
        await approval_message.add_reaction("❌")

        def reaction_check(reaction, user):
            return (
                reaction.message.id == approval_message.id
                and user.id in config.admin_user_ids
                and str(reaction.emoji) in ("✅", "❌")
            )

        def message_check(reply):
            return (
                reply.channel.id == data.message.channel.id
                and reply.author.id in config.admin_user_ids
                and reply.reference is not None
                and reply.reference.message_id == approval_message.id
            )

        while True:
            reaction_task = asyncio.ensure_future(
                data.bot.wait_for("reaction_add", check=reaction_check),
            )
            message_task = asyncio.ensure_future(
                data.bot.wait_for("message", check=message_check),
            )
            try:
                done, _ = await asyncio.wait(
                    {reaction_task, message_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for pending in (reaction_task, message_task):
                    if not pending.done():
                        pending.cancel()
                await asyncio.gather(
                    reaction_task,
                    message_task,
                    return_exceptions=True,
                )
            result = next(iter(done)).result()
            if isinstance(result, tuple):
                allowed = str(result[0].emoji) == "✅"
            else:
                verdict = await judge_approval(
                    result.content or "",
                    tool_name=tool_name,
                    rule=rule,
                    detail=detail,
                )
                if verdict is None:
                    try:
                        await result.add_reaction("❓")
                    except Exception:
                        pass
                    continue
                allowed = verdict
            try:
                await approval_message.reply(
                    "🆗 許可しました" if allowed else "🚫 拒否しました",
                )
            except Exception:
                pass
            return allowed

    set_ask_handler(permission_ask)


async def invoke_model(
    data: ModelInput,
    *,
    config: ModelConfig,
    deps: ModelDeps,
    calling_phase: Any,
) -> ModelResult:
    """Wire callbacks and invoke the selected provider exactly once."""
    data.job.phase = calling_phase
    await data.status.update(f"📨 {deps.provider_label(data.provider)}へ送信中")
    log.info(
        "Routing provider=%s (restricted=%s, is_thread=%s, model=%s)",
        data.provider,
        not data.caller_is_admin,
        data.is_thread,
        data.forced_model or "<provider-default>",
    )

    renderer_kwargs = {
        "track_task": deps.track_task,
        "job": data.job,
        "split_message": deps.split_message,
        "math_presentation_mode": config.math_presentation_mode,
    }
    if deps.delivery_callback is not None:
        renderer_kwargs["delivery_callback"] = deps.delivery_callback
    renderer = deps.live_renderer(data.message, data.status, **renderer_kwargs)
    if deps.register_renderer is not None:
        deps.register_renderer(renderer)
    incremental_send = renderer.incremental_send
    stream_preview = renderer.stream_preview
    renderer_tool_invocation_send = renderer.tool_invocation_send

    async def tool_invocation_send(*args, **kwargs):
        # LiveRenderer replaces delayed tool notices on consecutive tool
        # events. Drain the previous task before asking it to create the next
        # one so the replacement cannot survive as an orphaned task.
        pending_notice = getattr(renderer, "tool_notice_task", None)
        if pending_notice is not None:
            if not pending_notice.done():
                pending_notice.cancel()
            await asyncio.gather(pending_notice, return_exceptions=True)
        return await renderer_tool_invocation_send(*args, **kwargs)

    scratch_stream = (
        renderer.scratch_stream_preview if config.scratchpad_stream_enabled else None
    )
    scratch_finalize = (
        renderer.scratch_finalize if config.scratchpad_stream_enabled else None
    )
    if config.plan_stream_enabled:

        def plan_stream(text: str):
            return renderer.plan_stream_preview(text, "計画")

        def verify_stream(text: str):
            return renderer.plan_stream_preview(text, "検証")

        plan_finalize = renderer.plan_finalize
    else:
        plan_stream = None
        verify_stream = None
        plan_finalize = None

    sender_speaker_label = deps.speaker_label(data.message.author.id)
    tier = None
    route_judgment = False
    if data.caller_is_admin and config.deliberation_router_enabled:
        try:
            from config import DELIB_ROUTER_AUTO_ENABLED
            from services.claude_api.router import decide_tier

            decision = await decide_tier(
                data.raw_user_text,
                has_attachments=bool(
                    data.image_paths
                    or data.text_attachments
                    or data.arbitrary_attachments
                ),
                auto_enabled=DELIB_ROUTER_AUTO_ENABLED,
            )
            route_judgment = decision.judgment
            tier_override = data.tier_overrides.get(
                (data.message.channel.id, data.message.author.id),
            )
            if tier_override in ("deliberate", "chat"):
                tier = tier_override
                log.info("delib_router overridden by /tier: %s", tier_override)
            else:
                tier = decision.tier
        except Exception as exc:
            log.warning("delib_router failed (chat 扱い): %s", exc)

    deliberate = tier == "deliberate"
    if deliberate:
        await data.status.update("🧠 熟考モード")
    if data.caller_is_admin:
        await _install_permission_handler(data, config)

    data.status.spin_start(mode=config.spinner_mode)
    try:
        response_text, session_info, actually_resumed = await deps.call_with_tools(
            data.prompt,
            data.message.channel.id,
            data.sessions,
            provider=data.provider,
            status_callback=data.status.update,
            incremental_text_callback=None if deliberate else incremental_send,
            text_stream_callback=None if deliberate else stream_preview,
            tool_invocation_callback=tool_invocation_send,
            scratch_stream_callback=scratch_stream,
            scratch_finalize_callback=scratch_finalize,
            thinking_stream_callback=plan_stream,
            thinking_finalize_callback=plan_finalize,
            verify_stream_callback=verify_stream,
            verify_finalize_callback=plan_finalize,
            progress=data.status.prog,
            tier=tier,
            route_judgment=route_judgment,
            restricted=not data.caller_is_admin,
            raw_user_text=data.raw_user_text,
            is_thread=data.is_thread,
            has_image_attachment=bool(data.image_paths),
            author=sender_speaker_label,
            user_id=data.message.author.id,
            forced_model_override=data.forced_model,
            forced_effort_override=data.forced_effort,
            active_skill_prompt=data.active_skill_prompt,
        )
    finally:
        data.status.spin_stop()

    return ModelResult(
        response_text=response_text,
        session_info=session_info,
        actually_resumed=actually_resumed,
        renderer=renderer,
        tier=tier,
        route_judgment=route_judgment,
    )
