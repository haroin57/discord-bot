#!/usr/bin/env python3
"""invest_kb ingest CLI.

Subcommands:
  edinet     — pull SakanaAI/EDINET-Bench from HuggingFace, extract narrative
               sections only (no labels, no predictions) and ingest.
  cfa-rpc    — read a local clone of a CFA-Institute-RPC repo, pull markdown
               cells from each .ipynb and ingest.
  hayatoito  — fetch hayatoito.github.io/2020/investing/ via the public URL
               and ingest the parsed article as Markdown.
  status     — show counts per source from the LanceDB table.

Usage:
  python scripts/ingest_invest_kb.py edinet --limit 50 --dry-run
  python scripts/ingest_invest_kb.py cfa-rpc --repo-path /tmp/the-automation-ahead
  python scripts/ingest_invest_kb.py hayatoito
  python scripts/ingest_invest_kb.py status
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

# Make `services` importable when run as a script.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

log = logging.getLogger("ingest_invest_kb")


# ---------------------------------------------------------------------------
# EDINET loader
# ---------------------------------------------------------------------------
def _load_edinet_rows(limit: int | None) -> list[dict]:
    """Return source-row dicts ready for upsert_documents.

    SECURITY/INTEGRITY: only pulls narrative text. Labels (`label`,
    `explanation`) and prediction-related columns are deliberately not read.
    """
    from datasets import load_dataset

    # 3 configs in this dataset; we want them all but only the narrative text.
    all_rows: list[dict] = []
    for cfg in ("fraud_detection", "earnings_forecast", "industry_prediction"):
        try:
            ds = load_dataset("SakanaAI/EDINET-Bench", name=cfg, split="train")
        except Exception as exc:  # noqa: BLE001
            log.warning("EDINET-Bench %s load failed: %s", cfg, exc)
            continue
        for i, row in enumerate(ds):
            if limit is not None and len(all_rows) >= limit:
                break
            narrative = _extract_narrative(row)
            if not narrative.strip():
                continue
            edinet_code = (
                _dig(row, "edinet_code")
                or _dig(row, "company_metadata", "edinet_code")
                or _dig(row, "metadata", "edinet_code")
                or f"unknown-{cfg}-{i}"
            )
            company_name = (
                _dig(row, "company_name")
                or _dig(row, "company_metadata", "company_name")
                or "(no-title)"
            )
            fy_end = _to_unix(
                _dig(row, "fiscal_year_end")
                or _dig(row, "company_metadata", "fiscal_year_end")
            )
            all_rows.append({
                "text": narrative,
                "source": "edinet",
                "source_id": f"{edinet_code}-{cfg}-{i}",
                "title": company_name,
                "source_url": (
                    f"https://disclosure.edinet-fsa.go.jp/E01EW/BLMainController.jsp"
                    f"?uji.verb=W1E62012&edinetCode={edinet_code}"
                ),
                "lang": "ja",
                "license": "PDL-1.0",
                "published_at": fy_end,
                "category": "disclosure",
            })
    return all_rows


def _dig(d, *path):
    """Safe nested dict get."""
    cur = d
    for k in path:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return None
    return cur


def _extract_narrative(row: dict) -> str:
    """Find narrative text under several possible keys.

    Deliberately NEVER touches: `label`, `explanation`, `prediction`,
    `next_year_*`, `target`, `revenue_change`, anything *_label.
    """
    forbidden_substrings = ("label", "predict", "explan", "target", "_change")

    def _is_text(v) -> bool:
        return isinstance(v, str) and len(v.strip()) >= 80

    def _is_safe_key(k: str) -> bool:
        kl = k.lower()
        return not any(s in kl for s in forbidden_substrings)

    pieces: list[str] = []

    # Common shapes
    for key in (
        "narrative_sections",
        "narrative",
        "narrative_text",
        "text",
        "annual_report_text",
    ):
        v = row.get(key)
        if isinstance(v, dict):
            for k, t in v.items():
                if _is_safe_key(k) and _is_text(t):
                    pieces.append(f"# {k}\n{t}")
        elif _is_text(v):
            pieces.append(v)
    # Sometimes shoved under "text" with sub-keys
    text_root = row.get("text")
    if isinstance(text_root, dict):
        for k, t in text_root.items():
            if _is_safe_key(k) and _is_text(t):
                pieces.append(f"# {k}\n{t}")
    return "\n\n".join(pieces)


def _to_unix(val) -> int:
    if not val:
        return 0
    try:
        if isinstance(val, (int, float)):
            return int(val)
        from datetime import datetime
        s = str(val)
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return int(datetime.strptime(s[:10], fmt).timestamp())
            except ValueError:
                continue
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# CFA RPC loader
# ---------------------------------------------------------------------------
def _load_cfa_rows(repo_path: Path) -> list[dict]:
    """Walk a CFA-Institute-RPC repo clone, pull markdown cells from .ipynb."""
    import nbformat

    if not repo_path.exists():
        raise FileNotFoundError(f"--repo-path not found: {repo_path}")
    rows: list[dict] = []
    repo_name = repo_path.name
    for ipynb in sorted(repo_path.rglob("*.ipynb")):
        try:
            nb = nbformat.read(ipynb, as_version=4)
        except Exception as exc:  # noqa: BLE001
            log.warning("nbformat read failed (%s): %s", ipynb, exc)
            continue
        md_cells = [
            c.source for c in nb.cells if c.cell_type == "markdown" and (c.source or "").strip()
        ]
        if not md_cells:
            continue
        text = "\n\n".join(md_cells)
        rel = ipynb.relative_to(repo_path)
        title = (
            re.search(r"^#\s+(.+)$", md_cells[0], re.MULTILINE)
            or re.search(r"^##\s+(.+)$", md_cells[0], re.MULTILINE)
        )
        rows.append({
            "text": text,
            "source": "cfa-rpc",
            "source_id": f"{repo_name}/{rel.as_posix()}",
            "title": title.group(1).strip() if title else rel.stem,
            "source_url": (
                f"https://github.com/CFA-Institute-RPC/{repo_name}/blob/main/{rel.as_posix()}"
            ),
            "lang": "en",
            "license": "MIT",
            "category": "framework",
            "published_at": 0,
        })
    return rows


# ---------------------------------------------------------------------------
# hayatoito loader
# ---------------------------------------------------------------------------
HAYATOITO_URL = "https://hayatoito.github.io/2020/investing/"


# Patch 2 (2026-06-17): additional neutral sources to cover the concept-level
# queries citation eval surfaced as worst (CAPM, EMH, PER/PBR/ROE/ROA,
# rebalancing, NISA/iDeCo specifics). Order chosen for neutrality:
#   - Wikipedia finance theory articles (academic neutral, well-cited)
#   - FSA (Japan's Financial Services Agency) NISA / iDeCo official pages
#   - Damodaran's NYU lecture page (academic standard, no commercial pitch)
_WIKI_URLS = [
    ("https://en.wikipedia.org/wiki/Capital_asset_pricing_model", "en", "CAPM"),
    ("https://en.wikipedia.org/wiki/Modern_portfolio_theory", "en", "Modern Portfolio Theory"),
    ("https://en.wikipedia.org/wiki/Efficient-market_hypothesis", "en", "Efficient Market Hypothesis"),
    ("https://en.wikipedia.org/wiki/Price%E2%80%93earnings_ratio", "en", "PER"),
    ("https://en.wikipedia.org/wiki/P/B_ratio", "en", "PBR"),
    ("https://en.wikipedia.org/wiki/Return_on_equity", "en", "ROE"),
    ("https://en.wikipedia.org/wiki/Return_on_assets", "en", "ROA"),
    ("https://en.wikipedia.org/wiki/Rebalancing_investments", "en", "Rebalancing"),
    ("https://en.wikipedia.org/wiki/Index_fund", "en", "Index Fund"),
    ("https://en.wikipedia.org/wiki/Dollar_cost_averaging", "en", "Dollar Cost Averaging"),
    ("https://en.wikipedia.org/wiki/Asset_allocation", "en", "Asset Allocation"),
    ("https://en.wikipedia.org/wiki/Sharpe_ratio", "en", "Sharpe Ratio"),
    ("https://en.wikipedia.org/wiki/Bond_duration", "en", "Bond Duration"),
    ("https://en.wikipedia.org/wiki/Diversification_(finance)", "en", "Diversification"),
]

_FSA_URLS = [
    ("https://www.fsa.go.jp/policy/nisa2/about/nisa2024/", "ja", "新NISA 制度の概要 (金融庁)"),
    ("https://www.fsa.go.jp/policy/nisa2/about/", "ja", "NISA とは (金融庁)"),
    ("https://www.ideco-koushiki.jp/start/", "ja", "iDeCo を始める (公式)"),
]


def _fetch_url(url: str) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; invest_kb_ingest/1.0)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _html_to_markdown(html: str) -> str:
    """Coarse HTML -> markdown. Strip scripts/styles, convert headings,
    paragraphs, list items, then drop residual tags."""
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    # Headings
    for lv, tag in [(1, "h1"), (2, "h2"), (3, "h3"), (4, "h4"), (5, "h5"), (6, "h6")]:
        html = re.sub(
            rf"<{tag}[^>]*>([\s\S]*?)</{tag}>",
            lambda m: f"\n\n{'#' * lv} {m.group(1).strip()}\n\n",
            html, flags=re.IGNORECASE,
        )
    html = re.sub(r"<li[^>]*>([\s\S]*?)</li>", r"\n- \1", html, flags=re.IGNORECASE)
    html = re.sub(r"<p[^>]*>([\s\S]*?)</p>", r"\n\n\1\n\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    # HTML entities (very small subset; let stdlib handle the rest)
    import html as _html_lib
    html = _html_lib.unescape(html)
    # Strip remaining tags
    html = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n\s*\n+", "\n\n", html).strip()
    return html


def _load_wikipedia_rows(urls=None) -> list[dict]:
    """Pull main article body from each wiki page. Use the public REST plain-
    text endpoint when available to avoid wrestling with the full HTML."""
    rows: list[dict] = []
    for url, lang, title in (urls or _WIKI_URLS):
        try:
            html = _fetch_url(url)
        except Exception as exc:
            log.warning("wiki fetch failed (%s): %s", url, exc)
            continue
        # Strip everything outside <main> if present; wikipedia uses
        # <main id="content"> or just <div id="content"> in legacy skin.
        m = (
            re.search(r"<main[^>]*>([\s\S]*?)</main>", html, re.IGNORECASE)
            or re.search(r'<div[^>]*id="bodyContent"[^>]*>([\s\S]*?)</div>\s*<div[^>]*id="footer"', html, re.IGNORECASE)
            or re.search(r'<div[^>]*id="content"[^>]*>([\s\S]*?)</div>\s*<div[^>]*id="footer"', html, re.IGNORECASE)
        )
        body = m.group(1) if m else html
        # Drop wikipedia's edit-section anchors, references, navboxes
        body = re.sub(r"<span[^>]*class=\"mw-editsection\"[\s\S]*?</span>", " ", body, flags=re.IGNORECASE)
        body = re.sub(r"\[edit\]", " ", body, flags=re.IGNORECASE)
        body = re.sub(r"\[\d+\]", " ", body)
        text = _html_to_markdown(body)
        if not text or len(text) < 500:
            log.warning("wiki body suspiciously short for %s: %d chars", url, len(text))
        rows.append({
            "text": text,
            "source": "wikipedia",
            "source_id": title.replace(" ", "_"),
            "title": title,
            "source_url": url,
            "lang": lang,
            "license": "CC-BY-SA-4.0",
            "category": "framework",
            "published_at": 0,
        })
    return rows


def _load_fsa_rows(urls=None) -> list[dict]:
    rows: list[dict] = []
    for url, lang, title in (urls or _FSA_URLS):
        try:
            html = _fetch_url(url)
        except Exception as exc:
            log.warning("fsa fetch failed (%s): %s", url, exc)
            continue
        m = (
            re.search(r"<main[^>]*>([\s\S]*?)</main>", html, re.IGNORECASE)
            or re.search(r"<article[^>]*>([\s\S]*?)</article>", html, re.IGNORECASE)
            or re.search(r"<body[^>]*>([\s\S]*?)</body>", html, re.IGNORECASE)
        )
        body = m.group(1) if m else html
        text = _html_to_markdown(body)
        if not text or len(text) < 200:
            log.warning("fsa body too short for %s: %d chars", url, len(text))
        rows.append({
            "text": text,
            "source": "fsa",
            "source_id": url.rsplit("/", 2)[-2] if url.endswith("/") else url.rsplit("/", 1)[-1],
            "title": title,
            "source_url": url,
            "lang": lang,
            "license": "Japan-Government-PDL",
            "category": "tax_jp",
            "published_at": 0,
        })
    return rows


def _load_hayatoito_rows() -> list[dict]:
    """Fetch the article via urllib and crudely convert to markdown text."""
    import urllib.request

    req = urllib.request.Request(HAYATOITO_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    # Strip script/style
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)

    # Pull the main article. Hayatoito uses a simple template; the body is
    # inside <main>...</main> (or <article>) — fall back to <body> if needed.
    m = (
        re.search(r"<main[^>]*>([\s\S]*?)</main>", html, re.IGNORECASE)
        or re.search(r"<article[^>]*>([\s\S]*?)</article>", html, re.IGNORECASE)
        or re.search(r"<body[^>]*>([\s\S]*?)</body>", html, re.IGNORECASE)
    )
    body = m.group(1) if m else html

    # Convert heading tags to markdown headings, paragraphs/lists to plain
    # text. Coarse but enough for chunk-friendly text.
    def _h(tag, level):
        nonlocal body
        body = re.sub(
            rf"<{tag}[^>]*>([\s\S]*?)</{tag}>",
            lambda mm: f"\n\n{'#' * level} {mm.group(1).strip()}\n\n",
            body,
            flags=re.IGNORECASE,
        )

    for lv, tag in [(1, "h1"), (2, "h2"), (3, "h3"), (4, "h4"), (5, "h5"), (6, "h6")]:
        _h(tag, lv)

    body = re.sub(r"<li[^>]*>([\s\S]*?)</li>", r"\n- \1", body, flags=re.IGNORECASE)
    body = re.sub(r"<p[^>]*>([\s\S]*?)</p>", r"\n\n\1\n\n", body, flags=re.IGNORECASE)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    # Strip remaining tags
    body = re.sub(r"<[^>]+>", " ", body)
    # Collapse whitespace
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n\s*\n\s*\n+", "\n\n", body).strip()

    return [{
        "text": body,
        "source": "hayatoito",
        "source_id": "2020-investing",
        "title": "普通の人が資産運用で99点をとる方法",
        "source_url": HAYATOITO_URL,
        "lang": "ja",
        "license": "public-blog",
        "category": "philosophy",
        "published_at": _to_unix("2020-01-01"),
    }]


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------
async def _run_ingest(rows: list[dict], dry_run: bool) -> dict:
    from services.invest_kb.ingest import upsert_documents
    return await upsert_documents(rows, dry_run=dry_run)


def _setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_edinet(args):
    rows = _load_edinet_rows(limit=args.limit)
    log.info("EDINET rows extracted: %d", len(rows))
    result = asyncio.run(_run_ingest(rows, dry_run=args.dry_run))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_cfa(args):
    rows = _load_cfa_rows(Path(args.repo_path))
    log.info("CFA rows extracted: %d", len(rows))
    result = asyncio.run(_run_ingest(rows, dry_run=args.dry_run))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_hayatoito(args):
    rows = _load_hayatoito_rows()
    log.info("hayatoito rows extracted: %d (lengths=%s)", len(rows), [len(r["text"]) for r in rows])
    result = asyncio.run(_run_ingest(rows, dry_run=args.dry_run))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_wikipedia(args):
    rows = _load_wikipedia_rows()
    log.info("wikipedia rows extracted: %d (lengths=%s)", len(rows), [len(r["text"]) for r in rows])
    result = asyncio.run(_run_ingest(rows, dry_run=args.dry_run))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_fsa(args):
    rows = _load_fsa_rows()
    log.info("fsa rows extracted: %d (lengths=%s)", len(rows), [len(r["text"]) for r in rows])
    result = asyncio.run(_run_ingest(rows, dry_run=args.dry_run))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_status(args):
    from services.invest_kb.client import table_stats
    print(json.dumps(table_stats(), ensure_ascii=False, indent=2))


def main():
    _setup_logging()
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_e = sub.add_parser("edinet")
    p_e.add_argument("--limit", type=int, default=None)
    p_e.add_argument("--dry-run", action="store_true")
    p_e.set_defaults(func=cmd_edinet)

    p_c = sub.add_parser("cfa-rpc")
    p_c.add_argument("--repo-path", required=True)
    p_c.add_argument("--dry-run", action="store_true")
    p_c.set_defaults(func=cmd_cfa)

    p_h = sub.add_parser("hayatoito")
    p_h.add_argument("--dry-run", action="store_true")
    p_h.set_defaults(func=cmd_hayatoito)

    p_w = sub.add_parser("wikipedia")
    p_w.add_argument("--dry-run", action="store_true")
    p_w.set_defaults(func=cmd_wikipedia)

    p_f = sub.add_parser("fsa")
    p_f.add_argument("--dry-run", action="store_true")
    p_f.set_defaults(func=cmd_fsa)

    p_s = sub.add_parser("status")
    p_s.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
