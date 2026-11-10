"""Rendering/download latency regressions with isolated files and no network."""

import asyncio
import io
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


@pytest.fixture
def png():
    stream = io.BytesIO()
    Image.new("RGB", (12, 10), (25, 50, 75)).save(stream, format="PNG")
    return stream.getvalue()


class Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload
        self.content = self
        self.consumed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def iter_chunked(self, size):
        for offset in range(0, len(self.payload), size):
            chunk = self.payload[offset : offset + size]
            self.consumed += len(chunk)
            yield chunk

    async def read(self):
        self.consumed = len(self.payload)
        return self.payload


def install_renderer(kind, monkeypatch, png, backend=None):
    from utils import math_renderer, table_renderer

    calls = []

    async def render(*args, **kwargs):
        calls.append((args, kwargs))
        if backend is not None:
            await backend()
        else:
            await asyncio.sleep(0.01)
        if kind == "math":
            Path(kwargs["output_path"]).write_bytes(png)
        return png

    if kind == "math":
        monkeypatch.setattr(math_renderer, "_KATEX_AVAILABLE", True)
        monkeypatch.setattr(math_renderer, "render_with_browser_daemon", render)
        return math_renderer.render_math_image, calls
    monkeypatch.setattr(table_renderer, "render_screenshot", render)
    return table_renderer.render_table_image, calls


@pytest.mark.parametrize("kind", ["table", "math"])
async def test_identical_concurrent_renders_share_completed_image(
    kind, tmp_path, monkeypatch, png
):
    render, calls = install_renderer(kind, monkeypatch, png)
    paths = await asyncio.gather(
        *(render("x", output_dir=str(tmp_path)) for _ in range(8))
    )
    assert len(calls) == 1
    assert len(set(paths)) == 1
    assert Path(paths[0]).read_bytes() == png
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("kind", ["table", "math"])
async def test_different_render_inputs_do_not_share_a_lock(
    kind, tmp_path, monkeypatch, png
):
    started = 0
    both_started = asyncio.Event()

    async def backend():
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 2)

    render, calls = install_renderer(kind, monkeypatch, png, backend)
    paths = await asyncio.gather(
        render("a", output_dir=str(tmp_path)),
        render("b", output_dir=str(tmp_path)),
    )
    assert len(calls) == 2
    assert paths[0] != paths[1]


@pytest.mark.parametrize("kind", ["table", "math"])
async def test_cancelled_render_allows_waiter_to_retry(
    kind, tmp_path, monkeypatch, png
):
    started = asyncio.Event()
    attempt = 0

    async def backend():
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            started.set()
            await asyncio.Future()

    render, calls = install_renderer(kind, monkeypatch, png, backend)
    owner = asyncio.create_task(render("x", output_dir=str(tmp_path)))
    await started.wait()
    waiter = asyncio.create_task(render("x", output_dir=str(tmp_path)))
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    path = await asyncio.wait_for(waiter, 2)
    assert Path(path).read_bytes() == png
    assert len(calls) == 2
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("kind", ["table", "math"])
async def test_png_is_fully_validated_once_per_publication_and_cache_hit(
    kind, tmp_path, monkeypatch, png
):
    from utils import render_store

    render, calls = install_renderer(kind, monkeypatch, png)
    original = render_store._fully_decodes_png
    decodes = []

    def decode(path):
        decodes.append(path)
        return original(path)

    monkeypatch.setattr(render_store, "_fully_decodes_png", decode)
    first = await render("x", output_dir=str(tmp_path))
    assert len(decodes) == 1
    assert await render("x", output_dir=str(tmp_path)) == first
    assert len(decodes) == 2
    # Cache validation remains active after same-length content corruption.
    damaged = bytearray(png)
    damaged[-18] ^= 1
    Path(first).write_bytes(damaged)
    assert await render("x", output_dir=str(tmp_path)) == first
    assert len(calls) == 2
    assert Path(first).read_bytes() == png


@pytest.mark.parametrize("operation", ["image", "binary", "table", "math"])
async def test_decode_keeps_loop_responsive_and_cancellation_drains_worker(
    operation, tmp_path, monkeypatch, png
):
    from utils import attachment_downloader, render_store

    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    original = (
        attachment_downloader._fully_decoded_image
        if operation in {"image", "binary"}
        else render_store._fully_decodes_png
    )

    def decode(path, **kwargs):
        loop.call_soon_threadsafe(started.set)
        try:
            if not release.wait(3):
                raise AssertionError("decode blocked the event loop")
            assert Path(path).is_file()
            return original(path, **kwargs)
        finally:
            finished.set()

    semaphore = asyncio.Semaphore(1)
    if operation in {"image", "binary"}:
        monkeypatch.setattr(attachment_downloader, "_fully_decoded_image", decode)
        attachment = SimpleNamespace(
            id=123, size=0, filename="x.png", content_type="image/png",
            url="https://cdn.discordapp.com/x.png",
        )
        session = SimpleNamespace(get=lambda *_args, **_kw: Response(png))
        kwargs = dict(
            attachment_dir=str(tmp_path), max_bytes=1024,
            semaphore=semaphore, host_check=lambda _url: True,
        )
        if operation == "binary":
            coroutine = attachment_downloader.download_arbitrary(
                session, attachment, **kwargs,
                denied_extensions=frozenset(), sanitize_basename=lambda name: name,
            )
        else:
            coroutine = attachment_downloader.download_image(session, attachment, **kwargs)
    else:
        monkeypatch.setattr(render_store, "_fully_decodes_png", decode)
        render, _ = install_renderer(operation, monkeypatch, png)
        coroutine = render("x", output_dir=str(tmp_path))
    task = asyncio.create_task(coroutine)
    try:
        await asyncio.wait_for(started.wait(), 4)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # A second shutdown cancellation must not orphan the worker.
        await asyncio.sleep(0)
        assert not task.done()
        assert list(tmp_path.glob("*.part"))
        if operation in {"image", "binary"}:
            assert semaphore.locked()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert finished.is_set()
    assert not semaphore.locked()
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.png"))


