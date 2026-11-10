#!/usr/bin/env python3
"""Reproducible static inventory + optional sanitized Linux resource snapshot.

No application imports, environment values, process command lines, database
contents, remote API calls, or service mutations. Static flags identify review
candidates, not measured bottlenecks. Newly added source files are included.
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

NATIVE = {"pyarrow", "lancedb", "pandas", "numpy", "chromadb", "mem0", "janome", "matplotlib", "PIL"}
SERVICES = ["discord-bot.service", "discord-bot-kanae-claude.service", "hermes-discord-codex.service", "chromadb-server.service", "mcp-mem0-shared.service", "intervention-scheduler.service", "vi-signal-scheduler.service"]


def call_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def scan_file(root: Path, relative: str) -> dict:
    source = (root / relative).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative)
    imports = []
    signals = collections.defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                imports.append(name)
                if name.split(".")[0] in NATIVE:
                    signals["native_or_heavy_import"].append(node.lineno)
        if isinstance(node, ast.Call):
            name = call_name(node.func)
            if any(value in name for value in ("subprocess", "Popen", "create_subprocess")):
                signals["process_spawn"].append(node.lineno)
            if name.endswith((".connect", ".execute", ".executemany", ".write_text", ".write_bytes", ".open", ".fsync", "atomic_write_json")):
                signals["storage_or_native_io"].append(node.lineno)
            if name.endswith((".create_task", ".to_thread", ".run_in_executor")):
                signals["background_or_thread"].append(node.lineno)
            if name.endswith((".read_text", ".read_bytes", ".to_pandas", ".readlines")):
                signals["whole_object_materialization"].append(node.lineno)
    return {
        "path": relative, "lines": len(source.splitlines()),
        "imports": sorted(set(imports)),
        "signals": {key: sorted(set(value)) for key, value in sorted(signals.items())},
        "role": "test" if relative.startswith("tests/") else "script" if relative.startswith("scripts/") else "application",
    }


def runtime_snapshot() -> dict:
    snapshot = {}
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        allowed = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
        snapshot["memory_kib"] = {
            line.split(":")[0]: int(line.split()[1])
            for line in meminfo.read_text().splitlines() if line.split(":")[0] in allowed
        }
    snapshot["pressure"] = {
        kind: Path(f"/proc/pressure/{kind}").read_text().strip()
        for kind in ("cpu", "memory", "io") if Path(f"/proc/pressure/{kind}").exists()
    }
    try:
        result = subprocess.run(
            ["systemctl", "show", *SERVICES, "-p", "Id", "-p", "MainPID", "-p", "MemoryCurrent", "-p", "CPUUsageNSec", "-p", "ActiveState"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        units = []
        for block in result.stdout.strip().split("\n\n"):
            row = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            pid = row.get("MainPID", "0")
            if pid != "0":
                try:
                    values = Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines()
                    row["pss_kib"] = next(int(line.split()[1]) for line in values if line.startswith("Pss:"))
                except (OSError, StopIteration):
                    row["pss_kib"] = None
            if row:
                units.append(row)
        snapshot["systemd"] = units
    except (OSError, subprocess.SubprocessError):
        snapshot["systemd"] = "unavailable"
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime", action="store_true")
    args = parser.parse_args()
    result = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=args.root, capture_output=True, check=True)
    files = sorted(set(result.stdout.decode().strip("\0").split("\0")))
    entries = [scan_file(args.root, path) for path in files if path.endswith(".py") and (args.root / path).is_file()]
    counts = collections.Counter(entry["role"] for entry in entries)
    data = {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Python AST inventory; flags require manual review; no runtime attribution inferred",
        "counts": dict(counts), "python_lines": sum(entry["lines"] for entry in entries),
        "other_code_and_deploy_files": [path for path in files if path.endswith((".js", ".mjs", ".ts", ".sh", ".service", ".timer", ".toml", ".jsonc", ".yml")) and "worker-configuration" not in path],
        "files": entries,
    }
    if args.runtime:
        data["runtime"] = runtime_snapshot()
    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
        print(json.dumps({"output": str(args.output), "counts": data["counts"], "python_lines": data["python_lines"]}))
    else:
        print(body, end="")


if __name__ == "__main__":
    main()
