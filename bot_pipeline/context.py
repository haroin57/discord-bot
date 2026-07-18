"""Context and attachment acquisition stage for a Discord turn."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

from bot_pipeline.admission import AttachmentLimits, classify_attachment_rejections
from bot_pipeline.attachment_types import (
    AttachmentArtifacts,
    AttachmentProvenance,
    BinaryArtifact,
    ImageArtifact,
    TextArtifact,
)


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextConfig:
    admin_user_ids: set[int]
    max_user_message_chars: int
    mem0_min_query_length: int
    mem0_smart_search: bool
    mem0_recall_limit: int
    mem0_recall_limit_restricted: int
    mem0_history_search_enabled: bool
    mem0_history_search_limit: int
    mem0_history_embed_enabled: bool
    mem0_history_embed_limit: int
    attachment_limits: AttachmentLimits


@dataclass(frozen=True)
class ContextDeps:
    launch_context_tasks: Callable[..., Any]
    sanitize: Callable[[str], str]
    cap_text: Callable[[str, int], str]
    get_session: Callable[..., Any]
    is_session_fresh: Callable[..., bool]
    fetch_thread_context: Callable[..., Any]
    fetch_channel_context: Callable[..., Any]
    resolve_message_links: Callable[..., Any]
    resolve_channel_mention_contexts: Callable[..., Any]
    search_memories_smart: Callable[..., Any]
    should_use_memory_llm_mode: Callable[..., Any]
    get_http_session: Callable[..., Any]
    download_attachments: Callable[..., Any]
    attachment_metadata_by_path: Callable[..., Any]
    archive_images: Callable[..., Any]
    download_text_attachments: Callable[..., Any]
    download_arbitrary_attachments: Callable[..., Any]
    classify_attachment_rejections: Callable[..., Any] | None = None
    create_task: Callable[..., asyncio.Task] | None = None


@dataclass(frozen=True)
class ContextResult:
    will_resume: bool
    reply_context: str
    reply_is_trusted: bool
    resolved_prompt: str
    conv_context: str
    cross_thread_context: str
    forwarded_context: str
    image_paths: list[str]
    text_attachments: list[tuple[str, str]]
    arbitrary_attachments: list[dict]
    attachment_artifacts: AttachmentArtifacts
    attachment_rejections: list[tuple[str, str]]
    mem0_task: Any
    invest_kb_task: Any
    history_search_task: Any
    history_embed_task: Any
    task_owner: Any

    async def close(self) -> None:
        """Release preprocessing tasks that were not consumed downstream."""
        close = getattr(self.task_owner, "close", None)
        if close is not None:
            await close()


def build_forwarded_context(message: Any, sanitize: Callable[[str], str]) -> str:
    """Render at most three forwarded-message snapshots as untrusted context."""
    snapshots = getattr(message, "message_snapshots", None)
    if not snapshots:
        return ""
    parts: list[str] = []
    for snapshot in snapshots[:3]:
        snap_msg = getattr(snapshot, "message", None)
        if snap_msg is None:
            continue
        content = getattr(snap_msg, "content", "") or ""
        author = getattr(snap_msg, "author", None)
        author_name = (
            getattr(author, "display_name", "Unknown") if author else "Unknown"
        )
        if content:
            parts.append(
                f"[Forwarded message by {sanitize(author_name)}]\n{sanitize(content)}",
            )
        attachments = list(getattr(snap_msg, "attachments", None) or [])
        if attachments:
            metadata = []
            for attachment in attachments[:10]:
                filename = sanitize(getattr(attachment, "filename", "attachment"))
                content_type = sanitize(
                    getattr(attachment, "content_type", "") or "unknown",
                )
                size = getattr(attachment, "size", 0)
                metadata.append(
                    f"- filename={filename!r} mime={content_type!r} size={size}",
                )
            parts.append("[Forwarded attachment metadata]\n" + "\n".join(metadata))
    return "\n\n".join(parts)


async def prepare_context(
    *,
    bot: Any,
    message: Any,
    status: Any,
    prompt: str,
    raw_user_text: str,
    caller_is_admin: bool,
    mem0_user_id: str,
    will_resume_override: bool | None,
    image_paths: list[str],
    binary_paths: list[str] | None = None,
    config: ContextConfig,
    deps: ContextDeps,
) -> ContextResult:
    """Launch recall tasks, acquire attachments, and resolve Discord context."""
    launched = deps.launch_context_tasks(
        bot=bot,
        message=message,
        prompt=prompt,
        raw_user_text=raw_user_text,
        caller_is_admin=caller_is_admin,
        mem0_user_id=mem0_user_id,
        sanitize=deps.sanitize,
        cap_text=deps.cap_text,
        get_session=deps.get_session,
        is_session_fresh=deps.is_session_fresh,
        fetch_thread_context=deps.fetch_thread_context,
        fetch_channel_context=deps.fetch_channel_context,
        resolve_message_links=deps.resolve_message_links,
        resolve_channel_mention_contexts=deps.resolve_channel_mention_contexts,
        search_memories_smart=deps.search_memories_smart,
        should_use_memory_llm_mode=deps.should_use_memory_llm_mode,
        admin_user_ids=config.admin_user_ids,
        max_user_message_chars=config.max_user_message_chars,
        mem0_min_query_length=config.mem0_min_query_length,
        mem0_smart_search=config.mem0_smart_search,
        mem0_recall_limit=config.mem0_recall_limit,
        mem0_recall_limit_restricted=config.mem0_recall_limit_restricted,
        mem0_history_search_enabled=config.mem0_history_search_enabled,
        mem0_history_search_limit=config.mem0_history_search_limit,
        mem0_history_embed_enabled=config.mem0_history_embed_enabled,
        mem0_history_embed_limit=config.mem0_history_embed_limit,
        will_resume_override=will_resume_override,
        create_task=deps.create_task,
    )

    try:
        reply_result = await launched.reply
        if len(reply_result) == 4:
            (
                reply_context,
                reference_attachments,
                reply_is_trusted,
                reference_author_id,
            ) = reply_result
        else:
            # Canonical preprocess contract remains the legacy three-tuple;
            # provenance metadata travels on the task owner.
            reply_context, reference_attachments, reply_is_trusted = reply_result
            reference_author_id = getattr(launched, "reply_author_id", None)

        direct_attachments = list(message.attachments)
        reference_attachments = list(reference_attachments)
        all_attachments = direct_attachments + reference_attachments
        if all_attachments:
            log.info(
                "incoming attachments: count=%d %s",
                len(all_attachments),
                [
                    {
                        "filename": getattr(item, "filename", None),
                        "content_type": getattr(item, "content_type", None),
                        "size": getattr(item, "size", None),
                    }
                    for item in all_attachments
                ],
            )

        direct_source = AttachmentProvenance(
            sender_id=getattr(message.author, "id", None),
            origin="discord-message",
            trusted=caller_is_admin,
        )
        reply_source = AttachmentProvenance(
            sender_id=reference_author_id,
            origin="discord-reply",
            trusted=bool(reply_is_trusted),
        )
        groups = (
            (direct_attachments, direct_source),
            (reference_attachments, reply_source),
        )

        accepted = 0
        rejected: list[tuple[str, str]] = []
        classifier = deps.classify_attachment_rejections
        for attachments, source in groups:
            if not attachments:
                continue
            if classifier is None:
                group_accepted, group_rejected = classify_attachment_rejections(
                    attachments,
                    caller_is_admin=source.trusted,
                    limits=config.attachment_limits,
                )
            else:
                group_accepted, group_rejected = classifier(
                    attachments,
                    caller_is_admin=source.trusted,
                )
            accepted += group_accepted
            rejected.extend(group_rejected)
        if all_attachments:
            await status.update(
                f"📎 添付ファイルを確認中({accepted}件受理"
                + (f"・{len(rejected)}件不可" if rejected else "")
                + ")",
            )

        artifacts = AttachmentArtifacts()
        text_attachments: list[tuple[str, str]] = []
        arbitrary_attachments: list[dict] = []
        http_session = await deps.get_http_session()
        # Keep the direct downloader call even for an empty Discord list. The
        # bot namespace exposes these callables as compatibility patch points,
        # and existing integrations inject synthetic attachment results there.
        download_groups = [(direct_attachments, direct_source)]
        if reference_attachments:
            download_groups.append((reference_attachments, reply_source))
        for attachments, source in download_groups:
            downloaded_images = await deps.download_attachments(
                http_session,
                attachments,
            )
            image_paths.extend(downloaded_images)
            artifacts.images.extend(
                ImageArtifact(path=path, provenance=source)
                for path in downloaded_images
            )
            if downloaded_images:
                inbound_metadata = deps.attachment_metadata_by_path(
                    attachments,
                    downloaded_images,
                )
                refs = await asyncio.to_thread(
                    deps.archive_images,
                    downloaded_images,
                    channel_id=message.channel.id,
                    request_message_id=message.id,
                    direction="inbound",
                    author_id=source.sender_id,
                    source="discord-attachment",
                    per_path_metadata=inbound_metadata,
                )
                if refs:
                    log.info(
                        "R2 staged %d inbound image(s) for message %s",
                        len(refs),
                        message.id,
                    )

            downloaded_texts = await deps.download_text_attachments(
                http_session,
                attachments,
            )
            text_attachments.extend(downloaded_texts)
            artifacts.texts.extend(
                TextArtifact(
                    filename=filename,
                    content=content,
                    provenance=source,
                )
                for filename, content in downloaded_texts
            )

            # Binary inspection is authorized by attachment provenance, not by
            # the author of the message that happened to reference it.
            downloaded_binaries = await deps.download_arbitrary_attachments(
                http_session,
                attachments,
                caller_is_admin=source.trusted,
            )
            arbitrary_attachments.extend(downloaded_binaries)
            if binary_paths is not None:
                binary_paths.extend(
                    str(metadata["path"])
                    for metadata in downloaded_binaries
                    if metadata.get("path")
                )
            artifacts.binaries.extend(
                BinaryArtifact(
                    metadata=metadata,
                    provenance=AttachmentProvenance(
                        sender_id=source.sender_id,
                        origin=source.origin,
                        trusted=False,
                    ),
                )
                for metadata in downloaded_binaries
            )

        resolved_prompt = await launched.links
        conv_context = await launched.thread
        cross_thread_context = await launched.cross_thread
        return ContextResult(
            will_resume=launched.will_resume,
            reply_context=reply_context,
            reply_is_trusted=reply_is_trusted,
            resolved_prompt=resolved_prompt,
            conv_context=conv_context,
            cross_thread_context=cross_thread_context,
            forwarded_context=build_forwarded_context(message, deps.sanitize),
            image_paths=image_paths,
            text_attachments=text_attachments,
            arbitrary_attachments=arbitrary_attachments,
            attachment_artifacts=artifacts,
            attachment_rejections=rejected,
            mem0_task=launched.mem0,
            invest_kb_task=launched.invest_kb,
            history_search_task=launched.history_search,
            history_embed_task=launched.history_embed,
            task_owner=launched,
        )
    except BaseException:
        close = getattr(launched, "close", None)
        if close is not None:
            await close()
        raise
