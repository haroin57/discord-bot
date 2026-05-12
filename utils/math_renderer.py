"""Render LaTeX math expressions as PNG images using KaTeX + Puppeteer.

Uses the same Puppeteer installation as table_renderer.py.
Renders KaTeX HTML with a Discord-style dark theme and captures
a tight screenshot around the math element.
"""

import asyncio
import hashlib
import logging
import os

from utils.render_semaphore import render_sema as _render_sema  # shared with table_renderer

__all__ = ["render_math_image"]

log = logging.getLogger(__name__)

MATH_OUTPUT_DIR = "/tmp/math_renders"
PUPPETEER_CONFIG = "/home/ubuntu/puppeteer-config.json"
PUPPETEER_MODULE = (
    "/usr/local/lib/node_modules/@mermaid-js/mermaid-cli"
    "/node_modules/puppeteer/lib/cjs/puppeteer/puppeteer.js"
)


def _find_chrome_executable() -> str | None:
    """Find Chrome executable in puppeteer cache."""
    import glob as _glob
    patterns = [
        "/home/ubuntu/.cache/puppeteer/chrome/*/chrome-linux64/chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome",
    ]
    for pattern in patterns:
        matches = _glob.glob(pattern)
        if matches:
            return sorted(matches)[-1]  # Latest version
    return None


CHROME_EXECUTABLE = _find_chrome_executable()

# NOTE: _render_sema is imported from utils.render_semaphore at module-top so
# it is the SAME object instance as the one used by table_renderer. The prior
# `asyncio.Semaphore(1)` literal here created a separate object that did not
# actually block a concurrent table render.

