"""MCP server selection by user-text keyword scanning.

`pick_mcp_servers(raw_user_text)` returns the minimal set of MCP server
configs needed for a request. BASE is always loaded; WEB / HEAVY /
MEMORY / PAPER / DOCS tiers are added only when the user's raw text
mentions a trigger keyword. This avoids paying chromium-startup costs
or spinning up ChromaDB on every chat reply.

The server tier dicts and the union `MCP_SERVERS` live in `config.py`
since they bind to env-var credentials (BRAVE_API_KEY, TAVILY_API_KEY,
GITHUB_PERSONAL_ACCESS_TOKEN, etc.). This module is pure dispatch on
top of that config.
"""

from __future__ import annotations

import re

from config import (
    ADMIN_USER_IDS,
    AGENTIC_INBOX_ALLOWED_USER_IDS,
    KANAE_ALARM_ALLOWED_USER_IDS,
    MCP_SERVERS,
    MCP_SERVERS_ALARM,
    MCP_SERVERS_BASE,
    MCP_SERVERS_DOCS,
    MCP_SERVERS_HEAVY,
    MCP_SERVERS_INSHI,
    MCP_SERVERS_IOT,
    MCP_SERVERS_MAIL,
    MCP_SERVERS_MEMORY,
    MCP_SERVERS_PAPER,
    MCP_SERVERS_REALESTATE,
    MCP_SERVERS_WEB,
    NATURE_REMO_ALLOWED_USER_IDS,
)
from services.mcp_control import filter_enabled_mcp_configs

__all__ = [
    "get_mcp_servers_by_keys",
    "pick_mcp_servers",
    "select_mcp_servers",
]


# Keyword sets used by pick_mcp_servers(). Cheap substring scans on the
# user's raw text — model is still free to refuse / pick differently, this
# only decides which subprocesses we spin up for the request.
# Tuples are pre-lowercased at module load so pick_mcp_servers() can compare
# without a per-call .lower() on each keyword.
_WEB_TRIGGERS = tuple(kw.lower() for kw in (
    "検索", "調べて", "ググって", "ニュース",
    "twitter", "x.com", "ツイート", "tweet", "trend", "トレンド",
    # NOTE: 旧 "x " (X+空白) は「マトリックス では」等の日本語文で誤爆したため
    # 前後空白付きの " x " に変更（英文中の単独 X のみ拾う）。
    "search", "ググ", "Brave", " x ", "X(", "X検索",
    # Market-data tier (nikkei-indexes / yfj-quote) — eager spawn for finance
    # talk. NOTE: bare "vi" is deliberately absent (substring would misfire
    # on "via", "vim", "review", …); 日経vi covers the real usage.
    "株価", "日経", "日経vi", "市場", "相場", "チャート", "銘柄",
    "vix", "ドル円", "為替", "topix", "指数",
    # minkabu-fx オーダーブック (注文/建玉の価格帯別分布)。
    "オーダーブック", "orderbook", "order book", "建玉", "売り壁", "買い指値",
    "ショート", "ロング", "踏み上げ", "バタフライ", "板",
    # Google Maps tier — Places / Geocoding / Directions 系の質問で eager spawn.
    "地図", "マップ", "google maps", "googlemaps", "経路", "ルート", "道順",
    "最寄り駅", "近くの", "周辺の", "住所", "緯度", "経度", "ジオコー",
    "places", "directions",
))
_HEAVY_TRIGGERS = tuple(kw.lower() for kw in (
    "ブラウズ", "browse", "スクリーンショット", "screenshot",
    "playwright", "puppeteer", "render", "webページを開",
    "サイトを開", "アクセスして",
))
_MEMORY_TRIGGERS = tuple(kw.lower() for kw in (
    "記憶", "覚えて", "覚えとい", "覚えてる", "思い出", "忘れた", "忘れない",
    "mem0", "memory", "remember", "recall", "過去の",
    "前に話した", "前に言った", "以前", "昔話した",
    # Internal-module references — when the user talks about the Mem0 code
    # (search.py / dedup / extract / ingest / mcp_mem0), they almost always
    # want to *test* it, which means kanae will need mcp__mem0__search.
    "search.py", "dedup", "extract.py", "ingest.py", "mcp__mem0",
    "mcp_mem0", "mem0_server",
    "記憶検索", "メモリ検索", "ベクトル検索", "セマンティック検索",
))
_PAPER_TRIGGERS = tuple(kw.lower() for kw in (
    # "scholar" without a qualifier was a false-positive risk (matches
    # "scholarship", "scholar's", etc.). Use disambiguated forms only.
    "論文", "paper", "arxiv", "学術", "引用文献", "citation",
    "google scholar", "semantic scholar", "scholar.google",
))
_DOCS_TRIGGERS = tuple(kw.lower() for kw in (
    # Library / framework docs lookup via context7 MCP. Includes English +
    # Japanese cues; pre-lowercased so pick_mcp_servers skips .lower() per kw.
    # NOTE: 旧 "docs" (裸4文字の substring) は "pdocs" 等で誤爆したため、
    # 英字が直前に来ない場合のみマッチする _DOCS_BOUNDARY_RE に分離した。
    "context7", "ドキュメント", "公式ドキュメント", "api reference",
    "リファレンス", "ライブラリ", "library", "使い方", "how to use",
    "npm package", "pypi", "github.com/",
))
# "docsを見たい" / "の docs" は正当なトリガー、"pdocs" / "READMEのpdocsが" は誤爆。
# substring では区別できないので「直前が英字でない docs」のみ拾う。
_DOCS_BOUNDARY_RE = re.compile(r"(?<![a-z])docs")
# IOT tier — Nature Remo Cloud API。haroin 自宅の家電制御・センサー読み取り。admin-only、
# 自宅 IoT 操作を語った時だけ spawn する。
_IOT_TRIGGERS = tuple(kw.lower() for kw in (
    "エアコン", "クーラー", "暖房", "冷房", "照明", "電気を", "テレビ", "tv ",
    "リモコン", "remo", "nature remo", "家電", "リビング",
    "室温", "気温", "湿度", "温湿度", "湿度計", "温度計",
    "アプライアンス", "appliance", "アクテュエータ", "actuator",
))

