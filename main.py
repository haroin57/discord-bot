#!/usr/bin/env python3
"""Entry point — matches free-code entrypoints/ pattern."""

import logging

from config import DISCORD_TOKEN
from utils.attachments import cleanup_stale_attachments

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main():
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN is not set. Create a .env file with:")
        print("  DISCORD_TOKEN=your_discord_bot_token_here")
        raise SystemExit(1)

    cleanup_stale_attachments()

    # Import bot here to trigger client setup and event registration
    from bot import client
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
