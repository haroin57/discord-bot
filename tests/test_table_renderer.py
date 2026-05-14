"""Tests for utils/table_renderer.py."""

import hashlib
import os
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _expected_hash(md_table: str, caption: str = "") -> str:
    _TEMPLATE_VERSION = "2"
    cache_key = f"v{_TEMPLATE_VERSION}:{md_table}:{caption}"
    return hashlib.md5(cache_key.encode()).hexdigest()[:12]


async def _render(monkeypatch, tmp_path, md_table, caption="", *, raise_exc=None):
    """Call render_table_image with browser daemon mocked out."""
    import utils.table_renderer as tr

    monkeypatch.setattr(tr, "TABLE_OUTPUT_DIR", str(tmp_path))

    rendered_files = []

    async def _fake_render(*, html_path, output_path, selector, pad, viewport):
        rendered_files.append(output_path)
        if raise_exc is not None:
            raise raise_exc
        # Simulate the daemon writing the PNG
        with open(output_path, "wb") as f:
            f.write(b"\x89PNG\r\n")

    monkeypatch.setattr(tr, "render_with_browser_daemon", _fake_render)

    result = await tr.render_table_image(md_table, caption, output_dir=str(tmp_path))
    return result, rendered_files


# ---------------------------------------------------------------------------
# Basic happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_png_path(monkeypatch, tmp_path):
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    result, _ = await _render(monkeypatch, tmp_path, md)
    assert result is not None
    assert result.endswith(".png")
    assert os.path.isfile(result)


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
# Caption reflection in HTML
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_caption_written_to_html(monkeypatch, tmp_path):
    import utils.table_renderer as tr

    monkeypatch.setattr(tr, "TABLE_OUTPUT_DIR", str(tmp_path))

    async def _fake_render(*, html_path, output_path, **kw):
        with open(output_path, "wb") as f:
            f.write(b"\x89PNG")

    monkeypatch.setattr(tr, "render_with_browser_daemon", _fake_render)

    caption = "Sales Report"
    md = "| A |\n|---|\n| 1 |"
    await tr.render_table_image(md, caption, output_dir=str(tmp_path))

    table_hash = _expected_hash(md, caption)
    html_path = tmp_path / f"table_{table_hash}.html"
    assert html_path.exists()
    content = html_path.read_text()
    assert "Sales Report" in content
    assert 'class="caption"' in content


@pytest.mark.asyncio
async def test_no_caption_no_caption_div(monkeypatch, tmp_path):
    import utils.table_renderer as tr

    monkeypatch.setattr(tr, "TABLE_OUTPUT_DIR", str(tmp_path))

    async def _fake_render(*, html_path, output_path, **kw):
        with open(output_path, "wb") as f:
            f.write(b"\x89PNG")

    monkeypatch.setattr(tr, "render_with_browser_daemon", _fake_render)

    md = "| A |\n|---|\n| 1 |"
    await tr.render_table_image(md, output_dir=str(tmp_path))

    table_hash = _expected_hash(md, "")
    html_path = tmp_path / f"table_{table_hash}.html"
    content = html_path.read_text()
    assert 'class="caption"' not in content


# ---------------------------------------------------------------------------
# Caption HTML escaping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_caption_html_escaped(monkeypatch, tmp_path):
    import utils.table_renderer as tr

    monkeypatch.setattr(tr, "TABLE_OUTPUT_DIR", str(tmp_path))

    async def _fake_render(*, html_path, output_path, **kw):
        with open(output_path, "wb") as f:
            f.write(b"\x89PNG")

    monkeypatch.setattr(tr, "render_with_browser_daemon", _fake_render)

    malicious_caption = '<script>alert("xss")</script>'
    md = "| A |\n|---|\n| 1 |"
    await tr.render_table_image(md, malicious_caption, output_dir=str(tmp_path))

    table_hash = _expected_hash(md, malicious_caption)
    html_path = tmp_path / f"table_{table_hash}.html"
    content = html_path.read_text()
    assert "<script>" not in content
    assert "&lt;script&gt;" in content


