"""Format retrieved Mem0 memories as a prompt-injection block."""

import re

from services.sentinel import POWER_PHRASE_RE

# Markers an attacker could have stored in a fact (via the ingestion path)
# that would close the <retrieved_memories> fence or impersonate any trusted
# prompt-scaffold block when echoed back into the prompt. Strip them
# defensively at format time as second-order injection defense.
#
# This pattern set MIRRORS `_SENTINEL_RE` in bot.py / services/prompt_builder.py
# so a fact echoed via format_memories_for_prompt cannot inject a scaffold
# marker that the outer sanitize layer used to catch. The outer
# `_sanitize_untrusted_text(mem0_block)` call was removed for hot-path
# performance, so this blocklist is now the single source of scrubbing.
_FACT_TEXT_BLOCKLIST = re.compile(
    r"\[(?:Message from|End of|Mem0|Attached images|Current message|Thread:|"
    r"Channel recent|Referenced message|Replied-to message|End of thread|"
    r"End of channel|End of Mem0|History|End of history|Trusted context|"
    r"untrusted_user_input)"
    r"[^\]\n]*\]"
    r"|</?retrieved_memories[^>]*>"
    r"|</?history_search[^>]*>"
    r"|</?untrusted_user_input[^>]*>",
    re.IGNORECASE,
)


def _scrub_fact_text(text: str) -> str:
    """Drop markers that could close the memories fence or impersonate trust headers.

    Also applies POWER_PHRASE_RE to redact power-grab phrases (M15).
    """
    if not text:
        return text
    text = _FACT_TEXT_BLOCKLIST.sub("[redacted-marker]", text)
    text = POWER_PHRASE_RE.sub("[redacted]", text)
    return text


def format_memories_for_prompt(memories: list[dict]) -> str:
    """Format retrieved memories as a prompt-injection block."""
    if not memories:
        return ""

    lines = ["[Mem0 — この人についての長期記憶]"]
    for m in memories:
        text = m.get("memory", m.get("text", ""))
        if text:
            lines.append(f"- {_scrub_fact_text(text)}")
    lines.append("[End of Mem0 memories]")
    return "\n".join(lines)


def format_history_for_prompt(results: list[dict]) -> str:
    """Format BM25 history-search hits as a prompt-injection block.

    Mirrors format_memories_for_prompt: same defensive _scrub_fact_text on the
    verbatim line so a stored message can't close the <history_search> fence.
    Each hit is labelled with its role (発言者: user=この人 / assistant=私).
    """
    if not results:
        return ""

    lines = ["[History — このチャンネルの過去ログでキーワード一致した行]"]
    for r in results:
        text = r.get("memory") or r.get("text") or ""
        if not text:
            continue
        role = r.get("role", "")
        author = (r.get("author") or "").strip()
        if role == "user":
            # history_search はチャンネル過去ログから keyword 一致で行を拾う。
            # 複数の admin が同居するチャンネルでは、現発言者とは別人の過去発言も
            # 混ざる。"この人" は Mem0 ブロック ([Mem0 — この人についての…]) と同語で
            # 現発言者の情報だと誤読させるため避ける。author が分かればそれを使い、
            # 不明なら話者非断定の中立ラベルにフォールバックする (author 列は
            # JSONL author 保存の修正後に実名化される)。
            who = author or "過去の発言者"
        elif role == "assistant":
            who = "私"
        elif role == "channel":
            # Non-mention channel message — the speaker can be anyone, so show
            # the author display name when known.
            who = author or "誰か"
        else:
            who = author or role
        lines.append(f"- ({who}) {_scrub_fact_text(text)}")
    if len(lines) == 1:
        return ""
    lines.append("[End of history]")
    return "\n".join(lines)
