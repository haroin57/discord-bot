"""Tests for services.emotion.timeseries — append, load, error handling."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.emotion.state import EmotionState
from services.emotion.timeseries import (
    TIMESERIES_FILENAME,
    append_timeseries,
    load_timeseries,
    timeseries_size_bytes,
)

JST = timezone(timedelta(hours=9))


def _make_state(**overrides) -> EmotionState:
    defaults = {
        "valence": 0.5,
        "arousal": 0.4,
        "loneliness": 0.3,
        "affection": 0.8,
        "curiosity": 0.6,
        "anxiety": 0.2,
        "updated_at": datetime.now(JST).isoformat(),
        "derived_labels": ["喜んでいる"],
        "labels": {"valence": "機嫌よし"},
        "emotion_journal": [
            {
                "ts": datetime.now(JST).isoformat(),
                "v": 0.5, "ar": 0.4, "lo": 0.3,
                "af": 0.8, "cu": 0.6, "ax": 0.2,
                "trigger": "実装完了",
                "source": "conversation",
            },
        ],
    }
    defaults.update(overrides)
    return EmotionState(**defaults)


class TestAppendTimeseries:
    def test_creates_file(self, tmp_path):
        state = _make_state()
        append_timeseries(state, tmp_path, source="conversation")
        ts_file = tmp_path / TIMESERIES_FILENAME
        assert ts_file.is_file()
        lines = ts_file.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_appends_line(self, tmp_path):
        state = _make_state()
        append_timeseries(state, tmp_path, source="conversation")
        append_timeseries(state, tmp_path, source="silence")
        ts_file = tmp_path / TIMESERIES_FILENAME
        lines = ts_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_record_schema(self, tmp_path):
        state = _make_state()
        append_timeseries(state, tmp_path, source="conversation")
        ts_file = tmp_path / TIMESERIES_FILENAME
        rec = json.loads(ts_file.read_text().strip())
        required = {
            "ts", "valence", "arousal", "loneliness",
            "affection", "curiosity", "anxiety", "source", "trigger",
        }
        assert required.issubset(rec.keys())
        assert rec["source"] == "conversation"
        assert rec["valence"] == pytest.approx(0.5)
        assert rec["trigger"] == "実装完了"

    def test_derived_labels_included(self, tmp_path):
        state = _make_state(derived_labels=["照れ気味", "興味津々"])
        append_timeseries(state, tmp_path, source="conversation")
        rec = json.loads((tmp_path / TIMESERIES_FILENAME).read_text().strip())
        assert rec["derived_labels"] == ["照れ気味", "興味津々"]

    def test_empty_labels_omitted(self, tmp_path):
        state = _make_state(derived_labels=[], labels={})
        append_timeseries(state, tmp_path, source="silence")
        rec = json.loads((tmp_path / TIMESERIES_FILENAME).read_text().strip())
        assert "derived_labels" not in rec
        assert "labels" not in rec

    def test_failure_no_raise(self, tmp_path):
        state = _make_state()
        ts_file = tmp_path / TIMESERIES_FILENAME
        ts_file.touch()
        os.chmod(ts_file, 0o000)
        try:
            append_timeseries(state, tmp_path, source="conversation")
        finally:
            os.chmod(ts_file, 0o644)

    def test_empty_journal_trigger(self, tmp_path):
        state = _make_state(emotion_journal=[])
        append_timeseries(state, tmp_path, source="silence")
        rec = json.loads((tmp_path / TIMESERIES_FILENAME).read_text().strip())
        assert rec["trigger"] == ""


class TestLoadTimeseries:
    def test_missing_file(self, tmp_path):
        assert load_timeseries(tmp_path) == []

    def test_empty_file(self, tmp_path):
        (tmp_path / TIMESERIES_FILENAME).write_text("")
        assert load_timeseries(tmp_path) == []

    def test_loads_records(self, tmp_path):
        state = _make_state()
        append_timeseries(state, tmp_path, source="conversation")
        append_timeseries(state, tmp_path, source="silence")
        records = load_timeseries(tmp_path)
        assert len(records) == 2
        assert records[0]["source"] == "conversation"
        assert records[1]["source"] == "silence"

    def test_filter_since(self, tmp_path):
        now = datetime.now(JST)
        old_ts = (now - timedelta(hours=48)).isoformat()
        new_ts = now.isoformat()
        lines = [
            json.dumps({"ts": old_ts, "valence": 0.1, "source": "silence"}),
            json.dumps({"ts": new_ts, "valence": 0.9, "source": "conversation"}),
        ]
        (tmp_path / TIMESERIES_FILENAME).write_text("\n".join(lines) + "\n")

        since = now - timedelta(hours=24)
        records = load_timeseries(tmp_path, since=since)
        assert len(records) == 1
        assert records[0]["valence"] == pytest.approx(0.9)

    def test_filter_until(self, tmp_path):
        now = datetime.now(JST)
        old_ts = (now - timedelta(hours=48)).isoformat()
        new_ts = now.isoformat()
        lines = [
            json.dumps({"ts": old_ts, "valence": 0.1, "source": "silence"}),
            json.dumps({"ts": new_ts, "valence": 0.9, "source": "conversation"}),
        ]
        (tmp_path / TIMESERIES_FILENAME).write_text("\n".join(lines) + "\n")

        until = now - timedelta(hours=24)
        records = load_timeseries(tmp_path, until=until)
        assert len(records) == 1
        assert records[0]["valence"] == pytest.approx(0.1)

    def test_corrupt_line_skipped(self, tmp_path):
        lines = [
            json.dumps({"ts": datetime.now(JST).isoformat(), "valence": 0.5, "source": "s"}),
            "THIS IS NOT JSON",
            json.dumps({"ts": datetime.now(JST).isoformat(), "valence": 0.7, "source": "c"}),
        ]
        (tmp_path / TIMESERIES_FILENAME).write_text("\n".join(lines) + "\n")
        records = load_timeseries(tmp_path)
        assert len(records) == 2


class TestTimeseriesSizeBytes:
    def test_missing_file(self, tmp_path):
        assert timeseries_size_bytes(tmp_path) == 0

    def test_returns_size(self, tmp_path):
        content = '{"ts":"2026-01-01","valence":0.5}\n'
        (tmp_path / TIMESERIES_FILENAME).write_text(content)
        assert timeseries_size_bytes(tmp_path) == len(content.encode("utf-8"))
