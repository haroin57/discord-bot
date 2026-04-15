"""Render Markdown tables as PNG images using Puppeteer.

Uses the puppeteer installation bundled with mermaid-cli (mmdc).
Renders HTML tables with a Discord-style dark theme and captures
a tight screenshot around the table element.
"""

import asyncio
import hashlib
import logging
import os

import markdown

__all__ = ["render_table_image"]

log = logging.getLogger(__name__)

TABLE_OUTPUT_DIR = "/tmp/table_renders"
PUPPETEER_CONFIG = "/home/ubuntu/puppeteer-config.json"
PUPPETEER_MODULE = (
    "/usr/local/lib/node_modules/@mermaid-js/mermaid-cli"
    "/node_modules/puppeteer/lib/cjs/puppeteer/puppeteer.js"
)

# Semaphore: limit to 1 concurrent Puppeteer process (2 GB RAM constraint)
_render_sema = asyncio.Semaphore(1)

# ---------------------------------------------------------------------------
# Node.js render script (written once to disk)
# ---------------------------------------------------------------------------
_RENDER_SCRIPT = r"""
const fs = require('fs');

// Use setTimeout to avoid circular dependency issue with puppeteer CJS build
setTimeout(async () => {
  try {
    let puppeteer = require(process.argv[2]);
    // Handle both CJS default export and named export patterns
    if (typeof puppeteer.launch !== 'function' && puppeteer.default) {
      puppeteer = puppeteer.default;
    }
    const configPath = process.argv[3];
    const htmlPath = process.argv[4];
    const outPath = process.argv[5];

    let config = {};
    try { config = JSON.parse(fs.readFileSync(configPath, 'utf8')); } catch {}

    const browser = await puppeteer.launch({
      headless: 'new',
      args: [...(config.args || []), '--no-sandbox', '--disable-setuid-sandbox',
             '--disable-gpu', '--disable-dev-shm-usage'],
    });

    try {
      const page = await browser.newPage();
      await page.setViewport({ width: 1400, height: 900 });

      const html = fs.readFileSync(htmlPath, 'utf8');
      await page.setContent(html, { waitUntil: 'networkidle0', timeout: 15000 });

      const el = await page.$('.table-container');
      if (!el) { process.exit(1); }
      const box = await el.boundingBox();
      const pad = 20;

      await page.screenshot({
        path: outPath,
        clip: {
          x: Math.max(0, box.x - pad),
          y: Math.max(0, box.y - pad),
          width: box.width + pad * 2,
          height: box.height + pad * 2,
        },
        type: 'png',
      });
    } finally {
      await browser.close();
    }
  } catch (err) {
    console.error(err);
    process.exit(1);
  }
}, 0);
"""

# ---------------------------------------------------------------------------
# CSS (Discord dark theme)
# ---------------------------------------------------------------------------
TABLE_CSS = """
body {
  background: #2b2d31;
  margin: 0;
  padding: 20px;
  font-family: 'Noto Sans JP', 'Segoe UI', Arial, sans-serif;
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

HTML_TEMPLATE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;600;700&display=swap" rel="stylesheet">
<style>{css}</style>
</head><body>
<div class="table-container">
{caption_html}
{table_html}
</div>
</body></html>"""


def _md_table_to_html(md_table: str) -> str:
    """Convert a Markdown table string to an HTML <table>."""
    return markdown.markdown(md_table, extensions=["tables"])


async def render_table_image(
    md_table: str,
    caption: str = "",
    *,
    output_dir: str = TABLE_OUTPUT_DIR,
) -> str | None:
    """Render a Markdown table as a PNG image. Returns the file path, or None on failure."""
    os.makedirs(output_dir, exist_ok=True)

    table_hash = hashlib.md5(md_table.encode()).hexdigest()[:12]
    out_path = os.path.join(output_dir, f"table_{table_hash}.png")

    # Cache hit
    if os.path.isfile(out_path):
        return out_path

    # Build HTML
    table_html = _md_table_to_html(md_table)
    caption_html = f'<div class="caption">{caption}</div>' if caption else ""
    full_html = HTML_TEMPLATE.format(
        css=TABLE_CSS,
        caption_html=caption_html,
        table_html=table_html,
    )

    html_path = os.path.join(output_dir, f"table_{table_hash}.html")
    with open(html_path, "w") as f:
        f.write(full_html)

    # Ensure render script exists
    script_path = os.path.join(output_dir, "_render.js")
    if not os.path.isfile(script_path):
        with open(script_path, "w") as f:
            f.write(_RENDER_SCRIPT)

    async with _render_sema:
        try:
            proc = await asyncio.create_subprocess_exec(
                "node",
                script_path,
                PUPPETEER_MODULE,
                PUPPETEER_CONFIG,
                html_path,
                out_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            if proc.returncode != 0:
                log.error("Table render failed (rc=%d): %s", proc.returncode, stderr.decode()[:500])
                return None

            return out_path if os.path.isfile(out_path) else None

        except asyncio.TimeoutError:
            log.error("Table render timed out")
            try:
                proc.kill()
            except Exception:
                pass
            return None
        except Exception as e:
            log.error("Table render error: %s", e)
            return None
