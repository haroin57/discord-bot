"""evalsearch — tool loop 評価用の凍結コーパス検索 MCP サーバー（stdio）。

tests/fixtures/tool_loop_eval/corpus/*.md を検索対象とする。世界が凍っているので
アーキテクチャ変更で tool 呼び出し列が変わっても採点が安定する（録画リプレイの
代わりに「世界ごと凍らせる」方式。docs/2026-07-02-tool-loop-eval-design.md）。

起動: python evalsearch_server.py [--mode normal|error|flaky]
  error: 全ツール呼び出しが例外を返す（E3 用）
  flaky: ツールごとに初回呼び出しだけ失敗、以降成功（I1 用）
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

CORPUS_DIR = Path(__file__).parent / "corpus"

_parser = argparse.ArgumentParser()
_parser.add_argument("--mode", default="normal",
                     choices=["normal", "error", "flaky"])
_MODE = _parser.parse_args().mode
_flaky_seen: set[str] = set()

mcp = FastMCP("evalsearch")


def _load_corpus() -> list[dict]:
    docs = []
    for p in sorted(CORPUS_DIR.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        m = re.search(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        meta = {}
        body = text
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = text[m.end():]
        docs.append({
            "id": meta.get("id", p.stem),
            "title": meta.get("title", p.stem),
            "date": meta.get("date", ""),
            "body": body.strip(),
        })
    return docs


_DOCS = _load_corpus()


def _gate(tool: str) -> None:
    if _MODE == "error":
        raise RuntimeError("evalsearch: simulated backend outage")
    if _MODE == "flaky" and tool not in _flaky_seen:
        _flaky_seen.add(tool)
        raise RuntimeError("evalsearch: simulated transient failure (retry may succeed)")


def _tokenize(q: str) -> list[str]:
    # 空白区切り + 2-gram（日本語の部分一致用）
    toks = [t for t in re.split(r"\s+", q.strip()) if t]
    grams = []
    jp = re.sub(r"\s+", "", q)
    grams += [jp[i:i + 2] for i in range(len(jp) - 1)] if len(jp) > 2 else []
    return toks + grams[:60]


@mcp.tool()
def search(query: str, limit: int = 5) -> str:
    """凍結コーパスを検索し、関連記事の id / title / 抜粋を返す。

    詳細本文が必要なら get_article(id) を呼ぶこと。
    """
    _gate("search")
    toks = _tokenize(query)
    scored = []
    for d in _DOCS:
        hay = (d["title"] + "\n" + d["body"]).lower()
        score = sum(hay.count(t.lower()) for t in toks if len(t) >= 2)
        score += 5 * sum(1 for t in toks if len(t) >= 2 and t.lower() in d["title"].lower())
        if score > 0:
            scored.append((score, d))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return "検索結果: 0件"
    out = [f"検索結果: {min(len(scored), limit)}件"]
    for score, d in scored[:limit]:
        snippet = d["body"][:180].replace("\n", " ")
        out.append(f"[{d['id']}] {d['title']} ({d['date']}) score={score}\n  {snippet}…")
    return "\n".join(out)


@mcp.tool()
def get_article(article_id: str) -> str:
    """記事 id を指定して全文を取得する。"""
    _gate("get_article")
    for d in _DOCS:
        if d["id"] == article_id:
            return f"# {d['title']} ({d['date']})\n\n{d['body']}"
    return f"記事 {article_id!r} は存在しません。search で id を確認してください。"


if __name__ == "__main__":
    sys.stderr.write(f"evalsearch: {len(_DOCS)} docs, mode={_MODE}\n")
    mcp.run()
