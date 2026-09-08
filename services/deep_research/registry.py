"""Deep Research のサブエージェント定義と実行（ADR-0028）.

公式 Claude Code CLI の AgentDefinition 構造（宣言的定義 + fresh-context + bounded
result）を harness に移植したもの。サブエージェントは `run_agent_loop`（tool_loop.py）
を直接呼ぶ — これにより subscriber-shape の request（system 4-block / betas /
output_config / cch hook）を再利用しつつ、`claude_semaphore`（entry.py 側）と
session/JSONL を通らない（FR-2 不変条件）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from config import (
    DEEP_RESEARCH_MAX_TOOL_RESULT_CHARS,
    DEEP_RESEARCH_SEARCHER_IDLE_TIMEOUT_S,
    DEEP_RESEARCH_SUBAGENT_DISABLE_CONTEXT_1M,
    DEEP_RESEARCH_SUBAGENT_INPUT_BUDGET_CHARS,
    TOOL_CALL_TIMEOUT_S,
)
from services.claude_api.client import MODEL_BETAS
from services.claude_api.mcp_client import _parse_qualified_tool_name
from services.claude_api.stall import StallWatchdog
from services.claude_api.tool_loop import run_agent_loop

from .sources import parse_sources
from .types import ResearchState, SubAgentResult

log = logging.getLogger(__name__)

# CLI の Agent tool maxResultSizeChars 準拠: サブエージェントの最終要約を有界化し、
# 親（orchestrator）の context が肥大しないようにする。
_MAX_RESULT_CHARS = 100_000

# Anthropic API の一時的な過負荷（HTTP 529 overloaded_error / 5xx）と
# 一時的接続例外を最大 N 回・指数バックオフでリトライする。本ルート（call_with_tools）と
# 同じ堅牢性をサブエージェントにも持たせるための backstop。
# rid 5fcf8755（2026-06-24 22:21）: clarify→planner→researcher が全部 overloaded で
# 43秒で done ok=False になり「バグった」ように見えた、を防ぐ。
_TRANSIENT_RETRY_MAX = 4
_TRANSIENT_BACKOFF_BASE_S = 4.0


def _is_transient_api_error(exc: BaseException) -> bool:
    """overloaded / 5xx / 一時的接続エラーなら True（リトライ対象）。429 や 400 は False。

    401（authentication_error）は OAuth token の expire でも起きるため、ここでは True を
    返してリトライさせる。リトライの先頭で ``ensure_valid_token()`` を再呼び出しすると、
    expire したトークンが refresh されて 2 回目で復活する（rid 9e974424 で観測した
    integrator 401 → degrade の事故対策、haroin 2026-06-26）。
    """
    # anthropic.APIStatusError を最優先で見る（status_code を持つ）。
    msg = str(exc) if exc else ""
    sc = getattr(exc, "status_code", None)
    if isinstance(sc, int):
        if sc == 529:
            return True
        if sc == 401:
            return True   # OAuth token expire の可能性。refresh 後に再試行
        if 500 <= sc < 600:
            return True
    if "overloaded_error" in msg or "Overloaded" in msg:
        return True
    if "authentication_error" in msg or "Invalid authentication credentials" in msg:
        return True       # 同上（status_code が取れない/ラップされてる場合）
    cname = type(exc).__name__
    if cname in ("APIConnectionError", "APITimeoutError"):
        return True
    return False


_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_central_question_and_plan(text: str, limit: int) -> tuple[str, list[str]]:
    """MAGI planner 出力（理想は ``{"central_question": ..., "subquestions": [...]}``）から
    暫定の中心的問いとサブ質問リストを頑健に抽出する。

    JSON object を最初に試し、ダメなら ``extract_plan`` で配列/箇条書きにフォールバック
    （その場合 central_question は空文字＝Stage2 研ぎ直しで初めて埋まる）。
    """
    cq = ""
    subqs: list[str] = []
    m = _JSON_OBJ_RE.search(text or "")
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                cq = str(data.get("central_question", "") or "").strip()
                raw = data.get("subquestions") or data.get("sub_questions") or []
                if isinstance(raw, list):
                    subqs = [str(x).strip() for x in raw if str(x).strip()][:limit]
        except (json.JSONDecodeError, ValueError):
            pass
    if not subqs:
        subqs = extract_plan(text, limit)
    return cq, subqs


def extract_plan(text: str, limit: int) -> list[str]:
    """planner 出力（理想は JSON 配列）からサブ質問リストを頑健に抽出する。

    コードフェンスや前置きが混ざっても最初の ``[...]`` を拾って JSON パースする。
    失敗時は行ベース（箇条書き / 番号付き）でフォールバック。最大 ``limit`` 件。
    """
    if not text:
        return []
    candidates: list[str] = []
    m = _JSON_ARRAY_RE.search(text)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                candidates = [str(x).strip() for x in data if str(x).strip()]
        except (json.JSONDecodeError, ValueError):
            candidates = []
    if not candidates:
        # フォールバック: 箇条書き / 番号付き行を拾う
        for line in text.splitlines():
            s = line.strip()
            s = re.sub(r"^[-*0-9.\)\s]+", "", s).strip()
            if len(s) >= 6:
                candidates.append(s)
    # dedup（順序保持）
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
        if len(out) >= limit:
            break
    return out


@dataclass
class ResearchAgentDef:
    """CLI の AgentDefinition に相当する宣言的サブエージェント定義。"""

    agent_type: str          # "researcher" | "searcher" | "synthesizer" | "magi_lead" | ...
    model: str               # CLI: definition frontmatter model
    system_prompt: str       # CLI: getSystemPrompt()
    max_tool_turns: int      # 検索ループの turn 上限
    idle_timeout_s: float = DEEP_RESEARCH_SEARCHER_IDLE_TIMEOUT_S
    # 思考力の per-request override（effort=max など）。None なら client 既定の
    # OUTPUT_CONFIG を使う。run_agent_loop に output_config_override として渡る。
    effort: str | None = None


async def _guarded_call_tool(pool, server_locks: dict, qualified_name: str, args: dict) -> str:
    """MCP ツール呼び出しを server-key 単位の lock + wall-clock timeout で保護する。

    `McpPool.call_tool`（mcp_client.py:356）は spawn ロックしか持たないため、並列
    searcher が同一 server の stdio session を同時に叩くと protocol が混線しうる。
    server-key ごとに 1 本の Lock で直列化し、`wait_for` でキャンセル不能ハングを防ぐ。
    """
    server, _ = _parse_qualified_tool_name(qualified_name)
    lock = server_locks.setdefault(server, asyncio.Lock())
    async with lock:
        return await asyncio.wait_for(
            pool.call_tool(qualified_name, args or {}), timeout=TOOL_CALL_TIMEOUT_S,
        )


def _make_tool_callbacks(pool, server_locks: dict, tools_for_api: list[dict], state: ResearchState):
    """run_agent_loop 用の (can_use_tool, execute_tool) を生成。

    - can_use_tool: 広告した web ツールのみ許可（searcher に Bash/Edit 等は与えない）。
    - execute_tool: 保護付き MCP 呼び出し → 生戻り値からソースを機械抽出 → tool_result 返却。
    """
    allowed = {t.get("name") for t in (tools_for_api or []) if t.get("name")}

    def can_use_tool(name, tool_input):
        if name in allowed:
            return True, ""
        return False, f"Deep Research では次のツールのみ使用できます: {sorted(allowed)}"

    async def execute_tool(name, tool_input):
        try:
            raw = await _guarded_call_tool(pool, server_locks, name, tool_input)
        except asyncio.TimeoutError:
            return f"<tool_use_error>MCP call '{name}' timed out after {TOOL_CALL_TIMEOUT_S}s</tool_use_error>"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface as tool error, never crash the loop
            return f"<tool_use_error>{type(exc).__name__}: {exc}</tool_use_error>"
        try:
            parse_sources(name, raw, state)   # 全文からソース抽出（URL は失わない）
        except Exception:
            log.warning("parse_sources failed for %s", name, exc_info=True)
        return _bound_tool_result(raw)

    return can_use_tool, execute_tool


def _compact_briefing(system_prompt: str, briefing: str, budget_chars: int) -> str:
    """system + briefing の合計を上界 ``budget_chars`` 以内に収める。

    system_prompt は人格/タスク仕様なので削れない（CLAUDE.md 等が入る）。briefing の
    末尾を切って ``…[deep_research: 入力上界 N tokens 相当に収めるため M 文字省略]`` の
    マーカーを残す。冒頭は中心的問い／必須指示があるので必ず残す。これは context-1m を
    切った subagent が 200K tokens を超えそうな時の最後の防波堤。
    """
    total = len(system_prompt) + len(briefing)
    if total <= budget_chars:
        return briefing
    # system_prompt は減らせない（人格と Discord 出力ルールを含むため）。
    # briefing に割り当てられる予算を計算し、超過分を末尾から削る。
    avail = max(0, budget_chars - len(system_prompt))
    if avail <= 0:
        log.warning(
            "deep_research compact_briefing: system_prompt alone exceeds budget "
            "(sys=%d, budget=%d). briefing forced to marker only.",
            len(system_prompt), budget_chars,
        )
        return f"[deep_research: 入力上界超過のため briefing 全体を省略 ({len(briefing)} chars dropped)]"
    dropped = len(briefing) - avail
    log.warning(
        "deep_research compact_briefing: total=%d > budget=%d, dropping %d chars from briefing",
        total, budget_chars, dropped,
    )
    return (
        briefing[:avail]
        + f"\n…[deep_research: 入力上界に収めるため {dropped} 文字を省略しました]"
    )


def _bound_tool_result(raw: str) -> str:
    """モデルの context に戻す tool 結果を有界化する（200k 超→long-context 課金 429 を防ぐ）。

    ソース URL は ``parse_sources`` が呼び出し側で全文から抽出済みなので、ここで切っても
    引用の根拠は失われない。fresh-context のサブエージェントが圧縮機構を通らないぶんを補う。
    """
    if not raw:
        return raw or ""
    if len(raw) <= DEEP_RESEARCH_MAX_TOOL_RESULT_CHARS:
        return raw
    dropped = len(raw) - DEEP_RESEARCH_MAX_TOOL_RESULT_CHARS
    return (
        raw[:DEEP_RESEARCH_MAX_TOOL_RESULT_CHARS]
        + f"\n…[deep_research: ツール結果が長いため {dropped} 文字を省略しました"
        "（ソースは抽出済み・引用可能）]"
    )


async def run_subagent(
    *,
    api_client,
    pool,
    server_locks: dict,
    defn: ResearchAgentDef,
    briefing: str,
    tools_for_api: list[dict],
    state: ResearchState,
    channel_id: int,
    status_callback=None,
    tool_invocation_callback=None,
    text_stream_callback=None,
    model_override: str | None = None,
) -> SubAgentResult:
    """1 サブエージェントを fresh-context で実行し、最終要約のみ返す（CLI 準拠）。

    `run_agent_loop` を `history=[]` で呼ぶため session/JSONL を一切触らない。例外は
    内部で握って `ok=False` の結果に変換する（部分成功 synthesis を可能にする）。
    `CancelledError` のみ伝播（/abort 用）。
    """
    can_use_tool, execute_tool = _make_tool_callbacks(pool, server_locks, tools_for_api, state)
    model = model_override or defn.model
    # effort=max 等の思考力 override（MAGI lead / integrator 用）。None は client 既定。
    output_config_override = {"effort": defn.effort} if defn.effort else None
    # context window バウンド: subagent では context-1m beta を切って 200K 以内に。
    # long-context credits の枯渇による 429 を防ぐ（rid 9e974424 対策）。
    subagent_betas = [b for b in MODEL_BETAS if not (
        DEEP_RESEARCH_SUBAGENT_DISABLE_CONTEXT_1M and b.startswith("context-1m")
    )]
    # briefing を上界文字数に圧縮（system + briefing 合計で 540K chars ≒ 180K tokens を超えない）。
    briefing = _compact_briefing(
        defn.system_prompt, briefing, DEEP_RESEARCH_SUBAGENT_INPUT_BUDGET_CHARS,
    )
    from services.codex_app_server import auxiliary
    if auxiliary.enabled():
        async def execute_codex_tool(name, arguments):
            allowed, reason = can_use_tool(name, arguments)
            if not allowed:
                return reason
            if tool_invocation_callback:
                await tool_invocation_callback(name, arguments)
            return await execute_tool(name, arguments)
        try:
            result = await auxiliary.run_task(
                prompt=briefing, instructions=defn.system_prompt,
                tools=tools_for_api, execute_tool=execute_codex_tool,
                effort=defn.effort or 'high', timeout_s=defn.idle_timeout_s,
                max_tool_calls=defn.max_tool_turns,
                text_stream_callback=text_stream_callback,
            )
            state.add_usage(auxiliary.normalized_usage(result))
            text = result.final_text.strip()
            return SubAgentResult(subq=briefing, summary_md=text[:_MAX_RESULT_CHARS],
                                  cited_sids=[], ok=bool(text), error=None if text else 'empty result')
        except Exception as exc:
            log.warning('Codex research helper failed: %s', type(exc).__name__)
            return SubAgentResult(subq=briefing, summary_md='', ok=False, error=type(exc).__name__)
    # 一時的な過負荷（overloaded_error / 5xx）はバックオフでリトライ。本ルートと同じ堅牢性。
    last_exc: BaseException | None = None
    result = None
    for attempt in range(1, _TRANSIENT_RETRY_MAX + 1):
        # OAuth トークンを **毎試行の直前** に確認・必要なら refresh する
        # （haroin 2026-06-26 rid 9e974424）：長時間 run（>15分）の途中で token が
        # expire し、integrator が古い token を再利用して 401 で死ぬ事故があった。
        # ループ先頭で refresh しておくと、1 回目が 401 でも 2 回目は新トークンで復活。
        try:
            from services.oauth_manager import ensure_valid_token
            await ensure_valid_token()
        except ImportError:
            pass
        except Exception:  # noqa: BLE001
            log.warning("ensure_valid_token before subagent failed (non-fatal)", exc_info=True)
        try:
            async with StallWatchdog(
                channel_id, status_callback, idle_timeout_s=defn.idle_timeout_s,
            ) as watchdog:
                result = await run_agent_loop(
                    briefing,
                    channel_id,
                    history=[],
                    system_prompt=defn.system_prompt,
                    tools_for_api=tools_for_api,
                    can_use_tool=can_use_tool,
                    execute_tool=execute_tool,
                    status_callback=status_callback,
                    tool_invocation_callback=tool_invocation_callback,
                    text_stream_callback=text_stream_callback,
                    max_turns=defn.max_tool_turns,
                    budget_role="subagent",  # ADR-0036
                    stall_watchdog=watchdog,
                    api_client=api_client,
                    model=model,
                    extra_betas=subagent_betas,
                    output_config_override=output_config_override,
                )
            break  # success
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < _TRANSIENT_RETRY_MAX and _is_transient_api_error(exc):
                backoff = _TRANSIENT_BACKOFF_BASE_S * (2 ** (attempt - 1))
                log.warning(
                    "deep_research subagent (%s) transient error attempt %d/%d, retrying in %.1fs: %s",
                    defn.agent_type, attempt, _TRANSIENT_RETRY_MAX, backoff, exc,
                )
                await asyncio.sleep(backoff)
                continue
            log.exception("deep_research subagent (%s) failed", defn.agent_type)
            return SubAgentResult(subq=briefing, summary_md="", ok=False, error=f"{type(exc).__name__}: {exc}")
    if result is None:
        return SubAgentResult(
            subq=briefing, summary_md="", ok=False,
            error=f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown",
        )

    state.add_usage(getattr(result, "usage_total", None))
    text = (getattr(result, "text", "") or "").strip()
    return SubAgentResult(
        subq=briefing,
        summary_md=text[:_MAX_RESULT_CHARS],
        cited_sids=[],
        ok=bool(text),
        error=None if text else "empty result",
    )
