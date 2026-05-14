#!/usr/bin/env python3
"""Mem0 quality cleanup: classify every fact's value_class and prune noise.

For each user_id, fetch all facts, ask Sonnet (via CLI-compat shim so we
keep using the OAuth subscription / overage path) which of six classes the
fact belongs to:

    noise       — meaningless fragment, bot reactions only      → DELETE
    transient   — short-lived state (e.g. "RAM 2.2GB right now") → DELETE
    preference  — long-lived taste / affinity
    decision    — design rule / commitment
    knowledge   — technical fact
    episodic    — a specific past event worth remembering

For non-deleted facts we annotate metadata with `value_class` and `value_confidence`
so downstream search / rerank can boost or de-prioritize on this signal.

Usage:
    .venv/bin/python scripts/mem0_value_cleanup.py --dry-run
    .venv/bin/python scripts/mem0_value_cleanup.py --user-id 573489206240870410
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

# Allow `python scripts/...` from repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

# Load .env before importing config / mem0 client.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mem0_cleanup")


VALUE_CLASSES = {"noise", "transient", "preference", "decision", "knowledge", "episodic"}
DELETE_CLASSES = {"noise", "transient"}

# Confidence threshold below which we keep the fact regardless of class
# (defensive: avoid deleting on low-confidence "noise" guesses).
KEEP_CONFIDENCE_FLOOR = 0.70


_CLASSIFY_PROMPT = """以下の Mem0 fact を 6 種類のいずれかに分類してください。

fact: <untrusted_fact>{text}</untrusted_fact>

クラス定義:
- noise: 意味の薄い断片、bot 操作確認、リアクションのみ
  例: "Restarted/Rebooted something", "OK", "了解しました"
- transient: 「いま」「今日」「今月」など短期間でしか valid じゃない状態
  例: "RAM 2.2GB 使用中", "今日 15:30 から会議", "PID 1099508 で ingest 実行中"
- preference: 嗜好・好み (長期間 valid)
  例: "Rust が好き", "ビフォアシリーズが好き"
- decision: 設計判断・ルール (長期間 valid)
  例: "dev に直接 push 禁止", "git --no-verify は使わない"
- knowledge: 技術知識・事実 (長期間 valid)
  例: "Mem0 v1.1 で graph 機能廃止"
- episodic: 出来事・経験 (時系列価値あり、長期保存対象)
  例: "2026-04-15 にユニットテストでバグ発見した"

