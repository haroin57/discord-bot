"""Render Markdown tables as PNG images via Cloudflare Browser Rendering.

Phase 2-1: the local Puppeteer daemon was replaced by the Cloudflare Browser
Rendering REST screenshot API (utils/cf_browser.py). Renders HTML tables with a
Discord-style dark theme and clips a tight screenshot around the table element
(top-level ``selector``), so the output is the table box, not a full page.
"""

import logging

import markdown

from utils.cf_browser import render_screenshot
from utils.render_store import (
    is_cached,
    prepare,
    publish_png,
    remove_if_exists,
    render_paths,
    temporary_png_path,
    write_html,
    write_png,
)

__all__ = ["render_table_image"]

log = logging.getLogger(__name__)

TABLE_OUTPUT_DIR = "/tmp/table_renders"
# ---------------------------------------------------------------------------
# CSS (Discord dark theme)
# ---------------------------------------------------------------------------
TABLE_CSS = """
body {
  background: #2b2d31;
  margin: 0;
  padding: 20px;
  /* 'Noto Sans JP' is the Google Fonts web name (now offline post B1).
     'Noto Sans CJK JP' / 'Noto Sans CJK' are the system family names for
     /usr/share/fonts/opentype/noto/NotoSansCJK-*.ttc — without them,
     Japanese glyphs fall through to DejaVu/Liberation and render as tofu. */
  font-family: 'Noto Sans JP', 'Noto Sans CJK JP', 'Noto Sans CJK', 'Segoe UI', Arial, sans-serif;
}
.table-container {
  display: inline-block;
  background: #2b2d31;
}
.caption {
  color: #f2f3f5;
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 8px;
}
table {
  border-collapse: collapse;
  color: #dbdee1;
  font-size: 14px;
}
th {
  background: #1e1f22;
  color: #f2f3f5;
  font-weight: 600;
  padding: 8px 16px;
  border: 1px solid #3f4147;
  text-align: left;
  white-space: nowrap;
}
td {
  padding: 6px 16px;
  border: 1px solid #3f4147;
}
tr:nth-child(even) {
  background: #232428;
}
strong, b { color: #f2f3f5; }
"""

# NOTE: No Google Fonts <link rel="stylesheet"> here on purpose. Previously
# every cache-miss render waited for an external CDN response under
# `waitUntil:'networkidle0'`, adding 1-3 s per render and stalling for
# 15 s on CDN outage. We now rely on the system-installed Noto Sans CJK
# (TABLE_CSS uses local fallbacks) — no network request fires.
HTML_TEMPLATE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<style>{css}</style>
</head><body>
<div class="table-container">
{caption_html}
{table_html}
</div>
</body></html>"""


# Tags / attributes permitted in the sanitized table HTML. Anything outside
# this allowlist (e.g. <script>, <img onerror=...>, on* attributes) is stripped
# by bleach before the HTML is handed to headless Chromium.
_TABLE_HTML_ALLOWED_TAGS = (
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "strong",
    "em",
    "b",
    "i",
    "code",
    "br",
    "span",
)
_TABLE_HTML_ALLOWED_ATTRS = {"th": ["align"], "td": ["align"]}


def _md_table_to_html(md_table: str) -> str:
    """Convert a Markdown table string to an HTML <table>, then sanitize.

    `markdown.markdown(..., extensions=["tables"])` pass-throughs raw HTML in
    cells by default, so a malicious cell value can inject <script>, <img
    onerror=...>, etc. into the Puppeteer-rendered page (which runs with
    --no-sandbox). Bleach removes anything outside the table-only allowlist.
    """
    import bleach

    raw_html = markdown.markdown(md_table, extensions=["tables"])
    return bleach.clean(
        raw_html,
        tags=_TABLE_HTML_ALLOWED_TAGS,
        attributes=_TABLE_HTML_ALLOWED_ATTRS,
        strip=True,
    )


async def render_table_image(
    md_table: str,
    caption: str = "",
    *,
    output_dir: str = TABLE_OUTPUT_DIR,
) -> str | None:
    """Render a Markdown table as a PNG image. Returns the file path, or None on failure."""
    prepare(output_dir)

    # Cache key includes a template/sanitizer version so previously-rendered
    # PNGs that may have contained injected HTML are not served after the
    # bleach allowlist + caption escape was added. Bump _TEMPLATE_VERSION on
    # every sanitizer / template change.
    _TEMPLATE_VERSION = "3"  # bumped: Puppeteer daemon -> Cloudflare Browser Rendering
    cache_key = f"v{_TEMPLATE_VERSION}:{md_table}:{caption}"
    out_path, html_path = render_paths(
        output_dir,
        prefix="table",
        cache_key=cache_key,
    )

    # Cache hit
    if is_cached(out_path):
        return out_path

    # Build HTML
    import html as _html

    table_html = _md_table_to_html(md_table)
    # Caption comes from an LLM-rendered markdown heading and can include
    # arbitrary user-controlled text; HTML-escape so it cannot inject <script>
    # or event-handler attributes into the rendered page.
    caption_html = (
        f'<div class="caption">{_html.escape(caption)}</div>' if caption else ""
    )
    full_html = HTML_TEMPLATE.format(
        css=TABLE_CSS,
        caption_html=caption_html,
        table_html=table_html,
    )

    # Keep the .html on disk for debugging/parity; the remote browser renders
    # from the inlined string (it cannot see local files).
    write_html(html_path, full_html)

    # Clip to the table container so the PNG is the table box (not a full page).
    # Tables use only local fonts + inline CSS, so no network wait is needed.
    part_path = temporary_png_path(out_path)
    try:
        png_bytes = await render_screenshot(
            full_html,
            selector=".table-container",
        )
        if not png_bytes:
            log.warning("Table render failed (Browser Rendering returned no image)")
            return None
        write_png(part_path, png_bytes)
        if not publish_png(part_path, out_path):
            log.warning("Table render returned an incomplete PNG")
            return None
    except OSError as e:
        log.error("Failed to write table PNG: %s", e)
        return None
    finally:
        remove_if_exists(part_path)
    return out_path if is_cached(out_path) else None
