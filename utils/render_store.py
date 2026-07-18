"""Filesystem boundary shared by image renderers."""

from __future__ import annotations

import hashlib
import logging
import os
import struct
import time
import uuid
import zlib

from PIL import Image, UnidentifiedImageError

log = logging.getLogger(__name__)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
MAX_PNG_DIMENSION = 16_384
MAX_PNG_PIXELS = 100_000_000


def render_paths(
    output_dir: str,
    *,
    prefix: str,
    cache_key: str,
) -> tuple[str, str]:
    """Return deterministic PNG and HTML paths for one render input."""
    render_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
    stem = os.path.join(output_dir, f"{prefix}_{render_hash}")
    return f"{stem}.png", f"{stem}.html"


def prepare(output_dir: str) -> None:
    os.makedirs(output_dir, exist_ok=True)


def _has_valid_png_structure(data: bytes) -> bool:
    if not data.startswith(PNG_SIGNATURE):
        return False
    offset = len(PNG_SIGNATURE)
    saw_ihdr = False
    saw_iend = False
    while offset < len(data):
        if len(data) - offset < 12:
            return False
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            return False
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : chunk_end])[0]
        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != expected_crc:
            return False
        if not saw_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                return False
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            if (
                width < 1
                or height < 1
                or width > MAX_PNG_DIMENSION
                or height > MAX_PNG_DIMENSION
                or width * height > MAX_PNG_PIXELS
                or bit_depth not in {1, 2, 4, 8, 16}
                or color_type not in {0, 2, 3, 4, 6}
                or compression != 0
                or filtering != 0
                or interlace not in {0, 1}
            ):
                return False
            saw_ihdr = True
        elif chunk_type == b"IHDR":
            return False
        if chunk_type == b"IEND":
            if length != 0 or chunk_end != len(data):
                return False
            saw_iend = True
            break
        offset = chunk_end
    return saw_ihdr and saw_iend


def _fully_decodes_png(path: str) -> bool:
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                return False
            image.verify()
        with Image.open(path) as image:
            image.load()
            return (
                0 < image.width <= MAX_PNG_DIMENSION
                and 0 < image.height <= MAX_PNG_DIMENSION
                and image.width * image.height <= MAX_PNG_PIXELS
            )
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return False


def is_cached(path: str) -> bool:
    """Return whether path is a structurally valid, fully decoded PNG."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < 45:
            valid = False
        else:
            with open(path, "rb") as file:
                data = file.read()
            valid = _has_valid_png_structure(data) and _fully_decodes_png(path)
    except OSError:
        valid = False
    if not valid:
        try:
            os.remove(path)
        except OSError:
            pass
    return valid


def temporary_png_path(path: str) -> str:
    """Return a same-directory unique staging path for atomic publication."""
    return f"{path}.{uuid.uuid4().hex}.part"


def publish_png(part_path: str, path: str) -> bool:
    """Validate and atomically publish one completed PNG cache entry."""
    if not is_cached(part_path):
        return False
    os.replace(part_path, path)
    return is_cached(path)


def remove_if_exists(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def write_html(path: str, html: str) -> None:
    with open(path, "w") as file:
        file.write(html)


def write_png(path: str, png: bytes) -> None:
    with open(path, "wb") as file:
        file.write(png)


def cleanup_stale_files(
    directories: list[str],
    *,
    max_age_seconds: float = 3600,
    now: float | None = None,
) -> None:
    """Remove expired attachment and render artifacts from owned directories."""
    current_time = time.time() if now is None else now
    for directory in directories:
        if not directory or not os.path.isdir(directory):
            continue
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for filename in entries:
            path = os.path.join(directory, filename)
            try:
                if (
                    os.path.isfile(path)
                    and current_time - os.path.getmtime(path) > max_age_seconds
                ):
                    os.remove(path)
                    log.info("Cleaned up stale file: %s", path)
            except FileNotFoundError:
                log.debug("cleanup_stale_files: already gone: %s", path)
            except OSError as exc:
                log.warning(
                    "cleanup_stale_files: failed to remove %s: [errno %s] %s",
                    path,
                    exc.errno,
                    exc,
                )
