"""Discord response preparation and publication stage."""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import discord

from utils.markdown_parser import MathSegment, TableSegment, TextSegment
from utils.math_presentation import PresentationBlockKind, PresentationKind


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SegmentedDeps:
    parse_segments: Callable[[str], list]
    plan_segments: Callable[[list], list]
    classify_math: Callable[[Any], Any]
    latex_to_discord_text: Callable[[str], str]
    render_table_image: Callable[..., Any]
    render_math_image: Callable[..., Any]
    reply_chain_sender: Any
    archive_images: Callable[..., Any]
    split_message: Callable[[str], list[str]]


@dataclass(frozen=True)
class PublishDeps:
    normalize_image_filenames: Callable[[str], str]
    extract_image_paths: Callable[..., list[str]]
    extract_file_paths: Callable[[str], list[str]]
    is_attach_ignored: Callable[[str], bool]
    archive_images: Callable[..., Any]
    bind_response_message: Callable[..., Any]
    parse_segments: Callable[[str], list]
    mask_code_blocks: Callable[[str], str]
    has_tables: Callable[..., bool]
    has_math: Callable[..., bool]
    split_message: Callable[[str], list[str]]
    reply_chain_sender: Any
    send_segmented: Callable[..., Any]
    log_math_presentation: Callable[[list], None]


@dataclass
class DeliveryState:
    """Publication receipt used to fence session and turn persistence."""

    streamed: bool = False
    body_delivered: bool = False
    request_id: str = ""
    message_ids: list[int] = field(default_factory=list)
    receipt_callback: Callable[["DeliveryState"], Any] | None = None
    _first_ack_sent: bool = False

    async def record_message(
        self,
        message_or_id: Any,
        *,
        on_first_ack: Callable[[], Any] | None = None,
    ) -> None:
        message_id = getattr(message_or_id, "id", message_or_id)
        if isinstance(message_id, int) and message_id not in self.message_ids:
            self.message_ids.append(message_id)
        self.body_delivered = True
        if self.receipt_callback is not None:
            result = self.receipt_callback(self)
            if inspect.isawaitable(result):
                await result
        if not self._first_ack_sent and on_first_ack is not None:
            self._first_ack_sent = True
            result = on_first_ack()
            if inspect.isawaitable(result):
                await result


def log_math_presentation(
    segments: list,
    *,
    math_presentation_mode: str,
    classify_math: Callable[[Any], Any],
) -> None:
    math_segments = [
        segment for segment in segments if isinstance(segment, MathSegment)
    ]
    if not math_segments:
        return
    if math_presentation_mode != "adaptive":
        log.info(
            "Math presentation mode=%s text=0 image=%d reasons=%s",
            math_presentation_mode,
            len(math_segments),
            {"legacy": len(math_segments)},
        )
        return
    decisions = [classify_math(segment) for segment in math_segments]
    reasons: dict[str, int] = {}
    for decision in decisions:
        reasons[decision.reason] = reasons.get(decision.reason, 0) + 1
    log.info(
        "Math presentation mode=%s text=%d image=%d reasons=%s",
        math_presentation_mode,
        sum(item.kind is PresentationKind.TEXT for item in decisions),
        sum(item.kind is PresentationKind.IMAGE for item in decisions),
        reasons,
    )


