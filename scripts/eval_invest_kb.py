#!/usr/bin/env python3
"""Run the full invest_kb evaluation suite.

Sections:
  (a) MCQ accuracy on pfnet-research/japanese-lm-fin-harness datasets, in
      3 modes (baseline_no_kb / kb_no_rerank / kb_full) for comparison.
  (b) Opus-judged citation quality on the hand-written golden scenario set.
  (c) Gate false-positive rate on the chitchat set.

Report is written to ``.state/invest_kb_eval/<utc-date>.md``.

Usage:
  python scripts/eval_invest_kb.py \\
      --harness-path /tmp/japanese-lm-fin-harness \\
      --scenarios tests/fixtures/invest_eval_scenarios.jsonl \\
      --chitchat tests/fixtures/chitchat_scenarios.jsonl \\
      --mcq-limit 25 \\
      --mcq-datasets cma_basics,fp2,security_sales_1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

log = logging.getLogger("eval_invest_kb")


def _resolve_mcq_path(harness_path: Path, name: str) -> Path:
    return harness_path / "jlm_fin_eval" / "datasets" / name / "data.json"


async def _run_mcq(args) -> dict:
    from services.invest_kb_eval.mcq import score_mcq
    harness_path = Path(args.harness_path)
    datasets = [s.strip() for s in args.mcq_datasets.split(",") if s.strip()]
    modes = [s.strip() for s in args.mcq_modes.split(",") if s.strip()]
    out: dict = {}
    for ds in datasets:
        ds_path = _resolve_mcq_path(harness_path, ds)
        if not ds_path.exists():
            log.warning("dataset missing: %s — skip", ds_path)
            continue
        out[ds] = {}
        for mode in modes:
            log.info("MCQ %s [%s]", ds, mode)
            try:
                res = await score_mcq(str(ds_path), mode=mode, limit=args.mcq_limit, concurrency=args.concurrency)
            except Exception as exc:
                log.warning("score_mcq failed for %s/%s: %s", ds, mode, exc)
                res = {"error": str(exc), "accuracy": None}
            out[ds][mode] = res
    return out


async def _run_citation(args) -> dict:
    from services.invest_kb_eval.citation import grade_citations
    try:
        return await grade_citations(
            args.scenarios, limit=args.scenarios_limit, concurrency=args.concurrency,
        )
    except Exception as exc:
        log.warning("citation grading failed: %s", exc)
        return {"error": str(exc)}


def _run_gate(args) -> dict:
    from services.invest_kb_eval.gate import measure_gate_fpr
    try:
        return measure_gate_fpr(args.chitchat)
    except Exception as exc:
        return {"error": str(exc)}


def _format_report(mcq: dict, citation: dict, gate: dict) -> str:
    lines = ["# invest_kb evaluation report", ""]

    # (a) MCQ
    lines.append("## (a) MCQ accuracy (pfnet harness)")
    if not mcq:
        lines.append("(no MCQ results)")
    for ds, modes in mcq.items():
        lines.append(f"### {ds}")
        lines.append("| mode | n | correct | accuracy |")
        lines.append("|------|---|---------|----------|")
        for mode, res in modes.items():
            acc = res.get("accuracy")
            n = res.get("n", "?")
            cor = res.get("correct", "?")
            lines.append(f"| {mode} | {n} | {cor} | {acc} |")
        # Delta
        modes_keys = set(modes.keys())
        if {"baseline_no_kb", "kb_full"} <= modes_keys:
            try:
                delta = modes["kb_full"]["accuracy"] - modes["baseline_no_kb"]["accuracy"]
                lines.append(f"\nDelta (kb_full - baseline_no_kb): **{delta:+.2f}pt**")
            except (TypeError, KeyError):
                pass
        if {"kb_no_rerank", "kb_full"} <= modes_keys:
            try:
                delta = modes["kb_full"]["accuracy"] - modes["kb_no_rerank"]["accuracy"]
                lines.append(f"Delta (kb_full - kb_no_rerank): **{delta:+.2f}pt**\n")
            except (TypeError, KeyError):
                pass
        lines.append("")

    # (b) Citation
    lines.append("## (b) Citation quality (Opus judge)")
    if "error" in citation:
        lines.append(f"error: {citation['error']}")
    else:
        avg = citation.get("average", {})
        lines.append(
            f"- n={citation.get('n', 0)}, "
            f"relevance={avg.get('relevance', 0)}, "
            f"neutrality={avg.get('neutrality', 0)}, "
            f"factual={avg.get('factual', 0)}"
        )
        items = citation.get("per_item", [])
        # Top failures by relevance
        def _sort_key(i):
            return i.get("scores", {}).get("relevance", 0) if i.get("scores") else 0
        worst = sorted(items, key=_sort_key)[:5]
        if worst:
            lines.append("\n### Worst by relevance")
            for w in worst:
                s = w.get("scores", {})
                lines.append(
                    f"- query={w.get('query')[:60]!r} → r={s.get('relevance',0)} "
                    f"neu={s.get('neutrality',0)} fac={s.get('factual',0)} "
                    f"top_id={w.get('top_id','-')}\n  comment: {w.get('comment','')[:140]}"
                )
        lines.append("")

    # (c) Gate
    lines.append("## (c) Gate false-positive rate (chitchat)")
    if "error" in gate:
        lines.append(f"error: {gate['error']}")
    else:
        lines.append(
            f"- n={gate.get('n', 0)} fired={gate.get('fired', 0)} "
            f"fpr={gate.get('fpr_pct', 0)}%"
        )
        if gate.get("offenders"):
            lines.append("- offenders:")
            for o in gate["offenders"]:
                lines.append(f"  - {o!r}")
        lines.append("")

    return "\n".join(lines)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--harness-path", default="/tmp/japanese-lm-fin-harness")
    p.add_argument("--scenarios", default="tests/fixtures/invest_eval_scenarios.jsonl")
    p.add_argument("--chitchat", default="tests/fixtures/chitchat_scenarios.jsonl")
    p.add_argument("--mcq-datasets", default="cma_basics,fp2,security_sales_1")
    p.add_argument("--mcq-modes", default="baseline_no_kb,kb_no_rerank,kb_full")
    p.add_argument("--mcq-limit", type=int, default=25)
    p.add_argument("--scenarios-limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--output-dir", default=".state/invest_kb_eval")
    p.add_argument("--skip-mcq", action="store_true")
    p.add_argument("--skip-citation", action="store_true")
    p.add_argument("--skip-gate", action="store_true")
    args = p.parse_args()

    mcq: dict = {}
    citation: dict = {}
    gate: dict = {}
    if not args.skip_gate:
        log.info("running gate FPR …")
        gate = _run_gate(args)
    if not args.skip_mcq:
        log.info("running MCQ …")
        mcq = await _run_mcq(args)
    if not args.skip_citation:
        log.info("running citation grading …")
        citation = await _run_citation(args)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_md = _format_report(mcq, citation, gate)
    report_path = out_dir / f"{ts}.md"
    report_path.write_text(report_md, encoding="utf-8")
    raw_path = out_dir / f"{ts}.json"
    raw_path.write_text(
        json.dumps({"mcq": mcq, "citation": citation, "gate": gate}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("report -> %s", report_path)
    print(report_md)


if __name__ == "__main__":
    asyncio.run(main())
