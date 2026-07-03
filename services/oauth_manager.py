"""OAuth token manager — reads Claude Code OAuth tokens and patches Anthropic SDK.

Reads tokens from ~/.claude/.credentials.json (same file Claude Code uses),
handles automatic refresh via the platform.claude.com token endpoint,
and monkey-patches the Anthropic SDK so that all clients (including Mem0's
internal client) use Bearer auth instead of x-api-key.

二重アカウント対応（2026-07-02）: primary（~/.claude/.credentials.json、CLI と共有）
と failover（~/.claude/.credentials-failover.json、旧契約アカウント）の 2 アカウント
を保持し、quota 切れ / OAuth 恒久死で failover へ切替える。refresh の書き戻し先は
**必ずそのアカウント自身の credentials ファイル**（failover の refresh が primary
ファイルを clobber しない — 片方の login し直しがもう片方を壊す事故の恒久封じ）。
切替状態は .state/oauth_failover.json に永続化され再起動を跨ぐ。

Reference: free-code/src/services/oauth/client.ts
"""

import json
import logging
import os
import stat
import tempfile
import time
from dataclasses import dataclass, field

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
FAILOVER_CREDENTIALS_PATH = os.environ.get(
    "OAUTH_FAILOVER_CREDENTIALS_PATH",
    os.path.expanduser("~/.claude/.credentials-failover.json"),
)
# 切替機能のフラグ（OAUTH_NO_REFRESH と同じく env 直読み — config より前に
# import されるモジュールなので config 依存を作らない）
OAUTH_FAILOVER_ENABLED = os.environ.get(
    "OAUTH_FAILOVER_ENABLED", "true",
).lower() not in ("0", "false", "no", "off", "")
# 切替状態の永続先（BOT_DIR/.state — config.STATE_DIR と同じ場所を自前解決）
FAILOVER_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".state", "oauth_failover.json",
)
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
# Refresh failure tracking (SRE-C9) — per-account（二重アカウント対応）
# ---------------------------------------------------------------------------
_REFRESH_FAILURE_ALERT_THRESHOLD = 3


# ---------------------------------------------------------------------------
# State — per-account
# ---------------------------------------------------------------------------
@dataclass
class _AccountState:
    """1 アカウント分の OAuth 状態。refresh の書き戻し先は credentials_path。"""
    name: str                       # "primary" | "failover"
    credentials_path: str
    access_token: "str | None" = None
    refresh_token: "str | None" = None
    expires_at: int = 0             # epoch ms
    refresh_failure_count: int = 0
    # True when the last refresh failure was permanent (invalid_grant).
    refresh_permanent: bool = False
    session_primed: bool = False


_accounts: dict[str, _AccountState] = {
    "primary": _AccountState("primary", CREDENTIALS_PATH),
    "failover": _AccountState("failover", FAILOVER_CREDENTIALS_PATH),
}
_active: str = "primary"
# quota 起因 failover 時の primary 再試行時刻（epoch 秒）。auth 起因は None
#（手動復旧 = /login による credential 更新の検知まで張り付き）。
_primary_retry_at: "float | None" = None
_failover_reason: "str | None" = None
# failover 発動時刻。auth 起因の自動復旧判定（primary credential の mtime が
# これより新しい = /login し直された）に使う。
_failover_since: float = 0.0
# auth 復旧を試して駄目だった credential の mtime（同じファイルで毎リクエスト
# refresh を空振りしない）。
_auth_failback_tried_mtime: "float | None" = None
# 強制 failback（failover 死亡時の切り戻し）の最終試行時刻。空振り連打防止。
_last_forced_failback_at: float = 0.0
_FORCED_FAILBACK_COOLDOWN_S = 300.0

_http_session: "aiohttp.ClientSession | None" = None


def _acct() -> _AccountState:
    return _accounts[_active]


def get_active_account() -> str:
    """現在アクティブなアカウント名（"primary" | "failover"）。"""
    return _active


def _record_refresh_failure(account: _AccountState, err) -> None:
    account.refresh_failure_count += 1
    if account.refresh_failure_count >= _REFRESH_FAILURE_ALERT_THRESHOLD:
        log.error(
            "OAuth refresh failed %d consecutive times (account=%s): %s. "
            "Bot will start returning 401 from Anthropic API soon.",
            account.refresh_failure_count, account.name, err,
        )
    else:
        log.warning(
            "OAuth refresh failed (%d/%d, account=%s): %s",
            account.refresh_failure_count, _REFRESH_FAILURE_ALERT_THRESHOLD,
            account.name, err,
        )


