#!/usr/bin/env python3
"""Backfill taxonomy metadata onto untagged Mem0 memories.

Memories ingested before the P2 taxonomy gate (or whose taxonomy was wiped by
a metadata-replacing update) have no `category`. This batch finds them, asks
Haiku 4.5 to classify each from its stored text (services.mem0.extract.
_classify_for_metadata), and writes category/subcategory/importance/tier/tags
back.

CRITICAL — Mem0 1.0.11 `mem.update()` is a FULL payload replace (see
mem0/memory/main.py:_update_memory): only data/hash/created_at/updated_at and
session ids are auto-preserved; every other custom field is dropped unless
re-supplied. So we read the FULL existing metadata, merge the classification
fields on top, and pass the whole dict back. `tags` is a list, but ChromaDB
rejects list-valued metadata, so it is stored as a CSV string (mirrors
services/mem0/ingest.py).

Applies immediately by default (haroin: 可逆優先・即適用). Backfill only ADDS
metadata to untagged rows — it never deletes or rewrites text — so it is the
lowest-risk maintenance action. `--dry-run` reports without writing.

Usage:
    .venv/bin/python scripts/mem0_backfill_taxonomy.py [--user-id <id>] [--max N] [--dry-run]
    MEM0_BACKFILL_MAX=20 .venv/bin/python scripts/mem0_backfill_taxonomy.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, "/home/ubuntu/discord-bot")
from dotenv import load_dotenv  # noqa: E402

load_dotenv("/home/ubuntu/discord-bot/.env")

from services.mem0_service import _get_mem0, _refresh_mem0_client_token  # noqa: E402
from services.oauth_manager import ensure_valid_token  # noqa: E402
from services.mem0.extract import _classify_for_metadata  # noqa: E402

DEFAULT_USER_ID = "573489206240870410"
DEFAULT_MAX = int(os.environ.get("MEM0_BACKFILL_MAX", "50"))

# Classification keys we persist. `tags` is handled separately (list -> CSV).
_SCALAR_KEYS = ("category", "subcategory", "importance", "tier")


def _tags_to_csv(raw) -> str | None:
    """ChromaDB rejects list metadata — store tags as a CSV string
    (mirrors services/mem0/ingest.py)."""
    if not isinstance(raw, (list, tuple)):
        return str(raw).strip() or None if raw else None
    csv = ",".join(str(t).replace(",", " ").strip() for t in raw if str(t).strip())
    return csv or None


def _merge_classification(existing_meta: dict, classification: dict) -> dict:
    """Full metadata to pass to mem.update: existing fields preserved, new
    taxonomy fields layered on top."""
    full = dict(existing_meta)
    for k in _SCALAR_KEYS:
        if classification.get(k) is not None:
            full[k] = classification[k]
    # importance/tier should be ints if present
    for k in ("importance", "tier"):
        if k in full:
            try:
                full[k] = int(full[k])
            except (TypeError, ValueError):
                full.pop(k, None)
    tags_csv = _tags_to_csv(classification.get("tags"))
    if tags_csv and not full.get("tags"):
        full["tags"] = tags_csv
    full["taxonomy_backfilled"] = True  # provenance marker
    return full


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", default=DEFAULT_USER_ID)
    ap.add_argument("--max", type=int, default=DEFAULT_MAX,
                    help="max memories to backfill this run (cost cap)")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify + report, but do not write")
    args = ap.parse_args()

    mem = _get_mem0()
    if not mem:
        print("ERR: Mem0 init failed", file=sys.stderr)
        return 1
    await ensure_valid_token()
    _refresh_mem0_client_token()

    raw = mem.get_all(user_id=args.user_id, limit=10000)
    memories = raw.get("results", []) if isinstance(raw, dict) else raw
    untagged = [m for m in memories if not (m.get("metadata") or {}).get("category")]
    total = len(memories)
    print(f"[backfill] total={total} untagged={len(untagged)} "
          f"cap={args.max} dry_run={args.dry_run}")

    targets = untagged[: args.max]
    classified = updated = skipped = failed = 0

    for m in targets:
        text = m.get("memory") or m.get("text") or ""
        mid = m.get("id")
        if not text or not mid:
            skipped += 1
            continue
        try:
            classification = await _classify_for_metadata(
                [{"role": "user", "content": text}]
            )
        except Exception as exc:
            print(f"  classify failed {str(mid)[:8]}: {exc}", file=sys.stderr)
            failed += 1
            continue
        # best-effort: skip when the classifier couldn't name a category
        if not classification or not classification.get("category"):
            skipped += 1
            continue
        classified += 1
        full = _merge_classification(m.get("metadata") or {}, classification)
        cat = full.get("category")
        if args.dry_run:
            print(f"  [dry] {str(mid)[:8]} -> {cat}/{full.get('subcategory')} "
                  f"imp={full.get('importance')} tier={full.get('tier')}")
            continue
        try:
            await asyncio.to_thread(
                mem.update, memory_id=mid, data=text, metadata=full
            )
            updated += 1
        except Exception as exc:
            print(f"  update failed {str(mid)[:8]}: {exc}", file=sys.stderr)
            failed += 1

    print(f"[backfill] done: classified={classified} updated={updated} "
          f"skipped={skipped} failed={failed} remaining_untagged="
          f"{max(0, len(untagged) - updated)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
