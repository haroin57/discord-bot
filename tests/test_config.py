"""Tests for config.py — needs_tools and load_system_prompt."""

from config import needs_tools, load_system_prompt


class TestNeedsTools:
    def test_tool_keywords_match(self):
        assert needs_tools("ファイル読んで") is True
        assert needs_tools("実行して") is True
        assert needs_tools("git push") is True
        assert needs_tools("sudo apt update") is True
        assert needs_tools("Mermaidで書いて") is True
        assert needs_tools("systemctl restart") is True
        assert needs_tools("調べて") is True

    def test_no_tool_keywords(self):
        assert needs_tools("やっほー") is False
        assert needs_tools("元気？") is False
        assert needs_tools("今日の天気は？") is False
        assert needs_tools("ありがとう") is False
        assert needs_tools("") is False

    def test_partial_match_not_triggered(self):
        # "テスト" was removed from keywords to avoid false positives
        assert needs_tools("テストです") is False
        assert needs_tools("セッションのテスト") is False


class TestLoadSystemPrompt:
    def test_returns_default_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.BOT_DIR", str(tmp_path))
        result = load_system_prompt()
        assert "helpful assistant" in result

    def test_reads_claude_md(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.BOT_DIR", str(tmp_path))
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("カスタムプロンプト", encoding="utf-8")
        result = load_system_prompt()
        assert result == "カスタムプロンプト"
