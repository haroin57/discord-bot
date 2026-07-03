"""MCP web tool_result からのソース機械抽出（ADR-0028 / FR-6）.

LLM の引用に頼らず、検索ツール（brave / tavily / x-scraper）の生戻り値（JSON 文字列）
から URL を機械抽出して ``ResearchState.sources`` に sid 採番付きで登録する。citation
欠落・捏造を構造的に防ぐ。JSON で取れない場合は URL 正規表現でフォールバックする。
"""

from __future__ import annotations

import json
import logging
import re

from .types import ResearchState, SourceRef

log = logging.getLogger(__name__)

_URL_RE = re.compile(r'https?://[^\s"\'<>)\]}]+')
_URL_KEYS = ("url", "URL", "link", "href", "uri", "permalink")
_TITLE_KEYS = ("title", "name", "heading")
_SNIPPET_KEYS = ("description", "snippet", "content", "text", "summary", "body")


def _source_type_for(tool_name: str) -> str:
    n = (tool_name or "").lower()
    if "x-scraper" in n or "search_x" in n or "tweet" in n or "_x_" in n:
        return "x"
    if "news" in n:
        return "news"
    return "web"


def _first(node: dict, keys) -> str:
    for k in keys:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _walk_json(node, out: list[tuple[str, str, str]]) -> None:
    if isinstance(node, dict):
        url = _first(node, _URL_KEYS)
        if url.startswith("http"):
            out.append((_first(node, _TITLE_KEYS), url, _first(node, _SNIPPET_KEYS)))
        for v in node.values():
            _walk_json(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, out)


def _extract_from_json(raw: str):
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    out: list[tuple[str, str, str]] = []
    _walk_json(data, out)
    return out


def _extract_from_text(raw: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for m in _URL_RE.finditer(raw or ""):
        url = m.group(0).rstrip(".,);:'\"")
        out.append(("", url, ""))
    return out


def parse_sources(tool_name: str, raw_result: str, state: ResearchState) -> list[SourceRef]:
    """tool_result 文字列からソースを抽出して state に登録。戻り: 新規/既存 SourceRef 群。

    best-effort: 解析に失敗しても例外を投げず空リストを返す（呼び出し側はツール結果
    自体は別途モデルに渡す）。
    """
    source_type = _source_type_for(tool_name)
    found = _extract_from_json(raw_result)
    if found is None:
        found = _extract_from_text(raw_result)
    refs: list[SourceRef] = []
    for title, url, snippet in found:
        ref = state.register_source(
            url=url, title=title, source_type=source_type, snippet=snippet,
        )
        if ref is not None:
            refs.append(ref)
    return refs


def verify_citations(state: ResearchState) -> list[str]:
    """各 result の cited_sids のうち実在ソースに対応しない sid を返す（Phase 2 で本格活用）。"""
    known = {ref.sid for ref in state.sources.values()}
    missing: list[str] = []
    for res in state.results:
        for sid in res.cited_sids:
            if sid not in known and sid not in missing:
                missing.append(sid)
    return missing


def format_sources_block(state: ResearchState, *, limit: int = 40) -> str:
    """機械収集ソースを ``情報ソース`` ブロックに整形（全件）。"""
    if not state.sources:
        return ""
    lines = ["**情報ソース（自動収集）**"]
    for ref in list(state.sources.values())[:limit]:
        title = ref.title or ref.url
        lines.append(f"- [{ref.sid}] {title} — <{ref.url}>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 2: レポート本文の引用検証 + 機械付与のソース一覧（citation 厳密化）
# ---------------------------------------------------------------------------
_SID_RE = re.compile(r"\[S(\d+)\]")


def extract_cited_sids(report: str) -> list[str]:
    """レポート本文から ``[S3]`` 形式の引用を順序保持・ユニークで抽出。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _SID_RE.finditer(report or ""):
        sid = f"S{m.group(1)}"
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def verify_report_citations(report: str, state: ResearchState) -> tuple[list[str], list[str]]:
    """本文の ``[S#]`` 引用を実在ソースと機械照合。

    戻り: ``(present, missing)``。present = state.sources に存在する sid、
    missing = 本文が引用したが実在しない sid（synthesizer の捏造・誤番号）。
    """
    known = {ref.sid for ref in state.sources.values()}
    cited = extract_cited_sids(report)
    present = [s for s in cited if s in known]
    missing = [s for s in cited if s not in known]
    return present, missing


def _format_source_list(state: ResearchState, sids: list[str]) -> str:
    if not state.sources or not sids:
        return ""
    by_sid = {ref.sid: ref for ref in state.sources.values()}
    lines = ["**情報ソース**"]
    for sid in sids:
        ref = by_sid.get(sid)
        if ref is not None:
            lines.append(f"- [{ref.sid}] {ref.title or ref.url} — <{ref.url}>")
    return "\n".join(lines) if len(lines) > 1 else ""


def build_final_report(report: str, state: ResearchState) -> str:
    """synthesizer 本文に、機械収集ソースから決定的に組んだ情報ソース一覧を付与する。

    synthesizer には情報ソース一覧を書かせず（URL 捏造を構造的に排除）、本文で引用された
    ``[S#]`` に対応する実在ソースだけをここで列挙する。引用が無ければ全収集ソースを載せる。
    実在しない引用 sid があれば ⚠️ 注記を付ける（透明性）。
    """
    body = (report or "").rstrip()
    present, missing = verify_report_citations(body, state)
    sids = present or [ref.sid for ref in state.sources.values()]
    parts = [body]
    block = _format_source_list(state, sids)
    if block:
        parts.append("")
        parts.append(block)
    if missing:
        parts.append("")
        parts.append(
            f"-# ⚠️ 本文の引用 {', '.join(missing)} は出典一覧に対応が見つかりませんでした。"
        )
    return "\n".join(parts)
