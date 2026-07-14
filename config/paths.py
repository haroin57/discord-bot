"""Filesystem layout — every path the bot reads or writes (P5-c).

First domain split out of the config god-module. Import via the package
(`from config import STATE_DIR`) — the package __init__ re-exports all of
these, so call sites are unchanged.
"""

import os
import tempfile

from dotenv import load_dotenv

load_dotenv()  # idempotent; guards direct `import config.paths` in scripts

# Repo root is one level above the config/ package.
BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("BOT_STATE_DIR", os.path.join(BOT_DIR, ".state")),
))
HISTORY_DIR = os.path.join(STATE_DIR, "history")
SESSIONS_PATH = os.path.join(STATE_DIR, "sessions.json")
# Single source of truth for the session freshness window (機能2, 2026-06-17).
# Previously split: claude_sdk.py read SESSION_TTL_S from env defaulting to 6h
# (CLI legacy), while claude_api/session_store.py hard-coded 30m — so an env
# override moved one but not the other. Both now import this. 60m (raised from
# 30m on 2026-06-22, haroin) so a short break — lunch, a meeting — doesn't cold-
# reset the session and re-fetch Mem0. This is the resume / Mem0-refetch-skip
# window only; it does NOT change the replayed-tail size or per-turn token cost.
SESSION_TTL_S = int(os.environ.get("SESSION_TTL_S", str(60 * 60)))
# Where runtime_state.py persists in-memory state across restarts (機能2).
RUNTIME_STATE_PATH = os.path.join(STATE_DIR, "runtime_state.json")
# invest_kb (Phase A, 2026-06-17): LanceDB side-store for neutral investment
# knowledge (EDINET narrative / CFA RPC markdown / hayatoito philosophy etc).
# Separate from Mem0 because Mem0's 120-char fact compression discards
# long-form documentary text.
INVEST_KB_PATH = os.path.join(STATE_DIR, "invest_kb")
SETTINGS_PATH = os.path.join(BOT_DIR, ".claude", "settings.json")
CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
ATTACHMENT_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get(
        "BOT_ATTACHMENT_DIR",
        os.path.join(tempfile.gettempdir(), "discord_bot_attachments"),
    ),
))


def _ensure_mode_700(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


_ensure_mode_700(STATE_DIR)
_ensure_mode_700(HISTORY_DIR)
_ensure_mode_700(ATTACHMENT_DIR)
