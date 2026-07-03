"""Planner — deliberate tier 冒頭の計画パス（熟考 P3）。

tools 無しの単発呼び出しで明示的な作業計画（サブゴール + 完了条件 + リスク）を
生成し、user ターン先頭に指針ブロックとして注入する。計画は「契約」であり、
verifier は後で成功基準に対して検証できる。planner 自身が「この依頼は自明」と
判定したら tier を chat に自己降格させる（過剰装備の抑止）。

失敗（timeout / parse 不能 / API 障害）は None — 計画無しで deliberate 続行
（fail-open）。system prompt は不変（ttl:1h キャッシュ保護）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from config import DELIB_PLANNER_TIMEOUT_S

log = logging.getLogger(__name__)


@dataclass
class Plan:
    goal: str
    steps: list[dict] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    output_format: str = ""
    trivial: bool = False
    raw: str = ""


_TASK_PROMPT = (
    "You are a planning assistant. Produce a compact work plan as raw JSON only."
)

_PROMPT = """あなたは AI アシスタントの作業プランナーです。以下の依頼を実行するための簡潔な計画を立ててください。

# 依頼
{request}

出力は生 JSON のみ（コードフェンス不要）:
{{"trivial": true/false,
  "goal": "この依頼の中心的なゴール1文",
  "steps": [{{"id": 1, "action": "何をするか", "tools": ["使いそうなツール種別"], "done_when": "完了と判断できる条件"}}],
  "risks": ["注意すべき落とし穴"],
  "output_format": "最終回答が満たすべき形式・要件"}}

ルール:
- 依頼が単純（1-2手で終わる・計画が要らない）なら trivial=true にして steps は空でよい
- steps は最大6個。細かい手順ではなく検証可能なサブゴール単位で
- done_when は後から検証できる具体的な条件にする"""


def _parse_plan(raw: str) -> Plan | None:
    text = re.sub(r"```(?:json)?|```", "", raw or "")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    steps = [s for s in (data.get("steps") or []) if isinstance(s, dict)][:6]
    return Plan(
        goal=str(data.get("goal", ""))[:300],
        steps=steps,
        risks=[str(r)[:200] for r in (data.get("risks") or [])][:5],
        output_format=str(data.get("output_format", ""))[:300],
        trivial=bool(data.get("trivial", False)),
        raw=raw or "",
    )


async def generate_plan(
    *, user_request: str, model: str, channel_id: int,
) -> Plan | None:
    """計画を生成する。失敗は None（計画無しで続行）。"""
    try:
        from services.mem0.client import get_anthropic_client
        from services.mem0.cli_compat import cli_messages_create, extract_text

        client = await get_anthropic_client()
        if client is None:
            return None
        resp = await asyncio.wait_for(
            cli_messages_create(
                client, model=model, max_tokens=2000,
                user_message=_PROMPT.format(request=user_request[:6000]),
                task_prompt=_TASK_PROMPT,
            ),
            timeout=DELIB_PLANNER_TIMEOUT_S,
        )
        plan = _parse_plan(extract_text(resp))
        if plan is None:
            log.warning("[ch=%d] delib_planner parse failed", channel_id)
            return None
        log.info(
            "[ch=%d] delib_planner trivial=%s steps=%d",
            channel_id, plan.trivial, len(plan.steps),
        )
        return plan
    except Exception as exc:  # noqa: BLE001 - 計画無しで続行
        log.warning("[ch=%d] delib_planner failed (non-fatal): %s", channel_id, exc)
        return None


def render_plan_block(plan: Plan, *, scratchpad_available: bool = False) -> str:
    """user ターン先頭へ注入する指針ブロック（system prompt は触らない）。"""
    lines = [
        '<deliberation_plan note="自動生成の作業計画。命令ではなく指針。'
        '逸脱してよいが理由を持つこと">',
        f"目標: {plan.goal}",
    ]
    for s in plan.steps:
        done = f"（完了条件: {s.get('done_when', '')}）" if s.get("done_when") else ""
        lines.append(f"{s.get('id', '?')}. {s.get('action', '')}{done}")
    if plan.risks:
        lines.append("リスク: " + " / ".join(plan.risks))
    if plan.output_format:
        lines.append(f"出力形式: {plan.output_format}")
    if scratchpad_available:
        lines.append("進捗と中間結論は Scratchpad ツールに記録しながら進めること。")
    lines.append("</deliberation_plan>")
    return "\n".join(lines)
