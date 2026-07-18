from __future__ import annotations

import asyncio
import os
import struct
import time
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from PIL import Image

import bot_preprocess
import bot_ui
from services.mem0 import batch as mem0_batch
from bot_pipeline import prompt, publish
from bot_pipeline.attachment_types import (
    AttachmentArtifacts,
    AttachmentProvenance,
    BinaryArtifact,
)
from bot_pipeline.fifo import FifoAdmission
from bot_pipeline.runtime import RuntimeResources
from bot_pipeline.turn_outbox import TurnOutbox
from utils.context_gateway import ContextBlock, ContextItem
from utils import (
    attachment_downloader,
    attachments as attachment_utils,
    context_gateway,
)
from utils.render_store import is_cached
from utils.attachment_downloader import (
    download_arbitrary,
    download_image,
    download_text,
)


_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc````"
    b"\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB\x60\x82"
)


class _Author:
    id = 42
    display_name = "Admin"

    def __str__(self) -> str:
        return "admin#0042"


class _PromptBuilder:
    build_header = staticmethod(lambda *_args: "HEADER")
    build_speaker_line = staticmethod(lambda *_args: "SPEAKER")
    build_time_line = staticmethod(lambda: "TIME")
    build_sleep_directive = staticmethod(lambda **_kwargs: "")


def _prompt_deps() -> prompt.PromptDeps:
    return prompt.PromptDeps(
        sanitize=lambda value: value,
        wrap_user_input=prompt.wrap_user_input,
        cap_text=prompt.cap_text,
        load_history_channel=Mock(return_value=[]),
        format_memories_for_prompt=Mock(return_value=""),
        format_history_for_prompt=Mock(return_value=""),
        render_active_skill_prompt=Mock(return_value=""),
        skill_error_type=RuntimeError,
        get_request_id=lambda: "rid",
        prompt_builder=_PromptBuilder,
    )


def _prompt_input(**overrides) -> prompt.PromptInput:
    values = {
        "message": SimpleNamespace(author=_Author(), channel=SimpleNamespace(id=7)),
        "job": SimpleNamespace(phase=None),
        "status": SimpleNamespace(update=AsyncMock()),
        "request_key": (7, 42),
        "caller_is_admin": True,
        "mem0_user_id": "42",
        "will_resume": False,
        "reply_context": "",
        "reply_is_trusted": False,
        "resolved_prompt": "question",
        "conv_context": "",
        "cross_thread_context": "",
        "forwarded_context": "",
        "image_paths": [],
        "text_attachments": [],
        "arbitrary_attachments": [],
        "mem0_task": None,
        "invest_kb_task": None,
        "history_search_task": None,
        "history_embed_task": None,
        "skill_overrides": {},
    }
    values.update(overrides)
    return prompt.PromptInput(**values)


@pytest.mark.asyncio
async def test_forward_reference_never_enters_reply_resolver() -> None:
    fetch = AsyncMock()
    reference = SimpleNamespace(
        type=discord.MessageReferenceType.forward,
        message_id=99,
        resolved=SimpleNamespace(content="forwarded"),
        cached_message=SimpleNamespace(content="forwarded"),
    )
    message = SimpleNamespace(
        reference=reference,
        channel=SimpleNamespace(id=7, fetch_message=fetch),
        author=_Author(),
    )
    tasks = bot_preprocess.launch_context_tasks(
        bot=SimpleNamespace(user=SimpleNamespace(id=1)),
        message=message,
        prompt="question",
        raw_user_text="q",
        caller_is_admin=True,
        mem0_user_id="42",
        sanitize=lambda value: value,
        cap_text=prompt.cap_text,
        get_session=Mock(),
        is_session_fresh=Mock(),
        fetch_thread_context=AsyncMock(return_value=""),
        fetch_channel_context=AsyncMock(return_value=""),
        resolve_message_links=AsyncMock(return_value="question"),
        resolve_channel_mention_contexts=AsyncMock(return_value=""),
        search_memories_smart=AsyncMock(),
        should_use_memory_llm_mode=Mock(),
        admin_user_ids={42},
        max_user_message_chars=1000,
        mem0_min_query_length=100,
        mem0_smart_search=False,
        mem0_recall_limit=1,
        mem0_recall_limit_restricted=1,
        mem0_history_search_enabled=False,
        mem0_history_search_limit=1,
        mem0_history_embed_enabled=False,
        mem0_history_embed_limit=1,
    )
    assert await tasks.reply == ("", [], False)
    fetch.assert_not_awaited()
    await tasks.close()


