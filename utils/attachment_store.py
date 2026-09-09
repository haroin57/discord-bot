"""Filesystem persistence boundary for downloaded attachments."""

from __future__ import annotations

import os
import re

from utils.render_store import cleanup_stale_files, run_file_operation


def write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as file:
        file.write(data)


def attachment_metadata_by_path(attachments: list, paths: list[str]) -> dict[str, dict]:
    by_id: dict[str, object] = {}
    for attachment in attachments:
        attachment_id = getattr(attachment, "id", None)
        if attachment_id is None:
            continue
        safe_id = re.sub(r"[^0-9A-Za-z_-]", "", str(attachment_id))[:64]
        if safe_id:
            by_id[safe_id] = attachment

    result: dict[str, dict] = {}
    for path in paths:
        local_id = os.path.splitext(os.path.basename(path))[0]
        attachment = by_id.get(local_id)
        if attachment is None:
            continue
        result[path] = {
            "attachment_id": str(getattr(attachment, "id")),
            "original_name": getattr(attachment, "filename", None)
            or os.path.basename(path),
            "content_type": getattr(attachment, "content_type", None),
            "attachment_size": getattr(attachment, "size", None),
        }
    return result


def cleanup(paths: list[str]) -> None:
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


async def cleanup_async(paths: list[str]) -> None:
    if paths:
        await run_file_operation(cleanup, paths)


def cleanup_stale(directories: list[str], *, max_age_seconds: float = 3600) -> None:
    cleanup_stale_files(directories, max_age_seconds=max_age_seconds)
