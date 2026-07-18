"""Attachment download/cleanup and image path extraction."""

import asyncio
import os
import re

import aiohttp

from config import (
    ALLOWED_ATTACHMENT_HOSTS,
    ALLOWED_IMAGE_DIRS,
    ATTACHMENT_DL_CONCURRENCY,
    ATTACHMENT_DIR,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_IMAGE_HEIGHT,
    MAX_ATTACHMENT_IMAGE_PIXELS,
    MAX_ATTACHMENT_IMAGE_WIDTH,
    MAX_IMAGE_ATTACHMENTS,
    MAX_RESPONSE_IMAGES,
)
from utils import attachment_downloader, attachment_policy, attachment_store

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

# Bound total concurrent image downloads (module-wide, shared across channels).
_download_semaphore = asyncio.Semaphore(ATTACHMENT_DL_CONCURRENCY)


def _host_is_allowed(url: str) -> bool:
    """Refuse to hit anything that isn't a Discord CDN host."""
    return attachment_policy.host_is_allowed(url, ALLOWED_ATTACHMENT_HOSTS)


# Leading delimiter also accepts ':' and full-width '：' so that Japanese
# "ラベル：/abs/path.png" (colon immediately before the path, no space) is
# caught — the trailing set already allows CJK punctuation, so the anchors
# were asymmetric and silently dropped colon-prefixed paths.
IMAGE_PATH_RE = re.compile(
    r"(?:^|[\s`(:：])(\/[\w./_-]{1,512}\.(?:png|jpg|jpeg|gif|webp|svg))(?:\s|$|`|[)}\]]|[。、,.])",
    re.MULTILINE,
)

# Supplementary pattern: image paths enclosed in quotes or Markdown link syntax.
# Only when surrounded by a clear delimiter do we allow spaces and parens inside
# the path (to catch e.g. `/tmp/出力 (1).png`). The outer delimiters ensure we
# don't greedily consume prose text.
_IMAGE_PATH_QUOTED_RE = re.compile(
    r"""(?x)
    (?:
        # Backtick-enclosed: `path`  — allows spaces and parens inside
        `(\/[^`\n]{1,512}?\.(?:png|jpg|jpeg|gif|webp|svg))`
        |
        # Double-quote-enclosed: "path"
        \"(\/[^\"\n]{1,512}?\.(?:png|jpg|jpeg|gif|webp|svg))\"
        |
        # Single-quote-enclosed: 'path'
        \'(\/[^\'\n]{1,512}?\.(?:png|jpg|jpeg|gif|webp|svg))\'
        |
        # Markdown link: [text](path)
        # The path may contain parens (e.g. "(1)" in filenames); we match
        # lazily up to the extension, then accept the closing ')' that
        # immediately follows the extension — NOT any ')' inside the path.
        \[[^\]]{0,200}\]\((\/[^"\'\`\n]{1,512}?\.(?:png|jpg|jpeg|gif|webp|svg))\)
    )
    """,
    re.MULTILINE,
)

# Matches any absolute file path in response text (for admin file attachment).
# Looks for paths like /home/ubuntu/foo.txt or /tmp/bar.csv
FILE_PATH_RE = re.compile(
    r"(?:^|[\s`(:：])(\/[\w./_-]{1,512}\.[\w]{1,16})(?:\s|$|`|[)}\]]|[。、,.])",
    re.MULTILINE,
)

# Supplementary pattern: non-image file paths enclosed in quotes or Markdown links.
# Allows spaces and parens inside the path only when clear delimiters are present.
_FILE_PATH_QUOTED_RE = re.compile(
    r"""(?x)
    (?:
        # Backtick-enclosed: `path`  — allows spaces and parens inside
        `(\/[^`\n]{1,512}?\.[\w]{1,16})`
        |
        # Double-quote-enclosed: "path"
        \"(\/[^\"\n]{1,512}?\.[\w]{1,16})\"
        |
        # Single-quote-enclosed: 'path'
        \'(\/[^\'\n]{1,512}?\.[\w]{1,16})\'
        |
        # Markdown link: [text](path)
        # The path may contain parens in the filename; match lazily up to
        # the extension, then require the closing ')' immediately after.
        \[[^\]]{0,200}\]\((\/[^"\'\`\n]{1,512}?\.[\w]{1,16})\)
    )
    """,
    re.MULTILINE,
)

