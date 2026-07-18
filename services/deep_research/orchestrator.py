"""Deep Research オーケストレーター（ADR-0028）.

`/research` から fire-and-forget で呼ばれ、専用スレッドで多段 Web 調査を回し、引用付き
レポートをスレッドに出す。

- Phase 0: 単一エージェント反復型（`DEEP_RESEARCH_MULTI_AGENT=false` で使用、plan 失敗時の
  フォールバックでも使用）。
- Phase 1: CLI 式 multi-agent（planner → 並列 searcher → synthesizer）。

不変条件（docs/deep-research-mode.md「守るべき不変条件」）:
- session/JSONL を一切使わない（中間状態は ResearchState のみ）。
- サブエージェントは run_agent_loop 直呼びで claude_semaphore を通らない。
- MCP は 1 プール共有 + server-key lock + wait_for。playwright は使わない。
- 並列 searcher は専用 Semaphore で律速。watchdog の idle-cancel は子タスク単位で捕捉、
  本物の /abort は gather 越しに伝播する。
- finally で run 所有の stack.aclose()（MCP transport / subprocess group の回収）。
  process-wide orphan sweep は他 workflow の MCP を巻き込むため実行しない。
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Callable

import discord

from config import (
    DEEP_RESEARCH_CLARIFY_TIMEOUT_S,
    DEEP_RESEARCH_LEAD_MODEL,
    DEEP_RESEARCH_MAGI_APPROVE_HINT,
    DEEP_RESEARCH_MAGI_ENABLED,
    DEEP_RESEARCH_MAGI_IDLE_TIMEOUT_S,
    DEEP_RESEARCH_MAX_CQ_CORRECTIONS,
    DEEP_RESEARCH_MAX_PARALLEL,
    DEEP_RESEARCH_MAX_SEARCHES_PER_AGENT,
    DEEP_RESEARCH_MAX_SUBAGENTS,
    DEEP_RESEARCH_MIN_SUBAGENTS,
    DEEP_RESEARCH_MULTI_AGENT,
    DEEP_RESEARCH_REQUIRED_WEB_KEYS,
    DEEP_RESEARCH_SEARCHER_IDLE_TIMEOUT_S,
    DEEP_RESEARCH_SEARCHER_MODEL,
    DEEP_RESEARCH_SINGLE_AGENT_MAX_TURNS,
    DEEP_RESEARCH_SYNTH_IDLE_TIMEOUT_S,
    DEEP_RESEARCH_MCP_KEYS,
    DEEP_RESEARCH_TOTAL_TIME_BUDGET_S,
    DEEP_RESEARCH_TOTAL_TOKEN_BUDGET,
    MCP_SERVERS,
    MCP_SERVERS_WEB,
)
from services.claude_api.client import MODEL_BETAS
from services.claude_api.client_factory import make_api_client
from services.claude_api.mcp_client import (
    STATIC_MCP_TOOL_SCHEMAS,
    McpPool,
    mcp_tools_to_anthropic,
    static_mcp_tools_to_anthropic,
)
from services.claude_api.stall import StallWatchdog
from services.claude_api.tool_loop import run_agent_loop
from services.job_state import JobPhase, JobState, register_job, unregister_job
from services.mcp_control import (
    filter_enabled_mcp_configs,
    filter_enabled_mcp_keys,
)

from .agent_runner import (
    AgentRunner,
    ResearchAgentDef,
    extract_central_question_and_plan,
    extract_plan,
    run_subagent,
)
from .discord_adapter import (
    AgentView as _AgentView,
    SearcherView as _SearcherView,
    safe_edit as _adapter_safe_edit,
    safe_send as _adapter_safe_send,
    short as _adapter_short,
    thread_name as _adapter_thread_name,
    wait_for_reaction_or_reply,
)
from .evidence_registry import EvidenceRegistry
from .magi import run_integrator, run_magi_lens
from .magi_scheduler import MagiScheduler
from .prompts import (
    CLARIFIER_SYSTEM,
    PLANNER_MAGI_SYSTEM,
    PLANNER_SYSTEM,
    SYNTHESIZER_SYSTEM,
    build_clarify_briefing,
    build_evidence_digest,
    build_plan_briefing,
    build_plan_completion_briefing,
    build_plan_magi_briefing,
    build_refine_cq_briefing,
    build_research_briefing,
    build_research_system,
    build_search_briefing,
    build_searcher_system,
    build_synthesis_briefing,
    LENS_SPEC,
    REFINE_CQ_SYSTEM,
)
from .run_state import DEFAULT_RUNS, ResearchRun
from .sources import build_final_report
from .types import ResearchState, SubAgentResult

try:
    from services.correlation import new_request_id
except ImportError:  # pragma: no cover

    def new_request_id() -> str:  # type: ignore[misc]
        return ""


log = logging.getLogger(__name__)

_SPIN = "🔬"
# synthesizer の transient 失敗（429 等）時のリトライ前 backoff 秒。テストで 0 に上書き。
_SYNTH_RETRY_BACKOFF_S = 3.0


def _configured_max_concurrent_runs() -> int:
    raw = os.getenv("DEEP_RESEARCH_MAX_CONCURRENT_RUNS", "2")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        log.warning(
            "invalid DEEP_RESEARCH_MAX_CONCURRENT_RUNS=%r; using 2",
            raw,
        )
        return 2


# Process-wide finite admission gate. Searcher parallelism remains a separate,
# run-local limit; this gate bounds the number of complete research workflows.
_RUN_ADMISSION = asyncio.Semaphore(_configured_max_concurrent_runs())

# アクティブな研究スレッドの channel_id。on_message はここに含まれるスレッドのメッセージを
# スキップする — 明確化の返信や後続メンションが bot.wait_for と on_message に二重処理され、
# 別スレッドでタスクが多重起動するのを防ぐ（bot.wait_for は別リスナーなので、このセットに
# 関係なく明確化返信は届く）。研究完了で外れ、スレッド内の通常チャットは復帰する。
_active_threads = DEFAULT_RUNS.active_threads


def _mark_active(thread_id: int) -> None:
    DEFAULT_RUNS.mark_thread(thread_id)


def _unmark_active(thread_id: int) -> None:
    DEFAULT_RUNS.unmark_thread(thread_id)


def is_active_research_thread(channel_id: int) -> bool:
    """on_message から呼ぶ: この channel_id がアクティブな研究スレッドなら True。"""
    return DEFAULT_RUNS.is_active_thread(channel_id)


@dataclass
class DeepResearchDeps:
    """bot-module が所有する依存の注入（claude_tag.TagDeps と同型。sessions は渡さない）。"""

    bot: discord.Client
    send_segmented: Callable  # bot._send_segmented(message, text, files)
    get_http_session: Callable  # bot.get_http_session
    track_task: Callable  # bot._track_task


@dataclass
class _RunCtx:
    """1 回の /research 実行で research フェーズ間を引き回す状態。"""

    api_client: object
    pool: object
    server_locks: dict
    tools_for_api: list
    state: ResearchState
    thread: object
    anchor: object
    counter: dict
    tool_cb: Callable
    user: object = None  # 承認ゲートの待ち受け対象（FR-M1）
    deps: object = None  # bot.wait_for アクセス用
    run: ResearchRun | None = None
    agent_runner: AgentRunner | None = None
    evidence: EvidenceRegistry | None = None


async def _run_agent(ctx, **kwargs):
    """run に注入された runner を使い、簡易テストctxでは既存関数へフォールバックする。"""
    runner = getattr(ctx, "agent_runner", None)
    if runner is not None:
        return await runner.run(**kwargs)
    return await run_subagent(**kwargs)


async def _recall_user_memories(anchor, user, query: str, state: ResearchState) -> None:
    """リクエスト者の Mem0 長期記憶を search_memories_smart で recall（best-effort）。

    本ルート（bot.on_message）と同じ HyDE 含む hybrid 検索を使う。失敗しても run は止めない
    （Mem0 が落ちている／設定が無い場合に研究本体を巻き込まないため）。MAGI 3視点には
    渡さない: clarify/planner/refine/integrator の briefing にだけ注入する（独立性保護）。
    """
    if user is None or not getattr(user, "id", None):
        return
    try:
        from config import MEM0_RECALL_LIMIT
        from services.mem0.format import format_memories_for_prompt
        from services.mem0.search import search_memories_smart
    except ImportError:
        return
    await _safe_edit(anchor, f"{_SPIN} 長期記憶を確認しています…")
    try:
        memories = await search_memories_smart(
            query,
            str(user.id),
            limit=MEM0_RECALL_LIMIT,
            rewrite=True,
            rerank=True,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("deep_research mem0 recall failed (non-fatal)", exc_info=True)
        return
    block = format_memories_for_prompt(memories or [])
    if block:
        state.user_memories = block
        log.info("deep_research mem0 recall: %d memories injected", len(memories))


def available_research_keys() -> list[str]:
    """env で実際に有効な Deep Research 用 MCP key（在るものだけ。Web+X+金融+学術を横断）。"""
    return sorted(
        filter_enabled_mcp_keys(k for k in DEEP_RESEARCH_MCP_KEYS if k in MCP_SERVERS)
    )


def has_required_web_backend() -> bool:
    """brave-search または tavily が利用可能か（FR-1 前提条件）。"""
    return bool(
        filter_enabled_mcp_keys(
            k for k in DEEP_RESEARCH_REQUIRED_WEB_KEYS if k in MCP_SERVERS_WEB
        )
    )


async def run_deep_research(
    channel: discord.abc.Messageable,
    user: discord.abc.User,
    query: str,
    deps: DeepResearchDeps,
    *,
    skip_clarify: bool = False,
    depth: str = "standard",
) -> None:
    """公開エントリ。各呼び出しを独立した run state で並行実行する。"""
    acquired = False
    run: ResearchRun | None = None
    try:
        await _RUN_ADMISSION.acquire()
        acquired = True
        rid = new_request_id()
        run = DEFAULT_RUNS.create(query, run_id=rid or None)
        try:
            await _run(
                channel,
                user,
                query,
                deps,
                skip_clarify=skip_clarify,
                depth=depth,
                run=run,
            )
        finally:
            try:
                await run.cleanup()
            finally:
                if not DEFAULT_RUNS.discard(run):
                    log.error(
                        "deep_research run=%s retained after incomplete cleanup: %s",
                        run.run_id,
                        run.cleanup_diagnostic,
                    )
    finally:
        if acquired:
            _RUN_ADMISSION.release()


async def _close_owned_mcp_stack(stack: AsyncExitStack, run: ResearchRun) -> None:
    """run が所有する MCP transport と subprocess group だけを閉じる。"""
    try:
        # The stack contains only transports spawned through this run's pool.
        # Their context exits own TERM/KILL for their exact process groups.
        await run.close_owned_stack(stack.aclose)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning(
            "deep_research run=%s MCP cleanup failed after bounded retries: %s",
            run.run_id,
            run.cleanup_diagnostic,
            exc_info=True,
        )


async def _run(
    channel, user, query, deps, *, skip_clarify, depth, run: ResearchRun
) -> None:
    state = run.state
    origin = await channel.send(
        f"{_SPIN} **Deep Research**: {_short(query)}\n-# スレッドで進行します。中断はスレッド内で `/abort`。"
    )
    thread = await origin.create_thread(
        name=_thread_name(query), auto_archive_duration=1440
    )
    anchor = await thread.send(f"{_SPIN} 準備中…")

    job = JobState(
        rid=run.run_id,
        channel_id=thread.id,
        user_id=getattr(user, "id", 0),
        task=asyncio.current_task(),
    )
    register_job(job)

    stack = AsyncExitStack()
    run.require_mcp_cleanup()
    ok_done = False
    _mark_active(thread.id)
    try:
        await stack.__aenter__()

        # --- MCP プール（Web + X + 金融 + 学術 を横断利用。playwright/mem0 等は除外）---
        research_cfg = filter_enabled_mcp_configs(
            {k: MCP_SERVERS[k] for k in available_research_keys()}
        )
        if not has_required_web_backend():
            await _safe_edit(
                anchor,
                "❌ 一般 Web 検索バックエンド（brave-search / tavily）が未設定です。",
            )
            return
        api_client = await make_api_client(False)
        pool = McpPool(
            stack=stack,
            available_configs=research_cfg,
            process_observer=run.register_mcp_process,
        )
        # static_mcp_schemas.json に在るもの（brave/x-scraper/nikkei/yfj/polymarket）は
        # 「静的広告 + lazy spawn」で、searcher が実際に呼んだ時だけ起動して RAM を節約。
        # 無いもの（tavily/scholarly）だけ eager spawn して live tool を広告する。
        static_keys = [k for k in research_cfg if k in STATIC_MCP_TOOL_SCHEMAS]
        spawn_keys = [k for k in research_cfg if k not in STATIC_MCP_TOOL_SCHEMAS]
        if spawn_keys:
            await pool.spawn_eager(spawn_keys)
        tools_for_api = static_mcp_tools_to_anthropic(
            static_keys
        ) + mcp_tools_to_anthropic(
            {k: pool.handles[k] for k in spawn_keys if k in pool.handles}
        )
        if not tools_for_api:
            await _safe_edit(
                anchor, "❌ 検索ツールの起動に失敗しました。後で再試行してください。"
            )
            return

        # --- リクエスト者の Mem0 長期記憶を recall（best-effort）---
        # 本ルートと同じ search_memories_smart を使う。clarify/planner/refine/integrator の
        # briefing に注入されるが、MAGI 3視点（科学/直観/懐疑）には渡さない（独立性を守る）。
        await _recall_user_memories(anchor, user, query, state)

        # --- 明確化（常に1回。skip_clarify でバイパス）---
        job.phase = JobPhase.PREPROCESSING
        if not skip_clarify:
            await _clarify(
                thread, anchor, user, query, state, deps, api_client, run=run
            )

        # --- 調査（multi-agent / single-agent）---
        job.phase = JobPhase.CALLING_CLAUDE
        counter = {"n": 0, "t": 0.0}

        async def tool_cb(_name, _inp):
            counter["n"] += 1
            now = asyncio.get_running_loop().time()
            if now - counter["t"] < 1.5:
                return
            counter["t"] = now
            await _safe_edit(
                anchor,
                f"{_SPIN} 調査中… 検索 {counter['n']} 回 / ソース {len(state.sources)} 件",
            )

        ctx = _RunCtx(
            api_client=api_client,
            pool=pool,
            server_locks={},
            tools_for_api=tools_for_api,
            state=state,
            thread=thread,
            anchor=anchor,
            counter=counter,
            tool_cb=tool_cb,
            user=user,
            deps=deps,
            run=run,
            agent_runner=AgentRunner(executor=run_subagent),
            evidence=EvidenceRegistry(state),
        )

        if DEEP_RESEARCH_MULTI_AGENT:
            report = await _multi_agent_research(ctx)
        else:
            await _safe_edit(
                anchor, f"{_SPIN} Web を調査しています…（数分かかることがあります）"
            )
            report = await _single_agent_research(ctx)

        # --- 出力 ---
        job.phase = JobPhase.FINALIZING
        if report and report.strip():
            await deps.send_segmented(anchor, report, [])
            await _safe_edit(
                anchor,
                f"-# ✅ 調査完了 · 検索 {counter['n']} 回 / ソース {len(state.sources)} 件 / "
                f"~{state.tokens_used // 1000}k tokens",
            )
            ok_done = True
        else:
            # 何も得られなかった理由をユーザーに伝える（plan=0/sources=0 ＝全フェーズ
            # ハングか、Anthropic 過負荷の可能性が高い。失敗を黙って消すと「バグった」と
            # 見える）。
            if not state.plan and not state.sources:
                hint = (
                    "❌ 調査に失敗しました。Anthropic API の過負荷（overloaded）か "
                    "外部 MCP の不調の可能性があります。少し時間を置いて再試行してください。"
                )
            else:
                hint = "❌ 調査に失敗しました（有効な結果が得られませんでした）。"
            await _safe_edit(anchor, hint)
        log.info(
            "deep_research done ok=%s multi=%s subqs=%d searches=%d sources=%d tokens=%d elapsed=%.0fs",
            ok_done,
            DEEP_RESEARCH_MULTI_AGENT,
            len(state.plan),
            counter["n"],
            len(state.sources),
            state.tokens_used,
            state.elapsed_s(),
        )
    except asyncio.CancelledError:
        await _safe_edit(anchor, "⏹️ 中断しました。")
        raise
    except Exception:
        log.exception("deep_research failed")
        await _safe_edit(anchor, "❌ エラーが発生しました。")
    finally:
        _unmark_active(thread.id)
        job.phase = JobPhase.DONE if ok_done else JobPhase.FAILED
        unregister_job(job)
        await _close_owned_mcp_stack(stack, run)


# ---------------------------------------------------------------------------
# Phase 0: 単一エージェント
# ---------------------------------------------------------------------------
async def _single_agent_research(ctx: _RunCtx) -> str | None:
    defn = ResearchAgentDef(
        agent_type="researcher",
        model=DEEP_RESEARCH_LEAD_MODEL,
        system_prompt=build_research_system(ctx.tools_for_api),
        max_tool_turns=DEEP_RESEARCH_SINGLE_AGENT_MAX_TURNS,
        idle_timeout_s=DEEP_RESEARCH_SYNTH_IDLE_TIMEOUT_S,
    )
    result = await _run_agent(
        ctx,
        api_client=ctx.api_client,
        pool=ctx.pool,
        server_locks=ctx.server_locks,
        defn=defn,
        briefing=build_research_briefing(ctx.state.clarified_query),
        tools_for_api=ctx.tools_for_api,
        state=ctx.state,
        channel_id=ctx.thread.id,
        tool_invocation_callback=ctx.tool_cb,
    )
    return result.summary_md if result.ok else None


# ---------------------------------------------------------------------------
# Phase 1: CLI 式 multi-agent（planner → 並列 searcher → synthesizer）
# ---------------------------------------------------------------------------
async def _plan(ctx: _RunCtx) -> list[str]:
    """planner（tools なし）でテーマを 3〜N サブ質問に分解する。

    MAGI 有効時は「暫定の中心的問い + サブ質問」を返す MAGI planner を使い、
    暫定の中心的問い（Stage1）を ``state.central_question`` に格納して探索を方向づける。
    """
    await _safe_edit(ctx.anchor, f"{_SPIN} 調査計画を立てています…")
    magi = DEEP_RESEARCH_MAGI_ENABLED
    defn = ResearchAgentDef(
        agent_type="planner",
        model=DEEP_RESEARCH_LEAD_MODEL,
        system_prompt=PLANNER_MAGI_SYSTEM if magi else PLANNER_SYSTEM,
        max_tool_turns=1,
        idle_timeout_s=120,
    )
    briefing = (
        build_plan_magi_briefing(
            ctx.state.clarified_query,
            DEEP_RESEARCH_MAX_SUBAGENTS,
            ctx.state.user_memories,
            min_subagents=DEEP_RESEARCH_MIN_SUBAGENTS,
        )
        if magi
        else build_plan_briefing(
            ctx.state.clarified_query,
            DEEP_RESEARCH_MAX_SUBAGENTS,
            min_subagents=DEEP_RESEARCH_MIN_SUBAGENTS,
        )
    )
    result = await _run_agent(
        ctx,
        api_client=ctx.api_client,
        pool=ctx.pool,
        server_locks=ctx.server_locks,
        defn=defn,
        briefing=briefing,
        tools_for_api=[],
        state=ctx.state,
        channel_id=ctx.thread.id,
    )
    if not result.ok:
        return []
    if magi:
        cq, subqs = extract_central_question_and_plan(
            result.summary_md, DEEP_RESEARCH_MAX_SUBAGENTS
        )
        if _meaningful_cq(cq):
            ctx.state.central_question = cq  # Stage1 暫定（Stage2 で研ぎ直す）
        log.info("deep_research central_question (provisional): %r", (cq or "")[:300])
    else:
        subqs = extract_plan(result.summary_md, DEEP_RESEARCH_MAX_SUBAGENTS)
    if 0 < len(subqs) < DEEP_RESEARCH_MIN_SUBAGENTS:
        # 並列度を担保するため、planner が下限を満たさなかったら **補完呼び出し** を
        # 1 回だけ行う（rid 9e974424 で planner が 1 個しか返さず floor=5 を割った
        # 事故の回帰対策、haroin 2026-06-26）。失敗しても元の subqs で進む。
        need = DEEP_RESEARCH_MIN_SUBAGENTS - len(subqs)
        log.warning(
            "deep_research: planner produced %d sub-questions, below floor %d; "
            "requesting %d more",
            len(subqs),
            DEEP_RESEARCH_MIN_SUBAGENTS,
            need,
        )
        try:
            comp_defn = ResearchAgentDef(
                agent_type="planner_completion",
                model=DEEP_RESEARCH_LEAD_MODEL,
                system_prompt=PLANNER_SYSTEM,
                max_tool_turns=1,
                idle_timeout_s=120,
            )
            comp = await _run_agent(
                ctx,
                api_client=ctx.api_client,
                pool=ctx.pool,
                server_locks=ctx.server_locks,
                defn=comp_defn,
                briefing=build_plan_completion_briefing(
                    ctx.state.clarified_query,
                    subqs,
                    need,
                ),
                tools_for_api=[],
                state=ctx.state,
                channel_id=ctx.thread.id,
            )
            if comp.ok:
                extra = extract_plan(comp.summary_md, need)
                seen = {q.strip() for q in subqs}
                for q in extra:
                    if q and q.strip() not in seen:
                        subqs.append(q.strip())
                        seen.add(q.strip())
                        if len(subqs) >= DEEP_RESEARCH_MAX_SUBAGENTS:
                            break
                log.info(
                    "deep_research: planner completion added %d sub-questions, total %d",
                    len(extra),
                    len(subqs),
                )
        except Exception:
            log.warning(
                "deep_research: planner completion failed (non-fatal)", exc_info=True
            )
    return subqs


async def _multi_agent_research(ctx: _RunCtx) -> str | None:
    plan = await _plan(ctx)
    if not plan:
        log.info(
            "deep_research: planner produced no sub-questions; falling back to single-agent"
        )
        await _safe_edit(
            ctx.anchor,
            f"{_SPIN} 計画を立てられなかったため単一エージェントで調査します…",
        )
        return await _single_agent_research(ctx)
    ctx.state.plan = plan

    await _safe_edit(
        ctx.anchor,
        f"{_SPIN} 探索開始（{len(plan)} サブ質問・並列 {DEEP_RESEARCH_MAX_PARALLEL}）…",
    )
    # per-searcher 個別ライブメッセージ（並列ダッシュボード）。各 view が自分の
    # メッセージだけを編集するので並列に同時更新される。
    views: list[_SearcherView] = []
    for i, subq in enumerate(plan, 1):
        try:
            m = await ctx.thread.send(
                f"⏳ [{i}/{len(plan)}] {_short(subq, 60)} — 待機中"
            )
        except discord.HTTPException:
            m = None
        views.append(_SearcherView(i, len(plan), subq, m))

    searcher_def = ResearchAgentDef(
        agent_type="searcher",
        model=DEEP_RESEARCH_SEARCHER_MODEL,
        system_prompt=build_searcher_system(ctx.tools_for_api),
        max_tool_turns=DEEP_RESEARCH_MAX_SEARCHES_PER_AGENT,
        idle_timeout_s=DEEP_RESEARCH_SEARCHER_IDLE_TIMEOUT_S,
    )
    sema = asyncio.Semaphore(DEEP_RESEARCH_MAX_PARALLEL)
    done = {"n": 0}

    async def _bounded(
        view: "_SearcherView", model_override: str | None = None
    ) -> SubAgentResult:
        # 専用 semaphore で実走を律速（claude_semaphore は通らないので subscriber
        # バケットの保護はこれが担う）。各 searcher はツール呼び出しを自分の view に
        # 詳細表示する（tool_invocation_callback=view.on_tool）。
        async with sema:
            await view.searching()
            r = await _run_agent(
                ctx,
                api_client=ctx.api_client,
                pool=ctx.pool,
                server_locks=ctx.server_locks,
                defn=searcher_def,
                briefing=build_search_briefing(view.subq, ctx.state.clarified_query),
                tools_for_api=ctx.tools_for_api,
                state=ctx.state,
                channel_id=ctx.thread.id,
                tool_invocation_callback=view.on_tool,
                model_override=model_override,
            )
        done["n"] += 1
        await view.finish(r.ok and bool(r.summary_md.strip()))
        await _safe_edit(
            ctx.anchor,
            f"{_SPIN} 探索 {done['n']}/{len(plan)} 完了 / ソース {len(ctx.state.sources)} 件",
        )
        return r

    async def _collect(model_override: str | None = None) -> list[SubAgentResult]:
        # return_exceptions=True: searcher の watchdog idle-cancel は子タスク単位で結果に
        # 集約され gather は止まらない（兄弟 searcher は継続）。本物の /abort は _run の
        # タスクごと cancel されるため gather 越しに CancelledError が伝播する。
        raw = await asyncio.gather(
            *[_bounded(v, model_override) for v in views],
            return_exceptions=True,
        )
        out: list[SubAgentResult] = []
        for view, res in zip(views, raw):
            if isinstance(res, SubAgentResult):
                out.append(res)
            elif isinstance(res, BaseException):
                out.append(
                    SubAgentResult(
                        subq=view.subq, summary_md="", ok=False, error=str(res)
                    )
                )
        return out

    ctx.state.results = await _collect()
    if not any(r.ok and r.summary_md.strip() for r in ctx.state.results):
        # 全 searcher 失敗（例: searcher モデルが subscription で無効）→ lead model で1回リトライ。
        log.warning(
            "deep_research: all searchers failed on %s; retrying with lead model %s",
            DEEP_RESEARCH_SEARCHER_MODEL,
            DEEP_RESEARCH_LEAD_MODEL,
        )
        await _safe_edit(ctx.anchor, f"{_SPIN} 探索を再試行しています…")
        done["n"] = 0
        for v in views:
            await v.waiting()
        ctx.state.results = await _collect(model_override=DEEP_RESEARCH_LEAD_MODEL)
        if not any(r.ok and r.summary_md.strip() for r in ctx.state.results):
            log.warning("deep_research: all searchers failed after lead-model retry")
            return None

    # 予算（時間 / トークン）超過なら推論層をスキップして要約版に degrade。
    budget_reason = _over_budget(ctx.state)
    if budget_reason:
        log.warning(
            "deep_research over budget (%s); degrading to raw findings", budget_reason
        )
        await _safe_edit(ctx.anchor, f"{_SPIN} 予算上限のため要約版を作成します…")
        return build_final_report(_degraded_report(ctx.state), ctx.state)

    # MAGI 推論層（事実収集の後段に一段上のレイヤーを積む）。OFF なら旧 synthesizer。
    if DEEP_RESEARCH_MAGI_ENABLED:
        report = await _magi_inference(ctx)
        if report:
            return report
        log.warning(
            "deep_research: MAGI inference produced nothing; degrading to raw findings"
        )
        return build_final_report(_degraded_report(ctx.state), ctx.state)

    await _safe_edit(ctx.anchor, f"{_SPIN} レポートを執筆しています…")
    synth_def = ResearchAgentDef(
        agent_type="synthesizer",
        model=DEEP_RESEARCH_LEAD_MODEL,
        system_prompt=SYNTHESIZER_SYSTEM,
        max_tool_turns=1,
        idle_timeout_s=DEEP_RESEARCH_SYNTH_IDLE_TIMEOUT_S,
    )
    synth: SubAgentResult | None = None
    for attempt in range(2):
        synth = await _run_agent(
            ctx,
            api_client=ctx.api_client,
            pool=ctx.pool,
            server_locks=ctx.server_locks,
            defn=synth_def,
            briefing=build_synthesis_briefing(ctx.state),
            tools_for_api=[],
            state=ctx.state,
            channel_id=ctx.thread.id,
        )
        if synth.ok and synth.summary_md.strip():
            break
        if attempt == 0:
            log.warning(
                "deep_research: synthesizer failed (%s); retrying once", synth.error
            )
            await asyncio.sleep(_SYNTH_RETRY_BACKOFF_S)
    if synth is not None and synth.ok and synth.summary_md.strip():
        return build_final_report(synth.summary_md, ctx.state)
    # 最後の手段: searcher 結果を捨てず要約版で返す。
    log.warning(
        "deep_research: synthesizer failed after retry; degrading to raw findings"
    )
    return build_final_report(_degraded_report(ctx.state), ctx.state)


# ---------------------------------------------------------------------------
# MAGI 推論層（中心的問い研ぎ直し → 承認ゲート → 3レンズ並列 → integrator）
# ---------------------------------------------------------------------------
_MAGI_LENS_ORDER = ("science", "intuition", "skeptic")
_MAGI_LENS_EMOJI = {"science": "🧪", "intuition": "💡", "skeptic": "🤔"}


async def _magi_inference(ctx: _RunCtx) -> str | None:
    """事実収集後の MAGI 推論層。3レンズ独立推論 → integrator の BLUF レポート。"""
    scheduler = MagiScheduler(
        max_parallel=DEEP_RESEARCH_MAX_PARALLEL,
        lens_order=_MAGI_LENS_ORDER,
        lens_spec=LENS_SPEC,
        lens_emoji=_MAGI_LENS_EMOJI,
        view_factory=_AgentView,
        run_lens=run_magi_lens,
        run_integrator=run_integrator,
        safe_edit=_safe_edit,
    )
    return await scheduler.run(
        ctx,
        refine=_refine_central_question,
        approve=_central_question_gate,
    )


_CQ_LABELS = ("中心的問い", "核心")


def _meaningful_cq(cq: str) -> bool:
    """中心的問いとして実質的な内容があるか（ラベル/記号だけは空とみなす）。"""
    s = (cq or "").strip()
    for label in _CQ_LABELS:
        s = s.replace(label, "")
    s = s.strip("：:＃#＊*-—　 \t").strip()
    return len(s) >= 4


def _parse_refined_cq(text: str) -> tuple[str, str]:
    """研ぎ直し出力から「中心的問い」「核心」を抽出。

    モデルが ``中心的問い:`` の **次行** に本文を書くケース（同一行を期待していたが
    Opus がしばしば改行する）に対応する。ラベル行そのものを拾う退行を防ぐ。
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    n = len(lines)

    def _value_for(label: str) -> str:
        for i, s in enumerate(lines):
            if s.startswith((f"{label}:", f"{label}：")):
                rest = (
                    s.split("：", 1)[-1]
                    if s.startswith(f"{label}：")
                    else s.split(":", 1)[-1]
                )
                rest = rest.strip()
                if rest:
                    return rest
                # 値が次行以降にある: 別ラベルでない最初の非空行を採用
                for j in range(i + 1, n):
                    nxt = lines[j]
                    if not nxt:
                        continue
                    if any(nxt.startswith((f"{lb}:", f"{lb}：")) for lb in _CQ_LABELS):
                        break
                    return nxt
        return ""

    cq = _value_for("中心的問い")
    rationale = _value_for("核心")
    if not cq:
        # フォールバック: ラベル/見出し記号でない最初の実質行
        for s in lines:
            cand = s.lstrip("＃#＊*-—　 \t").strip()
            if cand and not any(cand.startswith(lb) for lb in _CQ_LABELS):
                cq = cand
                break
    return cq, rationale


