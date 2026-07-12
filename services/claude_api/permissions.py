"""Rule-based permissions — free-code (Claude Code) permissions.ts の判定部移植。

ルール書式（本家 settings.json 互換）:
    "Bash"                  → ツール全体
    "Bash(npm publish:*)"   → content プレフィクスマッチ（末尾 :* ）
    "Bash(ls)"              → content 完全一致
    "Read(/etc/*)"          → glob（fnmatch）
評価順（本家 checkRuleBasedPermissions:1071- の簡約。ask UI は Discord
リアクションで実装するため ask_bridge 経由のハンドラに委譲する）:
    deny(全体→content) → ask(全体→content) → allow(全体→content) → 既定
既定は admin=allow（従来挙動）。restricted はこの層に入る前に既存の
hard deny list（restricted.py）で落ちる。

ルールファイル: <BOT_DIR>/permissions.json {"deny": [], "ask": [], "allow": []}
mtime キャッシュ付きホットリロード（bot 再起動不要で編集が効く）。
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

_BOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RULES_PATH = os.environ.get(
    "PERMISSION_RULES_PATH", os.path.join(_BOT_DIR, "permissions.json"),
)

_RULE_RE = re.compile(r"^([A-Za-z0-9_\-]+(?:__[A-Za-z0-9_\-]+)*)(?:\((.*)\))?$")

_cache: dict = {"mtime": None, "rules": {"deny": [], "ask": [], "allow": []}}


def _parse_rule(raw: str) -> tuple[str, str | None] | None:
    m = _RULE_RE.match(raw.strip())
    if not m:
        log.warning("permissions: unparsable rule %r (ignored)", raw)
        return None
    return m.group(1), m.group(2)


def load_rules() -> dict:
    try:
        mtime = os.path.getmtime(RULES_PATH)
    except OSError:
        return {"deny": [], "ask": [], "allow": []}
    if _cache["mtime"] == mtime:
        return _cache["rules"]
    try:
        raw = json.load(open(RULES_PATH, encoding="utf-8"))
        rules = {
            k: [r for r in (_parse_rule(x) for x in raw.get(k, [])) if r]
            for k in ("deny", "ask", "allow")
        }
        # 元文字列も理由表示用に保持
        rules["_raw"] = {k: list(raw.get(k, [])) for k in ("deny", "ask", "allow")}
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        log.warning("permissions.json load failed (%s); fail-open to no rules", exc)
        return {"deny": [], "ask": [], "allow": []}
    _cache["mtime"] = mtime
    _cache["rules"] = rules
    return rules


def observable_input(tool_name: str, tool_input: dict) -> str:
    """ルール照合と ask 表示に使う「観測可能な入力」（本家 observable input 相当）。"""
    if not isinstance(tool_input, dict):
        return str(tool_input)
    bare = tool_name.split("__", 2)[-1] if "__" in tool_name else tool_name
    if bare == "Bash":
        return str(tool_input.get("command") or "")
    if bare in ("Read", "Write", "Edit", "NotebookEdit"):
        return str(tool_input.get("file_path") or "")
    if bare in ("Glob", "Grep"):
        return str(tool_input.get("pattern") or "")
    if bare == "Skill":
        return str(tool_input.get("skill") or "")
    if bare in ("Agent", "Task"):
        return str(tool_input.get("subagent_type") or "")
    try:
        return json.dumps(tool_input, ensure_ascii=False)[:400]
    except (TypeError, ValueError):
        return str(tool_input)[:400]


def _segments(tool_name: str, content: str) -> list[str]:
    """Bash は複合コマンド（&& ; |）の各セグメントでも照合する。
    本家はサブコマンド解析まで持つ（bashPermissions.ts）— これはその簡約で、
    「cd x && rm -rf y」がプレフィクス照合をすり抜けるのを防ぐ。"""
    bare = tool_name.split("__", 2)[-1] if "__" in tool_name else tool_name
    if bare != "Bash":
        return [content]
    segs = [content]
    for part in re.split(r"&&|\|\||;|\|", content):
        part = part.strip()
        if part and part != content:
            segs.append(part)
    return segs


def _content_matches(rule_content: str, tool_name: str, content: str) -> bool:
    for seg in _segments(tool_name, content):
        if rule_content.endswith(":*"):
            if seg.startswith(rule_content[:-2].rstrip()):
                return True
        elif "*" in rule_content or "?" in rule_content:
            if fnmatch.fnmatch(seg, rule_content):
                return True
        elif seg == rule_content:
            return True
    return False


def _tool_matches(rule_tool: str, tool_name: str) -> bool:
    bare = tool_name.split("__", 2)[-1] if "__" in tool_name else tool_name
    return rule_tool in (tool_name, bare)


def evaluate(tool_name: str, tool_input: dict) -> tuple[str | None, str]:
    """(decision, matched_rule_str)。decision は deny/ask/allow/None。"""
    rules = load_rules()
    content = observable_input(tool_name, tool_input)
    for kind in ("deny", "ask", "allow"):
        for i, (rtool, rcontent) in enumerate(rules.get(kind, [])):
            if not _tool_matches(rtool, tool_name):
                continue
            if rcontent is None or _content_matches(rcontent, tool_name, content):
                raw_list = rules.get("_raw", {}).get(kind, [])
                raw = raw_list[i] if i < len(raw_list) else f"{rtool}({rcontent})"
                return kind, raw
    return None, ""


AskHandler = Callable[[str, str, str], Awaitable[bool]]


def wrap_permission_gate(base_gate, *, restricted: bool):
    """既存の同期 gate をルール評価＋ask 対応の async gate に包む。

    tool_loop 側は awaitable を await する（inspect.isawaitable 対応済み）。
    ask ハンドラは ask_bridge の contextvar から取得 — bot.py が Discord
    リアクション承認ハンドラをリクエスト毎に設定する。ハンドラ不在で ask
    ルールに踏んだ場合は fail-closed（拒否）。
    """

    async def gate(tool_name: str, tool_input: dict) -> tuple[bool, str]:
        ok, reason = base_gate(tool_name, tool_input)
        if not ok:
            return ok, reason
        if restricted:
            return ok, reason  # ルール層は admin のみ（restricted は既存 deny 一式）
        decision, rule = evaluate(tool_name, tool_input)
        if decision == "deny":
            return False, f"denied by permission rule: {rule}"
        if decision == "ask":
            from services.claude_api.ask_bridge import get_ask_handler

            handler = get_ask_handler()
            if handler is None:
                return False, (
                    f"approval required by rule {rule} but no approver is "
                    "attached to this session (ask → deny)"
                )
            detail = observable_input(tool_name, tool_input)
            try:
                approved = await handler(tool_name, detail, rule)
            except Exception as exc:  # noqa: BLE001 — ask 失敗は拒否側に倒す
                log.warning("permission ask failed: %s", exc)
                return False, f"ask flow failed ({exc}); denied"
            if approved:
                return True, ""
            return False, f"denied by user (rule: {rule})"
        return True, ""

    return gate
