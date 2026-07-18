"""Regression tests for Claude Code request attestation."""

from __future__ import annotations

import importlib

import httpx


BODY_WITH_PLACEHOLDER = b'{"system":"cch=00000","messages":[]}'
EXPECTED_PATCHED_BODY = b'{"system":"cch=6b2bf","messages":[]}'
BODY_WITHOUT_PLACEHOLDER = b'{"system":"plain system prompt","messages":[]}'


def test_attestation_module_is_importable() -> None:
    module = importlib.import_module("services.claude_api.attestation")

    assert module.CCH_SEED == 0x6E52736AC806831E
    assert callable(module.patch_cch_body)
    assert callable(module.cch_request_hook)


def test_patch_cch_body_matches_fixed_vector_deterministically() -> None:
    from services.claude_api.attestation import patch_cch_body

    assert patch_cch_body(BODY_WITH_PLACEHOLDER) == EXPECTED_PATCHED_BODY
    assert patch_cch_body(BODY_WITH_PLACEHOLDER) == EXPECTED_PATCHED_BODY


def test_patch_cch_body_without_placeholder_is_unchanged() -> None:
    from services.claude_api.attestation import patch_cch_body

    result = patch_cch_body(BODY_WITHOUT_PLACEHOLDER)

    assert result is BODY_WITHOUT_PLACEHOLDER


async def test_cch_request_hook_matches_fixed_vector_and_updates_length() -> None:
    from services.claude_api.attestation import cch_request_hook

    request = httpx.Request(
        "POST",
        "https://api.anthropic.com/v1/messages",
        headers={"content-length": "999"},
        content=BODY_WITH_PLACEHOLDER,
    )

    await cch_request_hook(request)

    assert request.content == EXPECTED_PATCHED_BODY
    assert request.headers["content-length"] == "36"


async def test_cch_request_hook_without_placeholder_is_unchanged() -> None:
    from services.claude_api.attestation import cch_request_hook

    request = httpx.Request(
        "POST",
        "https://api.anthropic.com/v1/messages",
        headers={"content-length": "777"},
        content=BODY_WITHOUT_PLACEHOLDER,
    )
    original_content = request.content

    await cch_request_hook(request)

    assert request.content is original_content
    assert request.headers["content-length"] == "777"
