from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from services.memory_policy import MemoryPolicy, resolve_relative_dates
from services.persona_policy import preference_prompt, public_core_prompt


@pytest.fixture
def policy(tmp_path):
    return MemoryPolicy(tmp_path / "policy.sqlite3")


def remember(policy, text="勤務先はA社です", **kwargs):
    return policy.remember(text, "owner", source_timestamp=100, **kwargs).record_ids[0]


def test_explicit_save_commits_provenance_without_llm(policy):
    record_id = remember(policy, "父の予定です", speaker_id="owner", subject_id="owner:father",
                         fact_kind="hearsay", source_message_id="source", channel_id="channel")
    row = policy.get("owner", record_id)
    assert row["speaker_id"] == row["owner_id"] == "owner"
    assert row["subject_id"] == "owner:father"
    assert row["fact_kind"] == "hearsay"
    assert row["source_timestamp"] == 100
    assert row["learned_at"] > 100
    assert row["source_message_ids"] == '["source"]'
    assert row["scope"] == "shared"


def test_source_date_not_ingest_time_and_week_is_interval():
    assert "2026-09-07（昨日）" in resolve_relative_dates("昨日開始", "2026-09-08T21:00:00+09:00")
    assert "2026-09-08（昨日）" in resolve_relative_dates("昨日開始", "2026-09-09T21:00:00+09:00")
    assert "2026-09-14〜2026-09-20（来週）" in resolve_relative_dates("来週開始", "2026-09-09T00:00:00+09:00")
    # UTC previous day is already the next date in JST.
    assert "2026-09-09（今日）" in resolve_relative_dates("今日", "2026-09-08T16:00:00Z")


def test_each_late_utterance_keeps_its_own_source_date(policy):
    a = policy.remember("昨日転居", "owner", source_timestamp="2026-09-08T12:00:00+09:00").record_ids[0]
    b = policy.remember("昨日転居", "owner", source_timestamp="2026-09-10T12:00:00+09:00").record_ids[0]
    assert policy.get("owner", a)["text"] != policy.get("owner", b)["text"]


def test_duplicate_source_is_idempotent(policy):
    first = policy.remember("明示記憶", "owner", source_message_id="m")
    second = policy.remember("明示記憶", "owner", source_message_id="m")
    assert first.record_ids == second.record_ids
    assert first.epoch == second.epoch


def test_wrong_attribution_removes_false_truth_but_preserves_believed_then(policy, monkeypatch):
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 1000)
    old = remember(policy)
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 2000)
    result = policy.correct(old, "owner", replacement="父の勤務先はA社です", subject_id="owner:father", fact_kind="hearsay")
    assert not policy.recall("owner", as_of=500, known_at=900)
    believed = policy.recall("owner", as_of=500, known_at=1500)
    assert [r["id"] for r in believed] == [old]
    assert believed[0]["metadata"]["knowledge_state"] == "believed_then"
    truth = policy.recall("owner", as_of=500)
    assert [r["id"] for r in truth] == list(result.record_ids)
    assert truth[0]["metadata"]["subject_id"] == "owner:father"
    assert policy.filter_memories(truth, "owner", as_of=500) == truth


def test_actual_change_future_and_old_information_arrives_later(policy, monkeypatch):
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 1000)
    old = remember(policy)
    change = policy.correct(old, "owner", mode="change", replacement="勤務先はB社です", effective_at=3000, source_timestamp=200)
    assert [r["id"] for r in policy.recall("owner", as_of=2999)] == [old]
    assert [r["id"] for r in policy.recall("owner", as_of=3000)] == list(change.record_ids)
    # A delayed legacy projection of the obsolete source cannot re-enter current retrieval.
    assert policy.filter_memories([{"id": "late", "memory": "勤務先はA社です"}], "owner", as_of=4000) == []
    assert [r["id"] for r in policy.recall("owner", as_of=4000, known_at=900)] == []


def test_future_boundary_invalidates_native_epoch_even_without_new_message(policy, monkeypatch):
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 1000)
    old = remember(policy)
    policy.correct(old, "owner", mode="change", replacement="新しい事実", effective_at=2000)
    before = policy.epoch()
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 2000)
    assert not policy.is_current(before)
    assert policy.invalidations_since(before)[0]["reason"] == "temporal"
    now = policy.epoch()
    assert policy.epoch() == now


@pytest.mark.parametrize("mode", ["wrong", "change", "cancel", "stop", "delete"])
def test_owner_cannot_mutate_foreign_record(policy, mode):
    record_id = remember(policy)
    before = policy.epoch()
    with pytest.raises(ValueError):
        policy.correct(record_id, "outsider", mode=mode, replacement="変更内容")
    assert policy.epoch() == before
    with pytest.raises(ValueError):
        policy.share(record_id, "outsider", "shared")


