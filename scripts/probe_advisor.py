"""Anthropic Advisor tool（advisor_20260301, beta）が現在の API 経路で使えるか probe する。

ADVISOR_ENABLED=true を .env に入れる前に 1 回実行する（staged rollout の gate）。
probe_tool_search.py と同じく本番 byte-shape（4ブロック system 等）を再現する。
advisor の応答ブロック type も観測して出力する（_block_to_dict の echo 対応判断用）。

実行: .venv/bin/python scripts/probe_advisor.py
  exit 0 = OK / 1 = advisor 起因の 400 / 2 = 判定不能（429等）
"""

import asyncio
import sys

sys.path.insert(0, "/home/ubuntu/discord-bot")

from config import CLAUDE_MODEL, SYSTEM_PROMPT  # noqa: E402
from services.claude_api.client import (  # noqa: E402
    CONTEXT_MANAGEMENT_CONFIG,
    MODEL_BETAS,
    OUTPUT_CONFIG,
    THINKING_CONFIG,
    get_metadata,
)
from services.claude_api.client_factory import make_api_client  # noqa: E402
from services.claude_api.tool_catalog import advisor_tool  # noqa: E402
from services.claude_api.tool_loop import (  # noqa: E402
    _build_cached_system,
    _inject_cache_breakpoints,
)


async def main() -> int:
    client = await make_api_client(False)
    betas = list(MODEL_BETAS)
    if "advisor-tool-2026-03-01" not in betas:
        betas.append("advisor-tool-2026-03-01")
    tools = [advisor_tool()]
    print(f"probe: advisor tool={tools[0]} model={CLAUDE_MODEL}")

    messages = _inject_cache_breakpoints([
        {"role": "user", "content": (
            "advisor ツールを1回だけ使って『1+1は？』と相談し、"
            "その回答を一言で報告して。"
        )},
    ])
    try:
        async with client.beta.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=_build_cached_system(SYSTEM_PROMPT, messages),
            messages=messages,
            tools=tools,
            betas=betas,
            metadata=get_metadata(),
            thinking=THINKING_CONFIG,
            output_config=OUTPUT_CONFIG,
            extra_body={"context_management": CONTEXT_MANAGEMENT_CONFIG},
        ) as stream:
            final = await stream.get_final_message()
    except Exception as exc:
        msg = str(exc).lower()
        if "advisor" in msg:
            print(f"NG: advisor rejected: {type(exc).__name__}: {exc}")
            return 1
        print(f"INCONCLUSIVE: {type(exc).__name__}: {exc}")
        return 2
    types = [getattr(b, "type", "?") for b in final.content]
    text = "".join(
        getattr(b, "text", "") for b in final.content
        if getattr(b, "type", None) == "text"
    )
    print(f"OK: stop_reason={final.stop_reason} block_types={types}")
    print(f"text={text[:120]!r}")
    # advisor の呼び出しと結果の中身を verbatim で出す（実行エラーの診断用）
    for b in final.content:
        btype = getattr(b, "type", None)
        if btype == "server_tool_use":
            print(f"server_tool_use: name={getattr(b, 'name', '?')} "
                  f"input={getattr(b, 'input', None)}")
        elif btype == "advisor_tool_result":
            content = getattr(b, "content", None)
            print(f"advisor_tool_result raw: {content!r}")
            if hasattr(content, "model_dump"):
                print(f"advisor_tool_result dump: {content.model_dump()}")
            elif isinstance(content, list):
                for item in content:
                    dump = (item.model_dump() if hasattr(item, "model_dump")
                            else item)
                    print(f"  item: {dump}")
    print("↑ block_types に advisor 系の type が出ていれば、_block_to_dict の"
          " echo 対応（strip 対象タプル拡張）を実装してから ADVISOR_ENABLED=true にすること。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
