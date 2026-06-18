"""Unit tests for services/claude_api/client_factory.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# build_cli_headers
# ---------------------------------------------------------------------------
class TestBuildCliHeaders:
    def test_build_cli_headers_contains_required_keys(self):
        from services.claude_api.client_factory import build_cli_headers

        headers = build_cli_headers()

        required_keys = {
            "x-app",
            "User-Agent",
            "X-Claude-Code-Session-Id",
            "X-Stainless-Lang",
            "X-Stainless-Package-Version",
            "X-Stainless-Runtime",
            "X-Stainless-Runtime-Version",
            "X-Stainless-OS",
            "X-Stainless-Arch",
            "X-Stainless-Timeout",
            "accept-language",
            "sec-fetch-mode",
            "x-client-request-id",
        }
        assert required_keys <= headers.keys()

    def test_build_cli_headers_x_app_is_cli(self):
        from services.claude_api.client_factory import build_cli_headers

        assert build_cli_headers()["x-app"] == "cli"

    def test_build_cli_headers_stainless_lang_is_js(self):
        from services.claude_api.client_factory import build_cli_headers

        assert build_cli_headers()["X-Stainless-Lang"] == "js"

    def test_build_cli_headers_unique_request_id(self):
        """Two successive calls must produce different x-client-request-id values."""
        from services.claude_api.client_factory import build_cli_headers

        first = build_cli_headers()["x-client-request-id"]
        second = build_cli_headers()["x-client-request-id"]
        assert first != second

    def test_build_cli_headers_request_id_is_valid_uuid(self):
        import uuid

        from services.claude_api.client_factory import build_cli_headers

        request_id = build_cli_headers()["x-client-request-id"]
        # Should not raise
        parsed = uuid.UUID(request_id)
        assert str(parsed) == request_id


# ---------------------------------------------------------------------------
# make_api_client
# ---------------------------------------------------------------------------
class TestMakeApiClient:
    async def test_make_api_client_restricted_with_key(self, monkeypatch):
        """When restricted=True and RESTRICTED_ANTHROPIC_API_KEY is set,
        make_async_api_key_client is called, OAuth flow is skipped."""
        import services.claude_api.client_factory as mod

        fake_client = MagicMock()
        fake_key_client = MagicMock(return_value=fake_client)

        monkeypatch.setattr(mod, "RESTRICTED_ANTHROPIC_API_KEY", "sk-test-restricted")
        monkeypatch.setattr(mod, "make_async_api_key_client", fake_key_client)

        mock_oauth_init = MagicMock()
        mock_ensure = AsyncMock()
        mock_prime = AsyncMock()
        mock_oauth_client = MagicMock()
        monkeypatch.setattr(mod, "oauth_init", mock_oauth_init)
        monkeypatch.setattr(mod, "ensure_valid_token", mock_ensure)
        monkeypatch.setattr(mod, "prime_subscriber_session", mock_prime)
        monkeypatch.setattr(mod, "make_async_oauth_client", mock_oauth_client)

        result = await mod.make_api_client(restricted=True)

        assert result is fake_client
        fake_key_client.assert_called_once()
        call_kwargs = fake_key_client.call_args
        assert call_kwargs.args[0] == "sk-test-restricted"
        assert "default_headers" in call_kwargs.kwargs

        mock_oauth_init.assert_not_called()
        mock_ensure.assert_not_called()
        mock_prime.assert_not_called()
        mock_oauth_client.assert_not_called()

    async def test_make_api_client_restricted_no_key_falls_through_to_oauth(
        self, monkeypatch
    ):
        """When restricted=True but no RESTRICTED_ANTHROPIC_API_KEY, use OAuth."""
        import services.claude_api.client_factory as mod

        fake_client = MagicMock()

        monkeypatch.setattr(mod, "RESTRICTED_ANTHROPIC_API_KEY", "")
        monkeypatch.setattr(mod, "make_async_api_key_client", MagicMock())

        mock_oauth_init = MagicMock()
        mock_ensure = AsyncMock()
        mock_prime = AsyncMock()
        mock_oauth_client = MagicMock(return_value=fake_client)
        monkeypatch.setattr(mod, "oauth_init", mock_oauth_init)
        monkeypatch.setattr(mod, "ensure_valid_token", mock_ensure)
        monkeypatch.setattr(mod, "prime_subscriber_session", mock_prime)
        monkeypatch.setattr(mod, "make_async_oauth_client", mock_oauth_client)

        result = await mod.make_api_client(restricted=True)

        assert result is fake_client
        mock_oauth_init.assert_called_once()
        mock_ensure.assert_awaited_once()
        mock_prime.assert_awaited_once()
        mock_oauth_client.assert_called_once()

    async def test_make_api_client_oauth_flow(self, monkeypatch):
        """Non-restricted path calls oauth_init, ensure_valid_token,
        prime_subscriber_session in that order, then make_async_oauth_client."""
        import services.claude_api.client_factory as mod

        call_order: list[str] = []
        fake_client = MagicMock()

        def _oauth_init():
            call_order.append("oauth_init")

        async def _ensure():
            call_order.append("ensure_valid_token")

        async def _prime():
            call_order.append("prime_subscriber_session")

        def _oauth_client(**kwargs):
            call_order.append("make_async_oauth_client")
            return fake_client

        monkeypatch.setattr(mod, "RESTRICTED_ANTHROPIC_API_KEY", "")
        monkeypatch.setattr(mod, "make_async_api_key_client", MagicMock())
        monkeypatch.setattr(mod, "oauth_init", _oauth_init)
        monkeypatch.setattr(mod, "ensure_valid_token", _ensure)
        monkeypatch.setattr(mod, "prime_subscriber_session", _prime)
        monkeypatch.setattr(mod, "make_async_oauth_client", _oauth_client)

        result = await mod.make_api_client(restricted=False)

        assert result is fake_client
        assert call_order == [
            "oauth_init",
            "ensure_valid_token",
            "prime_subscriber_session",
            "make_async_oauth_client",
        ]

    async def test_make_api_client_oauth_receives_default_headers(self, monkeypatch):
        """OAuth client is constructed with the CLI headers."""
        import services.claude_api.client_factory as mod

        captured_kwargs: dict = {}

        def _oauth_client(**kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(mod, "RESTRICTED_ANTHROPIC_API_KEY", "")
        monkeypatch.setattr(mod, "make_async_api_key_client", MagicMock())
        monkeypatch.setattr(mod, "oauth_init", MagicMock())
        monkeypatch.setattr(mod, "ensure_valid_token", AsyncMock())
        monkeypatch.setattr(mod, "prime_subscriber_session", AsyncMock())
        monkeypatch.setattr(mod, "make_async_oauth_client", _oauth_client)

        await mod.make_api_client(restricted=False)

        assert "default_headers" in captured_kwargs
        assert "x-app" in captured_kwargs["default_headers"]
        assert "x-client-request-id" in captured_kwargs["default_headers"]