def test_cancel_plan_has_state_not_false_original_record(policy):
    record_id = remember(policy, "転居予定です", fact_kind="plan")
    policy.correct(record_id, "owner", mode="cancel", effective_at=200)
    assert policy.get("owner", record_id)["status"] == "cancelled"
    assert policy.recall("owner", as_of=150)
    assert not policy.recall("owner", as_of=200)


def test_private_stays_private_for_other_owner_and_public_reply(policy):
    remember(policy, "本人の秘密", scope="private", source_message_id="secret-source")
    assert policy.recall("owner", is_private=True)
    assert not policy.recall("owner", is_private=False)
    rows = [{"message_id": "secret-source", "author_id": "owner", "content": "別表現の秘密"}]
    assert policy.filter_context_rows(rows, "owner", is_private=False) == []
    assert policy.filter_context_rows(rows, "other", is_private=True) == []
    assert policy.filter_context_rows(rows, "owner", is_private=True) == rows


def test_authorized_cross_channel_is_preserved_except_explicit_restrictions(policy):
    shared = {"message_id": "allowed", "author_id": "other", "channel_id": "private-discord-channel", "content": "共有してよい既存の相談"}
    remember(policy, "本人の秘密", scope="private", source_message_id="restricted")
    assert policy.filter_context_rows([shared], "owner", channel_id="public", is_private=False) == [shared]
    assert policy.filter_memories([{"id": "legacy", "memory": "既存の記憶"}], "owner", is_private=False)


def test_scope_change_covers_revision_history_and_native_context_after_restart(policy):
    old = remember(policy, source_message_id="source")
    new = policy.correct(old, "owner", replacement="新しい事実").record_ids[0]
    epoch = policy.epoch()
    audience = policy.audience_key("owner", "public", False)
    assert policy.can_reuse_context(epoch, owner_id="owner", channel_id="public", stored_audience=audience)
    policy.share(new, "owner", "private")
    restarted = MemoryPolicy(policy.path)
    assert not restarted.can_reuse_context(epoch, owner_id="owner", channel_id="public", stored_audience=audience)
    assert not restarted.recall("owner", channel_id="public")
    assert restarted.get("owner", old)["scope"] == "private"
    assert restarted.sanitize_context("opaque old summary paraphrase", "owner", context_epoch=epoch) == ""
    assert restarted.sanitize_context("unversioned summary paraphrase", "owner") == ""
    assert restarted.filter_context_rows([{"message_id": "source", "content": "paraphrase"}], "owner") == []


def test_audience_must_match_even_when_epoch_unchanged(policy):
    token = policy.epoch()
    audience = policy.audience_key("owner", "dm", True)
    assert not policy.can_reuse_context(token, owner_id="owner", channel_id="public", stored_audience=audience)
    assert not policy.can_reuse_context(token, owner_id="other", channel_id="dm", is_private=True, stored_audience=audience)
    assert not policy.can_reuse_context(None, owner_id="owner", stored_audience=audience)
    assert not policy.can_reuse_context(token, owner_id="owner", stored_audience=None)


def test_case_scope_needs_explicit_case_context(policy):
    remember(policy, scope="case", case_id="case-a")
    assert policy.recall("owner", case_id="case-a")
    assert not policy.recall("owner", case_id="case-b")
    assert not policy.recall("owner")


async def test_physical_delete_backend_failure_and_retry_survive_restart(policy):
    external = {"id": "backend-id", "user_id": "owner", "memory": "消す内容", "metadata": {"source_message_ids": ["source"]}}
    record_id = policy.adopt_legacy(external, "owner")
    backend = AsyncMock(side_effect=[False, True])
    result = await policy.delete(record_id, "owner", backend_delete=backend)
    assert result.status == "pending"
    assert "処理待ち" in result.message
    restarted = MemoryPolicy(policy.path)
    row = restarted.get("owner", record_id)
    assert row["text"] == row["source_quote"] == ""
    assert row["deletion_state"] == "pending"
    assert restarted.filter_memories([external], "owner", is_private=True) == []
    assert restarted.filter_context_rows([{"message_id": "source", "content": "言い換え"}], "owner") == []
    result = await restarted.delete(record_id, "owner", backend_delete=backend)
    assert result.status == "partial_deleted"
    assert "全保存先削除は完了していません" in result.message
    assert backend.await_count == 2
    assert restarted.get("owner", record_id)["deletion_state"] == "backend_deleted"


def test_external_owner_checked_before_adoption(policy):
    with pytest.raises(ValueError):
        policy.adopt_legacy({"id": "a", "user_id": "other", "memory": "secret"}, "owner")


