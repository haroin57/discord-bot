"""Per-speaker MCP authorization and secret-free Codex command-line setup."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys


def enabled() -> bool:
    return os.environ.get('CODEX_MCP_PARITY_ENABLED', '').lower() in {'true', '1'}


def server_allowed(name: str, user_id: int | None) -> bool:
    from config import ADMIN_USER_IDS, NATURE_REMO_ALLOWED_USER_IDS, KANAE_ALARM_ALLOWED_USER_IDS, AGENTIC_INBOX_ALLOWED_USER_IDS
    if user_id not in ADMIN_USER_IDS:
        return False
    gates = {'nature-remo': NATURE_REMO_ALLOWED_USER_IDS,
             'kanae-alarm': KANAE_ALARM_ALLOWED_USER_IDS,
             'agentic-inbox': AGENTIC_INBOX_ALLOWED_USER_IDS}
    return name not in gates or user_id in gates[name]


def authorized_configs(user_id: int | None) -> dict:
    from config import MCP_SERVERS
    from services.mcp_control import filter_enabled_mcp_configs
    return filter_enabled_mcp_configs({name: spec for name, spec in MCP_SERVERS.items() if server_allowed(name, user_id)})


def bridge_args(user_id: int | None) -> list[str]:
    from config import BOT_DIR, STATE_DIR
    from utils.atomic_write import atomic_write_json
    configs = authorized_configs(user_id)
    if not configs:
        return []
    path = Path(STATE_DIR) / 'codex_mcp' / str(int(user_id)) / 'config.json'
    payload = {'version': 1, 'user_id': user_id, 'servers': configs,
               'state_dir': str(STATE_DIR), 'workdir': os.environ.get('BOT_CWD', str(STATE_DIR))}
    # Only this mode-0600 file contains per-server credentials; not argv or
    # the Codex child environment. The child gets a narrowly scoped MCP port.
    try:
        same = path.is_file() and not path.is_symlink() and json.loads(path.read_text()) == payload
        same = same and path.stat().st_mode & 0o077 == 0
    except (OSError, ValueError):
        same = False
    if not same:
        atomic_write_json(path, payload)
    return ['-c', 'mcp_servers.discord-tools.enabled=true',
            '-c', f'mcp_servers.discord-tools.command={json.dumps(sys.executable)}',
            '-c', 'mcp_servers.discord-tools.args=' + json.dumps([str(Path(BOT_DIR) / 'mcp_codex_bridge.py'), str(path)]),
            '-c', 'mcp_servers.discord-tools.startup_timeout_sec=30',
            '-c', 'mcp_servers.discord-tools.tool_timeout_sec=180']