def _record_refresh_success(account: _AccountState) -> None:
    if account.refresh_failure_count > 0:
        log.info(
            "OAuth refresh recovered after %d failures (account=%s)",
            account.refresh_failure_count, account.name,
        )
    account.refresh_failure_count = 0
    account.refresh_permanent = False


def is_oauth_healthy() -> bool:
    """Public helper for health endpoint — True if last refresh succeeded
    or no failures recorded yet (active account 基準)."""
    return _acct().refresh_failure_count < _REFRESH_FAILURE_ALERT_THRESHOLD

# Subprocess mode: when this is true, ensure_valid_token() never invokes
# refresh_token(). Instead it re-reads credentials from disk so the
# subprocess picks up tokens refreshed by the parent. The bot's main
# process and any MCP servers it spawns share `~/.claude/credentials.json`,
# but each runs `services.oauth_manager` in its own Python interpreter —
# without this gate both ends race on refresh and invalidate each other's
# in-flight tokens. Set OAUTH_NO_REFRESH=1 in child processes only.
OAUTH_NO_REFRESH = os.environ.get("OAUTH_NO_REFRESH", "0") == "1"


async def _get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


def _read_credentials(path: "str | None" = None) -> dict | None:
    """Read Claude Code OAuth credentials from disk."""
    if path is None:
        path = CREDENTIALS_PATH  # 実行時解決（テストの monkeypatch を尊重）
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("claudeAiOauth")
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
        log.warning("Failed to read OAuth credentials from %s: %s", path, exc)
        return None


def _save_credentials(
    access: str, refresh: str, expires_at: int, extra: dict | None = None,
    *, path: "str | None" = None,
) -> None:
    """Write updated tokens back to the credentials file (atomic write).

    H-2 defence:
      * verify ``~/.claude`` is owned by us and has no g/o bits before
        we create a temp file in it (otherwise a co-tenant could race us
        to the ``os.replace``)
      * verify any existing ``.credentials.json`` is also privately-owned
        so we're not overwriting a file attached to someone else
      * fchmod the new fd to 0o600 BEFORE any content lands in it
    """
    if path is None:
        path = CREDENTIALS_PATH  # 実行時解決（テストの monkeypatch を尊重）
    try:
        creds_dir = os.path.dirname(path)
        if not _verify_path_privacy(creds_dir, is_dir=True):
            log.error("Refusing to save credentials: %s is not private", creds_dir)
            return
        if not _verify_path_privacy(path, is_dir=False):
            log.error("Refusing to overwrite credentials: %s is not private", path)
            return

        try:
            with open(path) as f:
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
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        log.warning("Failed to save OAuth credentials: %s", exc)


def _is_expired(account: "_AccountState | None" = None) -> bool:
    """Check if the account's access token is expired (with 5-min buffer)."""
    acct = account if account is not None else _acct()
    now_ms = int(time.time() * 1000)
    return (now_ms + EXPIRY_BUFFER_MS) >= acct.expires_at


