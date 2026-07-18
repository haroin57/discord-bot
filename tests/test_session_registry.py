"""Contracts for the single-owner session registry and repository."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import OrderedDict
from collections.abc import MutableMapping
from dataclasses import replace

import pytest

from services.session_registry import (
    RegistrySnapshot,
    SessionInfo,
    SessionRecord,
    SessionRegistry,
    SessionRegistryClosedError,
    SessionRegistryLoadError,
    SessionRepository,
)


class TestSessionRepositoryCharacterization:
    def test_legacy_channel_mapping_is_explicitly_invalidated(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text(
            json.dumps(
                {
                    "10": "legacy-id",
                    "11": {"session_id": "dict-id", "mcp_server_keys": ["mcp"]},
                }
            ),
            encoding="utf-8",
        )

        snapshot = SessionRepository(str(path)).load()

        assert snapshot.sessions == ()
        assert snapshot.invalidate_all_provider_continuity is True
        assert snapshot.validated_providers == frozenset()
        assert snapshot.needs_rewrite is True

    def test_corrupt_json_fails_closed_and_rewrites_v2(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text("{broken", encoding="utf-8")
        registry = SessionRegistry(SessionRepository(str(path)), max_sessions=100)

        registry.load()

        assert registry.is_provider_invalid("claude", 10, 20) is True
        assert registry.is_provider_invalid("codex", 10, 20) is True
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == 2
        assert payload["invalidate_all_provider_continuity"] is True

    def test_unknown_version_fails_closed(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text(json.dumps({"version": 999, "sessions": []}), encoding="utf-8")
        registry = SessionRegistry(SessionRepository(str(path)), max_sessions=100)

        registry.load()

        assert registry.is_provider_invalid("codex", 10, 20) is True
        assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2

    def test_fail_closed_validation_is_scoped_and_persists(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text("{broken", encoding="utf-8")
        registry = SessionRegistry(SessionRepository(str(path)), max_sessions=100)
        registry.load()

        registry.validate_provider("codex", 10, 20)

        restored = SessionRegistry(SessionRepository(str(path)), max_sessions=100)
        restored.load()
        assert restored.is_provider_invalid("codex", 10, 20) is False
        assert restored.is_provider_invalid("codex", 10, 21) is True
        assert restored.is_provider_invalid("claude", 10, 20) is True


class TestSessionRepositoryContract:
    def test_composite_keys_and_invalidations_survive_round_trip(self, tmp_path):
        path = tmp_path / "sessions.json"
        repository = SessionRepository(str(path))
        registry = SessionRegistry(repository, max_sessions=100)
        registry.set(10, 1, SessionInfo("one", ["a"]))
        registry.set(10, 2, SessionInfo("two", ["b"]))
        registry.invalidate_provider("codex", 10, 1)

        restored = SessionRegistry(repository, max_sessions=100)
        restored.load()

        assert restored.get(10, 1).session_id == "one"
        assert restored.get(10, 2).session_id == "two"
        assert restored.is_provider_invalid("codex", 10, 1) is True

    def test_channel_view_is_backed_by_registry_state(self, tmp_path):
        registry = SessionRegistry(
            SessionRepository(str(tmp_path / "sessions.json")),
            max_sessions=100,
        )
        view = registry.channel_view

        view[10] = SessionInfo("legacy")
        assert registry.get(10, None).session_id == "legacy"

        registry.set(11, None, SessionInfo("registry"))
        assert view[11].session_id == "registry"

    def test_channel_view_never_projects_user_owned_sessions(self, tmp_path):
        registry = SessionRegistry(
            SessionRepository(str(tmp_path / "sessions.json")),
            max_sessions=100,
        )
        registry.set(10, 1, SessionInfo("user-one"))
        registry.set(10, 2, SessionInfo("user-two"))

        assert 10 not in registry.channel_view
        with pytest.raises(KeyError):
            _ = registry.channel_view[10]

        registry.channel_view[10] = SessionInfo("legacy")
        assert registry.channel_view[10].session_id == "legacy"
        del registry.channel_view[10]
        assert registry.get(10, 1).session_id == "user-one"
        assert registry.get(10, 2).session_id == "user-two"

    def test_claude_invalidation_atomically_removes_resumable_session(self, tmp_path):
        registry = SessionRegistry(
            SessionRepository(str(tmp_path / "sessions.json")),
            max_sessions=100,
        )
        registry.set(10, 20, SessionInfo("stale"))

        registry.invalidate_provider("claude", 10, 20)

        assert registry.get(10, 20) is None
        assert registry.is_provider_invalid("claude", 10, 20) is True

    def test_re_registering_same_session_object_is_idempotent(self, tmp_path):
        registry = SessionRegistry(
            SessionRepository(str(tmp_path / "sessions.json")),
            max_sessions=100,
        )
        info = SessionInfo("same")
        registry.set(10, 20, info)
        revision = registry.snapshot().revision

        registry.set(10, 20, info)

        assert registry.snapshot().revision == revision

    def test_mutated_retrieved_session_advances_revision_once(self, tmp_path):
        registry = SessionRegistry(
            SessionRepository(str(tmp_path / "sessions.json")),
            max_sessions=100,
        )
        registry.set(10, 20, SessionInfo("first", ["a"], {"tokens": 1}))
        info = registry.get(10, 20)
        assert info is not None
        revision = registry.snapshot().revision

        info.session_id = "second"
        info.mcp_server_keys.append("b")
        assert info.last_usage is not None
        info.last_usage["tokens"] = 2
        registry.set(10, 20, info)

        assert registry.snapshot().revision == revision + 1
        stored = registry.get(10, 20)
        assert stored == SessionInfo("second", ["a", "b"], {"tokens": 2})

        registry.set(10, 20, info)
        assert registry.snapshot().revision == revision + 1

        info.mcp_server_keys.append("external")
        assert registry.get(10, 20).mcp_server_keys == ["a", "b"]


class TestChannelSessionViewWriterContract:
    def test_all_mapping_writers_are_explicitly_overridden(self, tmp_path):
        registry = SessionRegistry(
            SessionRepository(str(tmp_path / "sessions.json")),
            max_sessions=100,
        )
        view_type = type(registry.channel_view)
        inherited_writers = {
            "__setitem__",
            "__delitem__",
            "clear",
            "pop",
            "popitem",
            "setdefault",
            "update",
        }

        assert inherited_writers <= set(MutableMapping.__dict__)
        assert inherited_writers | {"__ior__", "move_to_end"} <= set(view_type.__dict__)

    def test_normal_writers_preserve_ordered_dict_semantics(self, tmp_path):
        registry = SessionRegistry(
            SessionRepository(str(tmp_path / "sessions.json")),
            max_sessions=100,
        )
        registry.set(99, 7, SessionInfo("user-owned"))
        view = registry.channel_view
        expected: OrderedDict[int, SessionInfo] = OrderedDict()

        initial = {10: SessionInfo("ten"), 20: SessionInfo("twenty")}
        view.update(initial)
        expected.update(initial)
        assert list(view.items()) == list(expected.items())

        replacement = SessionInfo("replacement")
        assert view.setdefault(10, replacement) == expected.setdefault(10, replacement)
        inserted = SessionInfo("thirty")
        assert view.setdefault(30, inserted) is inserted
        assert expected.setdefault(30, inserted) is inserted

        view |= {40: SessionInfo("forty")}
        expected |= {40: SessionInfo("forty")}
        view.move_to_end(10)
        expected.move_to_end(10)
        view.move_to_end(30, last=False)
        expected.move_to_end(30, last=False)
        assert list(view.items()) == list(expected.items())

        assert view.popitem(last=False) == expected.popitem(last=False)
        assert view.popitem() == expected.popitem()
        assert view.pop(20) == expected.pop(20)
        marker = object()
        assert view.pop(404, marker) is marker
        assert expected.pop(404, marker) is marker
        with pytest.raises(KeyError):
            view.pop(404)
        with pytest.raises(KeyError):
            expected.pop(404)

        view.clear()
        expected.clear()
        assert list(view.items()) == list(expected.items()) == []
        assert registry.get(99, 7) == SessionInfo("user-owned")
        with pytest.raises(KeyError):
            view.popitem(last=False)
        with pytest.raises(KeyError):
            view.move_to_end(404)
        with pytest.raises(TypeError):
            view.setdefault(404)

    def test_empty_and_missing_writers_share_sticky_load_failure(self):
        load_error = OSError("durable state unavailable")
        saves: list[RegistrySnapshot] = []

        class FailingRepository:
            def load(self) -> RegistrySnapshot:
                raise load_error

            def save(self, snapshot: RegistrySnapshot) -> None:
                saves.append(snapshot)

        registry = SessionRegistry(FailingRepository(), max_sessions=100)  # type: ignore[arg-type]
        view = registry.channel_view
        writers = [
            lambda: view.__setitem__(10, SessionInfo("blocked")),
            lambda: view.__delitem__(10),
            view.clear,
            lambda: view.pop(10),
            lambda: view.pop(10, None),
            view.popitem,
            lambda: view.popitem(last=False),
            lambda: view.setdefault(10),
            lambda: view.setdefault(10, SessionInfo("blocked")),
            lambda: view.update(),
            lambda: view.update({}),
            lambda: view.__ior__({}),
            lambda: view.move_to_end(10),
        ]

        sticky_failure = None
        for writer in writers:
            with pytest.raises(SessionRegistryLoadError) as caught:
                writer()
            assert caught.value.__cause__ is load_error
            if sticky_failure is None:
                sticky_failure = caught.value
            else:
                assert caught.value is sticky_failure

        assert registry.snapshot() == RegistrySnapshot(0, (), frozenset())
        assert saves == []

    def test_existing_key_writers_share_sticky_rewrite_failure(self):
        load_error = OSError("rewrite unavailable")
        save_calls = 0
        durable = RegistrySnapshot(
            4,
            (SessionRecord(10, None, "durable", ()),),
            frozenset(),
            needs_rewrite=True,
        )

        class RewriteFailingRepository:
            def load(self) -> RegistrySnapshot:
                return durable

            def save(self, snapshot: RegistrySnapshot) -> None:
                nonlocal save_calls
                save_calls += 1
                raise load_error

        registry = SessionRegistry(RewriteFailingRepository(), max_sessions=100)  # type: ignore[arg-type]
        with pytest.raises(OSError) as first_use:
            registry.load()
        assert first_use.value is load_error
        view = registry.channel_view
        assert view[10] == SessionInfo("durable")

        writers = [
            lambda: view.__setitem__(10, SessionInfo("blocked")),
            lambda: view.__delitem__(10),
            view.clear,
            lambda: view.pop(10),
            view.popitem,
            lambda: view.setdefault(10, SessionInfo("blocked")),
            lambda: view.update({10: SessionInfo("blocked")}),
            lambda: view.__ior__({10: SessionInfo("blocked")}),
            lambda: view.move_to_end(10),
        ]

        sticky_failure = None
        for writer in writers:
            with pytest.raises(SessionRegistryLoadError) as caught:
                writer()
            assert caught.value.__cause__ is load_error
            if sticky_failure is None:
                sticky_failure = caught.value
            else:
                assert caught.value is sticky_failure

        assert view[10] == SessionInfo("durable")
        assert save_calls == 1


class TestOrderedSaveRegression:
    async def test_first_use_load_failure_is_sticky_across_force_reload(self):
        load_error = OSError("durable state unavailable")
        durable = RegistrySnapshot(
            7,
            (SessionRecord(10, 20, "durable", ("mcp",)),),
            frozenset({("codex", 10, 20)}),
        )

        class RecoveringRepository:
            def __init__(self) -> None:
                self.load_calls = 0

            def load(self) -> RegistrySnapshot:
                self.load_calls += 1
                if self.load_calls == 1:
                    raise load_error
                return durable

            def save(self, snapshot: RegistrySnapshot) -> None:
                raise AssertionError(f"unexpected save: {snapshot!r}")

        repository = RecoveringRepository()
        registry = SessionRegistry(repository, max_sessions=100)  # type: ignore[arg-type]

        with pytest.raises(SessionRegistryLoadError) as first_use:
            await registry.ensure_loaded()
        assert first_use.value.__cause__ is load_error

        with pytest.raises(OSError) as forced:
            registry.load(force=True)
        assert forced.value is load_error
        with pytest.raises(SessionRegistryLoadError) as repeated:
            await registry.ensure_loaded()
        assert repeated.value is first_use.value
        assert repeated.value.__cause__ is load_error
        assert repository.load_calls == 1

        replacement = SessionRegistry(repository, max_sessions=100)  # type: ignore[arg-type]
        replacement.load()
        assert replacement.get(10, 20) == SessionInfo("durable", ["mcp"])
        assert replacement.is_provider_invalid("codex", 10, 20) is True

        registry.reset_memory_only()
        registry.load(force=True)
        assert registry.get(10, 20) == SessionInfo("durable", ["mcp"])

    async def test_async_saves_are_serialized_and_latest_revision_wins(self):
        first_started = threading.Event()
        release_first = threading.Event()
        saved: list[RegistrySnapshot] = []

        class RecordingRepository:
            def save(self, snapshot: RegistrySnapshot) -> None:
                if not saved:
                    first_started.set()
                    assert release_first.wait(timeout=2)
                saved.append(snapshot)

            def load(self) -> RegistrySnapshot:
                return RegistrySnapshot(0, (), frozenset())

        registry = SessionRegistry(RecordingRepository(), max_sessions=100)  # type: ignore[arg-type]
        registry.set(10, 20, SessionInfo("first"))
        assert await asyncio.to_thread(first_started.wait, 2)

        registry.set(10, 20, SessionInfo("second"))
        release_first.set()
        await registry.flush()

        assert [snapshot.revision for snapshot in saved] == [1, 2]
        assert saved[-1].sessions[0].session_id == "second"

    async def test_failed_old_save_keeps_newer_pending_revision(self):
        first_started = threading.Event()
        release_first = threading.Event()
        attempts = 0
        saved: list[RegistrySnapshot] = []

        class FailingOnceRepository:
            def save(self, snapshot: RegistrySnapshot) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    first_started.set()
                    assert release_first.wait(timeout=2)
                    raise OSError("disk unavailable")
                saved.append(snapshot)

            def load(self) -> RegistrySnapshot:
                return RegistrySnapshot(0, (), frozenset())

        registry = SessionRegistry(FailingOnceRepository(), max_sessions=100)  # type: ignore[arg-type]
        registry.set(10, 20, SessionInfo("first"))
        assert await asyncio.to_thread(first_started.wait, 2)
        registry.set(10, 20, SessionInfo("second"))
        release_first.set()

        try:
            await registry.flush()
        except OSError:
            pass
        else:
            raise AssertionError("the first durable save must fail")

        assert registry._pending_snapshot is not None
        assert registry._pending_snapshot.revision == 2
        await registry.flush()
        assert saved[-1].revision == 2
        assert saved[-1].sessions[0].session_id == "second"

    async def test_flush_sync_serializes_after_running_old_writer(self):
        first_started = threading.Event()
        release_first = threading.Event()
        saved: list[RegistrySnapshot] = []

        class SlowRepository:
            def save(self, snapshot: RegistrySnapshot) -> None:
                if snapshot.revision == 1:
                    first_started.set()
                    assert release_first.wait(timeout=2)
                saved.append(snapshot)

            def load(self) -> RegistrySnapshot:
                return saved[-1] if saved else RegistrySnapshot(0, (), frozenset())

        registry = SessionRegistry(SlowRepository(), max_sessions=100)  # type: ignore[arg-type]
        registry.set(10, 20, SessionInfo("first"))
        assert await asyncio.to_thread(first_started.wait, 2)
        registry.set(10, 20, SessionInfo("second"))
        threading.Timer(0.01, release_first.set).start()

        registry.flush_sync()
        await asyncio.sleep(0)

        assert [snapshot.revision for snapshot in saved] == [1, 2]
        assert saved[-1].sessions[0].session_id == "second"

    async def test_force_reload_drains_before_replacing_memory(self):
        first_started = threading.Event()
        release_first = threading.Event()
        persisted = RegistrySnapshot(0, (), frozenset())

        class SlowRepository:
            def save(self, snapshot: RegistrySnapshot) -> None:
                nonlocal persisted
                if snapshot.revision == 1:
                    first_started.set()
                    assert release_first.wait(timeout=2)
                persisted = snapshot

            def load(self) -> RegistrySnapshot:
                return persisted

        registry = SessionRegistry(SlowRepository(), max_sessions=100)  # type: ignore[arg-type]
        registry.set(10, 20, SessionInfo("first"))
        assert await asyncio.to_thread(first_started.wait, 2)
        registry.set(10, 20, SessionInfo("second"))
        threading.Timer(0.01, release_first.set).start()

        registry.load(force=True)
        await asyncio.sleep(0)

        assert registry.get(10, 20).session_id == "second"
        assert persisted.sessions[0].session_id == "second"


class TestSessionRegistryCloseContract:
    async def test_close_replays_sync_first_use_load_failure_by_identity(self):
        load_error = OSError("synchronous durable state unavailable")
        saves: list[RegistrySnapshot] = []

        class FailingRepository:
            def load(self) -> RegistrySnapshot:
                raise load_error

            def save(self, snapshot: RegistrySnapshot) -> None:
                saves.append(snapshot)

        registry = SessionRegistry(FailingRepository(), max_sessions=100)  # type: ignore[arg-type]
        with pytest.raises(OSError) as first_use:
            registry.load()
        assert first_use.value is load_error
        assert registry._load_task is None

        with pytest.raises(OSError) as close_failure:
            await registry.close()
        await asyncio.sleep(0)

        assert close_failure.value is load_error
        assert registry._close_error is load_error
        assert registry.snapshot() == RegistrySnapshot(0, (), frozenset())
        assert saves == []

        with pytest.raises(OSError) as repeated:
            await registry.close()
        assert repeated.value is load_error

    async def test_close_replays_completed_first_use_load_failure(self):
        load_error = OSError("durable state unavailable")

        class FailingRepository:
            def load(self) -> RegistrySnapshot:
                raise load_error

            def save(self, snapshot: RegistrySnapshot) -> None:
                raise AssertionError(f"unexpected save: {snapshot!r}")

        registry = SessionRegistry(FailingRepository(), max_sessions=100)  # type: ignore[arg-type]
        with pytest.raises(SessionRegistryLoadError):
            await registry.ensure_loaded()
        assert registry._load_task is not None
        assert registry._load_task.done() is True

        with pytest.raises(OSError) as close_failure:
            await registry.close()

        await asyncio.sleep(0)
        assert close_failure.value is load_error
        assert registry._close_error is load_error

    async def test_close_waits_for_admitted_first_use_load_before_read_fence(self):
        load_started = threading.Event()
        release_load = threading.Event()
        durable = RegistrySnapshot(
            9,
            (SessionRecord(10, 20, "durable", ("mcp",)),),
            frozenset({("codex", 10, 20), ("claude", 11, 21)}),
        )

        class BlockingRepository:
            def load(self) -> RegistrySnapshot:
                load_started.set()
                assert release_load.wait(timeout=2)
                return durable

            def save(self, snapshot: RegistrySnapshot) -> None:
                raise AssertionError(f"unexpected save: {snapshot!r}")

        registry = SessionRegistry(BlockingRepository(), max_sessions=100)  # type: ignore[arg-type]
        load_task = asyncio.create_task(registry.ensure_loaded())
        assert await asyncio.to_thread(load_started.wait, 2)

        close_task = asyncio.create_task(registry.close())
        await asyncio.sleep(0)
        assert registry._closed is True
        assert close_task.done() is False
        with pytest.raises(SessionRegistryClosedError):
            registry.load(force=True)
        closing_writers = [
            registry.channel_view.clear,
            lambda: registry.channel_view.pop(404, None),
            lambda: registry.channel_view.update({}),
            lambda: registry.channel_view.__ior__({}),
        ]
        for writer in closing_writers:
            with pytest.raises(SessionRegistryClosedError):
                writer()

        release_load.set()
        assert await load_task == durable
        await close_task

        assert registry.snapshot() == durable
        assert registry.get(10, 20) == SessionInfo("durable", ["mcp"])
        assert registry.is_provider_invalid("codex", 10, 20) is True
        assert registry.is_provider_invalid("claude", 11, 21) is True
        with pytest.raises(SessionRegistryClosedError):
            await registry.ensure_loaded()

    async def test_close_rejects_all_mutations_and_preserves_read_access(
        self,
        tmp_path,
    ):
        path = tmp_path / "sessions.json"
        registry = SessionRegistry(SessionRepository(str(path)), max_sessions=100)
        registry.set(10, None, SessionInfo("preserved"))
        registry.invalidate_provider("codex", 10, None)
        await registry.close()
        durable_before = path.read_bytes()
        snapshot_before = registry.snapshot()

        def _delete_channel_view() -> None:
            del registry.channel_view[10]

        mutations = [
            lambda: registry.set(11, None, SessionInfo("new")),
            lambda: registry.replace_channel_sessions({}),
            lambda: registry.clear(10, None),
            lambda: registry.clear_all_for_channel(10),
            registry.clear_all,
            lambda: registry.invalidate_provider("claude", 10, None),
            lambda: registry.validate_provider("codex", 10, None),
            registry.request_save,
            lambda: registry.channel_view.__setitem__(
                11,
                SessionInfo("view-update"),
            ),
            _delete_channel_view,
            registry.channel_view.clear,
            lambda: registry.channel_view.update({11: SessionInfo("mapping-update")}),
            lambda: registry.channel_view.update({}),
            lambda: registry.channel_view.pop(10),
            lambda: registry.channel_view.pop(404, None),
            lambda: registry.channel_view.popitem(),
            lambda: registry.channel_view.popitem(last=False),
            lambda: registry.channel_view.setdefault(10, SessionInfo("existing")),
            lambda: registry.channel_view.setdefault(404, SessionInfo("missing")),
            lambda: registry.channel_view.__ior__({}),
            lambda: registry.channel_view.move_to_end(10),
        ]
        for mutation in mutations:
            with pytest.raises(SessionRegistryClosedError):
                mutation()

        with pytest.raises(SessionRegistryClosedError):
            registry.load(force=True)
        with pytest.raises(SessionRegistryClosedError):
            registry.flush_sync()
        with pytest.raises(SessionRegistryClosedError):
            await registry.flush()

        # The read contract remains available after close.
        assert registry.snapshot() == snapshot_before
        stored = registry.get(10, None)
        assert stored is not None
        assert stored.session_id == "preserved"
        assert registry.is_provider_invalid("codex", 10, None) is True
        assert registry.channel_projection()[10].session_id == "preserved"
        assert registry.channel_view[10].session_id == "preserved"

        await asyncio.sleep(0)
        assert registry.snapshot() == snapshot_before
        assert path.read_bytes() == durable_before

    async def test_sticky_load_failure_blocks_every_registry_writer(self):
        load_error = OSError("durable state unavailable")
        saves: list[RegistrySnapshot] = []

        class FailingRepository:
            def load(self) -> RegistrySnapshot:
                raise load_error

            def save(self, snapshot: RegistrySnapshot) -> None:
                saves.append(snapshot)

        registry = SessionRegistry(FailingRepository(), max_sessions=100)  # type: ignore[arg-type]
        snapshot_before = registry.snapshot()

        mutations = [
            lambda: registry.set(10, None, SessionInfo("new")),
            lambda: registry.replace_channel_sessions({10: SessionInfo("new")}),
            lambda: registry.clear(10, None),
            lambda: registry.clear_all_for_channel(10),
            registry.clear_all,
            lambda: registry.invalidate_provider("claude", 10, None),
            lambda: registry.validate_provider("codex", 10, None),
            registry.request_save,
            registry.flush_sync,
            lambda: registry.channel_view.__setitem__(10, SessionInfo("view")),
            lambda: registry.channel_view.__delitem__(10),
            lambda: registry.channel_view.update({10: SessionInfo("mapping")}),
        ]

        sticky_failure = None
        for mutation in mutations:
            with pytest.raises(SessionRegistryLoadError) as caught:
                mutation()
            assert caught.value.__cause__ is load_error
            if sticky_failure is None:
                sticky_failure = caught.value
            else:
                assert caught.value is sticky_failure

        with pytest.raises(SessionRegistryLoadError) as async_flush:
            await registry.flush()
        assert async_flush.value is sticky_failure
        assert async_flush.value.__cause__ is load_error
        assert registry.snapshot() == snapshot_before
        assert saves == []

    async def test_cancelled_close_waiter_replays_sticky_save_failure(self):
        save_started = threading.Event()
        release_save = threading.Event()
        save_error = OSError("close persistence failed")

        class FailingRepository:
            def load(self) -> RegistrySnapshot:
                return RegistrySnapshot(0, (), frozenset())

            def save(self, snapshot: RegistrySnapshot) -> None:
                save_started.set()
                assert release_save.wait(timeout=2)
                raise save_error

        registry = SessionRegistry(FailingRepository(), max_sessions=100)  # type: ignore[arg-type]
        registry.set(10, None, SessionInfo("pending"))
        assert await asyncio.to_thread(save_started.wait, 2)

        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            waiter = asyncio.create_task(registry.close())
            await asyncio.sleep(0)
            assert waiter.cancel("stop waiting for close") is True
            with pytest.raises(asyncio.CancelledError):
                await waiter

            release_save.set()
            close_task = registry._close_task
            assert close_task is not None
            while not close_task.done():
                await asyncio.sleep(0)
            await asyncio.sleep(0)

            assert registry._close_error is save_error
            with pytest.raises(OSError) as caught:
                await registry.close()
            assert caught.value is save_error
            assert close_task.exception() is save_error
            await asyncio.sleep(0)
            assert unhandled == []
        finally:
            release_save.set()
            loop.set_exception_handler(previous_handler)


def test_session_info_public_contract_is_preserved():
    from services.session import SessionInfo as PublicSessionInfo

    assert PublicSessionInfo is SessionInfo
    info = PublicSessionInfo("id", ["mcp"], {"input_tokens": 1})
    assert replace(info, session_id="new").session_id == "new"
