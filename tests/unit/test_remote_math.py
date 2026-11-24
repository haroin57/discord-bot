"""Opt-in remote math must never allocate a local browser on remote failure."""

import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from utils import math_renderer as math
from utils import cf_browser


def png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (4, 4), (40, 40, 40)).save(output, format="PNG")
    return output.getvalue()


async def test_remote_math_needs_no_local_katex_and_preserves_escaping(tmp_path, monkeypatch):
    monkeypatch.setenv("MATH_RENDER_BACKEND", "cloudflare")
    monkeypatch.setattr(math, "_KATEX_AVAILABLE", False)
    local = AsyncMock(side_effect=AssertionError("local browser spawned"))
    remote = AsyncMock(return_value=png_bytes())
    monkeypatch.setattr(math, "render_with_browser_daemon", local)
    monkeypatch.setattr(cf_browser, "render_screenshot", remote)
    path = await math.render_math_image(r"\frac{1}{2}</script><script>bad</script>", output_dir=str(tmp_path))
    assert path is not None
    assert "</script><script>bad" not in remote.call_args.args[0]
    assert "file://" not in remote.call_args.args[0]
    assert remote.call_args.kwargs["wait_for_network_idle"] is True
    local.assert_not_called()


async def test_remote_failure_degrades_to_text_without_chromium(tmp_path, monkeypatch):
    monkeypatch.setenv("MATH_RENDER_BACKEND", "cloudflare")
    monkeypatch.setattr(cf_browser, "render_screenshot", AsyncMock(return_value=None))
    local = AsyncMock(side_effect=AssertionError("local browser spawned"))
    monkeypatch.setattr(math, "render_with_browser_daemon", local)
    assert await math.render_math_image("x", output_dir=str(tmp_path)) is None
    local.assert_not_called()
    assert not list(tmp_path.glob("*.png"))


async def test_backend_switch_does_not_reuse_incompatible_cache(tmp_path, monkeypatch):
    remote = AsyncMock(return_value=png_bytes())
    monkeypatch.setattr(cf_browser, "render_screenshot", remote)
    monkeypatch.setenv("MATH_RENDER_BACKEND", "cloudflare")
    first = await math.render_math_image("x", output_dir=str(tmp_path))
    assert first
    assert await math.render_math_image("x", output_dir=str(tmp_path)) == first
    assert remote.await_count == 1
    monkeypatch.setenv("MATH_RENDER_BACKEND", "local")
    monkeypatch.setattr(math, "_KATEX_AVAILABLE", False)
    assert await math.render_math_image("x", output_dir=str(tmp_path)) is None