async def refresh_token(account: "_AccountState | None" = None) -> bool:
    """Refresh the OAuth access token using the refresh token.

    書き戻し先は必ず ``account.credentials_path``（failover の refresh が
    primary ファイルを clobber しない）。引数省略時は active アカウント。
    """
    acct = account if account is not None else _acct()

    if not acct.refresh_token:
        _record_refresh_failure(acct, "no refresh token available")
        return False

    body = {
        "grant_type": "refresh_token",
        "refresh_token": acct.refresh_token,
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
                # Detect permanent failure (invalid_grant = refresh token
                # revoked/expired): retrying cannot recover it, and
                # ensure_valid_token() will raise OAuthRefreshFailed
                # immediately on the next expired-token check.
                is_permanent = False
                try:
                    data_err = json.loads(text)
                    if data_err.get("error") == "invalid_grant":
                        is_permanent = True
                except (json.JSONDecodeError, AttributeError):
                    pass
                if "invalid_grant" in text:
                    is_permanent = True
                if is_permanent:
                    acct.refresh_permanent = True
                    log.error(
                        "OAuth refresh token invalid (invalid_grant, "
                        "account=%s) — re-login required: %s",
                        acct.name, text,
                    )
                _record_refresh_failure(acct, f"HTTP {resp.status}: {text}")
                return False

            data = await resp.json()

        new_access = data["access_token"]
        new_refresh = data.get("refresh_token", acct.refresh_token)
        expires_in = data["expires_in"]  # seconds
        new_expires_at = int(time.time() * 1000) + expires_in * 1000

        acct.access_token = new_access
        acct.refresh_token = new_refresh
        acct.expires_at = new_expires_at

        # auth_token is injected directly via SDK patch, no env var needed

        # Persist to disk — そのアカウント自身のファイルへ
        _save_credentials(
            acct.access_token, acct.refresh_token, acct.expires_at,
            path=acct.credentials_path,
        )

        _record_refresh_success(acct)
        log.info(
            "OAuth token refreshed (account=%s), expires in %ds",
            acct.name, expires_in,
        )
        return True

    except Exception as exc:
        _record_refresh_failure(acct, exc)
        return False


async def ensure_valid_token() -> str | None:
    """Return a valid access token for the ACTIVE account, refreshing if needed.

    Raises OAuthRefreshFailed when the token is expired AND refresh has
    consecutively failed >= _REFRESH_FAILURE_ALERT_THRESHOLD times.

    In OAUTH_NO_REFRESH mode (subprocesses like mcp_mem0_server) we never
    refresh — we re-read the credentials file so a token rotated by the
    parent is picked up. This avoids the parent/child refresh race where
    each end invalidates the other's in-flight access token.
    （v1 制約: 子プロセスは常に primary ファイルを読む。failover 中の子は
    親の切替に追従しない — 将来 .state/oauth_failover.json を見て読み先を
    切替える拡張余地あり。子は書かないので clobber リスクは無い。）
    """
    acct = _acct()

    if OAUTH_NO_REFRESH:
        # Reload from disk in case the parent process refreshed.
        creds = _read_credentials(CREDENTIALS_PATH)
        primary = _accounts["primary"]
        if creds:
            disk_token = creds.get("accessToken")
            disk_expires = creds.get("expiresAt", 0)
            if disk_token and disk_token != primary.access_token:
                primary.access_token = disk_token
                primary.refresh_token = creds.get(
                    "refreshToken", primary.refresh_token)
                primary.expires_at = disk_expires
                log.info("OAuth token reloaded from disk (no-refresh mode)")
        return primary.access_token

    if acct.access_token and not _is_expired(acct):
        return acct.access_token

    if _is_expired(acct) and acct.refresh_token:
        # Permanent failure (invalid_grant): raise immediately without retrying.
        if acct.refresh_permanent:
            raise OAuthRefreshFailed(
                "refresh token invalid (invalid_grant) — re-login required"
            )
        log.info(
            "OAuth token expired or expiring soon, refreshing (account=%s)",
            acct.name,
        )
        if await refresh_token(acct):
            return acct.access_token

    # Token is expired and refresh is not recovering — raise if at threshold.
    if (
        _is_expired(acct)
        and acct.refresh_failure_count >= _REFRESH_FAILURE_ALERT_THRESHOLD
    ):
        raise OAuthRefreshFailed(
            f"OAuth token expired and refresh has failed "
            f"{acct.refresh_failure_count} consecutive times"
        )

    return acct.access_token


def get_access_token() -> str | None:
    """Synchronous getter for the active account's access token (no refresh)."""
    return _acct().access_token


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
            kwargs["auth_token"] = _acct().access_token
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
    """Merge OAuth-required headers into the caller-supplied dict.

    Adds:
    - ``anthropic-beta: oauth-2025-04-20`` (subscriber-tier flag).
    - ``anthropic-dangerous-direct-browser-access: true`` — the real CLI
      sends this on every OAuth-authenticated request (mitmproxy capture
      2026-05-13). Absence appears to be what triggered HTTP 429 on
      first-call from our refactor smoke.
    """
    out = dict(headers or {})
    existing_beta = out.get("anthropic-beta", "")
    if OAUTH_BETA_HEADER not in existing_beta:
        out["anthropic-beta"] = (
            f"{existing_beta},{OAUTH_BETA_HEADER}".strip(",")
            if existing_beta else OAUTH_BETA_HEADER
        )
    out.setdefault("anthropic-dangerous-direct-browser-access", "true")
    return out


async def _sanitize_outgoing_request(request) -> None:
    """httpx event hook: strip Python-SDK-only headers + dedupe.

    Diff source: mitmproxy capture of the real claude-cli 2.1.92 vs our
    outgoing request. Every header below appears only on the Python
    side and is a likely tell to subscriber-tier gating:
        x-stainless-async         (Python AsyncAnthropic only)
        x-stainless-helper-method (Python SDK convenience layer)
        x-stainless-stream-helper (ditto)
        x-stainless-read-timeout  (Python SDK only; CLI uses just
                                    x-stainless-timeout)

    Also collapse the duplicated x-stainless-timeout to the single CLI
    value and pretend we accept brotli (the real CLI does).
    """
    h = request.headers
    for k in (
        "x-stainless-async",
        "x-stainless-helper-method",
        "x-stainless-stream-helper",
        "x-stainless-read-timeout",
    ):
        if k in h:
            del h[k]
    timeouts = h.get_list("x-stainless-timeout")
    if len(timeouts) > 1 or (timeouts and "NOT_GIVEN" in timeouts[0]):
        del h["x-stainless-timeout"]
        h["x-stainless-timeout"] = "600"
    # The Node SDK advertises brotli; httpx omits it by default.
    h["accept-encoding"] = "br, gzip, deflate"


# Node.js v24's tls.DEFAULT_CIPHERS list (BoringSSL preference order). Used by
# anthropic-sdk on every /v1/messages POST. Python's ssl module backs onto
# OpenSSL so we cannot match BoringSSL's TLS extension layout / GREASE bytes
# — JA3 / JA4 will still hash differently — but we can at least pin the
# TLS 1.2 cipher-suite preference order so the Client Hello no longer carries
# the CPython-default "HIGH:!aNULL" fingerprint that screams "Python httpx".
_NODEJS_TLS12_CIPHER_ORDER = (
    "ECDHE-ECDSA-AES128-GCM-SHA256:"
    "ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:"
    "ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:"
    "ECDHE-RSA-CHACHA20-POLY1305:"
    "DHE-RSA-AES128-GCM-SHA256:"
    "DHE-RSA-AES256-GCM-SHA384"
)


def _make_nodejs_ssl_context():
    """Approximate Node.js v24 anthropic-sdk's TLS handshake fingerprint.

    What this DOES:
      * Pins TLS 1.2 cipher-suite preference to Node.js v24's BoringSSL order
      * Advertises ALPN ``['h2', 'http/1.1']`` (Node anthropic-sdk uses HTTP/2)
      * Falls under the default minimum TLS 1.2 floor

    What this does NOT do:
      * Match BoringSSL TLS 1.3 cipher order (OpenSSL emits a fixed list,
        BoringSSL emits another fixed list — we can't reorder TLS 1.3 from
        Python's stdlib ssl module)
      * Match the TLS extension order / GREASE bytes that JA4 hashes
      * Defeat a real JA3/JA4 deny-list — for that, curl_cffi w/ Chrome
        impersonation is the only option

    The point is to disappear from "obvious CPython default" outlier
    bucket, not to perfectly impersonate a Node client.
    """
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.set_ciphers(_NODEJS_TLS12_CIPHER_ORDER)
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    return ctx


def _make_http_client_with_hook():
    import httpx
    from services.claude_api.attestation import cch_request_hook
    return httpx.AsyncClient(
        event_hooks={"request": [_sanitize_outgoing_request, cch_request_hook]},
        timeout=600.0,
        http2=True,
        verify=_make_nodejs_ssl_context(),
    )


async def prime_subscriber_session() -> None:
    """Replay the CLI's pre-/v1/messages requests so server-side gating
    treats us as an active subscriber.

    The real claude-cli always calls these GET endpoints (with the same
    OAuth Bearer token) before its first /v1/messages POST:

        GET https://api.anthropic.com/api/oauth/profile
        GET https://api.anthropic.com/api/oauth/claude_cli/roles

    We've observed that without those preludes our /v1/messages calls
    come back HTTP 429 with zero anthropic-ratelimit-unified-* headers,
    which is the signature of "subscriber not recognized" rather than
    "quota exhausted". Replaying them is cheap (~200 ms total) and is
    only done once per account per process（アカウント切替時は新アカウントで
    再 prime される）.
    """
    acct = _acct()
    if acct.session_primed:
        return
    if acct.access_token is None:
        return
    try:
        import aiohttp
    except ImportError:
        log.warning("aiohttp not available, skipping session prime")
        return
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {acct.access_token}",
        "User-Agent": "axios/1.13.6",
        "Accept-Encoding": "gzip, compress, deflate, br",
    }
    urls = [
        "https://api.anthropic.com/api/oauth/profile",
        "https://api.anthropic.com/api/oauth/claude_cli/roles",
    ]
    try:
        async with aiohttp.ClientSession() as sess:
            for url in urls:
                try:
                    async with sess.get(url, headers=headers, timeout=10) as resp:
                        log.info(
                            "session prime (account=%s): %s → HTTP %d",
                            acct.name, url, resp.status,
                        )
                except Exception as exc:
                    log.warning("session prime %s failed: %s", url, exc)
        acct.session_primed = True
    except Exception as exc:
        log.warning("session prime aborted: %s", exc)