JSON のみで返してください (説明・コードブロック不要):
{{"class": "noise|transient|preference|decision|knowledge|episodic", "confidence": 0.0-1.0, "reason": "<30 文字以内>"}}"""


async def classify_fact(text: str) -> dict:
    """Return {'class': str, 'confidence': float, 'reason': str}.

    On any failure, fall back to {class: 'knowledge', confidence: 0.0} so the
    caller keeps the fact (fail-open, never delete on ambiguity).
    """
    try:
        from services.mem0.cli_compat import cli_messages_create
        from services.mem0.client import get_anthropic_client
        client = await get_anthropic_client()
        if client is None:
            return {"class": "knowledge", "confidence": 0.0, "reason": "no_client"}
        safe_text = text[:600].replace("{", "{{").replace("}", "}}")
        resp = await cli_messages_create(
            client,
            model="claude-sonnet-4-6",
            max_tokens=1500,
            user_message=_CLASSIFY_PROMPT.format(text=safe_text),
            task_prompt=(
                "You classify Mem0 memory facts into one of six value classes. "
                "Return JSON only."
            ),
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        cls = str(data.get("class", "knowledge")).lower()
        if cls not in VALUE_CLASSES:
            cls = "knowledge"
        conf = float(data.get("confidence", 0.5))
        reason = str(data.get("reason", ""))[:80]
        return {"class": cls, "confidence": conf, "reason": reason}
    except Exception as exc:
        log.warning("classify failed (fail-open): %s", exc)
        return {"class": "knowledge", "confidence": 0.0, "reason": f"error:{type(exc).__name__}"}


async def process_user(
    user_id: str, *, dry_run: bool, max_concurrent: int = 4, delay: float = 1.0,
) -> dict:
    """Fetch all facts for one user, classify, optionally delete."""
    from services.mem0.client import _get_mem0
    mem = _get_mem0()
    if mem is None:
        log.error("Mem0 not initialized")
        return {}

    log.info("fetching memories for user=%s", user_id[-4:])
    # Mem0 v1.1 get_all default limit is 100; pass an explicit high value to
    # fetch the whole user namespace (we expect ~1400 for senpai).
    raw = await asyncio.to_thread(mem.get_all, user_id=user_id, limit=10000)
    facts = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
    log.info("loaded %d facts for user=...%s", len(facts), user_id[-4:])

    stats: dict[str, int] = {c: 0 for c in VALUE_CLASSES}
    stats["deleted"] = 0
    stats["kept"] = 0
    stats["errors"] = 0
    samples: dict[str, list[tuple[str, float, str]]] = {c: [] for c in VALUE_CLASSES}

    sem = asyncio.Semaphore(max_concurrent)

    async def _one(f: dict) -> None:
        async with sem:
            text = f.get("memory") or ""
            mem_id = f.get("id") or ""
            if not text or not mem_id:
                stats["errors"] += 1
                return
            res = await classify_fact(text)
            cls = res["class"]
            conf = res["confidence"]
            stats[cls] += 1
            if len(samples[cls]) < 8:
                samples[cls].append((text[:80], conf, res["reason"]))

            should_delete = cls in DELETE_CLASSES and conf >= KEEP_CONFIDENCE_FLOOR
            if should_delete:
                if dry_run:
                    log.info(
                        "DRY-RUN would delete [%s conf=%.2f] %s | %s",
                        cls, conf, text[:60], res["reason"],
                    )
                    stats["deleted"] += 1
                else:
                    try:
                        await asyncio.to_thread(mem.delete, mem_id)
                        stats["deleted"] += 1
                    except Exception as exc:
                        log.warning("delete failed %s: %s", mem_id, exc)
                        stats["errors"] += 1
            else:
                # Annotate metadata with value_class for downstream rerank.
                # Mem0 update() partially merges metadata, so only the two
                # fields we want to set are passed. ``data`` is required by
                # the SDK so we re-send the existing text unchanged.
                stats["kept"] += 1
                if not dry_run:
                    try:
                        partial_meta = {
                            "value_class": cls,
                            "value_confidence": round(conf, 3),
                        }
                        await asyncio.to_thread(
                            mem.update,
                            memory_id=mem_id,
                            data=text,
                            metadata=partial_meta,
                        )
                    except Exception as exc:
                        log.warning("update metadata failed %s: %s", mem_id, exc)
                        stats["errors"] += 1
            await asyncio.sleep(delay)

    # Schedule all but bound concurrency by semaphore.
    await asyncio.gather(*(_one(f) for f in facts), return_exceptions=True)

    log.info("=== samples (top 8 per class) ===")
    for c, items in samples.items():
        log.info("[%s] %d items", c, stats[c])
        for txt, conf, reason in items:
            log.info("  conf=%.2f %s | %s", conf, txt, reason)

    log.info(
        "=== stats user=...%s — deleted=%d, kept=%d, errors=%d, "
        "noise=%d transient=%d preference=%d decision=%d knowledge=%d episodic=%d",
        user_id[-4:], stats["deleted"], stats["kept"], stats["errors"],
        stats["noise"], stats["transient"], stats["preference"],
        stats["decision"], stats["knowledge"], stats["episodic"],
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

    grand: dict[str, int] = {c: 0 for c in VALUE_CLASSES}
    grand.update({"deleted": 0, "kept": 0, "errors": 0})
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


if __name__ == "__main__":
    asyncio.run(main())
