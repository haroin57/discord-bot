#!/usr/bin/env python3
"""Merge duplicate entities in services.mem0.graph (entities + aliases + edges).

Three subcommands:

  list-candidates  Find probable duplicate groups via substring containment,
                   fuzzy ratio, and alias/canonical collisions. Writes a JSON
                   plan that an operator reviews before applying.

  apply            Read a merge plan JSON and merge each (target, sources)
                   group. Supports --dry-run via SAVEPOINT/ROLLBACK so the
                   operator can see row counts without committing.

  orphan-cleanup   Delete entities that have zero fact_entity_edges. Minimal
                   §7b implementation paired with §7a; rerun safely.

The script reuses the same sqlite file resolved by services.mem0.graph
(STATE_DIR/mem0_graph.sqlite), so it stays in lockstep with the live graph
the bot writes to. The bot does not need to be stopped — the merge runs
inside a single transaction per (target, sources) group under WAL.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("merge_entities")


# ---------------------------------------------------------------------------
# Candidate detection
# ---------------------------------------------------------------------------
FUZZY_RATIO_THRESHOLD = 0.85
MIN_NAME_LEN_FOR_FUZZY = 3  # ratio is unstable for very short strings


def _list_entities(conn, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, canonical_name, type, mention_count "
        "FROM entities WHERE user_id=? ORDER BY mention_count DESC, id ASC",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _list_aliases(conn, user_id: str) -> dict[str, list[int]]:
    """alias surface form → [entity_id, ...] for this user."""
    rows = conn.execute(
        "SELECT a.alias, a.entity_id "
        "FROM entity_aliases a JOIN entities e ON e.id=a.entity_id "
        "WHERE e.user_id=?",
        (user_id,),
    ).fetchall()
    out: dict[str, list[int]] = {}
    for r in rows:
        out.setdefault(r["alias"], []).append(r["entity_id"])
    return out


def _detect_candidates(entities: list[dict], aliases: dict[str, list[int]]) -> list[dict]:
    """Return a list of merge groups: {target_id, source_ids, reason, confidence}.

    Heuristics (precedence order, first match wins per source):
      1. alias-canonical collision: another entity's canonical_name appears
         as an alias of this one — strongest signal, confidence 0.95.
      2. substring containment: short name fully contained in a longer name,
         same type. confidence 0.90.
      3. SequenceMatcher ratio ≥ 0.85 for same-type pairs. confidence = ratio.

    The entity with the highest mention_count in each group is the target;
    ties broken by lowest id. Operators are expected to review and prune
    before --apply.
    """
    by_id = {e["id"]: e for e in entities}
    canonical_to_id = {e["canonical_name"]: e["id"] for e in entities}
    # group key → set of entity ids
    groups: dict[int, set[int]] = {}

    def _union(a: int, b: int) -> None:
        # Simple union by mention_count: stash both under the larger
        # mention_count's id; reconciliation happens at output time.
        if a == b:
            return
        for g in groups.values():
            if a in g or b in g:
                g.add(a)
                g.add(b)
                return
        groups[len(groups)] = {a, b}

    reasons: dict[tuple[int, int], tuple[str, float]] = {}

    # 1. alias-canonical collision
    for alias, ent_ids in aliases.items():
        canonical_owner = canonical_to_id.get(alias)
        if canonical_owner is None:
            continue
        for eid in ent_ids:
            if eid == canonical_owner:
                continue
            _union(canonical_owner, eid)
            key = tuple(sorted((canonical_owner, eid)))
            reasons[key] = (f"alias-canonical: {alias!r}", 0.95)

    # 2 & 3. pairwise name comparison among same-type entities. O(n^2) but
    # n is small (~500). Skip pairs already grouped via aliases.
    by_type: dict[str, list[dict]] = {}
    for e in entities:
        by_type.setdefault(e["type"], []).append(e)
    for type_ents in by_type.values():
        for i, a in enumerate(type_ents):
            for b in type_ents[i + 1:]:
                an, bn = a["canonical_name"], b["canonical_name"]
                key = tuple(sorted((a["id"], b["id"])))
                if key in reasons:
                    continue
                if len(an) >= MIN_NAME_LEN_FOR_FUZZY and len(bn) >= MIN_NAME_LEN_FOR_FUZZY:
                    if an in bn or bn in an:
                        _union(a["id"], b["id"])
                        reasons[key] = (f"substring: {an!r} ⊂ {bn!r}", 0.90)
                        continue
                    ratio = SequenceMatcher(None, an, bn).ratio()
                    if ratio >= FUZZY_RATIO_THRESHOLD:
                        _union(a["id"], b["id"])
                        reasons[key] = (
                            f"fuzzy: ratio={ratio:.2f} {an!r} ~ {bn!r}",
                            ratio,
                        )

    out: list[dict] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members_sorted = sorted(
            members,
            key=lambda eid: (-by_id[eid]["mention_count"], eid),
        )
        target = members_sorted[0]
        sources = members_sorted[1:]
        # pick the strongest reason involving target
        group_reasons = [
            reasons[key]
            for key in reasons
            if target in key and any(s in key for s in sources)
        ]
        if group_reasons:
            reason, confidence = max(group_reasons, key=lambda r: r[1])
        else:
            # fallback: union via transitive closure
            reason = "transitive"
            confidence = 0.80
        out.append({
            "target_id": target,
            "target_name": by_id[target]["canonical_name"],
            "source_ids": sources,
            "source_names": [by_id[s]["canonical_name"] for s in sources],
            "reason": reason,
            "confidence": round(confidence, 3),
        })
    out.sort(key=lambda g: (-g["confidence"], g["target_id"]))
    return out


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def _merge_one(conn, target_id: int, source_id: int) -> dict:
    """Merge ``source_id`` into ``target_id`` inside the caller's transaction.

    Returns row-count stats. Caller is responsible for COMMIT/ROLLBACK.
    """
    # 0. validate both exist
    row = conn.execute(
        "SELECT canonical_name, user_id, mention_count FROM entities WHERE id=?",
        (target_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"target entity {target_id} not found")
    target_user = row["user_id"]
    row = conn.execute(
        "SELECT canonical_name, user_id, mention_count FROM entities WHERE id=?",
        (source_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"source entity {source_id} not found")
    if row["user_id"] != target_user:
        raise ValueError(
            f"refuse cross-user merge: source.user_id={row['user_id']!r} != "
            f"target.user_id={target_user!r}"
        )
    source_mention = row["mention_count"]

    # 1. promote source canonical and aliases into target's alias table
    cur = conn.execute(
        "INSERT OR IGNORE INTO entity_aliases(entity_id, alias) "
        "SELECT ?, canonical_name FROM entities WHERE id=? "
        "  AND canonical_name != (SELECT canonical_name FROM entities WHERE id=?)",
        (target_id, source_id, target_id),
    )
    aliases_added = cur.rowcount or 0
    cur = conn.execute(
        "INSERT OR IGNORE INTO entity_aliases(entity_id, alias) "
        "SELECT ?, alias FROM entity_aliases WHERE entity_id=? "
        "  AND alias != (SELECT canonical_name FROM entities WHERE id=?)",
        (target_id, source_id, target_id),
    )
    aliases_added += cur.rowcount or 0

    # 2. migrate edges: insert-or-ignore then delete source's edges.
    # Two-step pattern keeps no orphan rows even when the same fact already
    # links to target (PRIMARY KEY collision is silently absorbed).
    cur = conn.execute(
        "INSERT OR IGNORE INTO fact_entity_edges(fact_id, entity_id, role, created_at) "
        "SELECT fact_id, ?, role, created_at FROM fact_entity_edges WHERE entity_id=?",
        (target_id, source_id),
    )
    edges_migrated = cur.rowcount or 0
    cur = conn.execute(
        "DELETE FROM fact_entity_edges WHERE entity_id=?", (source_id,),
    )
    edges_deleted = cur.rowcount or 0

    # 3. accumulate mention_count
    conn.execute(
        "UPDATE entities SET mention_count = mention_count + ? WHERE id=?",
        (source_mention, target_id),
    )

    # 4. delete the source entity (ON DELETE CASCADE drops its aliases row)
    conn.execute("DELETE FROM entities WHERE id=?", (source_id,))

    return {
        "aliases_added": aliases_added,
        "edges_migrated": edges_migrated,
        "edges_deleted_from_source": edges_deleted,
        "mention_count_added": source_mention,
    }


def _apply_plan(plan: list[dict], dry_run: bool) -> dict:
    from services.mem0 import graph as _graph  # noqa: F401  (force schema init)
    from services.mem0.graph import _conn, invalidate_top_entities_cache

    totals = {
        "groups": 0,
        "sources_merged": 0,
        "aliases_added": 0,
        "edges_migrated": 0,
        "edges_deleted_from_source": 0,
        "mention_count_added": 0,
    }
    affected_users: set[str] = set()
    with _conn() as c:
        for group in plan:
            target_id = int(group["target_id"])
            source_ids = [int(s) for s in group["source_ids"]]
            sp = f"merge_{target_id}_{int(time.time() * 1000)}"
            c.execute(f"SAVEPOINT {sp}")
            try:
                # capture user_id once before merge so cache invalidation
                # still works on dry-run after rollback
                row = c.execute(
                    "SELECT user_id FROM entities WHERE id=?", (target_id,),
                ).fetchone()
                if row:
                    affected_users.add(row["user_id"])
                for src in source_ids:
                    stats = _merge_one(c, target_id, src)
                    for k, v in stats.items():
                        if k in totals:
                            totals[k] += v
                    totals["sources_merged"] += 1
                totals["groups"] += 1
                if dry_run:
                    c.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    c.execute(f"RELEASE SAVEPOINT {sp}")
                else:
                    c.execute(f"RELEASE SAVEPOINT {sp}")
            except Exception as exc:
                c.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                c.execute(f"RELEASE SAVEPOINT {sp}")
                log.error(
                    "group target=%d sources=%s failed: %s",
                    target_id, source_ids, exc,
                )
                raise
        if not dry_run:
            c.commit()
    if not dry_run:
        for u in affected_users:
            invalidate_top_entities_cache(u)
    return totals


# ---------------------------------------------------------------------------
# Orphan cleanup
# ---------------------------------------------------------------------------
def _cleanup_orphans(user_id: str, dry_run: bool) -> dict:
    from services.mem0 import graph as _graph  # noqa: F401
    from services.mem0.graph import _conn, invalidate_top_entities_cache

    with _conn() as c:
        rows = c.execute(
            "SELECT id, canonical_name, mention_count FROM entities "
            "WHERE user_id=? AND id NOT IN ("
            "  SELECT DISTINCT entity_id FROM fact_entity_edges"
            ")",
            (user_id,),
        ).fetchall()
        orphans = [dict(r) for r in rows]
        if not orphans:
            return {"orphans": 0, "deleted": 0}
        if dry_run:
            for o in orphans[:20]:
                log.info("orphan: id=%d name=%r mention=%d",
                         o["id"], o["canonical_name"], o["mention_count"])
            return {"orphans": len(orphans), "deleted": 0}
        ids = [o["id"] for o in orphans]
        placeholders = ",".join("?" for _ in ids)
        cur = c.execute(
            f"DELETE FROM entities WHERE id IN ({placeholders})", ids,
        )
        c.commit()
        invalidate_top_entities_cache(user_id)
        return {"orphans": len(orphans), "deleted": cur.rowcount or 0}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_list_candidates(args: argparse.Namespace) -> int:
    from services.mem0 import graph as _graph  # noqa: F401
    from services.mem0.graph import _conn

    with _conn() as c:
        entities = _list_entities(c, args.user_id)
        aliases = _list_aliases(c, args.user_id)
    candidates = _detect_candidates(entities, aliases)
    if args.min_confidence:
        candidates = [g for g in candidates if g["confidence"] >= args.min_confidence]
    out = {
        "user_id": args.user_id,
        "generated_at": int(time.time()),
        "total_entities": len(entities),
        "total_groups": len(candidates),
        "groups": candidates,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    log.info(
        "wrote %d groups to %s (entities=%d, aliases=%d)",
        len(candidates), args.out, len(entities), len(aliases),
    )
    for g in candidates[:10]:
        log.info(
            "  conf=%.2f target=%s ← %s [%s]",
            g["confidence"], g["target_name"],
            ", ".join(g["source_names"]),
            g["reason"],
        )
    if len(candidates) > 10:
        log.info("  ...(%d more)", len(candidates) - 10)
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.plan).read_text())
    groups = data.get("groups", []) if isinstance(data, dict) else data
    if not isinstance(groups, list) or not groups:
        log.error("plan has no groups: %s", args.plan)
        return 2
    log.info(
        "applying %d groups from %s (dry_run=%s)",
        len(groups), args.plan, args.dry_run,
    )
    totals = _apply_plan(groups, dry_run=args.dry_run)
    log.info("done: %s", totals)
    return 0


def _cmd_orphan_cleanup(args: argparse.Namespace) -> int:
    stats = _cleanup_orphans(args.user_id, dry_run=args.dry_run)
    log.info("orphan-cleanup: %s (dry_run=%s)", stats, args.dry_run)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    lc = sub.add_parser("list-candidates", help="detect duplicate entity groups")
    lc.add_argument("--user-id", required=True)
    lc.add_argument("--out", required=True, help="merge plan JSON path")
    lc.add_argument("--min-confidence", type=float, default=0.0)
    lc.set_defaults(func=_cmd_list_candidates)

    ap = sub.add_parser("apply", help="merge groups from a plan JSON")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.set_defaults(func=_cmd_apply)

    oc = sub.add_parser("orphan-cleanup", help="delete entities with zero edges")
    oc.add_argument("--user-id", required=True)
    oc.add_argument("--dry-run", action="store_true")
    oc.set_defaults(func=_cmd_orphan_cleanup)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
