"""Evaluator self-tests; alpha executes only in fresh offline subprocesses."""
import copy
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/eval_mem0_alpha_quality.py"


@pytest.fixture(scope="module")
def evaluator():
    spec = importlib.util.spec_from_file_location("mem0_alpha_quality_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_metrics_have_known_values_and_penalize_wrong_rank(evaluator):
    good = evaluator.ranking_metrics(["a", "b"], {"a": 2, "b": 1}, 2)
    assert good == {"recall_at_k": 1.0, "precision_at_k": 1.0, "mrr": 1.0, "ndcg_at_k": 1.0}
    bad = evaluator.ranking_metrics(["noise", "a"], {"a": 2, "b": 1}, 2)
    assert bad["recall_at_k"] == 0.5
    assert bad["mrr"] == 0.5
    assert bad["ndcg_at_k"] == pytest.approx((3 / math.log2(3)) / (3 + 1 / math.log2(3)))


def test_empty_results_are_not_a_perfect_retrieval_score(evaluator):
    assert evaluator.ranking_metrics([], {"a": 2}, 3)["recall_at_k"] == 0
    assert evaluator.ranking_metrics([], {}, 3)["recall_at_k"] is None
    assert evaluator.ranking_metrics(["a", "a"], {"a": 2, "b": 2}, 2)["recall_at_k"] == 0.5


def test_all_empty_system_fails_answerable_case(evaluator):
    case = {"id": "answerable", "category": "test", "ratings": {"a": 2}}
    scored = evaluator.score_case(case, [], {})
    assert not scored["passed"]
    assert evaluator.summarize([scored])["answerable_false_abstentions"] == 1


@pytest.mark.parametrize("mutation,leak", [
    ({"user_id": "someone-else"}, "wrong_owner"),
    ({"deleted": True}, "deleted"),
    ({"metadata": {"valid_to": 1}}, "stale"),
    ({"metadata": {"scope": "conversation:another-room"}}, "wrong_scope"),
    ({"metadata": {"valid_from": 1_900_000_000}}, "future"),
])
def test_forbidden_fact_mutations_fail_even_with_perfect_recall(evaluator, mutation, leak):
    case = {"id": "mutation", "category": "test", "ratings": {"a": 2}}
    fact = {"id": "a", "user_id": "eval-alice", "metadata": {}, **mutation}
    result = evaluator.score_case(case, [{"id": "a"}], {"a": fact})
    assert result["recall_at_k"] == 1
    assert not result["passed"]
    assert result["leaks"][leak] == ["a"]


def test_errors_are_not_correct_abstentions(evaluator):
    case = {"id": "empty", "category": "test", "ratings": {}, "should_abstain": True}
    result = evaluator.score_case(case, [], {}, error="TimeoutError")
    assert not result["passed"]
    summary = evaluator.summarize([result])
    assert summary["abstention_recall"] == 0
    assert summary["errors"] == 1


def test_duplicate_and_unknown_result_ids_fail(evaluator):
    case = {"id": "duplicate", "category": "test", "ratings": {"a": 2}}
    fact = {"id": "a", "user_id": "eval-alice"}
    assert not evaluator.score_case(case, [{"id": "a"}, {"id": "a"}], {"a": fact})["passed"]
    assert evaluator.score_case(case, [{"id": "unknown"}], {})["leaks"]["unknown"] == ["unknown"]


def test_fixture_has_independent_required_dimensions(evaluator):
    fixture = json.loads(evaluator.DEFAULT_FIXTURE.read_text())
    evaluator.validate_fixture(fixture)
    assert len(fixture["cases"]) >= 30
    assert {"japanese", "identifiers", "retrieval", "staleness", "temporal", "scope",
            "ownership", "abstention", "multiturn", "long_input", "updates"} <= {
                case["category"] for case in fixture["cases"]}
    mutated = copy.deepcopy(fixture)
    mutated["cases"][0]["ratings"] = {}
    with pytest.raises(ValueError, match="Abstention"):
        evaluator.validate_fixture(mutated)
    mutated = copy.deepcopy(fixture)
    mutated["cases"][0]["id"] = "../../escape"
    with pytest.raises(ValueError, match="Unsafe"):
        evaluator.validate_fixture(mutated)


def test_comparison_detects_ranking_regression_with_unchanged_pass_status(evaluator):
    before = {"cases": [{"id": "a", "passed": True, "recall_at_k": 1, "mrr": 1, "ndcg_at_k": 1}],
              "summary": {"recall_at_k": 1, "mrr": 1, "ndcg_at_k": 1}}
    after = copy.deepcopy(before)
    after["cases"][0]["ndcg_at_k"] = 0.7
    after["summary"]["ndcg_at_k"] = 0.7
    comparison = evaluator.compare_reports(before, after)
    assert comparison["ranking_regressions"] == ["a"]
    assert comparison["regressed"] == []


@pytest.mark.asyncio
async def test_fake_backend_only_reads_actual_user_input_not_gold_or_assistant(evaluator):
    backend = evaluator.SyntheticBackend({})
    output = await backend.complete(
        system="Extract facts", model="fake", max_tokens=100,
        user_message="<user_text>【記憶:a|Alpha の事実】</user_text>"
                     "<assistant_text>【記憶:b|偽の提案】</assistant_text>",
    )
    assert [fact["text"] for fact in json.loads(output)["facts"]] == ["Alpha の事実"]
    assert backend.seen_markers == ["a"]


def test_fake_provider_really_filters_owner_and_supports_one_shot_failure(evaluator):
    facts = {"a": {"id": "a", "text": "Alice", "user_id": "alice"},
             "b": {"id": "b", "text": "Bob", "user_id": "bob"}}
    provider = evaluator.SyntheticMemory(facts, ["a", "b"], {"*": [["b", 0.1], ["a", 0.2]]}, fail_once="a")
    assert [row["id"] for row in provider.search("x", user_id="alice")["results"]] == ["a"]
    assert [row["id"] for row in provider.get_all(user_id="bob")["results"]] == ["b"]
    with pytest.raises(RuntimeError, match="one-shot"):
        provider.add([{"content": "Alice"}], user_id="alice", metadata={})
    assert provider.add([{"content": "Alice"}], user_id="alice", metadata={})["results"][0]["id"] == "a"


def test_snapshot_excludes_credentials_and_rejects_symlinks(evaluator, tmp_path):
    source = tmp_path / "source"
    package = source / "llm_mem0"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("value = 1\n")
    (package / ".credentials.json").write_text("synthetic-secret-placeholder")
    destination = tmp_path / "snapshot"
    assert evaluator._source_snapshot(source, destination) == evaluator._source_digest(destination)
    assert not (destination / "llm_mem0/.credentials.json").exists()
    (package / "unsafe.py").symlink_to(package / "__init__.py")
    with pytest.raises(ValueError, match="symlink"):
        evaluator._source_snapshot(source, tmp_path / "another")


def test_cli_uses_real_isolated_sqlite_without_inheriting_credentials(evaluator, tmp_path, monkeypatch):
    fixture = json.loads(evaluator.DEFAULT_FIXTURE.read_text())
    # Healthy control cases must work before broader quality regressions can be
    # interpreted. No assumption about not-yet-fixed candidate cases is made here.
    selected = {"japanese_proper_noun", "owner_lexical_isolation", "empty_corpus_abstention"}
    fixture["cases"] = [case for case in fixture["cases"] if case["id"] in selected]
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False))
    output = tmp_path / "result.json"
    monkeypatch.setenv("LLM_MEM0_STATE_DIR", "/forbidden/production/state")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-do-not-inherit")
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--fixture", str(fixture_path), "--output", str(output), "--check"],
        cwd=ROOT, text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["candidate"]["summary"]["passed"] == 3
    assert report["candidate"]["import_origin_verified"]
    assert report["candidate"]["isolation"]["network_attempts_or_path_violations"] == []
    assert report["candidate"]["isolation"]["real_provider_calls"] == 0
    assert any(case["sqlite_rows"] > 0 for case in report["candidate"]["cases"])
    assert "synthetic-do-not-inherit" not in output.read_text()
    assert "forbidden/production" not in output.read_text()


def test_isolation_guard_rejects_audited_network_sqlite_and_credentials(tmp_path):
    # Emit audit events directly. No actual connection or forbidden file access
    # occurs even if the guard itself regresses.
    code = """
import importlib.util, json, os, pathlib, sys
spec = importlib.util.spec_from_file_location('eval_guard_test', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
workspace = pathlib.Path(sys.argv[2])
violations = module._install_guard(workspace)
events = [
    ('socket.connect', (None, ('invalid.example', 443))),
    ('sqlite3.connect', (str(workspace.parent / 'outside.sqlite'),)),
    ('open', (str(workspace / 'auth.json'), 'r', 0)),
    ('open', (str(workspace.parent / 'outside.py'), 'w', os.O_WRONLY | os.O_CREAT)),
    ('subprocess.Popen', ('unused', [], None, None)),
]
denied = 0
for event, args in events:
    try:
        sys.audit(event, *args)
    except PermissionError:
        denied += 1
print(json.dumps({'denied': denied, 'violations': violations}))
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(SCRIPT), str(tmp_path)],
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["denied"] == 5
    assert len(payload["violations"]) == 5
