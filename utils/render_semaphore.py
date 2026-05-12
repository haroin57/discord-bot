"""Shared asyncio.Semaphore for all Puppeteer-based renderers.

The 2 GB VPS can only safely host one Chromium subprocess at a time. Before
this module existed, ``utils/table_renderer.py`` and ``utils/math_renderer.py``
each held their own ``asyncio.Semaphore(1)`` instance, so a table render and a
math render in the same response could spawn two browsers concurrently and
push the host into OOM.

Import ``render_sema`` from this module in any new renderer to enforce the
"one Puppeteer at a time" invariant globally.
"""

import asyncio

__all__ = ["render_sema"]

# Single shared semaphore — one Chromium subprocess at a time across all
# renderers. Do NOT shadow this with a local copy in another module.
render_sema = asyncio.Semaphore(1)