async def _refine_central_question(ctx: _RunCtx) -> None:
    """Stage2: evidence を踏まえて中心的問いを研ぎ直し state に格納（Opus）。"""
    await _safe_edit(ctx.anchor, f"{_SPIN} 中心的問いを研ぎ直しています…")
    defn = ResearchAgentDef(
        agent_type="cq_refiner",
        model=DEEP_RESEARCH_LEAD_MODEL,
        system_prompt=REFINE_CQ_SYSTEM,
        max_tool_turns=1,
        idle_timeout_s=DEEP_RESEARCH_MAGI_IDLE_TIMEOUT_S,
    )
    res = await _run_agent(
        ctx,
        api_client=ctx.api_client,
        pool=ctx.pool,
        server_locks=ctx.server_locks,
        defn=defn,
        briefing=build_refine_cq_briefing(
            ctx.state.central_question,
            build_evidence_digest(ctx.state),
            ctx.state.user_memories,
        ),
        tools_for_api=[],
        state=ctx.state,
        channel_id=ctx.thread.id,
    )
    if res.ok and res.summary_md.strip():
        cq, rationale = _parse_refined_cq(res.summary_md)
        if _meaningful_cq(cq):
            ctx.state.central_question = cq
            ctx.state.central_question_rationale = rationale
        else:
            log.warning(
                "deep_research: refined CQ unusable (%r); keeping prior/clarified",
                cq,
            )
    if not _meaningful_cq(ctx.state.central_question):
        # planner も refine も実質的な問いを返せなかった → 調査テーマ自体を問いに使う。
        ctx.state.central_question = ctx.state.clarified_query
    log.info(
        "deep_research central_question (refined): %r",
        ctx.state.central_question[:300],
    )