def make_async_oauth_client(**kwargs):
    """Build an AsyncAnthropic that authenticates via the OAuth Bearer token.

    The sync-side monkey patch does not cover AsyncAnthropic, so this factory
    direct-injects ``auth_token`` instead of relying on the patch. We also
    install a custom httpx client whose request hook strips Python-only
    Stainless headers so the call looks more like the Node CLI.
    """
    import anthropic
    acct = _acct()
    if acct.access_token is None:
        raise RuntimeError("OAuth not initialized; call init() first")
    kwargs.pop("api_key", None)
    kwargs["auth_token"] = acct.access_token
    kwargs["default_headers"] = _merge_oauth_headers(kwargs.pop("default_headers", None))
    kwargs.setdefault("http_client", _make_http_client_with_hook())
    return anthropic.AsyncAnthropic(**kwargs)


def make_async_api_key_client(api_key: str, **kwargs):
    """Build an AsyncAnthropic that bills against a separate API key.

    Used by the restricted-mode path so non-admin calls do not consume the
    OAuth subscription quota. Does NOT go through the sync-side OAuth patch
    and does NOT add OAUTH_BETA_HEADER (subscriber-tier only). Same
    Stainless-header-sanitizing http_client hook as the OAuth path.
    """
    import anthropic
    kwargs.pop("auth_token", None)  # never mix
    kwargs["api_key"] = api_key
    kwargs.setdefault("http_client", _make_http_client_with_hook())
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

