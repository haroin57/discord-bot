"""Durable research resumes across process/provider boundaries without live APIs."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.deep_research import orchestrator as orch
from services.deep_research.run_state import ResearchRun
from services.deep_research.types import ResearchState, SubAgentResult
from services.memory_policy import MemoryPolicy
from services.work.research import ResearchInterrupted, ResearchJournal, is_research_work, record_evaluation
from services.work.store import WorkAccessDenied, WorkStore


@pytest.fixture
def journal_state(tmp_path):
    store = WorkStore(tmp_path / "work.sqlite3")
    policy = MemoryPolicy(tmp_path / "policy.sqlite3")
    work = store.create("7", "42", "Compare housing", "request", requirements={"budget": 100, "station": "A"})
    journal = ResearchJournal(store, work.id, "7", channel_id="42", memory_policy=policy)
    state = ResearchState(query=work.goal, clarified_query=work.goal, work_id=work.id,
                          work_revision=work.revision, requirements=work.requirements)
    journal.attach_context(state)
    journal.transition(state, "running")
    return store, policy, journal, state


def test_required_answer_wait_survives_restart_and_old_checkpoint_invalidation(journal_state):
    store, policy, journal, state = journal_state
    question = journal.question(state, "What is the budget?")
    journal.pause(state, "Answer tomorrow")
    assert store.get(state.work_id).state == "waiting"
    assert store.list_questions(state.work_id, "7")[0]["status"] == "open"
    reopened = WorkStore(store.path)
    reopened.answer_question(state.work_id, question["id"], "7", "120", "answer-next-day")
    assert reopened.load_checkpoint(state.work_id, "research") is None
    assert is_research_work(reopened, state.work_id, "7")
    restored = ResearchJournal(reopened, state.work_id, "7", channel_id="42", memory_policy=policy).restore()
    assert restored.questions[question["id"]]["answer"] == "120"
    assert "120" in restored.clarified_query


def test_audience_change_and_memory_epoch_prevent_reusing_derived_research(journal_state):
    store, policy, journal, state = journal_state
    state.results = [SubAgentResult("q", "private-derived fact")]
    journal.save(state)
    assert journal.restore() is not None
    policy.remember("Use a different address", "7", channel_id="42")
    assert journal.restore() is None
    journal.attach_context(state)
    journal.save(state)
    store.amend(state.work_id, "7", "", "private-now", audience="owner")
    assert journal.restore() is None
    with pytest.raises(WorkAccessDenied):
        ResearchJournal(store, state.work_id, "8", channel_id="42", memory_policy=policy).restore()


def test_invalidated_required_question_is_superseded_without_being_answered(journal_state):
    store, policy, journal, state = journal_state
    question = journal.question(state, "Old inferred premise?")
    policy.remember("Use current conditions", "7", channel_id="42")
    fresh = ResearchJournal(store, state.work_id, "7", channel_id="42", memory_policy=policy)
    assert fresh.restore() is None
    restarted = ResearchState(query=state.query, work_id=state.work_id, work_revision=state.work_revision)
    fresh.reset_invalid_context(restarted)
    old = store.list_questions(state.work_id, "7")[0]
    assert old["status"] == "superseded"
    assert old["answer"] is None
    assert store.list_questions(state.work_id, "7", open_only=True) == []


def test_condition_amendment_replaces_old_pending_question_instead_of_answering_it(journal_state):
    store, policy, journal, state = journal_state
    question = journal.question(state, "What is the budget?")
    journal.pause(state, "Waiting for the budget")
    store.amend(state.work_id, "7", "予算は120です。", "budget-amendment")
    resumed = ResearchJournal(store, state.work_id, "7", channel_id="42", memory_policy=policy)
    restored = resumed.restore()
    resumed.supersede_stale_questions(restored)
    old = next(q for q in store.list_questions(state.work_id, "7") if q["id"] == question["id"])
    assert old["status"] == "superseded"
    assert old["answer"] is None
    assert restored.requirements["amendments"] == ["予算は120です。"]


@pytest.mark.asyncio
async def test_uncertain_publication_requires_reconciliation_and_is_not_resent(journal_state):
    from services.work.research import unresolved_publications
    from services.work.runtime import WorkRuntime
    store, policy, journal, state = journal_state
    state.completion_status = "partial"
    artifact = journal.artifact(state, "A complete file containing provisional results.")
    channel = SimpleNamespace(id=43, send=AsyncMock(side_effect=TimeoutError))
    with pytest.raises(TimeoutError):
        await journal.publish(state, channel, artifact, "A complete file containing provisional results.")
    assert store.get(state.work_id).state == "needs_reconciliation"
    pending = unresolved_publications(store, state.work_id)
    assert len(pending) == 1
    assert pending[0]["payload"]["filename"].endswith(".md")
    runtime = WorkRuntime(store, generate=AsyncMock(), policy=policy)
    with pytest.raises(ValueError, match="照合"):
        await runtime.start(state.work_id, "7", channel)
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_timeout_is_persisted_without_answering_or_starting_inference(journal_state, monkeypatch):
    store, policy, journal, state = journal_state
    state.central_question = "Which property meets the budget?"
    ctx = SimpleNamespace(state=state, journal=journal,
                          deps=SimpleNamespace(bot=MagicMock()), user=SimpleNamespace(id=7),
                          thread=SimpleNamespace(id=42), run=ResearchRun(state))
    monkeypatch.setattr(orch, "_present_cq", AsyncMock(return_value=SimpleNamespace(id=99)))
    monkeypatch.setattr(orch, "_await_gate_response", AsyncMock(side_effect=TimeoutError))
    with pytest.raises(ResearchInterrupted, match="回答待ち"):
        await orch._central_question_gate(ctx)
    assert store.get(state.work_id).state == "waiting"
    assert store.list_questions(state.work_id, "7")[0]["status"] == "open"
    assert journal.restore().phase == "waiting_input"


@pytest.mark.asyncio
async def test_unrelated_reaction_and_side_question_are_not_answers(monkeypatch):
    checks = {}
    async def wait_for(event, *, check):
        checks[event] = check
        await asyncio.Event().wait()
    bot = SimpleNamespace(wait_for=wait_for)
    with pytest.raises(TimeoutError):
        await orch._await_reaction_or_reply(bot, 7, 42, SimpleNamespace(id=99), timeout_s=.01)
    assert not checks["raw_reaction_add"](SimpleNamespace(message_id=99, user_id=7, emoji="👀"))
    assert checks["raw_reaction_add"](SimpleNamespace(message_id=99, user_id=7, emoji="✅"))
    side = SimpleNamespace(channel=SimpleNamespace(id=42), author=SimpleNamespace(id=7, bot=False),
                           content="ところで明日の天気は", reference=None)
    assert not checks["message"](side)
    side.reference = SimpleNamespace(message_id=99)
    side.content = "Budget 120"
    assert checks["message"](side)


def _channel():
    anchor = MagicMock(id=99)
    anchor.edit = AsyncMock()
    thread = SimpleNamespace(id=43, send=AsyncMock(return_value=anchor))
    origin = SimpleNamespace(id=98, create_thread=AsyncMock(return_value=thread))
    channel = SimpleNamespace(id=42, guild=SimpleNamespace(id=1), send=AsyncMock(return_value=origin))
    return channel, thread


@pytest.mark.asyncio
async def test_actual_orchestrator_resumes_and_researches_only_changed_condition(tmp_path, monkeypatch):
    store = WorkStore(tmp_path / "work.sqlite3")
    policy = MemoryPolicy(tmp_path / "policy.sqlite3")
    work = store.create("7", "42", "Compare housing", "housing", requirements={"budget": 100, "station": "A"})
    channel, thread = _channel()
    deps = orch.DeepResearchDeps(bot=MagicMock(), send_segmented=AsyncMock(),
        get_http_session=lambda: None, track_task=lambda task: None,
        work_store=store, work_id=work.id, memory_policy=policy)
    pool = MagicMock(handles={})
    pool.spawn_eager = AsyncMock()
    monkeypatch.setattr(orch, "McpPool", lambda **kwargs: pool)
    monkeypatch.setattr(orch, "make_api_client", AsyncMock(return_value=object()))
    monkeypatch.setattr(orch, "has_required_web_backend", lambda: True)
    monkeypatch.setattr(orch, "available_research_keys", lambda: [])
    monkeypatch.setattr(orch, "static_mcp_tools_to_anthropic", lambda keys: [{"name": "search"}])
    monkeypatch.setattr(orch, "_recall_user_memories", AsyncMock())
    monkeypatch.setattr(orch, "_plan", AsyncMock(return_value=["price", "access"]))
    monkeypatch.setattr(orch, "DEEP_RESEARCH_MAGI_ENABLED", False)
    monkeypatch.setattr(orch, "DEEP_RESEARCH_MULTI_AGENT", True)
    searched = []
    async def agent(ctx, **kwargs):
        kind = kwargs["defn"].agent_type
        if kind == "searcher":
            subq = "price" if "price" in kwargs["briefing"] else "access"
            searched.append(subq)
            claim = "The house costs 100." if subq == "price" else "The house has 3 bedrooms."
            ref = ctx.state.register_source(url=f"https://example.test/{subq}", snippet=claim)
            ref.evidence = [claim]
            key = "budget" if subq == "price" else "station"
            return SubAgentResult(subq, f'{claim} [{ref.sid}]\n<research_dependencies>{{"requirement_keys":["{key}"]}}</research_dependencies>')
        if kind == "research_record":
            return SubAgentResult("assessment", json.dumps({"answered": True, "criteria": [
                {"criterion": "調査の問いに根拠付きで回答する", "met": True, "evidence_sids": ["S1", "S2"]}], "unknowns": [],
                "decision": {"selected": "house", "reasons": ["supported price and access"], "change_conditions": ["price changes"]}}))
        return SubAgentResult("synthesis", "The house costs 100. [S1]\nThe house has 3 bedrooms. [S2]")
    monkeypatch.setattr(orch, "_run_agent", agent)
    await orch.run_deep_research(channel, SimpleNamespace(id=7), work.goal, deps, skip_clarify=True)
    assert sorted(searched) == ["access", "price"]
    assert store.list_artifacts(work.id, "7")
    old_record = store.load_checkpoint(work.id, "research")["payload"]
    assert old_record["evaluations"]["price"]["requirement_keys"] == ["budget"]
    store.amend(work.id, "7", "", "budget-changed", requirements={"budget": 120})
    searched.clear()
    await orch.resume_deep_research(channel, SimpleNamespace(id=7), work.id, deps)
    assert searched == ["price"]
    artifact = store.list_artifacts(work.id, "7")[0]
    assert artifact["version"] == 2
    assert artifact["metadata"]["decision"]["change_conditions"] == ["price changes"]
    publications = [call for call in thread.send.await_args_list if call.kwargs.get("file")]
    assert len(publications) == 2
    assert all(call.kwargs["file"].filename.endswith(".md") for call in publications)
    assert deps.send_segmented.await_count == 0