async def _present_cq(ctx: _RunCtx):
    """研いだ中心的問いを根拠付きで提示。送れたらメッセージ、無理なら None。"""
    cq = ctx.state.central_question
    text = f"🧭 **中心的問い**: {_short(cq, 300)}"
    if ctx.state.central_question_rationale:
        text += f"\n-# 核心: {_short(ctx.state.central_question_rationale, 200)}"
    text += (
        f"\n-# この問いで MAGI 推論へ進みます。{DEEP_RESEARCH_CLARIFY_TIMEOUT_S}秒以内に "
        f"{DEEP_RESEARCH_MAGI_APPROVE_HINT} リアクション"
        "／メンションで承認、リプライ（または素のメッセージ）で訂正。`/abort` で中止。"
    )
    try:
        return await ctx.thread.send(text)
    except discord.HTTPException:
        return None


async def _await_reaction_or_reply(
    bot,
    uid,
    thread_id: int,
    target_msg,
    *,
    timeout_s=None,
    run=None,
):
    """互換入口。実際の deadline と task 回収は Discord adapter が担当する。"""
    timeout = DEEP_RESEARCH_CLARIFY_TIMEOUT_S if timeout_s is None else timeout_s
    return await wait_for_reaction_or_reply(
        bot,
        uid,
        thread_id,
        target_msg,
        timeout_s=timeout,
        run=run,
    )