@pytest.mark.asyncio
async def test_unprovenanced_assets_and_binary_metadata_are_untrusted() -> None:
    binary = {
        "path": "/tmp/payload.bin",
        "filename": "payload.bin",
        "mime": "application/octet-stream",
        "size": 3,
    }
    result = await prompt.build_prompt(
        _prompt_input(
            arbitrary_attachments=[binary],
            attachment_artifacts=None,
        ),
        config=prompt.PromptConfig(10_000, 0, False, 0, 0),
        deps=_prompt_deps(),
        recalling_phase="recalling",
    )
    trusted = result.prompt.split("</trusted_user_input>", 1)[0]
    assert "/tmp/payload.bin" not in trusted
    assert "/tmp/payload.bin" in result.prompt.split("<untrusted_user_input>", 1)[1]

    result = await prompt.build_prompt(
        _prompt_input(
            attachment_artifacts=AttachmentArtifacts(
                binaries=[
                    BinaryArtifact(
                        binary,
                        AttachmentProvenance(42, "discord-message", True),
                    )
                ]
            )
        ),
        config=prompt.PromptConfig(10_000, 0, False, 0, 0),
        deps=_prompt_deps(),
        recalling_phase="recalling",
    )
    trusted = result.prompt.split("</trusted_user_input>", 1)[0]
    assert "/tmp/payload.bin" not in trusted


@pytest.mark.asyncio
async def test_context_items_preserve_author_trust_partition() -> None:
    block = ContextBlock(
        "combined",
        label="Channel history",
        items=(
            ContextItem(42, "Admin", "own", "Admin: own", True),
            ContextItem(99, "Admin-spoof", "external", "Admin-spoof: external", False),
        ),
    )
    result = await prompt.build_prompt(
        _prompt_input(conv_context=block),
        config=prompt.PromptConfig(10_000, 0, False, 0, 0),
        deps=_prompt_deps(),
        recalling_phase="recalling",
    )
    trusted = result.prompt.split("</trusted_user_input>", 1)[0]
    assert "Admin: own" in trusted
    assert "Admin-spoof: external" not in trusted
    assert (
        "Admin-spoof: external" in result.prompt.split("<untrusted_user_input>", 1)[1]
    )


@pytest.mark.asyncio
async def test_context_gateway_trusts_only_injected_exact_identities() -> None:
    messages = [
        SimpleNamespace(
            author=SimpleNamespace(id=900, display_name="Current", bot=True),
            content="current bot",
        ),
        SimpleNamespace(
            author=SimpleNamespace(id=42, display_name="Admin", bot=False),
            content="admin",
        ),
        SimpleNamespace(
            author=SimpleNamespace(id=901, display_name="External", bot=True),
            content="external bot",
        ),
    ]

    class Channel:
        id = 7

        async def history(self, **_kwargs):
            for item in messages:
                yield item

    before = SimpleNamespace(id=70, author=SimpleNamespace(id=42))
    block = await context_gateway.fetch_channel_context(
        Channel(),
        before,
        caller_is_admin=True,
        bot_user_id=900,
        admin_user_ids={42},
        context_limit=10,
        cache_get=lambda _key: None,
        cache_set=lambda _key, _value: None,
        should_include=lambda _message, _admin: True,
        format_body=lambda message: message.content,
        build_lines=lambda pairs: ([f"{name}: {body}" for name, body in pairs], 0),
    )
    trusted = {item.author_id: item.trusted for item in block.items}
    assert trusted == {900: True, 42: True, 901: False}

    fail_closed = await context_gateway.fetch_channel_context(
        Channel(),
        before,
        caller_is_admin=True,
        bot_user_id=None,
        admin_user_ids={42},
        context_limit=10,
        cache_get=lambda _key: None,
        cache_set=lambda _key, _value: None,
        should_include=lambda _message, _admin: True,
        format_body=lambda message: message.content,
        build_lines=lambda pairs: ([f"{name}: {body}" for name, body in pairs], 0),
    )
    assert {item.author_id: item.trusted for item in fail_closed.items} == {
        900: False,
        42: True,
        901: False,
    }


