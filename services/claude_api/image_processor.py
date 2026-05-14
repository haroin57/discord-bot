"""Image processing for the Anthropic Messages API.

Parses the `<attached_images>` block emitted by bot.py into base64
ImageBlocks suitable for the API's `content` array. Validates each
image with `utils.image_validator` (magic byte + size) and resolves
paths against `ATTACHMENT_DIR` to prevent directory traversal.

Pulled out of `services/claude_api/entry.py` so the call-site there
stays focused on the orchestrator + retry logic.
"""

from __future__ import annotations

import base64
import logging
import os
import re

from config import ATTACHMENT_DIR
from utils.image_validator import validate_image_bytes

__all__ = [
    "IMAGE_MAX_BYTES",
    "IMAGE_MAX_COUNT",
    "extract_attached_image_blocks",
    "safe_image_path",
]

log = logging.getLogger(__name__)

IMAGE_MAX_BYTES = 8 * 1024 * 1024  # 8 MB. base64 inflates ~1.33x → ~10.7 MB
# per image × IMAGE_MAX_COUNT ≈ 85 MB peak string payload. Per-message hard
# cap so a malicious or runaway parser doesn't OOM the 2 GB host.
IMAGE_MAX_COUNT = 8

# <attached_images>\n- /path (Use the Read tool to view this image)\n- /path\n</attached_images>
_ATTACHED_IMAGES_BLOCK_RE = re.compile(
    r"<attached_images>\s*(?P<body>.*?)\s*</attached_images>",
    re.DOTALL,
)


def safe_image_path(path: str) -> str | None:
    """Validate that ``path`` is under ATTACHMENT_DIR, not a symlink escape,
    and small enough. Returns the canonical realpath or None."""
    try:
        real_root = os.path.realpath(ATTACHMENT_DIR)
        candidate = os.path.realpath(path)
        if candidate != real_root and not candidate.startswith(real_root + os.sep):
            log.warning("image rejected: outside ATTACHMENT_DIR: %s", path)
            return None
        st = os.stat(candidate)
        if st.st_size > IMAGE_MAX_BYTES:
            log.warning("image rejected: too large (%d bytes): %s", st.st_size, path)
            return None
        return candidate
    except OSError as exc:
        log.warning("image rejected: %s (%s)", path, exc)
        return None


def extract_attached_image_blocks(prompt: str) -> tuple[str, list[dict]]:
    """Strip the last ``<attached_images>`` block and return base64 ImageBlocks.

    Only the *last* occurrence is treated as bot-generated, so a user
    typing the literal tag in their message is not parsed. The block is
    removed from the prompt entirely after extraction so the Read-tool
    hint inside it doesn't fool the model.
    """
    match = None
    # rfind via iteration — re has no rsearch helper.
    for m in _ATTACHED_IMAGES_BLOCK_RE.finditer(prompt):
        match = m
    if match is None:
        return prompt, []

    body = match.group("body")
    paths: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        token = line.lstrip("-").strip().split(" ", 1)[0]
        if token:
            paths.append(token)

    blocks: list[dict] = []
    for path in paths[:IMAGE_MAX_COUNT]:
        real = safe_image_path(path)
        if real is None:
            continue
        try:
            with open(real, "rb") as fh:
                data = fh.read(IMAGE_MAX_BYTES + 1)
        except OSError as exc:
            log.warning("image rejected: %s (%s)", real, exc)
            continue
        mime, err = validate_image_bytes(data, max_bytes=IMAGE_MAX_BYTES)
        if mime is None:
            log.warning("image rejected: %s (%s)", real, err)
            continue
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": base64.b64encode(data).decode("ascii"),
            },
        })

    stripped = prompt[: match.start()] + prompt[match.end():]
    return stripped.strip(), blocks
