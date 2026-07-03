"""tool loop 精度評価ランナー（docs/2026-07-02-tool-loop-eval-design.md）。

usage:
  .venv/bin/python scripts/eval_tool_loop.py --suite smoke --arch baseline --k 3
  --arch baseline  : DELIB_* 全OFF（現行アーキテクチャ）
  --arch candidate : DELIB_EVIDENCE/DISTILL 等を ON（--flags で上書き可）
  --only A1,B1     : ケース絞り込み
  --no-judge       : 自動チェックのみ（高速検証用）

ケースは worker サブプロセス（scripts/eval_case_worker.py）で実行する。
config が import 時凍結のため、フラグ差し替えは worker の env でのみ効く。
結果は .state/eval/{run_id}/ に report.md + 全 transcript + judge 生入出力を保存。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/discord-bot")

BOT_DIR = Path("/home/ubuntu/discord-bot")
CASES_DIR = BOT_DIR / "tests" / "fixtures" / "tool_loop_eval" / "cases"
EVAL_OUT_DIR = BOT_DIR / ".state" / "eval"
WORKER = BOT_DIR / "scripts" / "eval_case_worker.py"
PY = BOT_DIR / ".venv" / "bin" / "python"

# candidate アーキテクチャで ON にする既定フラグ（Phase 進行に合わせて拡張）
CANDIDATE_FLAGS = {
    "DELIB_EVIDENCE_ENABLED": "true",
    "DELIB_DISTILL_ENABLED": "true",
}

JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "claude-opus-4-8")


# ---------------------------------------------------------------------------
# 自動チェック
# ---------------------------------------------------------------------------

def _first_json_object(text: str):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def run_auto_checks(case: dict, result: dict) -> list[dict]:
    answer = result.get("answer") or ""
    tools = result.get("tool_calls") or []
    out = []
    for chk in case.get("auto_checks") or []:
        ctype = chk["type"]
        ok, detail = False, ""
        if ctype == "contains_all":
            missing = [v for v in chk["values"] if v not in answer]
            ok, detail = not missing, f"missing={missing}" if missing else ""
        elif ctype == "contains_none":
            found = [v for v in chk["values"] if v in answer]
            ok, detail = not found, f"found={found}" if found else ""
        elif ctype == "json_object":
            obj = _first_json_object(answer)
            if obj is None:
                ok, detail = False, "no parseable JSON object"
            else:
                missing = [k for k in chk.get("required_keys", []) if k not in obj]
                ok, detail = not missing, f"missing_keys={missing}" if missing else ""
        elif ctype == "tool_used":
            n = sum(1 for t in tools if chk["value"] in t["name"])
            ok = n >= chk.get("min_count", 1)
            detail = f"count={n}"
        elif ctype == "tool_used_any":
            ok = any(any(v in t["name"] for v in chk["values"]) for t in tools)
            detail = f"tools={[t['name'] for t in tools][:6]}"
        elif ctype == "no_tool_use":
            ok = not tools
            detail = f"tools={[t['name'] for t in tools][:6]}" if tools else ""
        elif ctype == "max_chars":
            ok = len(answer) <= chk["value"]
            detail = f"len={len(answer)}"
        else:
            detail = f"unknown check type {ctype!r}"
        out.append({"type": ctype, "ok": bool(ok), "detail": detail})
    return out


# ---------------------------------------------------------------------------
# judge（rubric 二値基準 + fabrication 検査）
# ---------------------------------------------------------------------------

def _transcript_digest(result: dict, cap: int = 12000) -> str:
    parts = []
    for t in result.get("tool_calls") or []:
        parts.append(f"[call] {t['name']} input={t['input']}")
    # tool_result の中身（api_messages の user フレーム）も digest に含める
    for row in result.get("transcript_rows") or []:
        for msg in row.get("api_messages") or []:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)[:800]
                    parts.append(f"[result] {str(text)[:800]}")
    digest = "\n".join(parts)
    return digest[:cap]


async def judge_one(case: dict, result: dict) -> dict:
    """rubric criteria の yes/no + fabrication 主張列挙。fail は保守的に扱う。"""
    from services.mem0.client import get_anthropic_client
    from services.mem0.cli_compat import cli_messages_create, extract_text

    rubric = case.get("rubric") or []
    fab = bool(case.get("fabrication_check"))
    if not rubric and not fab:
        return {"criteria": [], "fabrications": [], "degraded": False}

    criteria_block = "\n".join(f"{i+1}. {c}" for i, c in enumerate(rubric)) or "(なし)"
    digest = _transcript_digest(result)
    prompt = f"""あなたはAIアシスタントの回答品質の採点者です。以下を読み、JSON のみで答えてください。

