# みんかぶFX OANDAオーダーブック MCP 実装計画

- 作成日: 2026-06-24
- 起案: haroin（依頼）/ かなえ（設計）
- 関連: yfj-quote / nikkei-indexes / polymarket-mcp と同思想（薄いfetch shell + パーサ隔離）

## 1. 背景・目的

ドル円のオーダーブック（バタフライ）を、株価を `get_quote` で引くのと同じ感覚で
fetch できる MCP にする。2026-06-24 に手作業で
`https://fx.minkabu.jp/api/v2/oanda/{position,order}/USD_JPY` を叩いてバタフライを
描いた（`/tmp/discord_bot_attachments/usdjpy_butterfly_v2.png`）が、毎回 curl + 自前
パースは片手間すぎる。MCP に切れば、かなえが会話中に「ドル円のショートどこ？」で
即引ける／先輩の Claude Code からも他クライアントからも使える。yfj-quote と顔ぶれが
揃うのも綺麗。

## 2. 調査で確定した仕様（2026-06-24 実測）

### エンドポイント
```
https://fx.minkabu.jp/api/v2/oanda/{type}/{instrument}
```
- 認証不要、JSON 直返し
- react-on-rails の `OandaOrderBookWidget` がこのAPIを叩いている

### 有効な type は **order / position の2つだけ**
| type | 中身 |
|---|---|
| `order` | 未約定注文（指値・逆指値）の価格帯別分布 |
| `position` | 建玉（ロング・ショート）の価格帯別分布 |

`summary` / `technical` / `heatmap` / `sentiment` / `volume` / `candle` / `price` /
`ratio` は **全て `{}`（空）**。みんかぶは **存在しないルートも HTTP 200 + 空JSON で返す**
作りなので、type 名の推測探索は無意味。本物はこの2つで打ち止め。

### 有効な instrument は **10ペア + 金**
```
USD_JPY EUR_USD GBP_USD EUR_JPY GBP_JPY
AUD_JPY AUD_USD USD_CHF EUR_GBP XAU_USD(金)
```
- NZD_JPY / CAD_JPY は HTTP 500（HTMLエラーページ）＝**非対応**
- 金（XAU_USD）が取れるのが地味に重要。先輩が金の底を狙っているので、金のオーダー
  ブックも同じツールで見られる

### データ構造
```json
{
  "bucketWidth": "0.0500",
  "buckets": [
    {"price": 161.55, "longCountPercent": 204.86, "shortCountPercent": 323.67},
    ...
  ]
}
```
- `bucketWidth`: 価格刻み（FX=0.05円、金は別単位）
- `longCountPercent` / `shortCountPercent`: その価格帯のロング/ショート比率(%)
- **外れ値に注意**:
  - `order` は先頭に `price=0`（成行注文）が入る
  - `position` は `price=76〜78` など過去の古い建玉が混じる
  - → ズーム範囲フィルタ（現在値±N円）が必須。パーサ層に閉じる

## 3. MCP ツール設計

### ① fetch層（yfj-quote と同じ顔ぶれ）
- `get_orderbook(instrument)` — 注文(order)の生 buckets を返す。get_quote 相当
- `get_positions(instrument)` — 建玉(position)の生 buckets を返す
- `list_instruments()` — 対応10ペアを返す（ハードコード + 疎通確認）

### ② 解析層（MCP内で完結する薄い付加価値）
- `analyze_orderbook(instrument)` — 現在値付近にズームし自動抽出:
  - ショート/ロング集中価格（ピーク）と比率
  - L/S 比（売り買いどちらが優勢か）
  - 売り壁（上の抵抗）/ 買い指値の山（下の支持）
  - 現在値（yfj-quote or yfinance で補完）からの距離
  - → 「どこにショートが固まってるか」を構造化して一発で返す。
    2026-06-24 に手でやったバタフライ分析のツール化。介入アラートの入力にもなる

> 描画（matplotlib バタフライ）は MCP に入れない。MCP は数値を返す薄いシェルに徹し、
> 可視化は呼び出し側（bot / Claude Code）に任せる。yfj-quote が現値だけ返して描画は
> 外でやるのと同じ分離。

## 4. パーサ隔離（壊れ方まで設計する）

- `minkabu_parser.py` に分離。bucket スキーマは非公開でいつ変わるか分からない
- 外れ値除去（price=0 の成行、過去建玉の 76円台）もここに閉じる
- bucketWidth が文字列で来る（"0.0500"）等の癖もここで吸収
- 壊れたら 1ファイル直せば済む構造にする

## 5. 置き場所・技術スタック

- `/home/ubuntu/mcp-servers/minkabu-fx/`（既存 MCP に合わせる）
- Python + FastMCP、httpx 直叩き、当日キャッシュ（TTL 数分）
- yfj-quote を雛形にすれば fetch 3点セットは早い
- User-Agent: 実測では UA なし(`curl -s`)で取得可。礼儀として bot 名乗りの UA を付ける

## 6. モニタリング層（後の Phase / MCP の外）

- 定期取得 → SQLite に時系列保存 → **オーダーブックの変化検出**
- 「162 の売り壁が厚くなった」「161.55 のショートが利確で消えた」を差分で追う
  （日経225建玉の "道C" / VI fwd記録と同じ発想）
- **介入アラートに直結**: ショート集中 + 買い逆指値の山 = 踏み上げ燃料の定量化を
  30分 scheduler（intervention_alert.py）の入力に食わせる。今は ②変動ペースが
  ±2σ だけだが、「オーダーブックの偏り」を新シグナルとして足せる

## 7. 利用規約・留保

- スクレイピング（規約グレー帯）。**個人利用・低頻度(1req/秒)・当日キャッシュ・再配布なし**
  に縛る
- みんかぶの規約変更 / API 廃止リスクあり。パーサ隔離で被害を局所化
- OANDA 公式 ForexLabs API でも同データは取れるが口座トークンが要る。無料・無認証の
  みんかぶ経由を一次ソースにし、壊れたら ForexLabs を代替に検討

## 8. 実装順序・見積り

| Phase | 内容 | 見積り |
|---|---|---|
| 1 | fetch層3点 + minkabu_parser 隔離 | 2-3h |
| 2 | analyze_orderbook（解析層） | 1-2h |
| 3 | モニタリング層（SQLite時系列 + 変化検出 + 介入アラート連携） | 別途 |

**Phase 1 から。** これだけで「FXのバタフライを株価みたいに fetch」は達成。
yfj-quote が最初スナップショットだけで後から育ったのと同じ進化。

## 9. 未決事項

- 金(XAU_USD)の bucketWidth / 単位の確認（FXと刻みが違うはず）
- キャッシュ TTL（みんかぶ側の更新頻度を実測してから決める。数分〜十数分想定）
- analyze_orderbook の「壁」判定閾値（ピーク比 N% 以上、等）のチューニング
