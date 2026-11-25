"""External effect scope, duplicate delivery, and interrupted-outcome recovery."""

import asyncio
from types import SimpleNamespace

import pytest

from services.work.effects import (
    AdapterCapabilities, AdapterResult, DiscordNotificationAdapter, EffectExecutor, ScopeError,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Adapter:
    capabilities = AdapterCapabilities(("send",), False, True, False, "remote record exists")

    def __init__(self):
        self.submits = []
        self.remote = {}
        self.fail_after_send = False
        self.reconciles = 0

    async def submit(self, payload, *, idempotency_key):
        self.submits.append(idempotency_key)
        result = AdapterResult(remote_id="remote-1", result={"sent": payload})
        self.remote[idempotency_key] = result
        if self.fail_after_send:
            raise TimeoutError("response lost")
        return result

    async def reconcile(self, receipt):
        self.reconciles += 1
        return self.remote.get(receipt.id)


def executor(tmp_path, *, gate=None, clock=None):
    effect = EffectExecutor(tmp_path / "work.sqlite3", work_gate=gate, clock=clock or Clock())
    adapter = Adapter()
    effect.register_adapter("mail", adapter)
    return effect, adapter


def accept(effect, *, owner="alice", work_id=None, revision=None):
    grant = effect.authorize(owner, "mail", "send", "recipient-a",
                             allowed_payload_keys=["target", "content"],
                             payload_constraints={"content": "authorized text"},
                             work_id=work_id, revision=revision)
    return effect.accept(grant, owner, {"target": "recipient-a", "content": "authorized text"},
                         idempotency_key="request-1", work_id=work_id, revision=revision)


async def test_lost_response_reconciles_after_restart_without_duplicate_send(tmp_path):
    effect, adapter = executor(tmp_path)
    adapter.fail_after_send = True
    receipt = accept(effect)
    first = await effect.execute(receipt.id)
    assert first.state == "needs_reconciliation"
    restarted = EffectExecutor(effect.path)
    restarted.register_adapter("mail", adapter)
    final = await restarted.execute(receipt.id)
    assert final.state == "confirmed"
    assert final.remote_id == "remote-1"
    assert len(adapter.submits) == 1
    assert adapter.reconciles == 1


async def test_crashed_submitted_effect_never_replays_even_when_remote_missing(tmp_path):
    clock = Clock()
    effect, adapter = executor(tmp_path, clock=clock)
    receipt = accept(effect)
    with effect.transaction() as db:
        db.execute("UPDATE yui_effects SET state='submitted',lease_until=? WHERE id=?", (clock.now + 60, receipt.id))
    clock.now += 61
    restarted = EffectExecutor(effect.path, clock=clock)
    restarted.register_adapter("mail", adapter)
    for _ in range(2):
        assert (await restarted.execute(receipt.id)).state == "needs_reconciliation"
    assert adapter.submits == []


@pytest.mark.parametrize("mutation", ["target", "owner", "extra", "content"])
def test_scope_cannot_change_owner_destination_text_or_fields(tmp_path, mutation):
    effect, _ = executor(tmp_path)
    receipt = accept(effect)
    payload = dict(receipt.payload)
    owner = receipt.owner_id
    if mutation == "owner":
        owner = "mallory"
    elif mutation == "extra":
        payload["bcc"] = "mallory"
    else:
        payload[mutation] = "different"
    with pytest.raises(ScopeError):
        effect.accept(receipt.grant_id, owner, payload, idempotency_key="request-2")


async def test_revision_and_cancel_are_rechecked_at_submission(tmp_path):
    current = {"revision": 2, "active": True}
    effect, adapter = executor(tmp_path, gate=lambda wid, rev, owner: current["active"] and rev == current["revision"])
    stale = accept(effect, work_id="case-a", revision=1)
    assert (await effect.execute(stale.id)).state == "rejected"
    cancelled = accept(effect, work_id="case-a", revision=2)
    current["active"] = False
    assert (await effect.execute(cancelled.id)).state == "rejected"
    assert adapter.submits == []


async def test_unconfigured_work_gate_fails_closed(tmp_path):
    effect, adapter = executor(tmp_path)
    receipt = accept(effect, work_id="case-a", revision=1)
    assert (await effect.execute(receipt.id)).state == "rejected"
    assert adapter.submits == []


async def test_parallel_executors_submit_only_once(tmp_path):
    effect, adapter = executor(tmp_path)
    receipt = accept(effect)
    started, release = asyncio.Event(), asyncio.Event()
    original = adapter.submit

    async def delayed(payload, *, idempotency_key):
        started.set()
        await release.wait()
        return await original(payload, idempotency_key=idempotency_key)

    adapter.submit = delayed
    first = asyncio.create_task(effect.execute(receipt.id))
    await started.wait()
    second = EffectExecutor(effect.path, clock=effect.clock)
    second.register_adapter("mail", adapter)
    assert (await second.execute(receipt.id)).state == "submitted"
    release.set()
    assert (await first).state == "confirmed"
    assert len(adapter.submits) == 1


async def test_revocation_preserves_confirmed_result_and_cancels_unstarted(tmp_path):
    effect, adapter = executor(tmp_path)
    confirmed = accept(effect)
    await effect.execute(confirmed.id)
    effect.revoke(confirmed.grant_id, "alice")
    assert effect.get(confirmed.id).state == "confirmed"
    pending = accept(effect)
    effect.revoke(pending.grant_id, "alice")
    assert (await effect.execute(pending.id)).state == "cancelled"
    assert len(adapter.submits) == 1


def test_idempotency_key_reuse_does_not_spend_another_authorization(tmp_path):
    effect, _ = executor(tmp_path)
    receipt = accept(effect)
    duplicate = effect.accept(receipt.grant_id, "alice", receipt.payload, idempotency_key="request-1")
    assert receipt.id == duplicate.id


def test_adapter_capabilities_are_enforced_at_execution_boundary(tmp_path):
    effect, _ = executor(tmp_path)
    with pytest.raises(ScopeError):
        effect.authorize("alice", "mail", "draft", "recipient-a")
    grant = effect.authorize("alice", "mail", "send", "recipient-a")
    with pytest.raises(ScopeError):
        effect.accept(grant, "alice", {"target": "recipient-b"}, idempotency_key="new")
    incomplete = Adapter()
    incomplete.capabilities = AdapterCapabilities(("send",), False, True, True, "remote record exists")
    with pytest.raises(ValueError, match="cancellation"):
        effect.register_adapter("incomplete", incomplete)


def test_recovery_reports_all_unconfirmed_work_effects_without_sending(tmp_path):
    clock = Clock()
    effect, adapter = executor(tmp_path, clock=clock)
    receipt = accept(effect, work_id="case-a", revision=1)
    with effect.transaction() as db:
        db.execute("UPDATE yui_effects SET state='submitted',lease_until=? WHERE id=?", (999, receipt.id))
    assert effect.pending_work_ids() == {"case-a"}
    assert effect.get(receipt.id).state == "needs_reconciliation"
    assert adapter.submits == []


async def test_discord_lost_response_reconciles_only_own_message_marker(tmp_path):
    messages = []

    class Channel:
        id = 99

        async def send(self, *, embed, allowed_mentions):
            messages.append(SimpleNamespace(id=123, author=SimpleNamespace(id=42), embeds=[embed]))
            raise TimeoutError("Discord response lost")

        async def history(self, **kwargs):
            for message in messages:
                yield message

    client = SimpleNamespace(user=SimpleNamespace(id=42), get_channel=lambda _: Channel())
    effect = EffectExecutor(tmp_path / "work.sqlite3")
    effect.register_adapter("discord", DiscordNotificationAdapter(client))
    grant = effect.authorize("alice", "discord", "notify", "99")
    receipt = effect.accept(grant, "alice", {"channel_id": "99", "content": "Reminder"}, idempotency_key="one")
    assert (await effect.execute(receipt.id)).state == "needs_reconciliation"
    final = await effect.execute(receipt.id)
    assert final.state == "confirmed"
    assert final.result["read_confirmed"] is False
    assert len(messages) == 1
