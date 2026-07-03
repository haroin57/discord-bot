#!/usr/bin/env python3
"""Mem0 v4/v5 回帰評価ハーネス (T8b)。

tests/fixtures/mem0_eval/scenarios.json のシナリオを決定ルート
(`ingest._attribute_conflict_check`) と scope フィルタ (`search._is_fact_valid`)
に流し、期待値との一致を類型別に集計する。段階 ON の GO/NO-GO 判断材料。

- 既定: classify_conflict をシナリオ記載の `llm_conflict_type` を返すスタブに
  差し替え（決定的・オフライン。パイプライン配線と registry ロジックの検証）
- --live: 実 Haiku で classify_conflict を呼ぶ（LLM 判定込みの精度測定。
  Anthropic OAuth 必要、1シナリオ1コール）

使い方:
    .venv/bin/python scripts/eval_mem0_memory.py           # オフライン
    .venv/bin/python scripts/eval_mem0_memory.py --live    # 実LLM込み
"""

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(_ROOT, ".env"))

SCENARIOS = os.path.join(_ROOT, "tests", "fixtures", "mem0_eval", "scenarios.json")


class _FakeMem:
    def __init__(self, rows):
        self._rows = rows

    def get_all(self, user_id=None, filters=None, limit=None):
        rows = self._rows
        if filters:
            attr = filters.get("attribute")
            rows = [r for r in rows
                    if (r.get("metadata") or {}).get("attribute") == attr]
        return {"results": rows}


async def _run_conflict_scenario(sc: dict, live: bool) -> dict:
    """conflict 系シナリオ1件を決定ルートに流し、判定を返す。"""
    from services.mem0 import ingest
    called = {"n": 0}

    async def stub_classify(existing, candidate):
        called["n"] += 1
        return {"conflict_type": sc["llm_conflict_type"], "reason": "eval-stub"}

    mem = _FakeMem([sc["existing"]])
    kept = [dict(sc["candidate"])]
    if live:
        out = await ingest._attribute_conflict_check(
            mem, "eval-user", kept, exclude_ids=set())
        called["n"] = -1  # live では回数検証しない
    else:
        with mock.patch(
            "services.mem0.conflict.classify_conflict", stub_classify,
        ):
            out = await ingest._attribute_conflict_check(
                mem, "eval-user", kept, exclude_ids=set())
    archived = bool(out)
    exp = sc["expect"]
    ok = archived == exp["archived"]
    if not live and "classify_called" in exp:
        ok = ok and (called["n"] > 0) == exp["classify_called"]
    return {"id": sc["id"], "type": sc["type"], "ok": ok,
            "archived": archived, "expected": exp}


def _run_scope_scenario(sc: dict) -> dict:
    """scope 系シナリオ: _is_fact_valid の可視性判定。"""
    import config
    from services.mem0.search import _is_fact_valid
    with mock.patch.object(config, "MEM0_SCOPE_FILTER_ENABLED", True):
        visible = _is_fact_valid(sc["fact"], sc.get("query_channel"))
    ok = visible == sc["expect"]["visible"]
    return {"id": sc["id"], "type": sc["type"], "ok": ok,
            "visible": visible, "expected": sc["expect"]}


async def main(live: bool) -> int:
    with open(SCENARIOS, encoding="utf-8") as f:
        scenarios = json.load(f)["scenarios"]
    results = []
    for sc in scenarios:
        if sc["type"] == "scope_leak":
            results.append(_run_scope_scenario(sc))
        else:
            results.append(await _run_conflict_scenario(sc, live))

    by_type: dict[str, list] = defaultdict(list)
    for r in results:
        by_type[r["type"]].append(r)
    print(f"\n=== Mem0 eval ({'LIVE' if live else 'offline stub'}) ===")
    total_ok = 0
    for t, rows in sorted(by_type.items()):
        ok = sum(1 for r in rows if r["ok"])
        total_ok += ok
        print(f"{t:24s} {ok}/{len(rows)}")
        for r in rows:
            mark = "o" if r["ok"] else "X"
            print(f"  [{mark}] {r['id']}")
    print(f"{'TOTAL':24s} {total_ok}/{len(results)}")
    return 0 if total_ok == len(results) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Mem0 memory eval harness (T8b)")
    ap.add_argument("--live", action="store_true",
                    help="classify_conflict を実 Haiku で呼ぶ（既定はスタブ）")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.live)))