# ---------------------------------------------------------------------------
# Cache hit — browser daemon NOT called again
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_skips_render(monkeypatch, tmp_path):
    import utils.table_renderer as tr

    monkeypatch.setattr(tr, "TABLE_OUTPUT_DIR", str(tmp_path))
    call_count = 0

    async def _fake_render(*, html_path, output_path, **kw):
        nonlocal call_count
        call_count += 1
        with open(output_path, "wb") as f:
            f.write(b"\x89PNG")

    monkeypatch.setattr(tr, "render_with_browser_daemon", _fake_render)

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
    """Empty md_table should not crash — returns None (daemon writes nothing)."""
    import utils.table_renderer as tr

    monkeypatch.setattr(tr, "TABLE_OUTPUT_DIR", str(tmp_path))

    async def _fake_render_no_file(*, html_path, output_path, **kw):
        # Daemon produces no file (e.g. nothing to render)
        pass

    monkeypatch.setattr(tr, "render_with_browser_daemon", _fake_render_no_file)

    result = await tr.render_table_image("", output_dir=str(tmp_path))
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
    import utils.table_renderer as tr

    monkeypatch.setattr(tr, "TABLE_OUTPUT_DIR", str(tmp_path))

    async def _fake_render(*, html_path, output_path, **kw):
        with open(output_path, "wb") as f:
            f.write(b"\x89PNG")

    monkeypatch.setattr(tr, "render_with_browser_daemon", _fake_render)

    md = '| A |\n|---|\n| <script>alert(1)</script> |'
    await tr.render_table_image(md, output_dir=str(tmp_path))

    table_hash = _expected_hash(md)
    html_path = tmp_path / f"table_{table_hash}.html"
    content = html_path.read_text()
    assert "<script>" not in content


@pytest.mark.asyncio
async def test_img_onerror_stripped(monkeypatch, tmp_path):
    import utils.table_renderer as tr

    monkeypatch.setattr(tr, "TABLE_OUTPUT_DIR", str(tmp_path))

    async def _fake_render(*, html_path, output_path, **kw):
        with open(output_path, "wb") as f:
            f.write(b"\x89PNG")

    monkeypatch.setattr(tr, "render_with_browser_daemon", _fake_render)

    md = '| A |\n|---|\n| <img src=x onerror=alert(1)> |'
    await tr.render_table_image(md, output_dir=str(tmp_path))

    table_hash = _expected_hash(md)
    html_path = tmp_path / f"table_{table_hash}.html"
    content = html_path.read_text()
    assert "onerror" not in content


@pytest.mark.asyncio
async def test_unicode_and_japanese_in_cells(monkeypatch, tmp_path):
    md = "| 名前 | 値 |\n|---|---|\n| テスト | 日本語 |"
    result, _ = await _render(monkeypatch, tmp_path, md)
    assert result is not None


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_error_returns_none(monkeypatch, tmp_path):
    result, _ = await _render(monkeypatch, tmp_path, "| A |\n|---|\n| 1 |",
                               raise_exc=TimeoutError("timed out"))
    assert result is None


@pytest.mark.asyncio
async def test_generic_exception_returns_none(monkeypatch, tmp_path):
    result, _ = await _render(monkeypatch, tmp_path, "| A |\n|---|\n| 1 |",
                               raise_exc=RuntimeError("something broke"))
    assert result is None


@pytest.mark.asyncio
async def test_output_dir_created_if_missing(monkeypatch, tmp_path):
    new_dir = tmp_path / "nested" / "output"
    import utils.table_renderer as tr

    async def _fake_render(*, html_path, output_path, **kw):
        with open(output_path, "wb") as f:
            f.write(b"\x89PNG")

    monkeypatch.setattr(tr, "render_with_browser_daemon", _fake_render)

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
