"""Unit tests for services/mem0/client.py — all external I/O is mocked."""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy deps that client.py imports at module level or lazily
# ---------------------------------------------------------------------------

# Dependency replacements stay inside fixtures or test bodies. Collection
# imports the real application module without mutating sys.modules or the
# oauth_manager module shared by other test files.

# anthropic.AsyncAnthropic — real anthropic is installed (claude_code_sdk
# depends on it), so just leave the real module in place.

# Now import the module under test.
import services.mem0.client as client_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _reset_singletons():
    """Reset all module-level singletons between tests."""
    client_mod._mem0_instance = None
    client_mod._init_failed_at = None
    client_mod._last_token = None
    client_mod._async_anthropic_client = None
    client_mod._async_anthropic_token = None


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    _reset_singletons()
    # Reset oauth mocks to clean state
    monkeypatch.setattr(client_mod, "ensure_valid_token", AsyncMock(return_value=None))
    monkeypatch.setattr(
        client_mod, "get_access_token", MagicMock(return_value="test-token-abc")
    )
    monkeypatch.setattr(client_mod, "oauth_init", MagicMock(return_value=True))
    # Default tests to embedded mode so they don't hit the HTTP heartbeat.
    # Tests that exercise server-mode init must override this.
    monkeypatch.setenv("CHROMA_MODE", "embedded")
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

        with patch.dict(
            "sys.modules", {"mem0": types.SimpleNamespace(Memory=mock_memory_cls)}
        ):
            result = client_mod._get_mem0()

        assert result is mock_memory

    def test_returns_none_when_mem0_raises(self, monkeypatch, tmp_path):
        """_get_mem0 returns None if Memory.from_config raises."""
        mock_memory_cls = MagicMock()
        mock_memory_cls.from_config = MagicMock(side_effect=RuntimeError("chroma fail"))

        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))

        with patch.dict(
            "sys.modules", {"mem0": types.SimpleNamespace(Memory=mock_memory_cls)}
        ):
            result = client_mod._get_mem0()

        assert result is None

    def test_singleton_caches_successful_init(self, monkeypatch, tmp_path):
        """Second call to _get_mem0 returns cached instance without re-init."""
        mock_memory = MagicMock()
        mock_memory_cls = MagicMock()
        mock_memory_cls.from_config = MagicMock(return_value=mock_memory)

        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))

        with patch.dict(
            "sys.modules", {"mem0": types.SimpleNamespace(Memory=mock_memory_cls)}
        ):
            r1 = client_mod._get_mem0()
            r2 = client_mod._get_mem0()

        assert r1 is r2
        assert mock_memory_cls.from_config.call_count == 1

    def test_failure_triggers_cooldown_no_retry_within_window(
        self, monkeypatch, tmp_path
    ):
        """After failed init, calls within cooldown window return None without re-init."""
        mock_memory_cls = MagicMock()
        mock_memory_cls.from_config = MagicMock(side_effect=Exception("boom"))

        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))

        with patch.dict(
            "sys.modules", {"mem0": types.SimpleNamespace(Memory=mock_memory_cls)}
        ):
            r1 = client_mod._get_mem0()
            r2 = client_mod._get_mem0()

        assert r1 is None
        assert r2 is None
        assert mock_memory_cls.from_config.call_count == 1
        assert client_mod._init_failed_at is not None

    def test_failure_retries_after_cooldown_expires(self, monkeypatch, tmp_path):
        """After cooldown expires, _get_mem0 retries init and can succeed."""
        mock_memory = MagicMock()
        mock_memory_cls = MagicMock()
        # First call raises, second call succeeds
        mock_memory_cls.from_config = MagicMock(
            side_effect=[Exception("boom"), mock_memory]
        )

        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))

        with patch.dict(
            "sys.modules", {"mem0": types.SimpleNamespace(Memory=mock_memory_cls)}
        ):
            r1 = client_mod._get_mem0()
            assert r1 is None
            # Force cooldown to have already expired
            client_mod._init_failed_at = (
                client_mod.time.monotonic() - client_mod._INIT_RETRY_COOLDOWN_SEC - 1
            )
            r2 = client_mod._get_mem0()

        assert r2 is mock_memory
        assert mock_memory_cls.from_config.call_count == 2

    def test_server_mode_pings_heartbeat_before_init(self, monkeypatch, tmp_path):
        """In server mode, _get_mem0 pings ChromaDB heartbeat before Memory.from_config."""
        monkeypatch.setenv("CHROMA_MODE", "server")
        monkeypatch.setenv("CHROMA_HOST", "127.0.0.1")
        monkeypatch.setenv("CHROMA_PORT", "8765")
        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_get = MagicMock(return_value=mock_response)

        mock_memory = MagicMock()
        mock_memory_cls = MagicMock()
        mock_memory_cls.from_config = MagicMock(return_value=mock_memory)

        with patch.dict(
            "sys.modules",
            {
                "mem0": types.SimpleNamespace(Memory=mock_memory_cls),
                "requests": types.SimpleNamespace(get=mock_get),
            },
        ):
            result = client_mod._get_mem0()

        assert result is mock_memory
        mock_get.assert_called_once()
        url = mock_get.call_args[0][0]
        assert "127.0.0.1:8765" in url
        assert "/api/v2/heartbeat" in url

    def test_server_mode_heartbeat_failure_marks_init_failed(
        self, monkeypatch, tmp_path
    ):
        """If heartbeat fails, init is marked failed without attempting Memory.from_config."""
        monkeypatch.setenv("CHROMA_MODE", "server")
        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))

        mock_get = MagicMock(side_effect=ConnectionError("refused"))
        mock_memory_cls = MagicMock()
        mock_memory_cls.from_config = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "mem0": types.SimpleNamespace(Memory=mock_memory_cls),
                "requests": types.SimpleNamespace(get=mock_get),
            },
        ):
            result = client_mod._get_mem0()

        assert result is None
        assert client_mod._init_failed_at is not None
        mock_memory_cls.from_config.assert_not_called()

    def test_oauth_init_failure_logs_error(self, monkeypatch, tmp_path, caplog):
        """_get_mem0 logs an error when oauth_init returns False."""
        monkeypatch.setattr(client_mod, "oauth_init", MagicMock(return_value=False))

        mock_memory = MagicMock()
        mock_memory_cls = MagicMock()
        mock_memory_cls.from_config = MagicMock(return_value=mock_memory)
        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))

        import logging

        with caplog.at_level(logging.ERROR, logger="services.mem0.client"):
            with patch.dict(
                "sys.modules", {"mem0": types.SimpleNamespace(Memory=mock_memory_cls)}
            ):
                client_mod._get_mem0()

        assert any("OAuth init failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _refresh_mem0_client_token tests
# ---------------------------------------------------------------------------


class TestRefreshMem0ClientToken:
    def test_no_op_when_no_instance(self, monkeypatch):
        """Does nothing if _mem0_instance is None."""
        monkeypatch.setattr(
            client_mod, "get_access_token", MagicMock(return_value="new-token")
        )
        client_mod._mem0_instance = None
        client_mod._last_token = None
        client_mod._refresh_mem0_client_token()  # must not raise

    def test_no_op_when_token_unchanged(self, monkeypatch):
        """Does not update when token is the same as _last_token."""
        token = "same-token"
        monkeypatch.setattr(
            client_mod, "get_access_token", MagicMock(return_value=token)
        )
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
        monkeypatch.setattr(
            client_mod, "get_access_token", MagicMock(return_value="new-token")
        )
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
        monkeypatch.setattr(
            client_mod, "get_access_token", MagicMock(return_value="new-token")
        )
        client_mod._last_token = "old-token"

        bad_llm = MagicMock()
        bad_llm.client = MagicMock(spec=[])  # no auth_token attr
        client_mod._mem0_instance = MagicMock(llm=bad_llm)

        client_mod._refresh_mem0_client_token()  # must not raise

    def test_no_op_when_token_is_none(self, monkeypatch):
        """Does nothing if get_access_token returns None."""
        monkeypatch.setattr(
            client_mod, "get_access_token", MagicMock(return_value=None)
        )
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

    def test_default_collection_name(self, monkeypatch, tmp_path):
        """Default collection is kanae_memories_safe (post-SEGV rebuilt data)."""
        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))
        monkeypatch.delenv("MEM0_COLLECTION_NAME", raising=False)
        cfg = client_mod._build_config()
        assert cfg["vector_store"]["config"]["collection_name"] == "kanae_memories_safe"

    def test_collection_name_env_override(self, monkeypatch, tmp_path):
        """MEM0_COLLECTION_NAME env swaps the target collection (e.g. infer rebuild)."""
        monkeypatch.setattr("config.STATE_DIR", str(tmp_path))
        monkeypatch.setenv("MEM0_COLLECTION_NAME", "kanae_memories_infer")
        cfg = client_mod._build_config()
        assert (
            cfg["vector_store"]["config"]["collection_name"] == "kanae_memories_infer"
        )