def _mentions_bot(bot, msg) -> bool:
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    return any(
        getattr(u, "id", None) == bot_id for u in (getattr(msg, "mentions", None) or [])
    )


async def _await_gate_response(ctx: _RunCtx, gate_msg) -> tuple[str, str]:
    """ゲートの反応を deadline まで待つ。戻りは approve または correct。

    Discord の「返信」機能は元発言者（=bot）を自動メンションするため、`_mentions_bot`
    だけで approve 判定すると **リプライ=訂正** が approve に化ける。`msg.reference` が
    立っていればリプライ＝訂正として扱い、純粋な @ メンション（reference 無し）だけを
    approve とみなす。
    """
    run = getattr(ctx, "run", None)
    if run:
        run.begin_approval(DEEP_RESEARCH_CLARIFY_TIMEOUT_S)
        timeout = run.approval_time_left()
    else:
        timeout = DEEP_RESEARCH_CLARIFY_TIMEOUT_S
    kind, msg = await _await_reaction_or_reply(
        ctx.deps.bot,
        getattr(ctx.user, "id", None),
        ctx.thread.id,
        gate_msg,
        timeout_s=timeout,
        run=run,
    )
    if kind == "reaction":
        return ("approve", "")
    is_reply = getattr(getattr(msg, "reference", None), "message_id", None) is not None
    if not is_reply and _mentions_bot(ctx.deps.bot, msg):
        return ("approve", "")
    return ("correct", (getattr(msg, "content", "") or "").strip())


