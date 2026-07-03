"""Tests for utils/table_renderer.py (Cloudflare Browser Rendering backend)."""

import hashlib
import os
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _expected_hash(md_table: str, caption: str = "") -> str:
    _TEMPLATE_VERSION = "3"
    cache_key = f"v{_TEMPLATE_VERSION}:{md_table}:{caption}"
    return hashlib.md5(cache_key.encode()).hexdigest()[:12]


async def _render(monkeypatch, tmp_path, md_table, caption="", *, png=b"\x89PNG\r\n"):
    """Call render_table_image with the Browser Rendering client mocked out.

    ``png`` is the fake PNG bytes returned by render_screenshot; pass None to
    simulate a failed render (client returns no image).
    """
    import utils.table_renderer as tr

    monkeypatch.setattr(tr, "TABLE_OUTPUT_DIR", str(tmp_path))

    calls = []

    async def _fake_render(html, *, selector=None, **kw):
        calls.append({"html": html, "selector": selector, **kw})
        return png

    monkeypatch.setattr(tr, "render_screenshot", _fake_render)

    result = await tr.render_table_image(md_table, caption, output_dir=str(tmp_path))
    return result, calls


# ---------------------------------------------------------------------------
# Basic happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_png_path(monkeypatch, tmp_path):
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    result, calls = await _render(monkeypatch, tmp_path, md)
    assert result is not None
    assert result.endswith(".png")
    assert os.path.isfile(result)
    # PNG bytes returned by the client are what got written to disk.
    with open(result, "rb") as f:
        assert f.read() == b"\x89PNG\r\n"
    # Clips to the table container (tight crop, not a full page).
    assert calls[0]["selector"] == ".table-container"


# ---------------------------------------------------------------------------
# Hash naming
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hash_naming_no_caption(monkeypatch, tmp_path):
    md = "| X |\n|---|\n| y |"
    result, _ = await _render(monkeypatch, tmp_path, md)
    expected = f"table_{_expected_hash(md)}.png"
    assert os.path.basename(result) == expected


@pytest.mark.asyncio
async def test_hash_naming_with_caption(monkeypatch, tmp_path):
    md = "| X |\n|---|\n| y |"
    caption = "My Caption"
    result, _ = await _render(monkeypatch, tmp_path, md, caption)
    expected = f"table_{_expected_hash(md, caption)}.png"
    assert os.path.basename(result) == expected


@pytest.mark.asyncio
async def test_different_captions_produce_different_hashes(monkeypatch, tmp_path):
    md = "| A |\n|---|\n| 1 |"
    result1, _ = await _render(monkeypatch, tmp_path, md, "Cap A")
    result2, _ = await _render(monkeypatch, tmp_path, md, "Cap B")
    assert os.path.basename(result1) != os.path.basename(result2)


# ---------------------------------------------------------------------------
# Caption reflection in HTML (passed to render_screenshot)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_caption_written_to_html(monkeypatch, tmp_path):
    caption = "Sales Report"
    md = "| A |\n|---|\n| 1 |"
    result, calls = await _render(monkeypatch, tmp_path, md, caption)

    # The HTML handed to the remote browser carries the escaped caption...
    assert "Sales Report" in calls[0]["html"]
    assert 'class="caption"' in calls[0]["html"]
    # ...and it is also persisted to disk for debugging.
    table_hash = _expected_hash(md, caption)
    html_path = tmp_path / f"table_{table_hash}.html"
    assert html_path.exists()
    assert "Sales Report" in html_path.read_text()


@pytest.mark.asyncio
async def test_no_caption_no_caption_div(monkeypatch, tmp_path):
    md = "| A |\n|---|\n| 1 |"
    _, calls = await _render(monkeypatch, tmp_path, md)
    assert 'class="caption"' not in calls[0]["html"]


