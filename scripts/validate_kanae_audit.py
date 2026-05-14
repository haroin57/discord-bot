#!/usr/bin/env python3
"""Cross-validate the pass-1 audit by re-classifying flagged facts with a
stricter prompt. Delete only entries flagged misattributed in BOTH passes.

Input: pass-1 markdown report at /tmp/kanae_audit.md (output of
scripts/audit_kanae_facts.py — the `### \\`XXXXXXXX\\` — sim=...` headers
provide the fact IDs to revisit).

Output: /tmp/kanae_validate.md with per-fact verdict + a high-confidence
set (pass1 ∩ pass2). With --commit, deletes the high-confidence subset
from kanae's Mem0 namespace and clears graph edges.

Usage:
    python scripts/validate_kanae_audit.py             # dry-run, report only
    python scripts/validate_kanae_audit.py --commit    # actually delete
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / ".env")

import numpy as np  # noqa: E402

from scripts.audit_kanae_facts import (  # noqa: E402
    KANAE_ID,
    HAROIN_ID,
    CHANNEL_ID,
    TurnPair,
    _embed_texts,
    fetch_chat_pairs,
    link_facts_to_pairs,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("validate_kanae")


# Pass-2 prompt: stricter than pass-1. We require *unambiguous* evidence
# that the fact's subject is senpai, not kanae. Borderline cases default
# to "keep" rather than "misattributed".
_STRICT_PROMPT = """以下は Discord での 1 ターンと、そこから抽出されてかなえ namespace に保存された fact です。
pass-1 の audit でこの fact は「先輩の事実が誤ってかなえに割り当てられた」と判定されました。
本当に削除して良いか、厳しめの基準で **再判定** してください。

<senpai>
{senpai}
</senpai>

<kanae>
{kanae}
</kanae>

<extracted_fact_stored_under_kanae>
{fact}
</extracted_fact_stored_under_kanae>

== 厳しい判定基準 ==

"confirm_delete" を選ぶには **以下を全て満たす必要がある**:
  1. fact の主語が明らかに先輩であり、かなえに当てはまる解釈が不可能
  2. <senpai> の発話に同等の主張・嗜好が直接含まれている、または fact が会話に存在しない捏造
  3. かなえの「提案した」「述べた」「判断した」「説明できる」のような行動・能力・知識を含む文型ではない

逆に、以下のいずれかに該当するなら **"keep"** (削除しない):
  - fact 末尾に「提案した」「判断した」「述べた」「分析している」「指摘した」等、かなえの行動・発話を表す動詞がある
  - fact が「○○というスタイルを持つ」「○○できる」「○○を理解している」等、かなえの能力・性格・知識を述べている
  - 文脈から「かなえが先輩について語っている」「かなえの内省」とも解釈できる
  - 微妙でどちらとも言えない (ambiguous は "keep")

