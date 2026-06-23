"""Unit tests for services/oauth_manager.py.

Covers the functions not yet exercised by test_claude_api.py (TestTokenExpiry)
and test_e2e_security.py (TestOAuthPathPrivacy / test_save_credentials_refuses_insecure_dir).

Already-covered in other suites (not duplicated here):
  - _is_expired(): 4 cases in test_claude_api.py::TestTokenExpiry
  - _verify_path_privacy(): 4 cases + 1 save_credentials case in test_e2e_security.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Autouse fixture: reset module-level globals before every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_oauth_globals(monkeypatch):
    """Reset services.oauth_manager module globals before each test.

    Some test files (e.g. test_mem0_client.py) inject a fake module into
    sys.modules["services.oauth_manager"] at import time.  When those tests
    run first the real module is replaced and subsequent tests see a stub
    without the expected attributes.  We force-reload the real module here
    and re-register it in sys.modules so every test in this file always
    operates on the genuine implementation.

    monkeypatch.setattr is used for all globals so changes are rolled back
    automatically at teardown — even direct assignments like
    ``om._access_token = "new-access"`` made inside test bodies.
    """
    # Force the real module to be loaded (not a stub left by another test file).
    import services.oauth_manager as om
    real_mod = importlib.import_module("services.oauth_manager")
    # If what's in sys.modules is NOT the real module (no _access_token attr),
    # reload to get the genuine implementation.
    if not hasattr(real_mod, "_access_token"):
        real_mod = importlib.reload(
            importlib.import_module("services.oauth_manager")
        )
    # Ensure sys.modules always points to the real module for the duration.
    original_module = sys.modules.get("services.oauth_manager")
    sys.modules["services.oauth_manager"] = real_mod

    monkeypatch.setattr(real_mod, "_access_token", None)
    monkeypatch.setattr(real_mod, "_refresh_token", None)
    monkeypatch.setattr(real_mod, "_expires_at", 0)
    monkeypatch.setattr(real_mod, "_http_session", None)
    monkeypatch.setattr(real_mod, "_patched", False)
    monkeypatch.setattr(real_mod, "_refresh_failure_count", 0)
    monkeypatch.setattr(real_mod, "_refresh_permanent", False)

    yield

    # Restore whatever was in sys.modules before this fixture ran.
    if original_module is not None:
        sys.modules["services.oauth_manager"] = original_module
    else:
        sys.modules.pop("services.oauth_manager", None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_creds_dir(tmp_path):
    """Create a private (0o700) credentials directory and return its path."""
    d = tmp_path / ".claude"
    d.mkdir()
    os.chmod(d, 0o700)
    return d


def _make_creds_file(creds_dir, data: dict):
    """Write credentials JSON to a private (0o600) file."""
    f = creds_dir / ".credentials.json"
    f.write_text(json.dumps(data))
    os.chmod(f, 0o600)
    return f


# ---------------------------------------------------------------------------
# _verify_path_privacy – uid mismatch branch
# ---------------------------------------------------------------------------

class TestVerifyPathPrivacyUidMismatch:
    def test_rejects_wrong_owner(self, tmp_path, monkeypatch):
        """Return False when the path is owned by a different uid."""
        from services.oauth_manager import _verify_path_privacy
        import os as _os

        f = tmp_path / "foreign.json"
        f.write_text("{}")
        os.chmod(f, 0o600)

        # Pretend we are a different uid than the file owner.
        monkeypatch.setattr(_os, "getuid", lambda: os.stat(f).st_uid + 1)
        assert _verify_path_privacy(str(f), is_dir=False) is False

    def test_returns_false_on_oserror(self, monkeypatch):
        """OSError (other than FileNotFoundError) from lstat returns False."""
        from services.oauth_manager import _verify_path_privacy
        import os as _os

        def bad_lstat(path):
            raise OSError("permission denied")

        monkeypatch.setattr(_os, "lstat", bad_lstat)
        assert _verify_path_privacy("/some/path", is_dir=False) is False


# ---------------------------------------------------------------------------
# _read_credentials
# ---------------------------------------------------------------------------

class TestReadCredentials:
    def test_reads_valid_file(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(creds_dir, {
            "claudeAiOauth": {
                "accessToken": "tok-123",
                "refreshToken": "ref-456",
                "expiresAt": 9999999999999,
            }
        })
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))

        result = om._read_credentials()
        assert result is not None
        assert result["accessToken"] == "tok-123"

    def test_returns_none_on_missing_file(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        missing = tmp_path / "nope.json"
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(missing))

        result = om._read_credentials()
        assert result is None

    def test_returns_none_on_invalid_json(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        bad_file = creds_dir / ".credentials.json"
        bad_file.write_text("not json{{")
        os.chmod(bad_file, 0o600)
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(bad_file))

        result = om._read_credentials()
        assert result is None

    def test_returns_none_when_key_absent(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(creds_dir, {"someOtherKey": {}})
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))

        result = om._read_credentials()
        assert result is None


# ---------------------------------------------------------------------------
# _save_credentials round-trip
# ---------------------------------------------------------------------------

class TestSaveCredentials:
    def test_roundtrip(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = creds_dir / ".credentials.json"
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))

        om._save_credentials("access-t", "refresh-t", 12345678, extra={"subscriptionType": "pro"})

        assert creds_file.exists()
        mode = oct(os.stat(creds_file).st_mode & 0o777)
        assert mode == oct(0o600), f"Expected 0o600, got {mode}"

        data = json.loads(creds_file.read_text())
        oauth = data["claudeAiOauth"]
        assert oauth["accessToken"] == "access-t"
        assert oauth["refreshToken"] == "refresh-t"
        assert oauth["expiresAt"] == 12345678
        assert oauth["subscriptionType"] == "pro"

    def test_updates_existing_file(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(creds_dir, {
            "claudeAiOauth": {"accessToken": "old", "refreshToken": "old-ref", "expiresAt": 1}
        })
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))

        om._save_credentials("new-access", "new-refresh", 99999)

        data = json.loads(creds_file.read_text())
        assert data["claudeAiOauth"]["accessToken"] == "new-access"

    def test_refuses_insecure_file(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = creds_dir / ".credentials.json"
        creds_file.write_text("{}")
        os.chmod(creds_file, 0o644)  # world-readable — must be refused
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))

        original_content = creds_file.read_text()
        om._save_credentials("a", "r", 9999)
        # Content must be unchanged (write refused)
        assert creds_file.read_text() == original_content

    def test_refuses_insecure_dir(self, tmp_path, monkeypatch):
        """Lines 157-158: refuse when the credentials dir itself is not private."""
        import services.oauth_manager as om

        insecure_dir = tmp_path / ".claude"
        insecure_dir.mkdir()
        os.chmod(insecure_dir, 0o755)  # group/other readable — must be refused
        creds_file = insecure_dir / ".credentials.json"
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))

        # File does not exist yet; the dir check must fire first and return early.
        om._save_credentials("a", "r", 9999)
        assert not creds_file.exists()

    def test_atomic_write_cleans_up_tmp_on_error(self, tmp_path, monkeypatch):
        """Lines 187-192: BaseException during os.replace cleans up the tempfile."""
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = creds_dir / ".credentials.json"
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))

        def bad_replace(src, dst):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(os, "replace", bad_replace)

        # Should not raise — the outer except catches it.
        om._save_credentials("a", "r", 9999)

        # The tempfile must have been cleaned up.
        remaining_tmps = list(creds_dir.glob("*.tmp"))
        assert remaining_tmps == [], f"Leftover tempfiles: {remaining_tmps}"

    def test_atomic_write_unlink_oserror_is_swallowed(self, tmp_path, monkeypatch):
        """Lines 190-191: OSError from os.unlink inside cleanup is silently ignored."""
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = creds_dir / ".credentials.json"
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))

        def bad_replace(src, dst):
            raise OSError("replace failed")

        def bad_unlink(path):
            raise OSError("unlink also failed")

        monkeypatch.setattr(os, "replace", bad_replace)
        monkeypatch.setattr(os, "unlink", bad_unlink)

        # Both os.replace and os.unlink fail — outer except must still swallow it.
        om._save_credentials("a", "r", 9999)  # must not raise


# ---------------------------------------------------------------------------
# refresh_token
# ---------------------------------------------------------------------------

class TestRefreshToken:
    @pytest.mark.asyncio
    async def test_success(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_refresh_token", "ref-tok")
        monkeypatch.setattr(om, "_access_token", None)

        future_expires = int(time.time() * 1000) + 3600_000

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        })

        mock_session = MagicMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_cm)

        monkeypatch.setattr(om, "_save_credentials", lambda *a, **kw: None)

        async def fake_get_session():
            return mock_session

        monkeypatch.setattr(om, "_get_session", fake_get_session)

        result = await om.refresh_token()

        assert result is True
        assert om._access_token == "new-access"
        assert om._refresh_token == "new-refresh"
        assert om._expires_at > 0

    @pytest.mark.asyncio
    async def test_no_refresh_token_returns_false(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_refresh_token", None)

        result = await om.refresh_token()
        assert result is False

    @pytest.mark.asyncio
    async def test_http_error_returns_false(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_refresh_token", "ref-tok")

        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.text = AsyncMock(return_value="Unauthorized")

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_cm)

        async def fake_get_session():
            return mock_session

        monkeypatch.setattr(om, "_get_session", fake_get_session)

        result = await om.refresh_token()
        assert result is False

    @pytest.mark.asyncio
    async def test_network_exception_returns_false(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_refresh_token", "ref-tok")

        async def fake_get_session():
            raise RuntimeError("network down")

        monkeypatch.setattr(om, "_get_session", fake_get_session)

        result = await om.refresh_token()
        assert result is False


# ---------------------------------------------------------------------------
# ensure_valid_token
# ---------------------------------------------------------------------------

class TestEnsureValidToken:
    @pytest.mark.asyncio
    async def test_returns_token_when_not_expired(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_access_token", "valid-tok")
        monkeypatch.setattr(om, "_expires_at", int(time.time() * 1000) + 600_000)

        result = await om.ensure_valid_token()
        assert result == "valid-tok"

    @pytest.mark.asyncio
    async def test_refreshes_when_expired(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_access_token", "old-tok")
        monkeypatch.setattr(om, "_expires_at", 0)  # expired
        monkeypatch.setattr(om, "_refresh_token", "ref-tok")

        async def fake_refresh():
            om._access_token = "refreshed-tok"
            return True

        monkeypatch.setattr(om, "refresh_token", fake_refresh)

        result = await om.ensure_valid_token()
        assert result == "refreshed-tok"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_token_no_refresh(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_access_token", None)
        monkeypatch.setattr(om, "_expires_at", 0)
        monkeypatch.setattr(om, "_refresh_token", None)

        result = await om.ensure_valid_token()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_old_token_if_refresh_fails(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_access_token", "stale-tok")
        monkeypatch.setattr(om, "_expires_at", 0)
        monkeypatch.setattr(om, "_refresh_token", "ref-tok")

        async def fake_refresh():
            return False

        monkeypatch.setattr(om, "refresh_token", fake_refresh)

        result = await om.ensure_valid_token()
        # Falls through to return _access_token regardless
        assert result == "stale-tok"


# ---------------------------------------------------------------------------
# get_access_token
# ---------------------------------------------------------------------------

class TestGetAccessToken:
    def test_returns_none_by_default(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_access_token", None)
        assert om.get_access_token() is None

    def test_returns_current_token(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_access_token", "test-access-token")
        assert om.get_access_token() == "test-access-token"


# ---------------------------------------------------------------------------
# _patch_anthropic_sdk
# ---------------------------------------------------------------------------

class TestPatchAnthropicSdk:
    def test_patches_init(self, monkeypatch):
        import services.oauth_manager as om

        # Reset patch state so each test is independent.
        monkeypatch.setattr(om, "_patched", False)
        monkeypatch.setattr(om, "_access_token", "bearer-tok")

        # Build a minimal fake anthropic module with an Anthropic class.
        init_calls = []

        class FakeAnthropic:
            def __init__(self, **kwargs):
                init_calls.append(kwargs)

        import types
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.Anthropic = FakeAnthropic

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            # Force re-import inside the function by temporarily replacing module.
            import importlib
            # Call with the fake module already in sys.modules.
            om._patched = False  # ensure it runs
            # We need _patch_anthropic_sdk to import 'anthropic' from sys.modules.
            om._patch_anthropic_sdk()

        assert om._patched is True
        # Now verify the patched __init__ injects auth_token and drops api_key.
        instance = FakeAnthropic.__new__(FakeAnthropic)
        FakeAnthropic.__init__(instance, api_key="should-be-dropped")
        last_call = init_calls[-1]
        assert "api_key" not in last_call
        assert last_call.get("auth_token") == "bearer-tok"

    def test_skips_when_already_patched(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_patched", True)
        # Should return without doing anything (no import attempted).
        om._patch_anthropic_sdk()  # must not raise
        assert om._patched is True

    def test_handles_import_error(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_patched", False)
        with patch.dict("sys.modules", {"anthropic": None}):
            # When anthropic is None in sys.modules, import raises ImportError.
            om._patch_anthropic_sdk()
        # _patched should remain False since we couldn't patch.
        assert om._patched is False

    def test_merges_existing_beta_header(self, monkeypatch):
        """When default_headers already has anthropic-beta, the OAuth beta is appended."""
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_patched", False)
        monkeypatch.setattr(om, "_access_token", "tok")

        init_calls = []

        class FakeAnthropic:
            def __init__(self, **kwargs):
                init_calls.append(kwargs)

        import types
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.Anthropic = FakeAnthropic

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            om._patched = False
            om._patch_anthropic_sdk()

        # Call patched init with an existing beta header that differs from our OAuth one.
        instance = FakeAnthropic.__new__(FakeAnthropic)
        FakeAnthropic.__init__(
            instance,
            default_headers={"anthropic-beta": "other-beta-2025-01-01"},
        )
        last_call = init_calls[-1]
        beta = last_call["default_headers"]["anthropic-beta"]
        assert "other-beta-2025-01-01" in beta
        assert om.OAUTH_BETA_HEADER in beta

    def test_no_duplicate_beta_header(self, monkeypatch):
        """If our OAuth beta is already present, it must not be duplicated."""
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_patched", False)
        monkeypatch.setattr(om, "_access_token", "tok")

        init_calls = []

        class FakeAnthropic2:
            def __init__(self, **kwargs):
                init_calls.append(kwargs)

        import types
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.Anthropic = FakeAnthropic2

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            om._patched = False
            om._patch_anthropic_sdk()

        instance = FakeAnthropic2.__new__(FakeAnthropic2)
        FakeAnthropic2.__init__(
            instance,
            default_headers={"anthropic-beta": om.OAUTH_BETA_HEADER},
        )
        last_call = init_calls[-1]
        beta = last_call["default_headers"]["anthropic-beta"]
        # Must not be doubled.
        assert beta.count(om.OAUTH_BETA_HEADER) == 1


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

class TestInit:
    def test_returns_true_with_valid_creds(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        expires_future = int(time.time() * 1000) + 3600_000
        creds_file = _make_creds_file(creds_dir, {
            "claudeAiOauth": {
                "accessToken": "valid-tok",
                "refreshToken": "ref-tok",
                "expiresAt": expires_future,
                "subscriptionType": "pro_claude_ai",
            }
        })
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))
        monkeypatch.setattr(om, "_patched", True)  # skip SDK patch side-effect

        result = om.init()

        assert result is True
        assert om._access_token == "valid-tok"
        assert om._refresh_token == "ref-tok"

    def test_returns_false_when_no_creds_file(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        missing = tmp_path / "nope.json"
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(missing))

        result = om.init()
        assert result is False

    def test_returns_false_when_no_access_token(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(creds_dir, {
            "claudeAiOauth": {
                "refreshToken": "ref-only",
                "expiresAt": 0,
            }
        })
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))

        result = om.init()
        assert result is False

    def test_clears_env_vars(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(creds_dir, {
            "claudeAiOauth": {
                "accessToken": "tok",
                "refreshToken": "ref",
                "expiresAt": int(time.time() * 1000) + 3600_000,
            }
        })
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))
        monkeypatch.setattr(om, "_patched", True)
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "should-be-removed"
        os.environ["ANTHROPIC_API_KEY"] = "should-be-removed"

        om.init()

        assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
        assert "ANTHROPIC_API_KEY" not in os.environ

    def test_expired_token_still_loads(self, tmp_path, monkeypatch):
        """init() returns True even if token is already expired — refresh happens later."""
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(creds_dir, {
            "claudeAiOauth": {
                "accessToken": "expired-tok",
                "refreshToken": "ref-tok",
                "expiresAt": 1,  # far in the past
            }
        })
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))
        monkeypatch.setattr(om, "_patched", True)

        result = om.init()
        assert result is True
        assert om._access_token == "expired-tok"


# ---------------------------------------------------------------------------
# _get_session
# ---------------------------------------------------------------------------

class TestGetSession:
    @pytest.mark.asyncio
    async def test_creates_session(self, monkeypatch):
        import services.oauth_manager as om
        import aiohttp

        monkeypatch.setattr(om, "_http_session", None)

        session = await om._get_session()
        assert isinstance(session, aiohttp.ClientSession)
        await session.close()

    @pytest.mark.asyncio
    async def test_reuses_open_session(self, monkeypatch):
        import services.oauth_manager as om
        import aiohttp

        monkeypatch.setattr(om, "_http_session", None)
        s1 = await om._get_session()
        s2 = await om._get_session()
        assert s1 is s2
        await s1.close()

    @pytest.mark.asyncio
    async def test_creates_new_when_closed(self, monkeypatch):
        import services.oauth_manager as om
        import aiohttp

        closed = aiohttp.ClientSession()
        await closed.close()
        monkeypatch.setattr(om, "_http_session", closed)

        session = await om._get_session()
        assert session is not closed
        await session.close()


# ---------------------------------------------------------------------------
# SRE-C9: refresh failure counter and escalation
# ---------------------------------------------------------------------------

class TestRefreshFailureCounter:
    def test_refresh_failure_logged_at_warning_then_error(self, monkeypatch, caplog):
        import logging
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_refresh_failure_count", 0)

        with caplog.at_level(logging.WARNING, logger="services.oauth_manager"):
            om._record_refresh_failure("err1")
            om._record_refresh_failure("err2")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2

        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="services.oauth_manager"):
            om._record_refresh_failure("err3")

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "consecutive" in errors[0].message

    def test_refresh_success_resets_counter(self, monkeypatch, caplog):
        import logging
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_refresh_failure_count", 2)

        with caplog.at_level(logging.INFO, logger="services.oauth_manager"):
            om._record_refresh_success()

        assert om._refresh_failure_count == 0
        info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("recovered" in r.message for r in info_msgs)

    def test_is_oauth_healthy_reflects_threshold(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_refresh_failure_count", 0)
        assert om.is_oauth_healthy() is True

        monkeypatch.setattr(om, "_refresh_failure_count", 2)
        assert om.is_oauth_healthy() is True

        monkeypatch.setattr(om, "_refresh_failure_count", 3)
        assert om.is_oauth_healthy() is False

    @pytest.mark.asyncio
    async def test_ensure_valid_token_raises_when_expired_and_failing(self, monkeypatch):
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_access_token", "stale-tok")
        monkeypatch.setattr(om, "_expires_at", 0)  # expired
        monkeypatch.setattr(om, "_refresh_token", "ref-tok")
        monkeypatch.setattr(om, "_refresh_failure_count", 3)

        async def fake_refresh():
            return False

        monkeypatch.setattr(om, "refresh_token", fake_refresh)

        with pytest.raises(om.OAuthRefreshFailed):
            await om.ensure_valid_token()


# ---------------------------------------------------------------------------
# invalid_grant permanent-failure handling
# ---------------------------------------------------------------------------

class TestInvalidGrant:
    @pytest.mark.asyncio
    async def test_invalid_grant_json_sets_permanent_flag(self, monkeypatch):
        """Non-200 with JSON {"error": "invalid_grant"} sets _refresh_permanent=True."""
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_refresh_token", "ref-tok")
        monkeypatch.setattr(om, "_refresh_permanent", False)

        mock_resp = AsyncMock()
        mock_resp.status = 400
        mock_resp.text = AsyncMock(return_value='{"error":"invalid_grant","error_description":"Token revoked"}')

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_cm)

        async def fake_get_session():
            return mock_session

        monkeypatch.setattr(om, "_get_session", fake_get_session)

        result = await om.refresh_token()
        assert result is False
        assert om._refresh_permanent is True

    @pytest.mark.asyncio
    async def test_invalid_grant_substring_sets_permanent_flag(self, monkeypatch):
        """Non-200 body containing 'invalid_grant' text sets _refresh_permanent=True."""
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_refresh_token", "ref-tok")
        monkeypatch.setattr(om, "_refresh_permanent", False)

        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.text = AsyncMock(return_value="error=invalid_grant&description=revoked")

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_cm)

        async def fake_get_session():
            return mock_session

        monkeypatch.setattr(om, "_get_session", fake_get_session)

        result = await om.refresh_token()
        assert result is False
        assert om._refresh_permanent is True

    @pytest.mark.asyncio
    async def test_non_invalid_grant_error_does_not_set_permanent(self, monkeypatch):
        """Transient HTTP error (e.g. 503) does NOT set _refresh_permanent."""
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_refresh_token", "ref-tok")
        monkeypatch.setattr(om, "_refresh_permanent", False)

        mock_resp = AsyncMock()
        mock_resp.status = 503
        mock_resp.text = AsyncMock(return_value="Service Unavailable")

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_cm)

        async def fake_get_session():
            return mock_session

        monkeypatch.setattr(om, "_get_session", fake_get_session)

        result = await om.refresh_token()
        assert result is False
        assert om._refresh_permanent is False

    @pytest.mark.asyncio
    async def test_ensure_valid_token_raises_immediately_on_permanent(self, monkeypatch):
        """When _refresh_permanent is True, ensure_valid_token raises without retrying."""
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_access_token", "stale-tok")
        monkeypatch.setattr(om, "_expires_at", 0)  # expired
        monkeypatch.setattr(om, "_refresh_token", "ref-tok")
        monkeypatch.setattr(om, "_refresh_permanent", True)
        monkeypatch.setattr(om, "_refresh_failure_count", 0)  # count below threshold

        refresh_called = []

        async def fake_refresh():
            refresh_called.append(True)
            return False

        monkeypatch.setattr(om, "refresh_token", fake_refresh)

        with pytest.raises(om.OAuthRefreshFailed, match="invalid_grant"):
            await om.ensure_valid_token()

        # refresh_token() must NOT have been called
        assert refresh_called == []

    @pytest.mark.asyncio
    async def test_record_refresh_success_clears_permanent_flag(self, monkeypatch):
        """_record_refresh_success() resets _refresh_permanent to False."""
        import services.oauth_manager as om

        monkeypatch.setattr(om, "_refresh_permanent", True)
        monkeypatch.setattr(om, "_refresh_failure_count", 0)

        om._record_refresh_success()
        assert om._refresh_permanent is False