def _load_account_from_disk(account: _AccountState) -> bool:
    """credentials ファイルからアカウント状態をロードする。"""
    creds = _read_credentials(account.credentials_path)
    if not creds or not creds.get("accessToken"):
        return False
    account.access_token = creds.get("accessToken")
    account.refresh_token = creds.get("refreshToken")
    account.expires_at = creds.get("expiresAt", 0)
    return True


def init() -> bool:
    """Initialize OAuth manager: load tokens, set env, patch SDK.

    Call this BEFORE importing/initializing Mem0.
    Returns True if a valid token was loaded for the ACTIVE account.
    primary をロードし、failover credentials があればそれもロードする
    （無くても失敗にしない — 単一アカウント運用の後方互換）。
    `.state/oauth_failover.json` から active を復元する。
    """
    # パスを実行時解決（テストの monkeypatch / env 変更を尊重）
    _accounts["primary"].credentials_path = CREDENTIALS_PATH
    _accounts["failover"].credentials_path = FAILOVER_CREDENTIALS_PATH

    primary_ok = _load_account_from_disk(_accounts["primary"])
    if not primary_ok:
        log.error("No OAuth credentials found at %s", CREDENTIALS_PATH)

    failover_ok = _load_account_from_disk(_accounts["failover"])
    if failover_ok:
        log.info(
            "OAuth failover credentials loaded (%s)",
            FAILOVER_CREDENTIALS_PATH,
        )

    _load_failover_state()
    if _active == "failover" and not failover_ok:
        # 状態は failover を指すが creds が消えている → primary へフォールバック
        log.error(
            "failover state present but %s unreadable — falling back to primary",
            FAILOVER_CREDENTIALS_PATH,
        )
        _set_active("primary", reason=None, retry_at=None)

    if _acct().access_token is None:
        log.error("No access token in credentials (active=%s)", _active)
        return False

    # Do NOT set ANTHROPIC_AUTH_TOKEN env var — it leaks to CLI subprocesses
    # and causes "OAuth authentication is currently not supported" errors.
    # Instead, the SDK monkey-patch injects auth_token directly.
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    os.environ.pop("ANTHROPIC_API_KEY", None)

    # Patch the SDK
    _patch_anthropic_sdk()

    acct = _acct()
    expired_str = "EXPIRED" if _is_expired(acct) else "valid"
    log.info(
        "OAuth loaded — active=%s token %s, expires_at=%d, failover_available=%s",
        _active, expired_str, acct.expires_at, failover_ok,
    )

    return True


