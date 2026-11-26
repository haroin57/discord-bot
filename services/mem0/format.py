"""Format retrieved Mem0 memories as a prompt-injection block."""

from datetime import datetime, timezone

from services.sentinel import apply_sentinel_filters


def _scrub_fact_text(text: str) -> str:
    """Drop markers that could close a data fence or impersonate trust headers.

    Second-order injection defense: a fact (or a history-search line, or its
    speaker label) echoed back into the prompt must not be able to close the
    ``<retrieved_memories>`` / ``<history_search>`` fence or forge a trusted
    scaffold marker (``<trusted_user_input>`` / ``[現在の発言者 …]`` /
    ``<sleep_enforcement …>`` …).

    Delegates to the single-source ``apply_sentinel_filters`` in
    ``services.sentinel`` so this scrub stays exactly SENTINEL_RE-equivalent
    (F-w1-01 / F-w1-07) and gains NFKC + zero-width normalization (F-w1-03) —
    no separate, drifting blocklist to keep in sync. Also redacts power-grab
    phrases via POWER_PHRASE_RE (M15), both folded into that helper.
    """
    if not text:
        return text
    return apply_sentinel_filters(text)


_TIER_LABELS = {0: "Live", 1: "Hot", 2: "Warm", 3: "Cold"}

# Retrieval v1 associates (``_assoc=True``): extra facts linked to the direct
# hits by shared tags / graph entities rather than matched by the query.
ASSOC_PREFIX = "関連: "


def _format_epoch(ts) -> str | None:
    """unix 秒を YYYY-MM-DD に。壊れた値は None（メタ無し扱い）。"""
    try:
        ts = int(ts)
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _fact_meta_suffix(m: dict) -> str:
    """事実1行に付ける [imp=N Tier] [YYYY-MM-DD] サフィックス。

    importance / tier / 日付をモデルに見せることで、重要度5のコア事実と
    重要度2の断片、新しい状態と古い状態をモデルが区別できるようにする
    （従来は全メタデータを捨てて全事実が等価に見えていた）。
    メタが無い古いレコードは空文字を返し、従来表示のままになる。
    """
    md = m.get("metadata") or {}
    if not isinstance(md, dict):
        return ""
    bits: list[str] = []
    try:
        imp = int(md.get("importance"))
    except (TypeError, ValueError):
        imp = None
    tier_label = _TIER_LABELS.get(md.get("tier"))
    inner = " ".join(
        s for s in (f"imp={imp}" if imp is not None else "", tier_label or "")
        if s
    )
    if inner:
        bits.append(f"[{inner}]")
    date_str = _format_epoch(
        md.get("last_reinforced_at") or md.get("created_at_unix")
        or md.get("timestamp")
    )
    if date_str:
        bits.append(f"[{date_str}]")
    # Phase R (Mem0 v5 T2): 保存時に構造化した condition/scope を注入時に
    # 落とさない（flattening 防止）。「夜は紅茶」が条件を失って「紅茶が好き」
    # として無条件注入されるのを防ぐ。metadata が無ければ何も足さない。
    cond = md.get("condition")
    if isinstance(cond, str) and cond.strip():
        bits.append(f"[条件: {_scrub_fact_text(cond.strip()[:48])}]")
    scope = md.get("scope")
    if isinstance(scope, str):
        if scope.startswith("conversation"):
            bits.append("[この会話限定]")
        elif scope == "meta":
            bits.append("[メタ情報]")
    return (" " + " ".join(bits)) if bits else ""


def format_memories_for_prompt(memories: list[dict]) -> str:
    """Format retrieved memories as a prompt-injection block.

    Phase ⑦: a ``_fok_no_memory`` sentinel (see
    ``search.assess_recall_confidence`` / ``MEM0_FOK_ENABLED``) renders as an
    explicit "nothing relevant found" line rather than being silently
    dropped — a calibrated null result reads very differently to the model
    than the fact block simply not being there, and the latter invites
    confabulation ("memory wasn't consulted" vs. "memory was consulted and
    came up empty"). Core memories (unconditional, query-independent) still
    render normally alongside it when present.
    """
    if not memories:
        return ""

    fok_entry = next((m for m in memories if m.get("_fok_no_memory")), None)
    real = [m for m in memories if not m.get("_fok_no_memory")]
    if not real and not fok_entry:
        return ""

    lines = [
        "[Mem0 — この人についての長期記憶]",
        "(凡例: imp=重要度1-5 / Live>Hot>Warm>Cold=鮮度tier / [日付]=最終確認日 / "
        "[条件: ...]=その条件下でのみ成り立つ事実)",
    ]
    # core memory（常時注入枠）を先頭に固定し、クエリ依存の recall と区別する。
    # v1 associates（_assoc）は直接ヒットの後ろに「関連: 」付きで並べる。
    ordered = sorted(
        real, key=lambda m: 0 if m.get("_core") else (2 if m.get("_assoc") else 1),
    )
    for m in ordered:
        text = m.get("memory", m.get("text", ""))
        if text:
            prefix = ASSOC_PREFIX if m.get("_assoc") else ""
            lines.append(f"- {prefix}{_scrub_fact_text(text)}{_fact_meta_suffix(m)}")
    if fok_entry:
        lines.append(
            "(このクエリに一致する具体的な記憶は見つからなかった。"
            "推測で補完しないこと)"
        )
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
        # Scrub the speaker label too: `author` is a user-settable Discord
        # display name (bot_pipeline/record.py stores it verbatim), so an
        # attacker who renames themselves to a forged trust marker / speaker
        # spoof must not reach the admin prompt through this `(who)` slot
        # (F-w1-08).
        lines.append(f"- ({_scrub_fact_text(who)}) {_scrub_fact_text(text)}")
    if len(lines) == 1:
        return ""
    lines.append("[End of history]")
    return "\n".join(lines)


def strip_json_fence(text: str) -> str:
    """Remove a Markdown ```json fence around an LLM JSON reply, if present.

    LLMs asked for raw JSON wrap it in a fence often enough that nine call
    sites across extract/search/dedup/conflict had hand-rolled this exact
    dance (P5-b unification).
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    return text
