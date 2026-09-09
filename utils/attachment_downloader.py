"""Bounded, cancellation-safe attachment download orchestration."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import unicodedata
import warnings
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

import aiohttp
from PIL import Image, UnidentifiedImageError

from utils.image_validator import identify_image_mime
from utils.render_store import image_validation_lock, run_file_operation

log = logging.getLogger(__name__)

T = TypeVar("T")

_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_TEXT_MIMES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/html",
    "text/xml",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
}

# A validation receipt, not a payload cache. Each caller/provenance group owns
# a fresh set; asyncio children share that set only for the group's lifetime.
_validated_attachments: ContextVar[set[tuple] | None] = ContextVar(
    "validated_attachment_downloads", default=None,
)


@contextmanager
def attachment_reuse_scope():
    """Avoid re-downloading successfully validated media within one group."""
    token = _validated_attachments.set(set())
    try:
        yield
    finally:
        _validated_attachments.reset(token)


def _attachment_key(attachment) -> tuple | None:
    values = tuple(getattr(attachment, field, None) for field in
                   ("id", "url", "content_type", "filename", "size"))
    if not all(type(value) in (str, int, type(None)) for value in values):
        return None
    return (id(attachment), *values)


def _record_validated(attachment) -> None:
    receipts = _validated_attachments.get()
    if receipts is not None and (key := _attachment_key(attachment)) is not None:
        receipts.add(key)


def _already_validated(attachment) -> bool:
    receipts = _validated_attachments.get()
    return receipts is not None and _attachment_key(attachment) in receipts


def _declared_mime(attachment: Any) -> str:
    return (
        (getattr(attachment, "content_type", "") or "").split(";", 1)[0].strip().lower()
    )


def _dimensions_allowed(
    image: Image.Image,
    *,
    max_width: int,
    max_height: int,
    max_pixels: int,
) -> bool:
    width, height = image.size
    frames = max(1, int(getattr(image, "n_frames", 1)))
    return (
        1 <= width <= max_width
        and 1 <= height <= max_height
        and width * height * frames <= max_pixels
    )


def _fully_decoded_image(
    path: str,
    *,
    declared_mime: str,
    max_width: int = 8192,
    max_height: int = 8192,
    max_pixels: int = 16_777_216,
) -> bool:
    """Validate magic, declared MIME, structure, and complete pixel decode."""
    try:
        with open(path, "rb") as file:
            head = file.read(16)
        actual_mime = identify_image_mime(head)
        if actual_mime is None or actual_mime != declared_mime:
            return False
        with image_validation_lock, warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if not _dimensions_allowed(
                    image,
                    max_width=max_width,
                    max_height=max_height,
                    max_pixels=max_pixels,
                ):
                    return False
                image.verify()
            with Image.open(path) as image:
                if not _dimensions_allowed(
                    image,
                    max_width=max_width,
                    max_height=max_height,
                    max_pixels=max_pixels,
                ):
                    return False
                image.load()
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return False
    return True


def _decode_text(data: bytes) -> str | None:
    """Decode conservative text encodings and reject binary control data."""
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    for character in text:
        if character in "\n\r\t":
            continue
        if unicodedata.category(character) == "Cc":
            return None
    return text


async def gather_owned(awaitables: list[Coroutine[Any, Any, T]]) -> list[T]:
    """Run child operations and always cancel/await them with their owner."""
    tasks = [asyncio.create_task(awaitable) for awaitable in awaitables]
    if not tasks:
        return []
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def download_image(
    session: aiohttp.ClientSession,
    attachment,
    *,
    attachment_dir: str,
    max_bytes: int,
    semaphore: asyncio.Semaphore,
    host_check: Callable[[str], bool],
    max_width: int = 8192,
    max_height: int = 8192,
    max_pixels: int = 16_777_216,
) -> str | None:
    if attachment.size and attachment.size > max_bytes:
        log.warning(
            "Skipping oversized attachment %s (%d bytes)",
            attachment.filename,
            attachment.size,
        )
        return None
    if not host_check(attachment.url):
        log.warning("Rejecting non-allow-listed attachment URL: %s", attachment.url)
        return None

    extension = os.path.splitext(attachment.filename or "")[1].lower()
    if not extension:
        extension = ".png"
    elif extension not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        extension = ".bin"
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "", str(attachment.id))[:64] or "att"
    destination = os.path.realpath(
        os.path.join(attachment_dir, f"{safe_id}{extension}")
    )
    attachment_dir_real = os.path.realpath(attachment_dir)
    if not (
        destination == attachment_dir_real
        or destination.startswith(attachment_dir_real + os.sep)
    ):
        log.warning("Refusing attachment dest outside ATTACHMENT_DIR: %s", destination)
        return None

    part_path: str | None = None
    async with semaphore:
        try:
            async with session.get(attachment.url, allow_redirects=False) as response:
                if response.status != 200:
                    return None
                total = 0
                fd, part_path = tempfile.mkstemp(
                    prefix=f".{safe_id}.",
                    suffix=".part",
                    dir=attachment_dir_real,
                )
                with os.fdopen(fd, "wb") as file:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            log.warning(
                                "Aborting download of %s: exceeded %d bytes mid-stream",
                                attachment.filename,
                                max_bytes,
                            )
                            return None
                        await run_file_operation(file.write, chunk)
                declared_mime = _declared_mime(attachment)
                if not await run_file_operation(
                    _fully_decoded_image,
                    part_path,
                    declared_mime=declared_mime,
                    max_width=max_width,
                    max_height=max_height,
                    max_pixels=max_pixels,
                ):
                    log.warning(
                        "Rejecting image attachment with mismatched or invalid payload: %s",
                        attachment.filename,
                    )
                    return None
                os.replace(part_path, destination)
                part_path = None
                log.info(
                    "Downloaded attachment: %s -> %s", attachment.filename, destination
                )
                _record_validated(attachment)
                return destination
        except Exception as exc:
            log.warning(
                "Failed to download attachment %s: %s", attachment.filename, exc
            )
        finally:
            try:
                if part_path is not None:
                    os.remove(part_path)
            except OSError:
                pass
    return None


async def download_images(
    session: aiohttp.ClientSession,
    attachments: list,
    *,
    image_content_types: set[str],
    max_attachments: int,
    download_one: Callable[[aiohttp.ClientSession, object], Awaitable[str | None]],
) -> list[str]:
    image_attachments = [
        attachment
        for attachment in attachments
        if attachment.content_type
        and attachment.content_type.split(";")[0].strip() in image_content_types
    ]
    if len(image_attachments) > max_attachments:
        log.warning(
            "Capping image attachments %d -> %d (MAX_IMAGE_ATTACHMENTS)",
            len(image_attachments),
            max_attachments,
        )
        image_attachments = image_attachments[:max_attachments]
    results = await gather_owned(
        [download_one(session, attachment) for attachment in image_attachments]
    )
    return [path for path in results if path]


async def download_text(
    session: aiohttp.ClientSession,
    attachment,
    *,
    max_bytes: int,
    semaphore: asyncio.Semaphore,
    host_check: Callable[[str], bool],
) -> tuple[str, str] | None:
    size = attachment.size or 0
    if size > max_bytes:
        log.warning(
            "Skipping oversized text attachment %s (%d bytes)",
            attachment.filename,
            size,
        )
        return None
    if not host_check(attachment.url):
        log.warning(
            "Rejecting non-allow-listed text attachment URL: %s", attachment.url
        )
        return None
    async with semaphore:
        try:
            async with session.get(attachment.url, allow_redirects=False) as response:
                if response.status != 200:
                    return None
                chunks = []
                total = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        log.warning(
                            "Aborting text attachment %s: exceeded %d bytes mid-stream",
                            attachment.filename,
                            max_bytes,
                        )
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
                text = await run_file_operation(_decode_text, data)
                if text is None:
                    log.warning(
                        "Rejecting text attachment with invalid encoding/control bytes: %s",
                        attachment.filename,
                    )
                    return None
                log.info(
                    "Downloaded text attachment: %s (%d bytes)",
                    attachment.filename,
                    len(data),
                )
                _record_validated(attachment)
                return attachment.filename or "attachment.txt", text
        except Exception as exc:
            log.warning(
                "Failed to download text attachment %s: %s", attachment.filename, exc
            )
    return None


async def download_texts(
    session: aiohttp.ClientSession,
    attachments: list,
    *,
    text_content_types: set[str],
    max_attachments: int,
    download_one: Callable[
        [aiohttp.ClientSession, object], Awaitable[tuple[str, str] | None]
    ],
) -> list[tuple[str, str]]:
    text_attachments = [
        attachment
        for attachment in attachments
        if attachment.content_type
        and attachment.content_type.split(";")[0].strip().lower() in text_content_types
    ][:max_attachments]
    results = await gather_owned(
        [download_one(session, attachment) for attachment in text_attachments]
    )
    return [result for result in results if result]


async def download_arbitrary(
    session: aiohttp.ClientSession,
    attachment,
    *,
    attachment_dir: str,
    max_bytes: int,
    denied_extensions: frozenset[str],
    semaphore: asyncio.Semaphore,
    host_check: Callable[[str], bool],
    sanitize_basename: Callable[[str], str],
) -> dict | None:
    size_hint = attachment.size or 0
    if size_hint > max_bytes:
        log.warning(
            "Skipping oversized arbitrary attachment %s (%d bytes)",
            attachment.filename,
            size_hint,
        )
        return None
    if not host_check(attachment.url):
        log.warning(
            "Rejecting non-allow-listed arbitrary attachment URL: %s",
            attachment.url,
        )
        return None
    extension = os.path.splitext(attachment.filename or "")[1].lower()
    if extension in denied_extensions:
        log.warning(
            "Refusing denylisted extension for %s (ext=%s)",
            attachment.filename,
            extension,
        )
        return None
    if _already_validated(attachment):
        # The dedicated downloader already returned this exact validated
        # upload to the caller. Failed/oversized/mislabeled uploads have no
        # receipt and still run the existing binary-policy fallback below.
        return None
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "", str(attachment.id))[:64] or "att"
    safe_base = sanitize_basename(attachment.filename or "")
    destination = os.path.realpath(
        os.path.join(attachment_dir, f"{safe_id}-{safe_base}"),
    )
    attachment_dir_real = os.path.realpath(attachment_dir)
    if not (
        destination == attachment_dir_real
        or destination.startswith(attachment_dir_real + os.sep)
    ):
        log.warning(
            "Refusing arbitrary attachment dest outside ATTACHMENT_DIR: %s",
            destination,
        )
        return None

    part_path: str | None = None
    async with semaphore:
        try:
            async with session.get(attachment.url, allow_redirects=False) as response:
                if response.status != 200:
                    return None
                total = 0
                fd, part_path = tempfile.mkstemp(
                    prefix=f".{safe_id}.",
                    suffix=".part",
                    dir=attachment_dir_real,
                )
                with os.fdopen(fd, "wb") as file:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            log.warning(
                                "Aborting arbitrary download of %s: exceeded %d bytes",
                                attachment.filename,
                                max_bytes,
                            )
                            return None
                        await run_file_operation(file.write, chunk)
                declared_mime = _declared_mime(attachment)
                if declared_mime in _IMAGE_MIMES and await run_file_operation(
                    _fully_decoded_image,
                    part_path,
                    declared_mime=declared_mime,
                ):
                    # The dedicated image path already owns this valid payload.
                    return None
                if declared_mime in _TEXT_MIMES:
                    if await run_file_operation(_is_text_file, part_path):
                        # The dedicated text path already owns this valid payload.
                        return None
                os.replace(part_path, destination)
                part_path = None
                log.info(
                    "Downloaded arbitrary attachment: %s (%s, %d bytes) -> %s",
                    attachment.filename,
                    attachment.content_type or "?",
                    total,
                    destination,
                )
                return {
                    "path": destination,
                    "filename": attachment.filename or os.path.basename(destination),
                    "mime": attachment.content_type or "application/octet-stream",
                    "size": total,
                }
        except Exception as exc:
            log.warning(
                "Failed to download arbitrary attachment %s: %s",
                attachment.filename,
                exc,
            )
        finally:
            try:
                if part_path is not None:
                    os.remove(part_path)
            except OSError:
                pass
    return None


def _is_text_file(path: str) -> bool:
    with open(path, "rb") as file:
        return _decode_text(file.read()) is not None


async def download_arbitraries(
    session: aiohttp.ClientSession,
    attachments: list,
    *,
    caller_is_admin: bool,
    image_content_types: set[str],
    text_content_types: set[str],
    max_attachments: int,
    download_one: Callable[[aiohttp.ClientSession, object], Awaitable[dict | None]],
) -> list[dict]:
    if not caller_is_admin:
        return []
    # Inspect every admin upload through the binary-policy path. Valid declared
    # images/text are filtered by download_arbitrary after full validation;
    # MIME/magic/encoding mismatches are retained as untrusted binary metadata.
    candidates = list(attachments)
    if len(candidates) > max_attachments:
        log.warning(
            "Capping arbitrary attachments %d -> %d",
            len(candidates),
            max_attachments,
        )
        candidates = candidates[:max_attachments]
    results = await gather_owned(
        [download_one(session, attachment) for attachment in candidates]
    )
    return [result for result in results if result]
