"""Model-specific settings for replaying mutable conversation history."""

from services.claude_api.client import THINKING_CONFIG


THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"


def requires_adaptive_thinking(model: str) -> bool:
    return model == "claude-opus-5-5"


def model_betas(model: str, betas: list[str]) -> list[str]:
    result = list(betas)
    if requires_adaptive_thinking(model) and THINKING_BINDING_BETA not in result:
        result.append(THINKING_BINDING_BETA)
    return result


def model_thinking(model: str, *, disable: bool = False) -> dict:
    if requires_adaptive_thinking(model):
        return {
            "type": "adaptive",
            "display": "summarized",
            "block_binding": {"prefix_mismatch_behavior": "drop_block"},
        }
    if disable:
        return {"type": "disabled"}
    return dict(THINKING_CONFIG)
