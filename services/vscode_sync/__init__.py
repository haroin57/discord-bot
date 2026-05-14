"""VSCode Claude Code → Discord thread sync.

Tails ~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl files written by the
Claude Code VSCode extension and posts a filtered stream of events to a
configured Discord thread via the bot's own send() — no webhook needed.
"""

from services.vscode_sync.daemon import is_running, run_daemon, stop_daemon

__all__ = ["is_running", "run_daemon", "stop_daemon"]
