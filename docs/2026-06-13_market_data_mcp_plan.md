# 市場データMCP 実装プラン（日経公式 + Yahoo Finance Japan）

作成: 2026-06-13 / 依頼: haroin
前提検証済み: **両サイトともGraphQLではない**（実測 2026-06-13）。

## 実測アーキテクチャ（プランの根拠）

| サイト | 実体 | 取れる経路 |
|---|---|---|
| indexes.nikkei.co.jp | 静的ページ + WAF。動的APIなし（/graphql等は全403） | **ヒストリカルCSV直GET**（200, UA必須, cp932） |
| finance.yahoo.co.jp | SSR + `__PRELOADED_STATE__` 埋め込みJSON（119KB中に株価全部入り） | **ページGET → JSON抽出**（GraphQL露出なし） |

## MCP 1: nikkei-indexes（日経公式CSV）

**目的**: 日経VI・日経平均ほか日経算出指数の日次OHLCを一次ソースから取る。
VI戦略（ADR-0011 rev2）の発火判定・バックテストのデータ基盤。

**ツール**:
- `get_index_history(idx, days)` — CSVを取得しOHLCのJSONで返す
  - 既知URL: `https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_vi_daily_jp.csv`（日経VI）
  - `nikkei_stock_average_daily_jp.csv`（日経平均）。命名規則 `nikkei_<index名>_daily_jp.csv`
- `check_vi_signal()` — ADR-0011の発火3条件（VI始値≥base / VI終値>始値 / 日経終値<始値）を
  当日データで判定して返す。base はパラメータ（既定30、Open Question中）
- `list_known_indexes()` — 動作確認済みCSV名の台帳

**実装メモ**:
- httpx + `User-Agent` 必須（無いと403）。encoding=cp932。
- CSVは確報（引け後更新）。**ザラ場の速報値は取れない** → 速報はフィスコ/Yahoo側で補完（二段運用）。
- キャッシュ: 当日18時まで前営業日CSVをローカルキャッシュ（連打で日経のWAFを刺激しない）。

## MCP 2: yfj-quote（Yahoo Finance Japan）

**目的**: 個別株・指数・為替の現在値照会。毎回Brave検索でページを引く現状の置き換え。

**ツール**:
- `get_quote(code)` — `https://finance.yahoo.co.jp/quote/{code}` をGET、
  `__PRELOADED_STATE__` のJSONを正規表現で切り出して priceBoard 相当を返す
  （現在値・前日比・始値高値安値・出来高・時価総額）
- `search_ticker(query)` — Yahooの銘柄検索ページから候補コードを返す
- `get_fisco_vi_news()` — フィスコの日経VI速報記事を検索して日中の高安/現在値を抽出
  （日経VIのザラ場速報の唯一の無料経路。ADR-0011の速報側を担う）

**実装メモ**:
- `__PRELOADED_STATE__` はスキーマ非公開・予告なし変更があり得る。**パーサを1ファイルに隔離**し、
  壊れたら该当ファイルだけ直す構造に（壊れ方まで設計する）。
- レート: 1req/秒・リトライ1回。bot用途は照会頻度が低いので問題なし。
- 注意: スクレイピングはYahoo/日経の利用規約のグレー帯。**個人利用・低頻度・キャッシュ前提**に留める。
  再配布や高頻度ポーリングはしない。

## 共通設計

- 配置: `/home/ubuntu/mcp-servers/market-data/`（news-curator / x-scraper と同列）
- FastMCP (python) + httpx。テスト: 取得系はレスポンス固定のfixtureでユニットテスト。
- bot側 `.mcp.json` に登録 → 再起動で有効化。
- 段階: Phase 1 = nikkei-indexes（VI戦略に直結、CSVで壊れにくい）→
  Phase 2 = yfj-quote（パーサ脆弱性があるので後）。

## 見積り

- Phase 1: 実装+テスト 1〜2時間
- Phase 2: 2〜3時間（__PRELOADED_STATE__ の構造調査込み）
