"""Characterization tests for the stages composed by bot.py."""

from __future__ import annotations

import asyncio
import inspect
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot_pipeline import context, model, prompt, publish, record
from bot_pipeline.admission import AttachmentLimits
from bot_pipeline.attachment_types import (
    AttachmentArtifacts,
    AttachmentProvenance,
    BinaryArtifact,
    TextArtifact,
)
from utils.markdown_parser import TableSegment


class _Author:
    id = 42
    display_name = "Alice"

    def __str__(self) -> str:
        return "alice#0042"


class _PromptBuilder:
    @staticmethod
    def build_header(name, tag, is_admin):
        return f"HEADER({name}|{tag}|{is_admin})"

    @staticmethod
    def build_speaker_line(name, tag):
        return f"SPEAKER({name}|{tag})"

    @staticmethod
    def build_time_line():
        return "TIME"

    @staticmethod
    def build_sleep_directive(**_kwargs):
        return ""


def test_composition_root_preserves_stage_and_delivery_fence_order():
    import bot as composition_root

    source = inspect.getsource(composition_root.on_message)
    boundaries = [
        "bot_admission.admit_message",
        "provider_resume_hint",
        "bot_context.prepare_context",
        "bot_prompt.build_prompt",
        "bot_model.invoke_model",
        "bot_publish.prepare_response",
    ]
    offsets = [source.index(boundary) for boundary in boundaries]
    assert offsets == sorted(offsets)
    publish_call = source.index("published = await bot_publish.publish_response")
    delivery_commit = source.index(
        "if delivery.body_delivered:",
        publish_call,
    )
    assert publish_call < delivery_commit


@pytest.mark.asyncio
async def test_full_prompt_snapshot_preserves_component_order():
    message = SimpleNamespace(
        author=_Author(),
        channel=SimpleNamespace(id=7),
    )
    result = await prompt.build_prompt(
        prompt.PromptInput(
            message=message,
            job=SimpleNamespace(phase=None),
            status=SimpleNamespace(update=AsyncMock()),
            request_key=(7, 42),
            caller_is_admin=False,
            mem0_user_id="42",
            will_resume=False,
            reply_context="",
            reply_is_trusted=False,
            resolved_prompt="question",
            conv_context="context",
            cross_thread_context="",
            forwarded_context="",
            image_paths=[],
            text_attachments=[],
            arbitrary_attachments=[],
            mem0_task=None,
            invest_kb_task=None,
            history_search_task=None,
            history_embed_task=None,
            skill_overrides={},
        ),
        config=prompt.PromptConfig(
            max_prompt_chars=10_000,
            sleep_enforce_user_id=0,
            sleep_enforce_enabled=False,
            sleep_enforce_start_hour=0,
            sleep_enforce_end_hour=0,
        ),
        deps=prompt.PromptDeps(
            sanitize=lambda text: f"safe:{text}",
            wrap_user_input=prompt.wrap_user_input,
            cap_text=prompt.cap_text,
            load_history_channel=Mock(return_value=[]),
            format_memories_for_prompt=Mock(return_value=""),
            format_history_for_prompt=Mock(return_value=""),
            render_active_skill_prompt=Mock(return_value=""),
            skill_error_type=RuntimeError,
            get_request_id=lambda: "rid",
            prompt_builder=_PromptBuilder,
        ),
        recalling_phase="recalling",
    )

    assert result.prompt == (
        "HEADER(safe:Alice|safe:alice#0042|False)\n"
        "TIME\n"
        "<untrusted_user_input>\n"
        "safe:context\n\n"
        "safe:question\n"
        "</untrusted_user_input>"
    )
    assert result.path_label == "full"


