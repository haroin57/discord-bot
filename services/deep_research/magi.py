"""MAGI 推論層（科学 / 直観 / 懐疑 → integrator）の入れ子オーケストレーション（ADR-0030）.

事実収集（searcher）の後段で、3レンズが中心的問いに **独立に** 答え、integrator が合意
（BLUF テーゼ）と相違（本質的論点）を抽出する。コスト最適化のため各レンズは入れ子構成:

    decompose(Sonnet, tools 無し) → 並列 worker(Sonnet, 検証検索) → lead(Opus, effort=max, tools 無し)

Opus を使うのは各レンズの lead と integrator の **計4回の難所推論のみ**。検索・部品推論は
すべて Sonnet に逃がす。本モジュールは `run_subagent`（registry）と prompts の builder を
呼ぶだけの薄い層で、orchestrator から `_RunCtx` と表示用 view を受け取る（循環 import 回避）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re

from config import (
    DEEP_RESEARCH_MAGI_EFFORT,
    DEEP_RESEARCH_MAGI_IDLE_TIMEOUT_S,
    DEEP_RESEARCH_MAGI_MAX_WORKERS,
    DEEP_RESEARCH_MAGI_MODEL,
    DEEP_RESEARCH_MAGI_WORKER_MAX_SEARCHES,
    DEEP_RESEARCH_MAGI_WORKER_MODEL,
    DEEP_RESEARCH_REPORT_CRITIC_ENABLED,
    DEEP_RESEARCH_SEARCHER_IDLE_TIMEOUT_S,
)

from config import kanae_honorific

from .prompts import (
    LENS_SPEC,
    MAGI_DECOMPOSE_SYSTEM,
    REPORT_CRITIC_SYSTEM,
    build_decompose_briefing,
    build_evidence_digest,
    build_integrator_briefing,
    build_integrator_revision_briefing,
    build_integrator_system,
    build_lens_system,
    build_magi_lead_briefing,
    build_magi_worker_briefing,
    build_magi_worker_system,
    build_report_critic_briefing,
)
from .agent_runner import ResearchAgentDef, extract_plan, run_subagent
from .sources import build_final_report
from .types import LensResult

log = logging.getLogger(__name__)

_STANCE_RE = re.compile(r"^\s*STANCE\s*[:：]\s*(.+)$", re.MULTILINE)


def parse_lens_output(text: str) -> str:
    """lead 出力から STANCE 行（中心的問いへの立場の一文）を軽くパースして返す。

    fshiori/magi の POSITION タグ方式に倣う。STANCE が無ければ最初の非空行を gist に使う。
    """
    if not text:
        return ""
    m = _STANCE_RE.search(text)
    if m:
        return m.group(1).strip()
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith(("【", "#", "-")):
            return s
    return (text.strip().splitlines() or [""])[0][:200]


async def run_magi_lens(
    ctx, lens_key: str, view, sema: asyncio.Semaphore
) -> LensResult:
    """1 レンズ（科学/直観/懐疑）を入れ子構成で実行し LensResult を返す。

    例外は握って ok=False の LensResult に変換する（1レンズ失敗でも他レンズ・integrator は
    継続する＝FR-M5）。CancelledError のみ伝播（/abort 用）。
    """
    lens_label, _block = LENS_SPEC[lens_key]
    digest = build_evidence_digest(ctx.state)
    cq = ctx.state.central_question or ctx.state.clarified_query

    try:
        # (a) 分解（Sonnet, tools 無し）: レンズ観点の簡単な分析サブタスクへ。
        await view.working("分析を分解中…")
        decompose_def = ResearchAgentDef(
            agent_type="magi_decompose",
            model=DEEP_RESEARCH_MAGI_WORKER_MODEL,
            system_prompt=MAGI_DECOMPOSE_SYSTEM,
            max_tool_turns=1,
            idle_timeout_s=DEEP_RESEARCH_SEARCHER_IDLE_TIMEOUT_S,
        )
        async with sema:
            dres = await run_subagent(
                api_client=ctx.api_client,
                pool=ctx.pool,
                server_locks=ctx.server_locks,
                defn=decompose_def,
                briefing=build_decompose_briefing(
                    lens_label,
                    cq,
                    digest,
                    DEEP_RESEARCH_MAGI_MAX_WORKERS,
                ),
                tools_for_api=[],
                state=ctx.state,
                channel_id=ctx.thread.id,
            )
        subtasks = (
            extract_plan(dres.summary_md, DEEP_RESEARCH_MAGI_MAX_WORKERS)
            if dres.ok
            else []
        )

        # (b) ワーカー（Sonnet, 並列, 検証検索）: 各サブタスクを裏取りして分析部品に。
        worker_parts = ""
        if subtasks:
            await view.working(f"検証検索中…（{len(subtasks)} サブタスク）")
            worker_def = ResearchAgentDef(
                agent_type="magi_worker",
                model=DEEP_RESEARCH_MAGI_WORKER_MODEL,
                system_prompt=build_magi_worker_system(ctx.tools_for_api),
                max_tool_turns=DEEP_RESEARCH_MAGI_WORKER_MAX_SEARCHES,
                idle_timeout_s=DEEP_RESEARCH_SEARCHER_IDLE_TIMEOUT_S,
            )

            async def _worker(subtask: str):
                async with sema:
                    return await run_subagent(
                        api_client=ctx.api_client,
                        pool=ctx.pool,
                        server_locks=ctx.server_locks,
                        defn=worker_def,
                        briefing=build_magi_worker_briefing(lens_label, subtask, cq),
                        tools_for_api=ctx.tools_for_api,
                        state=ctx.state,
                        channel_id=ctx.thread.id,
                        tool_invocation_callback=view.on_tool,
                    )

            raw = await asyncio.gather(
                *[_worker(s) for s in subtasks], return_exceptions=True
            )
            chunks: list[str] = []
            for i, (st, r) in enumerate(zip(subtasks, raw), 1):
                if hasattr(r, "ok") and r.ok and r.summary_md.strip():
                    chunks.append(f"## 分析{i}: {st}\n{r.summary_md.strip()}")
            worker_parts = "\n\n".join(chunks)

        # (c) lead 統合（Opus, effort=max, tools 無し）: 難所の推論。
        await view.working("統合推論中…（Opus）")
        lead_def = ResearchAgentDef(
            agent_type="magi_lead",
            model=DEEP_RESEARCH_MAGI_MODEL,
            system_prompt=build_lens_system(lens_key),
            max_tool_turns=1,
            idle_timeout_s=DEEP_RESEARCH_MAGI_IDLE_TIMEOUT_S,
            effort=DEEP_RESEARCH_MAGI_EFFORT,
        )
        async with sema:
            lres = await run_subagent(
                api_client=ctx.api_client,
                pool=ctx.pool,
                server_locks=ctx.server_locks,
                defn=lead_def,
                briefing=build_magi_lead_briefing(lens_label, cq, digest, worker_parts),
                tools_for_api=[],
                state=ctx.state,
                channel_id=ctx.thread.id,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 1レンズの失敗で全体を止めない
        log.exception("magi lens %s failed", lens_key)
        return LensResult(
            lens_key=lens_key,
            lens_label=lens_label,
            stance="",
            body="",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    if not (lres.ok and lres.summary_md.strip()):
        return LensResult(
            lens_key=lens_key,
            lens_label=lens_label,
            stance="",
            body="",
            ok=False,
            error=lres.error or "empty lead output",
        )
    body = lres.summary_md.strip()
    return LensResult(
        lens_key=lens_key,
        lens_label=lens_label,
        stance=parse_lens_output(body),
        body=body,
        ok=True,
    )


def anonymize(lens_results: list[LensResult]) -> str:
    """ok な3レンズ出力を 視点A/B/C に匿名化し順序をシャッフルして1ブロックに整形。

    identity/self-preference バイアスを下げるため lens 名は伏せる（judge バイアス対策）。
    """
    ok = [r for r in lens_results if r.ok and r.body.strip()]
    shuffled = list(ok)
    random.shuffle(shuffled)
    labels = ["視点A", "視点B", "視点C", "視点D"]
    parts: list[str] = []
    for label, r in zip(labels, shuffled):
        parts.append(f"【{label}】\n{r.body.strip()}")
    return "\n\n".join(parts)


async def run_integrator(ctx) -> str | None:
    """3レンズ出力 + 証拠ベースを読み、BLUF 最終レポートを生成（旧 synthesizer を置換）。

    生存レンズが1つも無ければ None（呼び出し側が degrade）。
    """
    lens_results: list[LensResult] = list(ctx.state.lens_outputs)
    if not any(r.ok and r.body.strip() for r in lens_results):
        return None
    anonymized = anonymize(lens_results)
    digest = build_evidence_digest(ctx.state)
    cq = ctx.state.central_question or ctx.state.clarified_query
    # 最終回答だけ「佐藤かなえ」の人格・口調を乗せる（3視点の推論コアは厳密・中立のまま）。
    honorific = kanae_honorific(getattr(getattr(ctx, "user", None), "id", None))
    integ_def = ResearchAgentDef(
        agent_type="integrator",
        model=DEEP_RESEARCH_MAGI_MODEL,
        system_prompt=build_integrator_system(honorific),
        max_tool_turns=1,
        idle_timeout_s=DEEP_RESEARCH_MAGI_IDLE_TIMEOUT_S,
        effort=DEEP_RESEARCH_MAGI_EFFORT,
    )
    res = await run_subagent(
        api_client=ctx.api_client,
        pool=ctx.pool,
        server_locks=ctx.server_locks,
        defn=integ_def,
        briefing=build_integrator_briefing(
            cq,
            anonymized,
            digest,
            ctx.state.user_memories,
        ),
        tools_for_api=[],
        state=ctx.state,
        channel_id=ctx.thread.id,
    )
    if not (res.ok and res.summary_md.strip()):
        return None
    report = res.summary_md
    if DEEP_RESEARCH_REPORT_CRITIC_ENABLED:
        report = await _critic_pass(ctx, integ_def, cq, report, digest)
    return build_final_report(report, ctx.state)


async def _critic_pass(ctx, integ_def, cq: str, report: str, digest: str) -> str:
    """最終レポートへの adversarial verification（差し戻しは最大1回）。

    MAGI の懐疑レンズは証拠収集後・レポート生成前の独立推論で止まっており、
    integrator が書いた本文自体は誰も検証していなかった。critic（Sonnet・
    ツール無し）がレポートを証拠ベースと突き合わせ、指摘があれば integrator を
    1回だけ再実行する。critic / 修正のどちらが失敗しても元レポートで続行
    （FR-M5 と同じ degrade 思想）。
    """
    try:
        critic_def = ResearchAgentDef(
            agent_type="report_critic",
            model=DEEP_RESEARCH_MAGI_WORKER_MODEL,
            system_prompt=REPORT_CRITIC_SYSTEM,
            max_tool_turns=1,
            idle_timeout_s=DEEP_RESEARCH_SEARCHER_IDLE_TIMEOUT_S,
        )
        cres = await run_subagent(
            api_client=ctx.api_client,
            pool=ctx.pool,
            server_locks=ctx.server_locks,
            defn=critic_def,
            briefing=build_report_critic_briefing(report, digest),
            tools_for_api=[],
            state=ctx.state,
            channel_id=ctx.thread.id,
        )
        critique = (cres.summary_md or "").strip() if cres.ok else ""
        if not critique or critique.upper().startswith("PASS"):
            log.info("report critic: PASS (no revision)")
            return report
        log.info("report critic: %d chars of findings; revising once", len(critique))
        rres = await run_subagent(
            api_client=ctx.api_client,
            pool=ctx.pool,
            server_locks=ctx.server_locks,
            defn=integ_def,
            briefing=build_integrator_revision_briefing(cq, report, critique),
            tools_for_api=[],
            state=ctx.state,
            channel_id=ctx.thread.id,
        )
        if rres.ok and rres.summary_md.strip():
            return rres.summary_md
        log.warning("report critic revision failed; keeping original report")
        return report
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - critic 失敗でレポートを落とさない
        log.warning("report critic pass failed (non-fatal): %s", exc)
        return report
