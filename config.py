"""Configuration and constants — matches free-code constants/ pattern."""

import os
import tempfile

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
COOLDOWN_SECONDS = int(os.environ.get("BOT_COOLDOWN", "5"))
BOT_KEYWORDS = [s.strip() for s in os.environ.get("BOT_KEYWORDS", "かなえ").split(",") if s.strip()]

ADMIN_USER_IDS: set[int] = set()
_raw_ids = os.environ.get("ADMIN_USER_IDS", "")
if _raw_ids:
    ADMIN_USER_IDS = {int(uid.strip()) for uid in _raw_ids.split(",") if uid.strip().isdigit()}

# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------
CWD = os.environ.get("BOT_CWD", "/home/ubuntu")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "600"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "16384"))

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BOT_DIR, ".state")
HISTORY_DIR = os.path.join(STATE_DIR, "history")
SESSIONS_PATH = os.path.join(STATE_DIR, "sessions.json")
SETTINGS_PATH = os.path.join(BOT_DIR, ".claude", "settings.json")
CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
ATTACHMENT_DIR = os.path.join(tempfile.gettempdir(), "discord_bot_attachments")

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(ATTACHMENT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Limits (free-code: constants/limits)
# ---------------------------------------------------------------------------
MAX_SESSIONS = 100
MAX_HISTORY = 50
MAX_MESSAGES_PER_CHANNEL = 20
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_RESOLVED_LINKS = 5
MAX_RESPONSE_IMAGES = 10
MAX_COOLDOWN_ENTRIES = 1000
THREAD_HISTORY_LIMIT = int(os.environ.get("THREAD_HISTORY_LIMIT", "50"))

# Stream quality (free-code: queryModelWithStreaming)
STREAM_STALL_THRESHOLD_S = 30
STREAM_IDLE_TIMEOUT_S = 90

# OAuth (free-code: checkAndRefreshOAuthTokenIfNeeded)
TOKEN_REFRESH_BUFFER_MS = 5 * 60 * 1000

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
ALLOWED_IMAGE_DIRS = ("/tmp", "/home/ubuntu")

# Tool-use trigger keywords (matched against user's raw message only)
TOOL_KEYWORDS = [
    "ファイル", "実行して", "コマンド", "インストール", "作成して", "編集して", "削除して",
    "書いて", "書き換えて", "修正して", "デプロイ", "ビルドして",
    "mermaid", "Mermaid", "画像生成", "スクリーンショット",
    "git ", "npm ", "pip ", "sudo ", "apt ", "docker ",
    "再起動して", "systemctl", "systemd",
    "調べて", "検索して", "ググって", "ブラウズ",
]
import re as _re
TOOL_KEYWORDS_RE = _re.compile("|".join(_re.escape(kw) for kw in TOOL_KEYWORDS))


def needs_tools(text: str) -> bool:
    """Check if text contains tool-use trigger keywords."""
    return bool(TOOL_KEYWORDS_RE.search(text))

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
def load_system_prompt() -> str:
    claude_md = os.path.join(BOT_DIR, "CLAUDE.md")
    if os.path.isfile(claude_md):
        with open(claude_md, encoding="utf-8") as f:
            return f.read()
    return (
        "You are a helpful assistant running as a Discord bot. "
        "Answer concisely. You have full access to the local machine's filesystem and shell. "
        "When asked to perform tasks, do them directly."
    )


SYSTEM_PROMPT = os.environ.get("BOT_SYSTEM_PROMPT") or load_system_prompt()
BOT_ROLE_IDS: set[int] = set()  # populated at runtime by bot.py on_ready

# ---------------------------------------------------------------------------
# MCP Servers (API key不要のもの)
# ---------------------------------------------------------------------------
MCP_SERVERS: dict = {
    "context7": {
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
    },
    "playwright": {
        "command": "npx",
        "args": ["@playwright/mcp@latest"],
    },
    "x-scraper": {
        "command": "npx",
        "args": ["-y", "@enescinar/twitter-mcp"],
    },
}

# API key付きMCPは .env にキーがある場合のみ追加
_brave_key = os.environ.get("BRAVE_API_KEY")
if _brave_key:
    MCP_SERVERS["brave-search"] = {
        "command": "npx",
        "args": ["-y", "@anthropic-ai/mcp-server-brave-search"],
        "env": {"BRAVE_API_KEY": _brave_key},
    }

_tavily_key = os.environ.get("TAVILY_API_KEY")
if _tavily_key:
    MCP_SERVERS["tavily"] = {
        "command": "npx",
        "args": ["-y", "tavily-mcp"],
        "env": {"TAVILY_API_KEY": _tavily_key},
    }

_github_token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
if _github_token:
    MCP_SERVERS["github"] = {
        "type": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"Authorization": f"Bearer {_github_token}"},
    }