@pytest.mark.asyncio
async def test_cold_start_resume_override_loads_history_into_full_prompt():
    import bot_preprocess

    message = SimpleNamespace(
        author=_Author(),
        channel=SimpleNamespace(id=7),
        reference=None,
    )
    fetch_channel_context = AsyncMock(return_value="full channel history")
    tasks = bot_preprocess.launch_context_tasks(
        bot=SimpleNamespace(user=SimpleNamespace(id=999)),
        message=message,
        prompt="question",
        raw_user_text="q",
        caller_is_admin=True,
        mem0_user_id="42",
        sanitize=lambda text: text,
        cap_text=prompt.cap_text,
        get_session=Mock(),
        is_session_fresh=Mock(),
        fetch_thread_context=AsyncMock(return_value="thread history"),
        fetch_channel_context=fetch_channel_context,
        resolve_message_links=AsyncMock(return_value="question"),
        resolve_channel_mention_contexts=AsyncMock(return_value=""),
        search_memories_smart=AsyncMock(),
        should_use_memory_llm_mode=Mock(),
        admin_user_ids={42},
        max_user_message_chars=10_000,
        mem0_min_query_length=100,
        mem0_smart_search=False,
        mem0_recall_limit=10,
        mem0_recall_limit_restricted=3,
        mem0_history_search_enabled=False,
        mem0_history_search_limit=3,
        mem0_history_embed_enabled=False,
        mem0_history_embed_limit=3,
        will_resume_override=False,
    )
    reply_context, _attachments, reply_is_trusted = await tasks.reply
    conv_context = await tasks.thread
    resolved_prompt = await tasks.links
    cross_thread_context = await tasks.cross_thread

    result = await prompt.build_prompt(
        prompt.PromptInput(
            message=message,
            job=SimpleNamespace(phase=None),
            status=SimpleNamespace(update=AsyncMock()),
            request_key=(7, 42),
            caller_is_admin=True,
            mem0_user_id="42",
            will_resume=tasks.will_resume,
            reply_context=reply_context,
            reply_is_trusted=reply_is_trusted,
            resolved_prompt=resolved_prompt,
            conv_context=conv_context,
            cross_thread_context=cross_thread_context,
            forwarded_context="",
            image_paths=[],
            text_attachments=[],
            arbitrary_attachments=[],
            mem0_task=tasks.mem0,
            invest_kb_task=tasks.invest_kb,
            history_search_task=tasks.history_search,
            history_embed_task=tasks.history_embed,
            skill_overrides={},
        ),
        config=prompt.PromptConfig(
            max_prompt_chars=10_000,
            sleep_enforce_user_id=0,
            sleep_enforce_enabled=False,
            sleep_enforce_start_hour=0,
            sleep_enforce_end_hour=0,
        ),
        deps=prompt.PromptDeps(
            sanitize=lambda text: f"safe:{text}",
            wrap_user_input=prompt.wrap_user_input,
            cap_text=prompt.cap_text,
            load_history_channel=Mock(return_value=[]),
            format_memories_for_prompt=Mock(return_value=""),
            format_history_for_prompt=Mock(return_value=""),
            render_active_skill_prompt=Mock(return_value=""),
            skill_error_type=RuntimeError,
            get_request_id=lambda: "rid",
            prompt_builder=_PromptBuilder,
        ),
        recalling_phase="recalling",
    )

    assert tasks.will_resume is False
    fetch_channel_context.assert_awaited_once()
    assert result.path_label == "full"
    assert "safe:full channel history" in result.prompt