@pytest.mark.asyncio
async def test_fifo_ticket_reservation_survives_middle_cancellation() -> None:
    gate = FifoAdmission()
    first = gate.issue()
    second = gate.issue()
    third = gate.issue()
    order: list[int] = []
    release_first = asyncio.Event()

    async def run(ticket, number):
        await ticket.wait()
        order.append(number)
        if number == 1:
            await release_first.wait()
        ticket.release()

    tasks = [
        asyncio.create_task(run(first, 1)),
        asyncio.create_task(run(second, 2)),
        asyncio.create_task(run(third, 3)),
    ]
    await asyncio.sleep(0)
    tasks[1].cancel()
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert order == [1, 3]
    assert gate.idle


@pytest.mark.asyncio
async def test_fifo_async_context_is_cancel_safe_and_release_is_idempotent() -> None:
    gate = FifoAdmission()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[int] = []

    async def worker(number: int) -> None:
        async with gate:
            order.append(number)
            if number == 1:
                first_entered.set()
                await release_first.wait()

    first = asyncio.create_task(worker(1))
    await first_entered.wait()
    cancelled = asyncio.create_task(worker(2))
    third = asyncio.create_task(worker(3))
    await asyncio.sleep(0)
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    release_first.set()
    await asyncio.gather(first, third)
    assert order == [1, 3]
    assert await gate.__aexit__(None, None, None) is False
    assert gate.idle


@pytest.mark.asyncio
async def test_partial_publish_records_each_ack_before_failure() -> None:
    sent = SimpleNamespace(id=101)
    sender = SimpleNamespace(
        send=AsyncMock(side_effect=[sent, RuntimeError("second send failed")]),
    )
    delivery = publish.DeliveryState(request_id="rid")
    first_ack = AsyncMock()
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
        split_message=lambda _text: ["one", "two"],
        reply_chain_sender=Mock(return_value=sender),
        send_segmented=AsyncMock(),
        log_math_presentation=Mock(),
    )
    with pytest.raises(RuntimeError, match="second send failed"):
        await publish.publish_response(
            message=SimpleNamespace(channel=SimpleNamespace(id=7), id=70),
            bot_user=SimpleNamespace(id=1),
            renderer=SimpleNamespace(
                live_sent_any=False,
                rendered_images=[],
                rendered_image_message_ids={},
            ),
            response_text="answer",
            caller_is_admin=False,
            attachment_rejections=[],
            deps=deps,
            delivery_state=delivery,
            on_first_ack=first_ack,
        )
    assert delivery.body_delivered is True
    assert delivery.message_ids == [101]
    first_ack.assert_awaited_once()


class _DownloadResponse:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def iter_chunked(self, _size):
        yield self._payload

    async def read(self):
        return self._payload


@pytest.mark.asyncio
async def test_attachment_mime_and_control_mismatch_falls_to_binary_policy(
    tmp_path,
) -> None:
    image_attachment = SimpleNamespace(
        id=1,
        size=len(_PNG),
        filename="spoof.jpg",
        content_type="image/jpeg",
        url="https://cdn.discordapp.com/spoof.jpg",
    )
    image_session = SimpleNamespace(
        get=lambda *_args, **_kwargs: _DownloadResponse(_PNG),
    )
    assert (
        await download_image(
            image_session,
            image_attachment,
            attachment_dir=str(tmp_path),
            max_bytes=1024,
            semaphore=asyncio.Semaphore(1),
            host_check=lambda _url: True,
        )
        is None
    )

    text_attachment = SimpleNamespace(
        id=2,
        size=4,
        filename="spoof.txt",
        content_type="text/plain",
        url="https://cdn.discordapp.com/spoof.txt",
    )
    text_session = SimpleNamespace(
        get=lambda *_args, **_kwargs: _DownloadResponse(b"a\x00b\n"),
    )
    assert (
        await download_text(
            text_session,
            text_attachment,
            max_bytes=1024,
            semaphore=asyncio.Semaphore(1),
            host_check=lambda _url: True,
        )
        is None
    )
    metadata = await download_arbitrary(
        text_session,
        text_attachment,
        attachment_dir=str(tmp_path),
        max_bytes=1024,
        denied_extensions=frozenset(),
        semaphore=asyncio.Semaphore(1),
        host_check=lambda _url: True,
        sanitize_basename=lambda name: name,
    )
    assert metadata is not None
    assert metadata["path"].endswith("spoof.txt")


