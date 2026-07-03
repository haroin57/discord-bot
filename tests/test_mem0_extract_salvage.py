"""TDD: truncated-JSON salvage + max_tokens(env) + retry for extract.py.

Root cause (verified in prod logs 2026-06-20/22): ``extract_full_signals``
emitted JSON that got cut off mid-string ("Unterminated string ...") and the
whole batch's facts were dropped (``{"facts":[],"relations":[]}``) with no
salvage/retry. Real lost facts included "ニッスイ株をホールド…", a Goldman
Sachs investment sim, an FRED research habit. These tests pin the fix:

1. ``_salvage_json_objects`` recovers every *complete* object from a truncated
   JSON array, dropping only the cut tail.
2. ``extract_full_signals`` uses it on JSONDecodeError, and retries once when
   nothing could be salvaged (the early-cut case).
3. ``max_tokens`` is read from ``MEM0_EXTRACT_MAX_TOKENS`` (env), not hardcoded.
"""

import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ensure_stub(name: str, **attrs):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
    return sys.modules[name]


_ensure_stub("services.oauth_manager",
             ensure_valid_token=AsyncMock(),
             get_access_token=MagicMock(return_value="tok"),
             init=MagicMock(),
             make_async_oauth_client=MagicMock(),
             prime_subscriber_session=AsyncMock(),
             get_client=MagicMock(),
             OAuthRefreshFailed=type("OAuthRefreshFailed", (Exception,), {}),
             failback_to_primary=MagicMock(),
             failover_to_secondary=AsyncMock(),
             get_active_account=MagicMock(return_value="primary"),
             maybe_restore_primary=AsyncMock())

import services.mem0.cli_compat  # noqa: E402,F401  (ensure submodule is importable for patch())
from services.mem0.extract import (  # noqa: E402
    _salvage_json_objects,
    _salvage_facts,
    extract_full_signals,
)


def _make_resp(text: str, stop_reason: str = "end_turn"):
    item = MagicMock()
    item.text = text
    resp = MagicMock()
    resp.content = [item]
    resp.stop_reason = stop_reason
    return resp


# ===========================================================================
# Pure helper: _salvage_json_objects
# ===========================================================================

class TestSalvageJsonObjects:
    def test_recovers_complete_objects_dropping_cut_tail(self):
        # Two complete fact objects, third cut mid-string (the real failure mode).
        text = (
            '{"facts":[{"speaker":"self","text":"ニッスイ株をホールド"},'
            '{"speaker":"self","text":"円安が続くと見ている"},'
            '{"speaker":"self","text":"ゴールドマン・サックスの投資シミュ'
        )
        objs = _salvage_json_objects(text, "facts")
        assert len(objs) == 2
        assert objs[0]["text"] == "ニッスイ株をホールド"
        assert objs[1]["text"] == "円安が続くと見ている"

    def test_recovers_all_from_complete_array(self):
        text = json.dumps({"facts": [{"a": 1}, {"a": 2}, {"a": 3}]})
        objs = _salvage_json_objects(text, "facts")
        assert len(objs) == 3

    def test_missing_key_returns_empty(self):
        assert _salvage_json_objects('{"relations":[{"x":1}]}', "facts") == []

    def test_not_confused_by_braces_in_string_values(self):
        # Braces/brackets inside a string value must not break depth tracking.
        text = '{"facts":[{"text":"f(x){ return [a,b]; }","ok":true},{"text":"second"'
        objs = _salvage_json_objects(text, "facts")
        assert len(objs) == 1
        assert objs[0]["ok"] is True

    def test_handles_escaped_quotes_in_strings(self):
        text = '{"facts":[{"text":"言った\\"円安\\"と"},{"text":"次は'
        objs = _salvage_json_objects(text, "facts")
        assert len(objs) == 1
        assert '円安' in objs[0]["text"]

    def test_empty_array(self):
        assert _salvage_json_objects('{"facts":[]}', "facts") == []

    def test_relations_key(self):
        text = '{"facts":[{"speaker":"self","text":"x"}],"relations":[{"src":"A","rel_type":"loves","dst":"B"}]}'
        rels = _salvage_json_objects(text, "relations")
        assert len(rels) == 1
        assert rels[0]["src"] == "A"


class TestSalvageFacts:
    def test_returns_both_arrays(self):
        text = (
            '{"facts":[{"speaker":"self","text":"a"}],'
            '"relations":[{"src":"A","rel_type":"loves","dst":"B"}]}'
        )
        out = _salvage_facts(text)
        assert len(out["facts"]) == 1
        assert len(out["relations"]) == 1

    def test_truncated_relations_dropped(self):
        text = '{"facts":[{"speaker":"self","text":"a"}],"relations":[{"src":"A"'
        out = _salvage_facts(text)
        assert len(out["facts"]) == 1
        assert out["relations"] == []


# ===========================================================================
# Integration: extract_full_signals salvage + retry + max_tokens
# ===========================================================================

