"""Attachment download/cleanup and image path extraction."""

import asyncio
import fnmatch
import logging
import os
import re
import tempfile
import time
from urllib.parse import urlparse

import aiohttp

from config import (
    ALLOWED_ATTACHMENT_HOSTS,
    ALLOWED_IMAGE_DIRS,
    ATTACHMENT_DL_CONCURRENCY,
    ATTACHMENT_DIR,
    MAX_ATTACHMENT_BYTES,
    MAX_IMAGE_ATTACHMENTS,
    MAX_RESPONSE_IMAGES,
)

# Broader allow-list for admin users. Admin requests are already gated in
# bot.py so we can safely allow /home/ubuntu here while still enforcing
# magic-byte checks to prevent extension-spoofing.
ADMIN_IMAGE_DIRS = ALLOWED_IMAGE_DIRS + ("/home/ubuntu",)

# Directories to search when resolving bare image filenames to full paths.
_IMAGE_SEARCH_DIRS = (
    "/tmp/table_renders",
    "/tmp/math_renders",
    "/tmp/mermaid_output",
    "/tmp/discord_bot_attachments",
    "/home/ubuntu",
)

log = logging.getLogger(__name__)

IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

# Text-based content types that can be downloaded and inlined into the prompt.
TEXT_CONTENT_TYPES = {
    "text/plain",
    "text/csv",
    "text/markdown",
    "text/html",
    "text/xml",
    "application/json",
    "application/xml",
    "application/javascript",
}
MAX_TEXT_ATTACHMENT_BYTES = 100 * 1024  # 100KB cap for inline text
MAX_TEXT_ATTACHMENTS = 3

# First bytes of supported image formats. Used by extract_image_paths to
# reject files whose extension was faked to sneak a secret past the filter.
_IMAGE_MAGIC_PREFIXES: tuple[bytes, ...] = (
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"\xff\xd8\xff",         # JPEG
    b"GIF87a",               # GIF
    b"GIF89a",               # GIF
    b"RIFF",                 # WebP (RIFF....WEBP)
    b"<svg",                 # SVG (ascii)
    b"<?xml",                # SVG with XML declaration
)

# Bound total concurrent image downloads (module-wide, shared across channels).
_download_semaphore = asyncio.Semaphore(ATTACHMENT_DL_CONCURRENCY)


def _host_is_allowed(url: str) -> bool:
    """Refuse to hit anything that isn't a Discord CDN host."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host in ALLOWED_ATTACHMENT_HOSTS

IMAGE_PATH_RE = re.compile(
    r"(?:^|\s|`|\()(\/[\w./_-]+\.(?:png|jpg|jpeg|gif|webp|svg))(?:\s|$|`|[)}\]]|[。、,.])",
    re.MULTILINE,
)

# Matches any absolute file path in response text (for admin file attachment).
# Looks for paths like /home/ubuntu/foo.txt or /tmp/bar.csv
FILE_PATH_RE = re.compile(
    r"(?:^|\s|`|\()(\/[\w./_-]+\.[\w]+)(?:\s|$|`|[)}\]]|[。、,.])",
    re.MULTILINE,
)

# Admin-only: allowed dirs for arbitrary file attachment (broader than image dirs)
ADMIN_FILE_DIRS = ("/home/ubuntu", "/tmp")
# Max file size for Discord attachment (25MB Discord limit, we cap at 24MB)
MAX_FILE_ATTACH_BYTES = 24 * 1024 * 1024
MAX_FILE_ATTACHMENTS = 5

# Path to .attachignore file (glob patterns, one per line, # comments)
_ATTACHIGNORE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".attachignore")


def _load_attachignore() -> list[str]:
    """Load ignore patterns from .attachignore file."""
    if not os.path.isfile(_ATTACHIGNORE_PATH):
        return []
    patterns: list[str] = []
    try:
        with open(_ATTACHIGNORE_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    except OSError:
        pass
    return patterns


def is_attach_ignored(path: str) -> bool:
    """Check if a file path matches any .attachignore pattern."""
    patterns = _load_attachignore()
    if not patterns:
        return False
    real = os.path.realpath(path)
    for pat in patterns:
        # Support both exact paths and glob patterns
        if fnmatch.fnmatch(real, pat) or fnmatch.fnmatch(os.path.basename(real), pat):
            log.info("Skipping attach-ignored file: %s (pattern: %s)", path, pat)
            return True
    return False


def _has_image_magic(path: str) -> bool:
    """True only if the file's first bytes match a known image format.

    Prevents extension-spoofing attacks where an attacker tricks the model
    into emitting e.g. `/home/ubuntu/.ssh/id_rsa.png` — the path ends in
    .png but the content is a private key, and without magic-byte checks
    the bot would happily attach it back to Discord.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    return any(head.startswith(m) for m in _IMAGE_MAGIC_PREFIXES)