def test_attachment_image_caps_and_decompression_warning_fail_closed(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "bounded.png"
    Image.new("RGB", (4, 4)).save(path)
    assert not attachment_downloader._fully_decoded_image(
        str(path),
        declared_mime="image/png",
        max_width=3,
        max_height=10,
        max_pixels=100,
    )
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    assert not attachment_downloader._fully_decoded_image(
        str(path),
        declared_mime="image/png",
        max_width=10,
        max_height=10,
        max_pixels=100,
    )


@pytest.mark.asyncio
async def test_attachment_wrapper_propagates_configured_decode_caps(
    monkeypatch,
) -> None:
    downloader = AsyncMock(return_value=None)
    monkeypatch.setattr(attachment_downloader, "download_image", downloader)
    monkeypatch.setattr(attachment_utils, "MAX_ATTACHMENT_IMAGE_WIDTH", 101)
    monkeypatch.setattr(attachment_utils, "MAX_ATTACHMENT_IMAGE_HEIGHT", 102)
    monkeypatch.setattr(attachment_utils, "MAX_ATTACHMENT_IMAGE_PIXELS", 103)
    await attachment_utils._download_one(SimpleNamespace(), SimpleNamespace())
    assert downloader.await_args.kwargs["max_width"] == 101
    assert downloader.await_args.kwargs["max_height"] == 102
    assert downloader.await_args.kwargs["max_pixels"] == 103


@pytest.mark.asyncio
async def test_runtime_cancels_active_job_before_browser_and_rejects_late_task() -> (
    None
):
    finalized = asyncio.Event()

    async def active_job():
        try:
            await asyncio.Future()
        finally:
            finalized.set()

    active = asyncio.create_task(active_job())
    await asyncio.sleep(0)

    async def close_browser():
        assert finalized.is_set()

    resources = RuntimeResources(
        background_tasks=set(),
        close_browser=close_browser,
        active_tasks=lambda: [active],
    )
    await resources.close()

    async def late():
        return None

    with pytest.raises(RuntimeError, match="closing"):
        resources.create(late(), name="late")


@pytest.mark.asyncio
async def test_runtime_drains_active_job_without_fencing_owned_resources(tmp_path) -> None:
    release_job = asyncio.Event()
    job_finished = asyncio.Event()
    registered: list[object] = []
    unregistered: list[object] = []
    background_tasks: set[asyncio.Task] = set()

    async def active_job() -> None:
        await release_job.wait()
        job_finished.set()

    active = asyncio.create_task(active_job())
    await asyncio.sleep(0)
    resources = RuntimeResources(
        background_tasks=background_tasks,
        close_browser=lambda: asyncio.sleep(0),
        active_tasks=lambda: [active],
        drain_timeout_s=1,
    )
    job = SimpleNamespace(task=active)
    resources.register_job(
        job,
        register=registered.append,
        unregister=unregistered.append,
    )

    closer = asyncio.create_task(resources.close())
    await asyncio.sleep(0)
    assert resources.accepting_jobs is False
    assert not active.cancelled()

    late_owned_task = resources.create(asyncio.sleep(0), name="accepted-turn-child")
    await late_owned_task
    rendered = tmp_path / "accepted-turn-render.png"
    rendered.write_bytes(_PNG)
    renderer = SimpleNamespace(
        tool_notice_task=None,
        tool_notice_msg=None,
        rendered_images=[str(rendered)],
        rendered_image_message_ids={str(rendered): 1},
    )
    resources.register_renderer(renderer)
    release_job.set()
    await closer

    assert job_finished.is_set()
    assert not rendered.exists()
    assert registered == [job]
    assert unregistered == [job]


@pytest.mark.asyncio
async def test_runtime_marks_forced_drain_cancellation_as_shutdown() -> None:
    cancellation_args: list[tuple[object, ...]] = []

    async def active_job() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError as exc:
            cancellation_args.append(exc.args)
            raise

    active = asyncio.create_task(active_job())
    await asyncio.sleep(0)
    resources = RuntimeResources(
        background_tasks=set(),
        close_browser=lambda: asyncio.sleep(0),
        active_tasks=lambda: [active],
        drain_timeout_s=0.01,
    )

    await resources.close()

    assert cancellation_args == [("shutdown",)]


@pytest.mark.asyncio
async def test_runtime_force_cancels_parent_and_owned_child_in_same_wave() -> None:
    resources: RuntimeResources
    child_started = asyncio.Event()

    async def child_work() -> None:
        child_started.set()
        await asyncio.Future()

    active: asyncio.Task
    resources = RuntimeResources(
        background_tasks=set(),
        close_browser=lambda: asyncio.sleep(0),
        active_tasks=lambda: [active],
        drain_timeout_s=0,
        force_cancel_timeout_s=0.5,
    )
    child = resources.create(child_work(), name="owned-child")
    await child_started.wait()

    async def parent_job() -> None:
        try:
            await asyncio.Future()
        finally:
            await child

    active = asyncio.create_task(parent_job())
    await asyncio.sleep(0)

    await asyncio.wait_for(resources.close(), timeout=1)

    assert active.done()
    assert child.done()


@pytest.mark.asyncio
async def test_runtime_force_cleanup_is_bounded_when_task_suppresses_cancel() -> None:
    release_stubborn = asyncio.Event()
    browser_closed = asyncio.Event()

    async def stubborn_job() -> None:
        while not release_stubborn.is_set():
            try:
                await release_stubborn.wait()
            except asyncio.CancelledError:
                continue

    active = asyncio.create_task(stubborn_job())
    await asyncio.sleep(0)

    async def close_browser() -> None:
        browser_closed.set()

    resources = RuntimeResources(
        background_tasks=set(),
        close_browser=close_browser,
        active_tasks=lambda: [active],
        drain_timeout_s=0,
        force_cancel_timeout_s=0.02,
    )

    await asyncio.wait_for(resources.close(), timeout=0.5)
    assert browser_closed.is_set()
    assert not active.done()

    release_stubborn.set()
    await active


@pytest.mark.asyncio
async def test_runtime_drains_dispatch_after_job_phase_becomes_terminal() -> None:
    release_finally = asyncio.Event()
    finally_started = asyncio.Event()

    async def dispatch() -> None:
        finally_started.set()
        await release_finally.wait()

    task = asyncio.create_task(dispatch())
    await finally_started.wait()
    resources = RuntimeResources(
        background_tasks=set(),
        close_browser=lambda: asyncio.sleep(0),
        # Simulate live_job_tasks() dropping DONE/FAILED before task finally.
        active_tasks=lambda: [],
        drain_timeout_s=1,
    )
    assert resources.register_dispatch_task(task)

    closer = asyncio.create_task(resources.close())
    await asyncio.sleep(0)
    assert not closer.done()
    assert not task.cancelled()

    release_finally.set()
    await closer
    assert task.done()


@pytest.mark.asyncio
async def test_runtime_repeated_close_cancellation_drains_and_fences_late_jobs() -> (
    None
):
    browser_started = asyncio.Event()
    allow_browser_close = asyncio.Event()
    job_finalized = asyncio.Event()
    registered: list[object] = []
    unregistered: list[object] = []

    async def active_job() -> None:
        try:
            await asyncio.Future()
        finally:
            job_finalized.set()

    active = asyncio.create_task(active_job())
    await asyncio.sleep(0)

    async def close_browser() -> None:
        assert job_finalized.is_set()
        browser_started.set()
        await allow_browser_close.wait()

    resources = RuntimeResources(
        background_tasks=set(),
        close_browser=close_browser,
        active_tasks=lambda: [active],
    )
    job = SimpleNamespace(task=active)
    resources.register_job(
        job,
        register=registered.append,
        unregister=unregistered.append,
    )
    closer = asyncio.create_task(resources.close())
    await browser_started.wait()

    late_job = SimpleNamespace()
    with pytest.raises(RuntimeError, match="closing"):
        resources.register_job(
            late_job,
            register=registered.append,
            unregister=unregistered.append,
        )

    async def late_task() -> None:
        return None

    with pytest.raises(RuntimeError, match="closing"):
        resources.create(late_task(), name="late-browser-task")

    closer.cancel("first cancellation")
    await asyncio.sleep(0)
    closer.cancel("second cancellation")
    await asyncio.sleep(0)
    allow_browser_close.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await closer
    assert cancelled.value.args == ("first cancellation",)
    assert registered == [job]
    assert unregistered == [job]
    await resources.close()


@pytest.mark.asyncio
async def test_turn_outbox_replays_partial_state_after_restart(tmp_path) -> None:
    path = str(tmp_path / "turns.json")
    generated = tmp_path / "generated.png"
    generated.write_bytes(_PNG)
    first = TurnOutbox(path)
    first.begin("rid", request_message_id=70, channel_id=7, user_id=42)
    first.prepare("rid", {"response_text": "answer"})
    first.record_delivery("rid", message_ids=[101], streamed=False)
    first.add_archive_job(
        "rid",
        paths=[str(generated)],
        kwargs={"channel_id": 7},
        response_message_id=None,
    )
    generated.unlink()

    restarted = TurnOutbox(path)
    events: list[str] = []

    async def archive(_job):
        assert _job["response_message_id"] == 101
        assert os.path.isfile(_job["paths"][0])
        events.append("archive")

    async def session(_turn):
        events.append("session")

    async def record(_turn):
        events.append("record")

    assert await restarted.reconcile(
        "rid",
        archive=archive,
        session=session,
        record=record,
    )
    assert events == ["archive", "session", "record"]
    assert all(
        value == "done" for value in TurnOutbox(path).snapshot("rid")["steps"].values()
    )


@pytest.mark.parametrize("interruption", ["crash", "cancel", "provider-error"])
@pytest.mark.asyncio
async def test_turn_outbox_reconciles_acknowledged_incomplete_stream_as_partial(
    tmp_path,
    interruption,
) -> None:
    path = str(tmp_path / f"{interruption}.json")
    outbox = TurnOutbox(path)
    outbox.stage_request(
        "rid",
        request_message_id=70,
        channel_id=7,
        user_id=42,
        static_payload={"raw_user_text": "question", "response_text": ""},
        archive_intent={"kind": "generated_response_images"},
    )
    outbox.record_delivery("rid", message_ids=[101], streamed=True)

    restarted = TurnOutbox(path)
    assert [turn["request_id"] for turn in restarted.pending()] == ["rid"]
    archive = AsyncMock()
    session = AsyncMock()
    record = AsyncMock()
    assert await restarted.reconcile(
        "rid",
        archive=archive,
        session=session,
        record=record,
    )
    archive.assert_not_awaited()
    session.assert_not_awaited()
    record.assert_not_awaited()
    snapshot = restarted.snapshot("rid")
    assert snapshot["terminal_state"] == "partial"
    assert snapshot["steps"] == {
        "archive": "done",
        "session": "skipped",
        "record": "skipped",
    }


@pytest.mark.asyncio
async def test_turn_outbox_idempotency_key_survives_cancel_and_serializes_reconcile(
    tmp_path,
) -> None:
    path = str(tmp_path / "idempotent.json")
    outbox = TurnOutbox(path)
    outbox.stage_request(
        "rid",
        request_message_id=70,
        channel_id=7,
        user_id=42,
        static_payload={"raw_user_text": "question"},
        archive_intent={},
    )
    outbox.prepare("rid", {"response_text": "answer"})
    outbox.record_delivery("rid", message_ids=[101], streamed=True)
    durable_effects: set[str] = set()
    effect_count = 0

    async def record_then_cancel(turn) -> None:
        nonlocal effect_count
        key = turn["idempotency_key"]
        if key not in durable_effects:
            durable_effects.add(key)
            effect_count += 1
            raise asyncio.CancelledError

    async def no_op(_item) -> None:
        return None

    with pytest.raises(asyncio.CancelledError):
        await outbox.reconcile(
            "rid",
            archive=no_op,
            session=no_op,
            record=record_then_cancel,
        )

    restarted = TurnOutbox(path)
    await asyncio.gather(
        restarted.reconcile(
            "rid",
            archive=no_op,
            session=no_op,
            record=record_then_cancel,
        ),
        restarted.reconcile(
            "rid",
            archive=no_op,
            session=no_op,
            record=record_then_cancel,
        ),
    )
    assert effect_count == 1
    assert restarted.snapshot("rid")["steps"]["record"] == "done"


def test_mem0_turn_consumer_uses_durable_request_id_uniqueness(tmp_path) -> None:
    assert (
        mem0_batch.append_turn(
            tmp_path,
            7,
            "question",
            "answer",
            request_id="rid",
        )
        == 1
    )
    lease = mem0_batch.lease_batch(tmp_path, 7)
    assert lease is not None
    assert mem0_batch.ack_batch(tmp_path, lease)
    assert (
        mem0_batch.append_turn(
            tmp_path,
            7,
            "question",
            "answer",
            request_id="rid",
        )
        == 0
    )


@pytest.mark.asyncio
async def test_status_close_cancels_delayed_edit_and_fences_late_updates() -> None:
    message = SimpleNamespace(
        reply=AsyncMock(), channel=SimpleNamespace(send=AsyncMock())
    )
    tasks: set[asyncio.Task] = set()

    def track(coro, *, name=None):
        task = asyncio.create_task(coro, name=name)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    status = bot_ui.StatusController(message, track_task=track)
    status.status_msg = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
    status._last_status_update = time.monotonic()
    await status.update("late")
    await status.close()
    await status.update("later")
    await asyncio.sleep(0)
    status.status_msg.edit.assert_not_awaited()
    assert not tasks


@pytest.mark.asyncio
async def test_status_terminal_edit_lands_after_inflight_and_fences_stale_updates() -> None:
    old_edit_started = asyncio.Event()
    release_old_edit = asyncio.Event()
    edits: list[str] = []

    async def edit(*, content, **_kwargs):
        if content == "old progress":
            old_edit_started.set()
            await release_old_edit.wait()
        edits.append(content)

    message = SimpleNamespace(
        reply=AsyncMock(), channel=SimpleNamespace(send=AsyncMock())
    )
    tasks: set[asyncio.Task] = set()

    def track(coro, *, name=None):
        task = asyncio.create_task(coro, name=name)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    status = bot_ui.StatusController(message, track_task=track)
    status.status_msg = SimpleNamespace(edit=AsyncMock(side_effect=edit))

    await status.update("old progress")
    await asyncio.wait_for(old_edit_started.wait(), timeout=1)
    await status.update("queued stale progress")
    assert status._pending_status["task"] is not None

    terminal = asyncio.create_task(status.edit_final("terminal error"))
    await asyncio.sleep(0)
    await status.update("stale delayed progress")
    release_old_edit.set()
    await terminal
    await asyncio.sleep(0)

    assert edits == ["old progress", "terminal error"]
    await status.close()
    assert not tasks


@pytest.mark.parametrize(
    "payload",
    [
        _PNG[:-1],
        _PNG + b"trailing-data",
        _PNG[:29] + b"\x00\x00\x00\x00" + _PNG[33:],
    ],
)
def test_png_cache_rejects_truncated_trailing_or_bad_crc(tmp_path, payload) -> None:
    path = tmp_path / "cache.png"
    path.write_bytes(payload)
    assert is_cached(str(path)) is False
    assert not path.exists()


def test_png_cache_rejects_oversized_dimensions_with_valid_ihdr_crc(tmp_path) -> None:
    data = bytearray(_PNG)
    ihdr = struct.pack(">IIBBBBB", 20_000, 1, 8, 6, 0, 0, 0)
    data[16:29] = ihdr
    data[29:33] = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    path = tmp_path / "huge.png"
    path.write_bytes(data)
    assert is_cached(str(path)) is False
    assert not os.path.exists(path)
