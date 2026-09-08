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
from pathlib import Path
import stat
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Autouse fixture: reset module-level globals before every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_oauth_globals(monkeypatch, tmp_path):
    """Reset services.oauth_manager module globals before each test.

    Some test files (e.g. test_mem0_client.py) inject a fake module into
    sys.modules["services.oauth_manager"] at import time.  When those tests
    run first the real module is replaced and subsequent tests see a stub
    without the expected attributes.  We force-reload the real module here
    and re-register it in sys.modules so every test in this file always
    operates on the genuine implementation.

    monkeypatch.setattr is used for all globals so changes are rolled back
    automatically at teardown — even direct assignments like
    ``om._accounts["primary"].access_token = "new-access"`` made inside test bodies.
    """
    # Force the real module to be loaded (not a stub left by another test file).
    import services.oauth_manager as om

    real_mod = importlib.import_module("services.oauth_manager")
    # If what's in sys.modules is NOT the real module (no _accounts attr),
    # reload to get the genuine implementation.
    if not hasattr(real_mod, "_accounts"):
        real_mod = importlib.reload(importlib.import_module("services.oauth_manager"))
    # Ensure sys.modules always points to the real module for the duration.
    original_module = sys.modules.get("services.oauth_manager")
    sys.modules["services.oauth_manager"] = real_mod

    # Never let unit tests reload or overwrite the host user's real credentials.
    monkeypatch.setattr(
        real_mod, "CREDENTIALS_PATH", str(tmp_path / "isolated-primary.json")
    )
    monkeypatch.setattr(
        real_mod,
        "FAILOVER_CREDENTIALS_PATH",
        str(tmp_path / "isolated-failover.json"),
    )
    monkeypatch.setattr(
        real_mod, "FAILOVER_STATE_PATH", str(tmp_path / "isolated-state.json")
    )

    # 二重アカウント化 (2026-07-02): per-account 状態は _reset_for_tests() で初期化。
    real_mod._reset_for_tests()
    monkeypatch.setattr(real_mod, "_http_session", None)
    monkeypatch.setattr(real_mod, "_patched", False)
    monkeypatch.setattr(real_mod, "OAUTH_FAILOVER_ENABLED", True)
    # 2nd contamination vector: a sibling test (mcp_mem0_server path) sets
    # OAUTH_NO_REFRESH=1 in the env, which oauth_manager reads at import time
    # into a module-level bool (~line 137). The globals reset above misses it,
    # so ensure_valid_token() silently no-ops and returns None under a full
    # suite. Reset BOTH the env var and the already-bound constant (a reload
    # may re-read the env).
    monkeypatch.delenv("OAUTH_NO_REFRESH", raising=False)
    monkeypatch.setattr(real_mod, "OAUTH_NO_REFRESH", False)

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


def test_failover_state_defaults_to_private_bot_state_dir(tmp_path):
    state_dir = tmp_path / "runtime-state"
    env = os.environ.copy()
    env["BOT_STATE_DIR"] = str(state_dir)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from services.oauth_manager import FAILOVER_STATE_PATH; print(FAILOVER_STATE_PATH)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == str(state_dir / "oauth_failover.json")


def test_primary_and_failover_paths_are_explicit_per_identity(tmp_path):
    primary = tmp_path / "identity" / ".credentials.json"
    failover = tmp_path / "identity" / ".credentials-failover.json"
    env = os.environ.copy()
    env["OAUTH_PRIMARY_CREDENTIALS_PATH"] = str(primary)
    env["OAUTH_FAILOVER_CREDENTIALS_PATH"] = str(failover)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from services.oauth_manager import CREDENTIALS_PATH, "
            "FAILOVER_CREDENTIALS_PATH; print(CREDENTIALS_PATH); "
            "print(FAILOVER_CREDENTIALS_PATH)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == [str(primary), str(failover)]


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
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "tok-123",
                    "refreshToken": "ref-456",
                    "expiresAt": 9999999999999,
                }
            },
        )
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

        assert om._save_credentials(
            "access-t", "refresh-t", 12345678, extra={"subscriptionType": "pro"}
        ) is True

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
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "old",
                    "refreshToken": "old-ref",
                    "expiresAt": 1,
                }
            },
        )
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))

        assert om._save_credentials("new-access", "new-refresh", 99999) is True

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
        assert om._save_credentials("a", "r", 9999) is False
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
        assert om._save_credentials("a", "r", 9999) is False
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

        # Should not raise — the outer except reports failure as False.
        assert om._save_credentials("a", "r", 9999) is False

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

        # Both os.replace and os.unlink fail — outer except must still return False.
        assert om._save_credentials("a", "r", 9999) is False

    def test_fsyncs_file_and_parent_directory(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = creds_dir / ".credentials.json"
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))
        real_fsync = os.fsync
        fsync_targets = []

        def tracking_fsync(fd):
            fsync_targets.append(
                "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            )
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", tracking_fsync)

        assert om._save_credentials("access", "refresh", 9999999999999) is True
        assert fsync_targets == ["file", "directory"]

    def test_parent_directory_fsync_failure_returns_false(
        self, tmp_path, monkeypatch
    ):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = creds_dir / ".credentials.json"
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))
        real_fsync = os.fsync
        calls = 0

        def fail_parent_fsync(fd):
            nonlocal calls
            calls += 1
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("simulated parent fsync failure")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_parent_fsync)

        assert om._save_credentials("access", "refresh", 9999999999999) is False
        assert calls == 2

    def test_readback_mismatch_returns_false_without_logging_secrets(
        self, tmp_path, monkeypatch, caplog
    ):
        import logging
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = creds_dir / ".credentials.json"
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))
        monkeypatch.setattr(
            om,
            "_read_credentials",
            lambda path=None: {
                "accessToken": "different",
                "refreshToken": "different",
                "expiresAt": 1,
            },
        )
        access = "secret-access-for-test"
        refresh = "secret-refresh-for-test"

        with caplog.at_level(logging.ERROR, logger="services.oauth_manager"):
            assert om._save_credentials(access, refresh, 9999999999999) is False

        rendered = caplog.text
        assert access not in rendered
        assert refresh not in rendered


