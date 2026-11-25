"""Research facts retain provenance and refresh only dependent conclusions."""

from datetime import datetime, timedelta, timezone
import json

from services.deep_research.sources import parse_sources
from services.deep_research.types import ResearchState, SubAgentResult
from services.work.research import apply_assessment, record_evaluation, refresh_dependencies


def test_sources_keep_retrieval_and_publication_dates_separate():
    state = ResearchState(query="news")
    source = parse_sources("news_search", json.dumps({
        "url": "https://example.test/news", "title": "Release", "published_at": "2024-01-03",
    }), state)[0]
    assert source.as_of == "2024-01-03"
    assert datetime.fromisoformat(source.fetched_at).tzinfo is not None
    assert source.fresh_for_s == 86400
    concept = parse_sources("web_search", '{"url":"https://example.test/theorem"}', state)[0]
    assert concept.as_of is None
    assert concept.fresh_for_s is None


def test_one_condition_changes_only_dependent_evaluation():
    state = ResearchState(query="housing", requirements={"budget": 100, "station": "A"})
    state.register_source(url="https://example.test/prices")
    record_evaluation(state, "price", SubAgentResult("price", "Price [S1]"), {"requirement_keys": ["budget"]})
    record_evaluation(state, "access", SubAgentResult("access", "Station [S1]"), {"requirement_keys": ["station"]})
    stale = refresh_dependencies(state, {"budget": 120, "station": "A"})
    assert stale == ["price"]
    assert state.evaluations["access"]["stale"] is False
    assert "conditions_changed:budget" in state.evaluations["price"]["stale_reasons"]


def test_expiry_and_changed_source_revisit_only_dependents():
    state = ResearchState(query="market")
    price = state.register_source(url="https://example.test/price", fresh_for_s=60)
    state.register_source(url="https://example.test/theory")
    record_evaluation(state, "today", SubAgentResult("today", "Price [S1]"), {"requirement_keys": []})
    record_evaluation(state, "concept", SubAgentResult("concept", "Concept [S2]"), {"requirement_keys": []})
    future = datetime.fromisoformat(price.fetched_at) + timedelta(seconds=61)
    assert refresh_dependencies(state, {}, now=future) == ["today"]
    assert state.evaluations["concept"]["stale"] is False


def test_restart_snapshot_has_no_native_clock_or_recalled_private_memory():
    original = ResearchState(query="physics", mode="learning", user_memories="private fact")
    original.tokens_used = 300
    original.register_source(url="https://example.test/source", as_of="2020-01-01")
    payload = original.snapshot()
    assert payload["user_memories"] == ""
    assert "started_at" not in payload
    restored = ResearchState.restore(json.loads(json.dumps(payload)))
    assert restored.sources["https://example.test/source"].as_of == "2020-01-01"
    assert restored.mode == "learning"
    assert restored.tokens_before_resume == 300


def test_missing_evidence_or_stale_evaluation_cannot_be_reported_complete():
    state = ResearchState(query="housing", plan=["price"], completion_criteria=["price verified"])
    apply_assessment(state, "It is affordable.", {
        "answered": True, "criteria": [{"criterion": "price verified", "met": True,
        "evidence_sids": ["S99"]}], "unknowns": [], "decision": {"selected": "A"},
    })
    assert state.completion_status == "partial"
    assert any("price" in reason for reason in state.unknowns)
    assert any("引用" in reason for reason in state.unknowns)


def test_learning_record_keeps_check_questions_without_forcing_selection():
    state = ResearchState(query="gravity", mode="learning")
    learning = {"known": ["basic law"], "unknown": ["derivation"],
                "check_questions": ["Explain acceleration."], "next_explanation": "derivation"}
    apply_assessment(state, "A provisional explanation.", {"answered": True, "learning": learning,
                                                          "decision": {"selected": "irrelevant"}})
    assert state.learning_record == learning
    assert state.decision_record == {}


def test_learning_intent_is_independent_of_comparison_topic():
    from services.deep_research.orchestrator import _research_mode
    assert _research_mode("重力の仕組みを理解したい") == "learning"
    assert _research_mode("機械学習モデルを比較して採用案を選ぶ") == "decision"
    assert _research_mode("モデルを比較する", "learning") == "learning"


def test_unverified_primary_evidence_keeps_result_provisional():
    state = ResearchState(query="housing")
    ref = state.register_source(url="https://example.test/house", snippet="The house has 3 bedrooms.")
    ref.evidence = ["The house has 3 bedrooms."]
    apply_assessment(state, "The house has 3 bedrooms. [S1]", {
        "answered": True, "criteria": [], "unknowns": [],
        "evidence_quality": {"primary_sids": ["S1"], "unverified": ["Listing date needs confirmation."]},
    })
    assert state.completion_status == "partial"
    assert state.unknowns == ["Listing date needs confirmation."]
