#!/usr/bin/env python3
"""Mem0 maintenance orchestrator — the action-capable replacement for the
read-only weekly_mem0_compact cron.

Runs, in order, as subprocesses (so the individually-validated scripts stay
unchanged and a failure in one step never aborts the others):

  1. mem0_backfill_taxonomy.py        — tag untagged memories (additive, safe)
  2. mem0_consolidate.py --apply      — merge near-dup clusters, soft-archive
                                         originals (reversible: valid_to)
  3. weekly_mem0_compact.py           — monitoring report; its exit code 2
                                         (other-speaker contamination) is the
                                         alert signal we must preserve.

Writes a combined markdown log to $XDG_STATE_HOME/discord-bot/
mem0_maintenance_<date>.md (mode 700, owner-only — it quotes memory text).

Exit code: 2 if the compact step flagged contamination (so a cron alert hook
fires), else 0. Backfill/consolidate failures are recorded in the report but
do not raise an alert — they are best-effort and self-heal on the next run.

Scope/safety (haroin, 2026-06-20): backfill + consolidate only; reversible-
first (soft-archive, no hard delete); applied immediately (no dry-run gate);
runs 2x/day from cron (07:00 & 19:00 JST).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime

REPO = "/home/ubuntu/discord-bot"
PY = sys.executable or f"{REPO}/.venv/bin/python"
DEFAULT_USER_ID = "573489206240870410"

# Per-step wall-clock caps. Consolidate is LLM-heavy (Haiku over clusters);
# backfill is capped by MEM0_BACKFILL_MAX; compact is read-only and quick.
_TIMEOUTS = {"backfill": 900, "consolidate": 2400, "compact": 300}


def _state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    d = os.path.join(base, "discord-bot")
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _run(label: str, cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=REPO, capture_output=True, text=True,
            timeout=_TIMEOUTS.get(label, 900),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, f"(timeout after {_TIMEOUTS.get(label, 900)}s)"
    except Exception as exc:
        return 1, f"(failed to launch: {exc})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", default=DEFAULT_USER_ID)
    args = ap.parse_args()
    uid = args.user_id

    steps = [
        ("backfill", [PY, f"{REPO}/scripts/mem0_backfill_taxonomy.py",
                      "--user-id", uid]),
        ("consolidate", [PY, f"{REPO}/scripts/mem0_consolidate.py",
                         "--apply", "--user-id", uid]),
        ("compact", [PY, f"{REPO}/scripts/weekly_mem0_compact.py",
                     "--user-id", uid]),
    ]

    started = datetime.now().isoformat(timespec="seconds")
    lines = [f"# Mem0 maintenance run {started}", f"- user_id: {uid}", ""]
    compact_rc = 0

    for label, cmd in steps:
        print(f"[maintenance] running {label} ...", flush=True)
        rc, out = _run(label, cmd)
        if label == "compact":
            compact_rc = rc
        status = "OK" if rc == 0 else f"rc={rc}"
        lines += [f"## {label} ({status})", "", "```", out[-4000:], "```", ""]
        print(f"[maintenance] {label} -> {status}", flush=True)

    report = "\n".join(lines)
    out_path = os.path.join(_state_dir(),
                            f"mem0_maintenance_{datetime.now():%Y%m%d}.md")
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(report)
    print(f"[maintenance] wrote {out_path}", flush=True)

    # Preserve the contamination alert (exit 2) so the cron hook fires.
    return 2 if compact_rc == 2 else 0


if __name__ == "__main__":
    sys.exit(main())
