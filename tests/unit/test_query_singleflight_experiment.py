from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import experiment_query_singleflight as experiment


SOURCE = Path(__file__).resolve().parents[2] / "docs/2026-10-10-memory-query-experiment.json"


async def test_saved_response_controls_reduce_only_concurrent_owner_scoped_duplicates(monkeypatch):
    monkeypatch.setenv("MEM0_QUERY_SINGLEFLIGHT_ENABLED", "previous-value")
    monkeypatch.setenv("MEM0_QUERY_EXPANSION_MODE", "bundle")
    report = await experiment.run(SOURCE)

    assert report["new_provider_requests"] == 0
    assert report["simulation"] == "saved_response_controlled_replay"
    assert report["independent_saved_provider_query_samples"] == 6
    expected_dispatches = {
        "single": (18, 18),
        "concurrent_same_query_two": (36, 18),
        "concurrent_same_query_four": (72, 18),
        "concurrent_distinct_queries": (72, 72),
        "concurrent_distinct_owners": (72, 72),
        "sequential_same_query": (72, 72),
    }
    for control, counts in expected_dispatches.items():
        arms = report["controls"][control]
        assert tuple(arms[arm]["simulated_provider_dispatches"] for arm in ("baseline", "singleflight")) == counts
        for arm in arms.values():
            assert arm["cases"] == list(range(6))
            assert arm["matched_saved_representations"] == arm["consumers_checked"]
            assert arm["reference_quality_pass_count"] == arm["consumers_checked"]
    import os
    assert os.environ["MEM0_QUERY_SINGLEFLIGHT_ENABLED"] == "previous-value"
    assert os.environ["MEM0_QUERY_EXPANSION_MODE"] == "bundle"
    saved_path = SOURCE.parent / "2026-10-10-query-singleflight-controls.json"
    assert report == json.loads(saved_path.read_text())


@pytest.mark.parametrize("problem", ["not_synthetic", "missing_usage", "missing_call", "duplicate_reference"])
async def test_invalid_or_incomplete_reference_data_is_rejected(tmp_path, problem):
    payload = json.loads(SOURCE.read_text())
    parallel = next(row for row in payload["results"] if row["arm"] == "parallel")
    if problem == "not_synthetic":
        payload["synthetic"] = False
    elif problem == "missing_usage":
        parallel["calls"][0]["usage"].pop("output_tokens")
    elif problem == "missing_call":
        parallel["calls"].pop()
    else:
        payload["results"].append(parallel)
    source = tmp_path / "reference.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        await experiment.run(source)


async def test_failed_control_restores_expansion_configuration(monkeypatch):
    import os

    monkeypatch.setenv("MEM0_QUERY_SINGLEFLIGHT_ENABLED", "old-enabled")
    monkeypatch.setenv("MEM0_QUERY_EXPANSION_MODE", "old-mode")
    original_load = experiment.load_search

    def failing_module(*arguments):
        module = original_load(*arguments)

        async def fail(*arguments):
            raise ValueError("synthetic control failure")

        module._shared_query_representations = fail
        return module

    monkeypatch.setattr(experiment, "load_search", failing_module)
    with pytest.raises(ValueError, match="synthetic control failure"):
        await experiment.run(SOURCE)
    assert os.environ["MEM0_QUERY_SINGLEFLIGHT_ENABLED"] == "old-enabled"
    assert os.environ["MEM0_QUERY_EXPANSION_MODE"] == "old-mode"