JSON のみ返してください:
{{"verdict":"<confirm_delete|keep>","reason":"<40文字以内>"}}"""


def parse_audit_report(path: Path) -> list[dict]:
    """Pull (id, fact_text, reason, senpai, kanae) from pass-1 markdown."""
    txt = path.read_text(encoding="utf-8")
    entries: list[dict] = []
    # Split on "### `xxx`" headers
    chunks = re.split(r"\n### `([0-9a-f]{8})` — sim=([\d.]+)\n", txt)
    # chunks: [preamble, id1, sim1, body1, id2, sim2, body2, ...]
    for i in range(1, len(chunks), 3):
        if i + 2 >= len(chunks):
            break
        fid_short = chunks[i].strip()
        sim = float(chunks[i + 1])
        body = chunks[i + 2]

        def grab(label: str) -> str:
            m = re.search(
                rf"- \*\*{re.escape(label)}\*\*: (.+?)(?=\n- \*\*|\n###|\Z)",
                body, re.DOTALL,
            )
            return (m.group(1).strip() if m else "")

        entries.append({
            "id_short": fid_short,
            "sim": sim,
            "fact": grab("fact"),
            "reason": grab("reason"),
            "senpai_ts": grab("senpai_ts"),
            "senpai": grab("senpai"),
            "kanae": grab("kanae"),
        })
    return entries


async def _classify_strict(fact_text: str, senpai_text: str, kanae_text: str) -> dict | None:
    from services.mem0.cli_compat import cli_messages_create, extract_text
    from services.mem0.client import get_anthropic_client
    client = await get_anthropic_client()
    if client is None:
        return None
    try:
        s = senpai_text[:1500].replace("{", "{{").replace("}", "}}")
        k = kanae_text[:1500].replace("{", "{{").replace("}", "}}")
        f = fact_text[:600].replace("{", "{{").replace("}", "}}")
        resp = await cli_messages_create(
            client,
            model="claude-sonnet-4-6",
            max_tokens=600,
            user_message=_STRICT_PROMPT.format(senpai=s, kanae=k, fact=f),
            task_prompt=(
                "You are the strict 2nd-pass auditor. Default to 'keep' "
                "unless evidence is unambiguous. Return JSON only."
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
        verdict = (data.get("verdict") or "").strip().lower()
        if verdict not in ("confirm_delete", "keep"):
            verdict = "keep"
        return {"verdict": verdict, "reason": data.get("reason", "")[:80]}
    except Exception as exc:
        log.warning("strict classify failed: %s", exc)
        return None


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true",
                   help="actually delete confirmed entries from kanae namespace")
    p.add_argument("--audit-report", type=Path,
                   default=Path("/tmp/kanae_audit.md"))
    p.add_argument("--output", type=Path,
                   default=Path("/tmp/kanae_validate.md"))
    p.add_argument("--max-concurrent", type=int, default=16)
    args = p.parse_args()

    dry_run = not args.commit

    # 1. Parse pass-1 misattributed entries
    pass1 = parse_audit_report(args.audit_report)
    log.info("pass-1 misattr loaded: %d", len(pass1))
    if not pass1:
        log.error("no misattr entries found in %s", args.audit_report)
        return

    # 2. Need the full fact list (to resolve short id → full id) and full
    #    context (senpai/kanae text in the markdown is truncated to 200).
    #    Refetch from Mem0 + Discord to get the full strings for pass-2.
    from services.mem0.client import _get_mem0
    mem = _get_mem0()
    raw = await asyncio.to_thread(mem.get_all, user_id=KANAE_ID, limit=10000)
    all_facts = raw.get("results") if isinstance(raw, dict) else raw

    # Build id_short → full fact map
    short_to_full: dict[str, dict] = {}
    for f in all_facts:
        fid = f.get("id") or ""
        if fid:
            short_to_full[fid[:8]] = f

    # Filter to the pass-1 misattr we can resolve
    target_facts: list[dict] = []
    target_short_ids: list[str] = []
    for e in pass1:
        fid_short = e["id_short"]
        if fid_short in short_to_full:
            target_facts.append(short_to_full[fid_short])
            target_short_ids.append(fid_short)
    log.info("resolved %d/%d facts from Mem0", len(target_facts), len(pass1))
    if not target_facts:
        log.error("no resolvable facts; abort")
        return

    # 3. Re-fetch chat history + re-link the target subset
    pairs = await fetch_chat_pairs()
    log.info("fetched %d turn pairs", len(pairs))
    matches = await link_facts_to_pairs(pairs, target_facts)

    # 4. Strict re-classify
    log.info("strict re-classifying %d candidates...", len(matches))
    sem = asyncio.Semaphore(args.max_concurrent)
    results: list[dict] = []
    progress = {"done": 0}
    progress_lock = asyncio.Lock()

    async def _one(fact, pair, sim):
        async with sem:
            verdict = await _classify_strict(
                fact["memory"], pair.senpai_text, pair.kanae_text,
            )
            res = {
                "id": fact["id"], "id_short": fact["id"][:8],
                "fact": fact["memory"], "sim": sim,
                "pass1_reason": next(
                    (e["reason"] for e in pass1 if e["id_short"] == fact["id"][:8]),
                    "",
                ),
                "verdict": (verdict or {}).get("verdict", "classify_failed"),
                "strict_reason": (verdict or {}).get("reason", ""),
                "senpai": pair.senpai_text[:300],
                "kanae": pair.kanae_text[:300],
                "senpai_ts": pair.senpai_ts,
            }
            results.append(res)
            async with progress_lock:
                progress["done"] += 1
                if progress["done"] % 10 == 0 or progress["done"] == len(matches):
                    confirms = sum(1 for r in results if r["verdict"] == "confirm_delete")
                    keeps = sum(1 for r in results if r["verdict"] == "keep")
                    log.info(
                        "progress %d/%d | confirm=%d keep=%d",
                        progress["done"], len(matches), confirms, keeps,
                    )

    await asyncio.gather(
        *(_one(f, p, s) for (f, p, s) in matches),
        return_exceptions=True,
    )

    # 5. Stats + commit
    confirms = [r for r in results if r["verdict"] == "confirm_delete"]
    keeps = [r for r in results if r["verdict"] == "keep"]
    fails = [r for r in results if r["verdict"] == "classify_failed"]

    log.info("=== validation result ===")
    log.info("  pass1_misattr: %d", len(pass1))
    log.info("  pass2_confirm_delete: %d", len(confirms))
    log.info("  pass2_keep: %d", len(keeps))
    log.info("  pass2_failed: %d", len(fails))

    deleted = 0
    delete_failed = 0
    if not dry_run and confirms:
        from services.mem0 import graph
        log.info("COMMIT: deleting %d high-confidence misattr facts...", len(confirms))
        for r in confirms:
            try:
                await asyncio.to_thread(graph.delete_fact_edges, r["id"])
                await asyncio.to_thread(mem.delete, r["id"])
                deleted += 1
            except Exception as exc:
                delete_failed += 1
                log.warning("delete %s failed: %s", r["id_short"], exc)
        log.info("deleted=%d delete_failed=%d", deleted, delete_failed)

    # 6. Report
    lines = ["# Kanae validation report (pass-2 strict)", ""]
    lines.append(f"- mode: {'DRY-RUN' if dry_run else 'COMMIT'}")
    lines.append(f"- pass1_misattr_total: {len(pass1)}")
    lines.append(f"- pass2_confirm_delete: {len(confirms)}")
    lines.append(f"- pass2_keep: {len(keeps)}")
    lines.append(f"- pass2_failed: {len(fails)}")
    if not dry_run:
        lines.append(f"- deleted: {deleted}")
        lines.append(f"- delete_failed: {delete_failed}")
    lines.append("")
    lines.append(f"## confirm_delete ({len(confirms)})")
    lines.append("")
    for r in confirms:
        lines.append(f"### `{r['id_short']}`")
        lines.append(f"- **fact**: {r['fact'][:300]}")
        lines.append(f"- **pass1**: {r['pass1_reason']}")
        lines.append(f"- **pass2 (strict)**: {r['strict_reason']}")
        lines.append(f"- **senpai**: {r['senpai'][:200]}")
        lines.append(f"- **kanae**: {r['kanae'][:200]}")
        lines.append("")
    lines.append(f"## keep ({len(keeps)}) — pass-2 rescued these from deletion")
    lines.append("")
    for r in keeps:
        lines.append(f"### `{r['id_short']}`")
        lines.append(f"- **fact**: {r['fact'][:300]}")
        lines.append(f"- **pass1**: {r['pass1_reason']}")
        lines.append(f"- **pass2 (strict)**: {r['strict_reason']}")
        lines.append("")
    args.output.write_text("\n".join(lines), encoding="utf-8")
    log.info("report → %s", args.output)


if __name__ == "__main__":
    asyncio.run(main())
