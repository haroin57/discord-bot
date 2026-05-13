"""Tests for services.emotion.analyzer — Haiku-driven interaction classification."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.emotion.analyzer import _FALLBACK, _normalize, analyze_interaction


def _make_response(text: str):
    """Build a minimal fake AsyncAnthropic .messages.create() response."""
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


@pytest.fixture()
def patch_anthropic(monkeypatch):
    """Patch services.emotion.analyzer's lazy import of get_anthropic_client."""
    import services.mem0.client as client_mod
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock()
    monkeypatch.setattr(client_mod, "get_anthropic_client", AsyncMock(return_value=fake_client))
    return fake_client


class TestNormalize:
    def test_full_valid_payload(self):
        out = _normalize({
            "event_type": "long_good_chat",
            "intensity": 0.8,
            "topics": ["仕事", "映画"],
            "intimacy_level": 0.9,
            "senpai_engagement": 0.7,
            "conflict_detected": False,
        })
        assert out["event_type"] == "long_good_chat"
        assert out["intensity"] == 0.8
        assert out["topics"] == ["仕事", "映画"]

    def test_unknown_event_type_collapses_to_short_reply(self):
        out = _normalize({"event_type": "weird_thing"})
        assert out["event_type"] == "short_reply"

    def test_numeric_out_of_range_clamped(self):
        out = _normalize({
            "event_type": "praised",
            "intensity": 1.5,
            "intimacy_level": -0.3,
        })
        assert out["intensity"] == 1.0
        assert out["intimacy_level"] == 0.0

    def test_malformed_numeric_falls_back_to_half(self):
        out = _normalize({
            "event_type": "praised",
            "intensity": "not-a-number",
        })
        assert out["intensity"] == 0.5

    def test_topics_capped_at_three(self):
        out = _normalize({
            "event_type": "praised",
            "topics": ["a", "b", "c", "d", "e"],
        })
        assert out["topics"] == ["a", "b", "c"]

    def test_topics_not_a_list_becomes_empty(self):
        out = _normalize({"event_type": "praised", "topics": "stringy"})
        assert out["topics"] == []


class TestAnalyzeInteraction:
    @pytest.mark.asyncio
    async def test_returns_fallback_when_client_unavailable(self, monkeypatch):
        import services.mem0.client as client_mod
        monkeypatch.setattr(client_mod, "get_anthropic_client", AsyncMock(return_value=None))
        out = await analyze_interaction("hi", "yo", "")
        assert out == _FALLBACK

    @pytest.mark.asyncio
    async def test_returns_fallback_on_haiku_exception(self, patch_anthropic):
        patch_anthropic.messages.create.side_effect = RuntimeError("upstream down")
        out = await analyze_interaction("hi", "yo", "")
        assert out == _FALLBACK

    @pytest.mark.asyncio
    async def test_returns_fallback_on_garbage_output(self, patch_anthropic):
        patch_anthropic.messages.create.return_value = _make_response("not json at all")
        out = await analyze_interaction("hi", "yo", "")
        assert out == _FALLBACK

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self, patch_anthropic):
        payload = '```json\n{"event_type":"praised","intensity":0.7}\n```'
        patch_anthropic.messages.create.return_value = _make_response(payload)
        out = await analyze_interaction("最高でした", "ありがとう", "")
        assert out["event_type"] == "praised"
        assert out["intensity"] == 0.7

    @pytest.mark.asyncio
    async def test_happy_path(self, patch_anthropic):
        payload = (
            '{"event_type":"long_good_chat","intensity":0.85,'
            '"topics":["映画","仕事"],"intimacy_level":0.7,'
            '"senpai_engagement":0.9,"conflict_detected":false}'
        )
        patch_anthropic.messages.create.return_value = _make_response(payload)
        out = await analyze_interaction("長文", "長文返し", "直近")
        assert out["event_type"] == "long_good_chat"
        assert out["topics"] == ["映画", "仕事"]
        assert out["conflict_detected"] is False

    @pytest.mark.asyncio
    async def test_truncates_long_inputs(self, patch_anthropic):
        """User and assistant texts must be capped before prompt formatting
        so we don't ship a 100KB conversation to Haiku."""
        import re

        patch_anthropic.messages.create.return_value = _make_response(
            '{"event_type":"short_reply","intensity":0.5}'
        )
        await analyze_interaction("x" * 5000, "y" * 5000, "z" * 5000)
        call_kwargs = patch_anthropic.messages.create.call_args.kwargs
        prompt_text = call_kwargs["messages"][0]["content"]
        # The prompt template itself contains a handful of stray "y" / "z"
        # letters (e.g. "event_type", "intensity"), so a raw `.count()` is
        # too tight. Assert instead that no run of 501+ identical filler
        # chars survives — `_truncate(..., 500)` would have shortened it.
        assert re.search(r"x{501,}", prompt_text) is None
        assert re.search(r"y{501,}", prompt_text) is None
        assert re.search(r"z{501,}", prompt_text) is None