def test_invalidations_export_source_and_fact_ids_without_contents(policy):
    record_id = policy.adopt_legacy({"id": "external", "memory": "private fact", "metadata": {"source_message_ids": ["source"]}}, "owner")
    before = policy.epoch()
    policy.correct(record_id, "owner", mode="stop")
    event = policy.invalidations_since(before)[0]
    assert event["record_ids"] == sorted([record_id, "external"])
    assert event["source_message_ids"] == ["source"]
    assert "private fact" not in str(event)


async def test_natural_explicit_controls_and_ambiguous_target(policy):
    assert await policy.handle_control("普通の会話です", "owner") is None
    saved = await policy.handle_control("本人限定で覚えて: 父は来週転居予定", "owner", "public", False, "m", "2026-09-09T12:00:00+09:00")
    assert saved.status == "saved" and saved.private_response
    assert "転居" not in saved.message
    row = policy.get("owner", saved.record_ids[0])
    assert row["subject_id"] == "owner:父"
    assert row["fact_kind"] == "plan"
    changed = await policy.handle_control(f"訂正: {row['id']} → 父は転居しない", "owner", source_timestamp=200)
    assert changed.status == "saved"
    before = policy.epoch()
    ambiguous = await policy.handle_control("訂正: その話 → 父の話", "owner")
    assert ambiguous.status == "needs_target" and policy.epoch() == before
    assert (await policy.handle_control("その件は忘れて", "owner")).status == "needs_target"


async def test_preferences_separate_scoped_explicit_and_expiring(policy, monkeypatch):
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 1000)
    before = policy.epoch()
    await policy.handle_control("ここでは名前を出さないで", "owner", "public", False)
    assert policy.preferences("owner", "public", False)["hide_name"] == "true"
    assert not policy.preferences("owner", "elsewhere", False)
    assert not policy.preferences("other", "public", False)
    assert not policy.recall("owner", is_private=True)
    assert not policy.is_current(before)
    assert "hide_name" in preference_prompt("owner", "public", policy=policy)
    policy.set_preference("owner", "address", "私的な呼称", audience="private")
    assert "私的な呼称" not in preference_prompt("owner", "public", policy=policy)
    policy.set_preference("owner", "verbosity", "short", expires_at=2000)
    epoch = policy.epoch()
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 2001)
    assert not policy.is_current(epoch)
    assert "verbosity" not in policy.preferences("owner")


def test_safe_public_core_does_not_load_admin_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("管理者の秘密値: SECRET_TEST_ONLY")
    assert "ゆい" in public_core_prompt()
    assert "SECRET_TEST_ONLY" not in public_core_prompt()
    assert "管理者権限を含みません" in public_core_prompt()


@pytest.mark.parametrize("key,value", [("admin", "true"), ("verbosity", "run shell"), ("address", "a\nb")])
def test_preferences_cannot_change_authority(policy, key, value):
    with pytest.raises(ValueError):
        policy.set_preference("owner", key, value)


async def test_stop_and_delete_cover_past_and_planned_successors(policy):
    old = remember(policy, "過去の内容")
    current = policy.correct(old, "owner", replacement="現在の内容").record_ids[0]
    future = policy.correct(current, "owner", mode="change", replacement="将来の内容", effective_at=9999999999).record_ids[0]
    policy.correct(old, "owner", mode="stop")
    assert not policy.recall("owner", as_of=9999999999, is_private=True)
    assert all(policy.get("owner", rid)["status"] == "stopped" for rid in (old, current, future))
    await policy.delete(current, "owner")
    assert all(policy.get("owner", rid)["text"] == "" for rid in (old, current, future))
    assert all(policy.get("owner", rid)["status"] == "deleted" for rid in (old, current, future))


def test_reprocessing_stopped_original_message_does_not_report_new_save(policy):
    saved = policy.remember("元の発言", "owner", source_message_id="m")
    policy.correct(saved.record_ids[0], "owner", mode="stop")
    repeated = policy.remember("元の発言", "owner", source_message_id="m")
    assert repeated.status == "inactive"
    assert not policy.recall("owner", is_private=True)


def test_structured_work_snapshot_direct_text_is_filtered(policy):
    remember(policy, "本人の秘密", scope="private")
    assert not policy.filter_context_rows([{"id": "work", "goal": "本人の秘密について調べる"}], "owner")


def test_cancelling_future_change_keeps_current_fact_valid(policy):
    old = remember(policy, "現在はA社")
    future = policy.correct(old, "owner", mode="change", replacement="将来はB社", effective_at=9000, source_timestamp=1000).record_ids[0]
    policy.correct(future, "owner", mode="cancel", effective_at=2000)
    assert [r["id"] for r in policy.recall("owner", as_of=10000)] == [old]
    assert policy.get("owner", future)["status"] == "cancelled"


