"""Render Markdown tables as PNG images using Puppeteer.

Uses the puppeteer installation bundled with mermaid-cli (mmdc).
Renders HTML tables with a Discord-style dark theme and captures
a tight screenshot around the table element.
"""

import hashlib
import logging
import os

import markdown

from utils.browser_pool import render_with_browser_daemon

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
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "strong", "em", "b", "i", "code", "br", "span",
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
    os.makedirs(output_dir, exist_ok=True)

    # Cache key includes a template/sanitizer version so previously-rendered
    # PNGs that may have contained injected HTML are not served after the
    # bleach allowlist + caption escape was added. Bump _TEMPLATE_VERSION on
    # every sanitizer / template change.
    _TEMPLATE_VERSION = "2"  # bumped when bleach + html.escape were added
    cache_key = f"v{_TEMPLATE_VERSION}:{md_table}:{caption}"
    table_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
    out_path = os.path.join(output_dir, f"table_{table_hash}.png")

    # Cache hit
    if os.path.isfile(out_path):
        return out_path

    # Build HTML
    import html as _html
    table_html = _md_table_to_html(md_table)
    # Caption comes from an LLM-rendered markdown heading and can include
    # arbitrary user-controlled text; HTML-escape so it cannot inject <script>
    # or event-handler attributes into the rendered page.
    caption_html = f'<div class="caption">{_html.escape(caption)}</div>' if caption else ""
    full_html = HTML_TEMPLATE.format(
        css=TABLE_CSS,
        caption_html=caption_html,
        table_html=table_html,
    )

    html_path = os.path.join(output_dir, f"table_{table_hash}.html")
    with open(html_path, "w") as f:
        f.write(full_html)

    try:
        await render_with_browser_daemon(
            html_path=html_path,
            output_path=out_path,
            selector=".table-container",
            pad=20,
            viewport={"width": 1400, "height": 900},
        )
        return out_path if os.path.isfile(out_path) else None
    except TimeoutError:
        log.error("Table render timed out")
        return None
    except Exception as e:
        log.error("Table render failed: %s", e)
        return None
