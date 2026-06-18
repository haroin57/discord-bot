#!/usr/bin/env python3
"""Search Discord channel history for messages matching a keyword.

Usage:
    python3 scripts/search_history.py --keyword "東洋水産" --user-id 1037285234200297522 --all-channels --limit 2000
    python3 scripts/search_history.py --keyword "東洋水産" --channel-id 1234567890 --limit 5000

Reads channel message history via Discord API, finds messages from
target user containing the keyword, and prints them with surrounding
bot replies for context. Read-only — does NOT touch Mem0.
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from datetime import timezone, timedelta, datetime

_JST = timezone(timedelta(hours=9))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import discord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("search_history")


def _fmt_ts(msg) -> str:
    return msg.created_at.astimezone(_JST).strftime("%Y-%m-%d %H:%M")


def _short(text: str, max_len: int = 400) -> str:
    text = text.replace("\n", " ⏎ ")
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


async def search_channel(
    client: discord.Client,
    channel_id: int,
    target_user_id: int,
    keyword_re: re.Pattern,
    limit: int = 2000,
    context_window: int = 3,
) -> list[tuple]:
    """Stream channel and return matches with surrounding context.

    Each match is (channel_name, hit_msg, [(role, msg), ...] context).
    Context = `context_window` messages before + the hit + window after.
    """
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            log.debug("Cannot access channel %d: %s", channel_id, e)
            return []

    ch_name = getattr(channel, "name", f"DM-{channel_id}")
    bot_id = client.user.id if client.user else None
    results: list[tuple] = []

    # Keep a rolling buffer of recent messages so we can attach pre-context.
    from collections import deque
    buf: deque = deque(maxlen=context_window)
    # Track active "harvest tasks" that still need post-context.
    harvesting: list[dict] = []

    try:
        async for msg in channel.history(limit=limit, oldest_first=True):
            # Feed post-context to any active harvest tasks.
            for h in harvesting:
                h["post"].append(msg)
            harvesting = [h for h in harvesting if len(h["post"]) < context_window]

            is_target = msg.author.id == target_user_id
            content = msg.content or ""

            if is_target and keyword_re.search(content):
                pre_ctx = list(buf)
                harvest = {"hit": msg, "pre": pre_ctx, "post": []}
                harvesting.append(harvest)
                # Defer result emission until post-context fills.
                results.append((ch_name, harvest))

            buf.append(msg)

        # Drain any harvest tasks that didn't hit the post-window cap.
        # They already hold partial post; nothing more to do.
    except discord.Forbidden:
        log.debug("Forbidden: cannot read #%s (%d)", ch_name, channel_id)
        return []
    except Exception as e:
        log.warning("Error reading #%s (%d): %s", ch_name, channel_id, e)
        return []

    return [(ch, h) for (ch, h) in results]


def _print_hit(ch_name: str, harvest: dict, bot_id: int | None) -> None:
    hit = harvest["hit"]
    pre = harvest["pre"]
    post = harvest["post"]

    print()
    print("=" * 80)
    print(f"[#{ch_name}] {_fmt_ts(hit)}  msg_id={hit.id}")
    print("=" * 80)

    def _label(m):
        if m.author.bot and (bot_id is None or m.author.id == bot_id):
            return "BOT  "
        return f"USER ({m.author.display_name[:20]})"

    for m in pre:
        print(f"  {_fmt_ts(m)} [{_label(m)}] {_short(m.content)}")
    print(f">>>{_fmt_ts(hit)} [{_label(hit)}] {hit.content}")
    for m in post:
        print(f"  {_fmt_ts(m)} [{_label(m)}] {_short(m.content)}")


class SearchBot(discord.Client):
    def __init__(self, args):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = False
        super().__init__(intents=intents)
        self.args = args

    async def on_ready(self):
        log.info("Logged in as %s (%d)", self.user, self.user.id)
        try:
            await self.run_search()
        except Exception as e:
            log.error("Search failed: %s", e, exc_info=True)
        finally:
            await self.close()

    async def run_search(self):
        args = self.args
        target_user_id = int(args.user_id)
        keyword_re = re.compile(re.escape(args.keyword))

        if args.all_channels:
            channels = []
            for guild in self.guilds:
                for ch in guild.text_channels:
                    channels.append((ch.id, ch.name))
            # Also DMs aren't enumerable here; skipping.
            log.info("Searching %d text channels across %d guilds for '%s'",
                     len(channels), len(self.guilds), args.keyword)
        else:
            channels = [(int(args.channel_id), "?")]

        total_hits = 0
        for ch_id, ch_name in channels:
            results = await search_channel(
                self, ch_id, target_user_id, keyword_re,
                limit=args.limit, context_window=args.context,
            )
            if not results:
                continue
            log.info("Channel #%s (%d): %d hits", ch_name, ch_id, len(results))
            for ch_name_resolved, harvest in results:
                _print_hit(ch_name_resolved, harvest,
                           self.user.id if self.user else None)
                total_hits += 1

        print()
        print(f"=== TOTAL: {total_hits} hits for '{args.keyword}' ===")


def main():
    parser = argparse.ArgumentParser(description="Search Discord channel history")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--channel-id", help="Discord channel ID to search")
    group.add_argument("--all-channels", action="store_true",
                       help="Search all text channels")
    parser.add_argument("--user-id", required=True,
                        help="Discord user ID whose messages to match")
    parser.add_argument("--keyword", required=True,
                        help="Keyword to match (case-sensitive substring)")
    parser.add_argument("--limit", type=int, default=2000,
                        help="Max messages to fetch per channel (default 2000)")
    parser.add_argument("--context", type=int, default=3,
                        help="Messages before/after each hit (default 3)")
    args = parser.parse_args()

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN not set")
        sys.exit(1)

    bot = SearchBot(args)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
