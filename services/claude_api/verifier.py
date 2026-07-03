"""Draft verifier + judge-lite（熟考 P2）。

deliberate tier の最終ドラフトを fresh-context の検証者が evidence index と
突き合わせ、根拠なき断定・数値不一致・依頼との不整合を洗う。検証者は
run_agent_loop のミニループ（tools=[Read] のみ、cwd を当該リクエストの
evidence ディレクトリに jail）で動くので、蒸留ノートで怪しい主張は原文まで
潜って照合できる。

fail-open 原則: verifier 自身の失敗（parse 不能・timeout・API 障害）は
`Verdict(ok=True, degraded=True)` — 検証層の故障で回答を握り潰さない。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from config import (
    DELIB_JUDGE_MIN_CHARS,
    DELIB_JUDGE_MODEL,
    DELIB_VERIFIER_MAX_TURNS,
)

log = logging.getLogger(__name__)


@dataclass
class VerifierIssue:
    claim: str
    problem: str
    severity: str = "medium"  # high | medium | low


@dataclass
class Verdict:
    ok: bool
    issues: list[VerifierIssue] = field(default_factory=list)
    raw: str = ""
    degraded: bool = False


_VERIFIER_SYSTEM = """あなたは AI アシスタントの回答を検証する監査者です。回答ドラフトの事実主張を、添付の evidence ledger（実行された tool とその結果の台帳）と突き合わせてください。

指摘してよいのは以下のみ:
1. 数値・日付・URL・固有名詞が ledger の記録と食い違う
2. ledger にも一般常識にも根拠が無い断定
3. **推論の事実化**: ソースに書かれていない対応付け・因果・分類を、推論だと
   明示せずに事実として提示している（もっともらしくても ledger に無ければ issue）
4. ユーザーの依頼と回答の不整合（聞かれたことに答えていない・指定形式違反）

文体・言い回し・構成の好みは指摘禁止。蒸留ノート（distilled）だけでは判断できない主張は、ledger の raw= パスを Read で開いて原文照合すること（Read できるのは evidence ディレクトリ配下のみ）。

