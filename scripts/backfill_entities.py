#!/usr/bin/env python3
"""Retroactively extract entities from existing Mem0 facts and populate
services.mem0.graph (entities + fact_entity_edges).

For every fact in Mem0 (per user_id):
  1. Ask Sonnet to enumerate up to 3 entities for that fact (name/type/aliases)
  2. Upsert each entity in the sqlite graph layer
  3. Add a fact_entity edge

This is one-shot work; new facts ingested after this script runs will
auto-populate the graph via services/mem0/ingest.py.

Usage:
    python scripts/backfill_entities.py --dry-run --user-id 573489206240870410
    python scripts/backfill_entities.py --user-id 573489206240870410
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
log = logging.getLogger("backfill_entities")


_PROMPT = """以下の fact から、言及されている固有名詞・概念を 0〜3 個抽出してください。

fact: <untrusted_fact>{text}</untrusted_fact>

ルール:
- name は canonical 表記 (例: "Rust", "Namitape", "クラナド", "佐藤かなえ")
- type は person / project / tech / media / place / concept のいずれか
- aliases に表記揺れを 0〜2 個 (例: "ナミテープ", "namitape")
- 抽象的な単語 (例: "プログラミング", "音楽") は entity にしない
- 該当なし → "entities": []

JSON のみで返してください (説明・コードフェンス不要):
{{"entities":[{{"name":"<canonical>","type":"<one of 6>","aliases":["<alias>"]}}]}}"""


VALID_TYPES = {"person", "project", "tech", "media", "place", "concept"}


async def extract_entities(text: str) -> list[dict]:
    """Single Sonnet call returning a list of {name, type, aliases}."""
    if not text:
        return []
    try:
        from services.mem0.cli_compat import cli_messages_create, extract_text
        from services.mem0.client import get_anthropic_client
        client = await get_anthropic_client()
        if client is None:
            return []
        safe = text[:600].replace("{", "{{").replace("}", "}}")
        resp = await cli_messages_create(
            client,
            model="claude-sonnet-4-6",
            max_tokens=2000,
            user_message=_PROMPT.format(text=safe),
            task_prompt=(
                "You extract named entities from Mem0 fact statements. "
                "Return JSON only."
            ),
        )
        raw = extract_text(resp).strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        out = []
        for e in data.get("entities", []):
            if not isinstance(e, dict):
                continue
            name = (e.get("name") or "").strip()
            etype = (e.get("type") or "").strip()
            if not name or etype not in VALID_TYPES:
                continue
            aliases = [
                a.strip() for a in (e.get("aliases") or [])
                if isinstance(a, str) and a.strip()
            ][:3]
            out.append({"name": name, "type": etype, "aliases": aliases})
        return out[:3]
    except Exception as exc:
        log.warning("extract failed (fail-open): %s", exc)
        return []


async def process_user(
    user_id: str, *, dry_run: bool, max_concurrent: int = 4, delay: float = 1.0,
) -> dict:
    from services.mem0.client import _get_mem0
    from services.mem0 import graph
    mem = _get_mem0()
    if mem is None:
        log.error("Mem0 not initialized")
        return {}

    log.info("fetching memories for user=%s", user_id[-4:])
    raw = await asyncio.to_thread(mem.get_all, user_id=user_id, limit=20000)
    facts = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
    log.info("loaded %d facts for user=...%s", len(facts), user_id[-4:])

    stats = {"facts": len(facts), "entities_added": 0, "edges_added": 0,
             "skipped_no_entity": 0, "errors": 0}
    sem = asyncio.Semaphore(max_concurrent)

    async def _one(f: dict) -> None:
        async with sem:
            text = (f.get("memory") or "").strip()
            fid = f.get("id") or ""
            if not text or not fid:
                stats["errors"] += 1
                return
            entities = await extract_entities(text)
            if not entities:
                stats["skipped_no_entity"] += 1
                await asyncio.sleep(delay)
                return
            if dry_run:
                log.info(
                    "DRY-RUN %s → %s",
                    text[:60],
                    ", ".join(f"{e['name']}({e['type']})" for e in entities),
                )
                stats["entities_added"] += len(entities)
                stats["edges_added"] += len(entities)
                await asyncio.sleep(delay)
                return
            for ent in entities:
                try:
                    eid = await asyncio.to_thread(
                        graph.upsert_entity,
                        ent["name"], ent["type"], user_id,
                        aliases=ent.get("aliases", []),
                    )
                    if eid:
                        ok = await asyncio.to_thread(graph.add_edge, fid, eid)
                        stats["entities_added"] += 1
                        if ok:
                            stats["edges_added"] += 1
                except Exception as exc:
                    log.warning("graph upsert/edge failed: %s", exc)
                    stats["errors"] += 1
            await asyncio.sleep(delay)

    await asyncio.gather(*(_one(f) for f in facts), return_exceptions=True)
    log.info(
        "=== stats user=...%s — facts=%d entities=%d edges=%d "
        "skipped(no_entity)=%d errors=%d",
        user_id[-4:], stats["facts"], stats["entities_added"], stats["edges_added"],
        stats["skipped_no_entity"], stats["errors"],
    )
    return stats


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--user-id", action="append", help="repeat for multiple")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-concurrent", type=int, default=4)
    p.add_argument("--delay", type=float, default=1.0)
    args = p.parse_args()

    uids = args.user_id or [
        "573489206240870410",
        "1037285234200297522",
        "826096545514127400",
    ]
    log.info("targets: %s (dry_run=%s)", uids, args.dry_run)

    grand = {"facts": 0, "entities_added": 0, "edges_added": 0,
             "skipped_no_entity": 0, "errors": 0}
    for uid in uids:
        s = await process_user(
            uid, dry_run=args.dry_run,
            max_concurrent=args.max_concurrent, delay=args.delay,
        )
        for k, v in s.items():
            grand[k] = grand.get(k, 0) + v

    log.info("=== GRAND TOTAL ===")
    for k, v in grand.items():
        log.info("  %s = %d", k, v)

    if not args.dry_run:
        from services.mem0 import graph
        log.info("graph stats now: %s", graph.stats())


if __name__ == "__main__":
    asyncio.run(main())
