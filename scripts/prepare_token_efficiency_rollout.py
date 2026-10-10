"""Prepare a guarded, minimal Kanae query-sharing overlay and rollback snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_query_singleflight_payload import build_search

UNIT = "discord-bot-kanae-claude.service"
BASELINE = Path("/var/lib/kanae-claude-performance/20260916-opus5/alpha/llm_mem0/search.py")
TARGET = "/var/lib/kanae-claude-quality/20260909-botquality1/alpha/llm_mem0/search.py"
GENERATION = Path("/var/lib/kanae-claude-performance/20261010-token-efficiency1")
EXPECTED_BASELINE = "eacbe15dcb5a84c95feeaed50b4cbcf6f734e90125f3ae76725d1124b8e3f6e5"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_live(report, payload_hash, dependency_hash):
    if (report.get("status") != "complete" or report.get("live_provider_usage") is not True
            or report.get("synthetic") is not True or report.get("failed_attempts")
            or report.get("implementation_sha256") != payload_hash
            or report.get("singleflight_sha256") != dependency_hash):
        raise ValueError("Live evidence is incomplete or does not match the payload")
    rows = report.get("results", [])
    concurrent = [row for row in rows if row["control"] == "concurrent_same_query_two"]
    expected = {(case, arm) for case in range(6) for arm in ("baseline", "singleflight")}
    if len(concurrent) != 12 or {(row["case"], row["arm"]) for row in concurrent} != expected:
        raise ValueError("Six public cases must be measured in both arms")
    for row in rows:
        if (not row.get("dispatch_count_pass") or not row.get("provider_calls_complete")
                or not all(item.get("quality_pass") for item in row["consumer_quality"])
                or not all(call["stop_reason"] == "end_turn" and call["valid_json"] for call in row["calls"])):
            raise ValueError("A live dispatch, completion, or quality gate failed")
        if row["control"] == "concurrent_same_query_two" and row["arm"] == "singleflight":
            if not row["shared_outputs_equal"] or not row["independent_result_lists"]:
                raise ValueError("Shared results changed or mutable result lists were reused")
    sequential = [row for row in rows if row["control"] == "sequential_same_query_two"]
    if len(sequential) != 1 or sequential[0]["requests_attempted"] != 6:
        raise ValueError("Completed responses must not be retained for sequential requests")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--live-report", type=Path)
    args = parser.parse_args()
    baseline = BASELINE.read_bytes()
    if sha(baseline) != EXPECTED_BASELINE:
        raise ValueError("Deployed baseline changed")
    payload = build_search(baseline.decode()).encode()
    dependency = (ROOT / "services/query_expansion.py").read_bytes()
    mounts = subprocess.check_output([
        "systemctl", "show", UNIT, "--property=BindReadOnlyPaths", "--value",
    ], text=True).strip().split()
    expected_mount = f"{BASELINE}:{TARGET}:rbind"
    if mounts.count(expected_mount) != 1:
        raise ValueError("Effective source mount changed")
    ready = args.live_report is not None
    if ready:
        validate_live(json.loads(args.live_report.read_text()), sha(payload), sha(dependency))
    updated = [mount.replace(str(BASELINE), str(GENERATION / "alpha/llm_mem0/search.py"), 1)
               if mount == expected_mount else mount for mount in mounts]
    updated.append(f"{GENERATION}/services/query_expansion.py:{ROOT}/services/query_expansion.py:rbind")

    def config(entries, enabled):
        return "\n".join([
            "[Service]", "# Preserve every preceding quality/performance mount.",
            "BindReadOnlyPaths=", *(f"BindReadOnlyPaths={mount.removesuffix(':rbind')}" for mount in entries),
            f"Environment=MEM0_QUERY_SINGLEFLIGHT_ENABLED={int(enabled)}",
        ]) + "\n"

    directory = args.output_directory
    (directory / "alpha/llm_mem0").mkdir(parents=True, exist_ok=True)
    (directory / "services").mkdir(exist_ok=True)
    (directory / "before").mkdir(exist_ok=True)
    (directory / "alpha/llm_mem0/search.py").write_bytes(payload)
    (directory / "services/query_expansion.py").write_bytes(dependency)
    (directory / "before/search.py").write_bytes(baseline)
    rollout = config(updated, ready)
    rollback = config(mounts, False)
    (directory / "90-token-efficiency.conf").write_text(rollout)
    (directory / "rollback.conf").write_text(rollback)
    manifest = {
        "unit": UNIT, "baseline_source": str(BASELINE), "baseline_sha256": sha(baseline),
        "payload_sha256": sha(payload), "dependency_sha256": sha(dependency),
        "generation": str(GENERATION), "original_mounts": mounts, "new_mounts": updated,
        "unrelated_mounts_preserved": True, "unrelated_ast_preserved": True,
        "changed_existing_statements": 2, "deploy_ready": ready,
        "live_report": str(args.live_report) if ready else None,
        "rollout_sha256": sha(rollout.encode()), "rollback_sha256": sha(rollback.encode()),
        "production_state_modified": False,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
