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
- finally で stack.aclose()（MCP SIGTERM）。aclose 失敗時のみ kill_orphan を backstop。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Callable

import discord

from config import (
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
    kill_orphan_mcp_processes,
    mcp_tools_to_anthropic,
    static_mcp_tools_to_anthropic,
)
from services.claude_api.stall import StallWatchdog
from services.claude_api.status import tool_oneline
from services.claude_api.tool_loop import run_agent_loop
from services.job_state import JobPhase, JobState, register_job, unregister_job
from services.mcp_control import (
    filter_enabled_mcp_configs,
    filter_enabled_mcp_keys,
)

from .magi import run_integrator, run_magi_lens
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
from .registry import (
    ResearchAgentDef,
    extract_central_question_and_plan,
    extract_plan,
    run_subagent,
)
from .sources import build_final_report
from .types import LensResult, ResearchState, SubAgentResult

try:
    from services.correlation import new_request_id
except ImportError:  # pragma: no cover
    def new_request_id() -> str:  # type: ignore[misc]
        return ""

log = logging.getLogger(__name__)

_SPIN = "🔬"
_THREAD_NAME_MAX = 90
# synthesizer の transient 失敗（429 等）時のリトライ前 backoff 秒。テストで 0 に上書き。
_SYNTH_RETRY_BACKOFF_S = 3.0

# Phase 0/1 とも MCP プールを1回の /research で1つだけ起動する。同時 /research が走ると
# プールが多重起動して 8GB RAM を圧迫するので、1 件ずつに直列化する（busy は即拒否）。
# asyncio は単一スレッドなので await を挟まずにフラグを立てれば race しない。
_research_busy = False

# アクティブな研究スレッドの channel_id。on_message はここに含まれるスレッドのメッセージを
# スキップする — 明確化の返信や後続メンションが bot.wait_for と on_message に二重処理され、
# 別スレッドでタスクが多重起動するのを防ぐ（bot.wait_for は別リスナーなので、このセットに
# 関係なく明確化返信は届く）。研究完了で外れ、スレッド内の通常チャットは復帰する。
_active_threads: set[int] = set()


def _mark_active(thread_id: int) -> None:
    _active_threads.add(thread_id)


def _unmark_active(thread_id: int) -> None:
    _active_threads.discard(thread_id)


def is_active_research_thread(channel_id: int) -> bool:
    """on_message から呼ぶ: この channel_id がアクティブな研究スレッドなら True。"""
    return channel_id in _active_threads


@dataclass
class DeepResearchDeps:
    """bot-module が所有する依存の注入（claude_tag.TagDeps と同型。sessions は渡さない）。"""

    bot: discord.Client
    send_segmented: Callable        # bot._send_segmented(message, text, files)
    get_http_session: Callable      # bot.get_http_session
    track_task: Callable            # bot._track_task


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
    user: object = None      # 承認ゲートの待ち受け対象（FR-M1）
    deps: object = None      # bot.wait_for アクセス用


class _SearcherView:
    """1 searcher の個別ライブメッセージ（要望1/3）。

    各 searcher が自分のメッセージだけを編集するので、別 rate bucket で並列に更新でき、
    スレッド上で同時進行が見える。on_tool で現在のツール呼び出しを詳細表示する。
    """

    def __init__(self, idx: int, total: int, subq: str, msg) -> None:
        self.idx = idx
        self.total = total
        self.subq = subq
        self.msg = msg
        self.calls = 0
        self._t = 0.0
        self._label = f"[{idx}/{total}] {_short(subq, 60)}"

    async def waiting(self) -> None:
        await _safe_edit(self.msg, f"⏳ {self._label} — 待機中")

    async def searching(self) -> None:
        await _safe_edit(self.msg, f"🔍 {self._label} — 探索中…")

    async def on_tool(self, name: str, tool_input: dict) -> None:
        self.calls += 1
        now = asyncio.get_running_loop().time()
        if now - self._t < 1.2:   # 自メッセージ bucket（~1edit/s）に合わせて throttle
            return
        self._t = now
        await _safe_edit(self.msg, f"🔍 {self._label}\n-# {tool_oneline(name, tool_input)}")

    async def finish(self, ok: bool) -> None:
        if ok:
            await _safe_edit(self.msg, f"✅ {self._label} — 検索 {self.calls} 回")
        else:
            await _safe_edit(self.msg, f"❌ {self._label} — 失敗")


