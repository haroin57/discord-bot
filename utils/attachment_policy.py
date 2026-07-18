"""Pure attachment admission and response-path policy."""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from collections.abc import Callable, Iterable
from typing import Pattern
from urllib.parse import urlparse

from utils.image_validator import identify_image_mime

log = logging.getLogger(__name__)


def host_is_allowed(url: str, allowed_hosts: Iterable[str]) -> bool:
    """Return whether a URL is an HTTPS URL on the explicit host allow-list."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in allowed_hosts


def basename_is_secret(
    path: str,
    *,
    exact_names: frozenset[str],
    glob_patterns: tuple[str, ...],
) -> bool:
    base = os.path.basename(path)
    return base in exact_names or any(
        fnmatch.fnmatch(base, pattern) for pattern in glob_patterns
    )


def load_attachignore(
    path: str,
    *,
    cached_patterns: list[str] | None,
    cached_mtime: float,
) -> tuple[list[str], float]:
    """Load an attach-ignore file and return the refreshed cache state."""
    try:
        stat_result = os.stat(path)
    except OSError:
        return [], -1.0
    if cached_patterns is not None and stat_result.st_mtime == cached_mtime:
        return cached_patterns, cached_mtime
    patterns: list[str] = []
    try:
        with open(path) as file:
            for line in file:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    except OSError:
        pass
    return patterns, stat_result.st_mtime


def path_is_ignored(path: str, patterns: Iterable[str]) -> bool:
    real = os.path.realpath(path)
    for pattern in patterns:
        if fnmatch.fnmatch(real, pattern) or fnmatch.fnmatch(
            os.path.basename(real), pattern
        ):
            log.info("Skipping attach-ignored file: %s (pattern: %s)", path, pattern)
            return True
    return False


def has_image_magic(path: str) -> bool:
    try:
        with open(path, "rb") as file:
            head = file.read(12)
    except OSError:
        return False
    return identify_image_mime(head, allow_svg=True) is not None


def normalize_image_filenames(text: str, search_dirs: Iterable[str]) -> str:
    bare_pattern = re.compile(
        r"(?<![/\w])(\b[\w._-]+\.(?:png|jpg|jpeg|gif|webp|svg))\b",
    )
    replacements: dict[str, str] = {}
    for match in bare_pattern.finditer(text):
        filename = match.group(1)
        if filename in replacements:
            continue
        if match.start() > 0 and text[match.start() - 1] == "/":
            continue
        candidates = [
            os.path.join(directory, filename)
            for directory in search_dirs
            if os.path.isfile(os.path.join(directory, filename))
        ]
        if len(candidates) == 1:
            replacements[filename] = candidates[0]
            log.info("Normalized bare filename %s -> %s", filename, candidates[0])
    for filename, full_path in replacements.items():
        text = text.replace(filename, full_path)
    return text


def extract_image_paths(
    text: str,
    *,
    admin: bool,
    allowed_dirs: Iterable[str],
    admin_dirs: Iterable[str],
    max_paths: int,
    path_pattern: Pattern[str],
    quoted_pattern: Pattern[str],
    image_validator: Callable[[str], bool] = has_image_magic,
) -> list[str]:
    text = text[:100_000]
    directories = admin_dirs if admin else allowed_dirs
    allowed_real = tuple(os.path.realpath(directory) for directory in directories)
    raw_candidates = [match.group(1) for match in path_pattern.finditer(text)]
    for match in quoted_pattern.finditer(text):
        raw_candidates.append(
            next(group for group in match.groups() if group is not None)
        )

    if not raw_candidates:
        log.debug(
            "extract_image_paths: no regex candidates found in text (len=%d)", len(text)
        )

    paths: list[str] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        if len(paths) >= max_paths:
            break
        if raw in seen:
            continue
        seen.add(raw)
        try:
            real = os.path.realpath(raw)
        except (OSError, ValueError):
            log.debug("extract_image_paths: realpath failed for candidate: %r", raw)
            continue
        if not any(
            real == directory or real.startswith(directory + os.sep)
            for directory in allowed_real
        ):
            log.warning("Rejecting image path outside allow-list: %s", raw)
            continue
        if not os.path.isfile(real):
            log.debug("extract_image_paths: candidate does not exist on disk: %s", real)
            continue
        if not image_validator(real):
            log.warning("Rejecting path with non-image content: %s", raw)
            continue
        paths.append(real)
    return paths


def strip_attached_paths(text: str, paths: Iterable[str]) -> str:
    for path in paths:
        basename = os.path.basename(path)
        text = re.sub(r"\[attach:" + re.escape(path) + r"\]", "", text)
        text = re.sub(r"\[Attachment:\s*" + re.escape(basename) + r"\]", "", text)
        text = re.sub(r"(?m)^[ \t]*" + re.escape(path) + r"[ \t]*\n?", "", text)
        text = text.replace(path, "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_file_paths(
    text: str,
    *,
    allowed_dirs: Iterable[str],
    max_paths: int,
    max_bytes: int,
    path_pattern: Pattern[str],
    quoted_pattern: Pattern[str],
    secret_check: Callable[[str], bool],
    ignore_check: Callable[[str], bool],
) -> list[str]:
    text = text[:100_000]
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
    allowed_real = tuple(os.path.realpath(directory) for directory in allowed_dirs)
    raw_candidates = [match.group(1) for match in path_pattern.finditer(text)]
    for match in quoted_pattern.finditer(text):
        raw_candidates.append(
            next(group for group in match.groups() if group is not None)
        )

    if not raw_candidates:
        log.debug(
            "extract_file_paths: no regex candidates found in text (len=%d)", len(text)
        )

    paths: list[str] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        if len(paths) >= max_paths:
            break
        if raw in seen:
            continue
        seen.add(raw)
        if os.path.splitext(raw)[1].lower() in image_extensions:
            continue
        try:
            real = os.path.realpath(raw)
        except (OSError, ValueError):
            log.debug("extract_file_paths: realpath failed for candidate: %r", raw)
            continue
        if not any(
            real == directory or real.startswith(directory + os.sep)
            for directory in allowed_real
        ):
            log.warning("Rejecting file path outside admin allow-list: %s", raw)
            continue
        if secret_check(real):
            log.warning("Rejecting secret-basename file: %s", raw)
            continue
        if ignore_check(real) or not os.path.isfile(real):
            continue
        try:
            size = os.path.getsize(real)
        except OSError:
            continue
        if size > max_bytes:
            log.warning(
                "Rejecting file too large for Discord: %s (%d bytes)", raw, size
            )
            continue
        if size:
            paths.append(real)
    return paths


def sanitize_basename(name: str) -> str:
    base = os.path.basename(name or "").strip()
    base = re.sub(r"[^\w.\-()\[\] ]", "_", base, flags=re.UNICODE)
    return base[:80] or "attachment"
