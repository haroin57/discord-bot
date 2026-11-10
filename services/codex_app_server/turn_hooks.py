"""Bounded, metadata-only recovery hooks for one Discord request.

These hooks observe native tool outcomes and plans, not the wording of a
reply. A successful call only clears a failure with the same operation and
arguments. Other recovery paths are left for the model to assess once at Stop.
No tool output, command, argument, or plan text is copied into a hook prompt.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict, deque
from typing import Any


_TOOL_TYPES = {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"}
_FAILED = {"failed", "declined", "denied", "cancelled", "canceled"}

RECOVERY_PROMPT = """[Discord runtime: repeated tool failure]
The host observed the same operation with identical arguments fail three times
within the last twelve tool outcomes in this request. Inspect those results and
change the approach before making another identical call. Empty search results
can be valid outcomes. A failed call may still have had side effects: check the
current state before retrying a write, send, purchase, or other external action.
Do not bypass a denial or ask again for an approval that was declined. Continue
independent authorized work; if blocked, report the actual limitation. This is
execution feedback, not a new user request or additional authorization."""

_COMPLETION_PROMPT = """[Discord runtime: completion check]
Before delivering the final answer, reconcile the original request with the
tool results and your latest plan. The host observed {failures} operation(s)
whose latest outcome failed and {pending} unfinished plan step(s).
These counts are signals to check, not proof the task failed: an empty search,
an optional check, or a successful alternative may already explain them.
Finish any remaining authorized work that can be completed, or clearly state
what remains blocked. Verify state before repeating any operation that may have
had side effects; do not repeat completed actions or bypass declined approvals.
Update the plan to match the evidence. Do not mark unfinished work complete just
to clear this check. Return one self-contained answer in the established persona,
without referring to these internal hooks. This check runs at most once for this
request and does not expand the user's authorization."""


def _outcome(item: dict[str, Any]) -> bool | None:
    """True = success, False = failure, None = unknown/not applicable."""
    kind = item.get("type")
    if kind not in _TOOL_TYPES:
        return None
    status = item.get("status")
    if status in {"declined", "denied", "cancelled", "canceled"}:
        return False
    if kind == "commandExecution":
        code = item.get("exitCode")
        if type(code) is not int:
            return False if status == "failed" else None
        # A native search returning no matches is not an execution failure.
        actions = item.get("commandActions")
        if code == 1 and isinstance(actions, list) and actions and all(
            isinstance(action, dict) and action.get("type") == "search"
            for action in actions
        ):
            return None
        return code == 0
    if status in _FAILED:
        return False
    if kind == "dynamicToolCall":
        value = item.get("success")
        return value if isinstance(value, bool) else None
    if kind == "mcpToolCall":
        result = item.get("result")
        if item.get("error") is not None or (
            isinstance(result, dict) and result.get("isError") is True
        ):
            return False
    return True if status == "completed" else None


def _fingerprint(item: dict[str, Any]) -> str:
    kind = item["type"]
    if kind == "commandExecution":
        operation = (kind, item.get("cwd"), item.get("command"))
    elif kind == "fileChange":
        operation = (kind, item.get("changes"))
    else:
        operation = (
            kind, item.get("server"), item.get("namespace"), item.get("tool"),
            item.get("arguments"),
        )
    payload = json.dumps(operation, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class TurnHooks:
    """Request-local state shared by the initial turn and its single review."""

    def __init__(self) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._outcomes: OrderedDict[str, bool] = OrderedDict()
        self._recent: deque[str | None] = deque(maxlen=12)
        self.pending_steps = 0
        self.recovery_nudges = 0
        self.completion_reviews = 0

    @property
    def failure_count(self) -> int:
        return sum(not success for success in self._outcomes.values())

    def observe_plan(self, plan: object) -> None:
        if not isinstance(plan, list) or not all(
            isinstance(step, dict)
            and step.get("status") in {"pending", "inProgress", "completed"}
            for step in plan
        ):
            return
        self.pending_steps = sum(step["status"] != "completed" for step in plan)

    def observe_item(
        self, item: dict[str, Any], *, turn_id: str, allow_recovery: bool = True,
    ) -> str | None:
        outcome = _outcome(item)
        if outcome is None:
            return None
        # Ignore duplicate notifications and terminal snapshots. Items without
        # an id cannot safely be counted as distinct executions.
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            return None
        event_key = f"{turn_id}:{item_id}"
        if event_key in self._seen:
            return None
        self._seen[event_key] = None
        if len(self._seen) > 512:
            self._seen.popitem(last=False)
        fingerprint = _fingerprint(item)
        self._outcomes[fingerprint] = outcome
        self._outcomes.move_to_end(fingerprint)
        if len(self._outcomes) > 128:
            self._outcomes.popitem(last=False)
        if outcome:
            # A success breaks the failure streak for this operation, even
            # when other calls were interleaved.
            self._recent = deque(
                (None if key == fingerprint else key for key in self._recent),
                maxlen=12,
            )
        denied = item.get("status") in {"declined", "denied"}
        self._recent.append(fingerprint if not outcome and not denied else None)
        if allow_recovery and self.recovery_nudges == 0 and self._recent.count(fingerprint) >= 3:
            self.recovery_nudges += 1
            return RECOVERY_PROMPT
        return None

    def completion_prompt(self) -> str | None:
        if self.completion_reviews or not (self.failure_count or self.pending_steps):
            return None
        self.completion_reviews += 1
        return _COMPLETION_PROMPT.format(
            failures=self.failure_count, pending=self.pending_steps,
        )