class _AgentView:
    """汎用の個別ライブメッセージ（MAGI 3レンズ等。`_SearcherView` と同型）。

    各エージェントが自分のメッセージだけを編集するので別 rate bucket で並列更新できる。
    `on_tool` で検証検索のツール呼び出しを `tool_oneline` で詳細表示する。
    """

    def __init__(self, label: str, msg, *, emoji: str = "🔍") -> None:
        self.label = label
        self.msg = msg            # ステータス1行（待機/作業中/完了）。詳細推論ログは出さない。
        self.emoji = emoji
        self.calls = 0
        self._t = 0.0

    async def waiting(self) -> None:
        await _safe_edit(self.msg, f"⏳ {self.label} — 待機中")

    async def working(self, note: str = "作業中…") -> None:
        await _safe_edit(self.msg, f"{self.emoji} {self.label} — {note}")

    async def on_tool(self, name: str, tool_input: dict) -> None:
        self.calls += 1
        now = asyncio.get_running_loop().time()
        if now - self._t < 1.2:
            return
        self._t = now
        await _safe_edit(self.msg, f"{self.emoji} {self.label}\n-# {tool_oneline(name, tool_input)}")

    async def finish(self, ok: bool, gist: str = "") -> None:
        if ok:
            tail = f" — {_short(gist, 80)}" if gist else " — 完了"
            await _safe_edit(self.msg, f"✅ {self.label}{tail}")
        else:
            await _safe_edit(self.msg, f"❌ {self.label} — 失敗")


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
            query, str(user.id), limit=MEM0_RECALL_LIMIT, rewrite=True, rerank=True,
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
    return sorted(filter_enabled_mcp_keys(
        k for k in DEEP_RESEARCH_MCP_KEYS if k in MCP_SERVERS
    ))


def has_required_web_backend() -> bool:
    """brave-search または tavily が利用可能か（FR-1 前提条件）。"""
    return bool(filter_enabled_mcp_keys(
        k for k in DEEP_RESEARCH_REQUIRED_WEB_KEYS if k in MCP_SERVERS_WEB
    ))


async def run_deep_research(
    channel: discord.abc.Messageable,
    user: discord.abc.User,
    query: str,
    deps: DeepResearchDeps,
    *,
    skip_clarify: bool = False,
    depth: str = "standard",
) -> None:
    """エントリ。busy なら拒否、そうでなければ直列化して 1 件実行。"""
    global _research_busy
    if _research_busy:
        await _safe_send(channel, "⏳ 別の Deep Research が進行中です。完了してから再度どうぞ。")
        return
    _research_busy = True
    try:
        await _run(channel, user, query, deps, skip_clarify=skip_clarify, depth=depth)
    finally:
        _research_busy = False


