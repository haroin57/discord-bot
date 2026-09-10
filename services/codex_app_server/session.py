"""One Discord conversation backed by one Codex app-server process/thread.

This is the provider boundary copied conceptually from Hermes Agent's
``CodexAppServerSession``.  Codex owns its native agent/tool loop.  This module
only translates app-server events into the callbacks already consumed by the
Discord renderer and approval UI.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from services.claude_api.ask_bridge import get_ask_handler
from services.claude_api.permissions import evaluate as evaluate_permission
from services.codex_app_server.client import (
    AsyncCodexAppServerClient,
    CodexAppServerError,
    CodexProcessError,
)
from services.codex_app_server.turn_hooks import TurnHooks

log = logging.getLogger(__name__)


class CodexUnavailableError(RuntimeError):
    """The configured Codex binary or app-server could not be used."""


class CodexAuthError(RuntimeError):
    """The app-server is not authenticated with a ChatGPT account."""


def _auth_failure(message: str) -> CodexAuthError:
    from services.codex_app_server.readiness import record_auth_result
    record_auth_result(False)
    return CodexAuthError(message)


class CodexProtocolError(RuntimeError):
    """The app-server returned a response that violates our contract."""


class CodexTurnError(RuntimeError):
    """A Codex turn reached a failed terminal state."""


@dataclass
class CodexTurnResult:
    final_text: str
    thread_id: str
    turn_id: str
    model: str
    model_provider: str
    resumed: bool
    sandbox: str = ""
    approval_policy: str = ""
    token_usage_last: dict[str, Any] | None = None
    token_usage_total: dict[str, Any] | None = None
    model_context_window: int | None = None
    compacted: bool = False
    completed_items: list[dict[str, Any]] = field(default_factory=list)
    hook_recovery_nudges: int = 0
    hook_completion_reviews: int = 0


Callback = Callable[..., Awaitable[Any] | Any]

_IMAGE_LINE_RE = re.compile(
    r"(?m)^-\s+(?P<path>/[^\n]+?)\s+\(Use the Read tool to view this image\)\s*$"
)
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_AUTH_HINTS = (
    "not logged in",
    "login required",
    "unauthorized",
    "authentication",
    "invalid token",
    "refresh token",
    "codex login",
    "chatgpt account",
)


def _looks_like_auth_failure(*parts: object) -> bool:
    text = "\n".join(str(part or "") for part in parts).lower()
    return any(hint in text for hint in _AUTH_HINTS)


def _thread_id_from_result(result: dict[str, Any]) -> str:
    thread = result.get("thread") or {}
    value = (
        thread.get("id")
        or thread.get("sessionId")
        or result.get("threadId")
        or result.get("sessionId")
    )
    return str(value or "").strip()


def _resolved_sandbox_name(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    sandbox_type = str(value.get("type") or "")
    return {
        "dangerFullAccess": "danger-full-access",
        "readOnly": "read-only",
        "workspaceWrite": "workspace-write",
        "externalSandbox": "external-sandbox",
    }.get(sandbox_type, sandbox_type)


def _is_missing_thread_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "thread" in text and any(
        marker in text
        for marker in ("not found", "does not exist", "unknown", "missing")
    )


def _tool_failed(item: dict[str, Any]) -> bool:
    """Normalize terminal native/dynamic/MCP failures for the evidence ledger."""
    if item.get('status') in {'failed', 'declined', 'cancelled', 'canceled'}:
        return True
    if item.get('success') is False or item.get('error'):
        return True
    if item.get('type') == 'commandExecution' and item.get('exitCode') not in (None, 0):
        return True
    result = item.get('result')
    return isinstance(result, dict) and result.get('isError') is True


async def _invoke(callback: Callback | None, *args: Any) -> Any:
    if callback is None:
        return None
    try:
        value = callback(*args)
        if inspect.isawaitable(value):
            return await value
        return value
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # UI callbacks must never kill a model turn.
        log.debug("Codex callback failed: %s", exc, exc_info=True)
        return None


def build_turn_input(prompt: Any, *, allow_local_images: bool = True) -> list[dict[str, Any]]:
    """Translate the bot prompt and downloaded image paths to Codex inputs."""
    items: list[dict[str, Any]] = []
    text_parts: list[str] = []

    if isinstance(prompt, str):
        text_parts.append(prompt)
    elif isinstance(prompt, list):
        for block in prompt:
            if isinstance(block, str):
                text_parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype in {"text", "input_text"}:
                text_parts.append(str(block.get("text") or ""))
            elif btype in {"image", "input_image"}:
                url = block.get("url") or block.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                if isinstance(url, str) and url:
                    items.append({"type": "image", "url": url})
            elif btype == "image_url":
                image_url = block.get("image_url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                if isinstance(image_url, str) and image_url:
                    items.append({"type": "image", "url": image_url})
    else:
        text_parts.append(str(prompt or ""))

    text = "\n".join(part for part in text_parts if part)
    items.insert(0, {"type": "text", "text": text})

    # bot.py already downloaded and size-validated these paths.  Passing them
    # as localImage makes vision work natively instead of asking Codex to infer
    # an image from a prose-only path.
    if not allow_local_images:
        return [item for item in items if item.get('type') == 'text']
    seen: set[str] = set()
    for match in _IMAGE_LINE_RE.finditer(text):
        raw = match.group("path").strip()
        try:
            path = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if (
            str(path) in seen
            or not path.is_file()
            or path.suffix.lower() not in _IMAGE_EXTENSIONS
        ):
            continue
        seen.add(str(path))
        items.append({"type": "localImage", "path": str(path)})
        if len(seen) >= 10:
            break
    return items


class CodexSession:
    """A single app-server connection and persisted Codex thread."""

    def __init__(
        self,
        *,
        cwd: str,
        codex_bin: str,
        stored_thread_id: str | None = None,
        global_args: list[str] | None = None,
        extra_args: list[str] | None = None,
        codex_home: str | None = None,
        sandbox: str = "workspace-write",
        approval_policy: str = "untrusted",
        turn_timeout_s: float = 1800.0,
        post_tool_timeout_s: float = 180.0,
        turn_hooks_enabled: bool = False,
        child_env: dict[str, str] | None = None,
        client_factory: Callable[..., AsyncCodexAppServerClient] | None = None,
        dynamic_tools: list[dict] | None = None,
        tool_executor: Callable[..., Awaitable[str]] | None = None,
        ephemeral: bool = False,
        max_tool_calls: int = 30,
        allow_host_approvals: bool = True,
        allow_local_images: bool = True,
    ) -> None:
        self.cwd = os.path.abspath(os.path.expanduser(cwd))
        expanded_bin = os.path.expanduser(codex_bin)
        self.codex_bin = (
            os.path.abspath(expanded_bin)
            if os.sep in expanded_bin
            else expanded_bin
        )
        self.codex_home = codex_home
        self.global_args = list(global_args or [])
        self.extra_args = list(extra_args or [])
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.turn_timeout_s = turn_timeout_s
        self.post_tool_timeout_s = post_tool_timeout_s
        self.turn_hooks_enabled = turn_hooks_enabled
        self.child_env = dict(child_env or {})
        self.dynamic_tools = list(dynamic_tools or [])
        self.tool_executor = tool_executor
        self.ephemeral = ephemeral
        self.max_tool_calls = max(0, max_tool_calls)
        self.allow_host_approvals = allow_host_approvals
        self.allow_local_images = allow_local_images
        self._tool_call_count = 0
        self._client_factory = client_factory or AsyncCodexAppServerClient
        self._client: AsyncCodexAppServerClient | None = None
        self.thread_id = (stored_thread_id or "").strip() or None
        self.resumed = False
        self.resolved_model = ""
        self.model_provider = ""
        self.resolved_sandbox = ""
        self.resolved_approval_policy = ""
        self._instructions_hash = ""
        self._active_turn_id: str | None = None
        self._has_completed_turn = False
        self._resume_fell_back = False
        self._closed = False

    @property
    def is_alive(self) -> bool:
        return bool(self._client and self._client.is_alive)

    async def ensure_started(self, *, model: str, developer_instructions: str) -> str:
        if self._closed:
            raise CodexUnavailableError("Codex session is closed")
        instruction_hash = hashlib.sha256(
            developer_instructions.encode("utf-8", "replace")
        ).hexdigest()
        if self._client is not None and self._client.is_alive:
            return str(self.thread_id or "")

        self._resume_fell_back = False

        try:
            self._client = self._client_factory(
                codex_bin=self.codex_bin,
                codex_home=self.codex_home,
                global_args=self.global_args,
                extra_args=self.extra_args,
                env={**os.environ, **self.child_env},
            )
            await self._client.start()
            await self._client.initialize(
                client_name="kanae_discord_bot",
                client_title="Kanae Discord Bot",
                client_version="1.0",
                capabilities={"experimentalApi": True} if self.dynamic_tools else {},
            )
        except FileNotFoundError as exc:
            raise CodexUnavailableError(
                f"Codex binary not found: {self.codex_bin}"
            ) from exc
        except (OSError, CodexProcessError, CodexAppServerError) as exc:
            raise CodexUnavailableError(f"Codex app-server startup failed: {exc}") from exc

        assert self._client is not None
        try:
            account = await self._client.request("account/read", {}, timeout=15)
        except (CodexAppServerError, CodexProcessError, TimeoutError) as exc:
            if _looks_like_auth_failure(exc, *self._client.stderr_tail(20)):
                raise _auth_failure(
                    "Codex ChatGPT authentication is unavailable; run `codex login`."
                ) from exc
            raise CodexUnavailableError(f"Codex account preflight failed: {exc}") from exc
        if not isinstance(account, dict):
            raise CodexProtocolError("account/read returned a non-object response")
        account_obj = account.get("account") or {}
        if account_obj.get("type") != "chatgpt":
            # This integration intentionally proves and uses the ChatGPT OAuth
            # subscription route. API-key accounts are rejected rather than
            # silently changing billing paths.
            raise _auth_failure(
                "Codex is not logged in with ChatGPT OAuth; run `codex login`."
            )

        common: dict[str, Any] = {
            "model": model,
            "cwd": self.cwd,
            "approvalPolicy": self.approval_policy,
            "sandbox": self.sandbox,
            "developerInstructions": developer_instructions,
        }
        result: dict[str, Any] | None = None
        if self.thread_id:
            try:
                result = await self._client.request(
                    "thread/resume",
                    # Discord already owns its displayed history. Hydrating
                    # every past turn can return megabytes we never consume;
                    # excludeTurns retains Codex's durable model context.
                    {"threadId": self.thread_id, "excludeTurns": True, **common},
                    timeout=90,
                )
                self.resumed = True
            except TimeoutError:
                # Keep this diagnostic outside the bounded traceback so the
                # failing method and budget survive log truncation.
                log.warning(
                    "Codex thread/resume timed out thread=%s timeout_s=90",
                    str(self.thread_id)[:12],
                )
                raise
            except CodexAppServerError as exc:
                if not _is_missing_thread_error(exc):
                    if _looks_like_auth_failure(exc, *self._client.stderr_tail(20)):
                        raise _auth_failure(
                            "Codex OAuth session refresh failed; run `codex login`."
                        ) from exc
                    raise
                log.warning(
                    "Persisted Codex thread is unavailable; starting fresh: %s",
                    str(self.thread_id)[:12],
                )
                self.thread_id = None
                self._resume_fell_back = True

        if result is None:
            start_options = {"ephemeral": self.ephemeral}
            if self.dynamic_tools:
                start_options["dynamicTools"] = self.dynamic_tools
            try:
                result = await self._client.request(
                    "thread/start",
                    {**common, **start_options},
                    timeout=30,
                )
            except (CodexAppServerError, CodexProcessError, TimeoutError) as exc:
                if _looks_like_auth_failure(exc, *self._client.stderr_tail(20)):
                    raise _auth_failure("Codex OAuth thread startup failed; run `codex login`.") from exc
                raise
            self.resumed = False

        thread_id = _thread_id_from_result(result)
        if not thread_id:
            raise CodexProtocolError(
                f"thread start/resume returned no id (keys={sorted(result)})"
            )
        resolved_model = str(result.get("model") or model)
        provider = str(result.get("modelProvider") or "")
        if resolved_model != model:
            raise CodexProtocolError(
                f"Codex model mismatch: requested {model!r}, resolved {resolved_model!r}"
            )
        if provider and provider != "openai":
            raise CodexProtocolError(
                f"Codex provider mismatch: expected 'openai', resolved {provider!r}"
            )
        resolved_sandbox = _resolved_sandbox_name(result.get("sandbox"))
        if resolved_sandbox and resolved_sandbox != self.sandbox:
            raise CodexProtocolError(
                "Codex sandbox mismatch: "
                f"requested {self.sandbox!r}, resolved {resolved_sandbox!r}"
            )
        resolved_approval = result.get("approvalPolicy")
        if isinstance(resolved_approval, str) and resolved_approval != self.approval_policy:
            raise CodexProtocolError(
                "Codex approval policy mismatch: "
                f"requested {self.approval_policy!r}, resolved {resolved_approval!r}"
            )
        self.thread_id = thread_id
        self.resolved_model = resolved_model
        self.model_provider = provider or "openai"
        self.resolved_sandbox = resolved_sandbox or self.sandbox
        self.resolved_approval_policy = (
            resolved_approval
            if isinstance(resolved_approval, str)
            else self.approval_policy
        )
        self._instructions_hash = instruction_hash
        log.info(
            "Codex thread ready thread=%s resumed=%s model=%s provider=%s cwd=%s",
            thread_id[:12], self.resumed, resolved_model, self.model_provider, self.cwd,
        )
        return thread_id

    async def run_turn(
        self,
        prompt: Any,
        *,
        model: str,
        effort: str,
        developer_instructions: str,
        status_callback: Callback | None = None,
        incremental_text_callback: Callback | None = None,
        text_stream_callback: Callback | None = None,
        tool_invocation_callback: Callback | None = None,
        thinking_stream_callback: Callback | None = None,
        thinking_finalize_callback: Callback | None = None,
        progress: dict[str, Any] | None = None,
        resume_fallback_context: str = "",
        tool_result_callback: Callback | None = None,
        scratch_stream_callback: Callback | None = None,
        scratch_finalize_callback: Callback | None = None,
        turn_hooks: TurnHooks | None = None,
    ) -> CodexTurnResult:
        thread_id = await self.ensure_started(
            model=model, developer_instructions=developer_instructions,
        )
        assert self._client is not None
        await _invoke(status_callback, f"Codex {model} へ送信中")

        effective_prompt = prompt
        if self._resume_fell_back and resume_fallback_context:
            if isinstance(prompt, str):
                effective_prompt = f"{resume_fallback_context}\n\n{prompt}"
            elif isinstance(prompt, list):
                effective_prompt = [
                    {"type": "text", "text": resume_fallback_context},
                    *prompt,
                ]
        self._resume_fell_back = False

        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": build_turn_input(effective_prompt, allow_local_images=self.allow_local_images),
            "model": model,
            "effort": effort,
            "cwd": self.cwd,
            "approvalPolicy": self.approval_policy,
        }
        try:
            started = await self._client.request("turn/start", params, timeout=30)
        except (CodexAppServerError, CodexProcessError, TimeoutError) as exc:
            if _looks_like_auth_failure(exc, *self._client.stderr_tail(30)):
                raise _auth_failure(
                    "Codex OAuth request failed; run `codex login` and retry."
                ) from exc
            raise CodexTurnError(f"Codex turn/start failed: {exc}") from exc

        turn_id = str((started.get("turn") or {}).get("id") or "")
        if not turn_id:
            raise CodexProtocolError("turn/start returned no turn id")
        self._active_turn_id = turn_id
        self._tool_call_count = 0

        item_partials: dict[str, str] = {}
        item_phases: dict[str, str] = {}
        hooks = (turn_hooks or TurnHooks()) if self.turn_hooks_enabled else None
        reasoning_partial = ""
        completed_items: list[dict[str, Any]] = []
        final_text = ""
        last_agent_text = ""
        last_agent_phase = ""
        token_usage_last: dict[str, Any] | None = None
        token_usage_total: dict[str, Any] | None = None
        context_window: int | None = None
        compacted = False
        post_tool_last_activity_at: float | None = None
        deadline = time.monotonic() + self.turn_timeout_s

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Codex turn exceeded {self.turn_timeout_s:.0f}s"
                    )
                if (
                    post_tool_last_activity_at is not None
                    and time.monotonic() - post_tool_last_activity_at
                    > self.post_tool_timeout_s
                ):
                    raise TimeoutError(
                        "Codex stopped producing events after a tool result"
                    )
                if not self._client.is_alive:
                    raise CodexProcessError("Codex app-server exited during turn")

                try:
                    event = await self._client.next_event(
                        timeout=min(1.0, remaining),
                    )
                except TimeoutError:
                    continue
                if not isinstance(event, dict):
                    continue

                if post_tool_last_activity_at is not None:
                    # Measure inactivity, not total time since the tool ended.
                    # Token, delta, plan, and item events all prove that the
                    # server is still making progress toward a final answer.
                    post_tool_last_activity_at = time.monotonic()

                # A request from the server must always receive a response or
                # the turn blocks indefinitely.
                if "id" in event and "method" in event:
                    await self._handle_server_request(event)
                    continue

                method = str(event.get("method") or "")
                event_params = event.get("params") or {}
                event_thread = event_params.get("threadId")
                event_turn = event_params.get("turnId")
                if method == "turn/completed":
                    # Some protocol versions carry the id only in `turn`.
                    event_turn = event_turn or (
                        event_params.get("turn") or {}
                    ).get("id")
                if event_thread not in (None, thread_id):
                    continue
                if event_turn not in (None, turn_id):
                    continue

                if method == "thread/tokenUsage/updated":
                    usage = event_params.get("tokenUsage") or {}
                    if isinstance(usage.get("last"), dict):
                        token_usage_last = dict(usage["last"])
                    if isinstance(usage.get("total"), dict):
                        token_usage_total = dict(usage["total"])
                    window = usage.get("modelContextWindow")
                    if isinstance(window, int) and window > 0:
                        context_window = window
                    if progress is not None and token_usage_last:
                        output = token_usage_last.get("outputTokens")
                        if not isinstance(output, (int, float)):
                            output = token_usage_last.get("output_tokens")
                        if isinstance(output, (int, float)):
                            progress["tokens"] = int(output)
                    continue

                if method in {"thread/compacted"}:
                    compacted = True
                    continue

                if method == "turn/plan/updated":
                    if hooks is not None:
                        hooks.observe_plan(event_params.get("plan"))
                    continue

                if method == "item/started":
                    item = event_params.get("item") or {}
                    if item.get("type") == "agentMessage":
                        item_phases[str(item.get("id") or "")] = str(
                            item.get("phase") or "",
                        )
                    if item.get("type") == "contextCompaction":
                        compacted = True
                    await self._announce_tool(item, tool_invocation_callback)
                    continue

                if method == "item/agentMessage/delta":
                    if text_stream_callback is None:
                        # Final text comes from item/turn completion. Helpers
                        # without a stream consumer need no growing duplicate.
                        continue
                    item_id = str(event_params.get("itemId") or "")
                    delta = str(event_params.get("delta") or "")
                    if delta:
                        # A final answer (including an unknown legacy phase)
                        # must not reach Discord before the completion hook.
                        # Explicit commentary continues to stream normally.
                        if (
                            hooks is not None
                            and item_phases.get(item_id) != "commentary"
                        ):
                            continue
                        item_partials[item_id] = item_partials.get(item_id, "") + delta
                        await _invoke(text_stream_callback, item_partials[item_id])
                    continue

                if method in {
                    "item/reasoning/summaryTextDelta",
                    "item/plan/delta",
                }:
                    if thinking_stream_callback is None:
                        continue
                    delta = str(event_params.get("delta") or "")
                    if delta:
                        reasoning_partial += delta
                        await _invoke(thinking_stream_callback, reasoning_partial)
                    continue

                if method == "item/reasoning/textDelta":
                    # Raw chain-of-thought is never a Discord display channel.
                    # Only provider-authored summaries and explicit plan deltas
                    # are eligible for the existing transient plan renderer.
                    continue

                if method == "item/completed":
                    item = event_params.get("item") or {}
                    if not isinstance(item, dict):
                        continue
                    completed_items.append(item)
                    item_type = item.get("type")
                    if hooks is not None:
                        recovery_prompt = hooks.observe_item(item, turn_id=turn_id)
                        if recovery_prompt:
                            log.info(
                                "Codex hook repeated_failure thread=%s turn=%s",
                                thread_id[:12], turn_id[:12],
                            )
                            await _invoke(
                                status_callback,
                                "同じ操作の失敗が続いたため、手順を見直しています。",
                            )
                            try:
                                await self._client.request(
                                    "turn/steer",
                                    {
                                        "threadId": thread_id,
                                        "expectedTurnId": turn_id,
                                        "input": [{"type": "text", "text": recovery_prompt}],
                                    },
                                    timeout=max(
                                        0.001, min(5.0, deadline - time.monotonic()),
                                    ),
                                )
                            except (CodexAppServerError, CodexProcessError, TimeoutError):
                                # It may already have completed, or the server
                                # may not support steering. Do not resend an
                                # ambiguously accepted nudge. Stop still checks.
                                log.info(
                                    "Codex recovery steer unavailable; "
                                    "completion hook remains active",
                                )
                    if item_type == "agentMessage":
                        item_id = str(item.get("id") or "")
                        item_partials.pop(item_id, None)
                        item_phases.pop(item_id, None)
                        text = str(item.get("text") or "")
                        if text:
                            last_agent_text = text
                            phase = str(item.get("phase") or "")
                            last_agent_phase = phase
                            if phase in {"final_answer", "finalAnswer"}:
                                final_text = text
                            if hooks is None or phase == "commentary":
                                await _invoke(incremental_text_callback, text)
                    elif item_type == "reasoning":
                        await _invoke(thinking_finalize_callback)
                        reasoning_partial = ""
                    elif item_type == "contextCompaction":
                        compacted = True
                    if item_type in {
                        "commandExecution", "fileChange", "mcpToolCall",
                        "dynamicToolCall", "webSearch", "imageGeneration",
                    }:
                        post_tool_last_activity_at = time.monotonic()
                        name = str(item.get('tool') or item_type)
                        arguments = item.get('arguments') or {}
                        if not isinstance(arguments, dict):
                            arguments = {'input': str(arguments)}
                        if item_type == 'commandExecution':
                            arguments = {'command': item.get('command', '')}
                        is_error = _tool_failed(item)
                        await _invoke(tool_result_callback, name, arguments,
                                      json.dumps(item, ensure_ascii=False), is_error)
                        if name == 'Scratchpad' and arguments.get('section') and not is_error:
                            await _invoke(scratch_stream_callback, str(arguments['section']),
                                          str(arguments.get('content', '')), str(arguments.get('op', 'write')))
                            await _invoke(scratch_finalize_callback)
                    continue

                if method == "model/rerouted":
                    # A silent reroute would violate the user's exact-model
                    # requirement. Fail the turn instead of reporting a false
                    # gpt-5.6-sol proof.
                    destination = (
                        event_params.get("toModel")
                        or event_params.get("model")
                        or event_params.get("to")
                    )
                    if destination and destination != model:
                        raise CodexProtocolError(
                            f"Codex rerouted {model!r} to {destination!r}"
                        )
                    continue

                if method == "error":
                    message = str(
                        event_params.get("message")
                        or (event_params.get("error") or {}).get("message")
                        or "Codex app-server error"
                    )
                    if event_params.get("willRetry") is True:
                        await _invoke(status_callback, "Codex が接続を再試行中")
                        continue
                    if _looks_like_auth_failure(message):
                        raise _auth_failure(
                            "Codex OAuth failed; run `codex login` and retry."
                        )
                    raise CodexTurnError(message)

                if method == "turn/completed":
                    turn = event_params.get("turn") or {}
                    status = str(turn.get("status") or "")
                    if not final_text:
                        for item in turn.get("items") or []:
                            if (
                                isinstance(item, dict)
                                and item.get("type") == "agentMessage"
                                and item.get("text")
                            ):
                                last_agent_text = str(item["text"])
                                last_agent_phase = str(item.get("phase") or "")
                                if str(item.get("phase") or "") in {
                                    "final_answer", "finalAnswer",
                                }:
                                    final_text = last_agent_text
                    if status not in {"completed", "interrupted"}:
                        error = turn.get("error") or {}
                        message = str(error.get("message") or f"turn status={status}")
                        if _looks_like_auth_failure(message):
                            raise _auth_failure(
                                "Codex OAuth failed; run `codex login` and retry."
                            )
                        raise CodexTurnError(message)
                    if status == "interrupted":
                        raise asyncio.CancelledError
                    if hooks is not None:
                        # Normally item/completed is authoritative. Also cover
                        # servers that supply completed items only in the
                        # terminal snapshot; observe_item deduplicates ids.
                        for item in turn.get("items") or []:
                            if isinstance(item, dict):
                                hooks.observe_item(
                                    item, turn_id=turn_id, allow_recovery=False,
                                )
                        review_prompt = hooks.completion_prompt()
                        if review_prompt:
                            log.info(
                                "Codex hook completion_review thread=%s failures=%d pending_steps=%d",
                                thread_id[:12], hooks.failure_count, hooks.pending_steps,
                            )
                            await _invoke(
                                status_callback,
                                "実行結果と未完了の手順を確認しています。",
                            )
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError(
                                    "Codex completion check exceeded the turn deadline",
                                )
                            # Continue on the same thread, under the same model,
                            # effort, permissions, manager lock and deadline.
                            # Never replay the original request or its tools.
                            review_params = dict(params)
                            review_params["input"] = [
                                {"type": "text", "text": review_prompt},
                            ]
                            try:
                                reviewed = await self._client.request(
                                    "turn/start", review_params,
                                    timeout=min(30.0, remaining),
                                )
                            except (CodexAppServerError, CodexProcessError) as exc:
                                raise CodexTurnError(
                                    "Codex completion check could not start",
                                ) from exc
                            next_turn_id = str(
                                (reviewed.get("turn") or {}).get("id") or "",
                            )
                            if not next_turn_id or next_turn_id == turn_id:
                                raise CodexProtocolError(
                                    "completion check returned no new turn id",
                                )
                            turn_id = next_turn_id
                            self._active_turn_id = turn_id
                            final_text = last_agent_text = reasoning_partial = ""
                            last_agent_phase = ""
                            item_partials.clear()
                            item_phases.clear()
                            post_tool_last_activity_at = None
                            continue
                    break

            answer = (final_text or last_agent_text).strip()
            if not answer:
                raise CodexTurnError("Codex completed without an agent message")
            if hooks is not None and (final_text or last_agent_phase != "commentary"):
                await _invoke(text_stream_callback, answer)
                await _invoke(incremental_text_callback, answer)
            was_resumed = self.resumed or self._has_completed_turn
            self._has_completed_turn = True
            from services.codex_app_server.readiness import record_auth_result
            record_auth_result(True)
            return CodexTurnResult(
                final_text=answer,
                thread_id=thread_id,
                turn_id=turn_id,
                model=model,
                model_provider=self.model_provider or "openai",
                resumed=was_resumed,
                sandbox=self.resolved_sandbox,
                approval_policy=self.resolved_approval_policy,
                token_usage_last=token_usage_last,
                token_usage_total=token_usage_total,
                model_context_window=context_window,
                compacted=compacted,
                completed_items=completed_items,
                hook_recovery_nudges=hooks.recovery_nudges if hooks else 0,
                hook_completion_reviews=hooks.completion_reviews if hooks else 0,
            )
        except asyncio.CancelledError:
            await self.interrupt()
            raise
        except TimeoutError as exc:
            await self.interrupt()
            raise CodexTurnError(str(exc)) from exc
        finally:
            self._active_turn_id = None

    async def _announce_tool(
        self, item: dict[str, Any], callback: Callback | None,
    ) -> None:
        item_type = str(item.get("type") or "")
        if item_type == "commandExecution":
            await _invoke(callback, "Bash", {
                "command": item.get("command") or "",
                "cwd": item.get("cwd") or self.cwd,
            })
        elif item_type == "fileChange":
            await _invoke(callback, "Edit", {
                "changes": item.get("changes") or [],
            })
        elif item_type == "mcpToolCall":
            name = f"mcp__{item.get('server') or 'unknown'}__{item.get('tool') or 'tool'}"
            await _invoke(callback, name, item.get("arguments") or {})
        elif item_type == "dynamicToolCall":
            await _invoke(
                callback,
                str(item.get("tool") or "dynamicTool"),
                item.get("arguments") or {},
            )
        elif item_type == "webSearch":
            await _invoke(callback, "WebSearch", {"query": item.get("query") or ""})
        elif item_type == "imageGeneration":
            await _invoke(callback, "ImageGeneration", {})

    async def _handle_server_request(self, request: dict[str, Any]) -> None:
        assert self._client is not None
        request_id = request.get("id")
        method = str(request.get("method") or "")
        params = request.get("params") or {}

        if method == "item/tool/call" and self.tool_executor is not None:
            name = str(params.get("tool") or "")
            allowed = {tool["name"] for tool in self.dynamic_tools}
            valid_thread = bool(self.thread_id) and params.get("threadId") == self.thread_id
            valid_turn = bool(self._active_turn_id) and params.get("turnId") == self._active_turn_id
            arguments = params.get("arguments")
            if (name not in allowed or not valid_thread or not valid_turn
                    or not isinstance(arguments, dict)
                    or self._tool_call_count >= self.max_tool_calls):
                result = {"success": False, "contentItems": [{"type": "inputText", "text": "Tool call denied by host policy or budget."}]}
            else:
                self._tool_call_count += 1
                try:
                    text = await asyncio.wait_for(self.tool_executor(name, arguments), timeout=self.post_tool_timeout_s)
                    result = {"success": True, "contentItems": [{"type": "inputText", "text": str(text)}]}
                except Exception as exc:
                    result = {"success": False, "contentItems": [{"type": "inputText", "text": f"Tool execution failed: {type(exc).__name__}"}]}
            await self._client.respond(request_id, result)
            return

        if method in {
            "item/commandExecution/requestApproval", "execCommandApproval",
        }:
            command = str(params.get("command") or "")
            cwd = str(params.get("cwd") or self.cwd)
            reason = str(params.get("reason") or "Codex sandbox escalation")
            approved = await self._request_approval(
                "Bash", {"command": command}, f"cwd={cwd}\n{reason}\n{command}",
            )
            decision = "accept" if approved else "decline"
            if method == "execCommandApproval":
                decision = "approved" if approved else "denied"
            await self._client.respond(request_id, {"decision": decision})
            return

        if method in {"item/fileChange/requestApproval", "applyPatchApproval"}:
            reason = str(params.get("reason") or "Codex file change")
            grant_root = str(params.get("grantRoot") or self.cwd)
            approved = await self._request_approval(
                "Edit", {"file_path": grant_root}, f"{reason}\nroot={grant_root}",
            )
            decision = "accept" if approved else "decline"
            if method == "applyPatchApproval":
                decision = "approved" if approved else "denied"
            await self._client.respond(request_id, {"decision": decision})
            return

        if method == "mcpServer/elicitation/request":
            await self._client.respond(
                request_id,
                {"action": "decline", "content": None, "_meta": None},
            )
            return
        if method == "item/tool/requestUserInput":
            # Discord's approval UI is boolean, not a general form renderer.
            # Empty answers fail closed without leaving the turn hung.
            await self._client.respond(request_id, {"answers": {}})
            return
        if method == "item/permissions/requestApproval":
            await self._client.respond_error(
                request_id, -32000, "Permission escalation denied by Discord host",
            )
            return

        # Dynamic host tools and token injection are intentionally not exposed.
        # Codex reads/refreshes its own 0600 OAuth store instead.
        await self._client.respond_error(
            request_id, -32601, f"Unsupported server request: {method}",
        )

    async def _request_approval(
        self, tool_name: str, tool_input: dict[str, Any], detail: str,
    ) -> bool:
        if not self.allow_host_approvals:
            return False
        decision, rule = evaluate_permission(tool_name, tool_input)
        if decision == "deny":
            log.warning("Codex tool denied by permission rule %s", rule)
            return False
        if decision == "allow":
            return True
        handler = get_ask_handler()
        if handler is None:
            return False
        try:
            return bool(await handler(
                tool_name,
                detail[:800],
                rule or "Codex app-server approval",
            ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Codex Discord approval failed closed: %s", exc)
            return False

    async def interrupt(self) -> None:
        if not self._client or not self.thread_id or not self._active_turn_id:
            return
        try:
            await self._client.request(
                "turn/interrupt",
                {"threadId": self.thread_id, "turnId": self._active_turn_id},
                timeout=5,
            )
        except Exception:
            log.debug("Codex turn/interrupt failed during cleanup", exc_info=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            await self._client.close()
        self._client = None
