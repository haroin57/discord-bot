"""H系ケース用の Mem0 seed（評価専用 namespace、本番記憶と不可侵）。

実行: .venv/bin/python tests/fixtures/tool_loop_eval/seeds/mem0_seed.py
冪等: 既に seed 済みなら Mem0 の dedup が吸収する。
"""

import asyncio
import sys

sys.path.insert(0, "/home/ubuntu/discord-bot")

EVAL_USER_ID = "999900000000000001"


async def main() -> None:
    from services.mem0.ingest import add_memories

    result = await add_memories(
        [
            {"role": "user", "content": "私の好きな飲み物はルイボスティーです。覚えておいて。"},
            {"role": "assistant", "content": "ルイボスティーですね、覚えました。"},
        ],
        user_id=EVAL_USER_ID,
    )
    print(f"seeded user_id={EVAL_USER_ID} result={result}")


if __name__ == "__main__":
    asyncio.run(main())
