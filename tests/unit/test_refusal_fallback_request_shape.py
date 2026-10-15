"""Request-shape guards for the model-agnostic refusal fallback (ADR-0054).

Enabling the refusal retry outside the Fable family must NOT start declaring
the server-side `fallbacks` body field on those models: Opus 4.8 rejects that
field with 400 invalid_request (2026-07-14 "整形中" hang) and Opus 5.5 is
unverified, so an unsupported field would fail EVERY request rather than only
the refusals it is meant to rescue.
"""

from services.claude_api.request_shape import _build_stream_kwargs_impl


def _options(model: str) -> dict:
    return _build_stream_kwargs_impl(
        current_model=model,
        cached_system=[],
        messages=[{"role": "user", "content": "hi"}],
        extra_betas=[],
        output_config_override=None,
        refusal_fallback_model="claude-opus-4-8",
        tools_for_api=[],
        extra_headers=None,
        fallback_used=False,
        server_side_fallback_enabled=True,
    )


def test_opus55_refusal_target_does_not_declare_server_side_fallbacks():
    assert "fallbacks" not in _options("claude-opus-5-5")["extra_body"]


def test_fable_primary_still_declares_server_side_fallbacks():
    options = _options("claude-fable-5-1")
    assert options["extra_body"]["fallbacks"] == [{"model": "claude-opus-4-8"}]