@pytest.mark.asyncio
async def test_model_stage_preserves_provider_result_and_stream_callback():
    events: list[object] = []

    class Renderer:
        def __init__(self, *_args, **_kwargs):
            events.append("renderer")

        async def incremental_send(self, text):
            events.append(("incremental", text))

        async def stream_preview(self, text):
            events.append(("stream", text))

        async def tool_invocation_send(self, name, detail):
            events.append(("tool", name, detail))

    class Status:
        prog = Mock()

        async def update(self, text):
            events.append(("status", text))

        def spin_start(self, *, mode):
            events.append(("spin_start", mode))

        def spin_stop(self):
            events.append("spin_stop")

    captured = {}

    async def call_with_tools(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        await kwargs["text_stream_callback"]("cumulative snapshot")
        return "answer", "session", True

    message = SimpleNamespace(
        author=SimpleNamespace(id=42),
        channel=SimpleNamespace(id=7),
    )
    result = await model.invoke_model(
        model.ModelInput(
            bot=SimpleNamespace(),
            message=message,
            status=Status(),
            job=SimpleNamespace(phase=None),
            prompt="prompt",
            sessions={7: "old"},
            provider="claude",
            caller_is_admin=False,
            is_thread=False,
            raw_user_text="question",
            image_paths=[],
            text_attachments=[],
            arbitrary_attachments=[],
            forced_model=None,
            forced_effort=None,
            active_skill_prompt="",
            tier_overrides={},
        ),
        config=model.ModelConfig(
            scratchpad_stream_enabled=False,
            plan_stream_enabled=False,
            math_presentation_mode="legacy",
            deliberation_router_enabled=False,
            spinner_mode="text",
            admin_user_ids=set(),
        ),
        deps=model.ModelDeps(
            live_renderer=Renderer,
            track_task=Mock(),
            split_message=lambda text: [text],
            speaker_label=lambda _user_id: "speaker",
            provider_label=lambda _provider: "Claude",
            call_with_tools=call_with_tools,
        ),
        calling_phase="calling",
    )

    assert (result.response_text, result.session_info, result.actually_resumed) == (
        "answer",
        "session",
        True,
    )
    assert captured["args"] == ("prompt", 7, {7: "old"})
    assert captured["kwargs"]["provider"] == "claude"
    assert events == [
        ("status", "📨 Claudeへ送信中"),
        "renderer",
        ("spin_start", "text"),
        ("stream", "cumulative snapshot"),
        "spin_stop",
    ]


def test_forwarded_context_preserves_order_and_limit():
    snapshots = [
        SimpleNamespace(
            message=SimpleNamespace(
                content=f"body-{index}",
                author=SimpleNamespace(display_name=f"author-{index}"),
            ),
        )
        for index in range(4)
    ]
    rendered = context.build_forwarded_context(
        SimpleNamespace(message_snapshots=snapshots),
        lambda text: f"safe:{text}",
    )
    assert rendered == (
        "[Forwarded message by safe:author-0]\nsafe:body-0\n\n"
        "[Forwarded message by safe:author-1]\nsafe:body-1\n\n"
        "[Forwarded message by safe:author-2]\nsafe:body-2"
    )


@pytest.mark.asyncio
async def test_record_stage_keeps_admin_only_dispatch():
    recorder = AsyncMock()
    force_hook = Mock(return_value=True)
    common = dict(
        message=SimpleNamespace(),
        raw_user_text="question",
        response_text="answer",
        mem0_user_id="42",
        conv_context="context",
        record_turn=recorder,
        add_memories=Mock(),
        track_task=Mock(),
        load_history_channel=Mock(),
    )
    await record.record_admin_turn(caller_is_admin=False, **common)
    recorder.assert_not_awaited()
    await record.record_admin_turn(
        caller_is_admin=True,
        is_force_record_request_hook=force_hook,
        **common,
    )
    recorder.assert_awaited_once()
    assert recorder.await_args.kwargs["is_force_record_request"] is force_hook


@pytest.mark.asyncio
async def test_prepare_response_preserves_ui_teardown_order():
    events: list[str] = []

    async def pending_notice():
        try:
            await asyncio.Future()
        finally:
            events.append("notice_awaited")

    pending = asyncio.create_task(pending_notice())
    await asyncio.sleep(0)
    notice = SimpleNamespace(
        delete=AsyncMock(side_effect=lambda: events.append("delete"))
    )

    class Renderer:
        tool_notice_task = pending
        tool_notice_msg = notice
        live_sent_any = False

        def clear_tool_notice_msg(self):
            events.append("clear")

    class Status:
        def cancel_pending(self):
            events.append("status_cancel")

        async def delete_if_alive(self):
            events.append("status_delete")

    result = await publish.prepare_response(
        message=SimpleNamespace(
            channel=SimpleNamespace(id=7),
            author=SimpleNamespace(id=42),
            reply=AsyncMock(),
        ),
        status=Status(),
        renderer=Renderer(),
        response_text="`123` answer",
        raw_user_text="question",
        strip_numeric_inline_code=lambda text: text.replace("`", ""),
    )
    assert result == "123 answer"
    assert pending.done()
    assert events == [
        "notice_awaited",
        "delete",
        "clear",
        "status_cancel",
    ]


def test_cap_text_preserves_trust_wrapper_boundary():
    wrapped = "HEADER\n" + prompt.wrap_trusted("x" * 500) + "\nTAIL"
    capped = prompt.cap_text(wrapped, 80)

    assert "[...truncated by size cap...]" in capped
    assert capped.count("<trusted_user_input>") == 1
    assert capped.count("</trusted_user_input>") == 1
    assert capped.endswith("</trusted_user_input>")


@pytest.mark.asyncio
async def test_admin_prompt_uses_attachment_source_trust_not_caller_trust():
    direct = AttachmentProvenance(42, "discord-message", True)
    external = AttachmentProvenance(99, "discord-reply", False)
    artifacts = AttachmentArtifacts(
        texts=[
            TextArtifact("direct.txt", "direct body", direct),
            TextArtifact("external.txt", "external body", external),
        ],
        binaries=[
            BinaryArtifact(
                {
                    "path": "/tmp/direct.bin",
                    "filename": "direct.bin",
                    "mime": "application/octet-stream",
                    "size": 1,
                },
                direct,
            ),
        ],
    )
    message = SimpleNamespace(author=_Author(), channel=SimpleNamespace(id=7))
    result = await prompt.build_prompt(
        prompt.PromptInput(
            message=message,
            job=SimpleNamespace(phase=None),
            status=SimpleNamespace(update=AsyncMock()),
            request_key=(7, 42),
            caller_is_admin=True,
            mem0_user_id="42",
            will_resume=True,
            reply_context="",
            reply_is_trusted=False,
            resolved_prompt="question",
            conv_context="",
            cross_thread_context="",
            forwarded_context="",
            image_paths=[],
            text_attachments=[],
            arbitrary_attachments=[],
            mem0_task=None,
            invest_kb_task=None,
            history_search_task=None,
            history_embed_task=None,
            skill_overrides={},
            attachment_artifacts=artifacts,
        ),
        config=prompt.PromptConfig(10_000, 0, False, 0, 0),
        deps=prompt.PromptDeps(
            sanitize=lambda text: text,
            wrap_user_input=prompt.wrap_user_input,
            cap_text=prompt.cap_text,
            load_history_channel=Mock(return_value=[]),
            format_memories_for_prompt=Mock(return_value=""),
            format_history_for_prompt=Mock(return_value=""),
            render_active_skill_prompt=Mock(return_value=""),
            skill_error_type=RuntimeError,
            get_request_id=lambda: "rid",
            prompt_builder=_PromptBuilder,
        ),
        recalling_phase="recalling",
    )

    trusted_block = result.prompt.split("</trusted_user_input>", 1)[0]
    assert "direct body" in trusted_block
    assert "external body" not in trusted_block
    assert "<untrusted_user_input>" in result.prompt
    assert "external body" in result.prompt


@pytest.mark.asyncio
async def test_forwarded_message_is_outside_admin_trusted_input():
    message = SimpleNamespace(author=_Author(), channel=SimpleNamespace(id=7))
    result = await prompt.build_prompt(
        prompt.PromptInput(
            message=message,
            job=SimpleNamespace(phase=None),
            status=SimpleNamespace(update=AsyncMock()),
            request_key=(7, 42),
            caller_is_admin=True,
            mem0_user_id="42",
            will_resume=False,
            reply_context="",
            reply_is_trusted=False,
            resolved_prompt="trusted question",
            conv_context="",
            cross_thread_context="",
            forwarded_context="[Forwarded message by admin-spoof]\nuntrusted body",
            image_paths=[],
            text_attachments=[],
            arbitrary_attachments=[],
            mem0_task=None,
            invest_kb_task=None,
            history_search_task=None,
            history_embed_task=None,
            skill_overrides={},
        ),
        config=prompt.PromptConfig(10_000, 0, False, 0, 0),
        deps=prompt.PromptDeps(
            sanitize=lambda text: text,
            wrap_user_input=prompt.wrap_user_input,
            cap_text=prompt.cap_text,
            load_history_channel=Mock(return_value=[]),
            format_memories_for_prompt=Mock(return_value=""),
            format_history_for_prompt=Mock(return_value=""),
            render_active_skill_prompt=Mock(return_value=""),
            skill_error_type=RuntimeError,
            get_request_id=lambda: "rid",
            prompt_builder=_PromptBuilder,
        ),
        recalling_phase="recalling",
    )

    trusted_open = result.prompt.index("<trusted_user_input>\n")
    trusted_close = result.prompt.index("</trusted_user_input>")
    forwarded = result.prompt.index("<forwarded_messages>")
    untrusted_open = result.prompt.index("<untrusted_user_input>\n")
    untrusted_close = result.prompt.index("</untrusted_user_input>")
    assert trusted_open < trusted_close < untrusted_open < forwarded < untrusted_close
    assert "untrusted body" not in result.prompt[trusted_open:trusted_close]


@pytest.mark.asyncio
async def test_context_keeps_reply_attachment_provenance_and_denies_binary():
    async def value(result):
        return result

    class Owner:
        will_resume = False
        reply_author_id = 99
        mem0 = None
        invest_kb = None
        history_search = None
        history_embed = None

        def __init__(self):
            self.reply = asyncio.create_task(value(("reply", [reply], False)))
            self.links = asyncio.create_task(value("question"))
            self.thread = asyncio.create_task(value(""))
            self.cross_thread = asyncio.create_task(value(""))
            self.closed = False

        async def close(self):
            self.closed = True

    direct = SimpleNamespace(
        filename="direct.bin",
        content_type="application/octet-stream",
        size=1,
    )
    reply = SimpleNamespace(
        filename="reply.bin",
        content_type="application/octet-stream",
        size=1,
    )
    owner = Owner()
    classifier_calls: list[bool] = []

    def classifier(attachments, *, caller_is_admin):
        classifier_calls.append(caller_is_admin)
        if caller_is_admin:
            return len(attachments), []
        return 0, [(attachments[0].filename, "untrusted binary")]

    async def download_binaries(_session, attachments, *, caller_is_admin):
        if not caller_is_admin:
            return []
        return [
            {
                "path": "/tmp/direct.bin",
                "filename": attachments[0].filename,
                "mime": "application/octet-stream",
                "size": 1,
            }
        ]

    result = await context.prepare_context(
        bot=SimpleNamespace(),
        message=SimpleNamespace(
            attachments=[direct],
            author=SimpleNamespace(id=42),
            channel=SimpleNamespace(id=7),
            id=70,
            message_snapshots=[],
        ),
        status=SimpleNamespace(update=AsyncMock()),
        prompt="question",
        raw_user_text="question",
        caller_is_admin=True,
        mem0_user_id="42",
        will_resume_override=False,
        image_paths=[],
        config=context.ContextConfig(
            set(),
            1000,
            1000,
            False,
            1,
            1,
            False,
            1,
            False,
            1,
            AttachmentLimits(set(), set(), 10, 10, 10),
        ),
        deps=context.ContextDeps(
            launch_context_tasks=Mock(return_value=owner),
            sanitize=lambda text: text,
            cap_text=prompt.cap_text,
            get_session=Mock(),
            is_session_fresh=Mock(),
            fetch_thread_context=AsyncMock(),
            fetch_channel_context=AsyncMock(),
            resolve_message_links=AsyncMock(),
            resolve_channel_mention_contexts=AsyncMock(),
            search_memories_smart=AsyncMock(),
            should_use_memory_llm_mode=Mock(),
            get_http_session=AsyncMock(return_value=object()),
            download_attachments=AsyncMock(return_value=[]),
            attachment_metadata_by_path=Mock(return_value={}),
            archive_images=Mock(return_value=[]),
            download_text_attachments=AsyncMock(return_value=[]),
            download_arbitrary_attachments=download_binaries,
            classify_attachment_rejections=classifier,
        ),
    )

    assert classifier_calls == [True, False]
    assert [
        item.metadata["filename"] for item in result.attachment_artifacts.binaries
    ] == [
        "direct.bin",
    ]
    assert result.attachment_artifacts.binaries[0].provenance.trusted is False
    assert result.attachment_rejections == [("reply.bin", "untrusted binary")]
    await result.close()
    assert owner.closed is True


@pytest.mark.asyncio
async def test_context_task_owner_cancels_and_awaits_all_children():
    import bot_preprocess

    finalized: list[int] = []

    async def child(index):
        try:
            await asyncio.Future()
        finally:
            finalized.append(index)

    tasks = [asyncio.create_task(child(index)) for index in range(4)]
    await asyncio.sleep(0)
    owner = bot_preprocess.ContextTasks(
        False,
        *tasks,
        None,
        None,
        None,
        None,
    )
    await owner.close()

    assert sorted(finalized) == [0, 1, 2, 3]
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_segment_render_is_removed_when_send_fails(tmp_path):
    rendered = tmp_path / "table.png"
    rendered.write_bytes(b"png")
    sender = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("send failed")))
    deps = publish.SegmentedDeps(
        parse_segments=lambda _text: [TableSegment("|a|\n|-|\n|b|")],
        plan_segments=Mock(),
        classify_math=Mock(),
        latex_to_discord_text=lambda text: text,
        render_table_image=AsyncMock(return_value=str(rendered)),
        render_math_image=AsyncMock(),
        reply_chain_sender=Mock(return_value=sender),
        archive_images=Mock(return_value=[]),
        split_message=lambda text: [text],
    )

    with pytest.raises(RuntimeError, match="send failed"):
        await publish.send_segmented(
            SimpleNamespace(
                channel=SimpleNamespace(id=7),
                id=70,
            ),
            "table",
            [],
            bot_user=SimpleNamespace(id=1),
            math_presentation_mode="legacy",
            deps=deps,
        )
    assert not os.path.exists(rendered)


