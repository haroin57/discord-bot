"""Unit tests for services/mem0/client.py — all external I/O is mocked."""

import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy deps that client.py imports at module level or lazily
# ---------------------------------------------------------------------------

# services.oauth_manager — mock before client.py is imported
_oauth_mod = types.ModuleType("services.oauth_manager")
_oauth_mod.ensure_valid_token = AsyncMock(return_value=None)
_oauth_mod.get_access_token = MagicMock(return_value="test-token-abc")
_oauth_mod.init = MagicMock(return_value=True)
if "services.oauth_manager" not in sys.modules:
    sys.modules["services.oauth_manager"] = _oauth_mod
else:
    _existing_oauth = sys.modules["services.oauth_manager"]
    _existing_oauth.ensure_valid_token = AsyncMock(return_value=None)
    _existing_oauth.get_access_token = MagicMock(return_value="test-token-abc")
    _existing_oauth.init = MagicMock(return_value=True)

# config.STATE_DIR — needed by _build_config
if "config" not in sys.modules:
    _cfg_mod = types.ModuleType("config")
    _cfg_mod.STATE_DIR = "/tmp/test_mem0_state"
    sys.modules["config"] = _cfg_mod

# mem0.Memory — stubbed lazily via patch inside tests
_mem0_pkg = types.ModuleType("mem0")
_mem0_pkg.Memory = MagicMock()
if "mem0" not in sys.modules:
    sys.modules["mem0"] = _mem0_pkg

# anthropic.AsyncAnthropic — stubbed lazily
_anthropic_mod = types.ModuleType("anthropic")
_anthropic_mod.AsyncAnthropic = MagicMock()
if "anthropic" not in sys.modules:
    sys.modules["anthropic"] = _anthropic_mod

# Now import the module under test.
# Reload to pick up the stubs above (in case a previous test file loaded it).
import services.mem0.client as client_mod  # noqa: E402

importlib.reload(client_mod)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _reset_singletons():
    """Reset all module-level singletons between tests."""
    client_mod._mem0_instance = None
    client_mod._init_attempted = False
    client_mod._last_token = None
    client_mod._async_anthropic_client = None
    client_mod._async_anthropic_token = None


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    _reset_singletons()
    # Reset oauth mocks to clean state
    monkeypatch.setattr(client_mod, "ensure_valid_token", AsyncMock(return_value=None))
    monkeypatch.setattr(client_mod, "get_access_token", MagicMock(return_value="test-token-abc"))
    monkeypatch.setattr(client_mod, "oauth_init", MagicMock(return_value=True))
    yield
    _reset_singletons()


# ---------------------------------------------------------------------------
# _get_mem0 tests
# ---------------------------------------------------------------------------

