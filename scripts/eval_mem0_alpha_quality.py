#!/usr/bin/env python3
"""Deterministic synthetic alpha evaluation; never uses Chroma or a real LLM.

Examples (use a Python environment with the package's dependencies):
  python scripts/eval_mem0_alpha_quality.py --check
  python scripts/eval_mem0_alpha_quality.py --baseline-ref <commit> --output report.json
  python scripts/eval_mem0_alpha_quality.py --source-dir /tmp/wheel-install

Each source runs in a fresh, isolated Python child. The default candidate is
snapshotted first; --baseline-ref extracts only committed llm_mem0 source, without
checking out or modifying the repository. Actual SQLite FTS and alpha public APIs
run against a synthetic provider. These are pipeline/regression scores, NOT
real-embedding, real-LLM, production, or LongMemEval benchmark accuracy.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.metadata
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests/fixtures/mem0_alpha_quality/scenarios.json"
FIXED_TIME = 1_800_000_000
LIMITATION = (
    "Synthetic deterministic pipeline evaluation: actual temporary SQLite FTS, "
    "scripted vector distances and provider-boundary LLM responses. Does not "
    "measure real-provider semantic accuracy, real-user data, or production latency. "
    "Temporal cases test current validity only; historical as-of recall is not evaluated."
)


def ranking_metrics(ids: list[str], ratings: dict[str, int], k: int) -> dict:
    """Empty gold is unscored, not a perfect retrieval result; duplicates do not help."""
    ids = list(dict.fromkeys(ids))[:k]
    relevant = {fid for fid, grade in ratings.items() if grade > 0}
    if not relevant:
        return {"recall_at_k": None, "precision_at_k": None, "mrr": None, "ndcg_at_k": None}
    hits = sum(fid in relevant for fid in ids)
    dcg = sum((2 ** ratings.get(fid, 0) - 1) / math.log2(i + 2)
              for i, fid in enumerate(ids))
    ideal = sum((2 ** grade - 1) / math.log2(i + 2)
                for i, grade in enumerate(sorted(ratings.values(), reverse=True)[:k]))
    return {
        "recall_at_k": hits / len(relevant),
        "precision_at_k": hits / k,
        "mrr": next((1 / (i + 1) for i, fid in enumerate(ids) if fid in relevant), 0.0),
        "ndcg_at_k": dcg / ideal if ideal else 0.0,
    }


def score_case(case: dict, rows: list[dict], registry: dict[str, dict],
               *, error: str | None = None, checks: dict | None = None) -> dict:
    ids = [str(row["id"]) for row in rows if row.get("id")]
    unique = list(dict.fromkeys(ids))
    now = case.get("now", FIXED_TIME)
    leaks = {key: [] for key in ("wrong_owner", "stale", "deleted", "wrong_scope", "future", "unknown")}
    for fid in unique:
        fact = registry.get(fid)
        if fact is None:
            leaks["unknown"].append(fid)
            continue
        md = fact.get("metadata") or {}
        if fact["user_id"] != case.get("user_id", "eval-alice"):
            leaks["wrong_owner"].append(fid)
        if fact.get("deleted"):
            leaks["deleted"].append(fid)
        if md.get("valid_to") is not None and int(md["valid_to"]) <= now:
            leaks["stale"].append(fid)
        if md.get("valid_from") is not None and int(md["valid_from"]) > now:
            leaks["future"].append(fid)
        scope = md.get("scope", "global")
        if scope.startswith("conversation") and scope != f"conversation:{case.get('channel_id', 'room-a')}":
            leaks["wrong_scope"].append(fid)
    should_abstain = bool(case.get("should_abstain"))
    metrics = ranking_metrics(ids, case.get("ratings", {}), case.get("k", 3))
    forbidden = sorted(set(unique) & set(case.get("forbidden_ids", [])))
    checks = checks or {}
    okay = (
        not error and not any(leaks.values()) and not forbidden and len(ids) == len(unique)
        and (not should_abstain or not ids)
        and (should_abstain or metrics["recall_at_k"] == 1.0)
        and all(checks.values())
    )
    return {
        "id": case["id"], "category": case["category"], "k": case.get("k", 3),
        "passed": bool(okay), "retrieved_ids": ids, "ratings": case.get("ratings", {}),
        **metrics, "should_abstain": should_abstain, "abstained": not ids,
        "leaks": leaks, "forbidden_ids_returned": forbidden,
        "duplicate_count": len(ids) - len(unique), "checks": checks, "error": error,
    }


def summarize(results: list[dict]) -> dict:
    def mean(key: str):
        values = [r[key] for r in results if r.get(key) is not None]
        return sum(values) / len(values) if values else None

    negatives = [r for r in results if r["should_abstain"]]
    abstained = [r for r in results if r["abstained"] and not r["error"]]
    correct_abstentions = sum(not r["retrieved_ids"] and not r["error"] for r in negatives)
    return {
        "cases": len(results), "passed": sum(r["passed"] for r in results),
        "errors": sum(bool(r["error"]) for r in results),
        **{name: mean(name) for name in ("recall_at_k", "precision_at_k", "mrr", "ndcg_at_k")},
        "abstention_precision": correct_abstentions / len(abstained) if abstained else None,
        "abstention_recall": correct_abstentions / len(negatives) if negatives else None,
        "answerable_false_abstentions": sum(r["abstained"] and not r["should_abstain"] for r in results),
        "leak_counts": {key: sum(len(r["leaks"][key]) for r in results)
                        for key in ("wrong_owner", "stale", "deleted", "wrong_scope", "future", "unknown")},
        "failed_ids": [r["id"] for r in results if not r["passed"]],
    }


def validate_fixture(fixture: dict) -> None:
    if fixture.get("schema_version") != 1 or not fixture.get("cases"):
        raise ValueError("A nonempty version-1 fixture is required")
    facts = fixture["facts"]
    fact_ids = {row["id"] for row in facts}
    if len(fact_ids) != len(facts):
        raise ValueError("Duplicate fact IDs")
    seen = set()
    for case in fixture["cases"]:
        name = case["id"]
        if not re.fullmatch(r"[a-z0-9_]+", name) or name in seen:
            raise ValueError("Unsafe or duplicate scenario ID")
        seen.add(name)
        k = case.get("k", 3)
        if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 100:
            raise ValueError("Invalid ranking cutoff")
        ratings = case.get("ratings", {})
        if any(isinstance(grade, bool) or not isinstance(grade, int) or grade not in (0, 1, 2)
               for grade in ratings.values()):
            raise ValueError("Relevance grades must be integers 0, 1 or 2")
        if bool(case.get("should_abstain")) == any(grade > 0 for grade in ratings.values()):
            raise ValueError("Abstention and answerable labels must be mutually exclusive")
        referenced = set(ratings)
        for key in ("initial_ids", "forbidden_ids", "must_be_stored", "must_be_archived", "must_not_be_stored"):
            referenced.update(case.get(key, []))
        for hits in case.get("vectors", {}).values():
            for fid, distance in hits:
                referenced.add(fid)
                if not isinstance(distance, (int, float)) or not math.isfinite(distance):
                    raise ValueError("Vector distances must be finite numbers")
        for event in case.get("events", []):
            referenced.update(event.get("must_remain_active", []))
            for message in event["messages"]:
                for part in message["parts"]:
                    if isinstance(part, dict) and "fact" in part:
                        referenced.add(part["fact"])
        if not referenced <= fact_ids:
            raise ValueError("A scenario references an undefined fact")


class SyntheticMemory:
    """Provider boundary with real ownership, mutable metadata and failure injection.

    Explicit vector responses are adversarial retrieval inputs, not relevance
    labels. The provider never reads ratings, forbidden_ids, or should_abstain.
    """

    def __init__(self, facts: dict, initial_ids: list[str], vectors: dict,
                 *, leak_vector_owner=False, fail_once: str | None = None):
        self.templates = copy.deepcopy(facts)
        self.rows = {fid: copy.deepcopy(facts[fid]) for fid in initial_ids}
        self.vectors = vectors
        self.leak_vector_owner = leak_vector_owner
        self.fail_once = fail_once
        self.failed_insert_count = 0
        self.calls = {"search": 0, "get": 0, "get_all": 0, "add": 0, "update": 0}
        self.lock = threading.RLock()

    @staticmethod
    def _shape(row):
        return {"id": row["id"], "memory": row["text"], "user_id": row["user_id"],
                "metadata": {**row.get("metadata", {}), "user_id": row["user_id"]}}

    def search(self, query, user_id=None, filters=None, limit=20, **kwargs):
        owner = user_id or (filters or {}).get("user_id")
        with self.lock:
            self.calls["search"] += 1
            hits = []
            for item in self.vectors.get(query, self.vectors.get("*", [])):
                fid, distance = item
                row = self.rows.get(fid)
                if row and not row.get("deleted") and (self.leak_vector_owner or row["user_id"] == owner):
                    hits.append({**self._shape(row), "score": distance})
            return {"results": hits[:limit]}

    def get(self, memory_id):
        with self.lock:
            self.calls["get"] += 1
            row = self.rows.get(memory_id)
            return self._shape(row) if row and not row.get("deleted") else None

    def get_all(self, user_id=None, filters=None, limit=None, **kwargs):
        filters = dict(filters or {})
        owner = user_id or filters.pop("user_id", None)
        with self.lock:
            self.calls["get_all"] += 1
            rows = [self._shape(row) for row in self.rows.values()
                    if not row.get("deleted") and row["user_id"] == owner
                    and all(row.get("metadata", {}).get(key) == value for key, value in filters.items())]
            return {"results": rows[:limit] if limit is not None else rows}

    def add(self, messages, *, user_id, metadata, infer=False, **kwargs):
        assert infer is False, "The fake provider must never run a built-in extractor."
        text = "\n".join(m["content"] for m in messages)
        with self.lock:
            self.calls["add"] += 1
            match = next((row for row in self.templates.values()
                          if row["text"] == text and row["user_id"] == user_id), None)
            if match is None:
                raise ValueError("Unexpected synthetic fact; update fixture provider inputs explicitly.")
            fid = match["id"]
            if self.fail_once == fid and not self.failed_insert_count:
                self.failed_insert_count += 1
                raise RuntimeError("Synthetic one-shot insert failure")
            self.rows[fid] = {**copy.deepcopy(match), "metadata": copy.deepcopy(metadata)}
            return {"results": [{"id": fid, "memory": text, "event": "ADD"}]}

    def update(self, memory_id, data=None, metadata=None, **kwargs):
        with self.lock:
            self.calls["update"] += 1
            row = self.rows[memory_id]
            if data is not None:
                row["text"] = data
            row.setdefault("metadata", {}).update(metadata or {})
            return {"id": memory_id, "event": "UPDATE"}


class SyntheticBackend:
    """Reads explicit markers only from the actual extraction prompt's user block.

    This measures chunk/turn transmission and parsing, not language understanding.
    Response scripts for conflict/rerank are independent inputs, never gold labels.
    """

    def __init__(self, fact_metadata: dict, *, rerank_response=None, conflict_response="addition"):
        self.fact_metadata = fact_metadata
        self.rerank_response = rerank_response
        self.conflict_response = conflict_response
        self.calls = 0
        self.unexpected_calls = 0
        self.seen_markers: list[str] = []

    async def complete(self, *, system, user_message, model, max_tokens):
        self.calls += 1
        if "rerank" in system.lower() and self.rerank_response is not None:
            return json.dumps(self.rerank_response)
        if "contradicts" in system:
            return json.dumps({"conflict_type": self.conflict_response, "reason": "synthetic script"})
        if "classify the relation" in system:
            return json.dumps({"relation": "different", "reason": "synthetic script"})
        blocks = re.findall(r"<user_text>\s*(.*?)\s*</user_text>", user_message, re.S)
        if blocks:
            facts = []
            for marker, text in re.findall(r"【記憶:([^|】]+)\|([^】]+)】", "\n".join(blocks)):
                self.seen_markers.append(marker)
                facts.append({"speaker": "self", "text": text, "importance": 4,
                              "category": "personal", "scope": "global", "entities": [],
                              **self.fact_metadata.get(marker, {})})
            return json.dumps({"facts": facts, "relations": []}, ensure_ascii=False)
        self.unexpected_calls += 1
        raise RuntimeError("Unscripted offline LLM request")

    def default_model(self):
        return "synthetic-offline"

    def provider_id(self):
        return "synthetic-offline"

    def refresh_memory_llm(self, memory):
        pass

    def mem0_llm_config(self, *, model):
        raise RuntimeError("Real memory provider construction is forbidden")


def _render_messages(specs: list[dict], facts: dict) -> list[dict]:
    messages = []
    for message in specs:
        parts = []
        for part in message["parts"]:
            if isinstance(part, str):
                parts.append(part)
            elif "fact" in part:
                fid = part["fact"]
                parts.append(f"【記憶:{fid}|{facts[fid]['text']}】")
            else:
                parts.append(part.get("repeat", "雑談。") * part["times"])
        messages.append({"role": message["role"], "content": "\n".join(parts)})
    return messages


def _install_guard(workspace: Path) -> list[str]:
    """Fail closed on network, subprocesses and SQLite outside our temp directory."""
    violations = []

    def guard(event, args):
        if event in ("socket.connect", "socket.getaddrinfo", "socket.bind", "subprocess.Popen", "os.system"):
            violations.append(event)
            raise PermissionError(f"Offline evaluator forbids {event}")
        if event == "sqlite3.connect":
            path = os.fspath(args[0])
            if path != ":memory:" and not Path(path).resolve().is_relative_to(workspace):
                violations.append("sqlite_outside_workspace")
                raise PermissionError("SQLite path must be inside the isolated evaluation workspace")
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = Path(os.fsdecode(args[0]))
            if path.name in (".env", "auth.json", ".credentials.json"):
                violations.append("credential_or_dotenv_read")
                raise PermissionError("Credentials and dotenv files are forbidden")
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and not path.resolve().is_relative_to(workspace):
                violations.append("write_outside_workspace")
                raise PermissionError("Writes must remain inside the evaluation workspace")

    sys.addaudithook(guard)
    return violations


async def _evaluate(fixture: dict, source: Path, workspace: Path) -> dict:
    from llm_mem0 import auth, bm25_index, client, graph, ingest, search
    from llm_mem0 import add_memories, search_memories_smart

    facts = {row["id"]: {"user_id": "eval-alice", "metadata": {}, **row}
             for row in fixture["facts"]}
    results = []
    for case in fixture["cases"]:
        case_dir = workspace / case["id"]
        case_dir.mkdir()
        os.environ["LLM_MEM0_STATE_DIR"] = str(case_dir)
        bm25_index._reset_db_path_for_test(str(case_dir / "mem0_bm25.sqlite"))
        graph._store.reset_path_for_test(str(case_dir / "mem0_graph.sqlite"))
        # Old releases have a process-local seen-set; newer releases use
        # provider-confirmed scoped identities instead. Do not require either
        # private implementation in the cross-version evaluator.
        seen = getattr(ingest, "_INSERT_SEEN", None)
        if seen is not None:
            seen.clear()
        provider = SyntheticMemory(
            facts, case.get("initial_ids", []), case.get("vectors", {}),
            leak_vector_owner=case.get("leak_vector_owner", False), fail_once=case.get("fail_once"),
        )
        backend = SyntheticBackend(
            {fid: row.get("extraction_metadata", {}) for fid, row in facts.items()},
            rerank_response=case.get("rerank_response"),
            conflict_response=case.get("conflict_response", "addition"),
        )
        auth.set_auth_backend(backend)
        for fid in case.get("initial_ids", []):
            row = facts[fid]
            if row.get("indexed", True):
                if not bm25_index.index_fact(fid, row["text"], row.get("index_owner", row["user_id"])):
                    raise RuntimeError("Synthetic FTS fixture insertion failed")
        started = time.perf_counter()
        rows, checks, error = [], {}, None
        with mock.patch.object(client, "_get_mem0", return_value=provider), \
             mock.patch.object(search, "_get_mem0", return_value=provider), \
             mock.patch.object(ingest, "_get_mem0", return_value=provider), \
             mock.patch("time.time", return_value=case.get("now", FIXED_TIME)), \
             mock.patch.dict(os.environ, {
                 "MEM0_FOK_ENABLED": str(int(case.get("fok", False))),
                 "MEM0_CLIENT_DEDUP_ENABLED": str(int(case.get("dedup", False))),
             }):
            try:
                for i, event in enumerate(case.get("events", [])):
                    messages = _render_messages(event["messages"], facts)
                    outcome = await add_memories(
                        messages, user_id=case.get("user_id", "eval-alice"),
                        metadata={"channel_id": case.get("channel_id", "room-a"), "source": "synthetic-eval"},
                    )
                    for fid in event.get("must_remain_active", []):
                        row = provider.rows.get(fid)
                        checks[f"event_{i}_preserved_{fid}"] = bool(
                            row and not row.get("metadata", {}).get("valid_to"))
                    if event.get("expect_provider_failure"):
                        checks[f"event_{i}_failure_injected"] = provider.failed_insert_count == 1
                    elif outcome is None or (isinstance(outcome, dict) and outcome.get("error")):
                        checks[f"event_{i}_ingest_success"] = False
                route = case.get("route", "hybrid")
                kwargs = {"limit": case.get("k", 3), "current_channel_id": case.get("channel_id", "room-a")}
                if route == "smart":
                    rows = await search_memories_smart(
                        case["query"], case.get("user_id", "eval-alice"),
                        rewrite=False, rerank=case.get("rerank", False), include_core=False, **kwargs,
                    )
                elif route == "multi":
                    rows = await search.search_memories_multi(
                        case["queries"], case.get("user_id", "eval-alice"), **kwargs)
                else:
                    function = search.search_memories if route == "vector" else search.search_memories_hybrid
                    rows = await function(case["query"], case.get("user_id", "eval-alice"), **kwargs)
                if backend.unexpected_calls:
                    error = "Unscripted provider request"
            except Exception as exc:
                error = type(exc).__name__  # Do not accidentally expose provider content.
        registry = {**facts, **provider.rows}
        for fid in case.get("must_be_stored", []):
            checks[f"stored_{fid}"] = fid in provider.rows
        for fid in case.get("must_be_archived", []):
            checks[f"archived_{fid}"] = bool(provider.rows.get(fid, {}).get("metadata", {}).get("valid_to"))
        for fid in case.get("must_not_be_stored", []):
            checks[f"not_stored_{fid}"] = fid not in provider.rows
        checks["vector_provider_called"] = provider.calls["search"] > 0
        result = score_case(case, rows, registry, error=error, checks=checks)
        result.update({"latency_ms": round((time.perf_counter() - started) * 1000, 3),
                       "provider_calls": provider.calls, "llm_calls": backend.calls,
                       "extraction_markers_received": sorted(set(backend.seen_markers)),
                       "sqlite_rows": bm25_index.stats()["rows"]})
        results.append(result)
    imported = {name: str(Path(module.__file__).resolve()) for name, module in sys.modules.items()
                if (name == "llm_mem0" or name.startswith("llm_mem0.")) and getattr(module, "__file__", None)}
    if not all(Path(path).is_relative_to(source) for path in imported.values()):
        raise RuntimeError("An alpha import escaped the requested source snapshot")
    return {"summary": summarize(results), "categories": {
        category: summarize([row for row in results if row["category"] == category])
        for category in sorted({row["category"] for row in results})},
        "cases": results, "import_origin_verified": True, "imported_module_count": len(imported)}


def _worker(source: Path, fixture_path: Path) -> dict:
    # Clear inherited credentials/provider flags BEFORE any alpha import. -I/-B
    # already protect against ambient PYTHONPATH and bytecode writes.
    os.environ.clear()
    with tempfile.TemporaryDirectory(prefix="mem0-alpha-eval-worker-") as directory:
        workspace = Path(directory).resolve()
        for name in ("home", "state", "tmp"):
            (workspace / name).mkdir()
        os.environ.update({
            "HOME": str(workspace / "home"), "TMPDIR": str(workspace / "tmp"),
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC",
            "LLM_MEM0_STATE_DIR": str(workspace / "state"), "MEM0_COLLECTION_NAME": "synthetic-offline-eval",
            "LLM_MEM0_ALLOW_TOKEN_REFRESH": "false", "LLM_MEM0_DISABLE_KEYCHAIN": "true",
            "MEM0_CORE_MEMORY_ENABLED": "false", "MEM0_HYBRID_ENABLED": "true",
            "MEM0_HYDE_ENABLED": "false", "MEM0_HIDE_ARCHIVED": "true",
            "MEM0_SCOPE_FILTER_ENABLED": "true", "MEM0_EPISODE_BUNDLE_ENABLED": "false",
            "MEM0_RECALL_REINFORCE_ENABLED": "false", "MEM0_CLIENT_DEDUP_ENABLED": "false",
            "MEM0_INGEST_HASH_DEDUP": "true", "MEM0_ATTRIBUTE_CONFLICT_ENABLED": "true",
            "MEM0_FACT_STORE_ENABLED": "false", "MEM0_RELATION_EXTRACT_ENABLED": "false",
            "MEM0_GATE_ENABLED": "true", "MEM0_EXTRACT_INPUT_CHARS": "2000",
        })
        os.chdir(workspace)
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(source))
        violations = _install_guard(workspace)
        logging.disable(logging.CRITICAL)
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        validate_fixture(fixture)
        report = asyncio.run(_evaluate(fixture, source, workspace))
        report["isolation"] = {"network_attempts_or_path_violations": violations,
                               "temporary_sqlite": True, "dotenv_loaded": False,
                               "real_provider_calls": 0}
        if violations:
            raise RuntimeError("Offline isolation guard was triggered")
        return report


def _source_snapshot(source: Path, destination: Path) -> str:
    """Copy source only, never credentials, caches, editable metadata, or data."""
    package = source / "llm_mem0"
    if not (package / "__init__.py").is_file():
        raise ValueError("--source-dir must contain llm_mem0/__init__.py")
    digest = hashlib.sha256()
    paths = sorted(p for p in package.rglob("*") if p.is_file() and (p.suffix == ".py" or p.name == "py.typed"))
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError("Source snapshots cannot contain symlinked package files")
        relative = path.relative_to(source)
        data = path.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        digest.update(str(relative).encode() + b"\0" + data + b"\0")
    return digest.hexdigest()


def _baseline_snapshot(ref: str, destination: Path) -> tuple[str, str]:
    commit = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", f"{ref}^{{commit}}"], text=True).strip()
    prefix = "vendor/mem0-alpha/"
    raw = subprocess.check_output(["git", "-C", str(ROOT), "archive", commit, prefix + "llm_mem0"])
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for entry in archive:
            relative = Path(entry.name.removeprefix(prefix))
            if not entry.isfile():
                if not entry.isdir():
                    raise ValueError("Committed source contains a non-regular file")
                continue
            if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "llm_mem0":
                raise ValueError("Invalid committed package path")
            if relative.suffix != ".py" and relative.name != "py.typed":
                continue
            stream = archive.extractfile(entry)
            if stream is None:
                raise ValueError("Cannot read committed package member")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stream.read())
    return commit, _source_digest(destination)


def _source_digest(source: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((source / "llm_mem0").rglob("*")):
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed"):
            digest.update(str(path.relative_to(source)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _run_child(source: Path, fixture: Path, timeout: float) -> dict:
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--_worker",
         "--source-dir", str(source), "--fixture", str(fixture)],
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        text=True, capture_output=True, timeout=timeout, check=False,
    )
    if result.returncode:
        # Worker output contains only synthetic data; bounded diagnostics aid CI.
        raise RuntimeError(f"Isolated evaluator failed ({result.returncode}): {result.stderr[-3000:]}")
    return json.loads(result.stdout)


def compare_reports(baseline: dict, candidate: dict) -> dict:
    before = {r["id"]: r for r in baseline["cases"]}
    after = {r["id"]: r for r in candidate["cases"]}
    if before.keys() != after.keys():
        raise ValueError("Baseline and candidate cases differ")
    return {
        "newly_passing": [key for key in before if not before[key]["passed"] and after[key]["passed"]],
        "regressed": [key for key in before if before[key]["passed"] and not after[key]["passed"]],
        "ranking_regressions": [key for key in before if any(
            before[key].get(metric) is not None and after[key].get(metric) is not None
            and after[key][metric] < before[key][metric] - 1e-12
            for metric in ("recall_at_k", "mrr", "ndcg_at_k"))],
        "metric_deltas": {key: candidate["summary"][key] - baseline["summary"][key]
                          for key in ("recall_at_k", "mrr", "ndcg_at_k")
                          if candidate["summary"][key] is not None and baseline["summary"][key] is not None},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-dir", type=Path, default=ROOT / "vendor/mem0-alpha")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--baseline-ref", help="Committed source to evaluate in a separate child, without checkout")
    parser.add_argument("--output", type=Path, help="Optional JSON result; stdout otherwise")
    parser.add_argument("--check", action="store_true", help="Nonzero if any candidate case or baseline regression fails")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker:
        print(json.dumps(_worker(args.source_dir.resolve(), args.fixture.resolve()), ensure_ascii=False))
        return 0
    with tempfile.TemporaryDirectory(prefix="mem0-alpha-eval-sources-") as directory:
        scratch = Path(directory)
        fixture = scratch / "scenarios.json"
        fixture.write_bytes(args.fixture.read_bytes())
        validate_fixture(json.loads(fixture.read_text(encoding="utf-8")))
        source = scratch / "candidate"
        source_hash = _source_snapshot(args.source_dir.resolve(), source)
        report = {"schema_version": 1, "limitation": LIMITATION,
                  "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                  "python_version": sys.version.split()[0], "fixed_time": FIXED_TIME,
                  "sqlite_version": sqlite3.sqlite_version,
                  "janome_version": importlib.metadata.version("Janome"),
                  "candidate_source_sha256": source_hash,
                  "candidate": _run_child(source, fixture, args.timeout)}
        if args.baseline_ref:
            baseline = scratch / "baseline"
            commit, baseline_hash = _baseline_snapshot(args.baseline_ref, baseline)
            report.update({"baseline_commit": commit, "baseline_source_sha256": baseline_hash,
                           "baseline": _run_child(baseline, fixture, args.timeout)})
            report["comparison"] = compare_reports(report["baseline"], report["candidate"])
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8")
            print(json.dumps({"candidate": report["candidate"]["summary"],
                              "comparison": report.get("comparison")}, ensure_ascii=False, indent=2))
        else:
            print(encoded, end="")
    failed = report["candidate"]["summary"]["passed"] != report["candidate"]["summary"]["cases"]
    comparison = report.get("comparison", {})
    return int(args.check and (failed or bool(comparison.get("regressed"))
                              or bool(comparison.get("ranking_regressions"))))


if __name__ == "__main__":
    raise SystemExit(main())
