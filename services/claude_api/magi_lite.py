"""MAGI-lite — 判断系の問いへの3レンズ独立見解 + 合意度キャリブレーション（熟考 P5）。

deep_research の MAGI（分解→検証検索ワーカー→lead の入れ子）から lead 相当だけを
抜いた軽量版。executor が集めた evidence digest を共通入力に、科学/直観/懐疑の
3レンズが独立に見解を出し、integrator-lite が匿名化・シャッフルされた3見解から
統合ガイダンスと **consensus score（合意度 0-1）** を返す。

ensemble の真価は多数決ではなく不一致の計測にある: consensus が
DELIB_MAGI_CONSENSUS_MIN を下回るとき、revise prompt は「見解が割れている論点の
明示・断定回避・条件分岐提示」を必須化する（過信した断定を構造的に潰す）。

失敗は degraded=True（呼び出し側はスキップ）。レンズ prompt は
deep_research/prompts.py の LENS_SPEC / build_lens_system を流用、匿名化は
magi.anonymize を流用する。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from config import (
    DELIB_MAGI_CONSENSUS_MIN,
    DELIB_MAGI_MODEL,
    DELIB_MAGI_TIMEOUT_S,
)

log = logging.getLogger(__name__)


@dataclass
class MagiLiteResult:
    consensus: float
    divergences: list[str] = field(default_factory=list)
    guidance: str = ""
    lens_bodies: dict[str, str] = field(default_factory=dict)
    degraded: bool = False


_INTEGRATOR_TASK = (
    "You integrate three anonymized independent analyses. Raw JSON only."
)

_INTEGRATOR_PROMPT = """3つの独立した分析視点（匿名化・順不同）が同じ問いに答えました。以下を読み、生 JSON のみで出力してください。

# 問い
{question}

# 3視点の出力
{anonymized}

出力:
{{"consensus": 0.0-1.0 の合意度（結論・重要な根拠がどの程度一致しているか）,
  "divergences": ["視点間で割れている論点（無ければ空配列）"],
  "guidance": "最終回答を書く者への統合ガイダンス（3視点の一致点・相違点・見落としを2-6文で）"}}"""


async def _run_one_lens(client, lens_key: str, question: str,
                        digest: str, model: str) -> tuple[str, str | None]:
    from services.deep_research.prompts import build_lens_system
    from services.mem0.cli_compat import cli_messages_create, extract_text

    briefing = (
        f"# 中心的な問い\n{question[:3000]}\n\n"
        f"# 証拠ベース（実行済み tool の台帳）\n{digest[:8000]}\n\n"
        "あなたのレンズから独立に回答してください。他レンズとの調整は不要。"
    )
    try:
        resp = await cli_messages_create(
            client, model=model, max_tokens=2500,
            user_message=briefing,
            task_prompt=build_lens_system(lens_key)[:8000],
        )
        body = extract_text(resp).strip()
        return lens_key, (body or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("magi_lite lens %s failed: %s", lens_key, exc)
        return lens_key, None


def _parse_integration(raw: str) -> MagiLiteResult | None:
    text = re.sub(r"```(?:json)?|```", "", raw or "")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    try:
        consensus = float(data.get("consensus", 0.5))
    except (TypeError, ValueError):
        consensus = 0.5
    consensus = min(max(consensus, 0.0), 1.0)
    return MagiLiteResult(
        consensus=consensus,
        divergences=[str(d)[:200] for d in (data.get("divergences") or [])][:6],
        guidance=str(data.get("guidance", ""))[:2000],
    )


async def run_lenses(
    *, question: str, evidence_digest: str, channel_id: int, model: str,
) -> MagiLiteResult | None:
    """3レンズ並列 → integrator-lite。失敗は degraded / None。"""
    try:
        from services.deep_research.magi import anonymize
        from services.deep_research.prompts import LENS_SPEC
        from services.deep_research.types import LensResult
        from services.mem0.client import get_anthropic_client
        from services.mem0.cli_compat import cli_messages_create, extract_text

        client = await get_anthropic_client()
        if client is None:
            return None
        lens_model = DELIB_MAGI_MODEL or model
        results = await asyncio.wait_for(
            asyncio.gather(*(
                _run_one_lens(client, k, question, evidence_digest, lens_model)
                for k in LENS_SPEC
            )),
            timeout=DELIB_MAGI_TIMEOUT_S,
        )
        lens_bodies = {k: b for k, b in results if b}
        if len(lens_bodies) < 2:
            log.warning(
                "[ch=%d] magi_lite: only %d lens(es) survived → skip",
                channel_id, len(lens_bodies),
            )
            return MagiLiteResult(consensus=1.0, degraded=True)
        # 匿名化 + シャッフル（judge バイアス対策、deep_research の実装を流用）
        anonymized = anonymize([
            LensResult(lens_key=k, lens_label=LENS_SPEC[k][0],
                       stance="", body=b, ok=True)
            for k, b in lens_bodies.items()
        ])
        resp = await cli_messages_create(
            client, model=lens_model, max_tokens=1500,
            user_message=_INTEGRATOR_PROMPT.format(
                question=question[:2000], anonymized=anonymized[:16000],
            ),
            task_prompt=_INTEGRATOR_TASK,
        )
        parsed = _parse_integration(extract_text(resp))
        if parsed is None:
            return MagiLiteResult(consensus=1.0, degraded=True)
        parsed.lens_bodies = lens_bodies
        log.info(
            "[ch=%d] magi_lite consensus=%.2f divergences=%d lenses=%d",
            channel_id, parsed.consensus, len(parsed.divergences),
            len(lens_bodies),
        )
        return parsed
    except Exception as exc:  # noqa: BLE001
        log.warning("[ch=%d] magi_lite failed (non-fatal): %s", channel_id, exc)
        return MagiLiteResult(consensus=1.0, degraded=True)


def build_lens_revise_prompt(magi: MagiLiteResult) -> str:
    """3視点ガイダンスを反映させる revise prompt（既存 revise 機構に乗せる）。"""
    lines = [
        "この問いを3つの独立した視点（科学/直観/懐疑）で分析した結果が届きました。"
        "以下のガイダンスの妥当な部分を反映して、回答全体を書き直してください。",
        "",
        f"統合ガイダンス: {magi.guidance}",
    ]
    if magi.divergences:
        lines.append("視点間で割れた論点: " + " / ".join(magi.divergences))
    if magi.consensus < DELIB_MAGI_CONSENSUS_MIN:
        lines.append(
            f"⚠️ 合意度が低い（{magi.consensus:.2f}）。次を必須とする: "
            "(1) 見解が割れている論点を回答に明示する、"
            "(2) 断定を避け、条件分岐（「〜なら A、〜なら B」）で提示する、"
            "(3) 自分の推奨には確信度を添える。"
        )
    lines.append("文体・構成は元のまま、修正後の回答全文のみを出力すること。")
    return "\n".join(lines)