def normalize_image_filenames(text: str) -> str:
    """Resolve bare image filenames to full paths by searching known directories.

    E.g. ``nikkei225_butterfly.png`` -> ``/home/ubuntu/nikkei225_butterfly.png``
    Only replaces when exactly one match is found to avoid ambiguity.
    """
    # Match bare filenames (not starting with /) that look like images
    bare_re = re.compile(
        r"(?<![/\w])(\b[\w._-]+\.(?:png|jpg|jpeg|gif|webp|svg))\b",
    )
    replacements: dict[str, str] = {}
    for m in bare_re.finditer(text):
        fname = m.group(1)
        if fname in replacements:
            continue
        # Don't touch if it's already preceded by a slash (part of a path)
        start = m.start()
        if start > 0 and text[start - 1] == "/":
            continue
        # Search known dirs for this filename
        candidates = []
        for d in _IMAGE_SEARCH_DIRS:
            full = os.path.join(d, fname)
            if os.path.isfile(full):
                candidates.append(full)
        if len(candidates) == 1:
            replacements[fname] = candidates[0]
            log.info("Normalized bare filename %s -> %s", fname, candidates[0])
    for fname, full_path in replacements.items():
        text = text.replace(fname, full_path)
    return text


def extract_image_paths(text: str, *, admin: bool = False) -> list[str]:
    """Extract existing image file paths from response text (capped + hardened).

    Hardening vs the naive version:
      - `ALLOWED_IMAGE_DIRS` is narrow (attachment tmp + mermaid output)
      - Paths are realpath-resolved so symlinks can't escape the allowed dirs
      - Files must match an image magic-byte prefix (no extension spoofing)

    When *admin* is True, the broader ADMIN_IMAGE_DIRS allow-list is used,
    which includes /home/ubuntu. Magic-byte checks are still enforced.
    """
    dirs = ADMIN_IMAGE_DIRS if admin else ALLOWED_IMAGE_DIRS
    allowed_real = tuple(os.path.realpath(d) for d in dirs)
    paths: list[str] = []
    seen: set[str] = set()
    for m in IMAGE_PATH_RE.finditer(text):
        if len(paths) >= MAX_RESPONSE_IMAGES:
            break
        raw = m.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        try:
            real = os.path.realpath(raw)
        except (OSError, ValueError):
            continue
        if not any(real == d or real.startswith(d + os.sep) for d in allowed_real):
            log.warning("Rejecting image path outside allow-list: %s", raw)
            continue
        if not os.path.isfile(real):
            continue
        if not _has_image_magic(real):
            log.warning("Rejecting path with non-image content: %s", raw)
            continue
        paths.append(real)
    return paths


def strip_attached_paths(text: str, paths: list[str]) -> str:
    """Remove successfully attached file paths from the response text.

    Strips the bare path (and any surrounding whitespace on otherwise-empty
    lines) so the user sees the file as a Discord attachment instead of a
    raw path string.  Also handles the ``[attach:/path]`` and
    ``[Attachment: name]`` notation variants.
    """
    for p in paths:
        basename = os.path.basename(p)
        # Remove [attach:/path/to/file] pattern
        text = re.sub(
            r"\[attach:" + re.escape(p) + r"\]",
            "",
            text,
        )
        # Remove [Attachment: filename] pattern
        text = re.sub(
            r"\[Attachment:\s*" + re.escape(basename) + r"\]",
            "",
            text,
        )
        # Remove the bare path itself (possibly on its own line)
        text = re.sub(
            r"(?m)^[ \t]*" + re.escape(p) + r"[ \t]*\n?",
            "",
            text,
        )
        # Also catch inline occurrences (not on their own line)
        text = text.replace(p, "")
    # Collapse 3+ consecutive newlines down to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_file_paths(text: str) -> list[str]:
    """Extract existing file paths from response text (admin-only, any file type).

    Only files under ADMIN_FILE_DIRS, resolved via realpath to prevent symlink escape.
    Skips image files (those are handled by extract_image_paths).
    """
    image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
    allowed_real = tuple(os.path.realpath(d) for d in ADMIN_FILE_DIRS)
    paths: list[str] = []
    seen: set[str] = set()
    for m in FILE_PATH_RE.finditer(text):
        if len(paths) >= MAX_FILE_ATTACHMENTS:
            break
        raw = m.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        ext = os.path.splitext(raw)[1].lower()
        if ext in image_exts:
            continue  # handled by extract_image_paths
        try:
            real = os.path.realpath(raw)
        except (OSError, ValueError):
            continue
        if not any(real == d or real.startswith(d + os.sep) for d in allowed_real):
            log.warning("Rejecting file path outside admin allow-list: %s", raw)
            continue
        if not os.path.isfile(real):
            continue
        try:
            size = os.path.getsize(real)
        except OSError:
            continue
        if size > MAX_FILE_ATTACH_BYTES:
            log.warning("Rejecting file too large for Discord: %s (%d bytes)", raw, size)
            continue
        if size == 0:
            continue
        paths.append(real)
    return paths


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


