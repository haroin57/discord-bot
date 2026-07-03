"""eval_tool_loop の worker — 1ケース1試行を独立プロセスで実行する。

config は import 時凍結のため、DELIB_* / EVAL_MCP_ENABLED 等のフラグ切替は
親（scripts/eval_tool_loop.py）が設定した環境変数でのみ効く。これが
プロセス分離の理由。

usage: eval_case_worker.py <case_json_path>
stdout に結果 JSON を1行出力。実行後、当該 channel の JSONL を削除して
本番 .state を汚さない。
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import time

sys.path.insert(0, "/home/ubuntu/discord-bot")

EVAL_USER_ID = 999900000000000001


async def run_case(case: dict) -> dict:
    # config import（副作用込み）は env 設定済みのこのプロセスで初めて行う
    import os
    from services.claude_api.entry import call_with_tools
    from services.session import _history_file  # channel JSONL の実パス取得に使う

    channel_id = random.randrange(9_100_000_000_000_000_000, 9_200_000_000_000_000_000)
    user_id = int(case.get("user_id") or EVAL_USER_ID)

    # bot.py と同じ tier 判定（DELIB_ROUTER_ENABLED のときのみ）。eval でも
    # deliberate パイプライン（planner→verifier→revise）を通せるようにする。
    tier = None
    route_judgment = False
    from config import DELIB_ROUTER_ENABLED
    if DELIB_ROUTER_ENABLED:
        from services.claude_api.router import decide_tier
        decision = await decide_tier(case["prompt"])
        tier = decision.tier
        route_judgment = decision.judgment

    t0 = time.monotonic()
    error = None
    text = ""
    try:
        text, _info, _resumed = await call_with_tools(
            case["prompt"],
            channel_id,
            {},
            restricted=False,
            raw_user_text=case["prompt"],
            user_id=user_id,
            tier=tier,
            route_judgment=route_judgment,
        )
    except BaseException as exc:  # noqa: BLE001 - worker は結果として報告する
        def _leaves(e, depth=0):
            if depth < 4 and isinstance(e, BaseExceptionGroup):
                out = []
                for sub in e.exceptions:
                    out.extend(_leaves(sub, depth + 1))
                return out
            return [e]
        leaves = _leaves(exc)
        error = "; ".join(f"{type(e).__name__}: {e}" for e in leaves)[:600]
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
    wall_s = time.monotonic() - t0

    # transcript 回収（tool 呼び出し列の検査・judge digest 用）→ 掃除
    transcript_rows: list[dict] = []
    try:
        jsonl = _history_file(channel_id)
        if os.path.exists(jsonl):
            with open(jsonl, encoding="utf-8") as f:
                for line in f:
                    try:
                        transcript_rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            os.remove(jsonl)
    except Exception as exc:  # noqa: BLE001
        transcript_rows.append({"_transcript_error": str(exc)})

    tool_calls = []
    for row in transcript_rows:
        for msg in row.get("api_messages") or []:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use"):
                    tool_calls.append({
                        "type": b["type"],
                        "name": b.get("name", ""),
                        "input": json.dumps(b.get("input") or {}, ensure_ascii=False)[:300],
                    })

    return {
        "case_id": case["id"],
        "answer": text,
        "error": error,
        "wall_s": round(wall_s, 1),
        "tool_calls": tool_calls,
        "transcript_rows": transcript_rows,
    }


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as f:
        case = json.load(f)
    result = asyncio.run(run_case(case))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("error") is None else 3


if __name__ == "__main__":
    raise SystemExit(main())