async def test_text_stream_aborts_before_receiving_unbounded_body():
    from utils.attachment_downloader import download_text

    response = Response(b"a" * (8 * 1024 * 1024))
    requests = []

    def get(url, **kwargs):
        requests.append(kwargs)
        return response

    result = await download_text(
        SimpleNamespace(get=get),
        SimpleNamespace(size=0, filename="x.txt", url="https://cdn.discordapp.com/x"),
        max_bytes=100 * 1024, semaphore=asyncio.Semaphore(1),
        host_check=lambda _url: True,
    )
    assert result is None
    assert response.consumed <= 100 * 1024 + 64 * 1024
    assert requests == [{"allow_redirects": False}]


@pytest.mark.parametrize("payload, expected", [
    ("日本語\n".encode(), "日本語\n"),
    ("日本語\n".encode("utf-16"), "日本語\n"),
    (b"a\x00b", None),
    (b"\xff\xff", None),
    (b"", ""),
    (b"1234", "1234"),
])
async def test_bounded_text_preserves_encoding_and_exact_size_limit(payload, expected):
    from utils.attachment_downloader import download_text

    result = await download_text(
        SimpleNamespace(get=lambda *_args, **_kw: Response(payload)),
        SimpleNamespace(size=0, filename="x.txt", url="https://cdn.discordapp.com/x"),
        max_bytes=len(payload), semaphore=asyncio.Semaphore(1),
        host_check=lambda _url: True,
    )
    assert result == (None if expected is None else ("x.txt", expected))


def test_missing_image_filename_is_checked_once_per_directory(tmp_path, monkeypatch):
    from utils import attachment_policy

    checked = []

    def isfile(path):
        checked.append(path)
        return False

    monkeypatch.setattr(attachment_policy.os.path, "isfile", isfile)
    text = "missing.png " * 2000
    dirs = [str(tmp_path / str(i)) for i in range(5)]
    assert attachment_policy.normalize_image_filenames(text, dirs) == text
    assert len(checked) == 5


def test_filename_normalization_preserves_existing_paths_and_substrings(tmp_path):
    from utils.attachment_policy import normalize_image_filenames

    (tmp_path / "chart.png").write_bytes(b"image")
    text = "chart.png /other/chart.png /other/longchart.png"
    assert normalize_image_filenames(text, [str(tmp_path)]) == (
        f"{tmp_path}/chart.png /other/chart.png /other/longchart.png"
    )


@pytest.mark.parametrize("operation", ["file", "cleanup", "failing_file"])
async def test_file_worker_survives_all_tasks_cancellation_and_preserves_context(
    operation, monkeypatch
):
    import contextvars

    from utils import attachment_store
    from utils.render_store import run_file_operation

    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    context = contextvars.ContextVar("render-test-context", default="unset")
    token = context.set("request")
    observed = []

    def worker(*_args):
        observed.append(context.get())
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(3)
            if operation == "failing_file":
                raise OSError("disk failure during cancellation")
        finally:
            finished.set()

    existing = asyncio.all_tasks()
    if operation == "cleanup":
        monkeypatch.setattr(attachment_store, "cleanup", worker)
        coroutine = attachment_store.cleanup_async(["owned-file"])
    else:
        coroutine = run_file_operation(worker)
    owner = asyncio.create_task(coroutine)
    cancellation = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        # Match asyncio.run shutdown for every task introduced by this owner.
        for task in asyncio.all_tasks() - existing:
            task.cancel("first cancellation")
        await asyncio.sleep(0.01)
        assert not owner.done()
        assert not finished.is_set()
        owner.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not owner.done()
    finally:
        release.set()
        try:
            await owner
        except asyncio.CancelledError as exc:
            cancellation = exc
        context.reset(token)
    assert cancellation is not None
    assert cancellation.args == ("first cancellation",)
    assert finished.is_set()
    assert observed == ["request"]


async def test_cancelled_render_waiter_does_not_drop_other_waiters_lock(
    tmp_path, monkeypatch, png
):
    from utils import render_store

    started = asyncio.Event()
    release = asyncio.Event()

    async def backend():
        started.set()
        await release.wait()

    render, calls = install_renderer("table", monkeypatch, png, backend)
    owner = asyncio.create_task(render("x", output_dir=str(tmp_path)))
    await started.wait()
    waiter = asyncio.create_task(render("x", output_dir=str(tmp_path)))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    third = asyncio.create_task(render("x", output_dir=str(tmp_path)))
    await asyncio.sleep(0)
    release.set()
    assert (await owner) == (await third)
    assert len(calls) == 1
    assert not render_store._render_locks
