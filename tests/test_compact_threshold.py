"""TDD Feature B: model-window-based compaction threshold (CLI et6(K) port).

CLI: et6(K)=ZQ(K)-Pe1, ZQ(K)=min(window,cap)-output_reserve. We mirror it —
fire near the effective window (1M with the context-1m beta, else 200K) minus
output + compaction headroom — with a percent override knob to dial cost.
"""
from unittest.mock import patch

import config
from services.claude_api.compaction import (
    compact_threshold_tokens,
    effective_context_window,
)


class TestEffectiveWindow:
    def test_1m_when_beta_on(self):
        with patch.object(config, "CONTEXT_1M_ENABLED", True):
            assert effective_context_window() == 1_000_000

    def test_200k_when_beta_off(self):
        with patch.object(config, "CONTEXT_1M_ENABLED", False):
            assert effective_context_window() == 200_000


class TestCompactThreshold:
    def test_reserve_based_1m_near_full_below_window(self):
        with patch.object(config, "CONTEXT_1M_ENABLED", True), \
             patch.object(config, "HISTORY_COMPACT_PCT_OVERRIDE", 0), \
             patch.object(config, "CLAUDE_MAX_TOKENS", 64000):
            thr = compact_threshold_tokens()
        assert 800_000 < thr < 1_000_000  # near full but with headroom

    def test_reserve_based_200k_below_window(self):
        with patch.object(config, "CONTEXT_1M_ENABLED", False), \
             patch.object(config, "HISTORY_COMPACT_PCT_OVERRIDE", 0), \
             patch.object(config, "CLAUDE_MAX_TOKENS", 64000):
            thr = compact_threshold_tokens()
        assert 0 < thr < 200_000  # never exceeds the 200K window

    def test_pct_override_takes_precedence(self):
        with patch.object(config, "CONTEXT_1M_ENABLED", True), \
             patch.object(config, "HISTORY_COMPACT_PCT_OVERRIDE", 50):
            assert compact_threshold_tokens() == 500_000

    def test_pct_override_30pct_of_1m(self):
        with patch.object(config, "CONTEXT_1M_ENABLED", True), \
             patch.object(config, "HISTORY_COMPACT_PCT_OVERRIDE", 30):
            assert compact_threshold_tokens() == 300_000


def test_compact_instructions_has_text_only_preamble():
    """Feature C: COMPACT_INSTRUCTIONS must lead with the CLI's TEXT-ONLY
    preamble so the summarizer never wastes its one turn on a tool call."""
    from services.claude_api.compaction import COMPACT_INSTRUCTIONS
    head = COMPACT_INSTRUCTIONS[:500]
    assert "TEXT ONLY" in head
    assert "Do NOT call any tools" in head
    # preamble comes before the body
    assert COMPACT_INSTRUCTIONS.index("TEXT ONLY") < COMPACT_INSTRUCTIONS.index(
        "detailed summary of the conversation")
