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
    """Fetch messages from a channel, pairing user messages with bot replies.

    Returns list of (user_msg, bot_reply_or_None).
    """
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            log.error("Cannot access channel %d: %s", channel_id, e)
            return []

    log.info("Fetching up to %d messages from #%s (%d)", limit, getattr(channel, "name", "?"), channel_id)

    messages: list[discord.Message] = []
    async for msg in channel.history(limit=limit, oldest_first=True):
        messages.append(msg)

    log.info("Fetched %d total messages", len(messages))

    # Build pairs: user message + next bot reply (if any)
    pairs: list[tuple[discord.Message, discord.Message | None]] = []
    bot_id = client.user.id if client.user else None

    for i, msg in enumerate(messages):
        if msg.author.id != target_user_id:
            continue
        # Look for the next message from the bot as a reply
        bot_reply = None
        for j in range(i + 1, min(i + 5, len(messages))):
            candidate = messages[j]
            if candidate.author.bot and (bot_id is None or candidate.author.id == bot_id):
                # Check if it's a reply to this message or just the next bot message
                if (candidate.reference and candidate.reference.message_id == msg.id) or \
                   candidate.author.id == bot_id:
                    bot_reply = candidate
                    break
        pairs.append((msg, bot_reply))

    log.info("Found %d user messages (target=%d) with %d having bot replies",
             len(pairs), target_user_id, sum(1 for _, r in pairs if r))
    return pairs


async def ingest_pairs(
    pairs: list[tuple[discord.Message, discord.Message | None]],
    user_id: str,
    *,
    dry_run: bool = False,
    delay: float = 1.0,
    batch_size: int = 5,
) -> dict:
    """Feed message pairs through Mem0 ingestion pipeline.

    Returns stats dict.
    """
    from services.mem0.ingest import add_memories

    stats = {"total": len(pairs), "ingested": 0, "skipped": 0, "errors": 0,
             "kept": 0, "dup": 0, "low": 0}

    for i in range(0, len(pairs), batch_size):
        batch = pairs[i:i + batch_size]
        for user_msg, bot_reply in batch:
            user_text = user_msg.content or ""
            # Skip very short messages (commands, reactions, etc.)
            if len(user_text.strip()) < 10:
                stats["skipped"] += 1
                continue

            assistant_text = bot_reply.content if bot_reply else ""

            if dry_run:
                log.info("DRY-RUN: would ingest user='%s' bot='%s'",
                         user_text[:80], (assistant_text or "")[:80])
                stats["ingested"] += 1
                continue

            try:
                result = await add_memories(
                    user_text=user_text,
                    assistant_text=assistant_text,
                    user_id=user_id,
                    metadata={
                        "channel_id": str(user_msg.channel.id),
                        "channel_name": getattr(user_msg.channel, "name", "DM"),
                        "source": "history_ingest",
                    },
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

        # Rate limit between batches
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

        if args.all_channels:
            # Ingest from all text channels in all guilds
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

            stats = await ingest_pairs(
                pairs, args.user_id,
                dry_run=args.dry_run,
                delay=args.delay,
                batch_size=args.batch_size,
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
    parser.add_argument("--user-id", required=True, help="Discord user ID to extract messages for")
    parser.add_argument("--limit", type=int, default=500, help="Max messages to fetch per channel")
    parser.add_argument("--dry-run", action="store_true", help="Log what would be ingested without storing")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay between batches (seconds)")
    parser.add_argument("--batch-size", type=int, default=5, help="Messages per batch")
    args = parser.parse_args()

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN not set")
        sys.exit(1)

    bot = HistoryBot(args)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
