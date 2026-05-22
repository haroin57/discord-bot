"""Tests for scripts/kanae_daily.py — conversation collector + helpers.

Focus on the changes that anchor kanae_daily journal entries to the actual
Discord history so the journal stops hallucinating "先輩から通知が来た" at
times when haroin was silent. Each test isolates one behaviour of the
``_collect_recent_conversation`` rewrite plus the silence/timestamp
helpers it relies on.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import kanae_daily as kd

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def history_root(tmp_path: Path, monkeypatch) -> Path:
    """Provide an isolated HISTORY_DIR rooted at tmp_path."""
    root = tmp_path / "history"
    root.mkdir()
    monkeypatch.setattr(kd, "HISTORY_DIR", root)
    return root


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _row(ts: datetime, role: str, channel_id: int, content: str) -> dict:
    return {
        "timestamp": ts.isoformat(),
        "role": role,
        "channel_id": channel_id,
        "content": content,
    }


# ---------------------------------------------------------------------------
# _fmt_silence
# ---------------------------------------------------------------------------


def test_fmt_silence_none():
    now = datetime(2026, 5, 21, 12, 0, tzinfo=JST)
    assert kd._fmt_silence(None, now) == "(24h以内なし)"


def test_fmt_silence_seconds():
    now = datetime(2026, 5, 21, 12, 0, 30, tzinfo=JST)
    ts = datetime(2026, 5, 21, 12, 0, 0, tzinfo=JST)
    assert kd._fmt_silence(ts, now) == "30秒前"


def test_fmt_silence_minutes():
    now = datetime(2026, 5, 21, 12, 5, tzinfo=JST)
    ts = datetime(2026, 5, 21, 12, 0, tzinfo=JST)
    assert kd._fmt_silence(ts, now) == "5分前"


def test_fmt_silence_hours_with_minutes():
    now = datetime(2026, 5, 21, 14, 30, tzinfo=JST)
    ts = datetime(2026, 5, 21, 12, 0, tzinfo=JST)
    assert kd._fmt_silence(ts, now) == "2時間30分前"


def test_fmt_silence_days():
    now = datetime(2026, 5, 23, 12, 0, tzinfo=JST)
    ts = datetime(2026, 5, 21, 10, 0, tzinfo=JST)
    # 2 days 2 hours = 50 hours
    assert kd._fmt_silence(ts, now) == "2日2時間前"


# ---------------------------------------------------------------------------
# _fmt_ts_for_prompt
# ---------------------------------------------------------------------------


def test_fmt_ts_same_day():
    now = datetime(2026, 5, 21, 23, 0, tzinfo=JST)
    ts = datetime(2026, 5, 21, 7, 15, tzinfo=JST)
    assert kd._fmt_ts_for_prompt(ts, now) == "07:15"


def test_fmt_ts_crossing_day():
    now = datetime(2026, 5, 21, 0, 30, tzinfo=JST)
    ts = datetime(2026, 5, 20, 23, 50, tzinfo=JST)
    assert kd._fmt_ts_for_prompt(ts, now) == "05-20 23:50"


# ---------------------------------------------------------------------------
# _resolve_haroin_dm_channel
# ---------------------------------------------------------------------------


def test_resolve_haroin_dm_returns_none_when_empty(history_root, monkeypatch):
    monkeypatch.setattr("config.HAROIN_DM_CHANNEL_ID", None, raising=False)
    assert kd._resolve_haroin_dm_channel() is None


def test_resolve_haroin_dm_prefers_env_config(history_root, monkeypatch):
    monkeypatch.setattr("config.HAROIN_DM_CHANNEL_ID", 1490247632847896776, raising=False)
    # Even with a heavier other file present, env wins.
    _write_jsonl(history_root / "9999999999999999999.jsonl", [{"x": "y"}] * 100)
    assert kd._resolve_haroin_dm_channel() == 1490247632847896776


def test_resolve_haroin_dm_picks_largest_when_env_unset(history_root, monkeypatch):
    monkeypatch.setattr("config.HAROIN_DM_CHANNEL_ID", None, raising=False)
    small = history_root / "1059371770337046561.jsonl"
    big = history_root / "1490247632847896776.jsonl"
    _write_jsonl(small, [{"x": "y"}] * 5)
    _write_jsonl(big, [{"x": "y"}] * 200)
    assert kd._resolve_haroin_dm_channel() == 1490247632847896776


# ---------------------------------------------------------------------------
# _collect_recent_conversation — speaker labelling + temporal anchoring
# ---------------------------------------------------------------------------


def test_collect_empty_history_returns_sentinel(history_root, monkeypatch):
    monkeypatch.setattr("config.HAROIN_DM_CHANNEL_ID", None, raising=False)
    now = datetime(2026, 5, 21, 12, 0, tzinfo=JST)
    result = kd._collect_recent_conversation(now)
    assert result["lines"] == "(24時間以内の会話なし)"
    assert result["last_senpai_ts"] is None
    assert result["last_other_user_ts"] is None
    assert result["last_kanae_ts"] is None
    assert result["last_speaker_label"] is None
    assert result["message_count_24h"] == 0


def test_collect_haroin_dm_labelled_as_senpai(history_root, monkeypatch):
    haroin_ch = 1490247632847896776
    other_ch = 1059371770337046561
    monkeypatch.setattr("config.HAROIN_DM_CHANNEL_ID", haroin_ch, raising=False)

    now = datetime(2026, 5, 21, 12, 0, tzinfo=JST)
    _write_jsonl(history_root / f"{haroin_ch}.jsonl", [
        _row(now - timedelta(minutes=10), "user", haroin_ch, "今美佐枝編見終わった"),
        _row(now - timedelta(minutes=9), "assistant", haroin_ch, "おかえりなさい先輩"),
    ])
    _write_jsonl(history_root / f"{other_ch}.jsonl", [
        _row(now - timedelta(minutes=30), "user", other_ch, "お父さんからの伝言"),
    ])

    result = kd._collect_recent_conversation(now)
    assert "先輩: 今美佐枝編見終わった" in result["lines"]
    assert "他者: お父さんからの伝言" in result["lines"]
    assert "かなえ: おかえりなさい先輩" in result["lines"]
    assert result["last_senpai_ts"] == now - timedelta(minutes=10)
    assert result["last_other_user_ts"] == now - timedelta(minutes=30)
    assert result["last_kanae_ts"] == now - timedelta(minutes=9)
    assert result["last_speaker_label"] == "かなえ"
    assert result["message_count_24h"] == 3


def test_collect_24h_cutoff_drops_older(history_root, monkeypatch):
    haroin_ch = 1490247632847896776
    monkeypatch.setattr("config.HAROIN_DM_CHANNEL_ID", haroin_ch, raising=False)

    now = datetime(2026, 5, 21, 12, 0, tzinfo=JST)
    _write_jsonl(history_root / f"{haroin_ch}.jsonl", [
        # 25h old — should be dropped
        _row(now - timedelta(hours=25), "user", haroin_ch, "古い発話"),
        # 1h old — kept
        _row(now - timedelta(hours=1), "user", haroin_ch, "最近の発話"),
    ])
    result = kd._collect_recent_conversation(now)
    assert "古い発話" not in result["lines"]
    assert "最近の発話" in result["lines"]
    assert result["message_count_24h"] == 1


def test_collect_silence_when_haroin_quiet(history_root, monkeypatch):
    """The bug we are fixing: haroin silent for >24h must yield None ts."""
    haroin_ch = 1490247632847896776
    monkeypatch.setattr("config.HAROIN_DM_CHANNEL_ID", haroin_ch, raising=False)

    now = datetime(2026, 5, 21, 2, 45, tzinfo=JST)  # the 02:45 hallucination case
    _write_jsonl(history_root / f"{haroin_ch}.jsonl", [
        # haroin's last actual msg was 11h before, then kanae replied
        _row(now - timedelta(hours=11), "user", haroin_ch, "会社のミーティング"),
        _row(now - timedelta(hours=11), "assistant", haroin_ch, "行ってらっしゃい先輩"),
    ])
    result = kd._collect_recent_conversation(now)
    # haroin's ts is present (11h ago), but not "just now"
    assert result["last_senpai_ts"] == now - timedelta(hours=11)
    assert result["last_speaker_label"] == "かなえ"
    silence = kd._fmt_silence(result["last_senpai_ts"], now)
    assert silence == "11時間0分前"


def test_collect_skips_backup_files(history_root, monkeypatch):
    haroin_ch = 1490247632847896776
    monkeypatch.setattr("config.HAROIN_DM_CHANNEL_ID", haroin_ch, raising=False)

    now = datetime(2026, 5, 21, 12, 0, tzinfo=JST)
    _write_jsonl(history_root / f"{haroin_ch}.jsonl", [
        _row(now - timedelta(minutes=5), "user", haroin_ch, "現役の発話"),
    ])
    _write_jsonl(history_root / f"{haroin_ch}.jsonl.bak.pre-merge", [
        _row(now - timedelta(minutes=5), "user", haroin_ch, "バックアップの発話"),
    ])
    result = kd._collect_recent_conversation(now)
    assert "現役の発話" in result["lines"]
    assert "バックアップの発話" not in result["lines"]


def test_collect_caps_at_12_lines(history_root, monkeypatch):
    haroin_ch = 1490247632847896776
    monkeypatch.setattr("config.HAROIN_DM_CHANNEL_ID", haroin_ch, raising=False)

    now = datetime(2026, 5, 21, 12, 0, tzinfo=JST)
    rows = []
    for i in range(20):
        rows.append(_row(now - timedelta(minutes=20 - i), "user", haroin_ch, f"msg{i}"))
    _write_jsonl(history_root / f"{haroin_ch}.jsonl", rows)

    result = kd._collect_recent_conversation(now)
    # 12 most recent lines kept, last is msg19
    lines = result["lines"].splitlines()
    assert len(lines) == 12
    assert "msg19" in lines[-1]
    assert "msg8" in lines[0]  # 20 - 12 = 8 is the oldest kept
    assert result["message_count_24h"] == 20


def test_collect_skips_malformed_rows(history_root, monkeypatch):
    haroin_ch = 1490247632847896776
    monkeypatch.setattr("config.HAROIN_DM_CHANNEL_ID", haroin_ch, raising=False)

    now = datetime(2026, 5, 21, 12, 0, tzinfo=JST)
    path = history_root / f"{haroin_ch}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write("not-json\n")
        f.write(json.dumps({"role": "user", "channel_id": haroin_ch, "content": "no-ts"}) + "\n")
        f.write(json.dumps({"timestamp": "broken-ts", "role": "user", "channel_id": haroin_ch, "content": "bad-ts"}) + "\n")
        f.write(json.dumps(_row(now - timedelta(minutes=1), "user", haroin_ch, "正常")) + "\n")

    result = kd._collect_recent_conversation(now)
    assert "正常" in result["lines"]
    assert "no-ts" not in result["lines"]
    assert "bad-ts" not in result["lines"]
    assert result["message_count_24h"] == 1


# ---------------------------------------------------------------------------
# _build_daily_prompt — verifies the new contract is wired through
# ---------------------------------------------------------------------------


def test_build_daily_prompt_includes_status_block_and_guard_rules():
    now = datetime(2026, 5, 21, 2, 45, tzinfo=JST)
    conversation = {
        "lines": "[15:52] 先輩: 会社のミーティング\n[15:52] かなえ: 行ってらっしゃい先輩",
        "last_senpai_ts": now - timedelta(hours=11),
        "last_other_user_ts": None,
        "last_kanae_ts": now - timedelta(hours=11),
        "last_speaker_label": "かなえ",
        "message_count_24h": 2,
    }
    prompt = kd._build_daily_prompt(
        now=now,
        event_type="routine",
        recent_entries="(none)",
        summary="(none)",
        conversation=conversation,
    )
    # status block present
    assert "## 直近の会話状況" in prompt
    assert "11時間0分前" in prompt
    assert "直近の発話者: かなえ" in prompt
    # conversation block uses the new heading + sentinel
    assert "## 直近のDiscord会話" in prompt
    assert "これ以外の発話は直近24時間に存在しない" in prompt
    # the three new guard rules
    assert "通知音が鳴る描写も禁止" in prompt
    assert "incident は機材・環境・自己発の出来事に限定" in prompt
    assert "「来た」描写は禁止" in prompt


def test_build_daily_prompt_event_hints_have_no_external_initiator():
    """The hallucination-prone words must not appear in any event_hint."""
    now = datetime(2026, 5, 21, 12, 0, tzinfo=JST)
    conv = {
        "lines": "(none)",
        "last_senpai_ts": None,
        "last_other_user_ts": None,
        "last_kanae_ts": None,
        "last_speaker_label": None,
        "message_count_24h": 0,
    }
    forbidden = ("同僚からの連絡", "思わぬ来客", "嬉しい知らせ", "友達と出かける")
    for ev in ("routine", "minor_event", "major_event", "incident"):
        prompt = kd._build_daily_prompt(now, ev, "(none)", "(none)", conv)
        for word in forbidden:
            assert word not in prompt, f"event={ev} still contains {word!r}"