async def _central_question_gate(ctx: _RunCtx) -> None:
    """中心的問いの承認ゲート（FR-M1）。承認は deadline、訂正は上限付き。

    訂正が ``DEEP_RESEARCH_MAX_CQ_CORRECTIONS`` 回に達したら最後に研ぎ直した
    問いで自動続行する。bot/user 無しなら素通りする。
    """
    if not (
        ctx.deps and getattr(ctx.deps, "bot", None) is not None and ctx.user is not None
    ):
        return
    corrections = 0
    while True:
        gate_msg = await _present_cq(ctx)
        if gate_msg is None:
            return  # 提示できない環境なら承認待ちせず進む
        try:
            decision, text = await _await_gate_response(ctx, gate_msg)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await _safe_send(
                ctx.thread,
                "-# 承認待ちがタイムアウトしたため、この問いで続行します。",
            )
            return
        except Exception:
            log.warning(
                "deep_research CQ gate wait failed (non-fatal); proceeding",
                exc_info=True,
            )
            return
        if decision == "approve":
            await _safe_send(ctx.thread, "-# 承認を確認しました。MAGI 推論へ進みます。")
            return
        ctx.state.central_question = (
            f"{ctx.state.central_question}\n[ユーザーの訂正指示] {text}"
        )
        corrections += 1
        if corrections >= DEEP_RESEARCH_MAX_CQ_CORRECTIONS:
            await _safe_send(
                ctx.thread,
                f"-# 訂正が上限（{DEEP_RESEARCH_MAX_CQ_CORRECTIONS}回）に達したため、"
                "この訂正を反映した問いで続行します。",
            )
            await _refine_central_question(ctx)
            return
        await _safe_send(ctx.thread, "-# 訂正を反映して問いを研ぎ直します…")
        await _refine_central_question(ctx)


