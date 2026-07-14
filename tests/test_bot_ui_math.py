"""LiveRenderer regression tests for adaptive Discord math presentation.

These tests exercise the Discord-facing streaming layer rather than only the
parser helpers.  The recorder models reply/send/edit/delete closely enough to
assert what could actually become visible to a user while a response grows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot_ui


@dataclass
class _DiscordRecorder:
    """Record user-visible Discord operations in call order."""

    events: list[dict] = field(default_factory=list)
    messages: list[MagicMock] = field(default_factory=list)
    _next_id: int = 700_000_000_000_000_000

    def _sent_message(self, *, route: str, content=None, file=None):
        sent = MagicMock()
        sent.id = self._next_id
        self._next_id += 1
        sent.content = content
        sent.file = file
        sent.deleted = False

        async def edit(*, content=None, **kwargs):
            if content is not None:
                sent.content = content
            self.events.append(
                {
                    "op": "edit",
                    "message": sent,
                    "content": content,
                    "file": kwargs.get("file"),
                }
            )
            return sent

        async def delete():
            sent.deleted = True
            self.events.append(
                {
                    "op": "delete",
                    "message": sent,
                    "content": sent.content,
                    "file": sent.file,
                }
            )

        sent.edit = AsyncMock(side_effect=edit)
        sent.delete = AsyncMock(side_effect=delete)
        self.messages.append(sent)
        self.events.append(
            {
                "op": route,
                "message": sent,
                "content": content,
                "file": file,
            }
        )
        return sent

    async def reply(self, *args, **kwargs):
        content = args[0] if args else kwargs.get("content")
        return self._sent_message(
            route="reply", content=content, file=kwargs.get("file")
        )

    async def send(self, *args, **kwargs):
        content = args[0] if args else kwargs.get("content")
        return self._sent_message(
            route="send", content=content, file=kwargs.get("file")
        )

    @property
    def text_writes(self) -> list[str]:
        return [
            event["content"]
            for event in self.events
            if event["op"] in {"reply", "send", "edit"}
            and isinstance(event["content"], str)
        ]


def _plain_split(text: str, limit: int, *, numbered: bool = False) -> list[str]:
    del numbered
    return [text[index:index + limit] for index in range(0, len(text), limit)]


def _make_renderer(*, mode: str = "adaptive", split_message=_plain_split):
    recorder = _DiscordRecorder()
    message = MagicMock()
    message.reply = AsyncMock(side_effect=recorder.reply)
    message.channel.send = AsyncMock(side_effect=recorder.send)
    status = MagicMock()
    status.reposition = AsyncMock()
    renderer = bot_ui.LiveRenderer(
        message,
        status,
        track_task=lambda coro, name=None: coro,
        job=MagicMock(),
        split_message=split_message,
        math_presentation_mode=mode,
    )
    return renderer, message, status, recorder


def _png(tmp_path: Path, name: str = "math.png") -> str:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"math-image")
    return str(path)


@pytest.mark.asyncio
async def test_adaptive_simple_tex_coalesces_into_one_text_message(
    monkeypatch,
):
    renderer, message, _status, recorder = _make_renderer(mode="adaptive")
    render = AsyncMock()
    monkeypatch.setattr(bot_ui, "render_math_image", render)

    await renderer.render_incremental(
        r"The only solution is \(x=0\), as required.", final=True
    )

    assert recorder.text_writes == ["The only solution is x = 0, as required."]
    message.reply.assert_awaited_once_with(
        "The only solution is x = 0, as required."
    )
    message.channel.send.assert_not_awaited()
    render.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_simple_tex_still_renders_an_image(tmp_path, monkeypatch):
    renderer, message, _status, recorder = _make_renderer(mode="legacy")
    image_path = _png(tmp_path)
    render = AsyncMock(return_value=image_path)
    monkeypatch.setattr(bot_ui, "render_math_image", render)

    await renderer.render_incremental(r"\(x=0\)", final=True)

    render.assert_awaited_once_with("x=0", display_mode=False)
    message.reply.assert_awaited_once()
    assert message.reply.await_args.kwargs["file"] is not None
    assert recorder.text_writes == []
    assert renderer.rendered_images == [image_path]


@pytest.mark.asyncio
async def test_adaptive_single_tokens_coalesce_with_surrounding_prose(monkeypatch):
    renderer, message, _status, recorder = _make_renderer(mode="adaptive")
    render = AsyncMock()
    monkeypatch.setattr(bot_ui, "render_math_image", render)

    await renderer.render_incremental(
        r"The symbols \(A\), \(0\), and \(r\) stay inline.", final=True
    )

    assert recorder.text_writes == ["The symbols A, 0, and r stay inline."]
    message.reply.assert_awaited_once()
    message.channel.send.assert_not_awaited()
    render.assert_not_awaited()


@pytest.mark.asyncio
async def test_simple_prose_then_matrix_keeps_text_image_text_order_and_raw_offset(
    tmp_path, monkeypatch
):
    renderer, _message, _status, recorder = _make_renderer(mode="adaptive")
    image_path = _png(tmp_path)
    monkeypatch.setattr(
        bot_ui, "render_math_image", AsyncMock(return_value=image_path)
    )
    response = (
        r"before \(A\) then "
        r"\[\begin{pmatrix}1&0\\0&1\end{pmatrix}\]"
        r" after \(r\)."
    )
    raw_matrix_end = response.index(r"\]") + len(r"\]")

    await renderer.render_incremental(response, final=True)

    writes = [
        (event["content"], event["file"])
        for event in recorder.events
        if event["op"] in {"reply", "send"}
    ]
    assert len(writes) == 3
    assert writes[0] == ("before A then ", None)
    assert writes[1][0] is None
    assert writes[1][1] is not None
    assert writes[2] == (" after r.", None)
    # Presentation shortens earlier TeX, but the incremental cursor remains an
    # offset into the canonical raw response.
    assert renderer._emitted["upto"] == raw_matrix_end


@pytest.mark.parametrize("chunk_size", [1, 3, 7])
@pytest.mark.asyncio
async def test_streaming_never_exposes_raw_math_delimiters(
    chunk_size, tmp_path, monkeypatch
):
    renderer, _message, _status, recorder = _make_renderer(mode="adaptive")
    image_path = _png(tmp_path, f"math-{chunk_size}.png")
    monkeypatch.setattr(
        bot_ui, "render_math_image", AsyncMock(return_value=image_path)
    )
    response = (
        r"start \(x=0\), then "
        r"\[\begin{pmatrix}1&0\\0&1\end{pmatrix}\] end"
    )

    for end in range(chunk_size, len(response) + chunk_size, chunk_size):
        await renderer.render_incremental(
            response[: min(end, len(response))], force=True
        )

    for visible_text in recorder.text_writes:
        assert r"\(" not in visible_text
        assert r"\)" not in visible_text
        assert r"\[" not in visible_text
        assert r"\]" not in visible_text
        assert "$$" not in visible_text


@pytest.mark.asyncio
async def test_adaptive_transform_happens_before_message_splitting(monkeypatch):
    split_inputs: list[str] = []

    def recording_split(text: str, limit: int, *, numbered: bool = False):
        assert limit == bot_ui.LiveRenderer.LIVE_DRAFT_LIMIT
        assert numbered is False
        split_inputs.append(text)
        boundary = text.index("x")
        return [text[:boundary], text[boundary:]]

    renderer, _message, _status, recorder = _make_renderer(
        mode="adaptive", split_message=recording_split
    )
    render = AsyncMock()
    monkeypatch.setattr(bot_ui, "render_math_image", render)

    await renderer.render_incremental(
        r"1234567890\(x\ne0\)tail", final=True
    )

    assert split_inputs == ["1234567890x ≠ 0tail"]
    assert recorder.text_writes == ["1234567890", "x ≠ 0tail"]
    render.assert_not_awaited()


@pytest.mark.asyncio
async def test_complex_math_placeholder_is_deleted_before_image_send(
    tmp_path, monkeypatch
):
    renderer, _message, _status, recorder = _make_renderer(mode="adaptive")
    image_path = _png(tmp_path)
    monkeypatch.setattr(
        bot_ui, "render_math_image", AsyncMock(return_value=image_path)
    )
    open_response = r"before \[\begin{pmatrix}1&0"
    full_response = open_response + r"\\0&1\end{pmatrix}\] after"

    await renderer.render_incremental(open_response, force=True)
    placeholder = renderer._placeholder["msg"]
    assert placeholder is not None
    assert placeholder.content == "📊 描画中…"
    assert placeholder.deleted is False

    await renderer.render_incremental(full_response, force=True)

    assert placeholder.deleted is True
    placeholder.delete.assert_awaited_once()
    assert renderer._placeholder["msg"] is None
    delete_index = next(
        index
        for index, event in enumerate(recorder.events)
        if event["op"] == "delete" and event["message"] is placeholder
    )
    image_index = next(
        index
        for index, event in enumerate(recorder.events)
        if event["op"] in {"reply", "send"} and event["file"] is not None
    )
    assert delete_index < image_index


@pytest.mark.asyncio
async def test_adaptive_preserves_tex_inside_code_fence(monkeypatch):
    renderer, message, _status, recorder = _make_renderer(mode="adaptive")
    render = AsyncMock()
    monkeypatch.setattr(bot_ui, "render_math_image", render)
    response = "```tex\n\\(x=0\\)\n```\noutside \\(y=1\\)"

    await renderer.render_incremental(response, final=True)

    expected = "```tex\n\\(x=0\\)\n```\noutside y = 1"
    assert recorder.text_writes == [expected]
    message.reply.assert_awaited_once_with(expected)
    render.assert_not_awaited()


@pytest.mark.asyncio
async def test_math_image_file_has_unicode_alt_description(tmp_path, monkeypatch):
    renderer, message, _status, _recorder = _make_renderer(mode="legacy")
    image_path = _png(tmp_path)
    monkeypatch.setattr(
        bot_ui, "render_math_image", AsyncMock(return_value=image_path)
    )

    await renderer._emit_block(bot_ui.MathSegment(r"x\ne0", display_mode=False))

    attached = message.reply.await_args.kwargs["file"]
    assert attached.description == "x ≠ 0"


@pytest.mark.asyncio
async def test_math_render_failure_falls_back_to_raw_tex(monkeypatch):
    renderer, message, _status, recorder = _make_renderer(mode="adaptive")
    render = AsyncMock(side_effect=RuntimeError("renderer unavailable"))
    monkeypatch.setattr(bot_ui, "render_math_image", render)

    await renderer._emit_block(
        bot_ui.MathSegment(r"\frac{1}{2}", display_mode=True)
    )

    render.assert_awaited_once_with(r"\frac{1}{2}", display_mode=True)
    fallback = "```\n\\frac{1}{2}\n```"
    assert recorder.text_writes == [fallback]
    message.reply.assert_awaited_once_with(fallback)
    assert renderer.rendered_images == []
