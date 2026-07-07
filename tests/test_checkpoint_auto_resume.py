"""Tests for ADR-0040 — graceful checkpoint auto-resume.

Covers the three separable pieces:
- reason→cap policy (entry.auto_resume_cap)
- checkpoint-note stripping for segment joins (entry.strip_checkpoint_note)
- note selection on the checkpoint reply itself
  (tool_loop._build_checkpoint_result / _emit_checkpoint's policy hook)
"""

from __future__ import annotations

import services.claude_api.tool_loop as tl
from services.claude_api.entry import auto_resume_cap, strip_checkpoint_note
from services.claude_api.tool_loop import (
    _AUTO_RESUME_NOTE,
    _CHECKPOINT_NOTE,
    AUTO_RESUME_DIRECTIVE,
    _build_checkpoint_result,
)


class TestAutoResumeCap:
    def test_turn_ceiling_uses_default_max(self):
        assert auto_resume_cap("turn_ceiling", default_max=2, budget_max=1) == 2

    def test_stream_stall_uses_default_max(self):
        assert auto_resume_cap("stream_stall", default_max=2, budget_max=1) == 2

    def test_budget_reasons_use_budget_max(self):
        assert auto_resume_cap("wallclock", default_max=2, budget_max=1) == 1
        assert auto_resume_cap("output_tokens", default_max=2, budget_max=1) == 1

    def test_tool_loop_never_resumes(self):
        # ループ検知で止まったのに自動継続したらループが再開するだけ。
        assert auto_resume_cap("tool_loop", default_max=99, budget_max=99) == 0

    def test_unknown_reason_never_resumes(self):
        assert auto_resume_cap("something_new", default_max=2, budget_max=1) == 0
        assert auto_resume_cap("", default_max=2, budget_max=1) == 0
        assert auto_resume_cap(None, default_max=2, budget_max=1) == 0

    def test_budget_max_clamped_by_default_max(self):
        # 予算系の上限が全体上限を超えることはない。
        assert auto_resume_cap("wallclock", default_max=1, budget_max=5) == 1


class TestStripCheckpointNote:
    def test_strips_manual_note(self):
        text = "ここまでの成果です。\n\n" + _CHECKPOINT_NOTE
        assert strip_checkpoint_note(text) == "ここまでの成果です。"

    def test_strips_auto_note(self):
        text = "ここまでの成果です。\n\n" + _AUTO_RESUME_NOTE
        assert strip_checkpoint_note(text) == "ここまでの成果です。"

    def test_note_only_reply_becomes_empty(self):
        # 本文なし checkpoint はセグメントごと落とせるよう空文字列に。
        text = "⏸️ 作業をここまで保存しました。「続けて」で同じ文脈から再開できます。"
        assert strip_checkpoint_note(text) == ""

    def test_plain_text_passes_through(self):
        assert strip_checkpoint_note("普通の応答") == "普通の応答"

    def test_none_safe(self):
        assert strip_checkpoint_note(None) == ""


class TestCheckpointNoteSelection:
    def _kwargs(self, **over):
        base = dict(
            text_across_turns=["部分成果"],
            last_text="部分成果",
            fallback_used=False,
            fallback_note_sent_live=False,
            current_model="claude-fable-5",
            active_mcp_keys=[],
            prior_len=0,
            usage_totals={},
            paused_reason="turn_ceiling",
        )
        base.update(over)
        return base

    def test_manual_note_by_default(self):
        result = _build_checkpoint_result(**self._kwargs())
        assert _CHECKPOINT_NOTE in result.text
        assert _AUTO_RESUME_NOTE not in result.text

    def test_auto_note_when_resume_planned(self):
        result = _build_checkpoint_result(
            **self._kwargs(auto_resume_planned=True)
        )
        assert _AUTO_RESUME_NOTE in result.text
        assert "「続けて」" not in result.text

    def test_empty_body_auto_variant(self):
        result = _build_checkpoint_result(
            **self._kwargs(
                text_across_turns=[], last_text="", auto_resume_planned=True,
            )
        )
        assert "自動で実行します" in result.text
        assert "「続けて」" not in result.text

    def test_paused_reason_carried(self):
        result = _build_checkpoint_result(**self._kwargs())
        assert result.paused_reason == "turn_ceiling"
        assert result.synthetic_session_id  # checkpoint keeps a real session


class TestConfigDefaults:
    def test_defaults(self, monkeypatch):
        import importlib
        for var in (
            "CHECKPOINT_AUTO_RESUME_ENABLED",
            "CHECKPOINT_AUTO_RESUME_MAX",
            "CHECKPOINT_AUTO_RESUME_BUDGET_MAX",
        ):
            monkeypatch.delenv(var, raising=False)
        import config
        importlib.reload(config)
        try:
            assert config.CHECKPOINT_AUTO_RESUME_ENABLED is True
            assert config.CHECKPOINT_AUTO_RESUME_MAX == 2
            assert config.CHECKPOINT_AUTO_RESUME_BUDGET_MAX == 1
        finally:
            importlib.reload(config)


class TestDirective:
    def test_directive_is_a_user_instruction(self):
        # 再開ターンは通常の user 指示として履歴に乗る — 再説明・やり直しを
        # 抑止する文言が入っていること（再開後の重複作業防止）。
        assert "続けてください" in AUTO_RESUME_DIRECTIVE
        assert "やり直し" in AUTO_RESUME_DIRECTIVE

    def test_notes_are_distinct(self):
        assert tl._CHECKPOINT_NOTE != tl._AUTO_RESUME_NOTE
