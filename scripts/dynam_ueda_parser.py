#!/usr/bin/env python3
"""
ダイナム長野上田店のスロット稼働データを min-repo.com からスクレイピングして CSV化。

Usage:
    python dynam_ueda_parser.py --start 2026-04-18 --end 2026-06-17 --out ./dynam_ueda_output

Output CSVs:
    - events.csv         : 日次サマリ (date, event_type, total_diff, avg_diff, avg_g)
    - by_machine.csv     : 機種別平均 (date, machine, avg_diff, avg_g, win, total, payout_rate)
    - variety_units.csv  : バラエティ(1台設置)個別データ (date, machine, unit_no, diff, g, payout_rate)
    - timeseries.csv     : 機種別時系列 (date, machine, avg_diff)

Notes:
    - bot 側 EC2 で daidata.goraggio.com は CloudFront WAF で 403。
      代替路として min-repo.com (residential アクセスで普通に取れる) を使う。
    - rate limit: --rate-limit (default 3 秒) でリクエスト間隔を守る。
    - 想定実行環境: ローカル Claude Code セッション (Windows/Mac/Linux ローカル)。
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

import requests

# --- Constants ----------------------------------------------------------------

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

TAG_URL = (
    "https://min-repo.com/tag/"
    "%E3%83%80%E3%82%A4%E3%83%8A%E3%83%A0%E9%95%B7%E9%87%8E%E4%B8%8A%E7%94%B0%E5%BA%97/"
)
TAG_URL_PAGED = TAG_URL + "page/{page}/"
ARTICLE_URL = "https://min-repo.com/{id}/"

# イベント日カレンダー (手動メンテ、判明分のみ列挙)
# 必要に応じて追記してください。
SLOSELE_DAYS = {"2026-06-01", "2026-06-17"}
WRITER_VISIT_DAYS: set[str] = set()


# --- Fetch --------------------------------------------------------------------

def fetch_tag_page(page: int, session: requests.Session, timeout: int = 15) -> List[str]:
    """指定ページの上田店記事 ID リストを返す。"""
    url = TAG_URL if page == 1 else TAG_URL_PAGED.format(page=page)
    try:
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        print(f"[WARN] tag page {page} fetch failed: {e}", file=sys.stderr)
        return []
    html = r.text
    ids: List[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="https?://min-repo\.com/(\d+)/"', html):
        i = m.group(1)
        if i in seen:
            continue
        seen.add(i)
        ids.append(i)
    return ids


def fetch_article(id_: str, session: requests.Session, timeout: int = 15) -> Optional[str]:
    """記事ページの HTML を返す (失敗時は None)。"""
    url = ARTICLE_URL.format(id=id_)
    try:
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[WARN] article {id_} fetch failed: {e}", file=sys.stderr)
        return None


# --- Parse helpers ------------------------------------------------------------

def extract_title(html: str) -> str:
    m = re.search(r"<title>([^<]+)</title>", html)
    return m.group(1).strip() if m else ""


def parse_date_from_title(title: str, today: Optional[date] = None) -> Optional[date]:
    """タイトル例: '6/17(水) ダイナム長野上田店 ...' → date(YYYY,6,17)。

    年は 'today の月以降なら前年' という単純推定。
    """
    if today is None:
        today = date.today()
    m = re.search(r"(\d{1,2})/(\d{1,2})", title)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    year = today.year if month <= today.month else today.year - 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def is_target_shop(title: str) -> bool:
    return "ダイナム長野上田店" in title


def event_label_for_date(d: date) -> str:
    s = d.strftime("%Y-%m-%d")
    labels: List[str] = []
    if s in SLOSELE_DAYS:
        labels.append("slosele")
    if s in WRITER_VISIT_DAYS:
        labels.append("writer_visit")
    day = d.day
    if day % 10 == 7:
        labels.append("special_7")
    if day % 10 == 4:
        labels.append("special_4")
    if day % 10 == 5:
        labels.append("special_5")
    if day % 10 == 9:
        labels.append("special_9")
    return "+".join(labels) if labels else "normal"


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).replace("&amp;", "&").replace("&#8211;", "-").strip()


def parse_money(s: str) -> Optional[int]:
    s = s.replace(",", "").replace("+", "").strip()
    if s in ("", "-", "—"):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_rate(s: str) -> Optional[float]:
    s = s.strip().replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def get_tables(html: str) -> List[str]:
    return re.findall(r"<table[^>]*>(.*?)</table>", html, flags=re.DOTALL)


def get_rows(table_html: str) -> List[List[str]]:
    raw_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.DOTALL)
    parsed: List[List[str]] = []
    for r in raw_rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, flags=re.DOTALL)
        parsed.append([strip_tags(c) for c in cells])
    return parsed


# --- Per-article parser -------------------------------------------------------

def parse_article(html: str, date_str: str, event_label: str) -> dict:
    """記事 HTML から 4 種類の CSV 用レコードを抽出。"""
    out = {
        "events": [],
        "by_machine": [],
        "variety_units": [],
        "timeseries": [],
    }
    tables = get_tables(html)
    if len(tables) < 3:
        return out

    # table[0] = 当日サマリ
    t0 = get_rows(tables[0])
    summary = {r[0]: r[1] for r in t0 if len(r) >= 2}
    out["events"].append({
        "date": date_str,
        "event_type": event_label,
        "total_diff": parse_money(summary.get("総差枚", "")),
        "avg_diff": parse_money(summary.get("平均差枚", "")),
        "avg_g": parse_money(summary.get("平均G数", "")),
    })

    # table[1] = 機種別 (2台以上設置機種)
    t1 = get_rows(tables[1])
    for row in t1[1:]:
        if len(row) < 5:
            continue
        win, total = None, None
        if "/" in row[3]:
            try:
                win, total = [int(x) for x in row[3].split("/")]
            except ValueError:
                pass
        avg_diff = parse_money(row[1])
        out["by_machine"].append({
            "date": date_str,
            "machine": row[0],
            "avg_diff": avg_diff,
            "avg_g": parse_money(row[2]),
            "win": win,
            "total": total,
            "payout_rate": parse_rate(row[4]),
        })
        out["timeseries"].append({
            "date": date_str,
            "machine": row[0],
            "avg_diff": avg_diff,
        })

    # table[2] = バラエティ (1台設置機種、台番付き)
    t2 = get_rows(tables[2])
    for row in t2[1:]:
        if len(row) < 5:
            continue
        out["variety_units"].append({
            "date": date_str,
            "machine": row[0],
            "unit_no": row[1],
            "diff": parse_money(row[2]),
            "g": parse_money(row[3]),
            "payout_rate": parse_rate(row[4]),
        })

    return out


# --- CSV write ----------------------------------------------------------------

CSV_HEADERS = {
    "events": ["date", "event_type", "total_diff", "avg_diff", "avg_g"],
    "by_machine": ["date", "machine", "avg_diff", "avg_g", "win", "total", "payout_rate"],
    "variety_units": ["date", "machine", "unit_no", "diff", "g", "payout_rate"],
    "timeseries": ["date", "machine", "avg_diff"],
}


def write_csvs(out_dir: Path, rows_by_kind: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for kind, header in CSV_HEADERS.items():
        path = out_dir / f"{kind}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            w.writerows(rows_by_kind.get(kind, []))


# --- Main ---------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Scrape ダイナム長野上田店 slot data from min-repo.com → CSVs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start", required=True, help="start date YYYY-MM-DD (inclusive)")
    p.add_argument("--end", required=True, help="end date YYYY-MM-DD (inclusive)")
    p.add_argument("--out", default="./dynam_ueda_output", help="output dir (default: ./dynam_ueda_output)")
    p.add_argument("--tag-pages", type=int, default=5, help="tag pages to scan (default 5)")
    p.add_argument("--rate-limit", type=float, default=3.0, help="seconds between requests (default 3.0)")
    args = p.parse_args(argv)

    try:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
    except ValueError as e:
        print(f"[ERROR] invalid date format: {e}", file=sys.stderr)
        return 2
    if start > end:
        print("[ERROR] --start must be <= --end", file=sys.stderr)
        return 2

    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
    })

    # Stage 1: ID list 取得
    print(f"[INFO] scanning up to {args.tag_pages} tag pages ...", file=sys.stderr)
    candidate_ids: List[str] = []
    seen: set[str] = set()
    for page in range(1, args.tag_pages + 1):
        ids = fetch_tag_page(page, session)
        new = [i for i in ids if i not in seen]
        seen.update(new)
        candidate_ids.extend(new)
        print(f"[INFO] tag page {page}: {len(ids)} ids, total unique: {len(candidate_ids)}", file=sys.stderr)
        if not ids:
            break
        time.sleep(args.rate_limit)
    print(f"[INFO] candidate IDs: {len(candidate_ids)}", file=sys.stderr)

    # Stage 2: 各記事を fetch → フィルタ → parse
    rows_by_kind: dict = {k: [] for k in CSV_HEADERS}
    hit, skipped_other_shop, skipped_out_of_range, failed = 0, 0, 0, 0

    for id_ in candidate_ids:
        html = fetch_article(id_, session)
        if html is None:
            failed += 1
            time.sleep(args.rate_limit)
            continue
        title = extract_title(html)
        if not is_target_shop(title):
            skipped_other_shop += 1
            time.sleep(args.rate_limit)
            continue
        d = parse_date_from_title(title)
        if d is None:
            failed += 1
            time.sleep(args.rate_limit)
            continue
        if d < start or d > end:
            skipped_out_of_range += 1
            time.sleep(args.rate_limit)
            continue
        date_str = d.strftime("%Y-%m-%d")
        event_label = event_label_for_date(d)
        try:
            parsed = parse_article(html, date_str, event_label)
        except Exception as e:
            print(f"[WARN] parse error id={id_} date={date_str}: {e}", file=sys.stderr)
            failed += 1
            time.sleep(args.rate_limit)
            continue
        for kind in rows_by_kind:
            rows_by_kind[kind].extend(parsed[kind])
        hit += 1
        print(
            f"[OK] {date_str} ({event_label}) — "
            f"by_machine={len(parsed['by_machine'])} variety={len(parsed['variety_units'])}",
            file=sys.stderr,
        )
        time.sleep(args.rate_limit)

    # Stage 3: CSV write
    out_dir = Path(args.out)
    write_csvs(out_dir, rows_by_kind)

    print("", file=sys.stderr)
    print(f"[DONE] hit={hit}  other_shop={skipped_other_shop}  out_of_range={skipped_out_of_range}  failed={failed}", file=sys.stderr)
    print(f"[DONE] CSVs written to {out_dir}/", file=sys.stderr)
    for kind in CSV_HEADERS:
        path = out_dir / f"{kind}.csv"
        n = sum(1 for _ in open(path, encoding="utf-8")) - 1
        print(f"       {kind}.csv: {n} rows", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
