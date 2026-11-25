"""Durable request semantics independent of Discord and model providers."""

import json
import sqlite3

import pytest


@pytest.fixture
def store(tmp_path):
    from services.work.store import WorkStore
    return WorkStore(tmp_path / "work.sqlite3")


def create(store, **kwargs):
    return store.create("owner", "channel", "管理費込みの物件を比較します。", "create-1", **kwargs)


def test_acceptance_is_committed_with_event_before_return(store):
    work = create(store, requirements={"rent": 80000})
    with sqlite3.connect(store.path) as connection:
        saved = json.loads(connection.execute("SELECT data FROM works WHERE id=?", (work.id,)).fetchone()[0])
        assert saved["requirements"] == {"rent": 80000}
        assert connection.execute("SELECT kind FROM work_events").fetchone()[0] == "accepted"
    assert work.audience == "shared"
    assert work.state == "queued"
    assert work.revision == 1
    assert work.id.startswith("w_")


def test_idempotent_delivery_does_not_repeat_amendment(store):
    work = create(store)
    updated = store.amend(work.id, "owner", "管理費を含めます。", "message-2", requirements={"rent": 90000})
    assert create(store) == updated
    assert store.amend(work.id, "owner", "管理費を含めます。", "message-2", requirements={"rent": 90000}) == updated
    assert updated.requirements["amendments"] == ["管理費を含めます。"]
    assert len(store.list_events(work.id)) == 2


@pytest.mark.parametrize("kind", ["create", "amend"])
def test_same_request_id_with_different_content_fails(store, kind):
    from services.work.store import WorkIdempotencyConflict
    work = create(store)
    if kind == "create":
        call = lambda: store.create("intruder", "channel", work.goal, "create-1")
    else:
        store.amend(work.id, "owner", "A", "message-2")
        call = lambda: store.amend(work.id, "owner", "B", "message-2")
    with pytest.raises(WorkIdempotencyConflict):
        call()


def test_changes_merge_dicts_preserve_history_and_guard_revision(store):
    from services.work.store import WorkRevisionConflict
    work = create(store, requirements={"area": "A", "rent": 80000}, decisions=["駅前を優先します。"])
    updated = store.amend(work.id, "owner", "駅から10分です。", "message-2", expected_revision=1, requirements={"rent": 90000})
    assert updated.requirements["area"] == "A"
    assert updated.requirements["rent"] == 90000
    assert updated.decisions == work.decisions
    with pytest.raises(WorkRevisionConflict):
        store.amend(work.id, "owner", "古い版から変更します。", "message-3", expected_revision=1)
    assert store.get(work.id) == updated


def test_cross_user_access_and_shared_audience_do_not_grant_permissions(store):
    from services.work.store import WorkAccessDenied
    work = create(store, audience="shared", participants=["collaborator"])
    assert store.get(work.id, "collaborator") == work
    assert not store.list_for("intruder", "channel")
    for call in (
        lambda: store.get(work.id, "intruder"),
        lambda: store.amend(work.id, "intruder", "取消", "bad"),
        lambda: store.transition(work.id, "intruder", "cancelled"),
        lambda: store.list_events(work.id, "intruder"),
        lambda: store.list_artifacts(work.id, "intruder"),
        lambda: store.list_questions(work.id, "intruder"),
        lambda: store.load_checkpoint(work.id, "research", "intruder"),
    ):
        with pytest.raises(WorkAccessDenied):
            call()
    with pytest.raises(WorkAccessDenied):
        store.amend(work.id, "collaborator", "", "bad-share", audience="owner")
    private = store.amend(work.id, "owner", "", "make-private", audience="owner")
    assert store.get(work.id, "owner") == private
    with pytest.raises(WorkAccessDenied):
        store.get(work.id, "collaborator")


