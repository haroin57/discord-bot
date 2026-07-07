#!/usr/bin/env python3
"""Phase ② nightly maintenance: tier decay + gist compression.

Not the same system as ``scripts/mem0_consolidate.py`` (ADR-0016's embedding
near-dup clustering, run via ``scripts/mem0_maintenance.py``). This script is
NOT wired into that cron orchestrator — it's a new, separately-validated
capability (tier reclassification from ACT-R activation, plus fuzzy-trace
gist compression of Cold-tier clusters) and should be dry-run-observed for a
while before anyone adds it to the production cron rotation.

Usage:
    .venv/bin/python scripts/mem0_tier_decay.py --user-id 573489206240870410 \\
        --tier-decay --gist-compress
    .venv/bin/python scripts/mem0_tier_decay.py --user-id ... \\
        --tier-decay --apply
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
log = logging.getLogger("mem0_tier_decay")

DEFAULT_USER = "573489206240870410"


async def run(args) -> None:
    from services.mem0.decay import gist_compress_for_user, recompute_tiers_for_user

    if args.tier_decay:
        summary = await recompute_tiers_for_user(args.user_id, dry_run=not args.apply)
        log.info("[TIER_DECAY_SUMMARY user=%s %s]", args.user_id, summary)
    if args.gist_compress:
        summary = await gist_compress_for_user(args.user_id, dry_run=not args.apply)
        log.info("[GIST_COMPRESS_SUMMARY user=%s %s]", args.user_id, summary)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user-id", default=DEFAULT_USER)
    ap.add_argument("--tier-decay", action="store_true")
    ap.add_argument("--gist-compress", action="store_true")
    ap.add_argument("--apply", action="store_true",
                     help="Actually write changes (default: dry-run report only)")
    args = ap.parse_args()
    if not (args.tier_decay or args.gist_compress):
        ap.error("pass at least one of --tier-decay / --gist-compress")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
