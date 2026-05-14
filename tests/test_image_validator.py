"""Tests for utils.image_validator."""

from __future__ import annotations

import pytest

from utils.image_validator import (
    DEFAULT_IMAGE_MAX_BYTES,
    identify_image_mime,
    validate_image_bytes,
)


# ---------------------------------------------------------------------------
# identify_image_mime
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("header,expected", [
    (b"\x89PNG\r\n\x1a\nrest_of_file", "image/png"),
    (b"\xff\xd8\xff\xe0junk", "image/jpeg"),
    (b"GIF87a stuff", "image/gif"),
    (b"GIF89a stuff", "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBPmore", "image/webp"),
])
def test_recognises_raster_magics(header, expected):
    assert identify_image_mime(header) == expected


def test_rejects_unknown_magic():
    assert identify_image_mime(b"hello world this is text") is None


def test_riff_without_webp_is_rejected():
    """RIFF prefix alone is also used by .wav etc — require WEBP at offset 8."""
    assert identify_image_mime(b"RIFF\x00\x00\x00\x00WAVE_other") is None


def test_riff_too_short_to_check_webp():
    assert identify_image_mime(b"RIFFshort") is None


def test_svg_rejected_without_allow_flag():
    assert identify_image_mime(b"<svg xmlns=...") is None
    assert identify_image_mime(b"<?xml version='1.0'?><svg...") is None


def test_svg_accepted_with_allow_flag():
    assert identify_image_mime(b"<svg xmlns=...", allow_svg=True) == "image/svg+xml"
    assert identify_image_mime(b"<?xml version='1.0'?><svg...", allow_svg=True) == "image/svg+xml"


# ---------------------------------------------------------------------------
# validate_image_bytes
# ---------------------------------------------------------------------------

def test_validate_accepts_png():
    mime, err = validate_image_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    assert mime == "image/png"
    assert err == ""


def test_validate_rejects_oversize():
    huge = b"\x89PNG\r\n\x1a\n" + b"x" * (DEFAULT_IMAGE_MAX_BYTES + 1)
    mime, err = validate_image_bytes(huge)
    assert mime is None
    assert "oversized" in err


def test_validate_rejects_oversize_with_custom_cap():
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 200
    mime, err = validate_image_bytes(data, max_bytes=100)
    assert mime is None
    assert "oversized" in err


def test_validate_rejects_unknown_magic():
    mime, err = validate_image_bytes(b"not an image")
    assert mime is None
    assert "unrecognized" in err


def test_validate_svg_only_with_flag():
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    mime, err = validate_image_bytes(svg)
    assert mime is None
    mime, err = validate_image_bytes(svg, allow_svg=True)
    assert mime == "image/svg+xml"
    assert err == ""
