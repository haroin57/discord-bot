#!/usr/bin/env python3
"""Backfill the Phase-5 semantic transcript index from .state/history/*.jsonl.

The live append_jsonl hook only indexes NEW lines once MEM0_HISTORY_EMBED_ENABLED
is on; this seeds the index with the existing transcript. Embeds are batched
(one OpenAI call per --batch lines) so a few-thousand-line channel is minutes,
not hours. Idempotent: upsert by the same id history_embed.index_line uses.

  python3 scripts/backfill_history_embed.py                 # all channels
  python3 scripts/backfill_history_embed.py --channel-id 1488369795807055964
"""
import argparse
import glob
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(_REPO, ".env"))

# Force-enable for the backfill regardless of the runtime flag.
os.environ["MEM0_HISTORY_EMBED_ENABLED"] = "1"

from services.mem0 import history_embed  # noqa: E402


def _history_files(channel_id: str | None) -> list[str]:
    base = os.path.join(_REPO, ".state", "history")
    if channel_id:
        p = os.path.join(base, f"{channel_id}.jsonl")
        return [p] if os.path.isfile(p) else []
    return sorted(glob.glob(os.path.join(base, "*.jsonl")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel-id")
    ap.add_argument("--batch", type=int, default=128)
    args = ap.parse_args()

    files = _history_files(args.channel_id)
    if not files:
        print("no history JSONL found")
        return 1
    col = history_embed._get_collection()
    grand = 0
    for path in files:
        cid = os.path.splitext(os.path.basename(path))[0]
        ids: list[str] = []
        docs: list[str] = []
        metas: list[dict] = []
        n = 0

        def _flush():
            nonlocal ids, docs, metas, n, grand
            if not ids:
                return
            try:
                col.upsert(ids=ids, documents=docs, metadatas=metas)
                n += len(ids)
                grand += len(ids)
            except Exception as exc:  # don't let one bad batch kill the run
                print(f"  {cid}: batch of {len(ids)} failed ({exc}); skipping",
                      flush=True)
            else:
                print(f"  {cid}: indexed {n}", flush=True)
            ids, docs, metas = [], [], []

        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = r.get("role")
                content = (r.get("content") or "").strip()
                if not role or not content:
                    continue
                ts = r.get("timestamp", "")
                ids.append(history_embed._line_id(cid, ts, role, content))
                docs.append(history_embed.safe_text(content))
                metas.append({
                    "channel_id": str(cid),
                    "ts": ts or "",
                    "role": role,
                    "author": r.get("author", "") or "",
                })
                if len(ids) >= args.batch:
                    _flush()
        _flush()
        print(f"channel {cid}: done ({n} lines)", flush=True)
    print(f"=== backfill complete: {grand} lines indexed ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