# ---------------------------------------------------------------------------
# Failover（二重アカウント切替）
# ---------------------------------------------------------------------------

def _set_active(name: str, *, reason: "str | None", retry_at: "float | None") -> None:
    global _active, _failover_reason, _primary_retry_at
    _active = name
    _failover_reason = reason
    _primary_retry_at = retry_at


def _persist_failover_state() -> None:
    try:
        from utils.atomic_write import atomic_write_json
        atomic_write_json(FAILOVER_STATE_PATH, {
            "active": _active,
            "reason": _failover_reason,
            "primary_retry_at": _primary_retry_at,
            # 発動時刻を保持（retry 延長等の再 persist で上書きしない —
            # auth 自動復旧の mtime 比較の基準になる）。
            "since": _failover_since or time.time(),
        })
    except Exception as exc:  # noqa: BLE001 - 永続化失敗は運転継続
        log.warning("failover state persist failed (non-fatal): %s", exc)


def _clear_failover_state() -> None:
    try:
        os.remove(FAILOVER_STATE_PATH)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("failover state remove failed: %s", exc)


def _load_failover_state() -> None:
    global _active, _failover_reason, _primary_retry_at, _failover_since
    try:
        with open(FAILOVER_STATE_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if data.get("active") == "failover":
        _active = "failover"
        _failover_reason = data.get("reason")
        _primary_retry_at = data.get("primary_retry_at")
        try:
            _failover_since = float(data.get("since") or 0.0)
        except (TypeError, ValueError):
            _failover_since = 0.0
        log.warning(
            "OAuth failover state restored: reason=%s primary_retry_at=%s",
            _failover_reason, _primary_retry_at,
        )


def failover_to_secondary(
    reason: str, *, primary_retry_at: "float | None" = None,
) -> bool:
    """primary → failover へ切替える。成功で True。

    reason: "quota"（reset 時刻で自動復帰）| "auth"（手動復旧まで張り付き）。
    failover credentials が無い/壊れている/フラグ OFF/既に failover/子プロセス
    では False（呼び元は従来のエラー処理を続行する）。
    """
    if not OAUTH_FAILOVER_ENABLED or OAUTH_NO_REFRESH:
        return False
    if _active == "failover":
        return False
    failover = _accounts["failover"]
    if not _load_account_from_disk(failover):
        log.error(
            "oauth failover requested (reason=%s) but %s is missing/unreadable",
            reason, FAILOVER_CREDENTIALS_PATH,
        )
        return False
    failover.refresh_failure_count = 0
    failover.refresh_permanent = False
    failover.session_primed = False
    retry_at = primary_retry_at if reason != "auth" else None
    global _failover_since, _auth_failback_tried_mtime
    _failover_since = time.time()
    _auth_failback_tried_mtime = None
    _set_active("failover", reason=reason, retry_at=retry_at)
    _persist_failover_state()
    try:
        from services.claude_api.ratelimit import reset_state
        reset_state()
    except Exception:  # noqa: BLE001 - ratelimit 未import 文脈でも切替は成立させる
        pass
    log.warning(
        "OAuth FAILOVER: primary → failover (reason=%s, primary_retry_at=%s)",
        reason, retry_at,
    )
    return True


async def _revive_primary() -> bool:
    """primary credential を読み直し、失効していれば refresh を試す。

    True = primary は使える状態（切替はしない — 呼び元が行う）。
    """
    primary = _accounts["primary"]
    if not _load_account_from_disk(primary):
        return False
    if _is_expired(primary):
        primary.refresh_permanent = False
        primary.refresh_failure_count = 0
        if not await refresh_token(primary):
            return False
    return True


def _activate_primary(log_msg: str) -> None:
    """active を primary に戻し、failover 状態と ratelimit 観測をクリアする。"""
    _accounts["primary"].session_primed = False
    _set_active("primary", reason=None, retry_at=None)
    _clear_failover_state()
    try:
        from services.claude_api.ratelimit import reset_state
        reset_state()
    except Exception:  # noqa: BLE001
        pass
    log.warning(log_msg)


async def _try_auth_failback() -> bool:
    """auth 起因 failover の自動復旧: primary credential が failover 発動後に
    更新されていれば（= 人が /login し直した）、復帰を試みる。

    以前は auth 起因 = 永久張り付きで、primary を復旧させても bot は failover
    を使い続けた（2026-07-03 障害: failover の週次 quota 枯渇と重なり全滅）。
    mtime を復旧シグナルとして使うことで、/login だけで自動復帰する。
    同じ credential ファイルでの再試行はしない（refresh 空振り連打防止）。
    """
    global _auth_failback_tried_mtime
    try:
        mtime = os.stat(_accounts["primary"].credentials_path).st_mtime
    except OSError:
        return False
    if _failover_since and mtime <= _failover_since:
        return False  # failover 発動より古い credential のまま（未復旧）
    if _auth_failback_tried_mtime == mtime:
        return False  # この credential では既に試して駄目だった
    _auth_failback_tried_mtime = mtime
    if not await _revive_primary():
        log.info("auth failback: renewed primary creds still invalid")
        return False
    _activate_primary(
        "OAuth FAILBACK: primary credentials renewed (/login detected) — "
        "failover → primary",
    )
    return True


async def maybe_restore_primary() -> bool:
    """failover 中に primary へ復帰できるなら復帰する。

    毎リクエスト冒頭から呼ばれる前提の軽量パス（非該当時は timestamp /
    mtime 比較のみ）。
    - quota 起因: `_primary_retry_at`（reset 時刻）を過ぎたら復帰試行
    - auth 起因: primary credential の mtime が failover 発動より新しければ
      （/login し直し）復帰試行
    """
    global _primary_retry_at
    if _active != "failover" or OAUTH_NO_REFRESH:
        return False
    if _primary_retry_at is None:
        return await _try_auth_failback()
    if time.time() < _primary_retry_at:
        return False
    if not await _revive_primary():
        # まだ駄目 — 10分後に再試行（リクエスト毎の refresh 空振り防止）
        _primary_retry_at = time.time() + 600
        _persist_failover_state()
        log.info("oauth failback attempt failed; retry in 600s")
        return False
    _activate_primary("OAuth FAILBACK: failover → primary")
    return True


async def failback_to_primary(reason: str) -> bool:
    """failover アカウントが死んだ（quota/auth）ときの強制切り戻し。

    2026-07-03 障害の対策: 切替が primary→failover の一方向しかなく、
    failover の週次 quota が枯渇すると「primary は /login で復活済みなのに
    全滅エラー」になった。active=failover で API エラーを観測した呼び元
    （entry._try_failover）がこれを呼び、primary が生きていれば戻す。
    primary も死んでいれば False（呼び元は従来のエラー処理続行）。
    空振り連打防止のクールダウン付き。
    """
    global _last_forced_failback_at
    if _active != "failover" or OAUTH_NO_REFRESH:
        return False
    now = time.time()
    if now - _last_forced_failback_at < _FORCED_FAILBACK_COOLDOWN_S:
        return False
    _last_forced_failback_at = now
    if not await _revive_primary():
        log.info(
            "forced failback (reason=%s): primary still dead — staying on "
            "failover", reason,
        )
        return False
    _activate_primary(
        f"OAuth FAILBACK (forced, reason={reason}): failover dead — "
        "failover → primary",
    )
    return True


def _reset_for_tests() -> None:
    """テスト用: モジュール状態を初期化する（credentials パスは呼び元で
    monkeypatch してから使うこと）。"""
    global _active, _failover_reason, _primary_retry_at
    global _failover_since, _auth_failback_tried_mtime, _last_forced_failback_at
    _accounts["primary"] = _AccountState("primary", CREDENTIALS_PATH)
    _accounts["failover"] = _AccountState("failover", FAILOVER_CREDENTIALS_PATH)
    _active = "primary"
    _failover_reason = None
    _primary_retry_at = None
    _failover_since = 0.0
    _auth_failback_tried_mtime = None
    _last_forced_failback_at = 0.0