# Admin-only: allowed dirs for arbitrary file attachment (broader than image dirs)
ADMIN_FILE_DIRS = ("/home/ubuntu", "/tmp")
# Max file size for Discord attachment (25MB Discord limit, we cap at 24MB)
MAX_FILE_ATTACH_BYTES = 24 * 1024 * 1024
MAX_FILE_ATTACHMENTS = 5

# Hardcoded basename denylist applied regardless of allowed dir. Defence in
# depth on top of .attachignore: a prompt-injection attempt that makes the
# model emit a credential path should not be auto-attachable even if
# .attachignore is missing or its globs don't cover hidden-file variants
# (e.g. `.credentials.json` vs the existing `credentials.json` pattern).
_SECRET_BASENAME_EXACT = frozenset(
    {
        ".env",
        ".credentials.json",
        "credentials.json",
        "hosts.yml",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "authorized_keys",
        "known_hosts",
        ".netrc",
        ".pgpass",
    }
)
_SECRET_BASENAME_GLOBS = (
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.token",
    "*.secret",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
)


def _basename_is_secret(path: str) -> bool:
    """True if the file's basename matches a known credential/secret pattern.

    Applied as a denylist on top of the allowed-dir check so that even a
    permissive ADMIN_FILE_DIRS can't leak hidden credential files.
    """
    return attachment_policy.basename_is_secret(
        path,
        exact_names=_SECRET_BASENAME_EXACT,
        glob_patterns=_SECRET_BASENAME_GLOBS,
    )


# Path to .attachignore file (glob patterns, one per line, # comments)
_ATTACHIGNORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), ".attachignore"
)

# Cached parsed patterns. The file is small and rarely edited; cache on first
# read and invalidate via mtime so a manual edit picks up on the next call
# without restarting the bot. Previously each call re-opened the file —
# `is_attach_ignored` runs per candidate image path in every response so the
# repeated open() showed up in profiles.
_attachignore_cache: list[str] | None = None
_attachignore_mtime: float = -1.0


def _load_attachignore() -> list[str]:
    """Load ignore patterns from .attachignore, mtime-cached."""
    global _attachignore_cache, _attachignore_mtime
    _attachignore_cache, _attachignore_mtime = attachment_policy.load_attachignore(
        _ATTACHIGNORE_PATH,
        cached_patterns=_attachignore_cache,
        cached_mtime=_attachignore_mtime,
    )
    return _attachignore_cache


def is_attach_ignored(path: str) -> bool:
    """Check if a file path matches any .attachignore pattern."""
    return attachment_policy.path_is_ignored(path, _load_attachignore())


def _has_image_magic(path: str) -> bool:
    """True only if the file's first bytes match a known image format.

    Prevents extension-spoofing attacks where an attacker tricks the model
    into emitting e.g. `/home/ubuntu/.ssh/id_rsa.png` — the path ends in
    .png but the content is a private key, and without magic-byte checks
    the bot would happily attach it back to Discord.
    """
    return attachment_policy.has_image_magic(path)


def normalize_image_filenames(text: str) -> str:
    """Resolve bare image filenames to full paths by searching known directories.

    E.g. ``nikkei225_butterfly.png`` -> ``/home/ubuntu/nikkei225_butterfly.png``
    Only replaces when exactly one match is found to avoid ambiguity.
    """
    return attachment_policy.normalize_image_filenames(text, _IMAGE_SEARCH_DIRS)


def extract_image_paths(text: str, *, admin: bool = False) -> list[str]:
    """Extract existing image file paths from response text (capped + hardened).

    Hardening vs the naive version:
      - `ALLOWED_IMAGE_DIRS` is narrow (attachment tmp + mermaid output)
      - Paths are realpath-resolved so symlinks can't escape the allowed dirs
      - Files must match an image magic-byte prefix (no extension spoofing)

    When *admin* is True, the broader ADMIN_IMAGE_DIRS allow-list is used,
    which includes /home/ubuntu. Magic-byte checks are still enforced.
    """
    return attachment_policy.extract_image_paths(
        text,
        admin=admin,
        allowed_dirs=ALLOWED_IMAGE_DIRS,
        admin_dirs=ADMIN_IMAGE_DIRS,
        max_paths=MAX_RESPONSE_IMAGES,
        path_pattern=IMAGE_PATH_RE,
        quoted_pattern=_IMAGE_PATH_QUOTED_RE,
        image_validator=_has_image_magic,
    )


