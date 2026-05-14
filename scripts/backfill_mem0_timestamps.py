#!/usr/bin/env python3
"""Backfill `created_at` / `created_at_unix` metadata onto existing Mem0 facts
that don't carry it.

Strategy (see docs/2026-05-14-mem0-timestamp-backfill-plan.md):
  1. Pull the full Discord channel history for the target channel/user.
  2. Embed each user-authored message with OpenAI text-embedding-3-small.
  3. For every Mem0 fact missing `created_at_unix`, embed its text and find
     the nearest message by cosine similarity.
  4. If similarity ≥ THRESHOLD, write the message's timestamp into the
     fact's metadata (partial update; `created_at_source=vector_match`).

Usage:
    python scripts/backfill_mem0_timestamps.py --dry-run
    python scripts/backfill_mem0_timestamps.py --threshold 0.55 --channel-id 1490247632847896776 --user-id 573489206240870410
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("backfill_ts")

DEFAULT_CHANNEL = 1490247632847896776
DEFAULT_USER = "573489206240870410"
DEFAULT_THRESHOLD = 0.55
EMBED_MODEL = "text-embedding-3-small"
EMBED_BATCH = 100


def _normalize(v: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(v, axis=-1, keepdims=True)
    norm[norm == 0] = 1.0
    return v / norm


async def fetch_user_messages(channel_id: int, user_id: int, limit: int):
    """Login to Discord, pull `limit` newest msgs from `channel_id`, return
    only those authored by `user_id`. Cheap clone of the helper in
    scripts/ingest_channel_history.py."""
    import discord
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN missing")

    msgs: list = []

    @client.event
    async def on_ready():
        try:
            ch = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            count = 0
            async for m in ch.history(limit=limit, oldest_first=True):
                if m.author.id == user_id:
                    msgs.append(m)
                count += 1
            log.info("scanned %d total msgs, kept %d from user=%d", count, len(msgs), user_id)
        except Exception as exc:
            log.error("history fetch failed: %s", exc)
        finally:
            await client.close()

    await client.start(token)
    return msgs


def embed_batch(client, texts: list[str]) -> np.ndarray:
    """Embed a batch of texts with OpenAI; returns (N, dim) numpy array."""
    if not texts:
        return np.zeros((0, 1536), dtype=np.float32)
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    return np.array([e.embedding for e in resp.data], dtype=np.float32)


async def run(args):
    from services.mem0.client import _get_mem0
    from openai import OpenAI

    mem = _get_mem0()
    if mem is None:
        raise SystemExit("Mem0 not initialized")

    # 1) Discord 履歴取得
    msgs = await fetch_user_messages(args.channel_id, args.user_id_int, args.limit)
    if not msgs:
        log.warning("no user messages fetched — nothing to match against")
        return
    texts = [(m.content or "").strip() for m in msgs]

    # 2) Discord 側 embed (batched)
    oai = OpenAI()
    msg_vecs: list[np.ndarray] = []
    for i in range(0, len(texts), EMBED_BATCH):
        chunk = texts[i : i + EMBED_BATCH]
        # OpenAI rejects empty strings; replace with a placeholder
        chunk_safe = [t if t else "(empty)" for t in chunk]
        msg_vecs.append(embed_batch(oai, chunk_safe))
        log.info("embedded %d/%d msgs", min(i + EMBED_BATCH, len(texts)), len(texts))
    msg_matrix = _normalize(np.concatenate(msg_vecs, axis=0))

    # 3) Mem0 fact ロード
    log.info("loading mem0 facts for user=%s", args.user_id)
    raw = await asyncio.to_thread(mem.get_all, user_id=args.user_id, limit=10000)
    facts = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
    log.info("loaded %d facts", len(facts))

    todo = [
        f for f in facts
        if not (f.get("metadata") or {}).get("created_at_unix")
    ]
    log.info("facts without created_at_unix: %d (of %d total)", len(todo), len(facts))

    # 4) fact 側 embed
    fact_texts = [(f.get("memory") or "").strip() for f in todo]
    fact_vecs: list[np.ndarray] = []
    for i in range(0, len(fact_texts), EMBED_BATCH):
        chunk = fact_texts[i : i + EMBED_BATCH]
        chunk_safe = [t if t else "(empty)" for t in chunk]
        fact_vecs.append(embed_batch(oai, chunk_safe))
        log.info("embedded %d/%d facts", min(i + EMBED_BATCH, len(fact_texts)), len(fact_texts))
    fact_matrix = _normalize(np.concatenate(fact_vecs, axis=0)) if fact_vecs else np.zeros((0, 1536))

    # 5) cosine 最近傍
    sims = fact_matrix @ msg_matrix.T  # (Nfacts, Nmsgs)
    matched = below = 0
    samples_low: list[tuple[float, str, str]] = []
    samples_high: list[tuple[float, str, str]] = []

    for idx, f in enumerate(todo):
        if idx >= sims.shape[0]:
            continue
        row = sims[idx]
        best_msg_idx = int(np.argmax(row))
        score = float(row[best_msg_idx])
        if score < args.threshold:
            below += 1
            if len(samples_low) < 5:
                samples_low.append((score, fact_texts[idx][:60], texts[best_msg_idx][:60]))
            continue
        matched += 1
        if len(samples_high) < 5:
            samples_high.append((score, fact_texts[idx][:60], texts[best_msg_idx][:60]))

        msg = msgs[best_msg_idx]
        ts_unix = int(msg.created_at.timestamp())
        partial = {
            "created_at_unix": ts_unix,
            "created_at": msg.created_at.isoformat(),
            "created_at_source": "vector_match",
            "created_at_score": round(score, 3),
        }
        if args.dry_run:
            continue
        try:
            await asyncio.to_thread(
                mem.update,
                memory_id=f["id"],
                data=f.get("memory") or "",
                metadata=partial,
            )
        except Exception as exc:
            log.warning("update %s failed: %s", f["id"], exc)

    log.info("=== summary ===")
    log.info("facts without created_at_unix: %d", len(todo))
    log.info("matched (score >= %.2f): %d", args.threshold, matched)
    log.info("below threshold:          %d", below)
    log.info("--- top 5 high score matches ---")
    for s, fa, mg in sorted(samples_high, reverse=True):
        log.info("  %.3f  fact=%r  msg=%r", s, fa, mg)
    log.info("--- bottom 5 near-threshold (kept as null) ---")
    for s, fa, mg in sorted(samples_low):
        log.info("  %.3f  fact=%r  msg=%r", s, fa, mg)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--channel-id", type=int, default=DEFAULT_CHANNEL)
    p.add_argument("--user-id", default=DEFAULT_USER, help="Mem0 user_id (string)")
    p.add_argument("--limit", type=int, default=10000)
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    # discord.py wants int for fetch_channel
    args.user_id_int = int(args.user_id)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
