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

import asyncio
import logging

import aiohttp

from config import (
    CF_ACCOUNT_ID,
    CF_BROWSER_RENDERING_RETRIES,
    CF_BROWSER_RENDERING_TIMEOUT_S,
    CF_BROWSER_RENDERING_TOKEN,
    CF_BROWSER_RENDERING_URL,
)

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
    if not is_configured():
        log.warning(
            "Cloudflare Browser Rendering not configured "
            "(CF_BROWSER_RENDERING_TOKEN/CF_ACCOUNT_ID missing); skipping render"
        )
        return None

    body: dict = {"html": html}
    if selector:
        body["selector"] = selector

    screenshot_options: dict = {}
    if omit_background:
        screenshot_options["omitBackground"] = True
    if screenshot_options:
        body["screenshotOptions"] = screenshot_options

    if device_scale_factor is not None:
        # viewport.deviceScaleFactor drives HiDPI capture on the remote browser.
        # The API's viewport schema requires width+height too, so supply the old
        # daemon's math viewport (800x600 — the element is selector-clipped
        # afterwards, so these only bound the page, not the output crop).
        body["viewport"] = {
            "width": 800,
            "height": 600,
            "deviceScaleFactor": device_scale_factor,
        }

    goto_options: dict = {"timeout": goto_timeout_ms}
    if wait_for_network_idle:
        goto_options["waitUntil"] = "networkidle0"
    body["gotoOptions"] = goto_options

    headers = {
        "Authorization": f"Bearer {CF_BROWSER_RENDERING_TOKEN}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=CF_BROWSER_RENDERING_TIMEOUT_S)

    attempts = max(1, CF_BROWSER_RENDERING_RETRIES + 1)
    for attempt in range(attempts):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    CF_BROWSER_RENDERING_URL, json=body, headers=headers
                ) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if data:
                            return data
                        log.error("Browser Rendering returned 200 but empty body")
                        return None
                    # A 4xx other than 429 is a request-shape / auth problem —
                    # retrying will not help, so bail immediately.
                    transient = resp.status == 429 or resp.status >= 500
                    detail = (await resp.text())[:400]
                    log.error(
                        "Browser Rendering HTTP %s (attempt %d/%d): %s",
                        resp.status, attempt + 1, attempts, detail,
                    )
                    if not transient:
                        return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.error(
                "Browser Rendering request failed (attempt %d/%d): %s",
                attempt + 1, attempts, e,
            )

        # Brief backoff before the retry (skip after the final attempt).
        if attempt < attempts - 1:
            await asyncio.sleep(1.0)

    return None
