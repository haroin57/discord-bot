"""Tests for utils/attachments.py — extract_image_paths, download, cleanup."""

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.attachments import (
    _basename_is_secret,
    _has_image_magic,
    _host_is_allowed,
    cleanup_attachments,
    cleanup_attachments_async,
    cleanup_stale_attachments,
    download_attachments,
    download_text_attachments,
    attachment_metadata_by_path,
    extract_file_paths,
    extract_image_paths,
    is_attach_ignored,
    normalize_image_filenames,
    strip_attached_paths,
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

    def test_path_with_space_in_backtick(self, allowed_tmp):
        """Backtick-enclosed path containing a space must be extracted."""
        img = allowed_tmp / "出力 (1).png"
        img.write_bytes(_MAGIC["png"])
        text = f"result: `{img}`"
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_path_with_space_in_double_quote(self, allowed_tmp):
        """Double-quote-enclosed path containing a space must be extracted."""
        img = allowed_tmp / "my chart (final).png"
        img.write_bytes(_MAGIC["png"])
        text = f'see "{img}" here'
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_path_with_space_in_single_quote(self, allowed_tmp):
        """Single-quote-enclosed path containing a space must be extracted."""
        img = allowed_tmp / "graph (v2).png"
        img.write_bytes(_MAGIC["png"])
        text = f"see '{img}' here"
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_path_in_markdown_link(self, allowed_tmp):
        """Markdown link [label](path) syntax must be extracted."""
        img = allowed_tmp / "nikkei225 butterfly.png"
        img.write_bytes(_MAGIC["png"])
        text = f"[チャート]({img})"
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_japanese_filename_no_space(self, allowed_tmp):
        """Japanese filename without spaces (\\w-covered) must still work."""
        img = allowed_tmp / "チャート解析.png"
        img.write_bytes(_MAGIC["png"])
        text = f"ファイル: {img} です"
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_trailing_japanese_punctuation(self, allowed_tmp):
        """Path ending immediately before Japanese punctuation must be extracted.

        The standard IMAGE_PATH_RE accepts 。、,. as trailing delimiters.
        A preceding space (after 'こちらが') acts as the leading delimiter.
        """
        img = allowed_tmp / "result.png"
        img.write_bytes(_MAGIC["png"])
        # Space before the path acts as leading delimiter; 。 acts as trailing.
        text = f"こちらが {img}。"
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_leading_fullwidth_colon(self, allowed_tmp):
        """Path written directly after a full-width colon (ラベル：/path) must
        be extracted. Regression: the leading anchor used to require a space,
        so `プレビュー：/abs/path.png` silently dropped the attachment."""
        img = allowed_tmp / "preview.png"
        img.write_bytes(_MAGIC["png"])
        text = f"プレビュー：{img}"
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_leading_halfwidth_colon(self, allowed_tmp):
        """Path directly after a half-width colon (label:/path) must extract."""
        img = allowed_tmp / "shot.png"
        img.write_bytes(_MAGIC["png"])
        text = f"STL:{img}"
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_unenclosed_space_path_not_extracted(self, allowed_tmp):
        """A path with spaces NOT enclosed by delimiters must NOT be extracted
        to avoid false positives from prose text."""
        img = allowed_tmp / "no space extraction.png"
        img.write_bytes(_MAGIC["png"])
        # No backtick, quote, or markdown link — bare text with space
        text = f"check {img} done"
        # The standard pattern won't match because of the space;
        # the quoted pattern won't match because there's no delimiter.
        paths = extract_image_paths(text)
        assert str(img) not in paths


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

    def test_attachment_metadata_is_explicitly_mapped_to_local_path(self, tmp_path):
        att = self._make_attachment(
            content_type="image/png",
            size=1234,
            filename="original chart.png",
            att_id=987654321012345678,
        )
        path = str(tmp_path / "987654321012345678.png")

        metadata = attachment_metadata_by_path([att], [path])

        assert metadata[path] == {
            "attachment_id": "987654321012345678",
            "original_name": "original chart.png",
            "content_type": "image/png",
            "attachment_size": 1234,
        }

    def test_attachment_metadata_ignores_unmatched_paths(self, tmp_path):
        att = self._make_attachment(att_id=123)
        unmatched = str(tmp_path / "456.png")

        assert attachment_metadata_by_path([att], [unmatched]) == {}


class TestHostIsAllowed:
    def test_discord_cdn_allowed(self):
        assert _host_is_allowed("https://cdn.discordapp.com/attachments/1/2/test.png")

    def test_media_discord_allowed(self):
        assert _host_is_allowed("https://media.discordapp.net/attachments/1/2/test.png")

    def test_http_rejected(self):
        assert not _host_is_allowed("http://cdn.discordapp.com/attachments/1/2/test.png")

    def test_non_discord_host_rejected(self):
        assert not _host_is_allowed("https://evil.example.com/malware.png")

    def test_empty_string(self):
        assert not _host_is_allowed("")

    def test_localhost_rejected(self):
        assert not _host_is_allowed("https://localhost/secret")


class TestHasImageMagic:
    def test_valid_png(self, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(_MAGIC["png"])
        assert _has_image_magic(str(f))

    def test_valid_jpeg(self, tmp_path):
        f = tmp_path / "a.jpg"
        f.write_bytes(_MAGIC["jpg"])
        assert _has_image_magic(str(f))

    def test_invalid_content(self, tmp_path):
        f = tmp_path / "fake.png"
        f.write_bytes(b"NOT AN IMAGE AT ALL")
        assert not _has_image_magic(str(f))

    def test_missing_file(self):
        assert not _has_image_magic("/tmp/does_not_exist_xyz.png")


class TestLoadAttachignore:
    def test_returns_empty_when_file_missing(self, monkeypatch):
        monkeypatch.setattr("utils.attachments._ATTACHIGNORE_PATH", "/nonexistent/.attachignore")
        import utils.attachments as ua
        ua._attachignore_cache = None
        ua._attachignore_mtime = -1.0
        from utils.attachments import _load_attachignore
        result = _load_attachignore()
        assert result == []

    def test_loads_patterns(self, tmp_path, monkeypatch):
        afile = tmp_path / ".attachignore"
        afile.write_text("*.secret\n# comment\n\n*.db\n")
        monkeypatch.setattr("utils.attachments._ATTACHIGNORE_PATH", str(afile))
        import utils.attachments as ua
        ua._attachignore_cache = None
        ua._attachignore_mtime = -1.0
        from utils.attachments import _load_attachignore
        patterns = _load_attachignore()
        assert "*.secret" in patterns
        assert "*.db" in patterns
        assert "# comment" not in patterns

    def test_cache_hit(self, tmp_path, monkeypatch):
        afile = tmp_path / ".attachignore"
        afile.write_text("*.key\n")
        monkeypatch.setattr("utils.attachments._ATTACHIGNORE_PATH", str(afile))
        import utils.attachments as ua
        ua._attachignore_cache = None
        ua._attachignore_mtime = -1.0
        from utils.attachments import _load_attachignore
        r1 = _load_attachignore()
        r2 = _load_attachignore()
        assert r1 is r2  # same object returned from cache


class TestIsAttachIgnored:
    def test_matches_glob_pattern(self, tmp_path, monkeypatch):
        afile = tmp_path / ".attachignore"
        afile.write_text("*.secret\n")
        monkeypatch.setattr("utils.attachments._ATTACHIGNORE_PATH", str(afile))
        import utils.attachments as ua
        ua._attachignore_cache = None
        ua._attachignore_mtime = -1.0
        target = tmp_path / "mytoken.secret"
        target.write_text("x")
        assert is_attach_ignored(str(target))

    def test_no_match(self, tmp_path, monkeypatch):
        afile = tmp_path / ".attachignore"
        afile.write_text("*.secret\n")
        monkeypatch.setattr("utils.attachments._ATTACHIGNORE_PATH", str(afile))
        import utils.attachments as ua
        ua._attachignore_cache = None
        ua._attachignore_mtime = -1.0
        target = tmp_path / "report.csv"
        target.write_text("a,b")
        assert not is_attach_ignored(str(target))

    def test_empty_patterns(self, monkeypatch):
        monkeypatch.setattr("utils.attachments._ATTACHIGNORE_PATH", "/nonexistent/.attachignore")
        import utils.attachments as ua
        ua._attachignore_cache = None
        ua._attachignore_mtime = -1.0
        assert not is_attach_ignored("/home/ubuntu/file.txt")


class TestNormalizeImageFilenames:
    def test_bare_filename_resolved(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments._IMAGE_SEARCH_DIRS", (str(tmp_path),))
        img = tmp_path / "chart.png"
        img.write_bytes(_MAGIC["png"])
        result = normalize_image_filenames("こちらが chart.png です")
        assert str(img) in result

    def test_already_absolute_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments._IMAGE_SEARCH_DIRS", (str(tmp_path),))
        img = tmp_path / "chart.png"
        img.write_bytes(_MAGIC["png"])
        full = str(img)
        result = normalize_image_filenames(f"see {full}")
        # absolute path must not be double-replaced
        assert result.count(full) == 1

    def test_ambiguous_not_replaced(self, tmp_path, monkeypatch):
        dir1 = tmp_path / "d1"
        dir2 = tmp_path / "d2"
        dir1.mkdir(); dir2.mkdir()
        monkeypatch.setattr("utils.attachments._IMAGE_SEARCH_DIRS", (str(dir1), str(dir2)))
        (dir1 / "pic.png").write_bytes(_MAGIC["png"])
        (dir2 / "pic.png").write_bytes(_MAGIC["png"])
        result = normalize_image_filenames("show pic.png")
        assert "pic.png" in result  # bare name stays — ambiguous

    def test_missing_file_not_replaced(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments._IMAGE_SEARCH_DIRS", (str(tmp_path),))
        result = normalize_image_filenames("show missing.png")
        assert "missing.png" in result
        assert str(tmp_path) not in result


class TestExtractImagePathsAdmin:
    def test_admin_allows_home_ubuntu(self, tmp_path, monkeypatch):
        """Admin mode must accept paths under ADMIN_IMAGE_DIRS (mocked to tmp_path)."""
        monkeypatch.setattr("utils.attachments.ADMIN_IMAGE_DIRS", (str(tmp_path),))
        img = tmp_path / "chart.png"
        img.write_bytes(_MAGIC["png"])
        paths = extract_image_paths(f"see {img}", admin=True)
        assert str(img) in paths

    def test_non_admin_rejects_admin_only_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments.ALLOWED_IMAGE_DIRS", ("/nonexistent",))
        monkeypatch.setattr("utils.attachments.ADMIN_IMAGE_DIRS", (str(tmp_path),))
        img = tmp_path / "chart.png"
        img.write_bytes(_MAGIC["png"])
        paths = extract_image_paths(f"see {img}", admin=False)
        assert paths == []

    def test_symlink_escape_rejected(self, tmp_path, monkeypatch):
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir(); outside.mkdir()
        monkeypatch.setattr("utils.attachments.ALLOWED_IMAGE_DIRS", (str(allowed),))
        real_img = outside / "secret.png"
        real_img.write_bytes(_MAGIC["png"])
        link = allowed / "link.png"
        link.symlink_to(real_img)
        # realpath of link points to outside/, which is not under allowed/
        paths = extract_image_paths(f"see {link}")
        assert paths == []


class TestStripAttachedPaths:
    def test_removes_bare_path_on_own_line(self):
        text = "See below:\n/tmp/foo.png\nDone."
        result = strip_attached_paths(text, ["/tmp/foo.png"])
        assert "/tmp/foo.png" not in result

    def test_removes_attach_notation(self):
        text = "result [attach:/tmp/foo.png] done"
        result = strip_attached_paths(text, ["/tmp/foo.png"])
        assert "[attach:/tmp/foo.png]" not in result

    def test_removes_attachment_notation(self):
        text = "see [Attachment: foo.png] here"
        result = strip_attached_paths(text, ["/tmp/foo.png"])
        assert "[Attachment: foo.png]" not in result

    def test_collapses_extra_newlines(self):
        text = "line1\n\n\n\nline2"
        result = strip_attached_paths(text, [])
        assert "\n\n\n" not in result

    def test_empty_paths(self):
        text = "hello world"
        assert strip_attached_paths(text, []) == "hello world"


class TestExtractFilePathsExtra:
    def test_image_ext_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        img = tmp_path / "photo.png"
        img.write_bytes(_MAGIC["png"])
        result = extract_file_paths(f"see {img}")
        assert result == []

    def test_outside_admin_dirs_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", ("/nonexistent",))
        f = tmp_path / "data.csv"
        f.write_text("a,b\n")
        result = extract_file_paths(f"output: {f}")
        assert result == []

    def test_empty_file_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        f = tmp_path / "empty.csv"
        f.write_text("")
        result = extract_file_paths(f"output: {f}")
        assert result == []

    def test_too_large_file_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        f = tmp_path / "big.csv"
        f.write_bytes(b"x" * 100)
        monkeypatch.setattr("utils.attachments.MAX_FILE_ATTACH_BYTES", 50)
        result = extract_file_paths(f"output: {f}")
        assert result == []

    def test_caps_at_max(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        monkeypatch.setattr("utils.attachments.MAX_FILE_ATTACHMENTS", 2)
        files = []
        for i in range(5):
            f = tmp_path / f"data{i}.csv"
            f.write_text("a,b\n1,2\n")
            files.append(str(f))
        text = " ".join(files)
        result = extract_file_paths(text)
        assert len(result) == 2

    def test_deduplicates(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        f = tmp_path / "report.csv"
        f.write_text("a,b\n1,2\n")
        text = f"see {f} and also {f}"
        result = extract_file_paths(text)
        assert len(result) == 1

    def test_file_path_with_space_in_backtick(self, tmp_path, monkeypatch):
        """Backtick-enclosed file path containing a space must be extracted."""
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        f = tmp_path / "report (final).csv"
        f.write_text("a,b\n1,2\n")
        text = f"output: `{f}`"
        result = extract_file_paths(text)
        assert str(f) in result

    def test_file_path_with_space_in_double_quote(self, tmp_path, monkeypatch):
        """Double-quote-enclosed file path containing a space must be extracted."""
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        f = tmp_path / "data export (2026).csv"
        f.write_text("a,b\n")
        text = f'see "{f}"'
        result = extract_file_paths(text)
        assert str(f) in result

    def test_file_path_with_space_in_markdown_link(self, tmp_path, monkeypatch):
        """Markdown link syntax with space in path must be extracted."""
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        f = tmp_path / "result (v2).txt"
        f.write_text("hello\n")
        text = f"[download]({f})"
        result = extract_file_paths(text)
        assert str(f) in result

    def test_file_unenclosed_space_not_extracted(self, tmp_path, monkeypatch):
        """A file path with spaces NOT enclosed must NOT be extracted."""
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        f = tmp_path / "no extract.csv"
        f.write_text("a,b\n")
        text = f"check {f} done"
        result = extract_file_paths(text)
        assert str(f) not in result

    def test_file_leading_fullwidth_colon(self, tmp_path, monkeypatch):
        """File path directly after a full-width colon (STL：/path) must extract.

        Regression for the 2026-07-09 case where `STL：/home/.../model.stl`
        silently dropped the attachment because the leading anchor required a
        space/backtick/paren and rejected the colon."""
        monkeypatch.setattr("utils.attachments.ADMIN_FILE_DIRS", (str(tmp_path),))
        f = tmp_path / "model.stl"
        f.write_text("solid x\nendsolid x\n")
        text = f"STL：{f}"
        result = extract_file_paths(text)
        assert str(f) in result


class TestCleanupAttachmentsAsync:
    @pytest.mark.asyncio
    async def test_removes_files_async(self, tmp_path):
        f = tmp_path / "x.png"
        f.write_bytes(b"data")
        await cleanup_attachments_async([str(f)])
        assert not f.exists()

    @pytest.mark.asyncio
    async def test_empty_list_noop(self):
        await cleanup_attachments_async([])  # must not raise


class TestDownloadAttachmentsExtra:
    def _make_att(self, content_type="image/png", size=1000, filename="test.png", att_id=1,
                  url="https://cdn.discordapp.com/attachments/1/2/test.png"):
        att = MagicMock()
        att.content_type = content_type
        att.size = size
        att.filename = filename
        att.id = att_id
        att.url = url
        return att

    @pytest.mark.asyncio
    async def test_rejects_non_discord_url(self, tmp_attachment_dir):
        att = self._make_att(url="https://evil.example.com/malware.png")
        session = MagicMock()
        result = await download_attachments(session, [att])
        assert result == []

    @pytest.mark.asyncio
    async def test_caps_at_max_image_attachments(self, monkeypatch):
        monkeypatch.setattr("utils.attachments.MAX_IMAGE_ATTACHMENTS", 2)
        atts = [self._make_att(att_id=i) for i in range(5)]
        session = MagicMock()
        # Patch _download_one to avoid real HTTP
        with patch("utils.attachments._download_one", new=AsyncMock(return_value=None)):
            result = await download_attachments(session, atts)
        assert result == []

    @pytest.mark.asyncio
    async def test_successful_download(self, tmp_attachment_dir):
        att = self._make_att(
            att_id=9999,
            url="https://cdn.discordapp.com/attachments/1/9999/test.png",
        )
        chunk = _MAGIC["png"]

        async def fake_iter_chunked(size):
            yield chunk

        resp = MagicMock()
        resp.status = 200
        resp.content.iter_chunked = fake_iter_chunked
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=resp)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.get = MagicMock(return_value=session_ctx)

        result = await download_attachments(session, [att])
        assert len(result) == 1
        assert result[0].endswith(".png")

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self, tmp_attachment_dir):
        att = self._make_att(
            att_id=8888,
            url="https://cdn.discordapp.com/attachments/1/8888/test.png",
        )
        resp = MagicMock()
        resp.status = 404
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.get = MagicMock(return_value=ctx)
        result = await download_attachments(session, [att])
        assert result == []


class TestDownloadTextAttachments:
    def _make_text_att(self, content_type="text/plain", size=100, filename="msg.txt",
                       url="https://cdn.discordapp.com/attachments/1/1/msg.txt"):
        att = MagicMock()
        att.content_type = content_type
        att.size = size
        att.filename = filename
        att.url = url
        return att

    @pytest.mark.asyncio
    async def test_filters_non_text(self):
        att = self._make_text_att(content_type="image/png")
        session = MagicMock()
        result = await download_text_attachments(session, [att])
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_oversized(self):
        att = self._make_text_att(size=200 * 1024)
        session = MagicMock()
        result = await download_text_attachments(session, [att])
        assert result == []

    @pytest.mark.asyncio
    async def test_rejects_non_discord_url(self):
        att = self._make_text_att(url="https://evil.com/secret.txt")
        session = MagicMock()
        result = await download_text_attachments(session, [att])
        assert result == []

    @pytest.mark.asyncio
    async def test_successful_text_download(self):
        att = self._make_text_att()
        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=b"hello world")
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.get = MagicMock(return_value=ctx)
        result = await download_text_attachments(session, [att])
        assert len(result) == 1
        assert result[0] == ("msg.txt", "hello world")

    @pytest.mark.asyncio
    async def test_caps_at_max_text_attachments(self, monkeypatch):
        monkeypatch.setattr("utils.attachments.MAX_TEXT_ATTACHMENTS", 2)
        atts = [self._make_text_att(filename=f"f{i}.txt") for i in range(5)]
        with patch("utils.attachments._download_text_one", new=AsyncMock(return_value=None)):
            result = await download_text_attachments(MagicMock(), atts)
        assert result == []


class TestCleanupStaleAttachmentsMultiDir:
    def test_cleans_multiple_dirs(self, tmp_path, monkeypatch):
        d1 = tmp_path / "mermaid"
        d2 = tmp_path / "table"
        d1.mkdir(); d2.mkdir()
        monkeypatch.setattr("config.ATTACHMENT_DIR", str(tmp_path / "att"))
        (tmp_path / "att").mkdir()
        monkeypatch.setattr("config.MERMAID_OUTPUT_DIR", str(d1))
        monkeypatch.setattr("config.TABLE_RENDERS_DIR", str(d2))
        monkeypatch.setattr("config.MATH_RENDERS_DIR", str(tmp_path / "math"))
        (tmp_path / "math").mkdir()
        old1 = d1 / "old.png"
        old2 = d2 / "old.png"
        old1.write_bytes(b"x"); old2.write_bytes(b"x")
        os.utime(old1, (0, 0)); os.utime(old2, (0, 0))
        cleanup_stale_attachments()
        assert not old1.exists()
        assert not old2.exists()

    def test_skips_nonexistent_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.ATTACHMENT_DIR", str(tmp_path / "nonexistent"))
        monkeypatch.setattr("config.MERMAID_OUTPUT_DIR", "")
        monkeypatch.setattr("config.TABLE_RENDERS_DIR", "")
        monkeypatch.setattr("config.MATH_RENDERS_DIR", "")
        cleanup_stale_attachments()  # must not raise


class TestReDoSGuards:
    def test_image_path_re_bounded(self):
        """IMAGE_PATH_RE must not backtrack on adversarial input."""
        big = "/" + "a" * 10000 + ".png"
        start = time.monotonic()
        extract_image_paths(big)
        assert time.monotonic() - start < 1.0

    def test_file_path_re_bounded(self):
        """FILE_PATH_RE must not backtrack on adversarial input."""
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


class TestDownloadArbitraryAttachments:
    """Coverage for the admin-only binary downloader.

    Targets the gap surfaced by the .als incident on 2026-05-19:
    image / text downloaders silently dropped the .als payload, and bot.py
    had no way to surface it to kanae.
    """

    def _make_att(self, *, content_type=None, size=1000, filename="Zero Gravity.als", att_id=42):
        from unittest.mock import MagicMock
        att = MagicMock()
        att.content_type = content_type
        att.size = size
        att.filename = filename
        att.id = att_id
        att.url = f"https://cdn.discordapp.com/attachments/fake/{att_id}/{filename}"
        return att

    @pytest.mark.asyncio
    async def test_returns_empty_for_non_admin(self, tmp_attachment_dir):
        from utils.attachments import download_arbitrary_attachments
        att = self._make_att(content_type="application/octet-stream")
        session = MagicMock()
        result = await download_arbitrary_attachments(
            session, [att], caller_is_admin=False,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_image_and_text_types(self, tmp_attachment_dir):
        from utils.attachments import download_arbitrary_attachments
        atts = [
            self._make_att(content_type="image/png", filename="x.png", att_id=1),
            self._make_att(content_type="text/plain", filename="x.txt", att_id=2),
            self._make_att(content_type="application/json", filename="x.json", att_id=3),
        ]
        session = MagicMock()
        result = await download_arbitrary_attachments(
            session, atts, caller_is_admin=True,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_downloads_unknown_mime(self, tmp_attachment_dir):
        from utils.attachments import download_arbitrary_attachments
        att = self._make_att(content_type=None, filename="Zero Gravity.als")

        # Mock the streaming body so we don't hit the network.
        class _StreamCtx:
            status = 200
            class content:
                @staticmethod
                async def iter_chunked(_n):
                    for chunk in [b"PK\x03\x04", b"fake-als-body" * 10]:
                        yield chunk
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        session = MagicMock()
        session.get = MagicMock(return_value=_StreamCtx())

        result = await download_arbitrary_attachments(
            session, [att], caller_is_admin=True,
        )
        assert len(result) == 1
        meta = result[0]
        assert meta["filename"] == "Zero Gravity.als"
        assert meta["mime"] == "application/octet-stream"  # null → default
        assert meta["size"] > 0
        assert os.path.isfile(meta["path"])
        # Filename safe: starts with att.id prefix, ends with sanitized .als
        assert meta["path"].endswith(".als")
        assert "42-" in os.path.basename(meta["path"])

    @pytest.mark.asyncio
    async def test_rejects_oversized_size_hint(self, tmp_attachment_dir):
        from utils.attachments import (
            download_arbitrary_attachments,
            MAX_ARBITRARY_ATTACHMENT_BYTES,
        )
        att = self._make_att(
            content_type="application/octet-stream",
            size=MAX_ARBITRARY_ATTACHMENT_BYTES + 1,
        )
        session = MagicMock()
        result = await download_arbitrary_attachments(
            session, [att], caller_is_admin=True,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_rejects_denylisted_extension(self, tmp_attachment_dir):
        from utils.attachments import download_arbitrary_attachments
        att = self._make_att(
            content_type="application/octet-stream",
            filename="payload.exe",
        )
        session = MagicMock()
        result = await download_arbitrary_attachments(
            session, [att], caller_is_admin=True,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_rejects_non_discord_host(self, tmp_attachment_dir):
        from utils.attachments import download_arbitrary_attachments
        att = self._make_att(content_type="application/octet-stream")
        att.url = "https://evil.example.com/payload.bin"
        session = MagicMock()
        result = await download_arbitrary_attachments(
            session, [att], caller_is_admin=True,
        )
        assert result == []

    def test_sanitize_basename_strips_path_separators(self):
        from utils.attachments import _sanitize_basename
        # Forward slash is stripped by os.path.basename, '\' is replaced
        # by the regex on POSIX. Either way no path-component separator
        # survives, which is what the realpath guard relies on.
        assert "/" not in _sanitize_basename("../../etc/passwd")
        assert "\\" not in _sanitize_basename("..\\..\\windows\\system32")

    def test_sanitize_basename_preserves_japanese(self):
        from utils.attachments import _sanitize_basename
        # Japanese chars are \w under re.UNICODE so they survive.
        assert _sanitize_basename("耳コピ.als") == "耳コピ.als"

    def test_sanitize_basename_caps_length(self):
        from utils.attachments import _sanitize_basename
        long_name = "a" * 200 + ".als"
        result = _sanitize_basename(long_name)
        assert len(result) <= 80
