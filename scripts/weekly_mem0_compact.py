"""Weekly Mem0 housekeeping batch (P4 + P5 monitoring).

Run from cron once a week (suggested: Sun 09:00 JST). Produces a markdown
report under /tmp/mem0_compact_<date>.md and exits with non-zero status
if regression conditions are detected (alert hook).

Tasks performed
---------------
1. Pull every memory for 先輩(DEFAULT_USER_ID).
2. List memories added in the last 7 days that are missing taxonomy metadata.
3. List dedup_group_ids whose size grew significantly during the week.
4. List tier-3/importance<=2 memories older than 60 days (archive candidates).
5. (P5) Gate version distribution (v2/v1/missing) for ingestion quality observation.
6. (P5) Other-speaker contamination regression check — non-zero exit if hits.
7. (P5) Intra-week near-duplicate clusters (SequenceMatcher-based).

Exit codes:
  0  = healthy
  2  = other-speaker contamination detected (alert)

Usage:
  python weekly_mem0_compact.py [--user-id <id>] [--out /tmp/...]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

sys.path.insert(0, "/home/ubuntu/discord-bot")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/discord-bot/.env")

from services.mem0_service import _get_mem0, _refresh_mem0_client_token  # noqa: E402
from services.oauth_manager import ensure_valid_token  # noqa: E402

DEFAULT_USER_ID = "573489206240870410"

# Pre-compiled at module load (was re-compiled inside main() on every run).
# Detects other-speaker subjects in stored fact text — used by the P5
# contamination regression check. False positives possible on quotation
# contexts, so the alert threshold (>=2 hits) absorbs that noise.
OTHER_SPEAKER_PATTERN = re.compile(
    r"(お父さん|watabegg|b0nchichi|お母さん|お父様|父親)"
    r".{0,15}(言って|思って|考えて|話して|発言|主張|意見|希望|要望)"
)

# Hard cap on the O(n²) intra-week near-dup pair check. At N=200 the loop
# does ~20k SequenceMatcher comparisons (≈0.2 s at ~10 μs/cmp on 200-char
# strings); above that the cost grows faster than weekly cadence permits.
# If new_in_week exceeds this, the loop is skipped and the report logs
# the cap so we know to revisit with a MinHash / TF-IDF pre-filter.
MAX_NEAR_DUP_N = 200


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def main() -> None:
    # Default report location: XDG state dir, mode 700, so the report
    # (which contains memory text excerpts) is not world-readable in /tmp.
    state_dir = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    state_dir = os.path.join(state_dir, "discord-bot")
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    # makedirs mode only applies on create; chmod enforces 0o700 even when the
    # directory pre-existed with looser permissions (e.g. 0o755 from a prior run).
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass
    default_out = os.path.join(state_dir, f"mem0_compact_{datetime.now().strftime('%Y%m%d')}.md")

    p = argparse.ArgumentParser()
    p.add_argument("--user-id", default=DEFAULT_USER_ID)
    p.add_argument("--out", default=default_out)
    p.add_argument("--window-days", type=int, default=7)
    p.add_argument("--cold-days", type=int, default=60)
    args = p.parse_args()

    await ensure_valid_token()
    _refresh_mem0_client_token()
    mem = _get_mem0()
    if not mem:
        print("ERR: Mem0 init failed", file=sys.stderr)
        sys.exit(1)

    raw = mem.get_all(user_id=args.user_id, limit=10000)
    memories = raw.get("results", []) if isinstance(raw, dict) else raw
    total = len(memories)

    now = datetime.now(timezone.utc)
    window_cut = now - timedelta(days=args.window_days)
    cold_cut = now - timedelta(days=args.cold_days)

    untagged_new: list[dict] = []
    new_in_week: list[dict] = []                 # P5: for intra-week near-dup analysis
    dedup_growth: defaultdict[str, int] = defaultdict(int)
    archive_candidates: list[dict] = []

    cat_counts: Counter = Counter()
    tier_counts: Counter = Counter()
    gate_version_counts: Counter = Counter()      # P5: v2/v1/missing
    other_speaker_hits: list[dict] = []           # P5: regression check

    for m in memories:
        meta = m.get("metadata") or {}
        cat = meta.get("category")
        tier = meta.get("tier")
        importance = meta.get("importance")
        dedup_id = meta.get("dedup_group_id")
        created = _parse_dt(m.get("created_at"))

        if cat:
            cat_counts[cat] += 1
        if tier is not None:
            tier_counts[tier] += 1
        # P5: gate version + other-speaker
        gate_version_counts[meta.get("gate_version") or "missing"] += 1
        text = m.get("memory", "") or ""
        if OTHER_SPEAKER_PATTERN.search(text):
            other_speaker_hits.append(m)

        if created and created >= window_cut:
            new_in_week.append(m)
            if not cat:
                untagged_new.append(m)
            if dedup_id:
                dedup_growth[dedup_id] += 1

        if (
            tier == 3
            and (importance or 0) <= 2
            and created
            and created < cold_cut
        ):
            archive_candidates.append(m)

    growing = sorted(
        ((g, n) for g, n in dedup_growth.items() if n >= 3),
        key=lambda x: -x[1],
    )

    lines = [
        f"# Mem0 Weekly Compact Report",
        "",
        f"- generated: {now.isoformat()}",
        f"- user_id: {args.user_id}",
        f"- total memories: {total}",
        "",
        "## Category distribution",
        "",
    ]
    for cat, n in cat_counts.most_common():
        lines.append(f"- `{cat}`: {n}")
    lines += ["", "## Tier distribution", ""]
    for t in sorted(tier_counts):
        lines.append(f"- tier {t}: {tier_counts[t]}")

    lines += ["", f"## Untagged new memories (last {args.window_days}d): {len(untagged_new)}", ""]
    for m in untagged_new[:20]:
        lines.append(f"- `{m['id'][:8]}` {m.get('memory','')[:100]}")
    if len(untagged_new) > 20:
        lines.append(f"- ... +{len(untagged_new)-20} more")

    lines += ["", f"## Dedup groups growing this week (>=3 new members): {len(growing)}", ""]
    for g, n in growing:
        lines.append(f"- `{g}`: +{n}")

    lines += ["", f"## Archive candidates (tier 3, imp<=2, older than {args.cold_days}d): {len(archive_candidates)}", ""]
    for m in archive_candidates[:30]:
        lines.append(f"- `{m['id'][:8]}` {m.get('memory','')[:100]}")
    if len(archive_candidates) > 30:
        lines.append(f"- ... +{len(archive_candidates)-30} more")

    # P5: intra-week near-dup pairs (SequenceMatcher, O(n²))
    # Cap N to keep the weekly batch under ~1 s even on growth weeks. When
    # new_in_week exceeds MAX_NEAR_DUP_N, skip the check entirely and surface
    # the cap in the report so we know to revisit with a MinHash / TF-IDF
    # approach.
    near_dup_pairs: list[tuple[str, str, float]] = []
    near_dup_skipped_reason: str | None = None
    if len(new_in_week) > MAX_NEAR_DUP_N:
        near_dup_skipped_reason = (
            f"skipped: new_in_week={len(new_in_week)} > MAX_NEAR_DUP_N={MAX_NEAR_DUP_N}"
        )
    else:
        for i, a in enumerate(new_in_week):
            for b in new_in_week[i+1:]:
                ta = (a.get("memory") or "")[:200]
                tb = (b.get("memory") or "")[:200]
                if not ta or not tb:
                    continue
                sim = SequenceMatcher(None, ta, tb).ratio()
                if sim >= 0.85:
                    near_dup_pairs.append((a["id"][:8], b["id"][:8], sim))

    # P5: gate version distribution
    lines += ["", "## Gate version distribution (P5)", ""]
    for ver, n in gate_version_counts.most_common():
        lines.append(f"- `{ver}`: {n}")

    # P5: other-speaker contamination check (regression guard)
    lines += [
        "",
        f"## Other-speaker contamination (P5 regression check): {len(other_speaker_hits)}",
        "",
    ]
    if other_speaker_hits:
        lines.append("**ALERT**: P1 regression suspected — お父さん/watabegg 等が主語位置で検出された")
        for m in other_speaker_hits[:10]:
            lines.append(f"- `{m['id'][:8]}` {m.get('memory','')[:120]}")
        if len(other_speaker_hits) > 10:
            lines.append(f"- ... +{len(other_speaker_hits)-10} more")

    # P5: intra-week near-dup pairs
    lines += [
        "",
        f"## Intra-week near-duplicate pairs (sim>=0.85, P5): {len(near_dup_pairs)}",
        "",
    ]
    if near_dup_skipped_reason is not None:
        lines.append(f"- ⚠️ {near_dup_skipped_reason}")
    for a, b, sim in near_dup_pairs[:20]:
        lines.append(f"- `{a}` <-> `{b}` (sim={sim:.2f})")
    if len(near_dup_pairs) > 20:
        lines.append(f"- ... +{len(near_dup_pairs)-20} more")

    report = "\n".join(lines)
    # Report contains memory text excerpts that may include PII. Open with an
    # explicit 0o600 mode (owner read/write only) so a malicious local user
    # cannot read it via /tmp or pre-create a symlink to redirect the write.
    # O_NOFOLLOW refuses to follow any pre-existing symlink at args.out.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(args.out, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(report)

    # P5: alert criterion — 1件は引用文脈の可能性、2件以上で異常
    alert = len(other_speaker_hits) >= 2
    near_dup_summary = (
        "SKIPPED" if near_dup_skipped_reason is not None
        else str(len(near_dup_pairs))
    )
    print(
        f"wrote {args.out}  total={total} untagged_new={len(untagged_new)} "
        f"growing={len(growing)} archive_cand={len(archive_candidates)} "
        f"gate_v2={gate_version_counts.get('v2', 0)} "
        f"other_speaker={len(other_speaker_hits)} near_dup={near_dup_summary} "
        f"{'ALERT' if alert else 'ok'}"
    )
    if alert:
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
