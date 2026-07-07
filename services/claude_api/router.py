"""Tier router — リクエストを chat / deliberate に振り分ける（熟考 P2）。

deliberate tier では streaming を止めて planner → executor → (MAGI-lite) →
verifier → revise のパイプラインが走る。判定は bot.py（admin 確定後）で行い、
`call_with_tools(tier=...)` で伝搬する。restricted には適用しない
（entry.py 側でも tier=None 強制の二重ゲート）。

ユーザー決定（2026-07-02）: **積極自動** — 閾値低め（依頼型キーワード単発で
deliberate）。streaming 消失が増えるトレードオフは許容。明示ネガマーカー
（「さくっと」等）は即 chat。全決定を log に残して閾値チューニングの材料にする。

v1 はヒューリスティック純関数。`DELIB_ROUTER_MODE="llm"` は将来の Haiku 分類の
差し替え口（decide_tier の interface は変えない）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from config import (
    DELIB_ROUTER_MIN_CHARS,
    DELIB_ROUTER_SCORE_THRESHOLD,
)

log = logging.getLogger(__name__)

TIER_CHAT = "chat"
TIER_DELIBERATE = "deliberate"


@dataclass(frozen=True)
class RouteDecision:
    tier: str
    score: float
    reason: str
    judgment: bool = False  # 判断・評価系の問い（P5 MAGI-lite のトリガー）


# 明示マーカー（最優先・即決）
_FORCE_DELIB = ("じっくり", "熟考", "徹底的", "網羅的", "深掘り", "ちゃんと調べ")
_FORCE_CHAT = ("さくっと", "ざっくりでいい", "一言で", "手短に", "簡潔に一言", "パッと")

# 依頼型キーワード（+2）: 調査・分析・制作系の動詞/名詞
_TASK_KEYWORDS = (
    "調査", "調べて", "分析", "比較", "検討", "検証", "評価して", "レビュー",
    "設計", "計画", "戦略", "まとめて", "レポート", "実装して", "作って",
    "リファクタ", "デバッグ", "監査", "精査",
)

# 判断系（P5）: 賛否・二択・妥当性の問い
_JUDGMENT_RE = re.compile(
    r"(どっちが|どちらが|べきか|べき[？?]|賛否|是非|妥当|正しい[？?]|"
    r"良いと思う|どう思う|選ぶなら|おすすめはどれ|優先すべき)"
)


def route_request(
    raw_user_text: str, *, has_attachments: bool = False,
    auto_enabled: bool = True,
) -> RouteDecision:
    """ヒューリスティック tier 判定（純関数・同期）。

    ``auto_enabled=False``（既定運用、2026-07-08 ユーザー決定）ではスコア
    ベースの自動昇格を行わない — deliberate に入るのは明示マーカー
    （「熟考」「じっくり」等）か /tier コマンドの強制指定（呼び出し側で
    処理）だけ。judgment フラグは tier と独立に返す（MAGI-lite の材料）。
    """
    text = (raw_user_text or "").strip()
    lower = text.lower()
    judgment = bool(_JUDGMENT_RE.search(text))
    reasons: list[str] = []

    for m in _FORCE_CHAT:
        if m in text:
            return RouteDecision(TIER_CHAT, -99.0, f"force_chat:{m}", judgment)
    for m in _FORCE_DELIB:
        if m in text:
            return RouteDecision(TIER_DELIBERATE, 99.0, f"force_delib:{m}", judgment)

    if not auto_enabled:
        return RouteDecision(TIER_CHAT, 0.0, "auto_routing_disabled", judgment)

    score = 0.0
    hits = [kw for kw in _TASK_KEYWORDS if kw in lower]
    if hits:
        score += 2.0
        reasons.append(f"task_kw:{hits[:3]}")
    if len(text) >= 300:
        score += 2.0
        reasons.append("len>=300")
    elif len(text) >= DELIB_ROUTER_MIN_CHARS:
        score += 1.0
        reasons.append(f"len>={DELIB_ROUTER_MIN_CHARS}")
    n_questions = text.count("？") + text.count("?")
    bullet_lines = sum(
        1 for ln in text.splitlines() if ln.lstrip().startswith(("-", "・", "*"))
    )
    if n_questions >= 2 or bullet_lines >= 2:
        score += 1.0
        reasons.append("multi_ask")
    if has_attachments:
        score += 1.0
        reasons.append("attachments")
    if judgment:
        score += 1.0
        reasons.append("judgment")
    if len(text) < 20 and not hits:
        return RouteDecision(TIER_CHAT, score, "short_no_kw", judgment)

    tier = TIER_DELIBERATE if score >= DELIB_ROUTER_SCORE_THRESHOLD else TIER_CHAT
    return RouteDecision(tier, score, ",".join(reasons) or "no_signal", judgment)


async def decide_tier(
    raw_user_text: str, *, has_attachments: bool = False,
    auto_enabled: bool = True,
) -> RouteDecision:
    """判定の呼び出し口（最初から async — 将来の LLM 分類差し替えのため）。"""
    decision = route_request(
        raw_user_text, has_attachments=has_attachments,
        auto_enabled=auto_enabled,
    )
    log.info(
        "delib_router tier=%s score=%.1f judgment=%s reason=%s auto=%s",
        decision.tier, decision.score, decision.judgment, decision.reason,
        auto_enabled,
    )
    return decision