@pytest.mark.asyncio
async def test_stream_render_is_removed_when_archive_fails(tmp_path):
    rendered = tmp_path / "stream.png"
    rendered.write_bytes(b"png")
    renderer = SimpleNamespace(
        live_sent_any=True,
        committed="answer",
        render_incremental=AsyncMock(),
        rendered_images=[str(rendered)],
        rendered_image_message_ids={str(rendered): 123},
    )
    deps = publish.PublishDeps(
        normalize_image_filenames=lambda text: text,
        extract_image_paths=Mock(return_value=[]),
        extract_file_paths=Mock(return_value=[]),
        is_attach_ignored=Mock(return_value=False),
        archive_images=Mock(side_effect=RuntimeError("archive failed")),
        bind_response_message=Mock(),
        parse_segments=Mock(return_value=[]),
        mask_code_blocks=lambda text: text,
        has_tables=Mock(return_value=False),
        has_math=Mock(return_value=False),
        split_message=lambda text: [text],
        reply_chain_sender=Mock(),
        send_segmented=AsyncMock(),
        log_math_presentation=Mock(),
    )
    delivery = publish.DeliveryState()

    with pytest.raises(RuntimeError, match="archive failed"):
        await publish.publish_response(
            message=SimpleNamespace(
                channel=SimpleNamespace(id=7),
                id=70,
            ),
            bot_user=SimpleNamespace(id=1),
            renderer=renderer,
            response_text="answer",
            caller_is_admin=False,
            attachment_rejections=[],
            deps=deps,
            delivery_state=delivery,
        )

    assert delivery.streamed is True
    assert delivery.body_delivered is True
    assert not rendered.exists()
    assert renderer.rendered_images == []
    assert renderer.rendered_image_message_ids == {}