最後に必ず生 JSON のみで出力（コードフェンス不要）:
{"pass": true/false, "issues": [{"claim": "問題のある記述", "problem": "何が問題か", "severity": "high|medium|low"}]}
issues が空なら pass=true。軽微な文体差は issues に入れない。"""


def _extract_verdict_json(text: str) -> dict | None:
    text = re.sub(r"```(?:json)?|```", "", text)
    # 末尾に近い JSON object を拾う（前置きテキスト耐性）
    matches = list(re.finditer(r"\{.*\}", text, re.DOTALL))
    for m in reversed(matches):
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and "pass" in data:
                return data
        except json.JSONDecodeError:
            continue
    return None


def _parse_verdict(raw: str) -> Verdict:
    data = _extract_verdict_json(raw or "")
    if data is None:
        return Verdict(ok=True, degraded=True, raw=raw or "")
    issues = []
    for it in data.get("issues") or []:
        if isinstance(it, dict):
            issues.append(VerifierIssue(
                claim=str(it.get("claim", ""))[:300],
                problem=str(it.get("problem", ""))[:300],
                severity=str(it.get("severity", "medium")),
            ))
    return Verdict(ok=bool(data.get("pass", True)) and not issues,
                   issues=issues, raw=raw or "")


async def verify_draft(
    *,
    user_request: str,
    draft: str,
    evidence_digest: str,
    evidence_root: str | None,
    api_client,
    channel_id: int,
    model: str,
    extra_betas: list[str],
    status_callback=None,
) -> Verdict:
    """fresh-context 検証。失敗は常に fail-open（ok=True, degraded=True）。"""
    try:
        from services.claude_api.restricted import make_can_use_tool
        from services.claude_api.stall import StallWatchdog
        from services.claude_api.tool_loop import run_agent_loop
        from services.claude_api.tools import (
            BUILTIN_TOOLS,
            ToolContext,
            execute_builtin_tool,
        )

        read_def = BUILTIN_TOOLS["Read"]
        tools = [{
            "name": "Read",
            "description": read_def.description,
            "input_schema": read_def.input_schema,
        }]
        can_use = make_can_use_tool(restricted=False, allowed_tools=["Read"])
        # Read の可動域を当該リクエストの evidence ディレクトリに封じ込める。
        # restricted=True の cwd jail（tools.py）を流用した defense-in-depth。
        vctx = ToolContext(
            cwd=evidence_root or "/nonexistent-evidence-dir",
            restricted=True, channel_id=channel_id,
        )

        async def _exec(name: str, args: dict) -> str:
            return await execute_builtin_tool(name, args, vctx)

        briefing = (
            f"# ユーザーの依頼\n{user_request[:4000]}\n\n"
            f"# 回答ドラフト\n{draft[:12000]}\n\n"
            f"# evidence ledger\n{evidence_digest}\n\n"
            "上記を検証し、指示どおり JSON で verdict を返してください。"
        )
        # context-1m は不要（verifier 入力は小さい）— subagent と同じ絞り
        betas = [b for b in extra_betas if not b.startswith("context-1m")]
        async with StallWatchdog(
            channel_id, None, idle_timeout_s=180,
        ) as watchdog:
            result = await run_agent_loop(
                briefing,
                channel_id,
                history=[],
                system_prompt=_VERIFIER_SYSTEM,
                tools_for_api=tools,
                can_use_tool=can_use,
                execute_tool=_exec,
                status_callback=status_callback,
                max_turns=DELIB_VERIFIER_MAX_TURNS,
                stall_watchdog=watchdog,
                api_client=api_client,
                model=model,
                extra_betas=betas,
            )
        verdict = _parse_verdict(result.text)
        log.info(
            "[ch=%d] delib_verify ok=%s issues=%d degraded=%s",
            channel_id, verdict.ok, len(verdict.issues), verdict.degraded,
        )
        return verdict
    except Exception as exc:  # noqa: BLE001 - 検証層の故障で回答を殺さない
        log.warning("delib_verify failed (fail-open): %s", exc)
        return Verdict(ok=True, degraded=True, raw=f"{type(exc).__name__}: {exc}")


def build_revise_prompt(verdict: Verdict) -> str:
    lines = ["自動検証で以下の指摘が出ました。妥当な指摘だけを反映して回答全体を書き直してください。"]
    for i, it in enumerate(verdict.issues[:8], 1):
        lines.append(f"{i}. [{it.severity}] {it.claim} — {it.problem}")
    lines.append(
        "指摘が事実誤認だと判断した場合、その箇所は変えなくてよい（理由を回答に含める必要はない）。"
        "文体・構成は元のまま、修正後の回答全文のみを出力すること。"
    )
    return "\n".join(lines)


def build_caveat(verdict: Verdict) -> str:
    top = verdict.issues[0] if verdict.issues else None
    head = f"（最重要: {top.claim[:60]}）" if top else ""
    return (
        f"-# ⚠️ 自動検証で {len(verdict.issues)} 件の未解決指摘があります{head}"
    )


# ---------------------------------------------------------------------------
# judge-lite — chat tier の軽量品質ゲート（Haiku 単発・Read なし）
# ---------------------------------------------------------------------------

_JUDGE_TASK = (
    "You verify an assistant answer against its tool evidence. JSON only."
)

_JUDGE_PROMPT = """以下の回答に、evidence ledger にも一般常識にも根拠の無い事実断定（数値・固有名詞・出来事）が含まれていないか検査してください。文体は検査対象外。

# ユーザーの依頼
{request}

# 回答
{draft}

# evidence ledger
{digest}

生 JSON のみ:
{{"pass": true/false, "issues": [{{"claim": "...", "problem": "...", "severity": "high"}}]}}"""


def should_run_judge(
    response_text: str, evidence, *, tier: str | None, restricted: bool,
) -> bool:
    if restricted or tier == "deliberate":
        return False
    if evidence is None or not getattr(evidence, "records", None):
        return False  # tool を使っていない応答は対象外（雑談を巻き込まない）
    return len(response_text or "") >= DELIB_JUDGE_MIN_CHARS


async def judge_lite(
    *, user_request: str, draft: str, evidence_digest: str, channel_id: int,
) -> Verdict:
    try:
        from services.mem0.client import get_anthropic_client
        from services.mem0.cli_compat import cli_messages_create, extract_text

        client = await get_anthropic_client()
        if client is None:
            return Verdict(ok=True, degraded=True)
        resp = await cli_messages_create(
            client, model=DELIB_JUDGE_MODEL, max_tokens=800,
            user_message=_JUDGE_PROMPT.format(
                request=user_request[:2000], draft=draft[:8000],
                digest=evidence_digest[:6000],
            ),
            task_prompt=_JUDGE_TASK,
        )
        verdict = _parse_verdict(extract_text(resp))
        log.info(
            "[ch=%d] delib_judge_lite ok=%s issues=%d",
            channel_id, verdict.ok, len(verdict.issues),
        )
        return verdict
    except Exception as exc:  # noqa: BLE001
        log.warning("judge_lite failed (fail-open): %s", exc)
        return Verdict(ok=True, degraded=True)
