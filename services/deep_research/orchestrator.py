"""Deep Research オーケストレーター（ADR-0028）.

`/research` から fire-and-forget で呼ばれ、専用スレッドで多段 Web 調査を回し、引用付き
レポートをスレッドに出す。

- Phase 0: 単一エージェント反復型（`DEEP_RESEARCH_MULTI_AGENT=false` で使用、plan 失敗時の
  フォールバックでも使用）。
- Phase 1: CLI 式 multi-agent（planner → 並列 searcher → synthesizer）。

不変条件（docs/deep-research-mode.md「守るべき不変条件」）:
- session/JSONLを使わず、中間状態はResearchStateとWorkStoreに保持する。
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
import json
import re
from weakref import WeakValueDictionary
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
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
from services.work.research import (
    ResearchInterrupted, ResearchJournal, apply_assessment, assessment_briefing,
    completion_note, parse_record, record_evaluation, requirement_values,
    unresolved_publications,
)

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
_WORK_ADMISSION: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

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
    work_store: object = None
    work_id: str | None = None
    research_mode: str = "auto"
    allowed_assumptions: tuple[str, ...] = ()
    memory_policy: object = None


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
    journal: ResearchJournal | None = None


async def _run_agent(ctx, **kwargs):
    """run に注入された runner を使い、簡易テストctxでは既存関数へフォールバックする。"""
    journal = getattr(ctx, "journal", None)
    if journal:
        journal.assert_current(ctx.state)
    kwargs["briefing"] = kwargs.get("briefing", "") + _requirements_briefing(ctx.state)
    runner = getattr(ctx, "agent_runner", None)
    if runner is not None:
        result = await runner.run(**kwargs)
    else:
        result = await run_subagent(**kwargs)
    if journal:
        journal.assert_current(ctx.state)
    return result


def _requirements_briefing(state) -> str:
    if not getattr(state, "work_id", None):
        return ""
    return (
        f"\n\n[保存済み調査条件: 版 {state.work_revision}]\n"
        f"目的: {state.mode}\n条件: {json.dumps(state.requirements, ensure_ascii=False)}\n"
        "学習目的なら既知・未理解・確認問題・次の説明を整理します。判断目的なら"
        "選択肢・採用理由・必須条件・結論が変わる条件を整理します。"
        "根拠の取得日時と対象時点を区別し、未確認事項を明示します。"
    )


def _research_mode(query: str, requested: str = "auto") -> str:
    if requested in {"decision", "learning"}:
        return requested
    if re.search(r"比較|選ぶ|選択|判断|採用|どれ|which|compare|choose", query, re.I):
        return "decision"
    if re.search(r"理解|学び|学習|教えて|説明|仕組み|learn|understand|explain", query, re.I):
        return "learning"
    return "decision"


def _checkpoint(ctx, phase: str) -> None:
    ctx.state.phase = phase
    if getattr(ctx, "journal", None):
        ctx.journal.save(ctx.state, phase)


async def _recall_user_memories(anchor, user, query: str, state: ResearchState, *, journal=None) -> None:
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
    if journal is not None:
        policy = journal.memory_policy
        view = dict(channel_id=journal.channel_id, is_private=journal.is_private, case_id=state.work_id)
        memories = policy.filter_memories(memories or [], str(user.id), **view)
        memories.extend(policy.recall(str(user.id), query=query, **view))
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
    work_lock = None
    work_acquired = False
    try:
        # Persist admission before waiting for a process-local concurrency slot.
        journal = None
        store = getattr(deps, "work_store", None)
        owner_id = getattr(user, "id", None)
        if store is not None:
            work_id = getattr(deps, "work_id", None)
            if work_id is None:
                work = store.create(
                    owner_id, channel.id, query,
                    f"research:{new_request_id() or __import__('uuid').uuid4().hex}",
                    guild_id=getattr(getattr(channel, "guild", None), "id", None),
                    completion_criteria=["調査の問いに根拠付きで回答する"],
                )
                work_id = work.id
            else:
                work = store.get(work_id, owner_id)
            work_lock = _WORK_ADMISSION.setdefault(work_id, asyncio.Lock())
            await work_lock.acquire()
            work_acquired = True
            work = store.get(work_id, owner_id)
            if work.state in {"cancelled", "needs_reconciliation"} or unresolved_publications(store, work_id):
                from services.work.store import WorkStateConflict
                raise WorkStateConflict("取消済み、または送信結果の照合が必要な調査です。")
            if not work.completion_criteria:
                work = store.amend(work_id, owner_id, "", f"research-criteria:{work_id}:{work.revision}",
                                   expected_revision=work.revision,
                                   completion_criteria=["調査の問いに根拠付きで回答する"])
            from services.work.store import WorkAccessDenied
            if work.audience == "owner" and not isinstance(channel, discord.DMChannel):
                raise WorkAccessDenied("本人限定の調査は本人とのDMで再開してください。")
            if work.audience == "channel" and work.channel_id != str(channel.id):
                raise WorkAccessDenied("この調査は指定チャンネルで再開してください。")
            journal = ResearchJournal(
                store, work_id, owner_id, channel_id=channel.id,
                is_private=isinstance(channel, discord.DMChannel),
                memory_policy=getattr(deps, "memory_policy", None),
            )
            state = journal.restore() or ResearchState(
                query=work.goal, clarified_query=work.goal,
                mode=_research_mode(work.goal, getattr(deps, "research_mode", "auto")),
            )
            state.work_id, state.work_revision = work_id, work.revision
            state.requirements = journal.requirements(work)
            state.completion_criteria = list(work.completion_criteria) or ["調査の問いに根拠付きで回答する"]
            journal.reset_invalid_context(state)
            journal.supersede_stale_questions(state)
            journal.attach_context(state)
            journal.save(state, "accepted" if state.phase == "init" else None)
        await _RUN_ADMISSION.acquire()
        acquired = True
        rid = new_request_id()
        run = DEFAULT_RUNS.create(query, run_id=rid or None)
        if journal is not None:
            run.state = state
            run.journal = journal
            journal.transition(state, "running")
            state.incomplete_reason = ""
            state.unknowns = []
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
        except BaseException:
            # Thread/anchor creation can fail before _run enters its owned MCP
            # cleanup scope. Keep the accepted Work resumable in that case too.
            if journal:
                current = store.get(journal.work_id, owner_id)
                if current.state == "running" and current.revision == state.work_revision:
                    journal.pause(state, "調査の開始または継続を確認できませんでした。", waiting=False)
            raise
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
        if work_acquired:
            work_lock.release()


async def resume_deep_research(channel, user, work_id, deps, *, answer=None, question_id=None) -> None:
    """Resume by durable Work identity; no prior native session is needed."""
    store = deps.work_store
    if store is None:
        raise ValueError("research resume requires WorkStore")
    work = store.get(work_id, getattr(user, "id", None))
    if answer is not None:
        if not question_id:
            raise ValueError("question_id is required for a research answer")
        store.answer_question(work_id, question_id, user.id, answer,
                              f"research-resume-answer:{__import__('uuid').uuid4().hex}")
    await run_deep_research(channel, user, work.goal, replace(deps, work_id=work_id), skip_clarify=False)


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
    journal = run.journal
    origin = await channel.send(
        f"{_SPIN} **Deep Research**: {_short(query)}\n-# スレッドで進行します。中断はスレッド内で `/abort`。"
    )
    if isinstance(channel, discord.DMChannel):
        thread = channel
    else:
        thread = await origin.create_thread(name=_thread_name(query), auto_archive_duration=1440)
    anchor = await thread.send(f"{_SPIN} 準備中…")
    if journal:
        journal.store.bind_message(journal.work_id, origin.id, channel.id)
        journal.store.bind_message(journal.work_id, anchor.id, thread.id)

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
            if journal:
                journal.pause(state, "Web検索の接続が未設定です。", waiting=False)
            return
        from services.codex_app_server import auxiliary
        api_client = None if auxiliary.enabled() else await make_api_client(False)
        pool_type = McpPool
        if auxiliary.enabled():
            from services.deep_research.mcp_actor import ResearchMcpPool
            pool_type = ResearchMcpPool
        pool = pool_type(
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
            if journal:
                journal.pause(state, "調査ツールの起動が未完了です。", waiting=False)
            return

        # --- リクエスト者の Mem0 長期記憶を recall（best-effort）---
        # 本ルートと同じ search_memories_smart を使う。clarify/planner/refine/integrator の
        # briefing に注入されるが、MAGI 3視点（科学/直観/懐疑）には渡さない（独立性を守る）。
        if journal:
            await _recall_user_memories(anchor, user, query, state, journal=journal)
        else:
            await _recall_user_memories(anchor, user, query, state)

        # --- 明確化（常に1回。skip_clarify でバイパス）---
        job.phase = JobPhase.PREPROCESSING
        if not skip_clarify:
            await _clarify(
                thread, anchor, user, query, state, deps, api_client, run=run
            )
        elif journal:
            journal.save(state, "clarified")

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
            journal=journal,
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
            if journal:
                await _assess_completion(ctx, report)
                journal.update_summary(state)
                report = report + "\n\n" + completion_note(state)
                journal.assert_current(state)
                artifact = journal.artifact(state, report)
                _checkpoint(ctx, "publishing")
                await journal.publish(state, thread, artifact, report)
            else:
                await deps.send_segmented(anchor, report, [])
            await _safe_edit(
                anchor,
                f"-# {'調査完了' if not journal or state.completion_status == 'complete' else '暫定結果を保存しました'} · 検索 {counter['n']} 回 / ソース {len(state.sources)} 件 / "
                f"~{state.tokens_used // 1000}k tokens",
            )
            ok_done = True
            if journal:
                journal.transition(state, "completed" if state.completion_status == "complete" else "paused")
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
            if journal:
                journal.pause(state, "有効な調査結果が未取得です。", waiting=False)
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
    except ResearchInterrupted as exc:
        if journal:
            current = journal.store.get(journal.work_id, journal.actor_id)
            if current.revision == state.work_revision and current.state == "running":
                journal.pause(state, str(exc), waiting=state.phase == "waiting_input")
        await _safe_edit(anchor, str(exc) or "回答待ちとして保存しました。用件の再開で続行します。")
    except asyncio.CancelledError:
        if journal:
            current = journal.store.get(journal.work_id, journal.actor_id)
            if current.revision == state.work_revision and current.state == "running":
                journal.pause(state, "調査を中断しました。保存済みの地点から再開できます。", waiting=False)
        await _safe_edit(anchor, "⏹️ 中断しました。")
        raise
    except Exception:
        log.exception("deep_research failed")
        await _safe_edit(anchor, "❌ エラーが発生しました。")
        if journal:
            current = journal.store.get(journal.work_id, journal.actor_id)
            if current.revision == state.work_revision and current.state == "running":
                journal.pause(state, "実行エラーのため未完了です。", waiting=False)
    finally:
        _unmark_active(thread.id)
        job.phase = JobPhase.DONE if ok_done else JobPhase.FAILED
        unregister_job(job)
        await _close_owned_mcp_stack(stack, run)


async def _assess_completion(ctx, report: str) -> None:
    _checkpoint(ctx, "assessing")
    result = await _run_agent(
        ctx, api_client=ctx.api_client, pool=ctx.pool, server_locks=ctx.server_locks,
        defn=ResearchAgentDef(agent_type="research_record", model=DEEP_RESEARCH_LEAD_MODEL,
                              system_prompt="公開可能な調査結果を検証し、要求されたJSONのみを返します。",
                              max_tool_turns=1, idle_timeout_s=120),
        briefing=assessment_briefing(ctx.state, report), tools_for_api=[],
        state=ctx.state, channel_id=ctx.thread.id,
    )
    apply_assessment(ctx.state, report, parse_record(result.summary_md) if result.ok else {})
    _checkpoint(ctx, "assessed")


# ---------------------------------------------------------------------------
# Phase 0: 単一エージェント
# ---------------------------------------------------------------------------
async def _single_agent_research(ctx: _RunCtx) -> str | None:
    _checkpoint(ctx, "researching")
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
    if getattr(ctx, "journal", None):
        ctx.state.plan = [ctx.state.clarified_query]
        result.subq = ctx.state.clarified_query
        ctx.state.results = [result]
        record_evaluation(ctx.state, result.subq, result)
        _checkpoint(ctx, "researched")
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
    if ctx.state.plan:
        briefing += (
            "\n\n既存の調査項目があります。条件変更に関係しない項目の文言は変更せず、"
            "不足した項目だけ追加します。\n" + json.dumps(ctx.state.plan, ensure_ascii=False)
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
    _checkpoint(ctx, "planning")
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
    _checkpoint(ctx, "planned")
    reusable = {
        result.subq: result for result in ctx.state.results
        if result.ok and not ctx.state.evaluations.get(result.subq, {"stale": True}).get("stale")
    } if getattr(ctx, "journal", None) else {}

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
            if view.subq in reusable:
                r = reusable[view.subq]
                await view.finish(True)
                return r
            await view.searching()
            briefing = build_search_briefing(view.subq, ctx.state.clarified_query)
            if getattr(ctx, "journal", None):
                briefing += (
                    "\n末尾に、この評価が依存した条件キーを次の形式で記録します。"
                    "関係しない条件は含めません。不明なら[\"*\"]です。"
                    '<research_dependencies>{"requirement_keys": ["key"]}</research_dependencies>'
                )
            r = await _run_agent(
                ctx,
                api_client=ctx.api_client,
                pool=ctx.pool,
                server_locks=ctx.server_locks,
                defn=searcher_def,
                briefing=briefing,
                tools_for_api=ctx.tools_for_api,
                state=ctx.state,
                channel_id=ctx.thread.id,
                tool_invocation_callback=view.on_tool,
                model_override=model_override,
            )
            r.subq = view.subq
            if getattr(ctx, "journal", None):
                match = re.search(r"<research_dependencies>(.*?)</research_dependencies>", r.summary_md, re.S)
                metadata = parse_record(match.group(1)) if match else {}
                if match:
                    r.summary_md = r.summary_md[:match.start()] + r.summary_md[match.end():]
                record_evaluation(ctx.state, view.subq, r, metadata)
                ctx.state.results = [item for item in ctx.state.results if item.subq != view.subq] + [r]
                _checkpoint(ctx, "researching")
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
            if isinstance(res, ResearchInterrupted):
                raise res
            if isinstance(res, Exception) and type(res).__name__ in {"WorkRevisionConflict", "WorkStateConflict"}:
                raise ResearchInterrupted("条件または状態が変更されました。最新の条件で再開します。") from res
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
    _checkpoint(ctx, "researched")
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
        _checkpoint(ctx, "researched")
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
    _checkpoint(ctx, "synthesizing")
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
    if _approved_cq(ctx.state):
        return
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
        "\n-# この問いで進む場合は ✅ または 👍、または「承認」と返信します。"
        "訂正はこのメッセージへの返信で指定します。回答がない場合は保存して待機します。"
    )
    if getattr(ctx, "journal", None):
        pending = next((q for q in ctx.state.questions.values() if q.get("kind") == "central_question" and q.get("status") != "answered"), None)
        if pending:
            text += f"\n用件 `{ctx.state.work_id}` / 質問 `{pending['id']}`。"
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
    """特定質問への明示的な承認・訂正・後日の回答指定を区別します。"""
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
    content = (getattr(msg, "content", "") or "").strip()
    if content.lower() in {"承認", "進めて", "続けて", "approve", "continue"}:
        return ("approve", "")
    if _deferred_answer(content):
        return ("defer", content)
    return ("correct", content)


def _deferred_answer(text: str) -> bool:
    return bool(re.search(r"(?:明日|後日|あとで|後で|later|tomorrow).{0,12}(?:回答|返答|答え|answer)|(?:answer|reply).{0,12}(?:later|tomorrow)", text, re.I))


def _suspend_for_answer(state, journal, reason: str) -> None:
    state.phase = "waiting_input"
    state.completion_status = "partial"
    if journal:
        journal.pause(state, reason)
    raise ResearchInterrupted(reason)


def _approved_cq(state) -> bool:
    requirements = {key: value for key, value in state.requirements.items() if key != "answers"}
    return any(q.get("kind") == "central_question"
               and q.get("text") == state.central_question and q.get("status") == "answered"
               and q.get("requirement_values") == requirements
               and str(q.get("answer", "")).lower() in {"承認", "approve"}
               for q in state.questions.values())


async def _central_question_gate(ctx: _RunCtx) -> None:
    """未回答・通信失敗・訂正上限では質問を保存して実行資源を解放します。"""
    if not (
        ctx.deps and getattr(ctx.deps, "bot", None) is not None and ctx.user is not None
    ):
        return
    corrections = 0
    journal = getattr(ctx, "journal", None)
    while True:
        if _approved_cq(ctx.state):
            return
        question = journal.question(ctx.state, ctx.state.central_question, kind="central_question") if journal else None
        _checkpoint(ctx, "central_question")
        gate_msg = await _present_cq(ctx)
        if gate_msg is None:
            _suspend_for_answer(ctx.state, journal, "確認の提示が未完了です。回答待ちとして保存しました。")
        if journal:
            journal.bind_question(ctx.state, question["id"], gate_msg.id, ctx.thread.id)
        try:
            decision, text = await _await_gate_response(ctx, gate_msg)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            _suspend_for_answer(ctx.state, journal, "回答待ちとして保存しました。用件の質問に回答すると再開できます。")
        except Exception:
            log.warning(
                "deep_research CQ gate wait failed; preserving unanswered question",
                exc_info=True,
            )
            _suspend_for_answer(ctx.state, journal, "回答の取得を確認できませんでした。回答待ちとして保存しました。")
        if decision == "approve":
            if question:
                journal.answer(ctx.state, question["id"], "承認")
            ctx.state.approval_revision = ctx.state.work_revision
            _checkpoint(ctx, "central_question_approved")
            await _safe_send(ctx.thread, "-# 承認を確認しました。MAGI 推論へ進みます。")
            return
        if decision == "defer":
            if question:
                question["deferred_request"] = text
            _suspend_for_answer(ctx.state, journal, "後日の回答待ちとして保存しました。")
        if question:
            journal.answer(ctx.state, question["id"], text)
        ctx.state.central_question = (
            f"{ctx.state.central_question}\n[ユーザーの訂正指示] {text}"
        )
        corrections += 1
        if corrections >= DEEP_RESEARCH_MAX_CQ_CORRECTIONS:
            await _safe_send(
                ctx.thread,
                "-# 訂正を保存しました。次回は修正後の問いから確認します。",
            )
            await _refine_central_question(ctx)
            if journal:
                journal.question(ctx.state, ctx.state.central_question, kind="central_question")
            _suspend_for_answer(ctx.state, journal, "修正後の問いへの回答待ちとして保存しました。")
        await _safe_send(ctx.thread, "-# 訂正を反映して問いを研ぎ直します…")
        await _refine_central_question(ctx)


async def _clarify(
    thread, anchor, user, query, state, deps, api_client, *, run=None
) -> None:
    """不足する条件だけを質問し、未回答なら再開可能な回答待ちにします。"""
    journal = getattr(run, "journal", None)
    saved = [q for q in getattr(state, "questions", {}).values() if q.get("kind") == "clarification"]
    if saved and all(q.get("status") == "answered" for q in saved):
        return
    if getattr(state, "plan", None) and not saved:
        return
    await _safe_edit(anchor, f"{_SPIN} 調査方針を確認しています…")

    async def _noop_execute(_name, _inp):
        return "no tools available"

    questions = next((q["text"] for q in saved if q.get("status") != "answered"), "")
    if questions:
        return await _wait_clarification(thread, user, query, state, deps, run, questions)
    try:
        from services.codex_app_server import auxiliary
        if auxiliary.enabled():
            result = await auxiliary.run_task(
                prompt=build_clarify_briefing(getattr(state, 'clarified_query', None) or query, state.user_memories) + _requirements_briefing(state),
                instructions=CLARIFIER_SYSTEM, timeout_s=120,
            )
            state.add_usage(auxiliary.normalized_usage(result))
            questions = result.final_text.strip()
        else:
            async with StallWatchdog(thread.id, None, idle_timeout_s=120) as watchdog:
                result = await run_agent_loop(
                    build_clarify_briefing(getattr(state, 'clarified_query', None) or query, state.user_memories) + _requirements_briefing(state),
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
        structured = parse_record(questions)
        if isinstance(structured.get("questions"), list):
            items = [item for item in structured["questions"] if isinstance(item, dict) and item.get("text")]
            state.clarification_required = any(item.get("required", True) for item in items)
            defaults = [str(item["default"]) for item in items if item.get("default") is not None]
            state.clarification_default = " / ".join(defaults) if defaults else None
            questions = "\n".join("- " + str(item["text"]) for item in items)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("deep_research clarify failed (non-fatal)", exc_info=True)

    if not questions:
        return

    # bot/user が無い環境（テスト等）では待たずに既定スコープで進む。
    if not (deps and getattr(deps, "bot", None) is not None and user is not None):
        return

    await _wait_clarification(thread, user, query, state, deps, run, questions)


async def _wait_clarification(thread, user, query, state, deps, run, questions):
    journal = getattr(run, "journal", None)
    assumptions = getattr(deps, "allowed_assumptions", ())
    required = state.clarification_required and not bool(assumptions)
    question = journal.question(state, questions, required=required,
                                default=(" / ".join(assumptions) if assumptions else state.clarification_default)
                                if not required else None) if journal else None
    if assumptions:
        assumption_text = " / ".join(assumptions)
        if question:
            journal.answer(state, question["id"], assumption_text)
        state.clarified_query = f"{query}\n\n[事前に認められた仮定]\n{assumption_text}"
        if journal:
            journal.save(state, "clarified")
        return

    q_msg = await thread.send(
        "**調査前の確認です**。このメッセージへの返信で回答します。"
        "回答がない場合は回答待ちとして保存します。"
        + ("必須条件には本文で回答します。\n" if required else f"既定条件を採用する場合は ✅ または 👍 を指定します。既定条件: {state.clarification_default or '依頼本文の条件'}。\n")
        + questions[:1600]
        + (f"\n用件 `{state.work_id}` / 質問 `{question['id']}`。" if question else "")
    )
    if journal:
        journal.bind_question(state, question["id"], q_msg.id, thread.id)
        journal.save(state, "clarifying")

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
        _suspend_for_answer(state, journal, "回答待ちとして保存しました。用件の質問に後から回答できます。")
    except Exception:
        log.warning(
            "deep_research clarify wait failed; preserving unanswered question", exc_info=True
        )
        _suspend_for_answer(state, journal, "回答を確認できませんでした。回答待ちとして保存しました。")

    # clarify は「答えるか / スキップするか」の二択。メンションをスキップ扱いにしない
    # （Discord の Reply は元発言者＝bot を自動メンションするので、回答が approve に
    # 誤分類されて回答内容が捨てられる）。リアクションのみスキップ、本文があれば回答。
    if kind == "reaction":
        if required:
            _suspend_for_answer(state, journal, "必須条件への回答が未取得です。本文での回答待ちとして保存しました。")
        if question:
            journal.answer(state, question["id"], state.clarification_default or "依頼本文の条件で調査を進める")
        await thread.send("-# 既定スコープで進めます。")
        return
    ans = (getattr(msg, "content", "") or "").strip()
    if _deferred_answer(ans):
        if question:
            question["deferred_request"] = ans
        _suspend_for_answer(state, journal, "後日の回答待ちとして保存しました。")
    if ans:
        if question:
            journal.answer(state, question["id"], ans)
        state.clarified_query = f"{query}\n\n[明確化での補足]\n{ans}"
        if journal:
            journal.save(state, "clarified")
        await thread.send("-# 補足を反映して調査を続けます。")
    else:
        _suspend_for_answer(state, journal, "必須質問への回答が空です。回答待ちとして保存しました。")


# ---------------------------------------------------------------------------
# Phase 3: 予算（時間 / トークン）と degrade
# ---------------------------------------------------------------------------
def _over_budget(state: ResearchState) -> str | None:
    """時間 / 累積トークンの上限超過なら理由文字列、未超過なら None。"""
    if state.elapsed_s() - state.elapsed_before_resume > DEEP_RESEARCH_TOTAL_TIME_BUDGET_S:
        return f"時間上限 {DEEP_RESEARCH_TOTAL_TIME_BUDGET_S}s 超過"
    if state.tokens_used - state.tokens_before_resume > DEEP_RESEARCH_TOTAL_TOKEN_BUDGET:
        return f"トークン上限 {DEEP_RESEARCH_TOTAL_TOKEN_BUDGET} 超過"
    return None


def _degraded_report(state: ResearchState) -> str:
    """synthesizer をスキップして searcher 要約を連結した簡易レポート（予算/失敗時）。"""
    state.incomplete_reason = "予算上限または統合処理の失敗により、最終的な統合・検証が未完了です。"
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