# ---------------------------------------------------------------------------
# get_all_memories tests
# ---------------------------------------------------------------------------


class TestGetAllMemories:
    async def test_returns_list_when_result_is_dict(self, monkeypatch, tmp_path):
        mock_mem = MagicMock()
        mock_mem.get_all = MagicMock(return_value={"results": [{"id": "1"}]})
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        result = await client_mod.get_all_memories("user123")
        assert result == [{"id": "1"}]

    async def test_returns_list_when_result_is_list(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.get_all = MagicMock(return_value=[{"id": "2"}])
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        result = await client_mod.get_all_memories("user123")
        assert result == [{"id": "2"}]

    async def test_returns_empty_when_mem0_is_none(self, monkeypatch):
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=None))

        result = await client_mod.get_all_memories("user123")
        assert result == []

    async def test_returns_empty_on_exception(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.get_all = MagicMock(side_effect=RuntimeError("db error"))
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        result = await client_mod.get_all_memories("user123")
        assert result == []

    async def test_passes_limit_kwarg(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.get_all = MagicMock(return_value={"results": []})
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        await client_mod.get_all_memories("user123", limit=50)
        mock_mem.get_all.assert_called_once_with(user_id="user123", limit=50)

    async def test_no_limit_kwarg_when_none(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.get_all = MagicMock(return_value={"results": []})
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        await client_mod.get_all_memories("user123", limit=None)
        call_kwargs = mock_mem.get_all.call_args[1]
        assert "limit" not in call_kwargs

    async def test_returns_empty_when_result_is_unexpected_type(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.get_all = MagicMock(return_value=42)
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        result = await client_mod.get_all_memories("user123")
        assert result == []


# ---------------------------------------------------------------------------
# delete_memory tests
# ---------------------------------------------------------------------------


class TestDeleteMemory:
    async def test_returns_true_on_success(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.delete = MagicMock(return_value=None)
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        result = await client_mod.delete_memory("mem-id-123")
        assert result is True

    async def test_returns_false_when_mem0_is_none(self, monkeypatch):
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=None))

        result = await client_mod.delete_memory("mem-id-123")
        assert result is False

    async def test_returns_false_on_exception(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.delete = MagicMock(side_effect=RuntimeError("delete fail"))
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        result = await client_mod.delete_memory("mem-id-123")
        assert result is False

    async def test_calls_delete_with_correct_id(self, monkeypatch):
        mock_mem = MagicMock()
        mock_mem.delete = MagicMock(return_value=None)
        monkeypatch.setattr(client_mod, "_get_mem0", MagicMock(return_value=mock_mem))

        await client_mod.delete_memory("specific-mem-id")
        mock_mem.delete.assert_called_once_with("specific-mem-id")


# ---------------------------------------------------------------------------
# get_anthropic_client tests
# ---------------------------------------------------------------------------


class TestGetAnthropicClient:
    async def test_returns_none_when_no_token(self, monkeypatch):
        monkeypatch.setattr(
            client_mod, "get_access_token", MagicMock(return_value=None)
        )
        monkeypatch.setattr(
            client_mod, "ensure_valid_token", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(client_mod, "oauth_init", MagicMock(return_value=True))
        # "No auth at all" means no OAuth token AND no fallback keys. The real
        # .env ships an OPENCODE_API_KEY, so clear both module-level fallback
        # keys to isolate this test from the environment (otherwise the
        # OpenCode/OpenRouter fallback correctly returns an AsyncOpenAI).
        monkeypatch.setattr(client_mod, "_OPENROUTER_API_KEY", "")
        monkeypatch.setattr(client_mod, "_OPENCODE_API_KEY", "")

        result = await client_mod.get_anthropic_client()
        assert result is None

    async def test_builds_client_on_first_call(self, monkeypatch):
        monkeypatch.setattr(
            client_mod, "get_access_token", MagicMock(return_value="tok-1")
        )
        monkeypatch.setattr(
            client_mod, "ensure_valid_token", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(client_mod, "oauth_init", MagicMock(return_value=True))

        mock_client = MagicMock()
        mock_cls = MagicMock(return_value=mock_client)

        with patch.dict(
            "sys.modules", {"anthropic": types.SimpleNamespace(AsyncAnthropic=mock_cls)}
        ):
            result = await client_mod.get_anthropic_client()

        assert result is mock_client
        mock_cls.assert_called_once_with(auth_token="tok-1", max_retries=0)

    async def test_returns_cached_client_on_same_token(self, monkeypatch):
        monkeypatch.setattr(
            client_mod, "get_access_token", MagicMock(return_value="tok-same")
        )
        monkeypatch.setattr(
            client_mod, "ensure_valid_token", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(client_mod, "oauth_init", MagicMock(return_value=True))

        mock_client = MagicMock()
        client_mod._async_anthropic_client = mock_client
        client_mod._async_anthropic_token = "tok-same"

        result = await client_mod.get_anthropic_client()
        assert result is mock_client

    async def test_rebuilds_client_on_token_rotation(self, monkeypatch):
        monkeypatch.setattr(
            client_mod, "get_access_token", MagicMock(return_value="tok-new")
        )
        monkeypatch.setattr(
            client_mod, "ensure_valid_token", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(client_mod, "oauth_init", MagicMock(return_value=True))

        old_client = MagicMock()
        client_mod._async_anthropic_client = old_client
        client_mod._async_anthropic_token = "tok-old"

        new_client = MagicMock()
        mock_cls = MagicMock(return_value=new_client)

        with patch.dict(
            "sys.modules", {"anthropic": types.SimpleNamespace(AsyncAnthropic=mock_cls)}
        ):
            result = await client_mod.get_anthropic_client()

        assert result is new_client
        assert client_mod._async_anthropic_token == "tok-new"


@pytest.mark.asyncio
async def test_revoked_oauth_does_not_block_openai_compatible_fallback(monkeypatch):
    from services.oauth_manager import OAuthRefreshFailed

    monkeypatch.setattr(client_mod, "ensure_valid_token", AsyncMock(side_effect=OAuthRefreshFailed("revoked")))
    monkeypatch.setattr(client_mod, "get_access_token", lambda: None)
    monkeypatch.setattr(client_mod, "_desired_llm_provider", lambda: "opencode")
    expected = object()
    factory = MagicMock(return_value=expected)
    with patch("openai.AsyncOpenAI", factory):
        assert await client_mod.get_anthropic_client() is expected
    assert factory.call_args.kwargs["base_url"] == "https://api.opencode.ai/v1"


@pytest.mark.asyncio
async def test_memory_refresh_preserves_cancellation(monkeypatch):
    monkeypatch.setattr(client_mod, "ensure_valid_token", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await client_mod._refresh_token_if_available()
