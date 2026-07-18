"""Cloudflare Browser Rendering REST client (Phase 2-1).

Replaces the local Puppeteer daemon for the table / math renderers. The
``/browser-rendering/screenshot`` endpoint accepts an HTML string plus screenshot
options and returns the PNG **bytes** directly (200, ``Content-Type: image/png``).

A top-level ``selector`` clips the screenshot to that element — the remote
equivalent of the old daemon's element-crop, so we do NOT get a 1920x1080
full-page image with acres of background.

The renderers keep their existing contract (HTML string in, PNG file path out,
``None`` on failure → text fallback). This module only owns the network call:
build the request body, POST it with a bounded timeout + one retry, and return
the raw PNG bytes (or ``None``). It never raises to the caller.
"""

from __future__ import annotations

import logging

from config import (
    CF_ACCOUNT_ID,
    CF_BROWSER_RENDERING_RETRIES,
    CF_BROWSER_RENDERING_TIMEOUT_S,
    CF_BROWSER_RENDERING_TOKEN,
    CF_BROWSER_RENDERING_URL,
)
from utils.browser_backend import render_remote_screenshot

__all__ = ["render_screenshot", "is_configured"]

log = logging.getLogger(__name__)


def is_configured() -> bool:
    """True when a Browser Rendering token + account id are present.

    When False the renderers short-circuit to their text fallback instead of
    firing a doomed HTTP call (mirrors the old "KaTeX/daemon unavailable" path).
    """
    return bool(CF_BROWSER_RENDERING_TOKEN and CF_ACCOUNT_ID)


async def render_screenshot(
    html: str,
    *,
    selector: str | None = None,
    device_scale_factor: float | None = None,
    wait_for_network_idle: bool = False,
    goto_timeout_ms: int = 20000,
    omit_background: bool = False,
) -> bytes | None:
    """POST HTML to Cloudflare Browser Rendering; return PNG bytes or None.

    Parameters mirror what the two renderers need:
      - ``selector``: clip the screenshot to this element (tight crop). Omit for
        a full page.
      - ``device_scale_factor``: HiDPI multiplier (math uses 2 for crisp glyphs).
      - ``wait_for_network_idle``: wait for ``networkidle0`` before capture so a
        CDN-loaded KaTeX bundle + fonts finish (math). Tables need no network.
      - ``omit_background`` / ``goto_timeout_ms``: passthrough options.

    Never raises: on timeout, transient error, or non-200 it logs and returns
    ``None`` so callers fall back to text. Retries ``CF_BROWSER_RENDERING_RETRIES``
    times on network / 5xx / 429.
    """
    return await render_remote_screenshot(
        html,
        token=CF_BROWSER_RENDERING_TOKEN,
        account_id=CF_ACCOUNT_ID,
        url=CF_BROWSER_RENDERING_URL,
        timeout_seconds=CF_BROWSER_RENDERING_TIMEOUT_S,
        retries=CF_BROWSER_RENDERING_RETRIES,
        selector=selector,
        device_scale_factor=device_scale_factor,
        wait_for_network_idle=wait_for_network_idle,
        goto_timeout_ms=goto_timeout_ms,
        omit_background=omit_background,
    )
