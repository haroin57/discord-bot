"""Tests for services.emotion.daily — DailyEntry, load/save, format."""

import json

import pytest

from services.emotion.daily import (
    DAILY_BUFFER_SIZE,
    DailyEntry,
    format_recent_entries,
    load_daily,
    load_summary,
    save_daily,
    save_summary,
)

# ---------------------------------------------------------------------------
# Patch atomic_write so tests write real files without the fsync/replace dance
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_atomic_write(monkeypatch):
    from pathlib import Path

    def _write_json(path, data):
        Path(path).write_text(json.dumps(data), encoding="utf-8")

    def _write_text(path, text):
        Path(path).write_text(text, encoding="utf-8")

    monkeypatch.setattr("services.emotion.daily.atomic_write_json", _write_json)
    monkeypatch.setattr("services.emotion.daily.atomic_write_text", _write_text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(ts="2026-05-15T10:00:00+09:00", **kwargs) -> DailyEntry:
    defaults = dict(
        event_type="routine",
        importance=3,
        activity="コーヒーを飲む",
        detail="朝のルーティン",
        mood="normal",
        thought="特になし",
    )
    defaults.update(kwargs)
    return DailyEntry(ts=ts, **defaults)


# ---------------------------------------------------------------------------
# DailyEntry
# ---------------------------------------------------------------------------

class TestDailyEntry:
    def test_daily_entry_roundtrip(self):
        e = _entry(activity="作曲", mood="creative", importance=5)
        restored = DailyEntry.from_dict(e.to_dict())
        assert restored == e

    def test_daily_entry_from_dict_defaults(self):
        e = DailyEntry.from_dict({})
        assert e.ts == ""
        assert e.event_type == "routine"
        assert e.importance == 3
        assert e.activity == ""
        assert e.detail == ""
        assert e.mood == "normal"
        assert e.thought == ""


# ---------------------------------------------------------------------------
# load_daily
# ---------------------------------------------------------------------------

class TestLoadDaily:
    def test_load_daily_missing_file(self, tmp_path):
        result = load_daily(tmp_path)
        assert result == []

    def test_load_daily_valid_file(self, tmp_path):
        e = _entry(activity="散歩")
        data = {"entries": [e.to_dict()]}
        (tmp_path / "kanae_daily.json").write_text(
            json.dumps(data), encoding="utf-8"
        )
        result = load_daily(tmp_path)
        assert len(result) == 1
        assert result[0].activity == "散歩"

    def test_load_daily_corrupt_json(self, tmp_path):
        (tmp_path / "kanae_daily.json").write_text("{not valid", encoding="utf-8")
        result = load_daily(tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
# save_daily
# ---------------------------------------------------------------------------

class TestSaveDaily:
    def test_save_daily_truncates_to_buffer_size(self, tmp_path):
        entries = [_entry(ts=f"2026-05-15T{i:02d}:00:00+09:00") for i in range(200)]
        save_daily(entries, tmp_path)
        loaded = load_daily(tmp_path)
        assert len(loaded) == DAILY_BUFFER_SIZE

    def test_save_daily_keeps_most_recent(self, tmp_path):
        entries = [
            _entry(ts=f"2026-05-15T{i:02d}:00:00+09:00", activity=f"act_{i}")
            for i in range(100)
        ]
        save_daily(entries, tmp_path)
        loaded = load_daily(tmp_path)
        # should be the last DAILY_BUFFER_SIZE entries
        assert loaded[0].activity == f"act_{100 - DAILY_BUFFER_SIZE}"
        assert loaded[-1].activity == "act_99"


# ---------------------------------------------------------------------------
# load_summary / save_summary
# ---------------------------------------------------------------------------

class TestSummary:
    def test_load_summary_missing(self, tmp_path):
        assert load_summary(tmp_path) == ""

    def test_save_load_summary_roundtrip(self, tmp_path):
        text = "今日は作曲に集中した。いい感じ。"
        save_summary(text, tmp_path)
        assert load_summary(tmp_path) == text


# ---------------------------------------------------------------------------
# format_recent_entries
# ---------------------------------------------------------------------------

class TestFormatRecentEntries:
    def test_format_recent_entries_empty(self):
        assert format_recent_entries([]) == "(まだ日常ログがありません)"

    def test_format_recent_entries_limits_count(self):
        entries = [_entry(ts=f"2026-05-15T{i:02d}:00:00+09:00") for i in range(10)]
        output = format_recent_entries(entries, count=3)
        lines = output.strip().splitlines()
        assert len(lines) == 3

    def test_format_recent_entries_reversed(self):
        entries = [
            _entry(ts=f"2026-05-15T{i:02d}:00:00+09:00", activity=f"act_{i}")
            for i in range(5)
        ]
        output = format_recent_entries(entries, count=5)
        lines = output.strip().splitlines()
        # reversed(recent) → most recent timestamp first
        assert "act_4" in lines[0]
        assert "act_0" in lines[-1]
