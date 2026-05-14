#!/usr/bin/env python3
"""Opus-based entity + genre backfill across all Mem0 facts, with X (Twitter)
verification.

For every fact (per user_id):
  1. x-scraper search the fact text on X — grounds Opus on the actual identity
     of proper nouns it may not know from training cutoff (TAKU INOUE, niche
     manga, recent VTubers, etc.). X is faster than Brave (no per-IP rate
     limit) and Japanese cultural discussion is richer there.
  2. Ask Opus 4.7 with the fact + X context to (a) enumerate up to 5 entities
     with proper type and aliases and (b) classify the fact's *genre*
     (anime / manga / music / ...)
  3. Upsert each entity in services.mem0.graph (idempotent on name+user_id)
  4. Add a fact_entity edge (idempotent)
  5. mem.update() the fact's metadata to add the `genre` field

X failures (cookies expired / network) are non-fatal — fact still gets
processed, just without web grounding.

Usage:
    python scripts/backfill_entities_opus.py --dry-run --limit 20
    python scripts/backfill_entities_opus.py
    python scripts/backfill_entities_opus.py --no-web --user-id 573489206240870410
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
# x-scraper lives outside the discord-bot repo; add its src dir so we can
# import the XTrendsScraper directly instead of going through MCP stdio.
_XSCRAPER_SRC = Path("/home/ubuntu/free-code/x-scraper/src")
if _XSCRAPER_SRC.exists():
    sys.path.insert(0, str(_XSCRAPER_SRC))
_XSCRAPER_COOKIE_DIR = Path("/home/ubuntu/free-code/x-scraper/cookies")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("backfill_entities_opus")


GENRES = [
    "anime", "manga", "novel", "game", "music", "film",
    "literature", "philosophy", "art",
    "tech", "science",
    "personal", "invest", "ops", "meta", "other",
]
GENRES_STR = " | ".join(GENRES)

VALID_TYPES = {"person", "project", "tech", "media", "place", "concept"}


_PROMPT = """以下の fact を分析してください。

fact: <untrusted_fact>{text}</untrusted_fact>

# 補足: X (Twitter) 検索結果 (fact をクエリして取れたツイート上位)

このコンテクストは固有名詞の identity を裏取りするためのもの。Opus の訓練データに無い
新しい / マイナーな名前 (アーティスト・作品・人物・サービス) でも、ツイートから
正体が分かれば fact 化に活用してください。検索結果が空でも、fact 内の固有名詞は
そのまま entity 化する。

<x_context>
{web_context}
</x_context>

# Task 1: Entity extraction (0〜5 個)

- name は canonical 表記
  - 日本語/英語/略称の表記揺れがある場合、より一般的な表記を canonical に
  - 例: "CLANNAD" を canonical、aliases=["クラナド"]
  - 例: "Rust" を canonical、aliases=["ラスト"]
  - 例: "葬送のフリーレン" を canonical、aliases=["フリーレン"]
- type は person / project / tech / media / place / concept のいずれか
  - person: 人物 (作家・監督・俳優・ミュージシャン・開発者・思想家)
  - project: 開発プロジェクト・サービス・プロダクト
  - tech: 技術・言語・フレームワーク・ライブラリ
  - media: 作品 (漫画・小説・映画・アニメ・ゲーム・楽曲・アルバム)・書籍タイトル
  - place: 場所
  - concept: それ以外の固有概念
- aliases に表記揺れ 0〜3 個 (省略可)
- 抽象的な普通名詞 (例: "プログラミング", "音楽", "アニメ", "漫画") は entity にしない
- 該当なし → "entities": []

特に拾い漏れに注意:
- 「本棚にある漫画: タイトル」「○○の楽曲」「○○の本」 → タイトルや作者を media/person で抽出
- カタカナ・英大文字混じり (Midnight Grand Orchestra, TAKU INOUE 等) は基本固有名詞扱い
- 「沙耶の唄」「葬送のフリーレン」等の漢字混じり作品名も media

# Task 2: Genre classification

fact が言及している内容を以下から **1 つ** 選んでください:
{genres}

