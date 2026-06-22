#!/usr/bin/env python3
"""Re-ingest past Discord logs into Mem0 straight from the local JSONL mirror.

Unlike scripts/ingest_channel_history.py (which opens a 2nd Discord gateway
client — risky to run beside the live bot), this reads .state/history/{ch}.jsonl
directly, so it needs NO Discord connection and can fan out as wide as we like.
It feeds the now-fixed extraction pipeline (Phase 1/2: JSON-salvage + bigger
max_tokens + wider input) so facts previously lost to truncation are recovered;
dedup turns re-seen facts into reinforcement rather than duplicates.

  python3 scripts/reingest_from_jsonl.py --concurrency 4
  python3 scripts/reingest_from_jsonl.py --concurrency 4 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("reingest_jsonl")

_JST = timezone(timedelta(hours=9))

# (channel_id, channel_name) → which authors to ingest, mapped to Mem0 user_id.
HAROIN = "573489206240870410"
B0NCHICHI = "1037285234200297522"
AUTHOR_TO_UID = {"haroin": HAROIN, "b0nchichi": B0NCHICHI}

# channels requested by haroin (2026-06-22)
CHANNELS = [
    ("1488369795807055964", "一般"),       # ch1: haroin + b0nchichi (投資ch)
    ("1490247632847896776", None),          # ch2: haroin
]

MIN_USER_CHARS = 10  # mirror ingest_channel_history's short-message skip


def _state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".state")
    return base


def _history_path(channel_id: str) -> str:
    # The live bot writes under <repo>/.state/history; honour that exact path.
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo, ".state", "history", f"{channel_id}.jsonl")


def _ts_prefix(raw_ts: str) -> str:
    """'2026-04-22T18:32:01+0900' → '[2026-04-22 18:32 JST] ' (best effort)."""
    if not raw_ts:
        return ""
    try:
        dt = datetime.fromisoformat(raw_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"[{dt.astimezone(_JST).strftime('%Y-%m-%d %H:%M')} JST] "
    except (ValueError, TypeError):
        return ""


def build_pairs(channel_id: str, channel_name: str | None) -> list[dict]:
    """Pair each target-author utterance with the bot's next reply (if any).

    role=user rows were answered → reply is the following role=assistant row.
    role=channel rows are non-mention messages → ingested standalone (no reply).
    """
    path = _history_path(channel_id)
    if not os.path.isfile(path):
        log.warning("no JSONL for channel %s (%s)", channel_id, path)
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    pairs: list[dict] = []
    for i, r in enumerate(rows):
        role = r.get("role")
        author = str(r.get("author") or "")
        uid = AUTHOR_TO_UID.get(author)
        if uid is None or role not in ("user", "channel"):
            continue
        user_text = (r.get("content") or "").strip()
        if len(user_text) < MIN_USER_CHARS:
            continue
        assistant_text = ""
        if role == "user" and i + 1 < len(rows) and rows[i + 1].get("role") == "assistant":
            assistant_text = rows[i + 1].get("content") or ""
        ts = r.get("timestamp") or ""
        pref = _ts_prefix(ts)
        ts_unix = 0
        try:
            ts_unix = int(datetime.fromisoformat(ts).timestamp()) if ts else 0
        except (ValueError, TypeError):
            ts_unix = 0
        pairs.append({
            "uid": uid,
            "user_text": pref + user_text,
            "assistant_text": (pref + assistant_text) if assistant_text else "",
            "meta": {
                "channel_id": channel_id,
                "channel_name": channel_name or "DM",
                "source": "history_ingest_jsonl",
                **({"created_at_unix": ts_unix} if ts_unix else {}),
            },
        })
    return pairs


async def run(concurrency: int, dry_run: bool) -> None:
    all_pairs: list[dict] = []
    for cid, cname in CHANNELS:
        p = build_pairs(cid, cname)
        by_uid: dict[str, int] = {}
        for x in p:
            by_uid[x["uid"]] = by_uid.get(x["uid"], 0) + 1
        log.info("channel %s: %d pairs %s", cid, len(p), by_uid)
        all_pairs.extend(p)
    log.info("TOTAL pairs to ingest: %d (dry_run=%s, concurrency=%d)",
             len(all_pairs), dry_run, concurrency)

    if dry_run:
        for x in all_pairs[:5] + all_pairs[-5:]:
            log.info("DRY uid=%s | %s", x["uid"], x["user_text"][:80])
        return

    from services.mem0.ingest import add_memories
    stats = {"kept": 0, "dup": 0, "low": 0, "err": 0, "done": 0}
    sem = asyncio.Semaphore(concurrency)
    total = len(all_pairs)

    async def _one(x: dict):
        async with sem:
            try:
                res = await add_memories(
                    user_text=x["user_text"],
                    assistant_text=x["assistant_text"],
                    user_id=x["uid"],
                    metadata=x["meta"],
                    subject="self",
                )
                if isinstance(res, dict):
                    stats["kept"] += res.get("kept", 0)
                    sk = res.get("skipped", {})
                    stats["dup"] += sk.get("near_dup", 0)
                    stats["low"] += sk.get("low_importance", 0)
            except Exception as e:
                stats["err"] += 1
                log.warning("ingest error: %s", e)
            stats["done"] += 1
            if stats["done"] % 50 == 0 or stats["done"] == total:
                log.info("Progress %d/%d kept=%d dup=%d low=%d err=%d",
                         stats["done"], total, stats["kept"], stats["dup"],
                         stats["low"], stats["err"])

    await asyncio.gather(*(_one(x) for x in all_pairs), return_exceptions=True)
    log.info("=== DONE === kept=%d dup=%d low=%d err=%d (of %d pairs)",
             stats["kept"], stats["dup"], stats["low"], stats["err"], total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args.concurrency, args.dry_run))


if __name__ == "__main__":
    main()