async def send_segmented(
    message: Any,
    response_text: str,
    attach_files: list[discord.File],
    *,
    bot_user: Any,
    math_presentation_mode: str,
    deps: SegmentedDeps,
    delivery_state: DeliveryState | None = None,
    on_first_ack: Callable[[], Any] | None = None,
    register_archive: Callable[..., Any] | None = None,
) -> int | None:
    """Send text and rendered table/math segments in their source order."""
    parsed_segments = deps.parse_segments(response_text)
    segments = parsed_segments
    if math_presentation_mode == "adaptive":
        planned = deps.plan_segments(parsed_segments)
        segments = [
            TextSegment(block.text)
            if block.kind is PresentationBlockKind.TEXT
            else block.source
            for block in planned
            if block.kind is PresentationBlockKind.TEXT or block.source is not None
        ]
    log_math_presentation(
        parsed_segments,
        math_presentation_mode=math_presentation_mode,
        classify_math=deps.classify_math,
    )

    sender = deps.reply_chain_sender(message)
    rendered_images: list[str] = []
    attachment_response_id: int | None = None

    async def archive_rendered(path: str, sent: Any, source: str) -> None:
        archive_kwargs = {
            "channel_id": message.channel.id,
            "request_message_id": message.id,
            "direction": "outbound-render",
            "author_id": getattr(bot_user, "id", None),
            "source": source,
            "per_path_metadata": {path: {"response_message_id": sent.id}},
        }
        if register_archive is not None:
            result = register_archive(
                paths=[path],
                kwargs=archive_kwargs,
                response_message_id=None,
                bind_response=False,
            )
            if inspect.isawaitable(result):
                await result
            return
        refs = await asyncio.to_thread(
            deps.archive_images,
            [path],
            **archive_kwargs,
        )
        if refs:
            log.info("R2 staged rendered image for response %s", sent.id)

    for segment in segments:
        if isinstance(segment, TextSegment):
            text = segment.content.strip()
            if text:
                for chunk in deps.split_message(text):
                    sent = await sender.send(chunk)
                    if delivery_state is not None:
                        await delivery_state.record_message(
                            sent,
                            on_first_ack=on_first_ack,
                        )
        elif isinstance(segment, TableSegment):
            image_path = await deps.render_table_image(
                segment.markdown,
                caption=segment.caption,
            )
            if image_path:
                rendered_images.append(image_path)
                try:
                    attachment = discord.File(image_path)
                    try:
                        sent = await sender.send(file=attachment)
                        if delivery_state is not None:
                            await delivery_state.record_message(
                                sent,
                                on_first_ack=on_first_ack,
                            )
                        await archive_rendered(
                            image_path,
                            sent,
                            "table-renderer",
                        )
                    finally:
                        attachment.close()
                finally:
                    try:
                        os.remove(image_path)
                    except OSError:
                        pass
                    rendered_images.remove(image_path)
            else:
                log.warning("Table render failed, falling back to text")
                for chunk in deps.split_message(segment.markdown):
                    sent = await sender.send(chunk)
                    if delivery_state is not None:
                        await delivery_state.record_message(
                            sent,
                            on_first_ack=on_first_ack,
                        )
        elif isinstance(segment, MathSegment):
            image_path = await deps.render_math_image(
                segment.latex,
                display_mode=segment.display_mode,
            )
            if image_path:
                rendered_images.append(image_path)
                description = deps.latex_to_discord_text(segment.latex) or segment.latex
                try:
                    attachment = discord.File(
                        image_path,
                        description=description.strip()[:1024] or None,
                    )
                    try:
                        sent = await sender.send(file=attachment)
                        if delivery_state is not None:
                            await delivery_state.record_message(
                                sent,
                                on_first_ack=on_first_ack,
                            )
                        await archive_rendered(
                            image_path,
                            sent,
                            "math-renderer",
                        )
                    finally:
                        attachment.close()
                finally:
                    try:
                        os.remove(image_path)
                    except OSError:
                        pass
                    rendered_images.remove(image_path)
            else:
                log.warning("Math render failed, falling back to text")
                fallback = (
                    f"`{segment.latex}`"
                    if not segment.display_mode
                    else f"```\n{segment.latex}\n```"
                )
                sent = await sender.send(fallback)
                if delivery_state is not None:
                    await delivery_state.record_message(
                        sent,
                        on_first_ack=on_first_ack,
                    )

    if attach_files:
        sent = await message.channel.send(files=attach_files)
        attachment_response_id = sent.id
        if delivery_state is not None:
            await delivery_state.record_message(
                sent,
                on_first_ack=on_first_ack,
            )
    for path in rendered_images:
        try:
            os.remove(path)
        except OSError:
            pass
    return attachment_response_id


