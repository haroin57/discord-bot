#!/usr/bin/env python3
"""Phase ③ nightly maintenance: replay + schema abstraction.

Not the same system as ``scripts/mem0_consolidate.py`` (ADR-0016's
embedding near-dup clustering, run via ``scripts/mem0_maintenance.py``).
This script is NOT wired into that cron orchestrator — replay and schema
abstraction are new, separately-validated capabilities and should be
dry-run-observed for a while before anyone adds them to the production
cron rotation.

Usage:
    .venv/bin/python scripts/mem0_replay.py \\
        --channel 123456:573489206240870410 \\
        --replay --schema-abstract
    .venv/bin/python scripts/mem0_replay.py \\
        --channel 123456:573489206240870410 --replay --apply

``--channel channel_id:user_id`` may be repeated. Schema abstraction runs
once per distinct user_id seen across the supplied channels (it operates
over the whole fact store, not per-channel). Without --apply, both steps
run dry-run (compute + log only).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(_REPO / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mem0_replay")


async def run(channel_pairs, *, replay: bool, schema_abstract: bool, apply: bool) -> None:
    from services.mem0.replay import abstract_schemas_for_user, replay_channel_history

    seen_users: list[str] = []
    for channel_id, user_id in channel_pairs:
        if replay:
            summary = await replay_channel_history(channel_id, user_id, dry_run=not apply)
            log.info("[REPLAY_SUMMARY channel=%s user=%s %s]", channel_id, user_id, summary)
        if user_id not in seen_users:
            seen_users.append(user_id)
    if schema_abstract:
        for user_id in seen_users:
            summary = await abstract_schemas_for_user(user_id, dry_run=not apply)
            log.info("[SCHEMA_SUMMARY user=%s %s]", user_id, summary)


def _parse_channel(spec: str) -> tuple[int, str]:
    if ":" not in spec:
        raise argparse.ArgumentTypeError("expected channel_id:user_id")
    cid, uid = spec.split(":", 1)
    return int(cid), uid


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", action="append", required=True, dest="channels",
                     type=_parse_channel, help="channel_id:user_id (repeatable)")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--schema-abstract", action="store_true")
    ap.add_argument("--apply", action="store_true",
                     help="Actually write changes (default: dry-run report only)")
    args = ap.parse_args()
    if not (args.replay or args.schema_abstract):
        ap.error("pass at least one of --replay / --schema-abstract")
    asyncio.run(run(
        args.channels, replay=args.replay,
        schema_abstract=args.schema_abstract, apply=args.apply,
    ))


if __name__ == "__main__":
    main()
