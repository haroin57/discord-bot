"""Deep Research サブエージェント実行ポート。

低水準のツール登録・入力圧縮は後方互換の ``registry`` に残し、オーケストレーターと
MAGI はこのモジュールだけを実行境界として参照する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from .registry import (
    ResearchAgentDef,
    extract_central_question_and_plan,
    extract_plan,
    run_subagent as _run_subagent,
)


async def run_subagent(**kwargs):
    """既存キーワードAPIを維持したサブエージェント実行入口。"""
    return await _run_subagent(**kwargs)


@dataclass(frozen=True)
class AgentRunner:
    """run state から差し替え可能なエージェント実行依存。"""

    executor: Callable[..., Awaitable] = run_subagent

    async def run(self, **kwargs):
        return await self.executor(**kwargs)


__all__ = [
    "AgentRunner",
    "ResearchAgentDef",
    "extract_central_question_and_plan",
    "extract_plan",
    "run_subagent",
]