def strip_attached_paths(text: str, paths: list[str]) -> str:
    """Remove successfully attached file paths from the response text.

    Strips the bare path (and any surrounding whitespace on otherwise-empty
    lines) so the user sees the file as a Discord attachment instead of a
    raw path string.  Also handles the ``[attach:/path]`` and
    ``[Attachment: name]`` notation variants.
    """
    return attachment_policy.strip_attached_paths(text, paths)


def extract_file_paths(text: str) -> list[str]:
    """Extract existing file paths from response text (admin-only, any file type).

    Only files under ADMIN_FILE_DIRS, resolved via realpath to prevent symlink escape.
    Skips image files (those are handled by extract_image_paths).
    """
    return attachment_policy.extract_file_paths(
        text,
        allowed_dirs=ADMIN_FILE_DIRS,
        max_paths=MAX_FILE_ATTACHMENTS,
        max_bytes=MAX_FILE_ATTACH_BYTES,
        path_pattern=FILE_PATH_RE,
        quoted_pattern=_FILE_PATH_QUOTED_RE,
        secret_check=_basename_is_secret,
        ignore_check=is_attach_ignored,
    )


def _write_file(path: str, data: bytes) -> None:
    attachment_store.write_file(path, data)


async def _download_one(session: aiohttp.ClientSession, att) -> str | None:
    """Download a single image attachment with size guard and streaming abort."""
    return await attachment_downloader.download_image(
        session,
        att,
        attachment_dir=ATTACHMENT_DIR,
        max_bytes=MAX_ATTACHMENT_BYTES,
        max_width=MAX_ATTACHMENT_IMAGE_WIDTH,
        max_height=MAX_ATTACHMENT_IMAGE_HEIGHT,
        max_pixels=MAX_ATTACHMENT_IMAGE_PIXELS,
        semaphore=_download_semaphore,
        host_check=_host_is_allowed,
    )


async def download_attachments(
    http_session: aiohttp.ClientSession, attachments: list
) -> list[str]:
    """Download image attachments in parallel (capped count + shared semaphore)."""
    return await attachment_downloader.download_images(
        http_session,
        attachments,
        image_content_types=IMAGE_CONTENT_TYPES,
        max_attachments=MAX_IMAGE_ATTACHMENTS,
        download_one=_download_one,
    )


def attachment_metadata_by_path(attachments: list, paths: list[str]) -> dict[str, dict]:
    """Recover Discord attachment metadata for downloaded local image paths.

    ``_download_one`` deliberately names each file ``<attachment_id>.<ext>``.
    This helper keeps the long-standing ``download_attachments -> list[str]``
    contract while making the Discord snowflake and original filename explicit
    for durable archives.
    """
    return attachment_store.attachment_metadata_by_path(attachments, paths)


def _cleanup_attachments_sync(paths: list[str]) -> None:
    """Synchronous helper for cleanup_attachments; runs in a worker thread."""
    attachment_store.cleanup(paths)


def cleanup_attachments(paths: list[str]) -> None:
    """Remove temp attachment files. Synchronous wrapper kept for back-compat.

    Hot-path callers should prefer `cleanup_attachments_async` so the disk
    unlinks run on a worker thread instead of blocking the event loop.
    """
    _cleanup_attachments_sync(paths)


async def cleanup_attachments_async(paths: list[str]) -> None:
    """Async cleanup — offloads N os.remove() calls to a worker thread.

    Use from any `async def` finally-block (bot.py, web_api.py) so the
    event loop doesn't pause on temp-file unlinks.
    """
    await attachment_store.cleanup_async(paths)


async def _download_text_one(
    session: aiohttp.ClientSession, att
) -> tuple[str, str] | None:
    """Download a single text attachment and return (filename, content)."""
    return await attachment_downloader.download_text(
        session,
        att,
        max_bytes=MAX_TEXT_ATTACHMENT_BYTES,
        semaphore=_download_semaphore,
        host_check=_host_is_allowed,
    )