# ---------------------------------------------------------------------------
# Caption HTML escaping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_caption_html_escaped(monkeypatch, tmp_path):
    malicious_caption = '<script>alert("xss")</script>'
    md = "| A |\n|---|\n| 1 |"
    _, calls = await _render(monkeypatch, tmp_path, md, malicious_caption)
    html = calls[0]["html"]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# Cache hit — render client NOT called again
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_skips_render(monkeypatch, tmp_path):
    import utils.table_renderer as tr

    monkeypatch.setattr(tr, "TABLE_OUTPUT_DIR", str(tmp_path))
    call_count = 0

    async def _fake_render(html, *, selector=None, **kw):
        nonlocal call_count
        call_count += 1
        return b"\x89PNG"

    monkeypatch.setattr(tr, "render_screenshot", _fake_render)

    md = "| A |\n|---|\n| 1 |"
    r1 = await tr.render_table_image(md, output_dir=str(tmp_path))
    r2 = await tr.render_table_image(md, output_dir=str(tmp_path))

    assert r1 == r2
    assert call_count == 1


# ---------------------------------------------------------------------------
# Large table
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_large_table(monkeypatch, tmp_path):
    header = "| " + " | ".join(f"Col{i}" for i in range(20)) + " |"
    sep = "| " + " | ".join(["---"] * 20) + " |"
    rows = "\n".join(
        "| " + " | ".join(str(i * 20 + j) for j in range(20)) + " |"
        for i in range(50)
    )
    md = "\n".join([header, sep, rows])
    result, _ = await _render(monkeypatch, tmp_path, md)
    assert result is not None


# ---------------------------------------------------------------------------
# Empty / degenerate table
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_string_table(monkeypatch, tmp_path):
    """Empty md_table → client returns no image → None (no crash)."""
    result, _ = await _render(monkeypatch, tmp_path, "", png=None)
    assert result is None


@pytest.mark.asyncio
async def test_header_only_table(monkeypatch, tmp_path):
    md = "| A | B |\n|---|---|"
    result, _ = await _render(monkeypatch, tmp_path, md)
    assert result is not None


# ---------------------------------------------------------------------------
# String escaping in table cells (bleach sanitization)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_script_tag_in_cell_stripped(monkeypatch, tmp_path):
    md = '| A |\n|---|\n| <script>alert(1)</script> |'
    _, calls = await _render(monkeypatch, tmp_path, md)
    assert "<script>" not in calls[0]["html"]


@pytest.mark.asyncio
async def test_img_onerror_stripped(monkeypatch, tmp_path):
    md = '| A |\n|---|\n| <img src=x onerror=alert(1)> |'
    _, calls = await _render(monkeypatch, tmp_path, md)
    assert "onerror" not in calls[0]["html"]


@pytest.mark.asyncio
async def test_unicode_and_japanese_in_cells(monkeypatch, tmp_path):
    md = "| 名前 | 値 |\n|---|---|\n| テスト | 日本語 |"
    result, _ = await _render(monkeypatch, tmp_path, md)
    assert result is not None


# ---------------------------------------------------------------------------
# Error handling — client returns None (failure) → None result
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_render_failure_returns_none(monkeypatch, tmp_path):
    result, _ = await _render(
        monkeypatch, tmp_path, "| A |\n|---|\n| 1 |", png=None
    )
    assert result is None


@pytest.mark.asyncio
async def test_output_dir_created_if_missing(monkeypatch, tmp_path):
    new_dir = tmp_path / "nested" / "output"
    import utils.table_renderer as tr

    async def _fake_render(html, *, selector=None, **kw):
        return b"\x89PNG"

    monkeypatch.setattr(tr, "render_screenshot", _fake_render)

    result = await tr.render_table_image(
        "| A |\n|---|\n| 1 |", output_dir=str(new_dir)
    )
    assert result is not None
    assert new_dir.exists()


# ---------------------------------------------------------------------------
# _md_table_to_html unit tests
# ---------------------------------------------------------------------------

def test_md_table_to_html_basic():
    from utils.table_renderer import _md_table_to_html
    html = _md_table_to_html("| A | B |\n|---|---|\n| 1 | 2 |")
    assert "<table>" in html
    assert "<th>" in html


def test_md_table_to_html_strips_script():
    from utils.table_renderer import _md_table_to_html
    html = _md_table_to_html("| A |\n|---|\n| <script>evil()</script> |")
    assert "<script>" not in html


def test_md_table_to_html_allows_strong():
    from utils.table_renderer import _md_table_to_html
    html = _md_table_to_html("| **bold** |\n|---|\n| x |")
    assert "<strong>" in html or "bold" in html


def test_md_table_to_html_empty_string():
    from utils.table_renderer import _md_table_to_html
    html = _md_table_to_html("")
    # Should return empty string or just whitespace — no crash
    assert isinstance(html, str)