# ---------------------------------------------------------------------------
# refresh_token
# ---------------------------------------------------------------------------


class TestRefreshToken:
    @pytest.mark.asyncio
    async def test_success(self, monkeypatch):
        import services.oauth_manager as om

        om._accounts["primary"].refresh_token = "ref-tok"
        om._accounts["primary"].access_token = None

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }
        )

        mock_session = MagicMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_cm)

        monkeypatch.setattr(om, "_save_credentials", lambda *a, **kw: True)

        async def fake_get_session():
            return mock_session

        monkeypatch.setattr(om, "_get_session", fake_get_session)

        result = await om.refresh_token()

        assert result is True
        assert om._accounts["primary"].access_token == "new-access"
        assert om._accounts["primary"].refresh_token == "new-refresh"
        assert om._accounts["primary"].expires_at > 0

    @pytest.mark.asyncio
    async def test_persistence_failure_propagates_and_discards_expired_access(
        self, monkeypatch
    ):
        import services.oauth_manager as om

        acct = om._accounts["primary"]
        acct.access_token = "expired-access"
        acct.refresh_token = "refresh-token"
        acct.expires_at = 0

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }
        )
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_cm)

        async def fake_get_session():
            return mock_session

        monkeypatch.setattr(om, "_get_session", fake_get_session)
        monkeypatch.setattr(om, "_save_credentials", lambda *a, **kw: False)

        assert await om.refresh_token() is False
        assert acct.access_token is None
        assert acct.refresh_token == "refresh-token"
        assert acct.refresh_failure_count == 1

    @pytest.mark.asyncio
    async def test_no_refresh_token_returns_false(self, monkeypatch):
        import services.oauth_manager as om

        om._accounts["primary"].refresh_token = None

        result = await om.refresh_token()
        assert result is False

    @pytest.mark.asyncio
    async def test_http_error_returns_false(self, monkeypatch):
        import services.oauth_manager as om

        om._accounts["primary"].refresh_token = "ref-tok"

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

        om._accounts["primary"].refresh_token = "ref-tok"

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

        om._accounts["primary"].access_token = "valid-tok"
        om._accounts["primary"].expires_at = int(time.time() * 1000) + 600_000

        result = await om.ensure_valid_token()
        assert result == "valid-tok"

    @pytest.mark.asyncio
    async def test_refreshes_when_expired(self, monkeypatch):
        import services.oauth_manager as om

        om._accounts["primary"].access_token = "old-tok"
        om._accounts["primary"].expires_at = 0  # expired
        om._accounts["primary"].refresh_token = "ref-tok"

        async def fake_refresh(account=None):
            om._accounts["primary"].access_token = "refreshed-tok"
            om._accounts["primary"].expires_at = int(time.time() * 1000) + 3600_000
            return True

        monkeypatch.setattr(om, "refresh_token", fake_refresh)

        result = await om.ensure_valid_token()
        assert result == "refreshed-tok"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_token_no_refresh(self, monkeypatch):
        import services.oauth_manager as om

        om._accounts["primary"].access_token = None
        om._accounts["primary"].expires_at = 0
        om._accounts["primary"].refresh_token = None

        result = await om.ensure_valid_token()
        assert result is None

    @pytest.mark.asyncio
    async def test_does_not_return_expired_token_if_refresh_fails(self, monkeypatch):
        import services.oauth_manager as om

        om._accounts["primary"].access_token = "stale-tok"
        om._accounts["primary"].expires_at = 0
        om._accounts["primary"].refresh_token = "ref-tok"

        async def fake_refresh(account=None):
            return False

        monkeypatch.setattr(om, "refresh_token", fake_refresh)

        result = await om.ensure_valid_token()
        assert result is None
        assert om.get_access_token() is None

    @pytest.mark.asyncio
    async def test_refresh_failure_can_use_not_yet_expired_buffered_token(
        self, monkeypatch
    ):
        import services.oauth_manager as om

        acct = om._accounts["primary"]
        acct.access_token = "still-live"
        acct.expires_at = int(time.time() * 1000) + 60_000
        acct.refresh_token = "refresh"

        async def fake_refresh(account=None):
            return False

        monkeypatch.setattr(om, "refresh_token", fake_refresh)

        assert await om.ensure_valid_token() == "still-live"

    @pytest.mark.asyncio
    async def test_no_refresh_mode_does_not_return_hard_expired_disk_token(
        self, tmp_path, monkeypatch
    ):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "expired-access",
                    "refreshToken": "refresh-token",
                    "expiresAt": 1,
                }
            },
        )
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))
        monkeypatch.setattr(om, "OAUTH_NO_REFRESH", True)

        assert await om.ensure_valid_token() is None
        assert om._accounts["primary"].access_token is None

    @pytest.mark.asyncio
    async def test_concurrent_refresh_is_deduplicated_after_disk_reload(
        self, tmp_path, monkeypatch
    ):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "expired-access",
                    "refreshToken": "refresh-token",
                    "expiresAt": 1,
                }
            },
        )
        acct = om._accounts["primary"]
        acct.credentials_path = str(creds_file)
        assert om._load_account_from_disk(acct) is True
        post_calls = 0

        class Response:
            status = 200

            async def json(self):
                await asyncio.sleep(0)
                return {
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                }

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Session:
            def post(self, *args, **kwargs):
                nonlocal post_calls
                post_calls += 1
                return Response()

        async def fake_get_session():
            return Session()

        monkeypatch.setattr(om, "_get_session", fake_get_session)

        results = await asyncio.gather(
            om.ensure_valid_token(),
            om.ensure_valid_token(),
        )

        assert results == ["rotated-access", "rotated-access"]
        assert post_calls == 1
        persisted = json.loads(creds_file.read_text())["claudeAiOauth"]
        assert persisted["accessToken"] == "rotated-access"