def test_resolve_uses_binding_and_never_guesses_another_work(store):
    from services.work.store import WorkAccessDenied, WorkAmbiguous, WorkNotFound
    work = create(store)
    assert store.resolve("owner", "channel") == work
    other = store.create("owner", "channel", "別の比較です。", "create-2")
    with pytest.raises(WorkAmbiguous):
        store.resolve("owner", "channel")
    store.bind_message(work.id, "message", "channel")
    assert store.resolve("owner", "channel", reply_message_id="message") == work
    assert store.resolve("owner", "another-channel", work_id=other.id) == other
    with pytest.raises(WorkNotFound):
        store.resolve("owner", "channel", reply_message_id="missing")
    with pytest.raises(WorkNotFound):
        store.resolve("owner", "wrong-channel", reply_message_id="message")
    with pytest.raises(WorkAccessDenied):
        store.resolve("intruder", "channel", reply_message_id="message")


def test_binding_cannot_be_reassigned(store):
    from services.work.store import WorkIdempotencyConflict
    work = create(store)
    other = store.create("owner", "channel", "別件です。", "create-2")
    store.bind_message(work.id, "message", "channel")
    store.bind_message(work.id, "message", "channel")
    with pytest.raises(WorkIdempotencyConflict):
        store.bind_message(other.id, "message", "channel")


def test_bound_research_thread_resolves_work_once_without_reply(store):
    work = create(store)
    store.bind_message(work.id, "origin", "channel")
    store.bind_message(work.id, "thread-anchor", "research-thread")
    store.bind_message(work.id, "thread-question", "research-thread")
    assert store.list_for("owner", "channel") == [work]
    assert store.list_for("owner", "research-thread") == [work]
    assert store.list_for("owner") == [work]
    assert store.resolve("owner", "research-thread") == work
    assert store.resolve("owner", "research-thread", reply_message_id="thread-question") == work
    assert store.list_for("owner", "unrelated-thread") == []


def test_thread_binding_preserves_participant_and_private_permissions(store):
    from services.work.store import WorkNotFound
    work = create(store, participants=["collaborator"])
    store.bind_message(work.id, "thread-anchor", "research-thread")
    assert store.list_for("collaborator", "research-thread") == [work]
    assert store.list_for("intruder", "research-thread") == []
    with pytest.raises(WorkNotFound):
        store.resolve("intruder", "research-thread")
    private = store.amend(work.id, "owner", "", "private", audience="owner")
    assert store.list_for("owner", "research-thread") == [private]
    assert store.list_for("collaborator", "research-thread") == []
    with pytest.raises(WorkNotFound):
        store.resolve("collaborator", "research-thread")


@pytest.mark.parametrize("terminal_state", ["completed", "cancelled"])
def test_thread_binding_does_not_reactivate_closed_work(store, terminal_state):
    from services.work.store import WorkNotFound
    work = create(store)
    store.bind_message(work.id, "thread-anchor", "research-thread")
    store.transition(work.id, "owner", "running")
    closed = store.transition(work.id, "owner", terminal_state)
    assert store.list_for("owner", "research-thread") == []
    assert store.list_for("owner", "research-thread", active_only=False) == [closed]
    with pytest.raises(WorkNotFound):
        store.resolve("owner", "research-thread")


def test_thread_resolution_requires_explicit_selection_for_multiple_candidates(store):
    from services.work.store import WorkAmbiguous
    bound = create(store)
    local = store.create("owner", "research-thread", "スレッド内の別件です。", "thread-create")
    store.bind_message(bound.id, "thread-anchor", "research-thread")
    store.bind_message(bound.id, "thread-question", "research-thread")
    assert {work.id for work in store.list_for("owner", "research-thread")} == {bound.id, local.id}
    with pytest.raises(WorkAmbiguous):
        store.resolve("owner", "research-thread")
    assert store.resolve("owner", "research-thread", reply_message_id="thread-question") == bound


