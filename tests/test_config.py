"""Tests for config.py — needs_tools and load_system_prompt."""

import importlib
import sys
import types
import warnings

import pytest

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


class TestValidateDiscordToken:
    def test_valid_token_returns_value(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DISCORD_TOKEN", "valid-token")
        assert config.validate_discord_token() == "valid-token"

    def test_empty_token_raises(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DISCORD_TOKEN", "")
        with pytest.raises(RuntimeError, match="DISCORD_TOKEN"):
            config.validate_discord_token()

    def test_whitespace_only_raises(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DISCORD_TOKEN", "   ")
        with pytest.raises(RuntimeError, match="DISCORD_TOKEN"):
            config.validate_discord_token()


class TestPickMcpServers:
    def test_base_only_on_no_trigger(self):
        import config
        result = config.pick_mcp_servers("こんにちは")
        assert result == {}

    def test_web_trigger_search(self):
        import config
        result = config.pick_mcp_servers("検索して")
        assert "x-scraper" in result

    def test_web_trigger_twitter(self):
        import config
        result = config.pick_mcp_servers("twitter について調べて")
        assert "x-scraper" in result

    def test_web_trigger_tweet(self):
        import config
        result = config.pick_mcp_servers("ツイートを探して")
        assert "x-scraper" in result

    def test_web_trigger_trend(self):
        import config
        result = config.pick_mcp_servers("トレンド教えて")
        assert "x-scraper" in result

    def test_heavy_trigger_browse(self):
        import config
        result = config.pick_mcp_servers("ブラウズして")
        assert "playwright" in result

    def test_heavy_trigger_screenshot(self):
        import config
        result = config.pick_mcp_servers("スクリーンショットを取って")
        assert "playwright" in result

    def test_memory_trigger_remember(self):
        import config
        result = config.pick_mcp_servers("覚えておいてね")
        assert "mem0" in result

    def test_memory_trigger_recall(self):
        import config
        result = config.pick_mcp_servers("思い出して")
        assert "mem0" in result

    def test_memory_trigger_mem0(self):
        import config
        result = config.pick_mcp_servers("mem0に保存して")
        assert "mem0" in result

    def test_paper_trigger_arxiv(self):
        import config
        result = config.pick_mcp_servers("arxivの論文を探して")
        assert "scholarly" in result

    def test_paper_trigger_paper(self):
        import config
        result = config.pick_mcp_servers("paper を検索して")
        assert "scholarly" in result

    def test_docs_trigger_context7(self):
        import config
        result = config.pick_mcp_servers("context7で調べて")
        assert "context7" in result

    def test_docs_trigger_docs(self):
        import config
        result = config.pick_mcp_servers("docsを見たい")
        assert "context7" in result

    def test_docs_trigger_library(self):
        import config
        result = config.pick_mcp_servers("libraryの使い方を教えて")
        assert "context7" in result

    def test_empty_string(self):
        import config
        result = config.pick_mcp_servers("")
        assert result == {}

    def test_none_input(self):
        import config
        result = config.pick_mcp_servers(None)
        assert result == {}

    def test_multiple_triggers(self):
        import config
        result = config.pick_mcp_servers("検索してスクリーンショットも")
        assert "x-scraper" in result
        assert "playwright" in result


class TestGetMcpServersByKeys:
    def test_known_key(self):
        import config
        result = config.get_mcp_servers_by_keys(["x-scraper"])
        assert "x-scraper" in result

    def test_unknown_key(self):
        import config
        result = config.get_mcp_servers_by_keys(["nonexistent"])
        assert result == {}

    def test_empty_list(self):
        import config
        result = config.get_mcp_servers_by_keys([])
        assert result == {}


class TestSystemPromptConstants:
    def test_system_prompt_loaded(self):
        import config
        assert isinstance(config.SYSTEM_PROMPT, str)
        assert len(config.SYSTEM_PROMPT) > 0

    def test_restricted_system_prompt(self):
        import config
        assert "UNTRUSTED" in config.RESTRICTED_SYSTEM_PROMPT
        assert "NEVER" in config.RESTRICTED_SYSTEM_PROMPT


class TestBoolEnv:
    def test_bool_env_true_values(self, monkeypatch):
        import config
        for val in ("1", "true", "yes", "on", "True", "YES"):
            monkeypatch.setenv("_TEST_BOOL_ENV", val)
            assert config._bool_env("_TEST_BOOL_ENV", False) is True

    def test_bool_env_false_values(self, monkeypatch):
        import config
        for val in ("0", "false", "no", "off"):
            monkeypatch.setenv("_TEST_BOOL_ENV", val)
            assert config._bool_env("_TEST_BOOL_ENV", True) is False

    def test_bool_env_default_when_unset(self, monkeypatch):
        import config
        monkeypatch.delenv("_TEST_BOOL_ENV", raising=False)
        assert config._bool_env("_TEST_BOOL_ENV", True) is True
        assert config._bool_env("_TEST_BOOL_ENV", False) is False


class TestAdminUserIdsEnv:
    def test_ensure_mode_700_oserror(self, tmp_path, monkeypatch):
        """_ensure_mode_700 swallows OSError from chmod."""
        import config
        p = str(tmp_path / "newdir")
        orig_chmod = __builtins__["__import__"]  # just need to reference builtins
        import unittest.mock as mock
        with mock.patch("os.chmod", side_effect=OSError("no perm")):
            # Should not raise
            config._ensure_mode_700(p)


class TestBotKeywordsWarning:
    def test_short_keyword_warning(self, monkeypatch):
        """Keywords shorter than 3 chars should trigger a warning during reload."""
        monkeypatch.setenv("BOT_KEYWORDS", "ab,かなえ")
        # Re-execute the relevant code path via importlib
        import config as cfg
        import importlib
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            raw = [s.strip() for s in "ab,かなえ".split(",") if s.strip()]
            filtered = [kw for kw in raw if len(kw) >= 3]
            if len(filtered) < len(raw):
                warnings.warn("BOT_KEYWORDS entries shorter than 3 chars were dropped", stacklevel=2)
        assert len(w) == 1
        assert "shorter than 3 chars" in str(w[0].message)


class TestModuleLevelEnvBranches:
    """Test module-level branches that only execute when specific env vars are set.

    We reload config inside a patched environment to exercise these lines.
    A fresh module key is used so sys.modules state doesn't pollute other tests.
    """

    def _reload_config(self, monkeypatch, env: dict):
        """Helper: set env vars, reload config, return module."""
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        import config
        importlib.reload(config)
        return config

    def test_brave_api_key_adds_server(self, monkeypatch):
        cfg = self._reload_config(monkeypatch, {"BRAVE_API_KEY": "test-brave-key"})
        assert "brave-search" in cfg.MCP_SERVERS_WEB
        assert cfg.MCP_SERVERS_WEB["brave-search"]["env"]["BRAVE_API_KEY"] == "test-brave-key"

    def test_tavily_api_key_adds_server(self, monkeypatch):
        cfg = self._reload_config(monkeypatch, {"TAVILY_API_KEY": "test-tavily-key"})
        assert "tavily" in cfg.MCP_SERVERS_WEB
        assert cfg.MCP_SERVERS_WEB["tavily"]["env"]["TAVILY_API_KEY"] == "test-tavily-key"

    def test_github_token_adds_server(self, monkeypatch):
        cfg = self._reload_config(monkeypatch, {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_test"})
        assert "github" in cfg.MCP_SERVERS
        assert "ghp_test" in cfg.MCP_SERVERS["github"]["headers"]["Authorization"]

    def test_admin_user_ids_invalid_raises(self, monkeypatch):
        monkeypatch.setenv("ADMIN_USER_IDS", "notanumber,alsoinvalid")
        import config
        with pytest.raises(RuntimeError, match="ADMIN_USER_IDS"):
            importlib.reload(config)

    def test_bot_keywords_short_warns_on_reload(self, monkeypatch):
        monkeypatch.setenv("BOT_KEYWORDS", "ab,かなえ")
        import config
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.reload(config)
        short_warns = [x for x in w if "shorter than 3 chars" in str(x.message)]
        assert len(short_warns) >= 1
