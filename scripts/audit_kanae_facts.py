#!/usr/bin/env python3
"""Audit kanae's Mem0 facts against the original chat history.

For each fact stored under kanae's namespace:
  1. Fetch the full Discord channel history and build (senpai_msg, bot_reply)
     pairs (same shape as scripts/ingest_channel_history.py).
  2. Use OpenAI embeddings to find the bot_reply most similar to the fact —
     that's our best guess at the source turn the fact was extracted from.
  3. Ask Sonnet 4.6 to read the fact alongside the source turn and classify:
       - "kanae"                : fact is genuinely about kanae's character/
                                  knowledge/style/behavior/opinion
       - "haroin_misattributed" : fact is actually about senpai's
                                  preference/experience but was extracted
                                  under kanae's namespace by mistake
       - "ambiguous"            : cannot decide
  4. Write a markdown report of every flagged fact.
  5. With --commit, delete the "haroin_misattributed" facts from kanae's
     Mem0 namespace (also clear their graph edges so the entities don't
     dangle).

Usage:
    python scripts/audit_kanae_facts.py                 # dry-run, report only
    python scripts/audit_kanae_facts.py --commit        # actually delete
    python scripts/audit_kanae_facts.py --limit 100     # debug: classify only 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / ".env")

import discord  # noqa: E402
import numpy as np  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("audit_kanae")

KANAE_ID = "1489178684391817336"
HAROIN_ID = "573489206240870410"
CHANNEL_ID = 1490247632847896776


@dataclass
class TurnPair:
    """Plain-data snapshot of a (senpai, kanae) turn so we can hold onto it
    after the Discord client is closed."""
    senpai_text: str
    kanae_text: str
    senpai_ts: str
    kanae_ts: str


_CLASSIFY_PROMPT = """以下は Discord での 1 ターンと、そこから抽出されてかなえ namespace に保存された fact です。
fact が **かなえ本人の事実** か、**先輩の事実が誤ってかなえに割り当てられたもの** かを判定してください。

<senpai>
{senpai}
</senpai>

<kanae>
{kanae}
</kanae>

<extracted_fact_stored_under_kanae>
{fact}
</extracted_fact_stored_under_kanae>

判定基準:
- "kanae": fact がかなえ本人の能力・性格・知識・スタイル・行動・感情・意見・嗜好など、彼女自身の特性を述べている
  - 例: 「先輩を心配するスタイル」「Mem0 の内部実装を理解している」「事象の地平面を説明できる知識を持つ」
- "haroin_misattributed": fact が実際は先輩の嗜好・経験・行動・知識・所有物についてで、かなえに保存されたのは誤り
  - 例: 「四畳半神話大系が好きだと述べている」(主語が先輩なのにかなえ側にある)
  - 例: 「本棚にある漫画: ○○」(先輩の所有物)
  - 例: 「○○というキャラクターに自分を重ねている」(主語=先輩であるべき)
- "ambiguous": 文脈から主語が決定できない / どちらの可能性もある

判定のヒント: fact text の主語が <senpai> の発話と一致しているなら haroin_misattributed の可能性が高い。
かなえの発話・行動・推論・感想・知識を述べているなら kanae。

