"""Content-free provider usage telemetry for single-shot helper completions."""

from __future__ import annotations

import hashlib
import logging


log = logging.getLogger(__name__)
FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
COMPATIBLE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_prompt_tokens", "reasoning_tokens")


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _counter(value):
    return value if type(value) is int and value >= 0 else "-"


def _request_id(response) -> str:
    for name in ("_request_id", "id"):
        value = _field(response, name)
        if isinstance(value, str) and value and not any(character.isspace() for character in value):
            return value
    return "-"


def log_helper_usage(response, *, model: str, task_prompt: str,
                     user_chars: int, duration_ms: int) -> None:
    usage = getattr(response, "usage", None)
    values = {}
    for field in FIELDS:
        value = usage.get(field) if isinstance(usage, dict) else getattr(usage, field, None)
        values[field] = value if type(value) is int and value >= 0 else None
    text_chars = sum(len(block.text) for block in (getattr(response, "content", None) or [])
                     if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str))
    task_id = hashlib.sha256(task_prompt.encode()).hexdigest()[:16]
    log.info(
        "llm_helper_usage model=%s task_id=%s input_tokens=%s "
        "cache_read_input_tokens=%s cache_creation_input_tokens=%s output_tokens=%s "
        "duration_ms=%d user_chars=%d task_chars=%d output_text_chars=%d stop_reason=%s request_id=%s",
        model, task_id, *(values[field] if values[field] is not None else "-" for field in FIELDS),
        duration_ms, user_chars, len(task_prompt), text_chars,
        getattr(response, "stop_reason", None) or "-",
        _request_id(response),
    )


def log_compatible_helper_usage(response, *, model: str, task_prompt: str,
                                user_chars: int, duration_ms: int) -> None:
    usage = _field(response, "usage")
    values = {field: _field(usage, field) for field in COMPATIBLE_FIELDS[:3]}
    values["cached_prompt_tokens"] = _field(_field(usage, "prompt_tokens_details"), "cached_tokens")
    values["reasoning_tokens"] = _field(_field(usage, "completion_tokens_details"), "reasoning_tokens")
    choices = _field(response, "choices")
    first_choice = choices[0] if isinstance(choices, (list, tuple)) and choices else None
    content = _field(_field(first_choice, "message"), "content")
    text_chars = len(content) if isinstance(content, str) else "-"
    task_id = hashlib.sha256(task_prompt.encode()).hexdigest()[:16]
    log.info(
        "llm_compatible_helper_usage model=%s task_id=%s prompt_tokens=%s "
        "completion_tokens=%s total_tokens=%s cached_prompt_tokens=%s reasoning_tokens=%s "
        "duration_ms=%d user_chars=%d task_chars=%d output_text_chars=%s finish_reason=%s request_id=%s",
        model, task_id, *(_counter(values[field]) for field in COMPATIBLE_FIELDS),
        duration_ms, user_chars, len(task_prompt), text_chars,
        _field(first_choice, "finish_reason") or "-", _request_id(response),
    )


def log_helper_failure(*, route: str, model: str, task_prompt: str, user_chars: int,
                       duration_ms: int, outcome: str, error: BaseException | None = None) -> None:
    task_id = hashlib.sha256(task_prompt.encode()).hexdigest()[:16]
    log.info(
        "llm_helper_failure route=%s model=%s task_id=%s outcome=%s error_type=%s "
        "duration_ms=%d user_chars=%d task_chars=%d usage=unknown",
        route, model, task_id, outcome, type(error).__name__ if error is not None else "-",
        duration_ms, user_chars, len(task_prompt),
    )
