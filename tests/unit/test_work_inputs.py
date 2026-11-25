from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("text, expected", [
    ("明日9時に会議を知らせて", "2026-09-10T09:00:00+09:00"),
    ("30分後に休憩を通知して", "2026-09-10T00:20:00+09:00"),
    ("明後日の午後3時に面談をリマインドして", "2026-09-11T15:00:00+09:00"),
])
def test_reminder_dates_use_original_message_time(text, expected):
    from services.work.future_intent import parse_reminder
    intent = parse_reminder(text, source_time=datetime.fromisoformat("2026-09-09T23:50:00+09:00"))
    assert intent.run_at == datetime.fromisoformat(expected).timestamp()
    assert not intent.error


def test_missing_time_is_not_a_successful_registration():
    from services.work.future_intent import parse_reminder
    result = parse_reminder("後で会議を知らせて")
    assert result.run_at is None
    assert result.error
    assert parse_reminder("その条件の物件が出たら知らせて") is None


@pytest.mark.parametrize("text", ["昨日の比較の続きをお願いします", "前回の調査の続きを", "続きをお願い"])
def test_next_day_resumption_is_a_work_control(text):
    from services.work_intent import classify_work_intent
    assert classify_work_intent(text).action == "resume"


async def test_original_attachment_survives_conditions_but_not_revoked_memory_scope(tmp_path):
    from services.work.attachments import capture, input_artifacts
    from services.work.store import WorkStore
    from services.memory_policy import MemoryPolicy

    store, policy = WorkStore(tmp_path / "work.db"), MemoryPolicy(tmp_path / "memory.db")
    work = store.create("1", "10", "添付の候補を比較して", "new")
    source = SimpleNamespace(id=50, filename="conditions.txt", content_type="text/plain", size=10,
                             read=AsyncMock(return_value="候補の住所です。".encode()))
    message = SimpleNamespace(id=20, author=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=10, recipient=None), attachments=[source])
    assert await capture(store, work, message, policy=policy) == []
    work = store.amend(work.id, "1", "予算変更", "edit")
    inputs = input_artifacts(store, work, policy=policy, channel_id="10")
    assert len(inputs) == 1
    assert inputs[0]["body"] == "候補の住所です。"
    policy.remember("候補の住所です。", "1", scope="private", source_message_id="20")
    withheld = input_artifacts(store, work, policy=policy, channel_id="10")
    assert withheld[0]["metadata"]["encoding"] == "unavailable"
    assert "候補の住所" not in str(withheld)


async def test_unsupported_attachment_is_a_recorded_missing_input(tmp_path):
    from services.work.attachments import capture
    from services.work.store import WorkStore

    store = WorkStore(tmp_path / "work.db")
    work = store.create("1", "10", "このデータを比較して", "new")
    source = SimpleNamespace(id=50, filename="input.bin", content_type="application/octet-stream",
        size=5, read=AsyncMock(return_value=b"abcde"))
    message = SimpleNamespace(id=20, author=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=10, recipient=None), attachments=[source])
    errors = await capture(store, work, message)
    assert len(errors) == 1
    artifact = store.list_artifacts(work.id)[0]
    assert artifact["metadata"]["encoding"] == "unavailable"
    assert "解析できません" in artifact["body"]