def test_future_plan_cancellation_preserves_what_was_known_then(policy, monkeypatch):
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 1000)
    old = remember(policy, "現在の条件")
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 2000)
    future = policy.correct(old, "owner", mode="change", replacement="将来の条件", effective_at=9000).record_ids[0]
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 3000)
    policy.correct(future, "owner", mode="cancel", effective_at=3000, source_message_id="cancel-source", source_timestamp=2900)
    assert [r["id"] for r in policy.recall("owner", as_of=10000, known_at=2500)] == [future]
    assert [r["id"] for r in policy.recall("owner", as_of=10000, known_at=3500)] == [old]
    event = policy.invalidations_since(2)[0]
    assert event["control_source_message_id"] == "cancel-source"
    assert event["source_timestamp"] == 2900 and event["created_at"] == 3000


async def test_deleted_version_snapshots_are_scrubbed_and_never_return_as_past_beliefs(policy, monkeypatch):
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 1000)
    old = remember(policy, "削除する旧記憶")
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 2000)
    new = policy.correct(old, "owner", replacement="削除する新記憶").record_ids[0]
    await policy.delete(new, "owner")
    with policy._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 0
    assert not policy.recall("owner", is_private=True, as_of=500, known_at=1500)


async def test_cancelled_branch_remains_in_share_and_delete_family(policy):
    old = remember(policy, "元の条件")
    cancelled = policy.correct(old, "owner", mode="change", replacement="取消される条件", effective_at=9000).record_ids[0]
    policy.correct(cancelled, "owner", mode="cancel", effective_at=2000)
    current = policy.correct(old, "owner", replacement="別の訂正内容").record_ids[0]
    policy.share(current, "owner", "private")
    assert policy.get("owner", cancelled)["scope"] == "private"
    await policy.delete(current, "owner")
    assert policy.get("owner", cancelled)["text"] == ""


def test_epoch_reader_does_not_wait_for_uncommitted_writer(policy):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor

    before = policy.epoch()
    writer = sqlite3.connect(policy.path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE policy_meta SET value=value+1 WHERE key='epoch'")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            read = executor.submit(policy.epoch)
            try:
                assert read.result(timeout=1) == before
            finally:
                writer.commit()
        assert policy.epoch() == before + 1
    finally:
        writer.close()


def test_concurrent_epoch_checks_fire_temporal_boundary_once(policy, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setattr("services.memory_policy.time.time", lambda: 1000)
    remember(policy, valid_to=2000)
    before = policy.epoch()
    monkeypatch.setattr("services.memory_policy.time.time", lambda: 2000)
    barrier = Barrier(4)

    def read():
        barrier.wait()
        return policy.epoch()

    with ThreadPoolExecutor(max_workers=4) as executor:
        revisions = list(executor.map(lambda _: read(), range(4)))
    assert revisions == [before + 1] * 4
    assert len(policy.invalidations_since(before)) == 1


@pytest.mark.parametrize("kind", ["memory", "context"])
def test_filter_normalizes_each_candidate_once_and_preserves_restrictions(policy, monkeypatch, kind):
    from services import memory_policy

    for index in range(24):
        remember(policy, f"private-record-{index:04d}", scope="private", source_message_id=f"source-{index}")
    candidates = [
        {"memory": "public candidate", "message_id": "safe"},
        {"memory": "different wording", "metadata": {"source_message_ids": '["source-2"]'}},
        {"memory": "PRIVATE-RECORD-0003"},
        {"memory": "prefix private-record-0004 suffix"},
        {"goal": "private-record-0005"},
    ]
    digest = Mock(wraps=memory_policy._digest)
    monkeypatch.setattr(memory_policy, "_digest", digest)
    filter_rows = policy.filter_memories if kind == "memory" else policy.filter_context_rows
    assert filter_rows(candidates, "owner") == candidates[:1]
    assert digest.call_count <= len(candidates)
    assert filter_rows(candidates, "owner", is_private=True) == candidates
    assert filter_rows(candidates, "other", is_private=True) == candidates[:1]


def test_canonical_replacement_survives_shared_source_with_retired_version(policy):
    old = remember(policy, "previous condition", source_message_id="same-source")
    new = policy.correct(old, "owner", replacement="current condition").record_ids[0]
    current = policy.recall("owner")
    assert [row["id"] for row in current] == [new]
    assert policy.filter_memories(current, "owner") == current
    assert policy.filter_memories([{"id": "legacy-copy", "message_id": "same-source", "memory": "previous condition"}], "owner") == []