class TestGetMem0:
    def test_returns_instance_on_success(self, monkeypatch, tmp_path):
        """_get_mem0 returns a Memory instance when everything succeeds."""
        mock_memory = MagicMock()
        mock_memory_cls = MagicMock(return_value=mock_memory)
        mock_memory_cls.from_config = MagicMock(return_value=mock_memory)

        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))

        with patch.dict("sys.modules", {"mem0": types.SimpleNamespace(Memory=mock_memory_cls)}):
            result = client_mod._get_mem0()

        assert result is mock_memory

    def test_returns_none_when_mem0_raises(self, monkeypatch, tmp_path):
        """_get_mem0 returns None if Memory.from_config raises."""
        mock_memory_cls = MagicMock()
        mock_memory_cls.from_config = MagicMock(side_effect=RuntimeError("chroma fail"))

        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))

        with patch.dict("sys.modules", {"mem0": types.SimpleNamespace(Memory=mock_memory_cls)}):
            result = client_mod._get_mem0()

        assert result is None

    def test_init_attempted_flag_prevents_retry(self, monkeypatch, tmp_path):
        """Second call to _get_mem0 returns cached result without re-init."""
        mock_memory = MagicMock()
        mock_memory_cls = MagicMock()
        mock_memory_cls.from_config = MagicMock(return_value=mock_memory)

        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))

        with patch.dict("sys.modules", {"mem0": types.SimpleNamespace(Memory=mock_memory_cls)}):
            r1 = client_mod._get_mem0()
            r2 = client_mod._get_mem0()

        assert r1 is r2
        assert mock_memory_cls.from_config.call_count == 1

    def test_returns_cached_none_after_failure(self, monkeypatch, tmp_path):
        """After a failed init, subsequent calls return None without retrying."""
        mock_memory_cls = MagicMock()
        mock_memory_cls.from_config = MagicMock(side_effect=Exception("boom"))

        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))

        with patch.dict("sys.modules", {"mem0": types.SimpleNamespace(Memory=mock_memory_cls)}):
            r1 = client_mod._get_mem0()
            r2 = client_mod._get_mem0()

        assert r1 is None
        assert r2 is None
        assert mock_memory_cls.from_config.call_count == 1

    def test_oauth_init_failure_logs_error(self, monkeypatch, tmp_path, caplog):
        """_get_mem0 logs an error when oauth_init returns False."""
        monkeypatch.setattr(client_mod, "oauth_init", MagicMock(return_value=False))

        mock_memory = MagicMock()
        mock_memory_cls = MagicMock()
        mock_memory_cls.from_config = MagicMock(return_value=mock_memory)
        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))

        import logging
        with caplog.at_level(logging.ERROR, logger="services.mem0.client"):
            with patch.dict("sys.modules", {"mem0": types.SimpleNamespace(Memory=mock_memory_cls)}):
                client_mod._get_mem0()

        assert any("OAuth init failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _refresh_mem0_client_token tests
# ---------------------------------------------------------------------------

class TestRefreshMem0ClientToken:
    def test_no_op_when_no_instance(self, monkeypatch):
        """Does nothing if _mem0_instance is None."""
        monkeypatch.setattr(client_mod, "get_access_token", MagicMock(return_value="new-token"))
        client_mod._mem0_instance = None
        client_mod._last_token = None
        client_mod._refresh_mem0_client_token()  # must not raise

    def test_no_op_when_token_unchanged(self, monkeypatch):
        """Does not update when token is the same as _last_token."""
        token = "same-token"
        monkeypatch.setattr(client_mod, "get_access_token", MagicMock(return_value=token))
        client_mod._last_token = token

        mock_llm = MagicMock()
        mock_client = MagicMock()
        mock_client.auth_token = token
        mock_llm.client = mock_client
        client_mod._mem0_instance = MagicMock(llm=mock_llm)

        client_mod._refresh_mem0_client_token()
        assert mock_client.auth_token == token  # no change

    def test_updates_token_on_rotation(self, monkeypatch):
        """Updates auth_token when new token differs from _last_token."""
        monkeypatch.setattr(client_mod, "get_access_token", MagicMock(return_value="new-token"))
        client_mod._last_token = "old-token"

        mock_client = MagicMock(spec=["auth_token"])
        mock_client.auth_token = "old-token"
        mock_llm = MagicMock()
        mock_llm.client = mock_client
        client_mod._mem0_instance = MagicMock(llm=mock_llm)

        client_mod._refresh_mem0_client_token()
        assert mock_client.auth_token == "new-token"
        assert client_mod._last_token == "new-token"

    def test_handles_exception_gracefully(self, monkeypatch):
        """Swallows exceptions raised during token refresh."""
        monkeypatch.setattr(client_mod, "get_access_token", MagicMock(return_value="new-token"))
        client_mod._last_token = "old-token"

        bad_llm = MagicMock()
        bad_llm.client = MagicMock(spec=[])  # no auth_token attr
        client_mod._mem0_instance = MagicMock(llm=bad_llm)

        client_mod._refresh_mem0_client_token()  # must not raise

    def test_no_op_when_token_is_none(self, monkeypatch):
        """Does nothing if get_access_token returns None."""
        monkeypatch.setattr(client_mod, "get_access_token", MagicMock(return_value=None))
        client_mod._mem0_instance = MagicMock()
        client_mod._refresh_mem0_client_token()  # must not raise


# ---------------------------------------------------------------------------
# _build_config tests
# ---------------------------------------------------------------------------

class TestBuildConfig:
    def test_default_config_structure(self, monkeypatch, tmp_path):
        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))
        monkeypatch.delenv("MEM0_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("MEM0_LLM_MODEL", raising=False)
        monkeypatch.delenv("MEM0_EMBEDDER_PROVIDER", raising=False)
        monkeypatch.delenv("MEM0_EMBEDDER_MODEL", raising=False)

        cfg = client_mod._build_config()

        assert cfg["llm"]["provider"] == "anthropic"
        assert cfg["llm"]["config"]["model"] == "claude-haiku-4-5-20251001"
        assert cfg["vector_store"]["provider"] == "chroma"
        assert cfg["embedder"]["provider"] == "openai"
        assert cfg["embedder"]["config"]["model"] == "text-embedding-3-small"
        assert cfg["version"] == "v1.1"

    def test_env_overrides(self, monkeypatch, tmp_path):
        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))
        monkeypatch.setenv("MEM0_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MEM0_LLM_MODEL", "gpt-4o")
        monkeypatch.setenv("MEM0_EMBEDDER_PROVIDER", "huggingface")
        monkeypatch.setenv("MEM0_EMBEDDER_MODEL", "all-minilm")

        cfg = client_mod._build_config()

        assert cfg["llm"]["provider"] == "openai"
        assert cfg["llm"]["config"]["model"] == "gpt-4o"
        assert cfg["embedder"]["provider"] == "huggingface"
        assert cfg["embedder"]["config"]["model"] == "all-minilm"

    def test_creates_db_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))
        cfg = client_mod._build_config()
        import os
        assert os.path.isdir(cfg["vector_store"]["config"]["path"])


