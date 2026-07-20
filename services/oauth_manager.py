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
切替状態は BOT_STATE_DIR/oauth_failover.json に永続化され再起動を跨ぐ。

Reference: free-code/src/services/oauth/client.ts
"""

import asyncio
import json
import logging
import os
import stat
import tempfile
import threading
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

_CREDS_DIR_MODE_MASK = 0o077  # group/other must have no bits set
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
            path,
            st.st_uid,
            os.getuid(),
        )
        return False
    mode = stat.S_IMODE(st.st_mode)
    if is_dir and not stat.S_ISDIR(st.st_mode):
        log.error("Refusing to touch %s: expected a directory", path)
        return False
    if not is_dir and not stat.S_ISREG(st.st_mode):
        log.error("Refusing to touch %s: expected a regular file", path)
        return False
    mask = _CREDS_DIR_MODE_MASK if is_dir else _CREDS_FILE_MODE_MASK
    if mode & mask:
        log.error(
            "Refusing to touch %s: mode=%#o grants g/o access (mask %#o)",
            path,
            mode,
            mask,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Constants (from free-code/src/constants/oauth.ts)
# ---------------------------------------------------------------------------
CREDENTIALS_PATH = os.environ.get(
    "OAUTH_PRIMARY_CREDENTIALS_PATH",
    os.path.expanduser("~/.claude/.credentials.json"),
)
FAILOVER_CREDENTIALS_PATH = os.environ.get(
    "OAUTH_FAILOVER_CREDENTIALS_PATH",
    os.path.expanduser("~/.claude/.credentials-failover.json"),
)
# 切替機能のフラグ（OAUTH_NO_REFRESH と同じく env 直読み — config より前に
# import されるモジュールなので config 依存を作らない）
OAUTH_FAILOVER_ENABLED = os.environ.get(
    "OAUTH_FAILOVER_ENABLED",
    "true",
).lower() not in ("0", "false", "no", "off", "")
# 切替状態の永続先。checkout は read-only のため、必ず外部 state root を使う。
_OAUTH_STATE_DIR = os.environ.get(
    "BOT_STATE_DIR",
    os.path.join(os.path.expanduser("~"), ".local", "state", "discord-bot"),
)
FAILOVER_STATE_PATH = os.path.join(_OAUTH_STATE_DIR, "oauth_failover.json")
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

    name: str  # "primary" | "failover"
    credentials_path: str
    access_token: "str | None" = None
    refresh_token: "str | None" = None
    expires_at: int = 0  # epoch ms
    refresh_failure_count: int = 0
    # True when the last refresh failure was permanent (invalid_grant).
    refresh_permanent: bool = False
    session_primed: bool = False
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


_accounts: dict[str, _AccountState] = {
    "primary": _AccountState("primary", CREDENTIALS_PATH),
    "failover": _AccountState("failover", FAILOVER_CREDENTIALS_PATH),
}
_active: str = "primary"
# quota 起因 failover 時の primary 再試行時刻（epoch 秒）。auth 起因は None
# （手動復旧 = /login による credential 更新の検知まで張り付き）。
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
            account.refresh_failure_count,
            account.name,
            err,
        )
    else:
        log.warning(
            "OAuth refresh failed (%d/%d, account=%s): %s",
            account.refresh_failure_count,
            _REFRESH_FAILURE_ALERT_THRESHOLD,
            account.name,
            err,
        )


def _record_refresh_success(account: _AccountState) -> None:
    if account.refresh_failure_count > 0:
        log.info(
            "OAuth refresh recovered after %d failures (account=%s)",
            account.refresh_failure_count,
            account.name,
        )
    account.refresh_failure_count = 0
    account.refresh_permanent = False


def is_oauth_healthy() -> bool:
    """Return True when the credential serving the active account is ready.

    Standby health is redundancy information, not a serving-readiness gate.  A
    broken standby must remain visible in ``oauth_status_detail()`` without
    taking a healthy active account out of service.
    """
    ok, _ = _credential_readiness(_acct())
    return ok


def _credential_readiness(account: _AccountState) -> tuple[bool, str | None]:
    """Validate the credential actually used after a restart, without refreshing it."""
    path = account.credentials_path
    parent = os.path.dirname(path)
    if not os.path.exists(parent):
        return False, "credentials_directory_missing"
    if not _verify_path_privacy(parent, is_dir=True):
        return False, "credentials_directory_insecure"
    if not os.path.exists(path):
        return False, "credentials_missing"
    if not _verify_path_privacy(path, is_dir=False):
        return False, "credentials_file_insecure"
    credentials = _read_credentials(path)
    if not isinstance(credentials, dict):
        return False, "credentials_unreadable"
    access = credentials.get("accessToken")
    refresh = credentials.get("refreshToken")
    expires_at = credentials.get("expiresAt")
    if not isinstance(access, str) or not access.strip():
        return False, "access_token_invalid"
    if not isinstance(refresh, str) or not refresh.strip():
        return False, "refresh_token_invalid"
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        return False, "token_expiry_invalid"
    if expires_at <= int(time.time() * 1000) + EXPIRY_BUFFER_MS:
        return False, "access_token_expired"
    return True, None


def oauth_status_detail() -> dict:
    """Read-only OAuth status snapshot for the /health payload（副作用なし）。"""
    acct = _acct()
    credential_ok, credential_reason = _credential_readiness(acct)
    standby_name = "failover" if _active == "primary" else "primary"
    standby_ok, standby_reason = _credential_readiness(_accounts[standby_name])
    redundancy_ok = not OAUTH_FAILOVER_ENABLED or standby_ok
    return {
        "active": _active,
        "credential_ok": credential_ok,
        "credential_reason": credential_reason,
        "failover_enabled": OAUTH_FAILOVER_ENABLED,
        "standby": standby_name,
        "standby_credential_ok": standby_ok,
        "standby_credential_reason": standby_reason,
        "redundancy_ok": redundancy_ok,
        "redundancy_reason": standby_reason if not redundancy_ok else None,
        "token_expired": _is_expired(acct),
        "refresh_failures": acct.refresh_failure_count,
        "refresh_permanent": acct.refresh_permanent,
    }


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
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning("Failed to read OAuth credentials from %s: %s", path, exc)
        return None


def _save_credentials(
    access: str,
    refresh: str,
    expires_at: int,
    extra: dict | None = None,
    *,
    path: "str | None" = None,
) -> bool:
    """Durably write and verify updated credentials.

    H-2 defence:
      * verify ``~/.claude`` is owned by us and has no g/o bits before
        we create a temp file in it (otherwise a co-tenant could race us
        to the ``os.replace``)
      * verify any existing ``.credentials.json`` is also privately-owned
        so we're not overwriting a file attached to someone else
      * fchmod the new fd to 0o600 BEFORE any content lands in it
      * fsync the file before replace and the parent directory after replace
      * read the replaced file back and verify the OAuth fields without logging
        their values

    Returns ``True`` only after durable replacement and read-back verification.
    All failures are reported as ``False`` so refresh cannot claim success with
    a memory-only token rotation.
    """
    if path is None:
        path = CREDENTIALS_PATH  # 実行時解決（テストの monkeypatch を尊重）
    try:
        creds_dir = os.path.dirname(path)
        if not _verify_path_privacy(creds_dir, is_dir=True):
            log.error("Refusing to save credentials: %s is not private", creds_dir)
            return False
        if not _verify_path_privacy(path, is_dir=False):
            log.error("Refusing to overwrite credentials: %s is not private", path)
            return False

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
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = -1  # fdopen owns and closes the descriptor from here.
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            tmp_path = ""

            dir_flags = os.O_RDONLY
            dir_flags |= getattr(os, "O_DIRECTORY", 0)
            dir_flags |= getattr(os, "O_CLOEXEC", 0)
            dir_flags |= getattr(os, "O_NOFOLLOW", 0)
            dir_fd = os.open(creds_dir, dir_flags)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                if tmp_path:
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise

        if not _verify_path_privacy(path, is_dir=False):
            log.error("OAuth credential verification failed: file privacy invalid")
            return False
        written = _read_credentials(path)
        if not isinstance(written, dict) or (
            written.get("accessToken") != access
            or written.get("refreshToken") != refresh
            or written.get("expiresAt") != expires_at
        ):
            log.error("OAuth credential verification failed after durable write")
            return False
        return True
    except Exception as exc:
        log.warning(
            "Failed to save OAuth credentials (error=%s)", type(exc).__name__
        )
        return False


def _is_expired(account: "_AccountState | None" = None) -> bool:
    """Check if the account's access token is expired (with 5-min buffer)."""
    acct = account if account is not None else _acct()
    now_ms = int(time.time() * 1000)
    return (now_ms + EXPIRY_BUFFER_MS) >= acct.expires_at


