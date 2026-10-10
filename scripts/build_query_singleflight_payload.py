"""Build a narrowly verified single-flight overlay from the deployed search source."""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OLD_START = """expansion_tasks = [
    asyncio.create_task(_expand_query_via_graph(query, user_id)),
    asyncio.create_task(asyncio.to_thread(_expand_query_via_morpho, query)),
]"""
OLD_EXTEND = """expansion_tasks.extend([
    asyncio.create_task(_rewrite_query(query)),
    asyncio.create_task(_generate_hypothetical_fact(query)),
    asyncio.create_task(_extract_keywords_via_llm(query)),
])"""
NEW_APPEND = "expansion_tasks.append(asyncio.create_task(_shared_query_representations(query, user_id)))"
OLD_UNPACK = "variants, hyde, llm_keywords = expanded[2:]"
NEW_UNPACK = "variants, hyde, llm_keywords = expanded[2]"
ADDED_NAMES = {"QueryExpansionSingleFlight", "_QUERY_SINGLEFLIGHT",
               "_query_representations", "_shared_query_representations"}


def _extract_function(source: str, name: str) -> str:
    functions = [node for node in ast.parse(source).body if isinstance(node, ast.AsyncFunctionDef) and node.name == name]
    if len(functions) != 1 or functions[0].decorator_list:
        raise ValueError(f"Expected one undecorated helper: {name}")
    function = functions[0]
    return "".join(source.splitlines(keepends=True)[function.lineno - 1:function.end_lineno]).rstrip() + "\n"


def build_search(source: str, *, raw_helpers: str | None = None,
                 shared_helpers: str | None = None) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        name = node.id if isinstance(node, ast.Name) else getattr(node, "name", None)
        if name in ADDED_NAMES:
            raise ValueError(f"Baseline already refers to an added symbol: {name}")
        if isinstance(node, ast.alias) and (node.asname or node.name) in ADDED_NAMES:
            raise ValueError("Baseline already imports an added symbol")
    functions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_expand_queries"]
    if len(functions) != 1 or functions[0].decorator_list:
        raise ValueError("Expected one undecorated _expand_queries baseline")
    target = functions[0]
    patterns = [ast.parse(statement).body[0] for statement in (OLD_START, OLD_EXTEND, OLD_UNPACK)]
    matches = [[node for node in ast.walk(target) if ast.dump(node) == ast.dump(pattern)] for pattern in patterns]
    if any(len(found) != 1 for found in matches):
        raise ValueError("Effective expansion does not match the supported task layout")
    original_raw = raw_helpers if raw_helpers is not None else (ROOT / "services/mem0/search.py").read_text()
    original_shared = shared_helpers if shared_helpers is not None else (ROOT / "deploy/performance/mem0_alpha/search.py").read_text()
    imports_os = any(isinstance(node, ast.Import) and any(alias.name == "os" and (alias.asname or alias.name) == "os" for alias in node.names)
                     for node in tree.body)
    other_os_binding = any(
        (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == "os")
        or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == "os")
        or (isinstance(node, ast.ImportFrom) and any((alias.asname or alias.name) == "os" for alias in node.names))
        or (isinstance(node, ast.Import) and any((alias.asname or alias.name) == "os" and alias.name != "os" for alias in node.names))
        for node in ast.walk(tree)
    )
    if other_os_binding or (not imports_os and any(isinstance(node, ast.Name) and node.id == "os" for node in ast.walk(tree))):
        raise ValueError("Baseline has an unsupported os binding")
    os_import = "" if imports_os else "import os\n\n"
    additions = (
        os_import + "from services.query_expansion import QueryExpansionSingleFlight\n\n"
        "_QUERY_SINGLEFLIGHT = QueryExpansionSingleFlight()\n\n\n"
        + _extract_function(original_raw, "_query_representations") + "\n\n"
        + _extract_function(original_shared, "_shared_query_representations") + "\n\n"
    )
    replacements = [(matches[1][0], NEW_APPEND), (matches[2][0], NEW_UNPACK)]
    lines = source.splitlines(keepends=True)
    for node, statement in sorted(replacements, key=lambda pair: pair[0].lineno, reverse=True):
        lines[node.lineno - 1:node.end_lineno] = [" " * node.col_offset + statement + "\n"]
    lines[target.lineno - 1:target.lineno - 1] = [additions]
    result = "".join(lines)

    class ReplaceStatements(ast.NodeTransformer):
        def generic_visit(self, node):
            for original, statement in replacements:
                if ast.dump(node) == ast.dump(original):
                    return ast.parse(statement).body[0]
            return super().generic_visit(node)

    expected = copy.deepcopy(tree)
    target_index = tree.body.index(target)
    expected.body[target_index] = ReplaceStatements().visit(copy.deepcopy(target))
    expected.body[target_index:target_index] = ast.parse(additions).body
    updated = ast.parse(result)
    if ast.dump(updated) != ast.dump(expected):
        raise ValueError("Payload changes more than the two supported statements and added helpers")
    compile(result, "search.py", "exec")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    source = arguments.baseline.read_text()
    payload = build_search(source)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(payload)
    dependencies = (ROOT / "services/query_expansion.py").read_bytes()
    print(json.dumps({
        "baseline_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "dependency_sha256": hashlib.sha256(dependencies).hexdigest(),
        "changed_function": "_expand_queries",
        "changed_existing_statements": 2,
        "unrelated_ast_preserved": True,
        "bundle_expansion_added": False,
        "default_enabled": False,
        "required_dependency": "services/query_expansion.py",
    }, indent=2))


if __name__ == "__main__":
    main()