# ---------------------------------------------------------------------------
# get_access_token
# ---------------------------------------------------------------------------


class TestGetAccessToken:
    def test_returns_none_by_default(self, monkeypatch):
        import services.oauth_manager as om

        om._accounts["primary"].access_token = None
        assert om.get_access_token() is None

    def test_returns_current_token(self, monkeypatch):
        import services.oauth_manager as om

        om._accounts["primary"].access_token = "test-access-token"
        om._accounts["primary"].expires_at = int(time.time() * 1000) + 3600_000
        assert om.get_access_token() == "test-access-token"

    def test_returns_none_for_hard_expired_token(self, monkeypatch):
        import services.oauth_manager as om

        om._accounts["primary"].access_token = "expired-access-token"
        om._accounts["primary"].expires_at = 0
        assert om.get_access_token() is None


# ---------------------------------------------------------------------------
# _patch_anthropic_sdk
# ---------------------------------------------------------------------------


class TestPatchAnthropicSdk:
    def test_patches_init(self, monkeypatch):
        import services.oauth_manager as om

        # Reset patch state so each test is independent.
        monkeypatch.setattr(om, "_patched", False)
        om._accounts["primary"].access_token = "bearer-tok"

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
        om._accounts["primary"].access_token = "tok"

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
        om._accounts["primary"].access_token = "tok"

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