# ALARM tier — kanae-alarm（Mac 目覚まし: 大音量 YouTube 再生）。個人ゲート
# KANAE_ALARM_ALLOWED_USER_IDS（既定で haroin 単独）を語った時だけ spawn する。
_ALARM_TRIGGERS = tuple(kw.lower() for kw in (
    "アラーム", "目覚まし", "めざまし", "目覚め", "起こして", "叩き起こ",
    "alarm", "wake me", "wake up", "wakeup", "wake-up",
))


# REALESTATE tier — SUUMO/HOMES 物件検索 MCP. admin-only かつ trigger 必須.
# 一般ユーザーが trigger word を打っても user_id ゲートで弾かれる二重防御。
# ADR-0020、realestate-mcp/IMPLEMENTATION_PLAN.md。
_REALESTATE_TRIGGERS = tuple(kw.lower() for kw in (
    "不動産", "物件", "賃貸", "suumo", "homes", "ホームズ",
    "家賃", "間取り", "敷金", "礼金", "1ldk", "2ldk",
    "目黒区", "世田谷区", "品川区", "大田区",
    "大岡山", "祐天寺", "中目黒",
))


# INSHI tier — 院試誤答DB (inshi-vault :8017)。院試関連の語だけで spawn する
# （常駐 daemon への HTTP 接続なので spawn コスト自体はほぼゼロだが、
# 無関係な会話でツール枠を8個食わないためのゲート）。
_INSHI_TRIGGERS = tuple(kw.lower() for kw in (
    "院試", "過去問", "誤答", "模試", "ドリル", "間違いノート",
    "出題", "類題", "改題", "復習期限", "誤答db", "inshi", "白紙再現",
    "問a", "問b", "問c", "問4", "問6", "問7",
    "ポンピング", "双対", "シンプレックス", "mle", "採点",
))

# MAIL tier — agentic-inbox（haroin 個人の Workers メールサーバ）。メール操作を
# 語った時だけ、AGENTIC_INBOX_ALLOWED_USER_IDS の user のみ接続（fail-closed）。
_MAIL_TRIGGERS = tuple(kw.lower() for kw in (
    "メール", "mail", "inbox", "受信箱", "受信トレイ", "下書き", "draft",
    "件名", "宛先", "差出人", "返信して", "agentic",
))


def get_mcp_servers_by_keys(keys: list[str]) -> dict:
    """Reconstruct MCP server config dict from a list of server name keys."""
    return filter_enabled_mcp_configs(
        {k: v for k, v in MCP_SERVERS.items() if k in keys}
    )