async def _run(channel, user, query, deps, *, skip_clarify, depth) -> None:
    state = ResearchState(query=query, clarified_query=query)
    origin = await channel.send(
        f"{_SPIN} **Deep Research**: {_short(query)}\n-# スレッドで進行します。中断はスレッド内で `/abort`。"
    )
    thread = await origin.create_thread(name=_thread_name(query), auto_archive_duration=1440)
    anchor = await thread.send(f"{_SPIN} 準備中…")

    job = JobState(
        rid=new_request_id(),
        channel_id=thread.id,
        user_id=getattr(user, "id", 0),
        task=asyncio.current_task(),
    )
    register_job(job)

    stack = AsyncExitStack()
    ok_done = False
    _mark_active(thread.id)
    try:
        await stack.__aenter__()

        # --- MCP プール（Web + X + 金融 + 学術 を横断利用。playwright/mem0 等は除外）---
        research_cfg = filter_enabled_mcp_configs(
            {k: MCP_SERVERS[k] for k in available_research_keys()}
        )
        if not has_required_web_backend():
            await _safe_edit(anchor, "❌ 一般 Web 検索バックエンド（brave-search / tavily）が未設定です。")
            return
        api_client = await make_api_client(False)
        pool = McpPool(stack=stack, available_configs=research_cfg)
        # static_mcp_schemas.json に在るもの（brave/x-scraper/nikkei/yfj/polymarket）は
        # 「静的広告 + lazy spawn」で、searcher が実際に呼んだ時だけ起動して RAM を節約。
        # 無いもの（tavily/scholarly）だけ eager spawn して live tool を広告する。
        static_keys = [k for k in research_cfg if k in STATIC_MCP_TOOL_SCHEMAS]
        spawn_keys = [k for k in research_cfg if k not in STATIC_MCP_TOOL_SCHEMAS]
        if spawn_keys:
            await pool.spawn_eager(spawn_keys)
        tools_for_api = static_mcp_tools_to_anthropic(static_keys) + mcp_tools_to_anthropic(
            {k: pool.handles[k] for k in spawn_keys if k in pool.handles}
        )
        if not tools_for_api:
            await _safe_edit(anchor, "❌ 検索ツールの起動に失敗しました。後で再試行してください。")
            return

        # --- リクエスト者の Mem0 長期記憶を recall（best-effort）---
        # 本ルートと同じ search_memories_smart を使う。clarify/planner/refine/integrator の
        # briefing に注入されるが、MAGI 3視点（科学/直観/懐疑）には渡さない（独立性を守る）。
        await _recall_user_memories(anchor, user, query, state)

        # --- 明確化（常に1回。skip_clarify でバイパス）---
        job.phase = JobPhase.PREPROCESSING
        if not skip_clarify:
            await _clarify(thread, anchor, user, query, state, deps, api_client)

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
                anchor, f"{_SPIN} 調査中… 検索 {counter['n']} 回 / ソース {len(state.sources)} 件",
            )

        ctx = _RunCtx(
            api_client=api_client, pool=pool, server_locks={},
            tools_for_api=tools_for_api, state=state, thread=thread,
            anchor=anchor, counter=counter, tool_cb=tool_cb,
            user=user, deps=deps,
        )

        if DEEP_RESEARCH_MULTI_AGENT:
            report = await _multi_agent_research(ctx)
        else:
            await _safe_edit(anchor, f"{_SPIN} Web を調査しています…（数分かかることがあります）")
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
                hint = "❌ 調査に失敗しました。Anthropic API の過負荷（overloaded）か " \
                       "外部 MCP の不調の可能性があります。少し時間を置いて再試行してください。"
            else:
                hint = "❌ 調査に失敗しました（有効な結果が得られませんでした）。"
            await _safe_edit(anchor, hint)
        log.info(
            "deep_research done ok=%s multi=%s subqs=%d searches=%d sources=%d tokens=%d elapsed=%.0fs",
            ok_done, DEEP_RESEARCH_MULTI_AGENT, len(state.plan), counter["n"],
            len(state.sources), state.tokens_used, state.elapsed_s(),
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
        try:
            await stack.aclose()
        except Exception:
            log.warning("deep_research MCP stack aclose failed; killing orphans", exc_info=True)
            try:
                kill_orphan_mcp_processes()
            except Exception:
                pass


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
    result = await run_subagent(
        api_client=ctx.api_client, pool=ctx.pool, server_locks=ctx.server_locks,
        defn=defn, briefing=build_research_briefing(ctx.state.clarified_query),
        tools_for_api=ctx.tools_for_api, state=ctx.state, channel_id=ctx.thread.id,
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
            ctx.state.clarified_query, DEEP_RESEARCH_MAX_SUBAGENTS, ctx.state.user_memories,
            min_subagents=DEEP_RESEARCH_MIN_SUBAGENTS,
        )
        if magi else
        build_plan_briefing(
            ctx.state.clarified_query, DEEP_RESEARCH_MAX_SUBAGENTS,
            min_subagents=DEEP_RESEARCH_MIN_SUBAGENTS,
        )
    )
    result = await run_subagent(
        api_client=ctx.api_client, pool=ctx.pool, server_locks=ctx.server_locks,
        defn=defn, briefing=briefing,
        tools_for_api=[], state=ctx.state, channel_id=ctx.thread.id,
    )
    if not result.ok:
        return []
    if magi:
        cq, subqs = extract_central_question_and_plan(result.summary_md, DEEP_RESEARCH_MAX_SUBAGENTS)
        if _meaningful_cq(cq):
            ctx.state.central_question = cq   # Stage1 暫定（Stage2 で研ぎ直す）
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
            len(subqs), DEEP_RESEARCH_MIN_SUBAGENTS, need,
        )
        try:
            comp_defn = ResearchAgentDef(
                agent_type="planner_completion",
                model=DEEP_RESEARCH_LEAD_MODEL,
                system_prompt=PLANNER_SYSTEM,
                max_tool_turns=1, idle_timeout_s=120,
            )
            comp = await run_subagent(
                api_client=ctx.api_client, pool=ctx.pool, server_locks=ctx.server_locks,
                defn=comp_defn,
                briefing=build_plan_completion_briefing(
                    ctx.state.clarified_query, subqs, need,
                ),
                tools_for_api=[], state=ctx.state, channel_id=ctx.thread.id,
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
                    len(extra), len(subqs),
                )
        except Exception:
            log.warning("deep_research: planner completion failed (non-fatal)", exc_info=True)
    return subqs