async def _clarify(
    thread, anchor, user, query, state, deps, api_client, *, run=None
) -> None:
    """明確化質問を生成し、run-scoped deadline まで返信を待つ。

    返信＝回答を畳み込む / リアクション・メンション＝確認なしで既定スコープ続行 /
    `/abort`＝中断。deadline 到達時は既定スコープで続行する。
    """
    await _safe_edit(anchor, f"{_SPIN} 調査方針を確認しています…")

    async def _noop_execute(_name, _inp):
        return "no tools available"

    questions = ""
    try:
        async with StallWatchdog(thread.id, None, idle_timeout_s=120) as watchdog:
            result = await run_agent_loop(
                build_clarify_briefing(query, state.user_memories),
                thread.id,
                history=[],
                system_prompt=CLARIFIER_SYSTEM,
                tools_for_api=[],
                can_use_tool=lambda _n, _i: (False, "no tools"),
                execute_tool=_noop_execute,
                max_turns=1,
                stall_watchdog=watchdog,
                api_client=api_client,
                model=DEEP_RESEARCH_LEAD_MODEL,
                extra_betas=MODEL_BETAS,
            )
        state.add_usage(getattr(result, "usage_total", None))
        questions = (getattr(result, "text", "") or "").strip()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("deep_research clarify failed (non-fatal)", exc_info=True)

    if not questions:
        return

    # bot/user が無い環境（テスト等）では待たずに既定スコープで進む。
    if not (deps and getattr(deps, "bot", None) is not None and user is not None):
        return

    q_msg = await thread.send(
        f"**調査前の確認です**（{DEEP_RESEARCH_CLARIFY_TIMEOUT_S}秒以内に返信してください。"
        "確認なしで進める場合はこのメッセージにリアクション／メンション、中断は `/abort`）\n"
        + questions[:1800]
    )

    try:
        if run:
            run.begin_approval(DEEP_RESEARCH_CLARIFY_TIMEOUT_S)
            timeout = run.approval_time_left()
        else:
            timeout = DEEP_RESEARCH_CLARIFY_TIMEOUT_S
        kind, msg = await _await_reaction_or_reply(
            deps.bot,
            getattr(user, "id", None),
            thread.id,
            q_msg,
            timeout_s=timeout,
            run=run,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        await _safe_send(
            thread,
            "-# 確認待ちがタイムアウトしたため、既定スコープで進めます。",
        )
        return
    except Exception:
        log.warning(
            "deep_research clarify wait failed (non-fatal); proceeding", exc_info=True
        )
        return

    # clarify は「答えるか / スキップするか」の二択。メンションをスキップ扱いにしない
    # （Discord の Reply は元発言者＝bot を自動メンションするので、回答が approve に
    # 誤分類されて回答内容が捨てられる）。リアクションのみスキップ、本文があれば回答。
    if kind == "reaction":
        await thread.send("-# 既定スコープで進めます。")
        return
    ans = (getattr(msg, "content", "") or "").strip()
    if ans:
        state.clarified_query = f"{query}\n\n[明確化での補足]\n{ans}"
        await thread.send("-# 補足を反映して調査を続けます。")
    else:
        await thread.send("-# 既定スコープで進めます。")


# ---------------------------------------------------------------------------
# Phase 3: 予算（時間 / トークン）と degrade
# ---------------------------------------------------------------------------
def _over_budget(state: ResearchState) -> str | None:
    """時間 / 累積トークンの上限超過なら理由文字列、未超過なら None。"""
    if state.elapsed_s() > DEEP_RESEARCH_TOTAL_TIME_BUDGET_S:
        return f"時間上限 {DEEP_RESEARCH_TOTAL_TIME_BUDGET_S}s 超過"
    if state.tokens_used > DEEP_RESEARCH_TOTAL_TOKEN_BUDGET:
        return f"トークン上限 {DEEP_RESEARCH_TOTAL_TOKEN_BUDGET} 超過"
    return None


def _degraded_report(state: ResearchState) -> str:
    """synthesizer をスキップして searcher 要約を連結した簡易レポート（予算/失敗時）。"""
    parts = ["**調査結果（簡易版・予算上限または執筆失敗のため要約のみ）**", ""]
    for i, res in enumerate(state.results, 1):
        if res.ok and res.summary_md.strip():
            parts.append(f"**サブ質問 {i}: {_short(res.subq, 80)}**")
            parts.append(res.summary_md.strip())
            parts.append("")
    return "\n".join(parts).rstrip()


# ---------------------------------------------------------------------------
# helpers（transient な Discord エラーを握りつぶす）
# ---------------------------------------------------------------------------
def _short(text: str, n: int = 120) -> str:
    return _adapter_short(text, n)


def _thread_name(query: str) -> str:
    return _adapter_thread_name(query)


async def _safe_edit(msg: discord.Message | None, content: str) -> None:
    await _adapter_safe_edit(msg, content)


async def _safe_send(channel, content: str) -> None:
    await _adapter_safe_send(channel, content)