def pick_mcp_servers(raw_user_text: str, user_id: int | None = None) -> dict:
    """Return the minimal MCP server set needed for this request.

    BASE is always loaded. WEB / HEAVY / MEMORY tiers are added only when the
    user text actually mentions something that would need them. The huge win
    is NOT loading playwright (~500MB chromium) or ChromaDB on every chat
    reply.

    ``user_id`` (Discord snowflake) gates the REALESTATE tier: only when the
    sender is in ``ADMIN_USER_IDS`` AND the text mentions a realestate trigger
    do we spawn the SUUMO/HOMES MCP. Default ``None`` keeps the tier off, so
    callers that haven't been updated yet (e.g. legacy SDK path) stay safe.
    """
    text = (raw_user_text or "").lower()
    servers = dict(MCP_SERVERS_BASE)
    if any(kw in text for kw in _WEB_TRIGGERS):
        servers.update(MCP_SERVERS_WEB)
    if any(kw in text for kw in _HEAVY_TRIGGERS):
        servers.update(MCP_SERVERS_HEAVY)
    if any(kw in text for kw in _MEMORY_TRIGGERS):
        servers.update(MCP_SERVERS_MEMORY)
    if any(kw in text for kw in _INSHI_TRIGGERS):
        servers.update(MCP_SERVERS_INSHI)
    if any(kw in text for kw in _PAPER_TRIGGERS):
        servers.update(MCP_SERVERS_PAPER)
    if any(kw in text for kw in _DOCS_TRIGGERS) or _DOCS_BOUNDARY_RE.search(text):
        servers.update(MCP_SERVERS_DOCS)
    if (
        user_id is not None
        and user_id in ADMIN_USER_IDS
        and any(kw in text for kw in _REALESTATE_TRIGGERS)
    ):
        servers.update(MCP_SERVERS_REALESTATE)
    # IOT は admin 全員ではなく **NATURE_REMO_ALLOWED_USER_IDS** だけ（個人 IoT の安全装置）。
    # env が空なら fail-closed で誰も spawn できない。
    if (
        user_id is not None
        and user_id in NATURE_REMO_ALLOWED_USER_IDS
        and any(kw in text for kw in _IOT_TRIGGERS)
    ):
        servers.update(MCP_SERVERS_IOT)
    # ALARM は admin 全員ではなく KANAE_ALARM_ALLOWED_USER_IDS だけ
    # （haroin の Mac を大音量で鳴らす個人アクション。既定で haroin 単独）。
    if (
        user_id is not None
        and user_id in KANAE_ALARM_ALLOWED_USER_IDS
        and any(kw in text for kw in _ALARM_TRIGGERS)
    ):
        servers.update(MCP_SERVERS_ALARM)
    # MAIL は admin 全員ではなく AGENTIC_INBOX_ALLOWED_USER_IDS だけ
    # （haroin 個人の受信箱。送信系ツールも生えているので二重に絞る）。
    if (
        user_id is not None
        and user_id in AGENTIC_INBOX_ALLOWED_USER_IDS
        and any(kw in text for kw in _MAIL_TRIGGERS)
    ):
        servers.update(MCP_SERVERS_MAIL)
    return filter_enabled_mcp_configs(servers)


async def select_mcp_servers(
    raw_user_text: str, user_id: int | None = None,
) -> dict:
    """MCP サーバー選択の dispatcher（MCP_SELECTOR_MODE で方式を切替）。

    - keyword: 従来のキーワード部分一致のみ
    - semantic: 埋め込み類似のみ（例外時は keyword へフォールバック）
    - hybrid（既定）: 両者の和集合。keyword の recall を維持しつつ、
      キーワードに引っかからない言い回しを埋め込みが拾う。

    埋め込み呼び出し（~50-150ms）は entry.py で Mem0 recall 等と並行するため
    体感 TTFT への影響はほぼ無い。
    """
    from config import MCP_SELECTOR_MODE

    keyword = pick_mcp_servers(raw_user_text, user_id=user_id)
    if MCP_SELECTOR_MODE == "keyword":
        return keyword
    try:
        from services.mcp_semantic_selector import pick_mcp_servers_semantic
        semantic = await pick_mcp_servers_semantic(raw_user_text, user_id=user_id)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "semantic selector failed → keyword fallback: %s", exc,
        )
        return keyword
    if MCP_SELECTOR_MODE == "semantic":
        return filter_enabled_mcp_configs(semantic)
    merged = dict(keyword)
    merged.update(semantic)
    return filter_enabled_mcp_configs(merged)
