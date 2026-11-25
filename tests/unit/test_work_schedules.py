"""Opt-in watches survive restart without stale or duplicate external effects."""

import asyncio
from datetime import datetime, timezone
import threading

import pytest

from services.work.effects import AdapterCapabilities, AdapterResult, EffectExecutor, ScopeError
from services.work.schedules import Observation, ScheduleService, evaluate_criteria
from bot_commands.watch import parse_time


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Notifier:
    capabilities = AdapterCapabilities(("notify",), False, True, False, "message created", "channel_id")

    def __init__(self):
        self.messages = {}
        self.calls = 0
        self.lose_response = False

    async def submit(self, payload, *, idempotency_key):
        self.calls += 1
        self.messages[idempotency_key] = AdapterResult(remote_id=str(self.calls), result=payload)
        if self.lose_response:
            raise TimeoutError("response lost")
        return self.messages[idempotency_key]

    async def reconcile(self, receipt):
        return self.messages.get(receipt.id)


def services(tmp_path):
    clock, notifier = Clock(), Notifier()
    effect = EffectExecutor(tmp_path / "work.sqlite3", clock=clock)
    effect.register_adapter("discord", notifier)
    schedule = ScheduleService(effect.path, effect, clock=clock)
    schedule.register_authorizer("work", lambda owner, target, recipient: owner == "alice" and recipient == "55")
    return clock, notifier, effect, schedule


async def test_reminder_is_persisted_before_ack_and_survives_restart(tmp_path):
    clock, notifier, effect, schedule = services(tmp_path)
    saved = schedule.add_reminder("alice", "55", "tomorrow's deadline", run_at=1010)
    assert notifier.calls == 0
    assert await schedule.tick() == 0
    restored = ScheduleService(effect.path, effect, clock=clock)
    assert restored.get(saved.id, "alice").content == "tomorrow's deadline"
    clock.now = 1010
    await restored.tick()
    await restored.tick()
    assert notifier.calls == 1
    assert restored.get(saved.id).status == "completed"


async def test_expired_and_cancelled_schedules_never_start_notification(tmp_path):
    clock, notifier, _, schedule = services(tmp_path)
    expired = schedule.add_reminder("alice", "55", "old", run_at=1010, expires_at=1020)
    cancelled = schedule.add_reminder("alice", "55", "cancelled", run_at=1010)
    schedule.cancel(cancelled.id, "alice")
    clock.now = 1021
    await schedule.tick()
    assert notifier.calls == 0
    assert schedule.get(expired.id).status == "expired"
    assert schedule.get(cancelled.id).status == "cancelled"


async def test_watch_condition_change_cooldown_and_stop_first_match(tmp_path):
    clock, notifier, _, schedule = services(tmp_path)
    current = {"price": 120}

    async def observe(target, criteria, context):
        assert context.owner_id == "alice"
        return Observation(dict(current), f"Price {current['price']}")

    schedule.register_observer("listing", observe)
    watch = schedule.add_watch("alice", "55", observer="listing", target="listing-a",
                               criteria={"field": "price", "op": "lte", "value": 100},
                               expires_at=1500, interval_seconds=10, cooldown_seconds=30)
    await schedule.tick()
    assert notifier.calls == 0
    current["price"] = 95
    clock.now += 10
    await schedule.tick()
    assert notifier.calls == 1
    clock.now += 10
    await schedule.tick()
    assert notifier.calls == 1
    current["price"] = 90
    clock.now += 10
    await schedule.tick()
    assert notifier.calls == 1
    clock.now += 10
    await schedule.tick()
    assert notifier.calls == 2
    assert schedule.get(watch.id).notifications == 2
    once = schedule.add_watch("alice", "55", observer="listing", target="listing-a", criteria={},
                               expires_at=1500, interval_seconds=10, stop_rule="first_match")
    await schedule.tick()
    assert schedule.get(once.id).status == "completed"


async def test_lost_notification_response_retains_exact_observation_across_restart(tmp_path):
    clock, notifier, effect, schedule = services(tmp_path)
    notifier.lose_response = True
    value = {"revision": 1}
    schedule.register_observer("work", lambda *_: Observation(dict(value), f"revision {value['revision']}"))
    watch = schedule.add_watch("alice", "55", observer="work", target="case-a", criteria={},
                               expires_at=1500, interval_seconds=10, cooldown_seconds=0)
    await schedule.tick()
    assert schedule.get(watch.id).pending is not None
    value["revision"] = 2
    restarted = ScheduleService(effect.path, effect, clock=clock)
    restarted.register_authorizer("work", lambda *_: True)
    restarted.register_observer("work", lambda *_: Observation(dict(value), "revision 2"))
    clock.now += 10
    await restarted.tick()
    assert notifier.calls == 1
    assert restarted.get(watch.id).pending is None
    clock.now += 10
    await restarted.tick()
    assert notifier.calls == 2