def test_required_questions_persist_and_never_use_default_or_empty_answer(store):
    from services.work.store import WorkStateConflict, WorkValidationError, WorkStore
    work = create(store)
    work = store.transition(work.id, "owner", "running")
    with pytest.raises(WorkValidationError):
        store.add_question(work.id, "予算です。", required=True, default="8万円")
    question = store.add_question(work.id, "予算を指定してください。", request_id="question-1")
    assert store.add_question(work.id, "予算を指定してください。", request_id="question-1") == question
    with pytest.raises(WorkValidationError):
        store.answer_question(work.id, question["id"], "owner", " ", "answer-empty")
    store.transition(work.id, "owner", "waiting")
    restarted = WorkStore(store.path)
    assert restarted.list_questions(work.id)[0]["status"] == "open"
    with pytest.raises(WorkStateConflict):
        restarted.transition(work.id, "owner", "completed")
    answered = restarted.answer_question(work.id, question["id"], "owner", "8万円", "answer-1")
    assert restarted.answer_question(work.id, question["id"], "owner", "8万円", "answer-1") == answered
    assert answered.requirements["answers"][question["id"]] == "8万円"
    assert restarted.list_questions(work.id, open_only=True) == []
    assert restarted.transition(work.id, "owner", "completed").state == "completed"


def test_optional_default_remains_unanswered_until_explicit_answer(store):
    work = create(store)
    q = store.add_question(work.id, "駅までの距離です。", required=False, default="徒歩10分", choices=["徒歩10分", "徒歩20分"])
    assert q["default"] == "徒歩10分" and q["status"] == "open" and q["answer"] is None
    assert store.get(work.id).revision == work.revision
    updated = store.answer_question(work.id, q["id"], "owner", "車でも構いません。", "answer-1")
    assert updated.revision == work.revision + 1


def test_wrong_work_question_is_not_answered(store):
    from services.work.store import WorkNotFound
    work = create(store)
    other = store.create("owner", "channel", "別件", "create-2")
    question = store.add_question(other.id, "回答してください。")
    with pytest.raises(WorkNotFound):
        store.answer_question(work.id, question["id"], "owner", "はい", "answer")
    assert store.list_questions(other.id)[0]["status"] == "open"


def test_artifact_stable_id_versions_and_source_idempotency(store):
    work = create(store)
    first = store.add_artifact(work.id, expected_revision=work.revision, source_id="comparison", body="A", metadata={"source_ids": ["doc1"]})
    assert store.add_artifact(work.id, expected_revision=work.revision, source_id="comparison", body="A", metadata={"source_ids": ["doc1"]}) == first
    second = store.add_artifact(work.id, expected_revision=work.revision, source_id="comparison", body="B")
    assert first["id"] == second["id"] and second["version"] == 2
    assert store.list_artifacts(work.id) == [second]
    assert store.list_artifacts(work.id, history=True) == [second, first]


def test_changed_conditions_block_old_publish_and_invalidate_artifact_checkpoint(store):
    from services.work.store import WorkRevisionConflict
    work = create(store)
    artifact = store.add_artifact(work.id, expected_revision=1, source_id="comparison", body="旧条件です。")
    store.save_checkpoint(work.id, "research", {"provider": "codex", "completed_reads": [1]}, 1)
    updated = store.amend(work.id, "owner", "新条件です。", "amend")
    with pytest.raises(WorkRevisionConflict):
        store.add_artifact(work.id, expected_revision=1, source_id="comparison", body="古い実行の結果です。")
    with pytest.raises(WorkRevisionConflict):
        store.save_checkpoint(work.id, "research", {}, 1)
    assert store.list_artifacts(work.id) == []
    assert store.load_checkpoint(work.id, "research") is None
    invalid = store.list_artifacts(work.id, include_invalidated=True)
    assert invalid[0]["invalidated"]
    new = store.add_artifact(work.id, expected_revision=updated.revision, source_id="comparison", body="新条件です。")
    assert new["id"] == artifact["id"] and new["version"] == 2


def test_replayed_artifact_request_cannot_bypass_current_revision_guard(store):
    from services.work.store import WorkRevisionConflict
    work = create(store)
    artifact_args = dict(expected_revision=1, source_id="comparison", body="旧条件です。", request_id="saved-result")
    store.add_artifact(work.id, **artifact_args)
    store.amend(work.id, "owner", "新条件です。", "amend")
    with pytest.raises(WorkRevisionConflict):
        store.add_artifact(work.id, **artifact_args)