@pytest.mark.asyncio
async def test_nonstream_send_failure_keeps_delivery_uncommitted():
    renderer = SimpleNamespace(
        live_sent_any=False,
        rendered_images=[],
        rendered_image_message_ids={},
    )
    delivery = publish.DeliveryState()
    sender = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("send failed")))
    deps = publish.PublishDeps(
        normalize_image_filenames=lambda text: text,
        extract_image_paths=Mock(return_value=[]),
        extract_file_paths=Mock(return_value=[]),
        is_attach_ignored=Mock(return_value=False),
        archive_images=Mock(return_value=[]),
        bind_response_message=Mock(),
        parse_segments=Mock(return_value=[]),
        mask_code_blocks=lambda text: text,
        has_tables=Mock(return_value=False),
        has_math=Mock(return_value=False),
        split_message=lambda text: [text],
        reply_chain_sender=Mock(return_value=sender),
        send_segmented=AsyncMock(),
        log_math_presentation=Mock(),
    )

    with pytest.raises(RuntimeError, match="send failed"):
        await publish.publish_response(
            message=SimpleNamespace(
                channel=SimpleNamespace(id=7),
                id=70,
            ),
            bot_user=SimpleNamespace(id=1),
            renderer=renderer,
            response_text="answer",
            caller_is_admin=False,
            attachment_rejections=[],
            deps=deps,
            delivery_state=delivery,
        )

    assert delivery.streamed is False
    assert delivery.body_delivered is False


