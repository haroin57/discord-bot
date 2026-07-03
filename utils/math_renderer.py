"""Render LaTeX math expressions as PNG images using KaTeX via Cloudflare
Browser Rendering.

Phase 2-1: the local Puppeteer daemon was replaced by the Cloudflare Browser
Rendering REST screenshot API (utils/cf_browser.py). KaTeX is loaded from a
pinned jsDelivr CDN inside the remote browser (the old local file:// bundle is
invisible to Cloudflare's browser), and the screenshot is clipped tight to the
math element. A ``networkidle0`` wait ensures the KaTeX JS + web fonts finish
loading before capture.
"""

import hashlib
import logging
import os

from config import KATEX_CDN_BASE
from utils.cf_browser import is_configured, render_screenshot

__all__ = ["render_math_image"]

log = logging.getLogger(__name__)

MATH_OUTPUT_DIR = "/tmp/math_renders"

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

# KaTeX is served from a pinned jsDelivr CDN (config.KATEX_CDN_BASE) and loaded
# by the *remote* Cloudflare browser — the previous local file:// bundle is
# unreachable from Cloudflare's browser. Math rendering is therefore available
# whenever the Browser Rendering client is configured (token + account id).
# The network dependency that the old local bundle avoided is now inherent to
# every render (the whole call is a Cloudflare HTTP request); the CDN version is
# pinned to keep the supply-chain surface small.
_KATEX_AVAILABLE: bool = is_configured()
if not _KATEX_AVAILABLE:
    log.warning(
        "Cloudflare Browser Rendering not configured; math rendering disabled. "
        "Set CF_BROWSER_RENDERING_TOKEN/CF_ACCOUNT_ID to enable."
    )

# Field name stays `KATEX_CDN` for HTML_TEMPLATE.format(katex_cdn=...) substitution.
KATEX_CDN: str = KATEX_CDN_BASE

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
    """Render a LaTeX expression as a PNG image.

    Returns the file path on success, or None if KaTeX is unavailable or
    rendering fails. Requires a local KaTeX install (`npm install katex`).
    """
    if not _KATEX_AVAILABLE:
        log.warning("render_math_image called but KaTeX is not installed locally; returning None")
        return None
    os.makedirs(output_dir, exist_ok=True)

    # Cache key includes display mode and a template version so a change to
    # HTML_TEMPLATE / KaTeX options (e.g. trust:true -> trust:false) busts
    # previously-cached PNGs that may have been rendered with insecure
    # settings. Bump _TEMPLATE_VERSION on every template/option change.
    _TEMPLATE_VERSION = "3"  # bumped: Puppeteer daemon -> Cloudflare Browser Rendering (CDN KaTeX)
    cache_key = f"v{_TEMPLATE_VERSION}:{latex}:{'display' if display_mode else 'inline'}"
    math_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
    out_path = os.path.join(output_dir, f"math_{math_hash}.png")

    # Cache hit
    if os.path.isfile(out_path):
        return out_path

    # Build HTML — _get_math_css scales font/padding by expression complexity.
    full_html = HTML_TEMPLATE.format(
        katex_cdn=KATEX_CDN,
        css=_get_math_css(display_mode, latex),
        latex_json=_escape_for_json(latex),
        display_mode="true" if display_mode else "false",
    )

    # Keep the .html on disk for debugging/parity; the remote browser renders
    # from the inlined string (it cannot see local files).
    html_path = os.path.join(output_dir, f"math_{math_hash}.html")
    with open(html_path, "w") as f:
        f.write(full_html)

    # deviceScaleFactor=2 for crisp glyphs (matches the old viewport). Wait for
    # networkidle0 so the CDN KaTeX bundle + web fonts render before capture.
    png_bytes = await render_screenshot(
        full_html,
        selector=".math-container",
        device_scale_factor=2,
        wait_for_network_idle=True,
    )
    if not png_bytes:
        log.warning("Math render failed (Browser Rendering returned no image)")
        return None
    try:
        with open(out_path, "wb") as f:
            f.write(png_bytes)
    except OSError as e:
        log.error("Failed to write math PNG: %s", e)
        return None
    return out_path if os.path.isfile(out_path) else None
