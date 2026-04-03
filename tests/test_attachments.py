"""Tests for utils/attachments.py — extract_image_paths, cleanup."""

import os

from utils.attachments import cleanup_attachments, cleanup_stale_attachments, extract_image_paths


class TestExtractImagePaths:
    def test_finds_path_in_text(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(b"fake")
        text = f"画像は {img} に保存しました。"
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_ignores_nonexistent(self):
        text = "画像は /tmp/nonexistent_abc123.png です"
        assert extract_image_paths(text) == []

    def test_markdown_syntax(self, tmp_path):
        img = tmp_path / "out.png"
        img.write_bytes(b"fake")
        text = f"![diagram]({img})"
        paths = extract_image_paths(text)
        assert str(img) in paths

    def test_caps_at_max(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.attachments.MAX_RESPONSE_IMAGES", 2)
        imgs = []
        for i in range(5):
            img = tmp_path / f"img{i}.png"
            img.write_bytes(b"fake")
            imgs.append(str(img))
        text = " ".join(imgs)
        paths = extract_image_paths(text)
        assert len(paths) == 2

    def test_deduplicates(self, tmp_path):
        img = tmp_path / "dup.png"
        img.write_bytes(b"fake")
        text = f"{img} and again {img}"
        paths = extract_image_paths(text)
        assert len(paths) == 1


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
        cleanup_attachments([str(tmp_path / "gone.png")])  # no error


class TestCleanupStaleAttachments:
    def test_removes_old_files(self, tmp_attachment_dir):
        old = tmp_attachment_dir / "old.png"
        old.write_bytes(b"x")
        # Make it appear old
        os.utime(old, (0, 0))
        cleanup_stale_attachments()
        assert not old.exists()

    def test_keeps_recent_files(self, tmp_attachment_dir):
        recent = tmp_attachment_dir / "new.png"
        recent.write_bytes(b"x")
        cleanup_stale_attachments()
        assert recent.exists()