@pytest.mark.process_state_mutation
class TestInit:
    def test_returns_true_with_valid_creds(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        expires_future = int(time.time() * 1000) + 3600_000
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "valid-tok",
                    "refreshToken": "ref-tok",
                    "expiresAt": expires_future,
                    "subscriptionType": "pro_claude_ai",
                }
            },
        )
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))
        monkeypatch.setattr(om, "_patched", True)  # skip SDK patch side-effect

        result = om.init()

        assert result is True
        assert om._accounts["primary"].access_token == "valid-tok"
        assert om._accounts["primary"].refresh_token == "ref-tok"

    def test_returns_false_when_no_creds_file(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        missing = tmp_path / "nope.json"
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(missing))
        # failover も隔離（実機の .credentials-failover.json / state を拾わない）
        monkeypatch.setattr(
            om, "FAILOVER_CREDENTIALS_PATH", str(tmp_path / "fo-missing.json")
        )
        monkeypatch.setattr(om, "FAILOVER_STATE_PATH", str(tmp_path / "state.json"))
        om._reset_for_tests()
        result = om.init()
        assert result is False

    def test_returns_false_when_no_access_token(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "refreshToken": "ref-only",
                    "expiresAt": 0,
                }
            },
        )
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))
        monkeypatch.setattr(
            om, "FAILOVER_CREDENTIALS_PATH", str(tmp_path / "fo-missing.json")
        )
        monkeypatch.setattr(om, "FAILOVER_STATE_PATH", str(tmp_path / "state.json"))
        om._reset_for_tests()
        result = om.init()
        assert result is False

    def test_clears_env_vars(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "tok",
                    "refreshToken": "ref",
                    "expiresAt": int(time.time() * 1000) + 3600_000,
                }
            },
        )
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))
        monkeypatch.setattr(om, "_patched", True)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "should-be-removed")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "should-be-removed")

        om.init()

        assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
        assert "ANTHROPIC_API_KEY" not in os.environ

    def test_expired_token_still_loads(self, tmp_path, monkeypatch):
        """init() returns True even if token is already expired — refresh happens later."""
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "expired-tok",
                    "refreshToken": "ref-tok",
                    "expiresAt": 1,  # far in the past
                }
            },
        )
        monkeypatch.setattr(om, "CREDENTIALS_PATH", str(creds_file))
        monkeypatch.setattr(om, "_patched", True)

        result = om.init()
        assert result is True
        assert om._accounts["primary"].access_token == "expired-tok"


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

        om._accounts["primary"].refresh_failure_count = 0

        with caplog.at_level(logging.WARNING, logger="services.oauth_manager"):
            om._record_refresh_failure(om._accounts["primary"], "err1")
            om._record_refresh_failure(om._accounts["primary"], "err2")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2

        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="services.oauth_manager"):
            om._record_refresh_failure(om._accounts["primary"], "err3")

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "consecutive" in errors[0].message

    def test_refresh_success_resets_counter(self, monkeypatch, caplog):
        import logging
        import services.oauth_manager as om

        om._accounts["primary"].refresh_failure_count = 2

        with caplog.at_level(logging.INFO, logger="services.oauth_manager"):
            om._record_refresh_success(om._accounts["primary"])

        assert om._accounts["primary"].refresh_failure_count == 0
        info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("recovered" in r.message for r in info_msgs)

    def test_is_oauth_healthy_tolerates_idle_expired_but_refreshable_credential(
        self, tmp_path, monkeypatch
    ):
        """ADR-0041 Fix A: idle で access token が期限切れでも refresh 可能
        （refresh token 有効・失敗履歴なし）なら healthy。
        2026-07-21 の 10 時間偽 DOWN（idle 期限切れ → degraded 張り付き）の
        regression テスト。"""
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "tok",
                    "refreshToken": "refresh",
                    "expiresAt": 1,
                }
            },
        )
        om._accounts["primary"].credentials_path = str(creds_file)
        assert om.is_oauth_healthy() is True

    def test_is_oauth_healthy_rejects_dead_refresh_path_when_expired(
        self, tmp_path, monkeypatch
    ):
        """期限切れ + refresh 経路が死んでいる（invalid_grant 判明済み or
        連続失敗閾値超え）だけが本当の degraded。"""
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "tok",
                    "refreshToken": "refresh",
                    "expiresAt": 1,
                }
            },
        )
        om._accounts["primary"].credentials_path = str(creds_file)

        om._accounts["primary"].refresh_permanent = True
        assert om.is_oauth_healthy() is False

        om._accounts["primary"].refresh_permanent = False
        om._accounts["primary"].refresh_failure_count = (
            om._REFRESH_FAILURE_ALERT_THRESHOLD
        )
        assert om.is_oauth_healthy() is False

    def test_is_oauth_healthy_with_valid_memory_token_ignores_disk_state(
        self, tmp_path, monkeypatch
    ):
        """有効な in-memory token を保持している間は disk 状態に依らず健全
        （実際に供給できている事実が優先）。"""
        import services.oauth_manager as om

        om._accounts["primary"].access_token = "live-token"
        om._accounts["primary"].expires_at = int(time.time() * 1000) + 3600_000
        om._accounts["primary"].credentials_path = str(tmp_path / "missing.json")
        assert om.is_oauth_healthy() is True

    def test_redundancy_survives_standby_access_token_expiry(
        self, tmp_path, monkeypatch
    ):
        """standby の access token 期限切れは切替時 refresh で回復する設計
        (prepare_and_failover_to_secondary) なので冗長性喪失ではない。
        invalid_grant 判明済み (refresh_permanent) なら喪失。"""
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "valid-tok",
                    "refreshToken": "refresh",
                    "expiresAt": int(time.time() * 1000) + 3600_000,
                }
            },
        )
        om._accounts["primary"].credentials_path = str(creds_file)
        failover_file = creds_dir / ".credentials-failover.json"
        failover_file.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "stale-tok",
                        "refreshToken": "refresh",
                        "expiresAt": 1,
                    }
                }
            ),
            encoding="utf-8",
        )
        failover_file.chmod(0o600)
        om._accounts["failover"].credentials_path = str(failover_file)

        detail = om.oauth_status_detail()
        assert detail["standby_credential_ok"] is False
        assert detail["standby_credential_reason"] == "access_token_expired"
        assert detail["redundancy_ok"] is True
        assert detail["redundancy_reason"] is None

        om._accounts["failover"].refresh_permanent = True
        detail = om.oauth_status_detail()
        assert detail["redundancy_ok"] is False
        assert detail["redundancy_reason"] == "refresh_permanent"

    def test_is_oauth_healthy_validates_private_disk_credential(
        self, tmp_path, monkeypatch
    ):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "valid-tok",
                    "refreshToken": "refresh",
                    "expiresAt": int(time.time() * 1000) + 3600_000,
                }
            },
        )
        om._accounts["primary"].credentials_path = str(creds_file)
        failover_file = creds_dir / ".credentials-failover.json"
        failover_file.write_text(
            creds_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
        failover_file.chmod(0o600)
        om._accounts["failover"].credentials_path = str(failover_file)
        om._accounts["primary"].refresh_failure_count = 5
        om._accounts["primary"].refresh_permanent = True
        assert om.is_oauth_healthy() is True

        creds_file.chmod(0o644)
        assert om.is_oauth_healthy() is False

        creds_file.chmod(0o000)
        assert om.is_oauth_healthy() is False

    def test_is_oauth_healthy_reports_broken_standby_as_redundancy_only(
        self, tmp_path, monkeypatch
    ):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "valid-tok",
                    "refreshToken": "refresh",
                    "expiresAt": int(time.time() * 1000) + 3600_000,
                }
            },
        )
        om._accounts["primary"].credentials_path = str(creds_file)
        om._accounts["failover"].credentials_path = str(
            creds_dir / ".credentials-failover.json"
        )
        assert om.is_oauth_healthy() is True
        detail = om.oauth_status_detail()
        assert detail["credential_ok"] is True
        assert detail["redundancy_ok"] is False
        assert detail["redundancy_reason"] == "credentials_missing"
        assert detail["standby_credential_ok"] is False
        assert detail["standby_credential_reason"] == "credentials_missing"

    def test_is_oauth_healthy_rejects_missing_and_invalid_tokens(
        self, tmp_path, monkeypatch
    ):
        import services.oauth_manager as om

        creds_dir = _make_creds_dir(tmp_path)
        missing = creds_dir / ".credentials.json"
        om._accounts["primary"].credentials_path = str(missing)
        assert om.is_oauth_healthy() is False

        missing.write_text("not-json", encoding="utf-8")
        missing.chmod(0o600)
        assert om.is_oauth_healthy() is False

        creds_file = _make_creds_file(
            creds_dir,
            {
                "claudeAiOauth": {
                    "accessToken": "",
                    "refreshToken": "refresh",
                    "expiresAt": int(time.time() * 1000) + 3600_000,
                }
            },
        )
        om._accounts["primary"].credentials_path = str(creds_file)
        assert om.is_oauth_healthy() is False

    def test_load_account_from_disk_clears_stale_failure_on_newer_creds(
        self, tmp_path, monkeypatch
    ):
        """共有 credential を別プロセスが rotate → ディスクに新トークンが出たら、
        張り付いた invalid_grant 判定（permanent / failure_count）を自己解除する。"""
        import services.oauth_manager as om

        creds_path = tmp_path / ".credentials.json"
        creds_path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "fresh-access",
                        "refreshToken": "fresh-refresh",
                        "expiresAt": int(time.time() * 1000) + 3600_000,
                    }
                }
            )
        )

        acct = om._accounts["primary"]
        acct.credentials_path = str(creds_path)
        # 旧状態: invalid_grant で死んだ判定が張り付いている
        acct.access_token = "old-access"
        acct.refresh_token = "old-refresh"
        acct.expires_at = int(time.time() * 1000) + 60_000
        acct.refresh_failure_count = 3
        acct.refresh_permanent = True

        assert om._load_account_from_disk(acct) is True
        assert acct.refresh_token == "fresh-refresh"
        assert acct.refresh_failure_count == 0
        assert acct.refresh_permanent is False

    def test_load_account_from_disk_keeps_failures_when_creds_unchanged(
        self, tmp_path, monkeypatch
    ):
        """同一 credential の再ロードでは失敗状態を消さない（誤クリア防止）。"""
        import services.oauth_manager as om

        exp = int(time.time() * 1000) + 3600_000
        creds_path = tmp_path / ".credentials.json"
        creds_path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "same-access",
                        "refreshToken": "same-refresh",
                        "expiresAt": exp,
                    }
                }
            )
        )

        acct = om._accounts["primary"]
        acct.credentials_path = str(creds_path)
        acct.access_token = "same-access"
        acct.refresh_token = "same-refresh"
        acct.expires_at = exp
        acct.refresh_failure_count = 2
        acct.refresh_permanent = False

        assert om._load_account_from_disk(acct) is True
        assert acct.refresh_failure_count == 2

    @pytest.mark.asyncio
    async def test_ensure_valid_token_raises_when_expired_and_failing(
        self, monkeypatch
    ):
        import services.oauth_manager as om

        om._accounts["primary"].access_token = "stale-tok"
        om._accounts["primary"].expires_at = 0  # expired
        om._accounts["primary"].refresh_token = "ref-tok"
        om._accounts["primary"].refresh_failure_count = 3

        async def fake_refresh(account=None):
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

        om._accounts["primary"].refresh_token = "ref-tok"
        om._accounts["primary"].refresh_permanent = False

        mock_resp = AsyncMock()
        mock_resp.status = 400
        mock_resp.text = AsyncMock(
            return_value='{"error":"invalid_grant","error_description":"Token revoked"}'
        )

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
        assert om._accounts["primary"].refresh_permanent is True

    @pytest.mark.asyncio
    async def test_invalid_grant_substring_sets_permanent_flag(self, monkeypatch):
        """Non-200 body containing 'invalid_grant' text sets _refresh_permanent=True."""
        import services.oauth_manager as om

        om._accounts["primary"].refresh_token = "ref-tok"
        om._accounts["primary"].refresh_permanent = False

        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.text = AsyncMock(
            return_value="error=invalid_grant&description=revoked"
        )

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
        assert om._accounts["primary"].refresh_permanent is True

    @pytest.mark.asyncio
    async def test_non_invalid_grant_error_does_not_set_permanent(self, monkeypatch):
        """Transient HTTP error (e.g. 503) does NOT set _refresh_permanent."""
        import services.oauth_manager as om

        om._accounts["primary"].refresh_token = "ref-tok"
        om._accounts["primary"].refresh_permanent = False

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
        assert om._accounts["primary"].refresh_permanent is False

    @pytest.mark.asyncio
    async def test_ensure_valid_token_raises_immediately_on_permanent(
        self, monkeypatch
    ):
        """When _refresh_permanent is True, ensure_valid_token raises without retrying."""
        import services.oauth_manager as om

        om._accounts["primary"].access_token = "stale-tok"
        om._accounts["primary"].expires_at = 0  # expired
        om._accounts["primary"].refresh_token = "ref-tok"
        om._accounts["primary"].refresh_permanent = True
        om._accounts["primary"].refresh_failure_count = 0  # count below threshold

        async def fail_if_network_is_used():
            raise AssertionError("permanent refresh failure must not use the network")

        monkeypatch.setattr(om, "_get_session", fail_if_network_is_used)

        with pytest.raises(om.OAuthRefreshFailed, match="invalid_grant"):
            await om.ensure_valid_token()

        assert om._accounts["primary"].access_token is None

    @pytest.mark.asyncio
    async def test_record_refresh_success_clears_permanent_flag(self, monkeypatch):
        """_record_refresh_success() resets _refresh_permanent to False."""
        import services.oauth_manager as om

        om._accounts["primary"].refresh_permanent = True
        om._accounts["primary"].refresh_failure_count = 0

        om._record_refresh_success(om._accounts["primary"])
        assert om._accounts["primary"].refresh_permanent is False