# ユーザーの依頼
{case['prompt']}

# アシスタントの回答
{result.get('answer') or '(空)'}

# アシスタントが実行したツールとその結果（抜粋）
{digest or '(ツール未使用)'}

# 採点基準（各項目を独立に yes/no で判定）
{criteria_block}

# 追加検査
{"回答中の事実主張のうち、上のツール結果にも一般常識にも根拠が無い『捏造の疑いがある断定』を列挙してください（無ければ空配列）。" if fab else "fabrications は常に空配列でよい。"}

出力（生JSONのみ・コードフェンス不要）:
{{"criteria": [true/false, ...], "fabrications": ["主張...", ...]}}"""

    try:
        client = await get_anthropic_client()
        if client is None:
            return {"criteria": [None] * len(rubric), "fabrications": [],
                    "degraded": True, "raw": "no client"}
        resp = await cli_messages_create(
            client, model=JUDGE_MODEL, max_tokens=1200,
            user_message=prompt,
            task_prompt="You grade assistant answers against binary rubric criteria. JSON only.",
        )
        raw = extract_text(resp).strip()
        raw2 = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL)
        data = json.loads(raw2)
        criteria = [bool(x) for x in (data.get("criteria") or [])]
        # 基準数と合わない場合は不足分を None（不明）で埋める
        criteria = (criteria + [None] * len(rubric))[:len(rubric)]
        return {"criteria": criteria,
                "fabrications": [str(x) for x in (data.get("fabrications") or [])],
                "degraded": False, "raw": raw}
    except Exception as exc:  # noqa: BLE001
        return {"criteria": [None] * len(rubric), "fabrications": [],
                "degraded": True, "raw": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def load_suite(suite: str, only: set[str] | None) -> list[dict]:
    import yaml
    with open(CASES_DIR / f"{suite}.yaml", encoding="utf-8") as f:
        cases = yaml.safe_load(f)
    if only:
        cases = [c for c in cases if c["id"] in only]
    return cases


def worker_env(arch: str, extra_flags: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env["EVAL_MCP_ENABLED"] = "true"
    # baseline は DELIB_* を明示 OFF（親環境の残留を遮断）
    for k in CANDIDATE_FLAGS:
        env[k] = "false"
    if arch == "candidate":
        env.update(CANDIDATE_FLAGS)
    env.update(extra_flags)
    return env


def run_worker(case: dict, env: dict[str, str], run_dir: Path, trial: int) -> dict:
    case_path = run_dir / f"_case_{case['id']}.json"
    case_path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [str(PY), str(WORKER), str(case_path)],
        capture_output=True, text=True, timeout=900, env=env,
        cwd=str(BOT_DIR),
    )
    if proc.returncode not in (0, 3) or not proc.stdout.strip():
        return {"case_id": case["id"], "answer": "",
                "error": f"worker rc={proc.returncode} stderr={proc.stderr[-800:]}",
                "wall_s": 0, "tool_calls": [], "transcript_rows": []}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return {"case_id": case["id"], "answer": "",
                "error": f"worker output unparseable: {proc.stdout[-400:]}",
                "wall_s": 0, "tool_calls": [], "transcript_rows": []}


def write_report(run_dir: Path, arch: str, suite: str,
                 rows: list[dict]) -> str:
    lines = [f"# tool loop eval — suite={suite} arch={arch}",
             f"run_dir: {run_dir}", ""]
    gate_fab = 0
    gate_auto_fail = []
    total_criteria = passed_criteria = 0
    for r in rows:
        auto_ok = all(c["ok"] for c in r["auto_checks"]) if r["auto_checks"] else None
        crits = r["judge"]["criteria"]
        total_criteria += sum(1 for c in crits if c is not None)
        passed_criteria += sum(1 for c in crits if c is True)
        fab = r["judge"]["fabrications"]
        gate_fab += len(fab)
        if r["auto_checks"] and not auto_ok:
            gate_auto_fail.append(f"{r['case_id']}#{r['trial']}")
        lines.append(
            f"## {r['case_id']} trial={r['trial']} wall={r['wall_s']}s "
            f"error={r['error'] or '-'}"
        )
        for c in r["auto_checks"]:
            lines.append(f"- auto {c['type']}: {'PASS' if c['ok'] else 'FAIL'} {c['detail']}")
        for i, ok in enumerate(crits):
            mark = {True: "PASS", False: "FAIL", None: "N/A"}[ok]
            lines.append(f"- rubric[{i+1}]: {mark}")
        if fab:
            lines.append(f"- ⚠️ fabrications: {fab}")
        if r["judge"].get("degraded"):
            lines.append("- (judge degraded)")
        lines.append("")
    lines.append("## Gates")
    lines.append(f"- fabrication count: {gate_fab} → {'PASS' if gate_fab == 0 else 'FAIL'}")
    lines.append(f"- auto-check failures: {gate_auto_fail or 'none'}")
    if total_criteria:
        lines.append(f"- rubric pass rate: {passed_criteria}/{total_criteria} "
                     f"({100.0*passed_criteria/total_criteria:.0f}%)")
    report = "\n".join(lines)
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    return report


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="smoke")
    ap.add_argument("--arch", default="baseline", choices=["baseline", "candidate"])
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--only", default="")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--flags", default="",
                    help="追加env上書き: KEY=V,KEY2=V2")
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    extra = dict(kv.split("=", 1) for kv in args.flags.split(",") if "=" in kv)
    cases = load_suite(args.suite, only)
    run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{args.suite}_{args.arch}"
    run_dir = EVAL_OUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    env = worker_env(args.arch, extra)

    rows = []
    for case in cases:
        for trial in range(1, args.k + 1):
            print(f"▶ {case['id']} trial {trial}/{args.k} …", flush=True)
            result = run_worker(case, env, run_dir, trial)
            if result.get("error"):
                # transient（OAuth race / 429 / 接続断）を1回だけ吸収する
                print(f"  retry after error: {result['error'][:120]}", flush=True)
                time.sleep(20)
                result = run_worker(case, env, run_dir, trial)
            auto = run_auto_checks(case, result)
            judge = (
                {"criteria": [None] * len(case.get("rubric") or []),
                 "fabrications": [], "degraded": True, "raw": "skipped"}
                if args.no_judge else await judge_one(case, result)
            )
            row = {"case_id": case["id"], "trial": trial,
                   "wall_s": result.get("wall_s"), "error": result.get("error"),
                   "auto_checks": auto, "judge": judge}
            rows.append(row)
            # 生データ保存（transcript 込み）
            (run_dir / f"{case['id']}_t{trial}.json").write_text(
                json.dumps({"case": case, "result": result, "row": row},
                           ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            status = "OK" if not result.get("error") else "ERR"
            autopass = all(c["ok"] for c in auto) if auto else "-"
            print(f"  {status} wall={result.get('wall_s')}s auto={autopass}", flush=True)

    report = write_report(run_dir, args.arch, args.suite, rows)
    print("\n" + report.split("## Gates")[-1])
    print(f"\nreport: {run_dir}/report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
