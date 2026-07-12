"""Render LaTeX math expressions locally with KaTeX and Puppeteer."""

import hashlib
import logging
import os

from utils.browser_pool import render_with_browser_daemon

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

# Local-only assets avoid Cloudflare's Quick Actions rate limit and remove a
# network dependency from the response path.
_KATEX_CANDIDATES = [
    "/usr/lib/node_modules/katex/dist",
    "/usr/local/lib/node_modules/katex/dist",
    "/usr/local/lib/node_modules/@mermaid-js/mermaid-cli/node_modules/katex/dist",
]


def _resolve_katex_root() -> str | None:
    for path in _KATEX_CANDIDATES:
        if os.path.isfile(os.path.join(path, "katex.min.js")):
            return f"file://{path}"
    return None


_katex_root = _resolve_katex_root()
_KATEX_AVAILABLE: bool = _katex_root is not None
if not _KATEX_AVAILABLE:
    log.warning(
        "Local KaTeX not found (checked %s); math rendering disabled",
        ", ".join(_KATEX_CANDIDATES),
    )

KATEX_CDN: str = _katex_root or ""

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
        log.warning("render_math_image called without a local KaTeX installation")
        return None
    os.makedirs(output_dir, exist_ok=True)

    # Cache key includes display mode and a template version so a change to
    # HTML_TEMPLATE / KaTeX options (e.g. trust:true -> trust:false) busts
    # previously-cached PNGs that may have been rendered with insecure
    # settings. Bump _TEMPLATE_VERSION on every template/option change.
    _TEMPLATE_VERSION = "4"  # local Puppeteer backend
    cache_key = f"v{_TEMPLATE_VERSION}:{latex}:{'display' if display_mode else 'inline'}"
    math_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
    out_path = os.path.join(output_dir, f"math_{math_hash}.png")

    # Cache hit
    if os.path.isfile(out_path):
        return out_path

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

    try:
        await render_with_browser_daemon(
            html_path=html_path,
            output_path=out_path,
            selector=".math-container",
            pad=scale_params["screenshot_pad"],
            viewport={"width": 800, "height": 600, "deviceScaleFactor": 2},
        )
    except TimeoutError:
        log.error("Local math render timed out")
        return None
    except Exception as exc:
        log.error("Local math render failed: %s", exc)
        return None
    return out_path if os.path.isfile(out_path) else None
