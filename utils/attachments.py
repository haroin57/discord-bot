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
from utils.image_validator import identify_image_mime

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
    r"(?:^|\s|`|\()(\/[\w./_-]{1,512}\.(?:png|jpg|jpeg|gif|webp|svg))(?:\s|$|`|[)}\]]|[。、,.])",
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
    r"(?:^|\s|`|\()(\/[\w./_-]{1,512}\.[\w]{1,16})(?:\s|$|`|[)}\]]|[。、,.])",
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
_SECRET_BASENAME_EXACT = frozenset({
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
})
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
    base = os.path.basename(path)
    if base in _SECRET_BASENAME_EXACT:
        return True
    return any(fnmatch.fnmatch(base, pat) for pat in _SECRET_BASENAME_GLOBS)

# Path to .attachignore file (glob patterns, one per line, # comments)
_ATTACHIGNORE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".attachignore")

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
    try:
        st = os.stat(_ATTACHIGNORE_PATH)
    except OSError:
        _attachignore_cache = []
        _attachignore_mtime = -1.0
        return _attachignore_cache
    if _attachignore_cache is not None and st.st_mtime == _attachignore_mtime:
        return _attachignore_cache
    patterns: list[str] = []
    try:
        with open(_ATTACHIGNORE_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    except OSError:
        pass
    _attachignore_cache = patterns
    _attachignore_mtime = st.st_mtime
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
    return identify_image_mime(head, allow_svg=True) is not None


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
    text = text[:100_000]
    dirs = ADMIN_IMAGE_DIRS if admin else ALLOWED_IMAGE_DIRS
    allowed_real = tuple(os.path.realpath(d) for d in dirs)
    paths: list[str] = []
    seen: set[str] = set()

    # Collect raw candidates from both the standard pattern and the
    # quoted/enclosed supplementary pattern (handles spaces + parens in paths).
    raw_candidates: list[str] = []
    for m in IMAGE_PATH_RE.finditer(text):
        raw_candidates.append(m.group(1))
    for m in _IMAGE_PATH_QUOTED_RE.finditer(text):
        # The quoted RE has 4 groups; exactly one will be non-None per match.
        raw = next(g for g in m.groups() if g is not None)
        raw_candidates.append(raw)

    if not raw_candidates:
        log.debug("extract_image_paths: no regex candidates found in text (len=%d)", len(text))

    for raw in raw_candidates:
        if len(paths) >= MAX_RESPONSE_IMAGES:
            break
        if raw in seen:
            continue
        seen.add(raw)
        try:
            real = os.path.realpath(raw)
        except (OSError, ValueError):
            log.debug("extract_image_paths: realpath failed for candidate: %r", raw)
            continue
        if not any(real == d or real.startswith(d + os.sep) for d in allowed_real):
            log.warning("Rejecting image path outside allow-list: %s", raw)
            continue
        if not os.path.isfile(real):
            log.debug("extract_image_paths: candidate does not exist on disk: %s", real)
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
    text = text[:100_000]
    image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
    allowed_real = tuple(os.path.realpath(d) for d in ADMIN_FILE_DIRS)
    paths: list[str] = []
    seen: set[str] = set()

    # Collect raw candidates from both the standard pattern and the
    # quoted/enclosed supplementary pattern (handles spaces + parens in paths).
    raw_candidates: list[str] = []
    for m in FILE_PATH_RE.finditer(text):
        raw_candidates.append(m.group(1))
    for m in _FILE_PATH_QUOTED_RE.finditer(text):
        raw = next(g for g in m.groups() if g is not None)
        raw_candidates.append(raw)

    if not raw_candidates:
        log.debug("extract_file_paths: no regex candidates found in text (len=%d)", len(text))

    for raw in raw_candidates:
        if len(paths) >= MAX_FILE_ATTACHMENTS:
            break
        if raw in seen:
            continue
        seen.add(raw)
        ext = os.path.splitext(raw)[1].lower()
        if ext in image_exts:
            continue  # handled by extract_image_paths
        try:
            real = os.path.realpath(raw)
        except (OSError, ValueError):
            log.debug("extract_file_paths: realpath failed for candidate: %r", raw)
            continue
        if not any(real == d or real.startswith(d + os.sep) for d in allowed_real):
            log.warning("Rejecting file path outside admin allow-list: %s", raw)
            continue
        if _basename_is_secret(real):
            log.warning("Rejecting secret-basename file: %s", raw)
            continue
        if is_attach_ignored(real):
            continue
        if not os.path.isfile(real):
            log.debug("extract_file_paths: candidate does not exist on disk: %s", real)
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


def attachment_metadata_by_path(attachments: list, paths: list[str]) -> dict[str, dict]:
    """Recover Discord attachment metadata for downloaded local image paths.

    ``_download_one`` deliberately names each file ``<attachment_id>.<ext>``.
    This helper keeps the long-standing ``download_attachments -> list[str]``
    contract while making the Discord snowflake and original filename explicit
    for durable archives.
    """
    by_id: dict[str, object] = {}
    for att in attachments:
        attachment_id = getattr(att, "id", None)
        if attachment_id is None:
            continue
        safe_id = re.sub(r"[^0-9A-Za-z_-]", "", str(attachment_id))[:64]
        if safe_id:
            by_id[safe_id] = att
    result: dict[str, dict] = {}
    for path in paths:
        local_id = os.path.splitext(os.path.basename(path))[0]
        att = by_id.get(local_id)
        if att is None:
            continue
        result[path] = {
            "attachment_id": str(getattr(att, "id")),
            "original_name": getattr(att, "filename", None) or os.path.basename(path),
            "content_type": getattr(att, "content_type", None),
            "attachment_size": getattr(att, "size", None),
        }
    return result


def _cleanup_attachments_sync(paths: list[str]) -> None:
    """Synchronous helper for cleanup_attachments; runs in a worker thread."""
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


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
    import asyncio as _asyncio
    if not paths:
        return
    await _asyncio.to_thread(_cleanup_attachments_sync, paths)


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
_ARBITRARY_EXT_DENY = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".ps1",
    ".sh", ".bash", ".zsh", ".msi", ".app", ".dmg", ".deb", ".rpm",
})

