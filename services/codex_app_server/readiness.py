"""Local credential validity plus observed Codex authentication failures."""
import json
import os
from pathlib import Path
import stat

_auth_failed = False
_failed_fingerprint = None


def _auth_path() -> Path:
    return Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'auth.json'


def _fingerprint():
    try:
        info = _auth_path().lstat()
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
    except OSError:
        return None


def record_auth_result(ok: bool) -> None:
    global _auth_failed, _failed_fingerprint
    _auth_failed = not ok
    _failed_fingerprint = None if ok else _fingerprint()


def check() -> dict:
    path = _auth_path()
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            return {'ok': False, 'reason': 'codex_auth_store_not_private'}
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            return {'ok': False, 'reason': 'codex_chatgpt_credentials_missing'}
        tokens = payload.get('tokens') or {}
        if (not isinstance(tokens, dict) or payload.get('auth_mode') != 'chatgpt'
                or not tokens.get('access_token') or not tokens.get('refresh_token')):
            return {'ok': False, 'reason': 'codex_chatgpt_credentials_missing'}
        if _auth_failed and _failed_fingerprint == _fingerprint():
            return {'ok': False, 'reason': 'codex_auth_failure_observed'}
        return {'ok': True, 'auth_mode': 'chatgpt', 'check': 'private_credentials_and_observed_auth_errors',
                'remote_validation': False}
    except (OSError, ValueError, TypeError):
        return {'ok': False, 'reason': 'codex_auth_store_unreadable'}


def claude_required() -> bool:
    return not (os.environ.get('DEFAULT_LLM_PROVIDER', '').lower() == 'codex'
                and os.environ.get('BOT_AUX_LLM_PROVIDER', '').lower() == 'codex'
                and os.environ.get('CODEX_RESTRICTED_ENABLED', '').lower() in {'1', 'true'})