def test_shared_to_owner_invalidates_previous_outputs_even_after_shared_again(store):
    work = create(store)
    store.add_artifact(work.id, expected_revision=1, source_id="comparison", storage_refs=[{"uri": "r2://report/key"}])
    store.save_checkpoint(work.id, "research", {"opaque_summary": "private"}, 1)
    store.amend(work.id, "owner", "", "private", audience="owner")
    store.amend(work.id, "owner", "", "shared", audience="shared")
    assert store.list_artifacts(work.id) == []
    assert store.load_checkpoint(work.id, "research") is None


def test_completed_work_reopens_but_cancelled_work_does_not(store):
    from services.work.store import WorkStateConflict
    work = create(store)
    store.transition(work.id, "owner", "running")
    done = store.transition(work.id, "owner", "completed")
    updated = store.amend(work.id, "owner", "翌日の新条件です。", "tomorrow")
    assert updated.state == "queued" and updated.revision == done.revision + 1
    store.transition(work.id, "owner", "running")
    store.transition(work.id, "owner", "completed")
    assert store.transition(work.id, "owner", "queued").state == "queued"
    cancelled = store.transition(work.id, "owner", "cancelled")
    with pytest.raises(WorkStateConflict):
        store.transition(work.id, "owner", "queued")
    with pytest.raises(WorkStateConflict):
        store.amend(work.id, "owner", "再開します。", "resurrect")
    assert store.get(work.id) == cancelled


def test_state_boundaries_block_artifacts_and_old_effect_claims(store):
    from services.work.store import WorkRevisionConflict, WorkStateConflict
    work = create(store)
    paused = store.transition(work.id, "owner", "paused")
    assert store.transition(work.id, "owner", "paused") == paused
    with pytest.raises(WorkRevisionConflict):
        store.assert_current(work.id, work.revision, "owner")
    with pytest.raises(WorkStateConflict):
        store.assert_current(work.id, paused.revision, "owner", allowed_states={"running"})
    with pytest.raises(WorkStateConflict):
        store.add_artifact(work.id, expected_revision=paused.revision, source_id="a", body="A")
    checkpoint = store.save_checkpoint(work.id, "research", {"completed_reads": ["A"]}, paused.revision)
    assert checkpoint["payload"] == {"completed_reads": ["A"]}


def test_transaction_rolls_back_nested_domain_operations_and_own_tables(store):
    with pytest.raises(RuntimeError):
        with store.transaction() as connection:
            work = create(store)
            connection.execute("CREATE TABLE effect_claims (work_id TEXT)")
            connection.execute("INSERT INTO effect_claims VALUES (?)", (work.id,))
            store.amend(work.id, "owner", "未確定です。", "amend")
            raise RuntimeError("rollback")
    assert store.list_for("owner") == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='effect_claims'").fetchone() is None


def test_inner_savepoint_rollback_does_not_rollback_outer_transaction(store):
    with store.transaction():
        work = create(store)
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.amend(work.id, "owner", "破棄する変更です。", "bad")
                raise RuntimeError("abort inner")
        store.record_event(work.id, "read_completed", {"source": "doc"}, request_id="read-1")
    assert store.get(work.id).revision == 1
    assert [e["kind"] for e in store.list_events(work.id)] == ["accepted", "read_completed"]


def test_event_idempotency_and_pagination(store):
    work = create(store)
    event = store.record_event(work.id, "read_completed", {"source": "doc"}, request_id="read-1")
    assert store.record_event(work.id, "read_completed", {"source": "doc"}, request_id="read-1") == event
    assert store.list_events(work.id, after_id=1, limit=1) == [event]


def test_recover_does_not_resume_unknown_external_effect(store):
    work = create(store)
    other = store.create("owner", "channel", "読み取り用件です。", "create-2")
    store.transition(work.id, "owner", "running")
    store.transition(other.id, "owner", "running")
    store.record_event(work.id, "effect_dispatching", {"effect_id": "send-1"})
    recovered = {w.id: w for w in store.recover()}
    assert recovered[work.id].state == "needs_reconciliation"
    assert recovered[other.id].state == "paused"
    assert store.recover() == []