@pytest.mark.asyncio
async def test_nonstream_archive_failure_after_send_keeps_delivery_receipt(tmp_path):
    generated = tmp_path / "generated.png"
    generated.write_bytes(b"png")
    renderer = SimpleNamespace(
        live_sent_any=False,
        rendered_images=[],
        rendered_image_message_ids={},
    )
    delivery = publish.DeliveryState()
    sent = SimpleNamespace(id=701)
    sender = SimpleNamespace(send=AsyncMock(return_value=sent))
    deps = publish.PublishDeps(
        normalize_image_filenames=lambda text: text,
        extract_image_paths=Mock(return_value=[str(generated)]),
        extract_file_paths=Mock(return_value=[]),
        is_attach_ignored=Mock(return_value=False),
        archive_images=Mock(side_effect=RuntimeError("archive failed")),
        bind_response_message=Mock(),
        parse_segments=Mock(return_value=[]),
        mask_code_blocks=lambda text: text,
        has_tables=Mock(return_value=False),
        has_math=Mock(return_value=False),
        split_message=lambda text: [text],
        reply_chain_sender=Mock(return_value=sender),
        send_segmented=AsyncMock(),
        log_math_presentation=Mock(),
    )

    with pytest.raises(RuntimeError, match="archive failed"):
        await publish.publish_response(
            message=SimpleNamespace(
                channel=SimpleNamespace(id=7),
                id=70,
                reply=AsyncMock(),
            ),
            bot_user=SimpleNamespace(id=1),
            renderer=renderer,
            response_text=str(generated),
            caller_is_admin=True,
            attachment_rejections=[],
            deps=deps,
            delivery_state=delivery,
        )

    assert delivery.body_delivered is True
    assert delivery.message_ids == [701]