# ---------------------------------------------------------------------------
# OAuth 二重アカウント（failover）— 2026-07-02
# ---------------------------------------------------------------------------


def _setup_dual(tmp_path, monkeypatch, *, with_failover=True):
    """primary/failover の credentials ファイルと state パスを tmp に用意する。"""
    import services.oauth_manager as om

    d = _make_creds_dir(tmp_path)
    primary = _make_creds_file(
        d,
        {
            "claudeAiOauth": {
                "accessToken": "prim-acc",
                "refreshToken": "prim-ref",
                "expiresAt": int(time.time() * 1000) + 3_600_000,
            }
        },
    )
    fo = d / ".credentials-failover.json"
    if with_failover:
        fo.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "fo-acc",
                        "refreshToken": "fo-ref",
                        "expiresAt": int(time.time() * 1000) + 3_600_000,
                    }
                }
            )
        )
        os.chmod(fo, 0o600)
    monkeypatch.setattr(om, "CREDENTIALS_PATH", str(primary))
    monkeypatch.setattr(om, "FAILOVER_CREDENTIALS_PATH", str(fo))
    monkeypatch.setattr(om, "FAILOVER_STATE_PATH", str(tmp_path / "state.json"))
    om._reset_for_tests()
    return primary, fo


@pytest.mark.process_state_mutation
class TestFailoverToSecondary:
    def test_success_switches_and_persists(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        assert om.init() is True
        assert om.get_active_account() == "primary"
        ok = om.failover_to_secondary("quota", primary_retry_at=time.time() + 100)
        assert ok is True
        assert om.get_active_account() == "failover"
        assert om.get_access_token() == "fo-acc"
        state = json.loads(open(om.FAILOVER_STATE_PATH).read())
        assert state["active"] == "failover" and state["reason"] == "quota"
        assert state["primary_retry_at"] is not None

    def test_auth_reason_has_no_retry_at(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        om.init()
        assert om.failover_to_secondary("auth", primary_retry_at=time.time()) is True
        state = json.loads(open(om.FAILOVER_STATE_PATH).read())
        assert state["primary_retry_at"] is None  # auth は手動復旧まで張り付き

    def test_missing_failover_creds_returns_false(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch, with_failover=False)
        om.init()
        assert om.failover_to_secondary("quota") is False
        assert om.get_active_account() == "primary"

    def test_broken_failover_creds_returns_false(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _p, fo = _setup_dual(tmp_path, monkeypatch)
        fo.write_text("not json")
        assert om.failover_to_secondary("quota") is False

    def test_expired_failover_is_rejected_without_state_change(
        self, tmp_path, monkeypatch
    ):
        import services.oauth_manager as om

        _primary, fo = _setup_dual(tmp_path, monkeypatch)
        payload = json.loads(fo.read_text())
        payload["claudeAiOauth"]["expiresAt"] = 1
        fo.write_text(json.dumps(payload))
        om.init()

        assert om.failover_to_secondary("quota") is False
        assert om.get_active_account() == "primary"
        assert not os.path.exists(om.FAILOVER_STATE_PATH)

    def test_switch_does_not_forget_known_permanent_refresh_failure(
        self, tmp_path, monkeypatch
    ):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        om.init()
        om._accounts["failover"].refresh_permanent = True
        om._accounts["failover"].refresh_failure_count = 1

        assert om.failover_to_secondary("quota") is True
        assert om._accounts["failover"].refresh_permanent is True
        assert om._accounts["failover"].refresh_failure_count == 1

    def test_flag_off_returns_false(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        monkeypatch.setattr(om, "OAUTH_FAILOVER_ENABLED", False)
        assert om.failover_to_secondary("quota") is False

    def test_idempotent_when_already_failover(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        om.init()
        assert om.failover_to_secondary("quota") is True
        assert om.failover_to_secondary("quota") is False

    def test_no_refresh_child_returns_false(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        monkeypatch.setattr(om, "OAUTH_NO_REFRESH", True)
        assert om.failover_to_secondary("quota") is False

    @pytest.mark.asyncio
    async def test_prepare_rejects_expired_standby_when_refresh_fails(
        self, tmp_path, monkeypatch
    ):
        import services.oauth_manager as om

        _primary, fo = _setup_dual(tmp_path, monkeypatch)
        payload = json.loads(fo.read_text())
        payload["claudeAiOauth"]["expiresAt"] = 1
        fo.write_text(json.dumps(payload))
        om.init()

        async def fail_refresh(account=None):
            account.refresh_permanent = True
            account.refresh_failure_count += 1
            return False

        monkeypatch.setattr(om, "refresh_token", fail_refresh)

        assert await om.prepare_and_failover_to_secondary("quota") is False
        assert om.get_active_account() == "primary"
        assert not os.path.exists(om.FAILOVER_STATE_PATH)
        assert om._accounts["failover"].refresh_permanent is True

    @pytest.mark.asyncio
    async def test_prepare_refreshes_expired_standby_before_switch(
        self, tmp_path, monkeypatch
    ):
        import services.oauth_manager as om

        _primary, fo = _setup_dual(tmp_path, monkeypatch)
        payload = json.loads(fo.read_text())
        payload["claudeAiOauth"]["expiresAt"] = 1
        fo.write_text(json.dumps(payload))
        om.init()

        async def refresh_standby(account=None):
            refreshed = json.loads(fo.read_text())
            refreshed["claudeAiOauth"]["accessToken"] = "fo-refreshed"
            refreshed["claudeAiOauth"]["refreshToken"] = "fo-refreshed-ref"
            refreshed["claudeAiOauth"]["expiresAt"] = (
                int(time.time() * 1000) + 3_600_000
            )
            fo.write_text(json.dumps(refreshed))
            return True

        monkeypatch.setattr(om, "refresh_token", refresh_standby)

        assert await om.prepare_and_failover_to_secondary("quota") is True
        assert om.get_active_account() == "failover"
        assert om.get_access_token() == "fo-refreshed"

    def test_failed_client_build_rollback_restores_primary(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        om.init()
        assert om.failover_to_secondary("quota") is True

        assert om.rollback_failed_failover("client_rebuild_failed") is True
        assert om.get_active_account() == "primary"
        assert om.get_access_token() == "prim-acc"
        assert not os.path.exists(om.FAILOVER_STATE_PATH)


@pytest.mark.process_state_mutation
class TestMaybeRestorePrimary:
    @pytest.mark.asyncio
    async def test_before_retry_at_stays(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        om.init()
        om.failover_to_secondary("quota", primary_retry_at=time.time() + 3600)
        assert await om.maybe_restore_primary() is False
        assert om.get_active_account() == "failover"

    @pytest.mark.asyncio
    async def test_after_retry_at_restores(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        om.init()
        om.failover_to_secondary("quota", primary_retry_at=time.time() - 10)
        assert await om.maybe_restore_primary() is True
        assert om.get_active_account() == "primary"
        assert om.get_access_token() == "prim-acc"
        assert not os.path.exists(om.FAILOVER_STATE_PATH)  # 状態クリア

    @pytest.mark.asyncio
    async def test_auth_reason_never_auto_restores(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        om.init()
        om.failover_to_secondary("auth")
        assert await om.maybe_restore_primary() is False
        assert om.get_active_account() == "failover"

    @pytest.mark.asyncio
    async def test_failed_primary_refresh_pushes_retry_at(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        primary, _fo = _setup_dual(tmp_path, monkeypatch)
        # primary を期限切れにして refresh を失敗させる
        primary.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "prim-acc",
                        "refreshToken": "prim-ref",
                        "expiresAt": 1,
                    }
                }
            )
        )
        om.init()
        om.failover_to_secondary("quota", primary_retry_at=time.time() - 10)

        async def fail_refresh(account=None):
            return False

        monkeypatch.setattr(om, "refresh_token", fail_refresh)
        before = time.time()
        assert await om.maybe_restore_primary() is False
        assert om.get_active_account() == "failover"
        assert om._primary_retry_at > before + 500  # 後ろ倒し


# ---------------------------------------------------------------------------
# 双方向 failover（2026-07-03 障害対応）
# auth 張り付きの /login 自動復旧 + failover 死亡時の強制切り戻し
# ---------------------------------------------------------------------------


def _touch_future(path, offset_s: float = 5.0) -> None:
    """mtime を確実に「failover 発動より後」にする（FS 粒度対策）。"""
    t = time.time() + offset_s
    os.utime(str(path), (t, t))


@pytest.mark.process_state_mutation
class TestAuthFailbackOnRelogin:
    @pytest.mark.asyncio
    async def test_relogin_restores_primary(self, tmp_path, monkeypatch):
        """/login で primary credential が更新されたら auth 張り付きが解ける。"""
        import services.oauth_manager as om

        primary, _fo = _setup_dual(tmp_path, monkeypatch)
        om.init()
        om.failover_to_secondary("auth")
        assert om.get_active_account() == "failover"
        # /login 相当: 有効な credential でファイルが書き直される
        primary.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "prim-new",
                        "refreshToken": "prim-ref2",
                        "expiresAt": int(time.time() * 1000) + 3_600_000,
                    }
                }
            )
        )
        _touch_future(primary)
        assert await om.maybe_restore_primary() is True
        assert om.get_active_account() == "primary"
        assert om.get_access_token() == "prim-new"
        assert not os.path.exists(om.FAILOVER_STATE_PATH)  # 状態クリア

    @pytest.mark.asyncio
    async def test_same_dead_creds_tried_only_once(self, tmp_path, monkeypatch):
        """更新された credential が無効でも、同じ mtime では refresh を再試行しない。"""
        import services.oauth_manager as om

        primary, _fo = _setup_dual(tmp_path, monkeypatch)
        om.init()
        om.failover_to_secondary("auth")
        # 書き直されたが期限切れ（refresh も失敗する想定）
        primary.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "prim-new",
                        "refreshToken": "prim-ref2",
                        "expiresAt": 1,
                    }
                }
            )
        )
        _touch_future(primary)
        calls = {"n": 0}

        async def fail_refresh(account=None):
            calls["n"] += 1
            return False

        monkeypatch.setattr(om, "refresh_token", fail_refresh)
        assert await om.maybe_restore_primary() is False
        assert calls["n"] == 1
        # 同じ mtime → 再試行しない（refresh 空振り連打防止）
        assert await om.maybe_restore_primary() is False
        assert calls["n"] == 1
        assert om.get_active_account() == "failover"

    @pytest.mark.asyncio
    async def test_state_survives_restart_with_since(self, tmp_path, monkeypatch):
        """再起動（init で state 復元）後も since が引き継がれ mtime 判定が働く。"""
        import services.oauth_manager as om

        primary, _fo = _setup_dual(tmp_path, monkeypatch)
        om.init()
        om.failover_to_secondary("auth")
        # 再起動相当
        om._reset_for_tests()
        om.init()
        assert om.get_active_account() == "failover"
        assert om._failover_since > 0
        # /login → 自動復帰
        primary.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "prim-new",
                        "refreshToken": "prim-ref2",
                        "expiresAt": int(time.time() * 1000) + 3_600_000,
                    }
                }
            )
        )
        _touch_future(primary)
        assert await om.maybe_restore_primary() is True
        assert om.get_active_account() == "primary"


