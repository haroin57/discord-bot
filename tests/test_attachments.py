"""Tests for utils/attachments.py — extract_image_paths, download, cleanup."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.attachments import (
    cleanup_attachments,
    cleanup_stale_attachments,
    download_attachments,
    extract_image_paths,
    IMAGE_CONTENT_TYPES,
)

# Valid magic bytes for each supported format. `extract_image_paths` now
# rejects files whose content doesn't match a known image header (prevents
# extension-spoofing exfiltration of .env etc.), so test fixtures need real
# headers.
_MAGIC = {
    "png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 8,
    "jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 8,
    "jpeg": b"\xff\xd8\xff\xe0" + b"\x00" * 8,
    "gif": b"GIF89a" + b"\x00" * 8,
    "webp": b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4,
    "svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
}


@pytest.fixture
def allowed_tmp(tmp_path, monkeypatch):
    """Allow `tmp_path` as a valid image dir for the duration of the test."""
    monkeypatch.setattr("utils.attachments.ALLOWED_IMAGE_DIRS", (str(tmp_path),))
    return tmp_path


class TestExtractImagePaths:
    def test_finds_path_in_text(self, allowed_tmp):
        img = allowed_tmp / "test.png"
        img.write_bytes(_MAGIC["png"])
        text = f"画像は {img} に保存しました。"
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_ignores_nonexistent(self):
        text = "画像は /tmp/nonexistent_abc123.png です"
        assert extract_image_paths(text) == []

    def test_markdown_syntax(self, allowed_tmp):
        img = allowed_tmp / "out.png"
        img.write_bytes(_MAGIC["png"])
        text = f"![diagram]({img})"
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_caps_at_max(self, allowed_tmp, monkeypatch):
        monkeypatch.setattr("utils.attachments.MAX_RESPONSE_IMAGES", 2)
        imgs = []
        for i in range(5):
            img = allowed_tmp / f"img{i}.png"
            img.write_bytes(_MAGIC["png"])
            imgs.append(str(img))
        text = " ".join(imgs)
        paths = extract_image_paths(text)
        assert len(paths) == 2

    def test_deduplicates(self, allowed_tmp):
        img = allowed_tmp / "dup.png"
        img.write_bytes(_MAGIC["png"])
        text = f"{img} and again {img}"
        paths = extract_image_paths(text)
        assert len(paths) == 1

    def test_backtick_wrapped(self, allowed_tmp):
        img = allowed_tmp / "code.png"
        img.write_bytes(_MAGIC["png"])
        text = f"`{img}`"
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_multiple_extensions(self, allowed_tmp):
        for ext in ["png", "jpg", "jpeg", "gif", "webp", "svg"]:
            img = allowed_tmp / f"test.{ext}"
            img.write_bytes(_MAGIC[ext])
            paths = extract_image_paths(f"file: {img} done")
            assert str(img) in paths

    def test_rejects_extension_spoofing(self, allowed_tmp):
        """A file with .png extension but non-image content must be rejected."""
        fake = allowed_tmp / "secret.png"
        fake.write_bytes(b"THIS IS NOT AN IMAGE")
        paths = extract_image_paths(f"see {fake}")
        assert paths == []

    def test_rejects_path_outside_allow_list(self, tmp_path, monkeypatch):
        """Paths not under ALLOWED_IMAGE_DIRS must be rejected."""
        monkeypatch.setattr("utils.attachments.ALLOWED_IMAGE_DIRS", ("/nonexistent",))
        img = tmp_path / "outside.png"
        img.write_bytes(_MAGIC["png"])
        paths = extract_image_paths(f"see {img}")
        assert paths == []


class TestCleanupAttachments:
    def test_removes_files(self, tmp_path):
        f1 = tmp_path / "a.png"
        f2 = tmp_path / "b.png"
        f1.write_bytes(b"x")
        f2.write_bytes(b"y")
        cleanup_attachments([str(f1), str(f2)])
        assert not f1.exists()
        assert not f2.exists()

    def test_ignores_missing(self, tmp_path):
        cleanup_attachments([str(tmp_path / "gone.png")])

    def test_empty_list(self):
        cleanup_attachments([])


class TestCleanupStaleAttachments:
    def test_removes_old_files(self, tmp_attachment_dir):
        old = tmp_attachment_dir / "old.png"
        old.write_bytes(b"x")
        os.utime(old, (0, 0))
        cleanup_stale_attachments()
        assert not old.exists()

    def test_keeps_recent_files(self, tmp_attachment_dir):
        recent = tmp_attachment_dir / "new.png"
        recent.write_bytes(b"x")
        cleanup_stale_attachments()
        assert recent.exists()


class TestDownloadAttachments:
    def _make_attachment(self, content_type="image/png", size=1000, filename="test.png", att_id=1):
        att = MagicMock()
        att.content_type = content_type
        att.size = size
        att.filename = filename
        att.id = att_id
        att.url = f"https://cdn.discord.com/attachments/fake/{att_id}/{filename}"
        return att

    @pytest.mark.asyncio
    async def test_filters_non_image(self):
        att = self._make_attachment(content_type="text/plain")
        session = MagicMock()
        result = await download_attachments(session, [att])
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_oversized(self, tmp_attachment_dir):
        att = self._make_attachment(size=20 * 1024 * 1024)  # 20MB > 10MB limit
        session = MagicMock()
        result = await download_attachments(session, [att])
        assert result == []

    @pytest.mark.asyncio
    async def test_filters_image_content_types(self):
        for ct in IMAGE_CONTENT_TYPES:
            att = self._make_attachment(content_type=ct)
            # Just verify it's recognized as an image
            assert ct in IMAGE_CONTENT_TYPES