async def test_cancel_during_observation_prevents_new_effect(tmp_path):
    _, notifier, _, schedule = services(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def observe(*_):
        entered.set()
        await release.wait()
        return Observation({"ok": True}, "send")

    schedule.register_observer("work", observe)
    watch = schedule.add_watch("alice", "55", observer="work", target="case-a", criteria={}, expires_at=1500)
    ticking = asyncio.create_task(schedule.tick())
    await entered.wait()
    schedule.cancel(watch.id, "alice")
    release.set()
    await ticking
    assert notifier.calls == 0


async def test_periodic_reports_can_repeat_only_when_explicitly_requested(tmp_path):
    clock, notifier, _, schedule = services(tmp_path)
    schedule.register_observer("work", lambda *_: Observation({"same": True}, "status"))
    schedule.add_watch("alice", "55", observer="work", target="case-a", criteria={}, expires_at=1500,
                       interval_seconds=10, cooldown_seconds=10, diff_only=False)
    await schedule.tick()
    clock.now += 10
    await schedule.tick()
    assert notifier.calls == 2


async def test_audience_change_after_observation_blocks_notification(tmp_path):
    _, notifier, _, schedule = services(tmp_path)
    permitted = {"value": True}
    schedule.register_authorizer("work", lambda *_: permitted["value"])

    async def observe(*_):
        # The private result was read while allowed, then the user narrowed
        # sharing before the notification crossed its effect start boundary.
        permitted["value"] = False
        return Observation({"secret": "private"}, "private result")

    schedule.register_observer("work", observe)
    watch = schedule.add_watch("alice", "55", observer="work", target="case-a", criteria={}, expires_at=1500)
    await schedule.tick()
    assert notifier.calls == 0
    assert "authorization" in schedule.get(watch.id).error


def test_registration_does_not_accept_invalid_expiry_or_other_owner_cancel(tmp_path):
    _, _, _, schedule = services(tmp_path)
    with pytest.raises(ValueError):
        schedule.add_reminder("alice", "55", "text", run_at=1100, expires_at=1099)
    saved = schedule.add_reminder("alice", "55", "text", run_at=1100)
    with pytest.raises(ScopeError):
        schedule.cancel(saved.id, "mallory")
    assert schedule.list("mallory") == []


def test_time_requires_date_and_time_and_preserves_explicit_timezone():
    assert parse_time("2026-09-10 08:00") == datetime(2026, 9, 9, 23, tzinfo=timezone.utc).timestamp()
    assert parse_time("2026-09-10T08:00+00:00") == datetime(2026, 9, 10, 8, tzinfo=timezone.utc).timestamp()
    with pytest.raises(ValueError):
        parse_time("2026-09-10")


def test_criteria_are_data_only_and_missing_values_do_not_match():
    assert evaluate_criteria({"price": 90, "status": "open"}, {"all": [
        {"field": "price", "op": "lt", "value": 100}, {"status": "open"},
    ]})
    assert not evaluate_criteria({}, {"field": "price", "op": "lt", "value": 100})
    with pytest.raises(ValueError):
        evaluate_criteria({"price": 5}, {"field": "price", "op": "exec", "value": "rm"})


async def test_source_request_replay_survives_restart_completion_and_expiry(tmp_path):
    clock, notifier, effect, schedules = services(tmp_path)
    saved = schedules.add_reminder("alice", "55", "deadline", run_at=1010,
                                   expires_at=1020, request_id="discord:message:1")
    restarted = ScheduleService(effect.path, effect, clock=clock)
    duplicate = restarted.add_reminder("alice", "55", "deadline", run_at=1010,
                                       expires_at=1020, request_id="discord:message:1")
    assert duplicate.id == saved.id
    clock.now = 1010
    await restarted.tick()
    clock.now = 1100
    replay = restarted.add_reminder("alice", "55", "deadline", run_at=1010,
                                    expires_at=1020, request_id="discord:message:1")
    assert replay.id == saved.id and replay.status == "completed"
    await restarted.tick()
    assert notifier.calls == 1
    assert len(restarted.list("alice", include_inactive=True)) == 1


@pytest.mark.parametrize("change", [
    {"owner_id": "mallory"}, {"recipient": "66"}, {"content": "different"},
    {"run_at": 1011}, {"expires_at": 1050}, {"work_id": "different", "revision": 1},
])
def test_source_request_cannot_be_reused_for_changed_owner_payload_or_dates(tmp_path, change):
    _, _, _, schedules = services(tmp_path)
    original = dict(owner_id="alice", recipient="55", content="deadline", run_at=1010,
                    expires_at=1020, request_id="discord:message:1")
    schedules.add_reminder(**original)
    with pytest.raises(ScopeError, match="reused"):
        schedules.add_reminder(**{**original, **change})


async def test_watch_request_replay_ignores_generated_poll_time_but_rejects_changed_criteria(tmp_path):
    clock, _, effect, schedules = services(tmp_path)
    schedules.register_observer("listing", lambda *_: Observation({"price": 100}, "price"))
    request = dict(owner_id="alice", recipient="55", observer="listing", target="property-a",
                   criteria={"price": 100}, expires_at=1100, request_id="discord:interaction:2")
    saved = schedules.add_watch(**request)
    clock.now = 1020
    assert schedules.add_watch(**request).id == saved.id
    with pytest.raises(ScopeError, match="reused"):
        schedules.add_watch(**{**request, "criteria": {"price": 200}})
    with pytest.raises(ScopeError, match="reused"):
        schedules.add_watch(**{**request, "interval_seconds": 600})
    # A disconnected observer must not prevent looking up an already saved
    # request after process restart or lead to a second registration.
    restarted = ScheduleService(effect.path, effect, clock=clock)
    clock.now = 1200
    assert restarted.add_watch(**request).id == saved.id


async def test_orphan_grant_after_registration_crash_cannot_duplicate_notification(tmp_path, monkeypatch):
    clock, notifier, effect, schedules = services(tmp_path)
    authorize = effect.authorize

    def crash_after_grant(*args, **kwargs):
        authorize(*args, **kwargs)
        raise RuntimeError("crashed after inert grant commit")

    request = dict(owner_id="alice", recipient="55", content="deadline", run_at=1010,
                   request_id="discord:message:1")
    monkeypatch.setattr(effect, "authorize", crash_after_grant)
    with pytest.raises(RuntimeError, match="crashed"):
        schedules.add_reminder(**request)
    assert schedules.list("alice") == []
    monkeypatch.setattr(effect, "authorize", authorize)
    saved = schedules.add_reminder(**request)
    assert schedules.add_reminder(**request).id == saved.id
    clock.now = 1010
    await schedules.tick()
    assert notifier.calls == 1
    with effect.transaction() as db:
        assert db.execute("SELECT count(*) FROM yui_effect_grants").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM yui_schedule_requests").fetchone()[0] == 1


async def test_concurrent_source_replays_commit_one_schedule(tmp_path, monkeypatch):
    clock, notifier, effect, schedules = services(tmp_path)
    barrier, authorize = threading.Barrier(2), effect.authorize

    def synchronized_grant(*args, **kwargs):
        grant = authorize(*args, **kwargs)
        barrier.wait(timeout=5)
        return grant

    monkeypatch.setattr(effect, "authorize", synchronized_grant)
    request = dict(owner_id="alice", recipient="55", content="deadline", run_at=1010,
                   request_id="discord:message:1")
    first, second = await asyncio.gather(
        asyncio.to_thread(schedules.add_reminder, **request),
        asyncio.to_thread(schedules.add_reminder, **request),
    )
    assert first.id == second.id
    assert len(schedules.list("alice")) == 1
    clock.now = 1010
    await schedules.tick()
    assert notifier.calls == 1


async def test_direct_reminder_checks_current_publication_policy_before_submission(tmp_path):
    clock, notifier, effect, schedules = services(tmp_path)
    saved = schedules.add_reminder("alice", "55", "formerly shared memory", run_at=1010)
    inspected = []

    def current_policy(schedule):
        inspected.append(schedule)
        return schedule.pending["payload"]["content"] != "formerly shared memory"

    schedules.set_publication_policy(current_policy)
    clock.now = 1010
    await schedules.tick()
    assert len(inspected) == 1
    assert inspected[0].id == saved.id
    assert inspected[0].owner_id == "alice" and inspected[0].recipient == "55"
    assert notifier.calls == 0
    assert effect.list_effects("alice")[0].state == "rejected"


async def test_publication_policy_failure_fails_closed_without_sending(tmp_path):
    clock, notifier, _, schedules = services(tmp_path)
    schedules.add_reminder("alice", "55", "text", run_at=1010)

    def unavailable(_):
        raise RuntimeError("ledger unavailable")

    schedules.set_publication_policy(unavailable)
    clock.now = 1010
    await schedules.tick()
    assert notifier.calls == 0