@pytest.mark.process_state_mutation
class TestFailbackToPrimary:
    @pytest.mark.asyncio
    async def test_forced_failback_when_primary_alive(self, tmp_path, monkeypatch):
        """failover が死んだとき primary が生きていれば戻す（2026-07-03 の穴）。"""
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        om.init()
        om.failover_to_secondary("quota", primary_retry_at=time.time() + 3600)
        assert om.get_active_account() == "failover"
        assert await om.failback_to_primary("quota") is True
        assert om.get_active_account() == "primary"
        assert om.get_access_token() == "prim-acc"
        assert not os.path.exists(om.FAILOVER_STATE_PATH)

    @pytest.mark.asyncio
    async def test_primary_still_dead_stays_on_failover(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        primary, _fo = _setup_dual(tmp_path, monkeypatch)
        primary.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "prim-acc",
                        "refreshToken": "prim-ref",
                        "expiresAt": 1,
                    }
                }
            )
        )  # 期限切れ
        om.init()
        om.failover_to_secondary("auth")

        async def fail_refresh(account=None):
            return False

        monkeypatch.setattr(om, "refresh_token", fail_refresh)
        assert await om.failback_to_primary("quota") is False
        assert om.get_active_account() == "failover"

    @pytest.mark.asyncio
    async def test_cooldown_prevents_hammering(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        primary, _fo = _setup_dual(tmp_path, monkeypatch)
        primary.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "prim-acc",
                        "refreshToken": "prim-ref",
                        "expiresAt": 1,
                    }
                }
            )
        )
        om.init()
        om.failover_to_secondary("auth")
        calls = {"n": 0}

        async def fail_refresh(account=None):
            calls["n"] += 1
            return False

        monkeypatch.setattr(om, "refresh_token", fail_refresh)
        assert await om.failback_to_primary("quota") is False
        assert calls["n"] == 1
        # クールダウン内の再試行は refresh に到達しない
        assert await om.failback_to_primary("quota") is False
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_noop_when_active_is_primary(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        om.init()
        assert await om.failback_to_primary("quota") is False
        assert om.get_active_account() == "primary"


@pytest.mark.process_state_mutation
class TestFailoverPersistence:
    def test_state_survives_reinit(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        om.init()
        om.failover_to_secondary("quota", primary_retry_at=time.time() + 100)
        # 再起動相当
        om._reset_for_tests()
        assert om.init() is True
        assert om.get_active_account() == "failover"
        assert om.get_access_token() == "fo-acc"

    def test_reinit_falls_back_when_creds_gone(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _p, fo = _setup_dual(tmp_path, monkeypatch)
        om.init()
        om.failover_to_secondary("quota")
        os.remove(fo)
        om._reset_for_tests()
        assert om.init() is True
        assert om.get_active_account() == "primary"


@pytest.mark.process_state_mutation
class TestRefreshWriteback:
    @pytest.mark.asyncio
    async def test_failover_refresh_never_touches_primary_file(
        self, tmp_path, monkeypatch
    ):
        """本丸: failover の refresh が primary ファイルを clobber しない。"""
        import services.oauth_manager as om

        primary, fo = _setup_dual(tmp_path, monkeypatch)
        om.init()
        om.failover_to_secondary("quota")
        primary_before = primary.read_text()
        failover_payload = json.loads(fo.read_text())
        failover_payload["claudeAiOauth"]["expiresAt"] = 1
        fo.write_text(json.dumps(failover_payload))
        om._accounts["failover"].expires_at = 1

        class _Resp:
            status = 200

            async def json(self):
                return {
                    "access_token": "fo-new",
                    "refresh_token": "fo-ref2",
                    "expires_in": 3600,
                }

            async def text(self):
                return ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Sess:
            def post(self, *a, **k):
                return _Resp()

        async def fake_get_session():
            return _Sess()

        monkeypatch.setattr(om, "_get_session", fake_get_session)

        assert await om.refresh_token() is True
        assert primary.read_text() == primary_before  # primary 無傷
        fo_data = json.loads(fo.read_text())["claudeAiOauth"]
        assert fo_data["accessToken"] == "fo-new"  # failover のみ更新


@pytest.mark.process_state_mutation
class TestPrimePerAccount:
    def test_switch_resets_primed_flag(self, tmp_path, monkeypatch):
        import services.oauth_manager as om

        _setup_dual(tmp_path, monkeypatch)
        om.init()
        om._accounts["primary"].session_primed = True
        om.failover_to_secondary("quota")
        assert om._accounts["failover"].session_primed is False


class TestRatelimitReset:
    def test_reset_state_clears_observations(self):
        from services.claude_api import ratelimit as rl

        rl._state.five_h_status = "rejected"
        rl._state.five_h_utilization = 1.2
        rl.reset_state()
        st = rl.get_state()
        assert st.five_h_status == "allowed"
        assert st.five_h_utilization == 0.0
        blocked, _ = rl.should_block()
        assert blocked is False
