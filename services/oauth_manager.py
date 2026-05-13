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
import stat
import tempfile
import time

import aiohttp

log = logging.getLogger(__name__)


class OAuthRefreshFailed(RuntimeError):
    """Raised when OAuth token is expired and refresh has failed repeatedly."""


# ---------------------------------------------------------------------------
# H-2: permission verification helpers
# ---------------------------------------------------------------------------
# Any process that shares the `~/.claude` directory with us can otherwise
# race our atomic-rename (TOCTOU) or read the refreshed token. Refuse to
# write credentials back to disk unless the directory AND the existing file
# are restricted to the current user.

_CREDS_DIR_MODE_MASK = 0o077   # group/other must have no bits set
_CREDS_FILE_MODE_MASK = 0o077


def _verify_path_privacy(path: str, *, is_dir: bool) -> bool:
    """Return True iff `path` is owned by us and g/o perms are clear."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return True  # nothing to verify yet — the first write will create it
    except OSError as exc:
        log.error("Cannot stat %s: %s", path, exc)
        return False

    if st.st_uid != os.getuid():
        log.error(
            "Refusing to touch %s: owner=%d but we are uid=%d",
            path, st.st_uid, os.getuid(),
        )
        return False
    mode = stat.S_IMODE(st.st_mode)
    mask = _CREDS_DIR_MODE_MASK if is_dir else _CREDS_FILE_MODE_MASK
    if mode & mask:
        log.error(
            "Refusing to touch %s: mode=%#o grants g/o access (mask %#o)",
            path, mode, mask,
        )
        return False
    return True

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
# Refresh failure tracking (SRE-C9)
# ---------------------------------------------------------------------------
_refresh_failure_count = 0
_REFRESH_FAILURE_ALERT_THRESHOLD = 3


def _record_refresh_failure(err):
    global _refresh_failure_count
    _refresh_failure_count += 1
    if _refresh_failure_count >= _REFRESH_FAILURE_ALERT_THRESHOLD:
        log.error(
            "OAuth refresh failed %d consecutive times: %s. "
            "Bot will start returning 401 from Anthropic API soon.",
            _refresh_failure_count, err,
        )
    else:
        log.warning("OAuth refresh failed (%d/%d): %s",
                    _refresh_failure_count, _REFRESH_FAILURE_ALERT_THRESHOLD, err)


def _record_refresh_success():
    global _refresh_failure_count
    if _refresh_failure_count > 0:
        log.info("OAuth refresh recovered after %d failures", _refresh_failure_count)
    _refresh_failure_count = 0


def is_oauth_healthy() -> bool:
    """Public helper for health endpoint — True if last refresh succeeded
    or no failures recorded yet."""
    return _refresh_failure_count < _REFRESH_FAILURE_ALERT_THRESHOLD


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_access_token: str | None = None
_refresh_token: str | None = None
_expires_at: int = 0  # epoch ms
_http_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


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
    """Write updated tokens back to the credentials file (atomic write).

    H-2 defence:
      * verify ``~/.claude`` is owned by us and has no g/o bits before
        we create a temp file in it (otherwise a co-tenant could race us
        to the ``os.replace``)
      * verify any existing ``.credentials.json`` is also privately-owned
        so we're not overwriting a file attached to someone else
      * fchmod the new fd to 0o600 BEFORE any content lands in it
    """
    try:
        creds_dir = os.path.dirname(CREDENTIALS_PATH)
        if not _verify_path_privacy(creds_dir, is_dir=True):
            log.error("Refusing to save credentials: %s is not private", creds_dir)
            return
        if not _verify_path_privacy(CREDENTIALS_PATH, is_dir=False):
            log.error("Refusing to overwrite credentials: %s is not private", CREDENTIALS_PATH)
            return

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

        # Atomic write: fd → chmod → write → rename. Order matters: we
        # tighten permissions BEFORE writing any token bytes so there is
        # no window in which another reader could grab plaintext tokens
        # from an 0o600-untightened tempfile.
        fd, tmp_path = tempfile.mkstemp(dir=creds_dir, suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, CREDENTIALS_PATH)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
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
        _record_refresh_failure("no refresh token available")
        return False

    body = {
        "grant_type": "refresh_token",
        "refresh_token": _refresh_token,
        "client_id": CLIENT_ID,
        "scope": " ".join(REFRESH_SCOPES),
    }

    try:
        session = await _get_session()
        async with session.post(
            TOKEN_URL,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                _record_refresh_failure(f"HTTP {resp.status}: {text}")
                return False

            data = await resp.json()

        new_access = data["access_token"]
        new_refresh = data.get("refresh_token", _refresh_token)
        expires_in = data["expires_in"]  # seconds
        new_expires_at = int(time.time() * 1000) + expires_in * 1000

        _access_token = new_access
        _refresh_token = new_refresh
        _expires_at = new_expires_at

        # auth_token is injected directly via SDK patch, no env var needed

        # Persist to disk
        _save_credentials(_access_token, _refresh_token, _expires_at)

        _record_refresh_success()
        log.info("OAuth token refreshed, expires in %ds", expires_in)
        return True

    except Exception as exc:
        _record_refresh_failure(exc)
        return False


async def ensure_valid_token() -> str | None:
    """Return a valid access token, refreshing if needed.

    Raises OAuthRefreshFailed when the token is expired AND refresh has
    consecutively failed >= _REFRESH_FAILURE_ALERT_THRESHOLD times.
    """
    global _access_token

    if _access_token and not _is_expired():
        return _access_token

    if _is_expired() and _refresh_token:
        log.info("OAuth token expired or expiring soon, refreshing")
        if await refresh_token():
            return _access_token

    # Token is expired and refresh is not recovering — raise if at threshold.
    if _is_expired() and _refresh_failure_count >= _REFRESH_FAILURE_ALERT_THRESHOLD:
        raise OAuthRefreshFailed(
            f"OAuth token expired and refresh has failed {_refresh_failure_count} consecutive times"
        )

    return _access_token


def get_access_token() -> str | None:
    """Synchronous getter for the current access token (no refresh)."""
    return _access_token


# ---------------------------------------------------------------------------
# SDK Patching
# ---------------------------------------------------------------------------
_patched = False

# Captured before _patch_anthropic_sdk() reassigns __init__, so callers that
# need the un-patched constructor (e.g. restricted-mode API-key clients in
# services/claude_api/restricted.py) can bypass OAuth injection cleanly.
_orig_anthropic_init = None
_orig_async_anthropic_init = None


def _patch_anthropic_sdk() -> None:
    """Monkey-patch anthropic.Anthropic so all instances use OAuth Bearer auth.

    Mem0 internally does `anthropic.Anthropic(api_key=...)` which sets the
    x-api-key header. We intercept this to drop api_key and inject auth_token
    + the required beta header instead.

    Only the sync ``Anthropic`` class is patched. ``AsyncAnthropic`` is NOT
    touched — services/claude_api uses ``make_async_oauth_client()`` below
    to direct-inject the token instead. Mem0 only uses the sync client.
    """
    global _patched, _orig_anthropic_init, _orig_async_anthropic_init
    if _patched:
        return

    try:
        import anthropic

        _orig_anthropic_init = anthropic.Anthropic.__init__
        # Capture the async constructor too so make_async_*_client can build
        # patch-free instances without re-import dance. Defensive getattr so
        # the existing test harness that mocks `anthropic` with a stub still
        # works.
        _async_cls = getattr(anthropic, "AsyncAnthropic", None)
        _orig_async_anthropic_init = (
            _async_cls.__init__ if _async_cls is not None else None
        )

        def _oauth_init(self, **kwargs):
            # Drop api_key — we use auth_token directly (NOT env var)
            kwargs.pop("api_key", None)
            kwargs["auth_token"] = _access_token
            # Merge beta header
            headers = dict(kwargs.pop("default_headers", None) or {})
            existing_beta = headers.get("anthropic-beta", "")
            if OAUTH_BETA_HEADER not in existing_beta:
                beta = f"{existing_beta},{OAUTH_BETA_HEADER}".strip(",") if existing_beta else OAUTH_BETA_HEADER
                headers["anthropic-beta"] = beta
            kwargs["default_headers"] = headers
            _orig_anthropic_init(self, **kwargs)

        anthropic.Anthropic.__init__ = _oauth_init
        _patched = True
        log.info("Anthropic SDK patched for OAuth Bearer auth")
    except ImportError:
        log.warning("anthropic package not installed, skipping SDK patch")


def _merge_oauth_headers(headers: dict | None) -> dict:
    out = dict(headers or {})
    existing_beta = out.get("anthropic-beta", "")
    if OAUTH_BETA_HEADER not in existing_beta:
        out["anthropic-beta"] = (
            f"{existing_beta},{OAUTH_BETA_HEADER}".strip(",")
            if existing_beta else OAUTH_BETA_HEADER
        )
    return out


def make_async_oauth_client(**kwargs):
    """Build an AsyncAnthropic that authenticates via the OAuth Bearer token.

    The sync-side monkey patch does not cover AsyncAnthropic, so this factory
    direct-injects ``auth_token`` instead of relying on the patch.
    """
    import anthropic
    if _access_token is None:
        raise RuntimeError("OAuth not initialized; call init() first")
    kwargs.pop("api_key", None)
    kwargs["auth_token"] = _access_token
    kwargs["default_headers"] = _merge_oauth_headers(kwargs.pop("default_headers", None))
    return anthropic.AsyncAnthropic(**kwargs)


def make_async_api_key_client(api_key: str, **kwargs):
    """Build an AsyncAnthropic that bills against a separate API key.

    Used by the restricted-mode path so non-admin calls do not consume the
    OAuth subscription quota. Does NOT go through the sync-side OAuth patch.
    """
    import anthropic
    kwargs.pop("auth_token", None)  # never mix
    kwargs["api_key"] = api_key
    # OAUTH_BETA_HEADER is OAuth-subscriber-only; for API-key billing we omit it.
    return anthropic.AsyncAnthropic(**kwargs)


def make_sync_api_key_client(api_key: str, **kwargs):
    """Build a sync ``Anthropic`` client bypassing the OAuth patch.

    Kept for legacy ``call_direct``-style callers that still use the sync
    SDK and need a different billing account from OAuth.
    """
    import anthropic
    if _orig_anthropic_init is None:
        # Patch not installed yet — fall back to plain constructor.
        return anthropic.Anthropic(api_key=api_key, **kwargs)
    client = anthropic.Anthropic.__new__(anthropic.Anthropic)
    _orig_anthropic_init(client, api_key=api_key, **kwargs)
    return client


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

    # Do NOT set ANTHROPIC_AUTH_TOKEN env var — it leaks to CLI subprocesses
    # and causes "OAuth authentication is currently not supported" errors.
    # Instead, the SDK monkey-patch injects auth_token directly.
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
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
