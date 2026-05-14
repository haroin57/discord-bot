"""Image bytes validation — magic byte + MIME detection + size cap.

Centralises the magic-byte checks that previously lived inline in
`services/claude_api/entry.py` (for outbound API images) and
`utils/attachments.py` (for inbound Discord attachments / admin paths).
Both call sites have slightly different needs:

- The Anthropic API only accepts raster images (PNG / JPEG / GIF / WebP).
- Discord attachments may also include SVG, which is valid for users to
  see but cannot round-trip through `image` content blocks.

`allow_svg` toggles between the two modes.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_IMAGE_MAX_BYTES",
    "identify_image_mime",
    "validate_image_bytes",
]

DEFAULT_IMAGE_MAX_BYTES = 8 * 1024 * 1024  # 8 MB


def identify_image_mime(data: bytes, *, allow_svg: bool = False) -> str | None:
    """Return the MIME type if `data`'s first bytes match a known image format.

    WebP requires a special check because RIFF is also used by .wav etc;
    we verify the "WEBP" tag at offset 8 to disambiguate.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if allow_svg and (data.startswith(b"<svg") or data.startswith(b"<?xml")):
        return "image/svg+xml"
    return None


def validate_image_bytes(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_IMAGE_MAX_BYTES,
    allow_svg: bool = False,
) -> tuple[str | None, str]:
    """Validate `data` as an image. Returns ``(mime, error_msg)``.

    On success: ``(mime, "")``. On rejection: ``(None, reason)`` so the
    caller can log the specific failure without re-running the checks.
    """
    if len(data) > max_bytes:
        return None, f"oversized: {len(data)} > {max_bytes} bytes"
    mime = identify_image_mime(data, allow_svg=allow_svg)
    if mime is None:
        return None, "unrecognized image magic"
    return mime, ""
