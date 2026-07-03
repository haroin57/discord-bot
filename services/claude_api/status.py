"""Status callback helpers — safe invocation + tool-input hint extraction.

Ported from services/claude_sdk.py:308-354.
"""

import logging

log = logging.getLogger(__name__)


async def _safe_status(cb, msg: str) -> None:
    """Invoke status_callback without letting exceptions propagate."""
    if cb is None:
        return
    try:
        await cb(msg)
    except Exception as e:
        log.debug("status_callback failed (non-fatal): %s", e)


def _tool_input_hint(name: str, inp: dict | None) -> str:
    """Return a short human-readable hint for the Discord status message."""
    if not inp:
        return ""
    try:
        n = name.split("__")[-1] if "__" in name else name  # strip mcp prefix
        if n in ("Bash", "BashOutput"):
            return inp.get("command", "")
        if n in ("Read",):
            return inp.get("file_path", "")
        if n in ("Edit", "Write", "MultiEdit"):
            return inp.get("file_path", "")
        if n in ("Glob",):
            return inp.get("pattern", "")
        if n in ("Grep",):
            return inp.get("pattern", "")
        if n in ("WebFetch", "navigate"):
            return inp.get("url", "")
        if n in ("WebSearch", "brave_web_search", "brave_local_search"):
            return inp.get("query", "")
        if n in ("search_x",):
            return inp.get("query", "")
        if n in ("Agent",):
            return inp.get("description", "")
        if n in ("Scratchpad",):
            return inp.get("section", "")
        if n in ("TodoWrite",):
            todos = inp.get("todos") or []
            if not isinstance(todos, list):
                return ""
            in_prog = next(
                (
                    t.get("activeForm") or t.get("content")
                    for t in todos
                    if isinstance(t, dict) and t.get("status") == "in_progress"
                ),
                None,
            )
            if in_prog:
                return str(in_prog)
            return f"{len(todos)} todos"
        # Generic: show first string value
        for v in inp.values():
            if isinstance(v, str) and v:
                return v
        return ""
    except Exception:
        return ""


_TOOL_EMOJI = {
    "Bash": "💻", "BashOutput": "💻",
    "Read": "📖", "Write": "✏️", "Edit": "✏️", "MultiEdit": "✏️",
    "Glob": "🔎", "Grep": "🔎",
    "WebFetch": "🌐", "navigate": "🌐",
    "WebSearch": "🔍", "brave_web_search": "🔍", "brave_local_search": "🔍",
    "brave_news_search": "📰", "tavily_search": "🔍", "tavily_extract": "🌐",
    "search_x": "🐦", "get_tweet": "🐦", "get_x_trends": "📈",
    "TodoWrite": "🗒️", "Agent": "🤖", "Scratchpad": "💭",
}


def tool_oneline(name: str, inp: dict | None) -> str:
    """1行のツール呼び出し要約（並列ツール notice / Deep Research view 共用）。

    emoji + 短いツール名 + 簡潔な inline-code ヒント（改行・バッククォートを無害化し
    キャップ）。複数行で積み重ねて表示する箇所で各行が1行に収まることを保証する。
    """
    short = name.split("__")[-1] if "__" in name else name
    line = f"{_TOOL_EMOJI.get(short, '🔧')} {short}"
    hint = _tool_input_hint(name, inp)
    if hint:
        hint = hint.replace("`", "'").replace("\n", " ").strip()
        if len(hint) > 120:
            hint = hint[:120] + "…"
        if hint:
            line += f" `{hint}`"
    return line