@pytest.mark.asyncio
async def test_permission_loser_task_is_cancelled_and_awaited():
    from services.claude_api.ask_bridge import get_ask_handler, set_ask_handler

    loser_finalized = asyncio.Event()
    approval = SimpleNamespace(
        id=123,
        add_reaction=AsyncMock(),
        reply=AsyncMock(),
    )
    reaction = SimpleNamespace(
        message=SimpleNamespace(id=123),
        emoji="✅",
    )
    admin = SimpleNamespace(id=42)

    async def wait_for(event, *, check):
        if event == "reaction_add":
            result = (reaction, admin)
            assert check(*result)
            return result
        try:
            await asyncio.Future()
        finally:
            loser_finalized.set()

    data = SimpleNamespace(
        message=SimpleNamespace(
            channel=SimpleNamespace(id=7, send=AsyncMock(return_value=approval)),
        ),
        bot=SimpleNamespace(wait_for=wait_for),
    )
    try:
        await model._install_permission_handler(
            data,
            model.ModelConfig(False, False, "legacy", False, "text", {42}),
        )
        handler = get_ask_handler()
        assert handler is not None
        assert await handler("Read", "detail", "rule") is True
        assert loser_finalized.is_set()
    finally:
        set_ask_handler(None)


def test_composition_root_injects_legacy_patch_points():
    import bot

    source = inspect.getsource(bot.on_message)
    assert "fmt_clock=_fmt_clock" in source
    assert "classify_attachment_rejections=_classify_attachment_rejections" in source
    reconcile_source = inspect.getsource(bot._reconcile_turn_outbox)
    assert "is_force_record_request_hook=_is_force_record_request" in reconcile_source
    assert "register_renderer=_register_turn_renderer" in source