# ---------------------------------------------------------------------------
# Node.js render script
# ---------------------------------------------------------------------------
_RENDER_SCRIPT = r"""
const fs = require('fs');

setTimeout(async () => {
  try {
    let puppeteer = require(process.argv[2]);
    if (typeof puppeteer.launch !== 'function' && puppeteer.default) {
      puppeteer = puppeteer.default;
    }
    const configPath = process.argv[3];
    const htmlPath = process.argv[4];
    const outPath = process.argv[5];
    const chromePath = process.argv[6] || undefined;
    const screenshotPad = parseInt(process.argv[7] || '12', 10);

    let config = {};
    try { config = JSON.parse(fs.readFileSync(configPath, 'utf8')); } catch {}

    const launchOpts = {
      headless: 'new',
      args: [...(config.args || []), '--no-sandbox', '--disable-setuid-sandbox',
             '--disable-gpu', '--disable-dev-shm-usage'],
    };
    if (chromePath) { launchOpts.executablePath = chromePath; }
    const browser = await puppeteer.launch(launchOpts);

    try {
      const page = await browser.newPage();
      await page.setViewport({ width: 800, height: 600, deviceScaleFactor: 2 });

      const html = fs.readFileSync(htmlPath, 'utf8');
      // Phase B1: 'domcontentloaded' instead of 'networkidle0'. With the
      // local KaTeX bundle below replacing the CDN load, every cache-miss
      // render no longer waits 1-3 s (or up to 15 s on CDN outage) for
      // network quiescence.
      await page.setContent(html, { waitUntil: 'domcontentloaded', timeout: 5000 });

      const el = await page.$('.math-container');
      if (!el) { process.exit(1); }
      const box = await el.boundingBox();
      const pad = screenshotPad;

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
# CSS (Discord dark theme for math)
# ---------------------------------------------------------------------------
MATH_CSS_DISPLAY = """
body {{
  background: #2b2d31;
  margin: 0;
  padding: {body_pad}px;
}}
.math-container {{
  display: inline-block;
  background: #2b2d31;
  padding: {container_pad};
  color: #f2f3f5;
  font-size: {font_size}px;
  line-height: 1.5;
}}"""

MATH_CSS_INLINE = """
body {{
  background: #2b2d31;
  margin: 0;
  padding: {body_pad}px;
}}
.math-container {{
  display: inline-block;
  background: #2b2d31;
  padding: {container_pad};
  color: #f2f3f5;
  font-size: {font_size}px;
  line-height: 1.4;
}}"""

KATEX_THEME_CSS = """
/* KaTeX overrides for dark theme */
.katex { color: #f2f3f5; }
.katex .mord { color: #f2f3f5; }
.katex .mrel { color: #7dc4e4; }
.katex .mbin { color: #f5a97f; }
.katex .mop { color: #a6da95; }
.katex .mopen, .katex .mclose { color: #cad3f5; }
.katex .mpunct { color: #cad3f5; }
.katex .mfrac .frac-line { border-color: #f2f3f5; }
.katex .sqrt > .root { color: #f2f3f5; }
.katex .sqrt .sqrt-line { stroke: #f2f3f5; }
"""

def _estimate_complexity(latex: str) -> str:
    """Classify a LaTeX expression as 'simple', 'medium', or 'complex'."""
    import re
    # Strip whitespace for length check
    stripped = latex.strip()
    # Count structural commands that indicate complexity
    complex_cmds = len(re.findall(
        r'\\(frac|sum|prod|int|lim|begin|matrix|sqrt|binom|underset|overset)', stripped
    ))
    subscripts = stripped.count('_') + stripped.count('^')
    raw_len = len(re.sub(r'\\[a-zA-Z]+', '', stripped))  # length without command names

    if complex_cmds >= 2 or (raw_len > 40 and subscripts >= 3):
        return "complex"
    elif complex_cmds >= 1 or raw_len > 20 or subscripts >= 3:
        return "medium"
    else:
        return "simple"


def _get_scale_params(latex: str, display_mode: bool) -> dict:
    """Return font_size, body_pad, container_pad, screenshot_pad based on expression complexity."""
    complexity = _estimate_complexity(latex)
    if display_mode:
        params = {
            "simple":  {"font_size": 10, "body_pad": 1,  "container_pad": "1px 2px",  "screenshot_pad": 1},
            "medium":  {"font_size": 11, "body_pad": 2,  "container_pad": "2px 3px",  "screenshot_pad": 2},
            "complex": {"font_size": 12, "body_pad": 2,  "container_pad": "2px 4px",  "screenshot_pad": 2},
        }
    else:
        params = {
            "simple":  {"font_size": 9,  "body_pad": 1, "container_pad": "1px 2px", "screenshot_pad": 1},
            "medium":  {"font_size": 10, "body_pad": 1, "container_pad": "1px 2px", "screenshot_pad": 1},
            "complex": {"font_size": 11, "body_pad": 2, "container_pad": "2px 3px", "screenshot_pad": 2},
        }
    return params[complexity]


def _get_math_css(display_mode: bool, latex: str = "") -> str:
    params = _get_scale_params(latex, display_mode)
    template = MATH_CSS_DISPLAY if display_mode else MATH_CSS_INLINE
    base = template.format(
        font_size=params["font_size"],
        body_pad=params["body_pad"],
        container_pad=params["container_pad"],
    )
    return base + KATEX_THEME_CSS

# Local KaTeX bundle (Phase B1). Resolved at import; the field name stays
# `KATEX_CDN` for the HTML_TEMPLATE.format(katex_cdn=...) substitution. Falls
# back to the jsdelivr CDN if no local copy is present so renders remain
# functional in environments that ship without the npm package.
def _resolve_katex_root() -> str:
    candidates = [
        "/usr/lib/node_modules/katex/dist",
        "/usr/local/lib/node_modules/katex/dist",
        "/usr/local/lib/node_modules/@mermaid-js/mermaid-cli/node_modules/katex/dist",
    ]
    for p in candidates:
        if os.path.isfile(os.path.join(p, "katex.min.js")):
            return f"file://{p}"
    log.warning("Local KaTeX not found; falling back to jsdelivr CDN (slower).")
    return "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist"


KATEX_CDN = _resolve_katex_root()

HTML_TEMPLATE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<link rel="stylesheet" href="{katex_cdn}/katex.min.css">
<script src="{katex_cdn}/katex.min.js"></script>
<style>{css}</style>
</head><body>
<div class="math-container" id="math-target"></div>
<script>
try {{
  katex.render({latex_json}, document.getElementById('math-target'), {{
    displayMode: {display_mode},
    throwOnError: false,
    // trust: false (KaTeX default) prevents \\href, \\url, \\includegraphics
    // and \\htmlClass from receiving attacker-controlled values that could
    // execute javascript: URLs inside the Puppeteer page.
    trust: false,
    strict: false,
  }});
}} catch(e) {{
  document.getElementById('math-target').textContent = e.message;
}}
</script>
</body></html>"""


def _escape_for_json(s: str) -> str:
    """Escape a LaTeX string for embedding in JSON inside HTML."""
    import json
    return json.dumps(s)


async def render_math_image(
    latex: str,
    *,
    display_mode: bool = True,
    output_dir: str = MATH_OUTPUT_DIR,
) -> str | None:
    """Render a LaTeX expression as a PNG image. Returns the file path, or None on failure."""
    os.makedirs(output_dir, exist_ok=True)

    # Cache key includes display mode and a template version so a change to
    # HTML_TEMPLATE / KaTeX options (e.g. trust:true -> trust:false) busts
    # previously-cached PNGs that may have been rendered with insecure
    # settings. Bump _TEMPLATE_VERSION on every template/option change.
    _TEMPLATE_VERSION = "2"  # bumped when trust:true -> trust:false
    cache_key = f"v{_TEMPLATE_VERSION}:{latex}:{'display' if display_mode else 'inline'}"
    math_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
    out_path = os.path.join(output_dir, f"math_{math_hash}.png")

    # Cache hit
    if os.path.isfile(out_path):
        return out_path

    # Build HTML — scale CSS based on expression complexity
    scale_params = _get_scale_params(latex, display_mode)
    full_html = HTML_TEMPLATE.format(
        katex_cdn=KATEX_CDN,
        css=_get_math_css(display_mode, latex),
        latex_json=_escape_for_json(latex),
        display_mode="true" if display_mode else "false",
    )

    html_path = os.path.join(output_dir, f"math_{math_hash}.html")
    with open(html_path, "w") as f:
        f.write(full_html)

    # Ensure render script exists (always overwrite to pick up changes)
    script_path = os.path.join(output_dir, "_math_render.js")
    with open(script_path, "w") as f:
        f.write(_RENDER_SCRIPT)

    async with _render_sema:
        try:
            cmd = [
                "node",
                script_path,
                PUPPETEER_MODULE,
                PUPPETEER_CONFIG,
                html_path,
                out_path,
            ]
            if CHROME_EXECUTABLE:
                cmd.append(CHROME_EXECUTABLE)
            else:
                cmd.append("")  # placeholder for chromePath
            cmd.append(str(scale_params["screenshot_pad"]))  # adaptive screenshot padding
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            if proc.returncode != 0:
                log.error("Math render failed (rc=%d): %s", proc.returncode, stderr.decode()[:500])
                return None

            return out_path if os.path.isfile(out_path) else None

        except asyncio.TimeoutError:
            log.error("Math render timed out")
            try:
                proc.kill()
            except Exception:
                pass
            return None
        except Exception as e:
            log.error("Math render error: %s", e)
            return None
