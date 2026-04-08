"""OAuth token manager — reads Claude Code OAuth tokens and patches Anthropic SDK.

Reads tokens from ~/.claude/.credentials.json (same file Claude Code uses),
handles automatic refresh via the platform.claude.com token endpoint,
and monkey-patches the Anthropic SDK so that all clients (including Mem0's
internal client) use Bearer auth instead of x-api-key.

Reference: free-code/src/services/oauth/client.ts
"""

import json
import logging
import os
import tempfile
import time

import aiohttp

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (from free-code/src/constants/oauth.ts)
# ---------------------------------------------------------------------------
CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
REFRESH_SCOPES = [
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
]
# Refresh 5 minutes before expiry (same buffer as free-code)
EXPIRY_BUFFER_MS = 5 * 60 * 1000

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_access_token: str | None = None
_refresh_token: str | None = None
_expires_at: int = 0  # epoch ms


def _read_credentials() -> dict | None:
    """Read Claude Code OAuth credentials from disk."""
    try:
        with open(CREDENTIALS_PATH) as f:
            data = json.load(f)
        return data.get("claudeAiOauth")
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
        log.warning("Failed to read OAuth credentials from %s: %s", CREDENTIALS_PATH, exc)
        return None


def _save_credentials(access: str, refresh: str, expires_at: int, extra: dict | None = None) -> None:
    """Write updated tokens back to the credentials file (atomic write)."""
    try:
        try:
            with open(CREDENTIALS_PATH) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}

        oauth = data.get("claudeAiOauth", {})
        oauth["accessToken"] = access
        oauth["refreshToken"] = refresh
        oauth["expiresAt"] = expires_at
        if extra:
            oauth.update(extra)
        data["claudeAiOauth"] = oauth

        # Atomic write: temp file → rename to avoid corruption on concurrent access
        creds_dir = os.path.dirname(CREDENTIALS_PATH)
        fd, tmp_path = tempfile.mkstemp(dir=creds_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, CREDENTIALS_PATH)
        except BaseException:
            os.unlink(tmp_path)
            raise
    except Exception as exc:
        log.warning("Failed to save OAuth credentials: %s", exc)


def _is_expired() -> bool:
    """Check if the current access token is expired (with 5-min buffer)."""
    now_ms = int(time.time() * 1000)
    return (now_ms + EXPIRY_BUFFER_MS) >= _expires_at


async def refresh_token() -> bool:
    """Refresh the OAuth access token using the refresh token."""
    global _access_token, _refresh_token, _expires_at

    if not _refresh_token:
        log.error("No refresh token available")
        return False

    body = {
        "grant_type": "refresh_token",
        "refresh_token": _refresh_token,
        "client_id": CLIENT_ID,
        "scope": " ".join(REFRESH_SCOPES),
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                TOKEN_URL,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.error("OAuth token refresh failed (%d): %s", resp.status, text)
                    return False

                data = await resp.json()

        new_access = data["access_token"]
        new_refresh = data.get("refresh_token", _refresh_token)
        expires_in = data["expires_in"]  # seconds
        new_expires_at = int(time.time() * 1000) + expires_in * 1000

        _access_token = new_access
        _refresh_token = new_refresh
        _expires_at = new_expires_at

        # Update env var so any new Anthropic clients pick it up
        os.environ["ANTHROPIC_AUTH_TOKEN"] = _access_token

        # Persist to disk
        _save_credentials(_access_token, _refresh_token, _expires_at)

        log.info("OAuth token refreshed, expires in %ds", expires_in)
        return True

    except Exception as exc:
        log.error("OAuth token refresh error: %s", exc)
        return False


async def ensure_valid_token() -> str | None:
    """Return a valid access token, refreshing if needed."""
    global _access_token

    if _access_token and not _is_expired():
        return _access_token

    if _is_expired() and _refresh_token:
        log.info("OAuth token expired or expiring soon, refreshing")
        if await refresh_token():
            return _access_token

    return _access_token


def get_access_token() -> str | None:
    """Synchronous getter for the current access token (no refresh)."""
    return _access_token


# ---------------------------------------------------------------------------
# SDK Patching
# ---------------------------------------------------------------------------
_patched = False


def _patch_anthropic_sdk() -> None:
    """Monkey-patch anthropic.Anthropic so all instances use OAuth Bearer auth.

    Mem0 internally does `anthropic.Anthropic(api_key=...)` which sets the
    x-api-key header. We intercept this to drop api_key and inject auth_token
    + the required beta header instead.
    """
    global _patched
    if _patched:
        return

    try:
        import anthropic

        _orig_init = anthropic.Anthropic.__init__

        def _oauth_init(self, **kwargs):
            # Drop api_key — we use auth_token via env
            kwargs.pop("api_key", None)
            # Merge beta header
            headers = dict(kwargs.pop("default_headers", None) or {})
            existing_beta = headers.get("anthropic-beta", "")
            if OAUTH_BETA_HEADER not in existing_beta:
                beta = f"{existing_beta},{OAUTH_BETA_HEADER}".strip(",") if existing_beta else OAUTH_BETA_HEADER
                headers["anthropic-beta"] = beta
            kwargs["default_headers"] = headers
            _orig_init(self, **kwargs)

        anthropic.Anthropic.__init__ = _oauth_init
        _patched = True
        log.info("Anthropic SDK patched for OAuth Bearer auth")
    except ImportError:
        log.warning("anthropic package not installed, skipping SDK patch")


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init() -> bool:
    """Initialize OAuth manager: load tokens, set env, patch SDK.

    Call this BEFORE importing/initializing Mem0.
    Returns True if a valid token was loaded.
    """
    global _access_token, _refresh_token, _expires_at

    creds = _read_credentials()
    if not creds:
        log.error("No OAuth credentials found at %s", CREDENTIALS_PATH)
        return False

    _access_token = creds.get("accessToken")
    _refresh_token = creds.get("refreshToken")
    _expires_at = creds.get("expiresAt", 0)

    if not _access_token:
        log.error("No access token in credentials")
        return False

    # Set env var for Anthropic SDK (picked up automatically)
    os.environ["ANTHROPIC_AUTH_TOKEN"] = _access_token
    # Remove ANTHROPIC_API_KEY if set to avoid conflicts
    os.environ.pop("ANTHROPIC_API_KEY", None)

    # Patch the SDK
    _patch_anthropic_sdk()

    expired_str = "EXPIRED" if _is_expired() else "valid"
    log.info(
        "OAuth loaded — token %s, expires_at=%d, subscription=%s",
        expired_str,
        _expires_at,
        creds.get("subscriptionType", "unknown"),
    )

    return True
