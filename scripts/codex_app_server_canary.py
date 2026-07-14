#!/usr/bin/env python3
"""Run a reproducible Discord-provider canary against real Codex app-server.

The script uses the same provider router and Discord-facing entry point as
``bot.py`` without opening a second Discord gateway connection. It verifies
ChatGPT OAuth preflight, exact model/provider resolution, streaming callbacks,
native command execution, native Cloudflare MCP execution, and same-thread
resume. It never prints credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


CANARY_CHANNEL_ID = 990000000000000001
CANARY_USER_ID = 990000000000000002


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--effort", default="xhigh")
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument(
        "--post-discord",
        action="store_true",
        help="Post a sanitized PASS receipt to HAROIN_DM_CHANNEL_ID.",
    )
    parser.add_argument(
        "--expect-existing-thread",
        action="store_true",
        help="Require the first turn to resume state created by a prior process.",
    )
    return parser.parse_args()


async def _run(
    model: str,
    effort: str,
    keep_artifacts: bool,
    expect_existing_thread: bool,
) -> dict[str, Any]:
    from config import (
        CODEX_APP_SERVER_CWD,
        CODEX_APP_SERVER_DANGEROUSLY_BYPASS_APPROVALS_AND_SANDBOX,
        HISTORY_DIR,
    )
    from services.claude_api.ask_bridge import set_ask_handler
    from services.codex_app_server.manager import (
        close_all_codex_sessions,
        get_manager,
    )
    from services.provider_router import call_with_provider

    history_path = Path(HISTORY_DIR) / f"{CANARY_CHANNEL_ID}.jsonl"
    if not expect_existing_thread:
        history_path.unlink(missing_ok=True)

    approvals: list[dict[str, Any]] = []

    async def safe_canary_approval(
        tool_name: str,
        detail: str,
        rule: str,
    ) -> bool:
        command = detail.rsplit("\n", 1)[-1].strip()
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = []
        safe_commands = {"/usr/bin/pwd", "node --version"}
        if argv and argv[0] in {"/bin/bash", "bash", "/bin/sh", "sh"}:
            allowed = len(argv) == 3 and argv[1] in {"-c", "-lc"} and (
                argv[2] in safe_commands
            )
        else:
            allowed = command in safe_commands
        approvals.append({
            "tool": tool_name,
            "command": command,
            "rule": rule,
            "approved": allowed,
        })
        return allowed

    set_ask_handler(safe_canary_approval)
    statuses: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    streamed_text: list[str] = []

    async def on_tool(name: str, arguments: dict[str, Any]) -> None:
        tool_calls.append({"name": name, "arguments": arguments})

    async def on_stream(text: str) -> None:
        streamed_text.append(text)

    started_at = time.time()
    try:
        first_text, _first_session, first_resumed = await call_with_provider(
            "Return exactly DISCORD_CODEX_ROUTE_OK and nothing else. Do not use tools.",
            CANARY_CHANNEL_ID,
            {},
            provider="codex",
            restricted=False,
            raw_user_text="Codex route canary turn one",
            author="canary",
            user_id=CANARY_USER_ID,
            forced_model_override=model,
            forced_effort_override=effort,
            status_callback=statuses.append,
            text_stream_callback=on_stream,
        )
        if first_text.strip() != "DISCORD_CODEX_ROUTE_OK":
            raise RuntimeError(f"unexpected first response: {first_text!r}")

        second_text, _second_session, second_resumed = await call_with_provider(
            (
                "Use the terminal tool to run /usr/bin/pwd and node --version. "
                "Do not run any other command. Then reply with "
                "DISCORD_CODEX_TOOL_OK and summarize both outputs."
            ),
            CANARY_CHANNEL_ID,
            {},
            provider="codex",
            restricted=False,
            raw_user_text="Codex native tool canary turn two",
            author="canary",
            user_id=CANARY_USER_ID,
            forced_model_override=model,
            forced_effort_override=effort,
            status_callback=statuses.append,
            text_stream_callback=on_stream,
            tool_invocation_callback=on_tool,
        )
        if "DISCORD_CODEX_TOOL_OK" not in second_text:
            raise RuntimeError(f"tool marker missing: {second_text!r}")
        if re.search(r"\bv\d+\.\d+\.\d+\b", second_text) is None:
            raise RuntimeError(f"node PATH execution was not proven: {second_text!r}")
        expected_pwd = str(Path(CODEX_APP_SERVER_CWD).expanduser().resolve())
        if expected_pwd not in second_text:
            raise RuntimeError(
                f"Codex working directory was not proven: {second_text!r}"
            )
        commands = "\n".join(
            str(call.get("arguments", {}).get("command") or "")
            for call in tool_calls
        )
        if "/usr/bin/pwd" not in commands or "node --version" not in commands:
            raise RuntimeError(f"expected native command events missing: {commands!r}")

        mcp_call_start = len(tool_calls)
        third_text, _third_session, third_resumed = await call_with_provider(
            (
                "Use one configured Cloudflare MCP documentation search tool "
                "exactly once to look up the official meaning of Workers "
                "waitUntil. Do not use Bash, curl, WebSearch, or any other "
                "tool. After that native MCP call completes, reply with "
                "DISCORD_CODEX_MCP_OK and one sentence."
            ),
            CANARY_CHANNEL_ID,
            {},
            provider="codex",
            restricted=False,
            raw_user_text="Codex native Cloudflare MCP canary turn three",
            author="canary",
            user_id=CANARY_USER_ID,
            forced_model_override=model,
            forced_effort_override=effort,
            status_callback=statuses.append,
            text_stream_callback=on_stream,
            tool_invocation_callback=on_tool,
        )
        if "DISCORD_CODEX_MCP_OK" not in third_text:
            raise RuntimeError(f"MCP marker missing: {third_text!r}")
        mcp_turn_calls = tool_calls[mcp_call_start:]
        native_mcp_calls = [
            call for call in mcp_turn_calls
            if str(call.get("name") or "").startswith("mcp__cloudflare")
        ]
        if not native_mcp_calls:
            raise RuntimeError(f"native Cloudflare MCP event missing: {mcp_turn_calls!r}")
        if any(call.get("name") == "Bash" for call in mcp_turn_calls):
            raise RuntimeError(f"MCP turn used Bash as a proxy: {mcp_turn_calls!r}")

        context = get_manager().context(CANARY_CHANNEL_ID, CANARY_USER_ID) or {}
        if context.get("model") != model:
            raise RuntimeError(f"model mismatch in durable state: {context!r}")
        if context.get("model_provider") != "openai":
            raise RuntimeError(f"provider mismatch in durable state: {context!r}")
        if CODEX_APP_SERVER_DANGEROUSLY_BYPASS_APPROVALS_AND_SANDBOX:
            if context.get("sandbox") != "danger-full-access":
                raise RuntimeError(f"sandbox bypass was not resolved: {context!r}")
            if context.get("approval_policy") != "never":
                raise RuntimeError(f"approval bypass was not resolved: {context!r}")
            if approvals:
                raise RuntimeError(
                    f"bypass mode unexpectedly requested approval: {approvals!r}"
                )
        expected_resume = expect_existing_thread
        if first_resumed is not expected_resume or not second_resumed or not third_resumed:
            raise RuntimeError(
                "thread resume invariant failed: "
                f"expected_first={expected_resume} "
                f"first={first_resumed} second={second_resumed} "
                f"third={third_resumed}"
            )

        return {
            "ok": True,
            "model": context.get("model"),
            "model_provider": context.get("model_provider"),
            "sandbox": context.get("sandbox"),
            "approval_policy": context.get("approval_policy"),
            "thread_id": context.get("thread_id"),
            "first_resumed": first_resumed,
            "second_resumed": second_resumed,
            "third_resumed": third_resumed,
            "first_response": first_text,
            "second_response": second_text,
            "third_response": third_text,
            "native_mcp_tool": native_mcp_calls[0].get("name"),
            "mcp_turn_used_bash": any(
                call.get("name") == "Bash" for call in mcp_turn_calls
            ),
            "tool_calls": tool_calls,
            "approvals": approvals,
            "status_updates": statuses,
            "stream_event_count": len(streamed_text),
            "model_context_window": context.get("model_context_window"),
            "tool_pwd": expected_pwd,
            "elapsed_seconds": round(time.time() - started_at, 3),
        }
    finally:
        await close_all_codex_sessions()
        if not keep_artifacts:
            history_path.unlink(missing_ok=True)
            state_path = os.environ.get("CODEX_APP_SERVER_STATE_PATH")
            if state_path:
                Path(state_path).unlink(missing_ok=True)


async def _post_discord_receipt(result: dict[str, Any]) -> dict[str, Any]:
    """Post only non-secret proof fields through the production bot identity."""
    import aiohttp

    from config import (
        ADMIN_USER_IDS,
        HAROIN_DM_CHANNEL_ID,
        HISTORY_DIR,
        SPEAKER_LABELS,
        validate_discord_token,
    )

    token = validate_discord_token()
    second_response = str(result.get("second_response") or "")
    node_match = re.search(r"\bv\d+\.\d+\.\d+\b", second_response)
    content = "\n".join([
        "Codex app-server canary: PASS",
        f"model: {result['model']}",
        f"provider: {result['model_provider']}",
        "auth: ChatGPT OAuth managed by Codex CLI",
        (
            "route/resume: "
            f"{result['first_response']} / second_resumed={result['second_resumed']}"
        ),
        (
            f"native tools: pwd={result['tool_pwd']}, "
            f"node={node_match.group(0) if node_match else 'missing'}"
        ),
        f"native MCP: {result['native_mcp_tool']} (Bash proxy: false)",
        f"stream events: {result['stream_event_count']}",
    ])
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"Authorization": f"Bot {token}"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        raw_canary_channel = os.environ.get(
            "CODEX_CANARY_DISCORD_CHANNEL_ID", "",
        ).strip()
        channel_id = (
            int(raw_canary_channel)
            if raw_canary_channel.isdigit()
            else HAROIN_DM_CHANNEL_ID
        )
        if channel_id is None:
            preferred_ids = {
                user_id
                for user_id, label in SPEAKER_LABELS.items()
                if label.casefold() == "haroin"
            }
            fallback_channel_id: int | None = None
            history_paths = sorted(
                Path(HISTORY_DIR).glob("*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for history_path in history_paths[:100]:
                if not history_path.stem.isdigit():
                    continue
                candidate = int(history_path.stem)
                async with session.get(
                    f"https://discord.com/api/v10/channels/{candidate}",
                    headers=headers,
                ) as response:
                    if response.status != 200:
                        continue
                    channel = await response.json()
                if channel.get("type") != 1:
                    continue
                recipient_ids = {
                    int(recipient["id"])
                    for recipient in channel.get("recipients", [])
                    if str(recipient.get("id") or "").isdigit()
                }
                if preferred_ids & recipient_ids:
                    channel_id = candidate
                    break
                if fallback_channel_id is None and ADMIN_USER_IDS & recipient_ids:
                    fallback_channel_id = candidate
            if channel_id is None:
                channel_id = fallback_channel_id
        if channel_id is None:
            raise RuntimeError(
                "no verified admin DM channel is available for --post-discord"
            )

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        async with session.post(
            url,
            headers=headers,
            json={"content": content, "allowed_mentions": {"parse": []}},
        ) as response:
            if response.status != 200:
                body = (await response.text())[:500]
                raise RuntimeError(
                    f"Discord receipt failed: HTTP {response.status}: {body}"
                )
            payload = await response.json()
    return {
        "ok": True,
        "message_id_present": bool(payload.get("id")),
        "http_status": 200,
    }


def main() -> None:
    args = _parse_args()
    result = asyncio.run(_run(
        args.model,
        args.effort,
        args.keep_artifacts,
        args.expect_existing_thread,
    ))
    if args.post_discord:
        result["discord_delivery"] = asyncio.run(_post_discord_receipt(result))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
