"""Anthropic request-shape helpers shared by the direct API tool loop."""

from config import CLAUDE_MAX_TOKENS
from services.claude_api.client import (
    CLAUDE_MAX_TOKENS_OPUS,
    CLI_DEFAULT_SYSTEM_PROMPT,
    CLI_IDENTITY_TEXT,
    CONTEXT_MANAGEMENT_CONFIG,
    OUTPUT_CONFIG,
    THINKING_CONFIG,
    get_attribution_header_value,
    get_metadata,
)


REFUSAL_FALLBACK_HEADER = "x-is-refusal-fallback"


def _build_cached_system(system_prompt: str, messages: list[dict]) -> list[dict]:
    """Build the four cached system blocks used by the Claude CLI shape."""
    attribution = get_attribution_header_value(messages)
    return [
        {"type": "text", "text": attribution},
        {"type": "text", "text": CLI_IDENTITY_TEXT},
        {
            "type": "text",
            "text": CLI_DEFAULT_SYSTEM_PROMPT,
            "cache_control": {
                "type": "ephemeral",
                "ttl": "1h",
                "scope": "global",
            },
        },
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
    ]


def _mark_cache_breakpoint(msg: dict) -> dict:
    """Return a copy of ``msg`` with a ttl:1h breakpoint on its last block."""
    msg = dict(msg)
    content = msg["content"]
    if isinstance(content, str):
        msg["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
        ]
    elif isinstance(content, list) and content:
        content = list(content)
        last_block = dict(content[-1])
        last_block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        content[-1] = last_block
        msg["content"] = content
    return msg


def _inject_cache_breakpoints(messages: list[dict]) -> list[dict]:
    """Add CLI-compatible breakpoints to the two newest user messages."""
    user_indices = [i for i, message in enumerate(messages) if message["role"] == "user"]
    if not user_indices:
        return messages
    messages = list(messages)
    targets = {user_indices[-1]}
    if len(user_indices) >= 2:
        targets.add(user_indices[-2])
    for target_idx in targets:
        messages[target_idx] = _mark_cache_breakpoint(messages[target_idx])
    return messages


def _build_stream_kwargs_impl(
    *,
    current_model: str,
    cached_system: list[dict],
    messages: list[dict],
    extra_betas,
    output_config_override,
    refusal_fallback_model: str | None,
    tools_for_api,
    extra_headers,
    fallback_used: bool,
    server_side_fallback_enabled: bool,
    refusal_fallback_header: str = REFUSAL_FALLBACK_HEADER,
) -> dict:
    """Assemble the per-turn ``messages.stream`` keyword arguments."""
    max_tokens = (
        CLAUDE_MAX_TOKENS_OPUS
        if "opus" in (current_model or "").lower()
        else CLAUDE_MAX_TOKENS
    )
    stream_kwargs = dict(
        model=current_model,
        max_tokens=max_tokens,
        system=cached_system,
        messages=messages,
        betas=extra_betas,
        metadata=get_metadata(),
        thinking=THINKING_CONFIG,
        output_config=output_config_override or OUTPUT_CONFIG,
        extra_body={"context_management": CONTEXT_MANAGEMENT_CONFIG},
    )
    if server_side_fallback_enabled and refusal_fallback_model:
        stream_kwargs["extra_body"]["fallbacks"] = [
            {"model": refusal_fallback_model}
        ]
    if tools_for_api:
        stream_kwargs["tools"] = tools_for_api
    merged_headers = dict(extra_headers or {})
    if fallback_used:
        merged_headers[refusal_fallback_header] = "true"
    if merged_headers:
        stream_kwargs["extra_headers"] = merged_headers
    return stream_kwargs