async def _download_one(session: aiohttp.ClientSession, att) -> str | None:
    """Download a single image attachment with size guard and streaming abort."""
    if att.size and att.size > MAX_ATTACHMENT_BYTES:
        log.warning("Skipping oversized attachment %s (%d bytes)", att.filename, att.size)
        return None
    # SSRF guard: Discord sometimes returns unexpected URLs and without this
    # a malicious CDN entry could redirect the bot into internal network
    # targets (metadata endpoints, localhost services, etc.).
    if not _host_is_allowed(att.url):
        log.warning("Rejecting non-allow-listed attachment URL: %s", att.url)
        return None
    # Sanitize the destination filename — never trust att.id or att.filename
    # to be path-safe. We only use att.id (numeric) and a lowercased ext.
    ext = os.path.splitext(att.filename or "")[1].lower()
    if not ext:
        ext = ".png"  # harmless default when Discord sends an extensionless name
    elif ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        ext = ".bin"
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "", str(att.id))[:64] or "att"
    dest = os.path.realpath(os.path.join(ATTACHMENT_DIR, f"{safe_id}{ext}"))
    # Confirm the destination stays inside ATTACHMENT_DIR after realpath
    # (defence against symlinked attachment dir shenanigans).
    att_dir_real = os.path.realpath(ATTACHMENT_DIR)
    if not (dest == att_dir_real or dest.startswith(att_dir_real + os.sep)):
        log.warning("Refusing attachment dest outside ATTACHMENT_DIR: %s", dest)
        return None
    async with _download_semaphore:
        try:
            async with session.get(att.url, allow_redirects=False) as resp:
                if resp.status != 200:
                    return None
                total = 0
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > MAX_ATTACHMENT_BYTES:
                            log.warning(
                                "Aborting download of %s: exceeded %d bytes mid-stream",
                                att.filename, MAX_ATTACHMENT_BYTES,
                            )
                            f.close()
                            try:
                                os.remove(dest)
                            except OSError:
                                pass
                            return None
                        f.write(chunk)
                log.info("Downloaded attachment: %s -> %s", att.filename, dest)
                return dest
        except Exception as e:
            log.warning("Failed to download attachment %s: %s", att.filename, e)
            try:
                if os.path.isfile(dest):
                    os.remove(dest)
            except OSError:
                pass
    return None


async def download_attachments(http_session: aiohttp.ClientSession, attachments: list) -> list[str]:
    """Download image attachments in parallel (capped count + shared semaphore)."""
    image_atts = [a for a in attachments
                  if a.content_type and a.content_type.split(";")[0].strip() in IMAGE_CONTENT_TYPES]
    if not image_atts:
        return []
    if len(image_atts) > MAX_IMAGE_ATTACHMENTS:
        log.warning(
            "Capping image attachments %d -> %d (MAX_IMAGE_ATTACHMENTS)",
            len(image_atts), MAX_IMAGE_ATTACHMENTS,
        )
        image_atts = image_atts[:MAX_IMAGE_ATTACHMENTS]
    results = await asyncio.gather(*[_download_one(http_session, a) for a in image_atts])
    return [p for p in results if p]


def cleanup_attachments(paths: list[str]) -> None:
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


async def _download_text_one(session: aiohttp.ClientSession, att) -> tuple[str, str] | None:
    """Download a single text attachment and return (filename, content)."""
    size = att.size or 0
    if size > MAX_TEXT_ATTACHMENT_BYTES:
        log.warning("Skipping oversized text attachment %s (%d bytes)", att.filename, size)
        return None
    if not _host_is_allowed(att.url):
        log.warning("Rejecting non-allow-listed text attachment URL: %s", att.url)
        return None
    async with _download_semaphore:
        try:
            async with session.get(att.url, allow_redirects=False) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                if len(data) > MAX_TEXT_ATTACHMENT_BYTES:
                    log.warning("Text attachment %s too large after download", att.filename)
                    return None
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    text = data.decode("latin-1")
                log.info("Downloaded text attachment: %s (%d bytes)", att.filename, len(data))
                return (att.filename or "attachment.txt", text)
        except Exception as e:
            log.warning("Failed to download text attachment %s: %s", att.filename, e)
    return None


async def download_text_attachments(
    http_session: aiohttp.ClientSession, attachments: list
) -> list[tuple[str, str]]:
    """Download text-based attachments and return list of (filename, content).

    Filters by TEXT_CONTENT_TYPES; Discord auto-converts long messages to
    message.txt with content_type text/plain; charset=utf-8.
    """
    text_atts = []
    for a in attachments:
        if not a.content_type:
            continue
        base_type = a.content_type.split(";")[0].strip().lower()
        if base_type in TEXT_CONTENT_TYPES:
            text_atts.append(a)
    if not text_atts:
        return []
    if len(text_atts) > MAX_TEXT_ATTACHMENTS:
        text_atts = text_atts[:MAX_TEXT_ATTACHMENTS]
    results = await asyncio.gather(*[_download_text_one(http_session, a) for a in text_atts])
    return [r for r in results if r]


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
