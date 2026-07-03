"""Tool Search Tool が現在の API 経路（subscriber OAuth）で使えるか probe する.

TOOL_SEARCH_ENABLED=true を .env に入れる前に 1 回実行し、defer_loading 付き
リクエストが 400 にならないことを確認する（staged rollout の gate）。

重要: subscriber-tier gating は system[2]（CLI 規定プロンプト）等の byte-shape を
検査するため、裸の system 文字列を送ると 429 'Error' でゲートウェイに弾かれる
（2026-07-02 に実測）。本 probe は tool_loop と同じ 4 ブロック system / metadata /
output_config / context_management を再現し、tools は「フラグ ON 時に本番が送る
のと同じ」build_advertised_tools(tool_search_enabled=True) を使う。

実行: .venv/bin/python scripts/probe_tool_search.py
  exit 0 = OK（有効化してよい）
  exit 1 = tool search 起因の 400（従来モード継続）
  exit 2 = それ以外の失敗（429 等 — 判定不能、後で再実行）
"""

import asyncio
import sys

sys.path.insert(0, "/home/ubuntu/discord-bot")

from config import ADMIN_USER_IDS, CLAUDE_MODEL, SYSTEM_PROMPT  # noqa: E402
from services.claude_api.client import (  # noqa: E402
    CONTEXT_MANAGEMENT_CONFIG,
    MODEL_BETAS,
    OUTPUT_CONFIG,
    THINKING_CONFIG,
    get_metadata,
)
from services.claude_api.client_factory import make_api_client  # noqa: E402
from services.claude_api.entry import ALWAYS_ADVERTISED_MCP_KEYS  # noqa: E402
from services.claude_api.errors import is_tool_search_request_error  # noqa: E402
from services.claude_api.tool_catalog import build_advertised_tools  # noqa: E402
from services.claude_api.tool_loop import (  # noqa: E402
    _build_cached_system,
    _inject_cache_breakpoints,
)


async def main() -> int:
    client = await make_api_client(False)
    admin_id = next(iter(ADMIN_USER_IDS), None)
    # フラグ ON 時に本番 entry.py が送るのと同じ tools 配列（Tool Search +
    # hot set 非 defer + 残り全カタログ defer_loading）。
    tools = build_advertised_tools(
        restricted=False,
        live_handles={},
        always_keys=set(ALWAYS_ADVERTISED_MCP_KEYS),
        hot_keys=set(ALWAYS_ADVERTISED_MCP_KEYS),
        user_id=admin_id,
        tool_search_enabled=True,
        agent_tool_schema=None,
    )
    n_deferred = sum(1 for t in tools if t.get("defer_loading"))
    print(f"probe: tools={len(tools)} (deferred={n_deferred}) model={CLAUDE_MODEL}")

    messages = _inject_cache_breakpoints([
        {"role": "user", "content": "ping — 「ok」とだけ返して。ツールは使わないで。"},
    ])
    try:
        async with client.beta.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=_build_cached_system(SYSTEM_PROMPT, messages),
            messages=messages,
            tools=tools,
            betas=MODEL_BETAS,
            metadata=get_metadata(),
            thinking=THINKING_CONFIG,
            output_config=OUTPUT_CONFIG,
            extra_body={"context_management": CONTEXT_MANAGEMENT_CONFIG},
        ) as stream:
            final = await stream.get_final_message()
    except Exception as exc:
        if is_tool_search_request_error(exc):
            print(f"NG: tool search rejected by API: {type(exc).__name__}: {exc}")
            print("→ TOOL_SEARCH_ENABLED は false のままにすること。")
            return 1
        print(f"INCONCLUSIVE: request failed (tool search 起因ではない): "
              f"{type(exc).__name__}: {exc}")
        print("→ 判定不能。時間を置いて再実行。")
        return 2
    text = "".join(
        getattr(b, "text", "") for b in final.content
        if getattr(b, "type", None) == "text"
    )
    print(
        f"OK: stop_reason={final.stop_reason} model={final.model} "
        f"text={text[:40]!r}"
    )
    print("Tool Search + defer_loading はこの経路で受理されました。"
          "TOOL_SEARCH_ENABLED=true を .env に設定して再起動で有効化できます。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
