# Polymarket 地政学リスク指標ツール — 実装計画

## Status
計画中（設計判断5点の確定待ち）。Phase 0（API実証）は完了。

## Goal
地政学イベント（イラン核合意・ホルムズ・台湾・露宇 等）の Polymarket 確率を
定期取得し、**時系列で記録 → 変化を検出**して、金・原油・日経・リスクオン/オフの
判断材料にする「常設の温度計」。単発スクリプトではなく継続モニタリング。

## Phase 0: API実証（DONE 2026-06-23）
- `GET https://gamma-api.polymarket.com/public-search?q=iran` で
  `US-Iran Final Nuclear Deal by…?` (id 620025) がヒット。
- description に解決基準（resolution）が明記 → 「定義・期限の罠」をAPIで読める。
- market構造で取れるフィールド:
  - `question`, `outcomes` (["Yes","No"]), `outcomePrices` (["0.0355","0.9645"] = 確率)
  - `volumeNum`, `volume24hr`, `liquidityNum`, `endDate`, `closed`, `active`
- 認証不要・無料・キー不要。技術前提クリア。

## アーキテクチャ（2層構成）
**設計変更 2026-06-23**: bot統合案を破棄し、独立MCP化（haroin判断）。fetch と
モニタリングを別レイヤーに分離。MCPは「今いくつ？」を返す thin shell に徹する。

### ① MCP層（fetch / thin shell）— polymarket-mcp（新規・独立MCP）
yfj-quote と同じ顔ぶれで揃える:
- `search_markets(query)`            : キーワードで地政学市場を検索（public-search）。search_ticker相当
- `get_market(slug|id)`              : 現在の確率・出来高・流動性・期限・解決基準。get_quote相当
- `get_market_history(id, interval)` : 確率の時系列（CLOB API `/prices-history`）。get_price_history相当
- parser は `polymarket_parser.py` に隔離（Gamma/CLOB JSON の解釈をそこだけに閉じる）
- 配置: `/home/ubuntu/mcp-servers/` 配下、Python + FastMCP（yfj-quote / nikkei-indexes と同パターン）
- 思想: 記録・分析は持たない。スナップショット返却に徹する

### ② モニタリング層（後のPhase / 上位レイヤー・MCPの外）
- Phase A: MCPを定期ポーリング → SQLite に確率・出来高・時刻を記録
- Phase B: 時系列チャート（matplotlib）+ 複数期限カーブ + 変化検出
- Phase C: Discord通知（織り込み急変アラート）+ 原油/VIX クロスチェック

## 技術選定
- 言語: Python + FastMCP（既存MCPと同パターン）
- API: Polymarket Gamma API（市場メタ・現在値）+ CLOB API（価格履歴）。無料・キー不要
- 保存: SQLite（モニタリング層、時系列に十分・依存ゼロ）
- 可視化: matplotlib（既存資産、モニタリング層）
- 配信: Discord（既存bot経由、モニタリング層）

## データモデル（SQLite案）
```sql
CREATE TABLE markets (
  market_id  TEXT PRIMARY KEY,
  event_id   TEXT,
  ticker     TEXT,
  question   TEXT,
  theme      TEXT,          -- 'iran_nuclear','hormuz','taiwan' 等のキュレーションタグ
  end_date   TEXT,          -- 解決期限
  resolution TEXT,          -- 解決基準（description）
  active     INTEGER,
  created_at TEXT
);
CREATE TABLE snapshots (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id  TEXT,
  ts         TEXT,          -- 取得時刻 ISO8601 +0900
  yes_price  REAL,          -- outcomePrices[0]
  volume     REAL,          -- volumeNum
  volume_24h REAL,
  liquidity  REAL,
  FOREIGN KEY(market_id) REFERENCES markets(market_id)
);
CREATE INDEX idx_snap_market_ts ON snapshots(market_id, ts);
```

## 変化検出
- 直近2スナップショットの `yes_price` 差分。閾値（暫定 ±5pt）超で「変化フラグ」。
- 出力は絶対値より**変化**を主役にする（ニュースのインパクト定量化）。

## リスク / 壊れ方の設計
- **API障害**: 指数バックオフ・リトライ。失敗時は前回値保持＋欠損フラグ（捏造しない）。
- **薄商いノイズ**: `liquidityNum` / `volume24hr` 閾値未満は「低信頼」フラグ（除外ではなく注記）。
- **市場同定**: 1テーマに複数market（期限違い）→ theme タグで束ね、期限ごとにカーブ化。
- **解決リスク**: グレーな決着で resolution が揉める → `closed` 監視＋解決結果を記録、確率0/1張り付きは別扱い。
- **期限切れ**: endDate 経過で自動アーカイブ。

## 未決定の設計判断（要・先輩確認）
1. 置き場所: かなえbot統合（`/geo`コマンド＋通知）か、独立ツール（cron）か
2. 対象市場: 手動厳選か、キーワード自動探索か
3. 出力形態: 定期サマリー / 変化アラートのみ / オンデマンド
4. ポーリング頻度: 6hごと想定（地政学は分刻みでない）
5. 原油/VIXクロスチェック: Phase 1から組むか、後回しか