async def _multi_agent_research(ctx: _RunCtx) -> str | None:
    plan = await _plan(ctx)
    if not plan:
        log.info("deep_research: planner produced no sub-questions; falling back to single-agent")
        await _safe_edit(ctx.anchor, f"{_SPIN} 計画を立てられなかったため単一エージェントで調査します…")
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
            m = await ctx.thread.send(f"⏳ [{i}/{len(plan)}] {_short(subq, 60)} — 待機中")
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

    async def _bounded(view: "_SearcherView", model_override: str | None = None) -> SubAgentResult:
        # 専用 semaphore で実走を律速（claude_semaphore は通らないので subscriber
        # バケットの保護はこれが担う）。各 searcher はツール呼び出しを自分の view に
        # 詳細表示する（tool_invocation_callback=view.on_tool）。
        async with sema:
            await view.searching()
            r = await run_subagent(
                api_client=ctx.api_client, pool=ctx.pool, server_locks=ctx.server_locks,
                defn=searcher_def, briefing=build_search_briefing(view.subq, ctx.state.clarified_query),
                tools_for_api=ctx.tools_for_api, state=ctx.state, channel_id=ctx.thread.id,
                tool_invocation_callback=view.on_tool, model_override=model_override,
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
            *[_bounded(v, model_override) for v in views], return_exceptions=True,
        )
        out: list[SubAgentResult] = []
        for view, res in zip(views, raw):
            if isinstance(res, SubAgentResult):
                out.append(res)
            elif isinstance(res, BaseException):
                out.append(SubAgentResult(subq=view.subq, summary_md="", ok=False, error=str(res)))
        return out

    ctx.state.results = await _collect()
    if not any(r.ok and r.summary_md.strip() for r in ctx.state.results):
        # 全 searcher 失敗（例: searcher モデルが subscription で無効）→ lead model で1回リトライ。
        log.warning(
            "deep_research: all searchers failed on %s; retrying with lead model %s",
            DEEP_RESEARCH_SEARCHER_MODEL, DEEP_RESEARCH_LEAD_MODEL,
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
        log.warning("deep_research over budget (%s); degrading to raw findings", budget_reason)
        await _safe_edit(ctx.anchor, f"{_SPIN} 予算上限のため要約版を作成します…")
        return build_final_report(_degraded_report(ctx.state), ctx.state)

    # MAGI 推論層（事実収集の後段に一段上のレイヤーを積む）。OFF なら旧 synthesizer。
    if DEEP_RESEARCH_MAGI_ENABLED:
        report = await _magi_inference(ctx)
        if report:
            return report
        log.warning("deep_research: MAGI inference produced nothing; degrading to raw findings")
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
        synth = await run_subagent(
            api_client=ctx.api_client, pool=ctx.pool, server_locks=ctx.server_locks,
            defn=synth_def, briefing=build_synthesis_briefing(ctx.state),
            tools_for_api=[], state=ctx.state, channel_id=ctx.thread.id,
        )
        if synth.ok and synth.summary_md.strip():
            break
        if attempt == 0:
            log.warning("deep_research: synthesizer failed (%s); retrying once", synth.error)
            await asyncio.sleep(_SYNTH_RETRY_BACKOFF_S)
    if synth is not None and synth.ok and synth.summary_md.strip():
        return build_final_report(synth.summary_md, ctx.state)
    # 最後の手段: searcher 結果を捨てず要約版で返す。
    log.warning("deep_research: synthesizer failed after retry; degrading to raw findings")
    return build_final_report(_degraded_report(ctx.state), ctx.state)


# ---------------------------------------------------------------------------
# MAGI 推論層（中心的問い研ぎ直し → 承認ゲート → 3レンズ並列 → integrator）
# ---------------------------------------------------------------------------
_MAGI_LENS_ORDER = ("science", "intuition", "skeptic")
_MAGI_LENS_EMOJI = {"science": "🧪", "intuition": "💡", "skeptic": "🤔"}


async def _magi_inference(ctx: _RunCtx) -> str | None:
    """事実収集後の MAGI 推論層。3レンズ独立推論 → integrator の BLUF レポート。"""
    # Stage2: 集めた事実で中心的問いを研ぎ直す（帰納の本丸）。
    await _refine_central_question(ctx)
    # 推論前の承認ゲート（無制限待ち。リアクション/メンション=続行、リプライ=訂正）。
    await _central_question_gate(ctx)

    await _safe_edit(
        ctx.anchor, f"{_SPIN} MAGI 推論中…（科学 / 直観 / 懐疑 が中心的問いに独立回答）",
    )
    views: dict[str, _AgentView] = {}
    for k in _MAGI_LENS_ORDER:
        label = LENS_SPEC[k][0]
        try:
            m = await ctx.thread.send(f"⏳ {label} — 待機中")
        except discord.HTTPException:
            m = None
        views[k] = _AgentView(label, m, emoji=_MAGI_LENS_EMOJI[k])

    sema = asyncio.Semaphore(DEEP_RESEARCH_MAX_PARALLEL)

    async def _one(k: str) -> LensResult:
        # 詳細な推論ログはスレッドに出さない（進捗の1行ステータスのみ。最終統合は integrator）。
        r = await run_magi_lens(ctx, k, views[k], sema)
        await views[k].finish(r.ok, r.stance)
        return r

    raw = await asyncio.gather(*[_one(k) for k in _MAGI_LENS_ORDER], return_exceptions=True)
    results: list[LensResult] = []
    for k, r in zip(_MAGI_LENS_ORDER, raw):
        if isinstance(r, LensResult):
            results.append(r)
        elif isinstance(r, BaseException):
            results.append(LensResult(
                lens_key=k, lens_label=LENS_SPEC[k][0], stance="", body="",
                ok=False, error=str(r),
            ))
    ctx.state.lens_outputs = results
    survivors = sum(1 for r in results if r.ok and r.body.strip())
    if survivors == 0:
        return None

    await _safe_edit(ctx.anchor, f"{_SPIN} 🧩 3視点を統合しています…（合意と相違を抽出）")
    return await run_integrator(ctx)


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
                rest = s.split("：", 1)[-1] if s.startswith(f"{label}：") else s.split(":", 1)[-1]
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
        agent_type="cq_refiner", model=DEEP_RESEARCH_LEAD_MODEL,
        system_prompt=REFINE_CQ_SYSTEM, max_tool_turns=1,
        idle_timeout_s=DEEP_RESEARCH_MAGI_IDLE_TIMEOUT_S,
    )
    res = await run_subagent(
        api_client=ctx.api_client, pool=ctx.pool, server_locks=ctx.server_locks,
        defn=defn,
        briefing=build_refine_cq_briefing(
            ctx.state.central_question, build_evidence_digest(ctx.state),
            ctx.state.user_memories,
        ),
        tools_for_api=[], state=ctx.state, channel_id=ctx.thread.id,
    )
    if res.ok and res.summary_md.strip():
        cq, rationale = _parse_refined_cq(res.summary_md)
        if _meaningful_cq(cq):
            ctx.state.central_question = cq
            ctx.state.central_question_rationale = rationale
        else:
            log.warning(
                "deep_research: refined CQ unusable (%r); keeping prior/clarified", cq,
            )
    if not _meaningful_cq(ctx.state.central_question):
        # planner も refine も実質的な問いを返せなかった → 調査テーマ自体を問いに使う。
        ctx.state.central_question = ctx.state.clarified_query
    log.info(
        "deep_research central_question (refined): %r", ctx.state.central_question[:300],
    )