MAX_ARBITRARY_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB — Discord upload cap
MAX_ARBITRARY_ATTACHMENTS = 3


def _sanitize_basename(name: str) -> str:
    """Strip directory separators / control chars from a filename.

    The result is short, ASCII-safe-ish, and never contains '/' or '..'.
    Used for the user-visible basename only — the on-disk filename is
    prefixed with att.id so collisions are impossible.
    """
    base = os.path.basename(name or "").strip()
    # Drop anything that isn't a portable filename character. Keep dots,
    # dashes, underscores, alphanumerics, parens, brackets; replace the
    # rest with '_'. Trim to 80 chars so the full on-disk path stays
    # comfortably below PATH_MAX.
    base = re.sub(r"[^\w.\-()\[\] ]", "_", base, flags=re.UNICODE)
    return base[:80] or "attachment"


async def _download_arbitrary_one(
    session: aiohttp.ClientSession, att,
) -> dict | None:
    """Download one non-image, non-text attachment. Returns metadata dict."""
    size_hint = att.size or 0
    if size_hint > MAX_ARBITRARY_ATTACHMENT_BYTES:
        log.warning(
            "Skipping oversized arbitrary attachment %s (%d bytes)",
            att.filename, size_hint,
        )
        return None
    if not _host_is_allowed(att.url):
        log.warning(
            "Rejecting non-allow-listed arbitrary attachment URL: %s", att.url,
        )
        return None
    ext = os.path.splitext(att.filename or "")[1].lower()
    if ext in _ARBITRARY_EXT_DENY:
        log.warning(
            "Refusing denylisted extension for %s (ext=%s)",
            att.filename, ext,
        )
        return None
    if not ext or len(ext) > 16:
        ext = ".bin"
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "", str(att.id))[:64] or "att"
    safe_base = _sanitize_basename(att.filename or "")
    dest = os.path.realpath(
        os.path.join(ATTACHMENT_DIR, f"{safe_id}-{safe_base}"),
    )
    att_dir_real = os.path.realpath(ATTACHMENT_DIR)
    if not (dest == att_dir_real or dest.startswith(att_dir_real + os.sep)):
        log.warning(
            "Refusing arbitrary attachment dest outside ATTACHMENT_DIR: %s",
            dest,
        )
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
                        if total > MAX_ARBITRARY_ATTACHMENT_BYTES:
                            log.warning(
                                "Aborting arbitrary download of %s: exceeded %d bytes",
                                att.filename, MAX_ARBITRARY_ATTACHMENT_BYTES,
                            )
                            f.close()
                            try:
                                os.remove(dest)
                            except OSError:
                                pass
                            return None
                        f.write(chunk)
                log.info(
                    "Downloaded arbitrary attachment: %s (%s, %d bytes) -> %s",
                    att.filename, att.content_type or "?", total, dest,
                )
                return {
                    "path": dest,
                    "filename": att.filename or os.path.basename(dest),
                    "mime": (att.content_type or "application/octet-stream"),
                    "size": total,
                }
        except Exception as e:
            log.warning(
                "Failed to download arbitrary attachment %s: %s",
                att.filename, e,
            )
            try:
                if os.path.isfile(dest):
                    os.remove(dest)
            except OSError:
                pass
    return None


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
    if not caller_is_admin or not attachments:
        return []
    candidates = []
    for a in attachments:
        ct = (a.content_type or "").split(";")[0].strip().lower()
        if ct in IMAGE_CONTENT_TYPES or ct in TEXT_CONTENT_TYPES:
            continue
        candidates.append(a)
    if not candidates:
        return []
    if len(candidates) > MAX_ARBITRARY_ATTACHMENTS:
        log.warning(
            "Capping arbitrary attachments %d -> %d",
            len(candidates), MAX_ARBITRARY_ATTACHMENTS,
        )
        candidates = candidates[:MAX_ARBITRARY_ATTACHMENTS]
    results = await asyncio.gather(
        *[_download_arbitrary_one(http_session, a) for a in candidates],
    )
    return [r for r in results if r]


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
    now = time.time()
    # 1 hour — must be >> longest response round-trip to avoid racing with
    # an in-flight discord.File open in bot.py. Do not reduce below 600 s.
    threshold = 3600
    for d in dirs_to_clean:
        if not d or not os.path.isdir(d):
            continue
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for fname in entries:
            fpath = os.path.join(d, fname)
            try:
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > threshold:
                    os.remove(fpath)
                    log.info("Cleaned up stale file: %s", fpath)
            except FileNotFoundError:
                # Race: another coroutine (or a previous cleanup run) already
                # removed the file between os.path.isfile() and os.remove().
                log.debug("cleanup_stale_attachments: already gone: %s", fpath)
            except OSError as exc:
                log.warning(
                    "cleanup_stale_attachments: failed to remove %s: [errno %s] %s",
                    fpath, exc.errno, exc,
                )