JSON のみ返してください (説明・コードフェンス不要):
{{"attribution":"<kanae|haroin_misattributed|ambiguous>","reason":"<40文字以内>"}}"""


class HistoryClient(discord.Client):
    """One-shot client: login → fetch channel history → snapshot pairs → close."""

    def __init__(self, channel_id: int, haroin_id: int):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.channel_id = channel_id
        self.haroin_id = haroin_id
        self.pairs: list[TurnPair] = []

    async def on_ready(self):
        try:
            log.info("Logged in as %s, fetching #%d ...", self.user, self.channel_id)
            channel = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
            msgs: list[discord.Message] = []
            async for m in channel.history(limit=100000, oldest_first=True):
                msgs.append(m)
            log.info("Fetched %d messages", len(msgs))

            bot_id = self.user.id
            for i, m in enumerate(msgs):
                if m.author.id != self.haroin_id:
                    continue
                for j in range(i + 1, min(i + 5, len(msgs))):
                    c = msgs[j]
                    if c.author.bot and c.author.id == bot_id:
                        self.pairs.append(TurnPair(
                            senpai_text=m.content or "",
                            kanae_text=c.content or "",
                            senpai_ts=m.created_at.isoformat(),
                            kanae_ts=c.created_at.isoformat(),
                        ))
                        break
            log.info("Built %d (senpai, kanae) pairs", len(self.pairs))
        except Exception as exc:
            log.error("fetch failed: %s", exc, exc_info=True)
        finally:
            await self.close()


async def fetch_chat_pairs() -> list[TurnPair]:
    """Spin up a one-shot Discord client to grab the channel history."""
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")
    client = HistoryClient(CHANNEL_ID, int(HAROIN_ID))
    await client.start(token)
    return client.pairs


async def _embed_texts(texts: list[str], batch_size: int = 100) -> np.ndarray:
    """OpenAI text-embedding-3-small in batches → unit-normalized matrix."""
    from openai import AsyncOpenAI
    cli = AsyncOpenAI()
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        chunk = [(t or " ")[:2000] for t in texts[i:i + batch_size]]
        resp = await cli.embeddings.create(
            model="text-embedding-3-small",
            input=chunk,
        )
        out.extend([d.embedding for d in resp.data])
    arr = np.asarray(out, dtype=np.float32)
    # Defensive normalize (OpenAI usually returns unit vectors but anyway)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


async def link_facts_to_pairs(
    pairs: list[TurnPair], facts: list[dict],
) -> list[tuple[dict, TurnPair, float]]:
    """For each fact, return (fact, best_matching_pair, cosine_sim)."""
    log.info("embedding %d kanae replies + %d facts...", len(pairs), len(facts))
    reply_vec = await _embed_texts([p.kanae_text for p in pairs])
    fact_vec = await _embed_texts([f["memory"] for f in facts])
    sims = fact_vec @ reply_vec.T
    best = np.argmax(sims, axis=1)
    best_sim = sims[np.arange(len(facts)), best]
    log.info("link stats: mean_sim=%.3f median=%.3f min=%.3f",
             float(np.mean(best_sim)), float(np.median(best_sim)),
             float(np.min(best_sim)))
    return [(facts[i], pairs[int(best[i])], float(best_sim[i])) for i in range(len(facts))]


async def classify_fact(fact_text: str, pair: TurnPair) -> dict | None:
    """Sonnet 4.6 ternary classifier with context."""
    from services.mem0.cli_compat import cli_messages_create, extract_text
    from services.mem0.client import get_anthropic_client
    client = await get_anthropic_client()
    if client is None:
        return None
    try:
        senpai = pair.senpai_text[:1500].replace("{", "{{").replace("}", "}}")
        kanae = pair.kanae_text[:1500].replace("{", "{{").replace("}", "}}")
        fact = fact_text[:600].replace("{", "{{").replace("}", "}}")
        resp = await cli_messages_create(
            client,
            model="claude-sonnet-4-6",
            max_tokens=800,
            user_message=_CLASSIFY_PROMPT.format(senpai=senpai, kanae=kanae, fact=fact),
            task_prompt=(
                "You classify whether an extracted fact is genuinely about "
                "Kanae (the bot) or misattributed from senpai's preferences "
                "quoted in her response. Return JSON only."
            ),
        )
        raw = extract_text(resp).strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        if not raw:
            return None
        data = json.loads(raw)
        attr = (data.get("attribution") or "").strip().lower()
        if attr not in ("kanae", "haroin_misattributed", "ambiguous"):
            attr = "ambiguous"
        return {"attribution": attr, "reason": data.get("reason", "")[:80]}
    except Exception as exc:
        log.warning("classify failed: %s", exc)
        return None


async def audit(
    facts: list[dict], pairs: list[TurnPair], *,
    concurrency: int = 16, dry_run: bool = True,
) -> tuple[dict, list[dict]]:
    """Classify every fact and (when committing) delete misattributed ones."""
    from services.mem0 import graph
    from services.mem0.client import _get_mem0
    mem = _get_mem0()

    matches = await link_facts_to_pairs(pairs, facts)

    stats = {
        "total": len(matches), "kanae": 0, "haroin_misattributed": 0,
        "ambiguous": 0, "classify_failed": 0, "deleted": 0, "delete_failed": 0,
    }
    flagged: list[dict] = []
    sem = asyncio.Semaphore(concurrency)
    progress = {"done": 0}
    progress_lock = asyncio.Lock()

    async def _one(fact: dict, pair: TurnPair, sim: float):
        async with sem:
            result = await classify_fact(fact["memory"], pair)
            if result is None:
                stats["classify_failed"] += 1
            else:
                attr = result["attribution"]
                stats[attr] += 1
                if attr == "haroin_misattributed":
                    rec = {
                        "id": fact["id"], "memory": fact["memory"],
                        "reason": result["reason"], "sim": sim,
                        "senpai": pair.senpai_text[:300],
                        "kanae": pair.kanae_text[:300],
                        "senpai_ts": pair.senpai_ts,
                    }
                    flagged.append(rec)
                    if not dry_run:
                        try:
                            await asyncio.to_thread(
                                graph.delete_fact_edges, fact["id"],
                            )
                            await asyncio.to_thread(mem.delete, fact["id"])
                            stats["deleted"] += 1
                        except Exception as exc:
                            stats["delete_failed"] += 1
                            log.warning("delete failed %s: %s",
                                        fact["id"][:8], exc)
            async with progress_lock:
                progress["done"] += 1
                if progress["done"] % 50 == 0 or progress["done"] == len(matches):
                    log.info(
                        "progress %d/%d | kanae=%d misattr=%d ambig=%d "
                        "deleted=%d fail=%d",
                        progress["done"], len(matches),
                        stats["kanae"], stats["haroin_misattributed"],
                        stats["ambiguous"], stats["deleted"],
                        stats["classify_failed"],
                    )

    await asyncio.gather(
        *(_one(f, p, s) for (f, p, s) in matches),
        return_exceptions=True,
    )

    return stats, flagged


def write_report(
    stats: dict, flagged: list[dict], *, output: Path, dry_run: bool,
) -> None:
    lines = ["# Kanae fact audit report", ""]
    lines.append(f"- mode: {'DRY-RUN (no deletion)' if dry_run else 'COMMIT (deletions applied)'}")
    for k, v in stats.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append(f"## Flagged as haroin_misattributed ({len(flagged)})")
    lines.append("")
    for rec in flagged:
        lines.append(f"### `{rec['id'][:8]}` — sim={rec['sim']:.2f}")
        lines.append(f"- **fact**: {rec['memory'][:300]}")
        lines.append(f"- **reason**: {rec['reason']}")
        lines.append(f"- **senpai_ts**: {rec['senpai_ts']}")
        lines.append(f"- **senpai**: {rec['senpai'][:200]}")
        lines.append(f"- **kanae**: {rec['kanae'][:200]}")
        lines.append("")
    output.write_text("\n".join(lines), encoding="utf-8")
    log.info("report → %s (%d flagged)", output, len(flagged))


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true",
                   help="actually delete haroin_misattributed facts")
    p.add_argument("--max-concurrent", type=int, default=16)
    p.add_argument("--limit", type=int, default=None,
                   help="cap how many facts to classify (debug)")
    p.add_argument("--output", type=Path,
                   default=Path("/tmp/kanae_audit.md"))
    args = p.parse_args()

    dry_run = not args.commit

    # 1. Load kanae facts
    from services.mem0.client import _get_mem0
    mem = _get_mem0()
    raw = await asyncio.to_thread(mem.get_all, user_id=KANAE_ID, limit=10000)
    facts = raw.get("results") if isinstance(raw, dict) else raw
    if args.limit:
        facts = facts[:args.limit]
    log.info("kanae facts loaded: %d (mode=%s)",
             len(facts), "DRY-RUN" if dry_run else "COMMIT")

    # 2. Fetch chat history
    pairs = await fetch_chat_pairs()
    if not pairs:
        log.error("no pairs fetched; aborting")
        return

    # 3. Link + classify (with optional delete)
    stats, flagged = await audit(
        facts, pairs,
        concurrency=args.max_concurrent, dry_run=dry_run,
    )

    # 4. Report
    log.info("=== FINAL STATS ===")
    for k, v in stats.items():
        log.info("  %s: %d", k, v)
    write_report(stats, flagged, output=args.output, dry_run=dry_run)


if __name__ == "__main__":
    asyncio.run(main())