async def _present_cq(ctx: _RunCtx):
    """研いだ中心的問いを根拠付きで提示。送れたらメッセージ、無理なら None。"""
    cq = ctx.state.central_question
    text = f"🧭 **中心的問い**: {_short(cq, 300)}"
    if ctx.state.central_question_rationale:
        text += f"\n-# 核心: {_short(ctx.state.central_question_rationale, 200)}"
    text += (
        f"\n-# この問いで MAGI 推論へ進みます。{DEEP_RESEARCH_MAGI_APPROVE_HINT} リアクション"
        "／メンションで承認、リプライ（または素のメッセージ）で訂正。`/abort` で中止。"
    )
    try:
        return await ctx.thread.send(text)
    except discord.HTTPException:
        return None


async def _await_reaction_or_reply(bot, uid, thread_id: int, target_msg):
    """``target_msg`` へのリアクション or thread 内の本人メッセージを **無制限 OR 待ち**。

    戻り: ``("reaction", None)`` か ``("message", msg)``。中心的問いゲートと明確化の両方で
    共用する（時間制限なし。中断は /abort のタスクキャンセルで CancelledError 伝播）。
    """
    def _rcheck(payload) -> bool:
        return (
            getattr(payload, "message_id", None) == getattr(target_msg, "id", None)
            and getattr(payload, "user_id", None) == uid
        )

    def _mcheck(m) -> bool:
        return (
            getattr(m.channel, "id", None) == thread_id
            and getattr(m.author, "id", None) == uid
            and not getattr(m.author, "bot", False)
        )

    reaction_task = asyncio.ensure_future(bot.wait_for("raw_reaction_add", check=_rcheck))
    message_task = asyncio.ensure_future(bot.wait_for("message", check=_mcheck))
    try:
        done, _pending = await asyncio.wait(
            {reaction_task, message_task}, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (reaction_task, message_task):
            if not t.done():
                t.cancel()
                # cancel() だけだと pending task が浮いたまま GC 任せになり
                # "Task was destroyed but it is pending!" になる。回収まで待つ。
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - 回収時の例外は握る
                    pass
    if reaction_task in done and not reaction_task.cancelled():
        return ("reaction", None)
    return ("message", message_task.result())


def _mentions_bot(bot, msg) -> bool:
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    return any(getattr(u, "id", None) == bot_id for u in (getattr(msg, "mentions", None) or []))


async def _await_gate_response(ctx: _RunCtx, gate_msg) -> tuple[str, str]:
    """ゲートの反応を無制限待ち。戻り: ("approve","") か ("correct", 訂正本文)。

    Discord の「返信」機能は元発言者（=bot）を自動メンションするため、`_mentions_bot`
    だけで approve 判定すると **リプライ=訂正** が approve に化ける。`msg.reference` が
    立っていればリプライ＝訂正として扱い、純粋な @ メンション（reference 無し）だけを
    approve とみなす。
    """
    kind, msg = await _await_reaction_or_reply(
        ctx.deps.bot, getattr(ctx.user, "id", None), ctx.thread.id, gate_msg,
    )
    if kind == "reaction":
        return ("approve", "")
    is_reply = getattr(getattr(msg, "reference", None), "message_id", None) is not None
    if not is_reply and _mentions_bot(ctx.deps.bot, msg):
        return ("approve", "")
    return ("correct", (getattr(msg, "content", "") or "").strip())


async def _central_question_gate(ctx: _RunCtx) -> None:
    """中心的問いの承認ゲート（FR-M1）。承認まで待つが、訂正は上限付き。

    訂正が ``DEEP_RESEARCH_MAX_CQ_CORRECTIONS`` 回に達したら最後に研ぎ直した
    問いで自動続行する。無上限だと訂正を送り続けるだけで ``_research_busy`` を
    任意時間占有できてしまう。bot/user 無しなら素通り。
    """
    if not (ctx.deps and getattr(ctx.deps, "bot", None) is not None and ctx.user is not None):
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
        except Exception:
            log.warning("deep_research CQ gate wait failed (non-fatal); proceeding", exc_info=True)
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


async def _clarify(thread, anchor, user, query, state, deps, api_client) -> None:
    """明確化質問を生成→投稿→**無制限**で返信待ち（中心的問いゲートと同じ運用）。

    返信＝回答を畳み込む / リアクション・メンション＝確認なしで既定スコープ続行 /
    `/abort`＝中断。時間制限は設けない（じっくり答えられるように）。
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
        "**調査前の確認です**（時間制限はありません。答えが決まったら返信してください。"
        "確認なしで進める場合はこのメッセージにリアクション／メンション、中断は `/abort`）\n"
        + questions[:1800]
    )

    try:
        kind, msg = await _await_reaction_or_reply(
            deps.bot, getattr(user, "id", None), thread.id, q_msg,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("deep_research clarify wait failed (non-fatal); proceeding", exc_info=True)
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
    t = (text or "").strip().replace("\n", " ")
    return t if len(t) <= n else t[:n] + "…"


def _thread_name(query: str) -> str:
    base = (query or "").strip().replace("\n", " ")
    if not base:
        base = "deep research"
    return (f"🔬 {base}")[:_THREAD_NAME_MAX] or "deep research"


async def _safe_edit(msg: discord.Message | None, content: str) -> None:
    if msg is None:
        return
    try:
        await msg.edit(content=content[:1990])
    except discord.HTTPException:
        pass


async def _safe_send(channel, content: str) -> None:
    try:
        await channel.send(content)
    except discord.HTTPException:
        pass