async def download_text_attachments(
    http_session: aiohttp.ClientSession, attachments: list
) -> list[tuple[str, str]]:
    """Download text-based attachments and return list of (filename, content).

    Filters by TEXT_CONTENT_TYPES; Discord auto-converts long messages to
    message.txt with content_type text/plain; charset=utf-8.
    """
    return await attachment_downloader.download_texts(
        http_session,
        attachments,
        text_content_types=TEXT_CONTENT_TYPES,
        max_attachments=MAX_TEXT_ATTACHMENTS,
        download_one=_download_text_one,
    )


# ---------------------------------------------------------------------------
# Arbitrary binary attachments (admin only)
# ---------------------------------------------------------------------------
# Image / text downloaders above filter by MIME type and silently drop
# anything else. For admins that means a .als / .zip / .pdf etc. would
# vanish without a log line. download_arbitrary_attachments fills that
# gap: keep any attachment that BOTH the image and text downloaders
# rejected, save it under ATTACHMENT_DIR, and return enough metadata for
# bot.py to render an <attached_file> prompt block.

# Extensions we refuse to land on disk regardless of admin status. The
# files never run from ATTACHMENT_DIR (no caller execs them), but the
# denylist is defense in depth — a model that emits a path back to a
# .sh / .exe shouldn't be able to launder one in.
_ARBITRARY_EXT_DENY = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bat",
        ".cmd",
        ".ps1",
        ".sh",
        ".bash",
        ".zsh",
        ".msi",
        ".app",
        ".dmg",
        ".deb",
        ".rpm",
    }
)

MAX_ARBITRARY_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB — Discord upload cap
MAX_ARBITRARY_ATTACHMENTS = 3


def _sanitize_basename(name: str) -> str:
    """Strip directory separators / control chars from a filename.

    The result is short, ASCII-safe-ish, and never contains '/' or '..'.
    Used for the user-visible basename only — the on-disk filename is
    prefixed with att.id so collisions are impossible.
    """
    return attachment_policy.sanitize_basename(name)


async def _download_arbitrary_one(
    session: aiohttp.ClientSession,
    att,
) -> dict | None:
    """Download one non-image, non-text attachment. Returns metadata dict."""
    return await attachment_downloader.download_arbitrary(
        session,
        att,
        attachment_dir=ATTACHMENT_DIR,
        max_bytes=MAX_ARBITRARY_ATTACHMENT_BYTES,
        denied_extensions=_ARBITRARY_EXT_DENY,
        semaphore=_download_semaphore,
        host_check=_host_is_allowed,
        sanitize_basename=_sanitize_basename,
    )


async def download_arbitrary_attachments(
    http_session: aiohttp.ClientSession,
    attachments: list,
    *,
    caller_is_admin: bool,
) -> list[dict]:
    """Save admin-only non-image, non-text attachments to ATTACHMENT_DIR.

    Returns a list of dicts: {path, filename, mime, size}. Non-admin
    callers get an empty list — letting arbitrary external users land
    binaries on disk has no upside for this bot and several downsides.
    """
    return await attachment_downloader.download_arbitraries(
        http_session,
        attachments,
        caller_is_admin=caller_is_admin,
        image_content_types=IMAGE_CONTENT_TYPES,
        text_content_types=TEXT_CONTENT_TYPES,
        max_attachments=MAX_ARBITRARY_ATTACHMENTS,
        download_one=_download_arbitrary_one,
    )


def cleanup_stale_attachments() -> None:
    """Remove temp files older than 1h from attachment + render directories.

    SRE-M1: extended to cover /tmp/{mermaid,table,math}_renders too — these
    are content-addressed PNGs/HTMLs that previously accumulated indefinitely.

    The 1-hour threshold (3600 s) is intentionally generous relative to the
    longest plausible Claude response latency (~60 s) plus Discord's CDN
    re-fetch window. Raising this value reduces the chance of a
    discord.File(path) raising FileNotFoundError between extract and send.
    If a FileNotFound still occurs in bot.py the caller logs it with errno
    via log.warning so it is diagnosable in the journal.
    """
    # Import lazily so monkeypatch in tests can override config.MERMAID_OUTPUT_DIR etc.
    import config as _cfg

    dirs_to_clean = [
        _cfg.ATTACHMENT_DIR,
        _cfg.MERMAID_OUTPUT_DIR,
        _cfg.TABLE_RENDERS_DIR,
        _cfg.MATH_RENDERS_DIR,
    ]
    attachment_store.cleanup_stale(dirs_to_clean, max_age_seconds=3600)