async def prepare_response(
    *,
    message: Any,
    status: Any,
    renderer: Any,
    response_text: str,
    raw_user_text: str,
    strip_numeric_inline_code: Callable[[str], str],
) -> str | None:
    """Tear down transient UI and return the canonical response text."""
    pending_notice_task = renderer.tool_notice_task
    if pending_notice_task and not pending_notice_task.done():
        pending_notice_task.cancel()
    if pending_notice_task:
        await asyncio.gather(pending_notice_task, return_exceptions=True)
    notice_message = renderer.tool_notice_msg
    if notice_message is not None:
        try:
            await notice_message.delete()
        except (discord.errors.NotFound, discord.errors.HTTPException):
            pass
        except Exception as exc:
            log.warning("tool notice delete failed: %s", exc)
        renderer.clear_tool_notice_msg()

    response_text = strip_numeric_inline_code(response_text)
    status.cancel_pending()
    if (response_text or "").strip():
        return response_text
    if renderer.live_sent_any:
        log.info(
            "[ch=%s] empty response_text but streaming already delivered "
            "content; suppressing fallback",
            message.channel.id,
        )
        return None
    log.warning(
        "Empty response_text from model; sending fallback "
        "(channel=%s, user=...%s, user_text=%r)",
        message.channel.id,
        str(message.author.id)[-4:],
        (raw_user_text or "")[:80],
    )
    try:
        await message.reply("⚠️ うまく返事できなかったです。もう一度試してください。")
        await status.delete_if_alive()
    except discord.errors.HTTPException as exc:
        log.warning("Fallback reply also failed: %s", exc)
    return None


