#!/usr/bin/env python3
"""Ingest Discord channel history into Mem0 for a specific user.

Usage:
    python3 scripts/ingest_channel_history.py --channel-id 1234567890 --user-id 573489206240870410
    python3 scripts/ingest_channel_history.py --channel-id 1234567890 --user-id 573489206240870410 --limit 500 --dry-run
    python3 scripts/ingest_channel_history.py --all-channels --user-id 573489206240870410 --limit 200

Reads channel message history via Discord API, pairs user messages with
bot replies, and feeds them through the Mem0 ingestion pipeline (P2 gate
with 2-stage dedup).
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import timezone, timedelta

_JST = timezone(timedelta(hours=9))

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env before importing config
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import discord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ingest_history")


async def fetch_channel_messages(
    client: discord.Client,
    channel_id: int,
    target_user_id: int,
    limit: int = 500,
) -> list[tuple[discord.Message, discord.Message | None]]:
    """Stream channel history and pair user messages with their bot replies.

    Peak memory is O(window) instead of O(channel.history(limit)) — earlier
    versions materialised the full history list, which on a 500-msg fetch
    held tens of MB of Discord Message objects in RAM. We keep a small
    "pending" deque of user messages awaiting a reply within the next 4
    messages and emit pairs as soon as the window closes.

    Returns list of (user_msg, bot_reply_or_None).
    """
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            log.error("Cannot access channel %d: %s", channel_id, e)
            return []

    log.info("Streaming up to %d messages from #%s (%d)",
             limit, getattr(channel, "name", "?"), channel_id)

    bot_id = client.user.id if client.user else None
    # Match the prior algorithm's i+1..i+5 lookahead.
    window = 5
    pending: list[tuple[discord.Message, int]] = []  # (user_msg, count_since)
    pairs: list[tuple[discord.Message, discord.Message | None]] = []
    fetched = 0

    async for msg in channel.history(limit=limit, oldest_first=True):
        fetched += 1

        # Age out pending entries that have outlived the lookahead window.
        next_pending: list[tuple[discord.Message, int]] = []
        for um, count in pending:
            if count >= window:
                pairs.append((um, None))
            else:
                next_pending.append((um, count + 1))
        pending = next_pending

        is_bot_reply = (
            msg.author.bot
            and (bot_id is None or msg.author.id == bot_id)
        )
        if is_bot_reply:
            ref_id = msg.reference.message_id if msg.reference else None
            matched_idx: int | None = None
            # Prefer the explicit reply target.
            for idx, (um, _) in enumerate(pending):
                if ref_id is not None and ref_id == um.id:
                    matched_idx = idx
                    break
            # Fall back to oldest pending — matches the previous "first bot
            # message after the user" attribution.
            if matched_idx is None and pending:
                matched_idx = 0
            if matched_idx is not None:
                um, _ = pending.pop(matched_idx)
                pairs.append((um, msg))
        elif msg.author.id == target_user_id:
            pending.append((msg, 0))

    # Flush leftover pending (no reply seen before history ended).
    for um, _ in pending:
        pairs.append((um, None))

    log.info(
        "Streamed %d messages, found %d pairs (target=%d, %d with bot reply)",
        fetched, len(pairs), target_user_id,
        sum(1 for _, r in pairs if r),
    )
    return pairs


async def ingest_pairs(
    pairs: list[tuple[discord.Message, discord.Message | None]],
    user_id: str,
    *,
    dry_run: bool = False,
    delay: float = 1.0,
    batch_size: int = 5,
    concurrency: int = 1,
    subject: str = "self",
) -> dict:
    """Feed message pairs through Mem0 ingestion pipeline.

    ``concurrency`` controls how many ``add_memories`` calls run in
    parallel; ``delay`` is kept for backwards compat but only applies
    when ``concurrency<=1`` (legacy batched-sequential mode). ``subject``
    selects the extractor ("self" → senpai facts, "kanae" → kanae facts).

    Returns stats dict.
    """
    from services.mem0.ingest import add_memories

    stats = {"total": len(pairs), "ingested": 0, "skipped": 0, "errors": 0,
             "kept": 0, "dup": 0, "low": 0}

    progress_lock = asyncio.Lock()
    progress = {"done": 0}

    def _build_inputs(user_msg, bot_reply):
        user_text = user_msg.content or ""
        assistant_text = bot_reply.content if bot_reply else ""
        # Annotate both texts with the wall-clock JST timestamp so the
        # extractor can attach when-it-happened context to each fact.
        user_ts = user_msg.created_at.astimezone(_JST).strftime("%Y-%m-%d %H:%M")
        user_text_ts = f"[{user_ts} JST] {user_text}"
        if bot_reply is not None:
            bot_ts = bot_reply.created_at.astimezone(_JST).strftime("%Y-%m-%d %H:%M")
            assistant_text_ts = f"[{bot_ts} JST] {assistant_text}"
        else:
            assistant_text_ts = assistant_text
        meta = {
            "channel_id": str(user_msg.channel.id),
            "channel_name": getattr(user_msg.channel, "name", "DM"),
            "source": "history_ingest" + ("_kanae" if subject == "kanae" else ""),
            "created_at": user_msg.created_at.isoformat(),
            "created_at_unix": int(user_msg.created_at.timestamp()),
        }
        return user_text, user_text_ts, assistant_text_ts, meta

    async def _ingest_one(user_msg, bot_reply, sem: asyncio.Semaphore | None):
        # Skip filters: very short user messages (haroin) for self mode; for
        # kanae mode skip when the bot reply itself is missing or too short.
        user_text = user_msg.content or ""
        assistant_text = bot_reply.content if bot_reply else ""
        if subject == "kanae":
            if not bot_reply or len(assistant_text.strip()) < 20:
                stats["skipped"] += 1
                return
        else:
            if len(user_text.strip()) < 10:
                stats["skipped"] += 1
                return

        _, user_text_ts, assistant_text_ts, meta = _build_inputs(user_msg, bot_reply)

        if dry_run:
            preview_text = (assistant_text_ts or "") if subject == "kanae" else user_text_ts
            log.info("DRY-RUN [subject=%s] would ingest: '%s'",
                     subject, preview_text[:90])
            stats["ingested"] += 1
            return

        async def _do():
            try:
                result = await add_memories(
                    user_text=user_text_ts,
                    assistant_text=assistant_text_ts,
                    user_id=user_id,
                    metadata=meta,
                    subject=subject,
                )
                if result and isinstance(result, dict):
                    stats["kept"] += result.get("kept", 0)
                    skipped = result.get("skipped", {})
                    stats["dup"] += skipped.get("near_dup", 0)
                    stats["low"] += skipped.get("low_importance", 0)
                stats["ingested"] += 1
            except Exception as e:
                log.warning("Ingest error for msg %d: %s", user_msg.id, e)
                stats["errors"] += 1
            async with progress_lock:
                progress["done"] += 1
                if progress["done"] % 25 == 0 or progress["done"] == len(pairs):
                    log.info(
                        "Progress: %d/%d (kept=%d, dup=%d, low=%d, err=%d, skip=%d)",
                        progress["done"], len(pairs),
                        stats["kept"], stats["dup"], stats["low"],
                        stats["errors"], stats["skipped"],
                    )

        if sem is None:
            await _do()
        else:
            async with sem:
                await _do()

    if concurrency > 1:
        sem = asyncio.Semaphore(concurrency)
        await asyncio.gather(
            *(_ingest_one(u, b, sem) for u, b in pairs),
            return_exceptions=True,
        )
        return stats

    # Legacy batched-sequential path (concurrency<=1)
    for i in range(0, len(pairs), batch_size):
        batch = pairs[i:i + batch_size]
        for user_msg, bot_reply in batch:
            await _ingest_one(user_msg, bot_reply, None)
        if i + batch_size < len(pairs):
            log.info("Progress: %d/%d (kept=%d, dup=%d, low=%d)",
                     min(i + batch_size, len(pairs)), len(pairs),
                     stats["kept"], stats["dup"], stats["low"])
            await asyncio.sleep(delay)
    return stats


class HistoryBot(discord.Client):
    """Minimal Discord client for reading channel history."""

    def __init__(self, args):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = False
        super().__init__(intents=intents)
        self.args = args

    async def on_ready(self):
        log.info("Logged in as %s (%d)", self.user, self.user.id)
        try:
            await self.run_ingest()
        except Exception as e:
            log.error("Ingest failed: %s", e, exc_info=True)
        finally:
            await self.close()

    async def run_ingest(self):
        args = self.args
        target_user_id = int(args.user_id)

        # Resolve the Mem0 storage namespace. For kanae mode we ALWAYS write
        # under the bot's own ID (regardless of --user-id, which only filters
        # which human's turns to pair against).
        if args.subject == "kanae":
            mem0_user_id = str(self.user.id) if self.user else "kanae"
            log.info("subject=kanae → Mem0 namespace = bot id %s", mem0_user_id)
        else:
            mem0_user_id = str(args.user_id)

        if args.all_channels:
            channels = []
            for guild in self.guilds:
                for ch in guild.text_channels:
                    channels.append(ch.id)
            log.info("Found %d text channels across %d guilds", len(channels), len(self.guilds))
        else:
            channels = [int(args.channel_id)]

        grand_stats = {"total": 0, "ingested": 0, "skipped": 0, "errors": 0,
                       "kept": 0, "dup": 0, "low": 0}

        for ch_id in channels:
            pairs = await fetch_channel_messages(
                self, ch_id, target_user_id, limit=args.limit,
            )
            if not pairs:
                continue

            # For kanae mode, drop pairs without a bot reply (can't extract
            # kanae facts from a missing utterance).
            if args.subject == "kanae":
                before = len(pairs)
                pairs = [(u, b) for (u, b) in pairs if b is not None]
                log.info(
                    "kanae mode: kept %d/%d pairs with bot replies",
                    len(pairs), before,
                )

            stats = await ingest_pairs(
                pairs, mem0_user_id,
                dry_run=args.dry_run,
                delay=args.delay,
                batch_size=args.batch_size,
                concurrency=args.concurrency,
                subject=args.subject,
            )
            for k in grand_stats:
                grand_stats[k] += stats[k]

            log.info("Channel %d done: %s", ch_id, stats)

        log.info("=== FINAL STATS ===")
        log.info("Total pairs: %d | Ingested: %d | Skipped (short): %d | Errors: %d",
                 grand_stats["total"], grand_stats["ingested"],
                 grand_stats["skipped"], grand_stats["errors"])
        log.info("Mem0: kept=%d | dup=%d | low_importance=%d",
                 grand_stats["kept"], grand_stats["dup"], grand_stats["low"])


def main():
    parser = argparse.ArgumentParser(description="Ingest Discord channel history into Mem0")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--channel-id", help="Discord channel ID to ingest")
    group.add_argument("--all-channels", action="store_true", help="Ingest from all text channels")
    parser.add_argument("--user-id", required=True, help="Discord user ID to filter senpai messages by (for kanae mode this is just the pairing filter; Mem0 namespace is bot id)")
    parser.add_argument("--limit", type=int, default=500, help="Max messages to fetch per channel")
    parser.add_argument("--dry-run", action="store_true", help="Log what would be ingested without storing")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay between batches (concurrency<=1 only)")
    parser.add_argument("--batch-size", type=int, default=5, help="Messages per batch (concurrency<=1 only)")
    parser.add_argument("--concurrency", type=int, default=1, help="Parallel ingest tasks (overrides batch/delay)")
    parser.add_argument("--subject", choices=["self", "kanae"], default="self",
                        help="Which extractor to use: 'self' = senpai facts (default), 'kanae' = bot self-facts")
    args = parser.parse_args()

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN not set")
        sys.exit(1)

    bot = HistoryBot(args)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