def test_recovery_tracks_each_external_effect_separately(store):
    work = create(store)
    store.transition(work.id, "owner", "running")
    store.record_event(work.id, "effect_dispatching", {"effect_id": "send-1"})
    store.record_event(work.id, "effect_dispatching", {"effect_id": "send-2"})
    store.record_event(work.id, "effect_confirmed", {"effect_id": "send-2"})
    assert store.recover()[0].state == "needs_reconciliation"


def test_confirmed_external_effect_recovers_to_paused_without_replay(store):
    work = create(store)
    store.transition(work.id, "owner", "running")
    store.record_event(work.id, "effect_dispatching", {"operation": "publish"})
    store.record_event(work.id, "effect_confirmed", {"operation": "publish"})
    assert store.recover()[0].state == "paused"


def test_format_context_and_snapshot_do_not_mutate_saved_fields(store):
    work = create(store, preferences={"verbosity": "brief"}, next_action="予算を確認します。")
    snap = store.snapshot(work.id, "owner")
    snap["preferences"]["verbosity"] = "long"
    assert store.get(work.id).preferences == {"verbosity": "brief"}
    context = store.format_context(work.id, "owner")
    assert work.id in context and "予算を確認します。" in context


def test_execution_updates_preserve_revision_authority_and_unspecified_fields(store):
    work = create(store, requirements={"rent": 80000}, progress=["以前の確認です。"],
                  participants=["collaborator"], next_action="比較します。")
    store.save_checkpoint(work.id, "execution", {"memory_epoch": 7}, work.revision)
    updated = store.update_execution(work.id, "owner", work.revision, decisions=["候補Aです。"], unknowns=["管理費です。"])
    assert updated.revision == work.revision
    assert updated.decisions == ["候補Aです。"] and updated.unknowns == ["管理費です。"]
    assert updated.progress == work.progress and updated.next_action == work.next_action
    assert updated.goal == work.goal and updated.requirements == work.requirements
    assert updated.audience == work.audience and updated.participants == work.participants
    assert updated.state == work.state and store.load_checkpoint(work.id, "execution") is not None
    assert store.list_events(work.id)[-1]["kind"] == "execution_updated"
    assert store.list_events(work.id)[-1]["revision"] == work.revision
    cleared = store.update_execution(work.id, "owner", work.revision, progress=[], next_action="")
    assert cleared.progress == [] and cleared.next_action == ""


def test_execution_update_rejects_stale_unauthorized_and_invalid_fields(store):
    from services.work.store import WorkAccessDenied, WorkRevisionConflict, WorkValidationError
    work = create(store)
    with pytest.raises(WorkAccessDenied):
        store.update_execution(work.id, "intruder", work.revision, decisions=["変更します。"])
    with pytest.raises(WorkValidationError):
        store.update_execution(work.id, "owner", work.revision, decisions="リストではありません。")
    with pytest.raises(TypeError):
        store.update_execution(work.id, "owner", work.revision, audience="owner")
    store.amend(work.id, "owner", "新条件です。", "amend")
    with pytest.raises(WorkRevisionConflict):
        store.update_execution(work.id, "owner", work.revision, progress=["旧条件の結果です。"])
    assert store.get(work.id).progress == []


def test_execution_update_rolls_back_with_enclosing_checkpoint_transaction(store):
    work = create(store)
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.update_execution(work.id, "owner", work.revision, progress=["未確定です。"])
            store.save_checkpoint(work.id, "execution", {"progress": ["未確定です。"]}, work.revision)
            raise RuntimeError("checkpoint transaction failed")
    assert store.get(work.id).progress == []
    assert store.load_checkpoint(work.id, "execution") is None
    assert len(store.list_events(work.id)) == 1


def test_storage_failure_has_no_false_acceptance(tmp_path):
    from services.work.store import WorkStorageError, WorkStore
    parent = tmp_path / "file"
    parent.write_text("not a directory")
    with pytest.raises(WorkStorageError):
        WorkStore(parent / "db")


def test_default_store_is_lazily_created_under_state_dir(tmp_path, monkeypatch):
    import config
    import services.work.store as module
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(module, "_default_store", None)
    assert not (tmp_path / "state").exists()
    assert module.get_work_store().path == tmp_path / "state" / "work.sqlite3"
    assert module.get_work_store() is module.get_work_store()
