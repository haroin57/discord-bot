"""Attachment download/cleanup and image path extraction."""

import asyncio
import logging
import os
import re
import tempfile
import time

import aiohttp

from config import ALLOWED_IMAGE_DIRS, ATTACHMENT_DIR, MAX_ATTACHMENT_BYTES, MAX_RESPONSE_IMAGES

log = logging.getLogger(__name__)

IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

IMAGE_PATH_RE = re.compile(
    r"(?:^|\s|`|\()(\/[\w./_-]+\.(?:png|jpg|jpeg|gif|webp|svg))(?:\s|$|`|[)}\]]|[。、,.])",
    re.MULTILINE,
)


def extract_image_paths(text: str) -> list[str]:
    """Extract existing image file paths from response text (capped & path-validated)."""
    paths: list[str] = []
    seen: set[str] = set()
    for m in IMAGE_PATH_RE.finditer(text):
        if len(paths) >= MAX_RESPONSE_IMAGES:
            break
        p = m.group(1)
        if p in seen:
            continue
        seen.add(p)
        if any(p.startswith(d) for d in ALLOWED_IMAGE_DIRS) and os.path.isfile(p):
            paths.append(p)
    return paths


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


async def _download_one(session: aiohttp.ClientSession, att) -> str | None:
    """Download a single image attachment with size guard and streaming."""
    if att.size and att.size > MAX_ATTACHMENT_BYTES:
        log.warning("Skipping oversized attachment %s (%d bytes)", att.filename, att.size)
        return None
    ext = os.path.splitext(att.filename)[1] or ".png"
    dest = os.path.join(ATTACHMENT_DIR, f"{att.id}{ext}")
    try:
        async with session.get(att.url) as resp:
            if resp.status == 200:
                chunks: list[bytes] = []
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    chunks.append(chunk)
                await asyncio.to_thread(_write_file, dest, b"".join(chunks))
                log.info("Downloaded attachment: %s -> %s", att.filename, dest)
                return dest
    except Exception as e:
        log.warning("Failed to download attachment %s: %s", att.filename, e)
    return None


async def download_attachments(http_session: aiohttp.ClientSession, attachments: list) -> list[str]:
    """Download image attachments in parallel."""
    image_atts = [a for a in attachments
                  if a.content_type and a.content_type.split(";")[0].strip() in IMAGE_CONTENT_TYPES]
    if not image_atts:
        return []
    results = await asyncio.gather(*[_download_one(http_session, a) for a in image_atts])
    return [p for p in results if p]


def cleanup_attachments(paths: list[str]) -> None:
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def cleanup_stale_attachments() -> None:
    """Remove leftover attachment files older than 1 hour."""
    if not os.path.isdir(ATTACHMENT_DIR):
        return
    now = time.time()
    for fname in os.listdir(ATTACHMENT_DIR):
        fpath = os.path.join(ATTACHMENT_DIR, fname)
        try:
            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 3600:
                os.remove(fpath)
                log.info("Cleaned up stale attachment: %s", fpath)
        except OSError:
            pass
