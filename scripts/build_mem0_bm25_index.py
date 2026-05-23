#!/usr/bin/env python3
"""Rebuild the Mem0 BM25 (FTS5) index from existing ChromaDB facts.

Run this once after deploying Phase A-1 so the index reflects the ~1500
facts already in storage. New ingest writes are picked up automatically by
the ingest hook; this script only matters for the initial backfill (and
for periodic rebuilds if the index drifts).

Usage:
    .venv/bin/python scripts/build_mem0_bm25_index.py [--user-id 573...] [--dry-run] [--limit 5000]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _iter_facts(user_id_arg: str | None, limit: int) -> list[tuple[str, str, str]]:
    """Yield (fact_id, text, user_id) tuples from the Mem0 client.

    ``user_id_arg`` accepts a single id, a comma-separated list, or None.
    With None we fall back to KANAE_BOT_USER_ID. The Mem0 SDK's get_all
    requires a user_id, so this loops once per namespace and concatenates.

    NOTE: reindex_all is a wipe-and-rebuild — pass every namespace in a
    single invocation, otherwise the prior run gets dropped on the floor.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from services.mem0.client import _get_mem0

    mem = _get_mem0()
    if mem is None:
        raise RuntimeError("Mem0 client unavailable — check OAuth token and ChromaDB connectivity")

    user_ids: list[str] = []
    if user_id_arg:
        user_ids = [u.strip() for u in str(user_id_arg).split(",") if u.strip()]
    else:
        # Walk common namespaces. KANAE_BOT_USER_ID is the only stable
        # bot-side namespace; for senpai-side ids the caller must pass
        # --user-id explicitly.
        try:
            from config import KANAE_BOT_USER_ID
            if KANAE_BOT_USER_ID:
                user_ids.append(str(KANAE_BOT_USER_ID))
        except Exception:
            pass
        if not user_ids:
            raise SystemExit(
                "No user_id supplied and KANAE_BOT_USER_ID env is empty. "
                "Pass --user-id <discord-id> (or --user-id 'id1,id2') "
                "to index specific namespaces."
            )

    facts: list[tuple[str, str, str]] = []
    for uid in user_ids:
        try:
            raw = mem.get_all(user_id=uid, limit=limit)
        except Exception as exc:
            logging.warning("mem.get_all(%s) failed: %s", uid, exc)
            continue
        rows = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
        for r in rows:
            fid = r.get("id")
            text = r.get("memory") or r.get("text") or ""
            if fid and text:
                facts.append((fid, text, uid))
    return facts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--user-id",
        help="Mem0 namespace to index. Comma-separated for multi-namespace "
             "(e.g. '573489206240870410,1489178684391817336'). "
             "Single-namespace invocations OVERWRITE the global FTS5 store, "
             "so pass every namespace you want indexed in one call.",
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Count facts but don't write the index")
    ap.add_argument("--limit", type=int, default=10000,
                    help="Max facts to fetch per user_id (default 10000)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    _configure_logging(args.verbose)

    facts = _iter_facts(args.user_id, args.limit)
    logging.info("Discovered %d facts to index", len(facts))

    if args.dry_run:
        for fid, text, uid in facts[:5]:
            logging.info("DRY: %s (user=%s) — %s", fid, uid[-4:], text[:80])
        logging.info("DRY mode — no write performed (%d facts)", len(facts))
        return 0

    from services.mem0 import bm25_index
    count = bm25_index.reindex_all(facts)
    logging.info("BM25 index rebuilt: %d rows", count)
    stats = bm25_index.stats()
    logging.info("BM25 store: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