# ---------------------------------------------------------------------------
# get_all_memories tests
# ---------------------------------------------------------------------------

class TestGetAllMemories:
    def test_returns_list_when_result_is_dict(self, monkeypatch, tmp_path):
        mock_mem = MagicMock()
        mock_mem.get_all = MagicMock(return_value={"results": [{"id": "1"}]})
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        result = asyncio.get_event_loop().run_until_complete(
            client_mod.get_all_memories("user123")
        )
        assert result == [{"id": "1"}]

    def test_returns_list_when_result_is_list(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.get_all = MagicMock(return_value=[{"id": "2"}])
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        result = asyncio.get_event_loop().run_until_complete(
            client_mod.get_all_memories("user123")
        )
        assert result == [{"id": "2"}]

    def test_returns_empty_when_mem0_is_none(self, monkeypatch):
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=None))

        result = asyncio.get_event_loop().run_until_complete(
            client_mod.get_all_memories("user123")
        )
        assert result == []

    def test_returns_empty_on_exception(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.get_all = MagicMock(side_effect=RuntimeError("db error"))
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        result = asyncio.get_event_loop().run_until_complete(
            client_mod.get_all_memories("user123")
        )
        assert result == []

    def test_passes_limit_kwarg(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.get_all = MagicMock(return_value={"results": []})
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        asyncio.get_event_loop().run_until_complete(
            client_mod.get_all_memories("user123", limit=50)
        )
        mock_mem.get_all.assert_called_once_with(user_id="user123", limit=50)

    def test_no_limit_kwarg_when_none(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.get_all = MagicMock(return_value={"results": []})
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        asyncio.get_event_loop().run_until_complete(
            client_mod.get_all_memories("user123", limit=None)
        )
        call_kwargs = mock_mem.get_all.call_args[1]
        assert "limit" not in call_kwargs

    def test_returns_empty_when_result_is_unexpected_type(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.get_all = MagicMock(return_value=42)
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        result = asyncio.get_event_loop().run_until_complete(
            client_mod.get_all_memories("user123")
        )
        assert result == []


# ---------------------------------------------------------------------------
# delete_memory tests
# ---------------------------------------------------------------------------

class TestDeleteMemory:
    def test_returns_true_on_success(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.delete = MagicMock(return_value=None)
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        result = asyncio.get_event_loop().run_until_complete(
            client_mod.delete_memory("mem-id-123")
        )
        assert result is True

    def test_returns_false_when_mem0_is_none(self, monkeypatch):
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=None))

        result = asyncio.get_event_loop().run_until_complete(
            client_mod.delete_memory("mem-id-123")
        )
        assert result is False

    def test_returns_false_on_exception(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.delete = MagicMock(side_effect=RuntimeError("delete fail"))
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        result = asyncio.get_event_loop().run_until_complete(
            client_mod.delete_memory("mem-id-123")
        )
        assert result is False

    def test_calls_delete_with_correct_id(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.delete = MagicMock(return_value=None)
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        asyncio.get_event_loop().run_until_complete(
            client_mod.delete_memory("specific-mem-id")
        )
        mock_mem.delete.assert_called_once_with("specific-mem-id")


# ---------------------------------------------------------------------------
# get_anthropic_client tests
# ---------------------------------------------------------------------------

class TestGetAnthropicClient:
    def test_returns_none_when_no_token(self, monkeypatch):
        monkeypatch.setattr(client_mod, "get_access_token", MagicMock(return_value=None))
        monkeypatch.setattr(client_mod, "ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(client_mod, "oauth_init", MagicMock(return_value=True))

        result = asyncio.get_event_loop().run_until_complete(
            client_mod.get_anthropic_client()
        )
        assert result is None

    def test_builds_client_on_first_call(self, monkeypatch):
        monkeypatch.setattr(client_mod, "get_access_token", MagicMock(return_value="tok-1"))
        monkeypatch.setattr(client_mod, "ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(client_mod, "oauth_init", MagicMock(return_value=True))

        mock_client = MagicMock()
        mock_cls = MagicMock(return_value=mock_client)

        with patch.dict("sys.modules", {"anthropic": types.SimpleNamespace(AsyncAnthropic=mock_cls)}):
            result = asyncio.get_event_loop().run_until_complete(
                client_mod.get_anthropic_client()
            )

        assert result is mock_client
        mock_cls.assert_called_once_with(auth_token="tok-1")

    def test_returns_cached_client_on_same_token(self, monkeypatch):
        monkeypatch.setattr(client_mod, "get_access_token", MagicMock(return_value="tok-same"))
        monkeypatch.setattr(client_mod, "ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(client_mod, "oauth_init", MagicMock(return_value=True))

        mock_client = MagicMock()
        client_mod._async_anthropic_client = mock_client
        client_mod._async_anthropic_token = "tok-same"

        result = asyncio.get_event_loop().run_until_complete(
            client_mod.get_anthropic_client()
        )
        assert result is mock_client

    def test_rebuilds_client_on_token_rotation(self, monkeypatch):
        monkeypatch.setattr(client_mod, "get_access_token", MagicMock(return_value="tok-new"))
        monkeypatch.setattr(client_mod, "ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(client_mod, "oauth_init", MagicMock(return_value=True))

        old_client = MagicMock()
        client_mod._async_anthropic_client = old_client
        client_mod._async_anthropic_token = "tok-old"

        new_client = MagicMock()
        mock_cls = MagicMock(return_value=new_client)

        with patch.dict("sys.modules", {"anthropic": types.SimpleNamespace(AsyncAnthropic=mock_cls)}):
            result = asyncio.get_event_loop().run_until_complete(
                client_mod.get_anthropic_client()
            )

        assert result is new_client
        assert client_mod._async_anthropic_token == "tok-new"
