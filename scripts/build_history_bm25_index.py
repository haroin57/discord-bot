#!/usr/bin/env python3
"""Rebuild the history BM25 (FTS5) index from the per-channel JSONL transcripts.

Run this once after deploying the history-keyword-recall feature so the index
reflects every conversation line already in .state/history/*.jsonl. New writes
are picked up automatically by the append_jsonl hook; this script only matters
for the initial backfill (and for periodic rebuilds if the index drifts).

reindex_all is a wipe-and-rebuild, so this reads ALL channels in one pass.

Usage:
    .venv/bin/python scripts/build_history_bm25_index.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _iter_lines(history_dir: str):
    """Yield (channel_id, ts, role, text, author) for every transcript line.

    Each .jsonl file is named ``{channel_id}.jsonl``; rows carry role/content
    and (for newer rows) timestamp/author. role is "user"/"assistant" for
    answered turns or "channel" for non-mention messages the bot saw. Malformed
    lines are skipped, not fatal.
    """
    if not os.path.isdir(history_dir):
        logging.warning("history dir not found: %s", history_dir)
        return
    for fname in sorted(os.listdir(history_dir)):
        if not fname.endswith(".jsonl"):
            continue
        try:
            channel_id = int(fname[: -len(".jsonl")])
        except ValueError:
            logging.warning("skip non-channel file: %s", fname)
            continue
        path = os.path.join(history_dir, fname)
        try:
            with open(path, encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        row = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    role = row.get("role")
                    content = row.get("content")
                    if role in ("user", "assistant", "channel") and content:
                        yield (
                            channel_id, row.get("timestamp", ""), role,
                            content, row.get("author", ""),
                        )
        except OSError as exc:
            logging.warning("read failed %s: %s", path, exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild the history BM25 FTS5 index")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count lines without writing the index")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    _configure_logging(args.verbose)

    from config import HISTORY_DIR
    from services.mem0 import history_index

    lines = list(_iter_lines(HISTORY_DIR))
    logging.info("Collected %d transcript lines from %s", len(lines), HISTORY_DIR)

    if args.dry_run:
        logging.info("[dry-run] would reindex %d lines", len(lines))
        return 0

    count = history_index.reindex_all(lines)
    logging.info("Reindexed %d lines. stats=%s", count, history_index.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
