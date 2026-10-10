from __future__ import annotations

import ast
import asyncio
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from scripts.build_query_singleflight_payload import ADDED_NAMES, build_search


BASELINE = '''import asyncio
import logging
import os
from types import SimpleNamespace

log = logging.getLogger(__name__)
settings = SimpleNamespace(MEM0_REWRITE_MODEL="reference", MEM0_HYDE_ENABLED=True)
complete_json = None
_REWRITE_PROMPT = "unchanged rewrite prompt"
_HYDE_PROMPT = "unchanged hypothetical prompt"
_KEYWORD_EXTRACT_PROMPT = "unchanged keyword prompt"
UNCHANGED_CONSTANT = {"owner": "unchanged"}

async def unrelated_function(value):
    return value

class UnrelatedClass:
    attribute = "preserved"

async def _expand_queries(query: str, user_id: str, *, rewrite: bool) -> list[str]:
    expansion_tasks = [
        asyncio.create_task(_expand_query_via_graph(query, user_id)),
        asyncio.create_task(asyncio.to_thread(_expand_query_via_morpho, query)),
    ]
    if rewrite:
        expansion_tasks.extend([
            asyncio.create_task(_rewrite_query(query)),
            asyncio.create_task(_generate_hypothetical_fact(query)),
            asyncio.create_task(_extract_keywords_via_llm(query)),
        ])
    try:
        expanded = await asyncio.gather(*expansion_tasks)
    except BaseException:
        for task in expansion_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*expansion_tasks, return_exceptions=True)
        raise
    aliases, morpho = expanded[:2]
    queries = [query] + aliases + [morpho]
    if rewrite:
        variants, hyde, llm_keywords = expanded[2:]
        queries.extend(variants + [hyde, llm_keywords])
    return queries
'''


def load_module(source, name):
    module = ModuleType(name)
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def test_payload_preserves_all_other_definitions_imports_and_constants():
    payload = build_search(BASELINE)
    before = ast.parse(BASELINE)
    after = ast.parse(payload)
    original_names = {getattr(node, "name", None) for node in before.body}
    after_originals = [node for node in after.body if getattr(node, "name", None) in original_names
                       and not (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_QUERY_SINGLEFLIGHT" for target in node.targets))
                       and not (isinstance(node, ast.ImportFrom) and node.module == "services.query_expansion")]
    for old, new in zip(before.body, after_originals):
        if getattr(old, "name", None) != "_expand_queries":
            assert ast.dump(old) == ast.dump(new)
    assert "_expand_query_bundle" not in payload
    assert "UNCHANGED_CONSTANT = {\"owner\": \"unchanged\"}" in payload
    assert "async def unrelated_function(value):\n    return value" in payload
    assert build_search(BASELINE) == payload


@pytest.mark.parametrize("change", [
    lambda source: source.replace("expanded[2:]", "expanded[3:]"),
    lambda source: source.replace("expansion_tasks.extend([", "other_tasks.extend(["),
    lambda source: source.replace("_expand_query_via_morpho, query", "_expand_query_via_morpho, query, user_id"),
    lambda source: source.replace("async def _expand_queries", "def _expand_queries"),
])
def test_unrecognized_task_layout_is_rejected(change):
    with pytest.raises(ValueError):
        build_search(change(BASELINE))


@pytest.mark.parametrize("name", sorted(ADDED_NAMES))
def test_existing_symbols_are_not_shadowed(name):
    with pytest.raises(ValueError):
        build_search(BASELINE + f"\n{name} = None\n")


def test_payload_cannot_be_applied_twice():
    with pytest.raises(ValueError):
        build_search(build_search(BASELINE))


async def test_missing_os_dependency_is_added_without_changing_existing_code(monkeypatch):
    monkeypatch.setenv("MEM0_QUERY_SINGLEFLIGHT_ENABLED", "1")
    baseline = BASELINE.replace("import os\n", "")
    payload = build_search(baseline)
    module = load_module(payload, "without_os_baseline")
    assert "import os\n" in payload
    module._rewrite_query = AsyncMock(return_value=["rewritten"])
    module._generate_hypothetical_fact = AsyncMock(return_value="hypothetical")
    module._extract_keywords_via_llm = AsyncMock(return_value="keywords")
    assert await module._shared_query_representations("exact query", "owner") == (["rewritten"], "hypothetical", "keywords")


@pytest.mark.parametrize("binding", ["os = object()", "from unrelated import os", "import unrelated as os"])
def test_existing_os_bindings_are_not_replaced(binding):
    with pytest.raises(ValueError, match="os binding"):
        build_search(BASELINE.replace("import os\n", binding + "\n"))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("rewrite", [False, True])
async def test_full_payload_matches_baseline_and_preserves_fresh_graph_reads(monkeypatch, enabled, rewrite):
    from functools import partial

    monkeypatch.setenv("MEM0_QUERY_SINGLEFLIGHT_ENABLED", str(int(enabled)))
    outputs = []
    helper_counts = []
    for name, source in (("baseline", BASELINE), ("payload", build_search(BASELINE))):
        module = load_module(source, name)
        calls = []
        graph_reads = []
        release = asyncio.Event()
        ready = asyncio.Event()
        expected = 3 if enabled and name == "payload" else 6

        async def inline_thread(function, *arguments):
            return function(*arguments)

        async def graph(query, user_id):
            graph_reads.append((query, user_id))
            return [f"fresh alias {len(graph_reads)}"]

        async def helper(stage, result, query):
            calls.append((stage, query))
            if len(calls) == expected:
                ready.set()
            await release.wait()
            return result

        monkeypatch.setattr(asyncio, "to_thread", inline_thread)
        module._expand_query_via_graph = graph
        module._expand_query_via_morpho = lambda query: "morphological"
        for attribute, result in (("_rewrite_query", ["rewritten"]),
                                  ("_generate_hypothetical_fact", "hypothetical"),
                                  ("_extract_keywords_via_llm", "keywords")):
            setattr(module, attribute, partial(helper, attribute, result))
        tasks = [asyncio.create_task(module._expand_queries("exact query", "owner", rewrite=rewrite)) for _ in range(2)]
        try:
            if rewrite:
                await asyncio.wait_for(ready.wait(), 1)
        finally:
            release.set()
            outputs.append(await asyncio.gather(*tasks))
        helper_counts.append(len(calls))
        assert graph_reads == [("exact query", "owner")] * 2
        assert [result[1] for result in outputs[-1]] == ["fresh alias 1", "fresh alias 2"]
    assert outputs[0] == outputs[1]
    assert helper_counts == ([6, 3 if enabled else 6] if rewrite else [0, 0])


async def test_payload_drains_expansion_when_last_consumer_is_cancelled(monkeypatch):
    monkeypatch.setenv("MEM0_QUERY_SINGLEFLIGHT_ENABLED", "1")
    module = load_module(build_search(BASELINE), "cancelled_payload")
    started = asyncio.Event()
    drained = []

    async def inline_thread(function, *arguments):
        return function(*arguments)

    async def pending(stage, query):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.append(stage)

    from functools import partial
    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    module._expand_query_via_graph = AsyncMock(return_value=[])
    module._expand_query_via_morpho = lambda query: "morphological"
    for attribute in ("_rewrite_query", "_generate_hypothetical_fact", "_extract_keywords_via_llm"):
        setattr(module, attribute, partial(pending, attribute))
    task = asyncio.create_task(module._expand_queries("exact query", "owner", rewrite=True))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(drained) == ["_extract_keywords_via_llm", "_generate_hypothetical_fact", "_rewrite_query"]