class TestExtractFullSignalsSalvage:
    @pytest.mark.asyncio
    async def test_salvages_facts_from_truncated_response(self):
        truncated = (
            '{"facts":[{"speaker":"self","text":"ニッスイ株をホールド","importance":3,'
            '"category":"invest","subcategory":"equity","tags":["ニッスイ"]},'
            '{"speaker":"self","text":"円安が続くと見ている","importance":3,'
            '"category":"invest","subcategory":"macro","tags":[]},'
            '{"speaker":"self","text":"切れた途中の'  # cut mid-object
        )
        client = MagicMock()
        with patch("services.mem0.client.get_anthropic_client", AsyncMock(return_value=client)), \
             patch("services.mem0.cli_compat.cli_messages_create",
                   AsyncMock(return_value=_make_resp(truncated, stop_reason="max_tokens"))), \
             patch("config.MEM0_GATE_MODEL", "claude-haiku-4-5-20251001"):
            out = await extract_full_signals("ニッスイの話", "")
        texts = [f["text"] for f in out["facts"]]
        assert "ニッスイ株をホールド" in texts
        assert "円安が続くと見ている" in texts
        assert len(out["facts"]) == 2  # the cut tail is dropped, not the whole batch

    @pytest.mark.asyncio
    async def test_retries_once_when_nothing_salvageable(self):
        # First response is cut so early nothing can be salvaged; retry succeeds.
        early_cut = '{"facts":[{"speaker":"self","text":"ニッスイ株をホールドしており、「円安が'
        good = json.dumps({"facts": [
            {"speaker": "self", "text": "ニッスイ株をホールド", "importance": 3,
             "category": "invest", "subcategory": "equity", "tags": []},
        ], "relations": []})
        client = MagicMock()
        with patch("services.mem0.client.get_anthropic_client", AsyncMock(return_value=client)), \
             patch("services.mem0.cli_compat.cli_messages_create",
                   AsyncMock(side_effect=[_make_resp(early_cut, stop_reason="max_tokens"),
                                          _make_resp(good)])) as mock_create, \
             patch("config.MEM0_GATE_MODEL", "claude-haiku-4-5-20251001"):
            out = await extract_full_signals("ニッスイの話", "")
        assert mock_create.await_count == 2  # retried
        assert len(out["facts"]) == 1
        assert out["facts"][0]["text"] == "ニッスイ株をホールド"

    @pytest.mark.asyncio
    async def test_max_tokens_read_from_env(self):
        captured = {}

        async def capture(*args, **kwargs):
            captured.update(kwargs)
            return _make_resp(json.dumps({"facts": [], "relations": []}))

        client = MagicMock()
        with patch("services.mem0.client.get_anthropic_client", AsyncMock(return_value=client)), \
             patch("services.mem0.cli_compat.cli_messages_create", capture), \
             patch("config.MEM0_GATE_MODEL", "claude-haiku-4-5-20251001"), \
             patch("config.MEM0_EXTRACT_MAX_TOKENS", 13333):
            await extract_full_signals("test", "")
        assert captured.get("max_tokens") == 13333

    @pytest.mark.asyncio
    async def test_input_not_truncated_below_env_cap(self):
        # The batch summary (~3500-4000 chars in prod) used to be cut to 2000
        # before the LLM saw it. With MEM0_EXTRACT_INPUT_CHARS the cap is wider.
        captured = {}

        async def capture(*args, **kwargs):
            captured.update(kwargs)
            return _make_resp(json.dumps({"facts": [], "relations": []}))

        long_input = "ニッスイ" * 800  # 3200 chars, well past the old 2000 cut
        client = MagicMock()
        with patch("services.mem0.client.get_anthropic_client", AsyncMock(return_value=client)), \
             patch("services.mem0.cli_compat.cli_messages_create", capture), \
             patch("config.MEM0_GATE_MODEL", "claude-haiku-4-5-20251001"), \
             patch("config.MEM0_EXTRACT_INPUT_CHARS", 6000):
            await extract_full_signals(long_input, "")
        msg = captured["user_message"]
        assert msg.count("ニッスイ") == 800  # full input survived, not cut at 2000

    @pytest.mark.asyncio
    async def test_clean_json_still_works(self):
        good = json.dumps({"facts": [
            {"speaker": "self", "text": "先輩はPythonが好き", "importance": 4,
             "category": "tech", "subcategory": "python", "tags": ["python"]},
        ], "relations": []})
        client = MagicMock()
        with patch("services.mem0.client.get_anthropic_client", AsyncMock(return_value=client)), \
             patch("services.mem0.cli_compat.cli_messages_create",
                   AsyncMock(return_value=_make_resp(good))) as mock_create, \
             patch("config.MEM0_GATE_MODEL", "claude-haiku-4-5-20251001"):
            out = await extract_full_signals("Pythonが好き", "そう")
        assert mock_create.await_count == 1  # no needless retry on success
        assert len(out["facts"]) == 1