- anime: TV アニメ・劇場アニメ
- manga: 漫画 (本棚にある漫画も含む)
- novel: 小説・ライトノベル
- game: 家庭用ゲーム・ノベルゲーム・VN
- music: 楽曲・アーティスト・アルバム・ライブ
- film: 実写映画・ドラマ
- literature: ノンフィクション・実学書・哲学書 (一般向け)
- philosophy: 哲学・思想 (アカデミック寄り)
- art: 美術・写真・デザイン
- tech: 技術・プログラミング・LLM
- science: 物理・数学・科学
- personal: 個人・関係性・健康・進路
- invest: 投資・経済
- ops: 開発環境・運用・インフラ
- meta: LLM 対話メタ・スタイル
- other: 上記いずれでもない

# 出力

JSON のみ返してください (説明・前置き・コードフェンス不要):
{{"entities":[{{"name":"<canonical>","type":"<one of 6>","aliases":["<alias>"]}}],"genre":"<one of 16>"}}"""


async def web_search_for_fact(text: str, n: int = 3) -> str:
    """X (Twitter) search for the fact text via x-scraper. Returns formatted hits.

    Each tweet contributes a title line (already prefixed with [keyword] by
    the scraper) plus its summary if present. Empty string on cookie expiry
    / network error so the caller proceeds without web grounding.
    """
    if not _XSCRAPER_SRC.exists():
        return ""
    query = text[:100]
    try:
        from news_curator.config import Config
        from news_curator.scrapers.x_trends import XTrendsScraper
        scraper = XTrendsScraper(Config(cookie_dir=_XSCRAPER_COOKIE_DIR))
        items = await scraper.scrape(keywords=[query], days=30, limit=n)
        if not items:
            return ""
        lines = []
        for it in items[:n]:
            title = (getattr(it, "title", "") or "").strip()[:160]
            summary = (getattr(it, "summary", "") or "").strip()[:280]
            if title:
                lines.append(f"- {title}")
            if summary:
                lines.append(f"  {summary}")
        return "\n".join(lines)
    except Exception as exc:
        log.warning("x-scraper search failed (fail-open): %s", exc)
        return ""


async def analyze_fact(text: str, *, use_web: bool = True) -> dict:
    """Brave verify + Opus extract; returns ``{entities, genre, web_ctx}``."""
    if not text:
        return {"entities": [], "genre": "other", "web_ctx": ""}
    web_ctx = ""
    if use_web:
        web_ctx = await web_search_for_fact(text)
    try:
        from services.mem0.cli_compat import cli_messages_create, extract_text
        from services.mem0.client import get_anthropic_client
        client = await get_anthropic_client()
        if client is None:
            return {"entities": [], "genre": "other", "web_ctx": web_ctx}
        safe = text[:800].replace("{", "{{").replace("}", "}}")
        safe_web = (web_ctx or "(none)")[:1500].replace("{", "{{").replace("}", "}}")
        resp = await cli_messages_create(
            client,
            model="claude-opus-4-7",
            max_tokens=2500,
            user_message=_PROMPT.format(
                text=safe, web_context=safe_web, genres=GENRES_STR,
            ),
            task_prompt=(
                "You extract named entities and identify the genre of "
                "Mem0 fact statements, grounding on Brave search context. "
                "Return JSON only."
            ),
        )
        raw = extract_text(resp).strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        if not raw:
            log.warning("empty Opus response for fact=%r", text[:60])
            return {"entities": [], "genre": "other", "web_ctx": web_ctx}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("JSON parse failed (%s); tail=%r", exc, raw[-200:])
            return {"entities": [], "genre": "other", "web_ctx": web_ctx}

        entities = []
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
            entities.append({"name": name, "type": etype, "aliases": aliases})
        genre = (data.get("genre") or "other").strip().lower()
        if genre not in GENRES:
            genre = "other"
        return {"entities": entities[:5], "genre": genre, "web_ctx": web_ctx}
    except Exception as exc:
        log.warning("analyze_fact failed (fail-open): %s", exc)
        return {"entities": [], "genre": "other", "web_ctx": web_ctx}


async def process_user(
    user_id: str, *,
    dry_run: bool,
    max_concurrent: int = 4,
    delay: float = 0.5,
    limit: int | None = None,
    use_web: bool = True,
) -> dict:
    from services.mem0 import graph
    from services.mem0.client import _get_mem0
    mem = _get_mem0()
    if mem is None:
        log.error("Mem0 not initialized")
        return {}

    log.info("fetching memories for user=...%s", user_id[-4:])
    raw = await asyncio.to_thread(mem.get_all, user_id=user_id, limit=20000)
    facts = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
    if limit:
        facts = facts[:limit]
    log.info("loaded %d facts for user=...%s", len(facts), user_id[-4:])

    stats = {
        "facts": len(facts),
        "entities_upserted": 0,
        "edges_added": 0,
        "genre_set": 0,
        "skipped": 0,
        "errors": 0,
        "genre_dist": {g: 0 for g in GENRES},
    }
    sem = asyncio.Semaphore(max_concurrent)

    async def _one(f: dict) -> None:
        async with sem:
            text = (f.get("memory") or "").strip()
            fid = f.get("id") or ""
            if not text or not fid:
                stats["skipped"] += 1
                return
            result = await analyze_fact(text, use_web=use_web)
            entities = result["entities"]
            genre = result["genre"]
            web_hit = bool(result.get("web_ctx"))
            stats["genre_dist"][genre] += 1
            if web_hit:
                stats["web_hits"] = stats.get("web_hits", 0) + 1

            if dry_run:
                ent_str = ", ".join(
                    f"{e['name']}({e['type']})" for e in entities
                ) or "(none)"
                log.info(
                    "DRY-RUN [%s] web=%s genre=%s ent=%s | %s",
                    fid[:8], "y" if web_hit else "n", genre, ent_str, text[:60],
                )
                stats["entities_upserted"] += len(entities)
                stats["edges_added"] += len(entities)
                stats["genre_set"] += 1
                await asyncio.sleep(delay)
                return

            # Upsert entities + edges
            for ent in entities:
                try:
                    eid = await asyncio.to_thread(
                        graph.upsert_entity,
                        ent["name"], ent["type"], user_id,
                        aliases=ent.get("aliases", []),
                    )
                    if eid:
                        ok = await asyncio.to_thread(graph.add_edge, fid, eid)
                        stats["entities_upserted"] += 1
                        if ok:
                            stats["edges_added"] += 1
                except Exception as exc:
                    log.warning(
                        "graph upsert failed (fid=%s, ent=%s): %s",
                        fid[:8], ent.get("name"), exc,
                    )
                    stats["errors"] += 1

            # Update fact metadata with genre. mem.update() requires `data`,
            # so we re-pass the existing text — that triggers a re-embed but
            # the cost is small per fact (~$0.000002).
            try:
                existing_meta = f.get("metadata") or {}
                new_meta = dict(existing_meta)
                new_meta["genre"] = genre
                await asyncio.to_thread(
                    mem.update, fid, data=text, metadata=new_meta,
                )
                stats["genre_set"] += 1
            except Exception as exc:
                log.warning(
                    "metadata genre update failed (fid=%s): %s", fid[:8], exc,
                )
                stats["errors"] += 1

            await asyncio.sleep(delay)

    await asyncio.gather(*(_one(f) for f in facts), return_exceptions=True)
    log.info(
        "=== stats user=...%s facts=%d entities=%d edges=%d genre_set=%d "
        "errors=%d skipped=%d",
        user_id[-4:], stats["facts"], stats["entities_upserted"],
        stats["edges_added"], stats["genre_set"],
        stats["errors"], stats["skipped"],
    )
    nonzero = {k: v for k, v in stats["genre_dist"].items() if v}
    log.info("genre distribution: %s", nonzero)
    return stats


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--user-id", action="append", help="repeat for multiple")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-concurrent", type=int, default=6)
    p.add_argument("--delay", type=float, default=0.0)
    p.add_argument("--limit", type=int, default=None, help="cap facts processed (debug)")
    p.add_argument("--no-web", action="store_true", help="skip Brave web verification")
    args = p.parse_args()

    uids = args.user_id or [
        "573489206240870410",
        "1037285234200297522",
        "826096545514127400",
    ]
    log.info("targets: %s (dry_run=%s)", uids, args.dry_run)

    grand = {
        "facts": 0, "entities_upserted": 0, "edges_added": 0,
        "genre_set": 0, "errors": 0, "skipped": 0,
    }
    for uid in uids:
        s = await process_user(
            uid, dry_run=args.dry_run,
            max_concurrent=args.max_concurrent, delay=args.delay,
            limit=args.limit, use_web=not args.no_web,
        )
        for k, v in s.items():
            if isinstance(v, int):
                grand[k] = grand.get(k, 0) + v

    log.info("=== GRAND TOTAL ===")
    for k, v in grand.items():
        log.info("  %s = %d", k, v)

    if not args.dry_run:
        from services.mem0 import graph
        log.info("graph stats now: %s", graph.stats())


if __name__ == "__main__":
    asyncio.run(main())
