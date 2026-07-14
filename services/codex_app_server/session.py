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

log = logging.getLogger(__name__)


class CodexUnavailableError(RuntimeError):
    """The configured Codex binary or app-server could not be used."""


class CodexAuthError(RuntimeError):
    """The app-server is not authenticated with a ChatGPT account."""


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


def build_turn_input(prompt: Any) -> list[dict[str, Any]]:
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
        child_env: dict[str, str] | None = None,
        client_factory: Callable[..., AsyncCodexAppServerClient] | None = None,
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
        self.child_env = dict(child_env or {})
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
                capabilities={},
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
                raise CodexAuthError(
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
            raise CodexAuthError(
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
                    {"threadId": self.thread_id, **common},
                    timeout=30,
                )
                self.resumed = True
            except CodexAppServerError as exc:
                if not _is_missing_thread_error(exc):
                    if _looks_like_auth_failure(exc, *self._client.stderr_tail(20)):
                        raise CodexAuthError(
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
            result = await self._client.request(
                "thread/start",
                {**common, "ephemeral": False},
                timeout=30,
            )
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
            "input": build_turn_input(effective_prompt),
            "model": model,
            "effort": effort,
            "cwd": self.cwd,
            "approvalPolicy": self.approval_policy,
        }
        try:
            started = await self._client.request("turn/start", params, timeout=30)
        except (CodexAppServerError, CodexProcessError, TimeoutError) as exc:
            if _looks_like_auth_failure(exc, *self._client.stderr_tail(30)):
                raise CodexAuthError(
                    "Codex OAuth request failed; run `codex login` and retry."
                ) from exc
            raise CodexTurnError(f"Codex turn/start failed: {exc}") from exc

        turn_id = str((started.get("turn") or {}).get("id") or "")
        if not turn_id:
            raise CodexProtocolError("turn/start returned no turn id")
        self._active_turn_id = turn_id

        item_partials: dict[str, str] = {}
        reasoning_partial = ""
        completed_items: list[dict[str, Any]] = []
        final_text = ""
        last_agent_text = ""
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

                if method == "item/started":
                    item = event_params.get("item") or {}
                    if item.get("type") == "contextCompaction":
                        compacted = True
                    await self._announce_tool(item, tool_invocation_callback)
                    continue

                if method == "item/agentMessage/delta":
                    item_id = str(event_params.get("itemId") or "")
                    delta = str(event_params.get("delta") or "")
                    if delta:
                        item_partials[item_id] = item_partials.get(item_id, "") + delta
                        await _invoke(text_stream_callback, item_partials[item_id])
                    continue

                if method in {
                    "item/reasoning/summaryTextDelta",
                    "item/plan/delta",
                }:
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
                    if item_type == "agentMessage":
                        text = str(item.get("text") or "")
                        if text:
                            last_agent_text = text
                            phase = str(item.get("phase") or "")
                            if phase in {"final_answer", "finalAnswer"}:
                                final_text = text
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
                        raise CodexAuthError(
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
                                if str(item.get("phase") or "") in {
                                    "final_answer", "finalAnswer",
                                }:
                                    final_text = last_agent_text
                    if status not in {"completed", "interrupted"}:
                        error = turn.get("error") or {}
                        message = str(error.get("message") or f"turn status={status}")
                        if _looks_like_auth_failure(message):
                            raise CodexAuthError(
                                "Codex OAuth failed; run `codex login` and retry."
                            )
                        raise CodexTurnError(message)
                    if status == "interrupted":
                        raise asyncio.CancelledError
                    break

            answer = (final_text or last_agent_text).strip()
            if not answer:
                raise CodexTurnError("Codex completed without an agent message")
            was_resumed = self.resumed or self._has_completed_turn
            self._has_completed_turn = True
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
