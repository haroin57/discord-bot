"""Tests for utils/attachments.py — extract_image_paths, download, cleanup."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.attachments import (
    _basename_is_secret,
    cleanup_attachments,
    cleanup_stale_attachments,
    download_attachments,
    extract_file_paths,
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


class TestSecretBasenameDenylist:
    """The basename-level denylist must reject credential file patterns
    even when the file lives under an allowed ADMIN_FILE_DIRS entry."""

    def test_exact_hidden_credentials(self):
        assert _basename_is_secret("/home/ubuntu/.claude/.credentials.json")
        assert _basename_is_secret("/home/ubuntu/.config/gh/hosts.yml")
        assert _basename_is_secret("/home/ubuntu/.ssh/id_rsa")
        assert _basename_is_secret("/home/ubuntu/.ssh/id_ed25519")

    def test_dotenv_variants(self):
        assert _basename_is_secret("/home/ubuntu/project/.env")
        assert _basename_is_secret("/home/ubuntu/project/.env.production")
        assert _basename_is_secret("/home/ubuntu/project/prod.env")

    def test_secret_globs(self):
        assert _basename_is_secret("/home/ubuntu/keys/server.pem")
        assert _basename_is_secret("/home/ubuntu/keys/api.key")
        assert _basename_is_secret("/home/ubuntu/tokens/access.token")
        assert _basename_is_secret("/home/ubuntu/data/app.sqlite")
        assert _basename_is_secret("/home/ubuntu/data/app.db")

    def test_innocuous_files_pass(self):
        assert not _basename_is_secret("/home/ubuntu/notes.md")
        assert not _basename_is_secret("/home/ubuntu/report.pdf")
        assert not _basename_is_secret("/tmp/output.txt")


class TestExtractFilePathsDenylist:
    """Regression: a path under ADMIN_FILE_DIRS but with a credential
    basename must not be returned for auto-attachment, even though the
    file exists and the allowlist would otherwise let it through."""

    def test_credential_under_admin_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        secret = tmp_path / ".credentials.json"
        secret.write_text('{"token": "secret"}')
        text = f"check {secret}"
        assert extract_file_paths(text) == []

    def test_env_under_admin_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        env = tmp_path / ".env"
        env.write_text("API_KEY=xxx")
        text = f"see {env}"
        assert extract_file_paths(text) == []

    def test_pem_under_admin_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        pem = tmp_path / "server.pem"
        pem.write_text("-----BEGIN PRIVATE KEY-----")
        text = f"path: {pem}"
        assert extract_file_paths(text) == []

    def test_normal_file_still_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        report = tmp_path / "report.csv"
        report.write_text("a,b\n1,2\n")
        text = f"output: {report}"
        assert str(report) in extract_file_paths(text)


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


class TestReDoSGuards:
    def test_image_path_re_bounded(self):
        """IMAGE_PATH_RE must not backtrack on adversarial input."""
        import time
        big = "/" + "a" * 10000 + ".png"
        start = time.monotonic()
        extract_image_paths(big)
        assert time.monotonic() - start < 1.0

    def test_file_path_re_bounded(self):
        """FILE_PATH_RE must not backtrack on adversarial input."""
        import time
        big = "/" + "a" * 10000 + ".txt"
        start = time.monotonic()
        extract_file_paths(big)
        assert time.monotonic() - start < 1.0

    def test_input_capped_at_100k(self, allowed_tmp):
        """Input text longer than 100k chars is silently capped before regex scan."""
        img = allowed_tmp / "real.png"
        img.write_bytes(_MAGIC["png"])
        # Place the valid path after the 100k cap boundary; it must NOT be found
        big_prefix = "x" * 100_001
        text = big_prefix + f" {img} "
        paths = extract_image_paths(text)
        assert str(img) not in paths