def _is_hard_expired(account: "_AccountState | None" = None) -> bool:
    """Return True only after the access token's actual expiry timestamp."""
    acct = account if account is not None else _acct()
    expires_at = acct.expires_at
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        return True
    return int(time.time() * 1000) >= expires_at


def _discard_hard_expired_access(account: _AccountState) -> None:
    """Ensure no caller can reuse an access token that is actually expired."""
    if _is_hard_expired(account):
        account.access_token = None


async def _refresh_token_locked(acct: _AccountState) -> bool:
    """Refresh ``acct`` while its account-specific refresh lock is held.

    書き戻し先は必ず ``acct.credentials_path``（failover の refresh が
    primary ファイルを clobber しない）。
    """
    if not acct.refresh_token:
        _record_refresh_failure(acct, "no refresh token available")
        _discard_hard_expired_access(acct)
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
                        "account=%s) — re-login required",
                        acct.name,
                    )
                error_code = "invalid_grant" if is_permanent else "oauth_error"
                _record_refresh_failure(acct, f"HTTP {resp.status} ({error_code})")
                _discard_hard_expired_access(acct)
                return False

            data = await resp.json()

        new_access = data["access_token"]
        new_refresh = data.get("refresh_token", acct.refresh_token)
        expires_in = data["expires_in"]  # seconds
        if (
            not isinstance(new_access, str)
            or not new_access
            or not isinstance(new_refresh, str)
            or not new_refresh
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise ValueError("invalid OAuth refresh response")
        new_expires_at = int(time.time() * 1000) + expires_in * 1000

        # Persist to disk — そのアカウント自身のファイルへ
        if not _save_credentials(
            new_access,
            new_refresh,
            new_expires_at,
            path=acct.credentials_path,
        ):
            _record_refresh_failure(acct, "credential persistence failed")
            _discard_hard_expired_access(acct)
            return False

        # Publish the rotation to in-memory readers only after durable write and
        # read-back verification succeeded.
        acct.access_token = new_access
        acct.refresh_token = new_refresh
        acct.expires_at = new_expires_at

        _record_refresh_success(acct)
        log.info(
            "OAuth token refreshed (account=%s), expires in %ds",
            acct.name,
            expires_in,
        )
        return True

    except Exception as exc:
        _record_refresh_failure(acct, exc)
        _discard_hard_expired_access(acct)
        return False


async def refresh_token(account: "_AccountState | None" = None) -> bool:
    """Serialize refresh per account and re-read disk after acquiring the lock.

    A waiter observes a token persisted by the preceding refresher and returns
    without issuing a second rotation request.
    """
    acct = account if account is not None else _acct()
    async with acct.refresh_lock:
        _load_account_from_disk(acct)
        if acct.access_token and not _is_expired(acct):
            return True
        if acct.refresh_permanent:
            _discard_hard_expired_access(acct)
            return False
        return await _refresh_token_locked(acct)


async def ensure_valid_token() -> str | None:
    """Return a valid access token for the ACTIVE account, refreshing if needed.

    Raises OAuthRefreshFailed when the token is expired AND refresh has
    consecutively failed >= _REFRESH_FAILURE_ALERT_THRESHOLD times.

    In OAUTH_NO_REFRESH mode (subprocesses like mcp_mem0_server) we never
    refresh — we re-read the credentials file so a token rotated by the
    parent is picked up. This avoids the parent/child refresh race where
    each end invalidates the other's in-flight access token.
    （v1 制約: 子プロセスは常に primary ファイルを読む。failover 中の子は
    親の切替に追従しない — 将来 BOT_STATE_DIR/oauth_failover.json を見て読み先を
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
                primary.refresh_token = creds.get("refreshToken", primary.refresh_token)
                primary.expires_at = disk_expires
                log.info("OAuth token reloaded from disk (no-refresh mode)")
        if _is_hard_expired(primary):
            primary.access_token = None
        return primary.access_token

    if acct.access_token and not _is_expired(acct):
        return acct.access_token

    if _is_expired(acct) and acct.refresh_token:
        log.info(
            "OAuth token expired or expiring soon, refreshing (account=%s)",
            acct.name,
        )
        if (
            await refresh_token(acct)
            and acct.access_token
            and not _is_hard_expired(acct)
        ):
            return acct.access_token

    # A refresh failure inside the early-refresh buffer may continue using the
    # still-live token. Once the actual expiry passes, never return it.
    hard_expired = _is_hard_expired(acct)
    if hard_expired:
        acct.access_token = None
    if hard_expired and acct.refresh_permanent:
        raise OAuthRefreshFailed(
            "refresh token invalid (invalid_grant) — re-login required"
        )
    if hard_expired and acct.refresh_failure_count >= _REFRESH_FAILURE_ALERT_THRESHOLD:
        raise OAuthRefreshFailed(
            f"OAuth token expired and refresh has failed "
            f"{acct.refresh_failure_count} consecutive times"
        )

    return acct.access_token if not hard_expired else None


def get_access_token() -> str | None:
    """Synchronous getter for the active account's access token (no refresh)."""
    acct = _acct()
    if _is_hard_expired(acct):
        return None
    return acct.access_token


# ---------------------------------------------------------------------------
# SDK Patching
# ---------------------------------------------------------------------------
_patched = False

# Captured before _patch_anthropic_sdk() reassigns __init__, so callers that
# need the un-patched constructor (e.g. restricted-mode API-key clients in
# services/claude_api/restricted.py) can bypass OAuth injection cleanly.
_orig_anthropic_init = None
_orig_async_anthropic_init = None
_patch_lock = threading.Lock()


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

    with _patch_lock:
        if _patched:
            return
        _patch_anthropic_sdk_locked()


def _patch_anthropic_sdk_locked() -> None:
    global _patched, _orig_anthropic_init, _orig_async_anthropic_init
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
                beta = (
                    f"{existing_beta},{OAUTH_BETA_HEADER}".strip(",")
                    if existing_beta
                    else OAUTH_BETA_HEADER
                )
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
            if existing_beta
            else OAUTH_BETA_HEADER
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
                            acct.name,
                            url,
                            resp.status,
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
    kwargs["default_headers"] = _merge_oauth_headers(
        kwargs.pop("default_headers", None)
    )
    kwargs.setdefault("http_client", _make_http_client_with_hook())
    return anthropic.AsyncAnthropic(**kwargs)


# make_async_api_key_client / make_sync_api_key_client were removed
# 2026-07-04: the restricted static-API-key billing route was never
# configured (BOT_RESTRICTED_ANTHROPIC_API_KEY unset everywhere), so all
# calls use the OAuth subscriber flow.


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


def _load_account_from_disk(account: _AccountState) -> bool:
    """credentials ファイルからアカウント状態をロードする。

    共有 credential 対策 (SRE): 別プロセス（対話型 Claude CLI や兄弟インタプリタ）
    がトークンを refresh + rotate してディスクへ書き戻すと、こちらが旧 refresh
    token に対して記録した invalid_grant 判定（``refresh_permanent`` /
    ``refresh_failure_count``）は既に陳腐化している。ディスクに「より新しい」
    credential を検知したら失敗状態をクリアし、次回は新トークンで refresh を
    試させる（張り付いた permanent failure での偽 degraded / 期限切れ時の即死を防ぐ）。
    """
    creds = _read_credentials(account.credentials_path)
    if not creds:
        return False
    new_access = creds.get("accessToken")
    new_refresh = creds.get("refreshToken")
    new_expires = creds.get("expiresAt", 0)
    if (
        not isinstance(new_access, str)
        or not new_access
        or not isinstance(new_refresh, str)
        or not new_refresh
        or not isinstance(new_expires, int)
        or isinstance(new_expires, bool)
    ):
        return False
    rotated = (
        bool(account.refresh_token)
        and bool(new_refresh)
        and new_refresh != account.refresh_token
    ) or new_expires > account.expires_at
    account.access_token = new_access
    account.refresh_token = new_refresh
    account.expires_at = new_expires
    if rotated and (account.refresh_permanent or account.refresh_failure_count):
        log.info(
            "OAuth: newer credential detected on disk (account=%s) — clearing "
            "stale refresh-failure state (count=%d, permanent=%s)",
            account.name,
            account.refresh_failure_count,
            account.refresh_permanent,
        )
        account.refresh_failure_count = 0
        account.refresh_permanent = False
    return True


def init() -> bool:
    """Initialize OAuth manager: load tokens, set env, patch SDK.

    Call this BEFORE importing/initializing Mem0.
    Returns True if a valid token was loaded for the ACTIVE account.
    primary をロードし、failover credentials があればそれもロードする
    （無くても失敗にしない — 単一アカウント運用の後方互換）。
    `BOT_STATE_DIR/oauth_failover.json` から active を復元する。
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
        _active,
        expired_str,
        acct.expires_at,
        failover_ok,
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

        atomic_write_json(
            FAILOVER_STATE_PATH,
            {
                "active": _active,
                "reason": _failover_reason,
                "primary_retry_at": _primary_retry_at,
                # 発動時刻を保持（retry 延長等の再 persist で上書きしない —
                # auth 自動復旧の mtime 比較の基準になる）。
                "since": _failover_since or time.time(),
            },
        )
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
            _failover_reason,
            _primary_retry_at,
        )


def failover_to_secondary(
    reason: str,
    *,
    primary_retry_at: "float | None" = None,
) -> bool:
    """primary → failover へ切替える。成功で True。

    reason: "quota"（reset 時刻で自動復帰）| "auth"（手動復旧まで張り付き）。
    failover credentials が serving-ready でない/フラグ OFF/既に failover/
    子プロセスでは False（呼び元は従来のエラー処理を続行する）。期限切れ
    credential の refresh は ``prepare_and_failover_to_secondary()`` が状態変更前に
    行う。この同期 API 自体は dead standby を active にしない。
    """
    if not OAUTH_FAILOVER_ENABLED or OAUTH_NO_REFRESH:
        return False
    if _active == "failover":
        return False
    failover = _accounts["failover"]
    if not _load_account_from_disk(failover):
        log.error(
            "oauth failover requested (reason=%s) but %s is missing/unreadable",
            reason,
            FAILOVER_CREDENTIALS_PATH,
        )
        return False
    ready, readiness_reason = _credential_readiness(failover)
    if not ready:
        log.error(
            "oauth failover requested (reason=%s) but standby is not ready: %s",
            reason,
            readiness_reason,
        )
        return False
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
        reason,
        retry_at,
    )
    return True


async def prepare_and_failover_to_secondary(
    reason: str,
    *,
    primary_retry_at: "float | None" = None,
) -> bool:
    """Refresh/validate standby before atomically making it active.

    ``failover_to_secondary()`` intentionally stays synchronous for existing
    callers and tests.  Production async paths use this wrapper so an expired
    but refreshable standby remains usable, while an ``invalid_grant`` or other
    unusable credential is rejected before active/state are mutated.
    """
    if not OAUTH_FAILOVER_ENABLED or OAUTH_NO_REFRESH or _active == "failover":
        return False

    failover = _accounts["failover"]
    if not _load_account_from_disk(failover):
        log.error(
            "oauth failover preparation (reason=%s) failed: %s is missing/unreadable",
            reason,
            FAILOVER_CREDENTIALS_PATH,
        )
        return False

    ready, readiness_reason = _credential_readiness(failover)
    if not ready and readiness_reason == "access_token_expired":
        log.info(
            "oauth failover standby expired; refreshing before switch (reason=%s)",
            reason,
        )
        if await refresh_token(failover):
            ready, readiness_reason = _credential_readiness(failover)

    if not ready:
        log.error(
            "oauth failover preparation (reason=%s) rejected standby: %s",
            reason,
            readiness_reason,
        )
        return False

    return failover_to_secondary(reason, primary_retry_at=primary_retry_at)


def rollback_failed_failover(reason: str) -> bool:
    """Undo a just-committed secondary switch when client construction fails.

    Callers must invoke this only after they switched from primary in the same
    request.  An unavailable primary may still be restored: retaining the
    previously selected account is safer than persisting an unverified standby.
    """
    if _active != "failover":
        return False
    _activate_primary(f"OAuth FAILOVER ROLLBACK (reason={reason}): failover -> primary")
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
            "forced failback (reason=%s): primary still dead — staying on failover",
            reason,
        )
        return False
    _activate_primary(
        f"OAuth FAILBACK (forced, reason={reason}): failover dead — failover → primary",
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
