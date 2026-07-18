#!/usr/bin/env python3
"""Run one Phase 4 pytest lane with a stable marker expression."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = REPO_ROOT / "tests"


@dataclass(frozen=True)
class Lane:
    expression: str
    collect_all_first: bool = False


LANES = {
    "collection-contract": Lane("contract", collect_all_first=True),
    "unit-fast": Lane("unit"),
    "characterization": Lane("characterization"),
    "integration": Lane("integration"),
    "legacy": Lane("legacy"),
    "offline-eval": Lane("eval"),
}


def _pytest_command(*args: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-o",
        "addopts=--strict-markers",
        *args,
    ]


def _run(command: list[str]) -> int:
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    return completed.returncode


def _worker_count(value: str) -> str:
    if value == "auto" or re.fullmatch(r"[1-9][0-9]*", value):
        return value
    raise argparse.ArgumentTypeError("workers must be 'auto' or a positive integer")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=LANES)
    parser.add_argument(
        "--workers",
        default=None,
        type=_worker_count,
        help="pytest-xdist worker count, for example 'auto' or '2'",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="collect the fixed lane without executing it",
    )
    args = parser.parse_args(argv)
    lane = LANES[args.lane]

    if lane.collect_all_first:
        status = _run(_pytest_command("--collect-only", "-qq", str(TEST_ROOT)))
        if status:
            return status

    selected = ["-q", "-m", lane.expression, str(TEST_ROOT)]
    if args.workers:
        selected.extend(("-n", args.workers))
    if args.collect_only:
        selected.append("--collect-only")
    return _run(_pytest_command(*selected))


if __name__ == "__main__":
    raise SystemExit(main())
