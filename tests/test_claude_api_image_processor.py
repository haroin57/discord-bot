"""Unit tests for services/claude_api/image_processor.py."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from unittest.mock import patch

import pytest

# PNG magic bytes used across tests
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


# ---------------------------------------------------------------------------
# safe_image_path
# ---------------------------------------------------------------------------
class TestSafeImagePath:
    def test_safe_image_path_valid(self, tmp_path, monkeypatch):
        """A real file under ATTACHMENT_DIR is accepted and its realpath returned."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(tmp_path))

        img = tmp_path / "test.png"
        img.write_bytes(_PNG_BYTES)

        result = mod.safe_image_path(str(img))
        assert result == os.path.realpath(str(img))

    def test_safe_image_path_outside_dir(self, tmp_path, monkeypatch):
        """A file outside ATTACHMENT_DIR is rejected."""
        import services.claude_api.image_processor as mod

        att_dir = tmp_path / "attachments"
        att_dir.mkdir()
        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(att_dir))

        outside = tmp_path / "secret.png"
        outside.write_bytes(_PNG_BYTES)

        assert mod.safe_image_path(str(outside)) is None

    def test_safe_image_path_symlink_escape(self, tmp_path, monkeypatch):
        """A symlink inside ATTACHMENT_DIR that resolves outside is rejected."""
        import services.claude_api.image_processor as mod

        att_dir = tmp_path / "attachments"
        att_dir.mkdir()
        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(att_dir))

        outside_target = tmp_path / "real_file.png"
        outside_target.write_bytes(_PNG_BYTES)

        symlink = att_dir / "link.png"
        symlink.symlink_to(outside_target)

        assert mod.safe_image_path(str(symlink)) is None

    def test_safe_image_path_too_large(self, tmp_path, monkeypatch):
        """A file exceeding IMAGE_MAX_BYTES is rejected."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(tmp_path))
        monkeypatch.setattr(mod, "IMAGE_MAX_BYTES", 10)

        big = tmp_path / "big.png"
        big.write_bytes(b"X" * 100)

        assert mod.safe_image_path(str(big)) is None

    def test_safe_image_path_nonexistent(self, tmp_path, monkeypatch):
        """A path that does not exist returns None (OSError caught)."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(tmp_path))

        ghost = str(tmp_path / "does_not_exist.png")
        assert mod.safe_image_path(ghost) is None

    def test_safe_image_path_returns_realpath(self, tmp_path, monkeypatch):
        """The returned value is always os.path.realpath of the candidate."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(tmp_path))

        img = tmp_path / "sub" / "img.png"
        img.parent.mkdir()
        img.write_bytes(_PNG_BYTES)

        result = mod.safe_image_path(str(img))
        assert result == os.path.realpath(str(img))


# ---------------------------------------------------------------------------
# extract_attached_image_blocks
# ---------------------------------------------------------------------------
class TestExtractAttachedImageBlocks:
    def _make_image(self, directory: Path, name: str = "img.png") -> Path:
        p = directory / name
        p.write_bytes(_PNG_BYTES)
        return p

    def _patch_validator(self, monkeypatch):
        """Mock validate_image_bytes to accept any bytes as image/png,
        and stub _maybe_resize_image so fake PNGs pass through."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(
            mod,
            "validate_image_bytes",
            lambda data, max_bytes=None: ("image/png", None),
        )
        monkeypatch.setattr(
            mod,
            "_maybe_resize_image",
            lambda data, media_type, path_for_log: (data, media_type),
        )

    def test_extract_no_block(self, tmp_path, monkeypatch):
        """Prompt without any <attached_images> block is returned unchanged."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(tmp_path))

        prompt = "hello world"
        out, blocks = mod.extract_attached_image_blocks(prompt)
        assert out == prompt
        assert blocks == []

    def test_extract_strips_last_block(self, tmp_path, monkeypatch):
        """The <attached_images> block is removed from the returned prompt."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(tmp_path))
        self._patch_validator(monkeypatch)

        img = self._make_image(tmp_path)
        prompt = (
            f"hello\n<attached_images>\n- {img}\n</attached_images>\nthanks"
        )

        out, blocks = mod.extract_attached_image_blocks(prompt)
        assert "<attached_images>" not in out
        assert "hello" in out
        assert "thanks" in out

    def test_extract_parses_paths(self, tmp_path, monkeypatch):
        """Paths inside the block are extracted and turned into ImageBlocks."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(tmp_path))
        self._patch_validator(monkeypatch)

        img = self._make_image(tmp_path)
        prompt = (
            f"text\n<attached_images>\n"
            f"- {img} (Use the Read tool to view this image)\n"
            f"</attached_images>"
        )

        _, blocks = mod.extract_attached_image_blocks(prompt)

        assert len(blocks) == 1
        block = blocks[0]
        assert block["type"] == "image"
        assert block["source"]["type"] == "base64"
        assert block["source"]["media_type"] == "image/png"
        # base64 data must decode to the original bytes
        decoded = base64.b64decode(block["source"]["data"])
        assert decoded == _PNG_BYTES

    def test_extract_only_last_block_is_parsed(self, tmp_path, monkeypatch):
        """When multiple <attached_images> blocks exist, only the last is parsed."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(tmp_path))
        self._patch_validator(monkeypatch)

        img = self._make_image(tmp_path)
        prompt = (
            "<attached_images>\n- /etc/passwd\n</attached_images>\n"
            f"real text\n"
            f"<attached_images>\n- {img}\n</attached_images>"
        )

        out, blocks = mod.extract_attached_image_blocks(prompt)
        # Only the last block (the valid image) should produce a block
        assert len(blocks) == 1
        assert "/etc/passwd" not in str(blocks)
        # The prompt text from between blocks should be retained
        assert "real text" in out

    def test_extract_max_count(self, tmp_path, monkeypatch):
        """Only IMAGE_MAX_COUNT images are included even if more paths are listed."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(tmp_path))
        self._patch_validator(monkeypatch)
        # Limit to 3 for this test to keep file creation minimal
        monkeypatch.setattr(mod, "IMAGE_MAX_COUNT", 3)

        images = [self._make_image(tmp_path, f"img{i}.png") for i in range(5)]
        lines = "\n".join(f"- {img}" for img in images)
        prompt = f"<attached_images>\n{lines}\n</attached_images>"

        _, blocks = mod.extract_attached_image_blocks(prompt)
        assert len(blocks) == 3

    def test_extract_invalid_path_skipped(self, tmp_path, monkeypatch):
        """Paths rejected by safe_image_path produce no block."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(tmp_path))

        prompt = "<attached_images>\n- /etc/passwd\n</attached_images>"
        _, blocks = mod.extract_attached_image_blocks(prompt)
        assert blocks == []

    def test_extract_validate_image_bytes_failure_skipped(self, tmp_path, monkeypatch):
        """An image that fails validate_image_bytes is omitted from blocks."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(tmp_path))
        monkeypatch.setattr(
            mod,
            "validate_image_bytes",
            lambda data, max_bytes=None: (None, "bad magic"),
        )

        img = self._make_image(tmp_path)
        prompt = f"<attached_images>\n- {img}\n</attached_images>"

        _, blocks = mod.extract_attached_image_blocks(prompt)
        assert blocks == []

    def test_extract_prompt_stripped_even_on_empty_blocks(self, tmp_path, monkeypatch):
        """The <attached_images> block is removed from prompt even when all images fail."""
        import services.claude_api.image_processor as mod

        monkeypatch.setattr(mod, "ATTACHMENT_DIR", str(tmp_path))

        prompt = "pre\n<attached_images>\n- /no/such/file.png\n</attached_images>\npost"
        out, blocks = mod.extract_attached_image_blocks(prompt)
        assert "<attached_images>" not in out
        assert "pre" in out
        assert "post" in out
        assert blocks == []
