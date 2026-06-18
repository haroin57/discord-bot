#!/usr/bin/env python3
"""One-shot migration: move legacy "kanae_self" Mem0 entries to KANAE_BOT_USER_ID.

Background: scripts/kanae_daily.py used to write to user_id="kanae_self"
(string literal) while bot.py wrote to str(bot.user.id). Two namespaces
silently drifted apart — daily writes were stored in "kanae_self" but
the bot recall path only ever read from the bot.user.id namespace, so
daily summaries never reached kanae's prompt.

This script copies every memory from the legacy namespace into the
current one and (optionally) deletes the originals. Designed to be
idempotent and dry-run-safe so it can be re-executed if something looks
off.

Usage:
  python scripts/migrate_kanae_self_namespace.py            # dry-run
  python scripts/migrate_kanae_self_namespace.py --apply    # actually copy
  python scripts/migrate_kanae_self_namespace.py --apply --delete-source
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Allow running from repo root without setting PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrate")

LEGACY_USER_ID = "kanae_self"


async def _list_all(mem, user_id: str) -> list[dict]:
    res = await asyncio.to_thread(mem.get_all, user_id=user_id, limit=500)
    if isinstance(res, dict):
        return res.get("results", []) or []
    return res or []


def _scrub_metadata_for_reinsert(meta: dict | None) -> dict:
    """Drop keys ChromaDB / Mem0 reject on raw insert (None values, ids)."""
    if not isinstance(meta, dict):
        return {}
    out = {}
    for k, v in meta.items():
        if v is None:
            continue
        # `id` / `created_at` / `updated_at` come from Mem0 server-side
        # and would clobber the new entry's own metadata if echoed back.
        if k in ("id", "memory_id", "created_at", "updated_at", "hash"):
            continue
        out[k] = v
    out["migrated_from"] = LEGACY_USER_ID
    return out


async def main(apply: bool, delete_source: bool) -> int:
    from config import KANAE_BOT_USER_ID
    if not KANAE_BOT_USER_ID:
        log.error("KANAE_BOT_USER_ID env is unset; refusing to migrate.")
        return 2
    if KANAE_BOT_USER_ID == LEGACY_USER_ID:
        log.error(
            "KANAE_BOT_USER_ID==%r; nothing to migrate.", LEGACY_USER_ID,
        )
        return 0

    from services.mem0.client import _get_mem0, _refresh_mem0_client_token
    from services.oauth_manager import ensure_valid_token
    await ensure_valid_token()
    _refresh_mem0_client_token()
    mem = _get_mem0()
    if mem is None:
        log.error("Mem0 client unavailable")
        return 3

    legacy = await _list_all(mem, LEGACY_USER_ID)
    target_existing = await _list_all(mem, KANAE_BOT_USER_ID)
    existing_texts = {
        (m.get("memory") or m.get("text") or "").strip()
        for m in target_existing
    }

    log.info(
        "Found %d entries in legacy namespace %r; %d already in target %r",
        len(legacy), LEGACY_USER_ID, len(target_existing), KANAE_BOT_USER_ID,
    )

    plan = []
    for m in legacy:
        text = (m.get("memory") or m.get("text") or "").strip()
        if not text:
            continue
        if text in existing_texts:
            plan.append(("skip-dup", m, text))
            continue
        plan.append(("copy", m, text))

    for action, m, text in plan:
        log.info("[%s] id=%s %s", action, m.get("id"), text[:80])

    if not apply:
        log.info("Dry-run: %d to copy, %d duplicates skipped. Re-run with --apply to execute.",
                 sum(1 for a, *_ in plan if a == "copy"),
                 sum(1 for a, *_ in plan if a == "skip-dup"))
        return 0

    copied = 0
    for action, m, text in plan:
        if action != "copy":
            continue
        meta = _scrub_metadata_for_reinsert(m.get("metadata"))
        try:
            await asyncio.to_thread(
                mem.add,
                [{"role": "user", "content": text}],
                user_id=KANAE_BOT_USER_ID,
                metadata=meta,
                infer=False,
            )
            copied += 1
            log.info("Copied id=%s -> %s", m.get("id"), KANAE_BOT_USER_ID)
        except Exception as exc:
            log.error("Copy failed id=%s: %s", m.get("id"), exc)
    log.info("Copied %d entries", copied)

    if delete_source:
        deleted = 0
        for action, m, _ in plan:
            mid = m.get("id")
            if not mid:
                continue
            try:
                await asyncio.to_thread(mem.delete, memory_id=mid)
                deleted += 1
            except Exception as exc:
                log.error("Delete failed id=%s: %s", mid, exc)
        log.info("Deleted %d source entries", deleted)
    else:
        log.info(
            "--delete-source not set; legacy namespace left intact "
            "(re-run with --delete-source to clean up).",
        )
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="actually perform the copy")
    p.add_argument(
        "--delete-source", action="store_true",
        help="delete source entries after copy (requires --apply)",
    )
    args = p.parse_args()
    if args.delete_source and not args.apply:
        log.error("--delete-source requires --apply")
        sys.exit(2)
    sys.exit(asyncio.run(main(apply=args.apply, delete_source=args.delete_source)))