async def publish_response(
    *,
    message: Any,
    bot_user: Any,
    renderer: Any,
    response_text: str,
    caller_is_admin: bool,
    attachment_rejections: list[tuple[str, str]],
    deps: PublishDeps,
    delivery_state: DeliveryState | None = None,
    on_first_ack: Callable[[], Any] | None = None,
    register_archive: Callable[..., Any] | None = None,
) -> DeliveryState:
    """Extract safe attachments and publish the final Discord response."""
    delivery = delivery_state or DeliveryState()
    if renderer.live_sent_any:
        delivery.streamed = True
        delivery.body_delivered = True
        message_ids = getattr(renderer, "delivery_message_ids", [])
        if message_ids:
            for message_id in message_ids:
                await delivery.record_message(
                    message_id,
                    on_first_ack=on_first_ack,
                )
        elif on_first_ack is not None:
            # Legacy renderers expose only live_sent_any. It still proves a
            # successful Discord acknowledgement, though no ID is available.
            await delivery.record_message(None, on_first_ack=on_first_ack)
    attach_files: list[discord.File] = []
    outbound_image_paths: list[str] = []
    if caller_is_admin:
        response_text = deps.normalize_image_filenames(response_text)
        for image_path in deps.extract_image_paths(response_text, admin=True):
            if deps.is_attach_ignored(image_path):
                continue
            try:
                attach_files.append(discord.File(image_path))
                outbound_image_paths.append(image_path)
                log.info("Attaching image: %s", image_path)
            except OSError as exc:
                log.warning(
                    "Failed to open image for attachment %s: [errno %s] %s",
                    image_path,
                    exc.errno,
                    exc,
                )
            except Exception as exc:
                log.warning("Failed to attach image %s: %s", image_path, exc)
        for file_path in deps.extract_file_paths(response_text):
            if deps.is_attach_ignored(file_path):
                continue
            try:
                attach_files.append(discord.File(file_path))
                log.info("Attaching file: %s", file_path)
            except OSError as exc:
                log.warning(
                    "Failed to open file for attachment %s: [errno %s] %s",
                    file_path,
                    exc.errno,
                    exc,
                )
            except Exception as exc:
                log.warning("Failed to attach file %s: %s", file_path, exc)

    generated_archive_registered = False

    def generated_archive_kwargs() -> dict[str, Any]:
        return {
            "channel_id": message.channel.id,
            "request_message_id": message.id,
            "direction": "outbound-generated",
            "author_id": getattr(bot_user, "id", None),
            "source": "agent-output",
        }

    async def archive_generated(response_message_id: int | None) -> None:
        nonlocal generated_archive_registered
        if not outbound_image_paths:
            return
        archive_kwargs = generated_archive_kwargs()
        if register_archive is not None:
            result = register_archive(
                paths=list(outbound_image_paths),
                kwargs=archive_kwargs,
                response_message_id=response_message_id,
            )
            if inspect.isawaitable(result):
                await result
            return
        if generated_archive_registered or response_message_id is None:
            return
        generated_archive_registered = True
        refs = await asyncio.to_thread(
            deps.archive_images,
            outbound_image_paths,
            **archive_kwargs,
        )
        if refs:
            log.info(
                "R2 staged %d generated image(s) for request %s",
                len(refs),
                message.id,
            )
            await asyncio.to_thread(
                deps.bind_response_message,
                refs,
                response_message_id,
            )

    # Persist archive intent before the Discord send. The response message ID
    # is filled after acknowledgement; restart reconciliation can also recover
    # it from the durable delivery receipt if cancellation lands in between.
    await archive_generated(None)

    try:
        if renderer.live_sent_any:
            deps.log_math_presentation(deps.parse_segments(response_text))
            await renderer.render_incremental(
                renderer.committed or response_text,
                final=True,
            )
            if attach_files:
                reply_files = attach_files
                if len(reply_files) > 10:
                    log.warning(
                        "Capping attachments %d -> 10 for Discord limit",
                        len(reply_files),
                    )
                    reply_files = reply_files[:10]
                sent = await message.channel.send(files=reply_files)
                await delivery.record_message(sent, on_first_ack=on_first_ack)
                await archive_generated(sent.id)
        else:
            masked = deps.mask_code_blocks(response_text)
            has_rich = deps.has_tables(
                response_text,
                _masked=masked,
            ) or deps.has_math(response_text, _masked=masked)
            if len(response_text) > 8000 and not has_rich:
                all_files = [
                    discord.File(io.StringIO(response_text), filename="response.md"),
                    *attach_files,
                ]
                if len(all_files) > 10:
                    log.warning(
                        "Capping attachments %d -> 10 for Discord limit",
                        len(all_files),
                    )
                    all_files = all_files[:10]
                sent = await message.reply(
                    "📄 長文のため response.md として添付します。",
                    files=all_files,
                )
                await delivery.record_message(sent, on_first_ack=on_first_ack)
                await archive_generated(sent.id)
            elif has_rich:
                response_message_id = await deps.send_segmented(
                    message,
                    response_text,
                    attach_files,
                    delivery_state=delivery,
                    on_first_ack=on_first_ack,
                    register_archive=register_archive,
                )
                await archive_generated(response_message_id)
            else:
                sender = deps.reply_chain_sender(message)
                reply_files = attach_files
                if reply_files and len(reply_files) > 10:
                    log.warning(
                        "Capping attachments %d -> 10 for Discord limit",
                        len(reply_files),
                    )
                    reply_files = reply_files[:10]
                for index, chunk in enumerate(deps.split_message(response_text)):
                    if index == 0 and reply_files:
                        sent = await sender.send(chunk, files=reply_files)
                        await delivery.record_message(
                            sent,
                            on_first_ack=on_first_ack,
                        )
                        await archive_generated(sent.id)
                    else:
                        sent = await sender.send(chunk)
                        await delivery.record_message(
                            sent,
                            on_first_ack=on_first_ack,
                        )
    finally:
        for attachment in attach_files:
            attachment.close()
        if renderer.rendered_images:
            try:
                rendered_metadata = {
                    path: {"response_message_id": response_id}
                    for path, response_id in renderer.rendered_image_message_ids.items()
                }
                archive_kwargs = {
                    "channel_id": message.channel.id,
                    "request_message_id": message.id,
                    "direction": "outbound-render",
                    "author_id": getattr(bot_user, "id", None),
                    "source": "stream-renderer",
                    "per_path_metadata": rendered_metadata,
                }
                if register_archive is not None:
                    result = register_archive(
                        paths=list(renderer.rendered_images),
                        kwargs=archive_kwargs,
                        response_message_id=None,
                        bind_response=False,
                    )
                    if inspect.isawaitable(result):
                        await result
                else:
                    refs = await asyncio.to_thread(
                        deps.archive_images,
                        list(renderer.rendered_images),
                        **archive_kwargs,
                    )
                    if refs:
                        log.info(
                            "R2 staged %d streamed rendered image(s)",
                            len(refs),
                        )
            finally:
                for path in renderer.rendered_images:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                renderer.rendered_images.clear()
                renderer.rendered_image_message_ids.clear()

    if attachment_rejections:
        lines = [
            f"⚠️ {name} は{reason}のため読み込めませんでした"
            for name, reason in attachment_rejections[:10]
        ]
        try:
            await message.channel.send("\n".join(lines))
        except discord.errors.HTTPException:
            pass
    return delivery
