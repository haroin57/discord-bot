#!/usr/bin/env python3
"""Operate the Discord bot's durable R2 archive spool."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import r2_archive  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show local spool status")
    sub.add_parser("sync-state", help="stage changed .state files")
    flush = sub.add_parser("flush", help="upload queued objects")
    flush.add_argument("--limit", type=int, default=100)
    fetch = sub.add_parser("fetch", help="download one r2:// URI or object key")
    fetch.add_argument("key")
    fetch.add_argument("--output", required=True)
    recent = sub.add_parser("recent-images", help="show recent image refs for a channel")
    recent.add_argument("--channel", type=int, required=True)
    recent.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    if args.command == "status":
        result = r2_archive.pending_status()
    elif args.command == "sync-state":
        result = r2_archive.sync_state_once()
    elif args.command == "flush":
        result = r2_archive.flush_pending_once(limit=max(1, args.limit))
    elif args.command == "fetch":
        path = r2_archive.fetch_object(args.key, args.output)
        result = {"path": str(path), "size": path.stat().st_size}
    elif args.command == "recent-images":
        result = r2_archive.recent_images(args.channel, limit=args.limit)
    else:
        parser.error("unknown command")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
