# dynam_ueda_parser.py

ダイナム長野上田店のスロット稼働データを `min-repo.com` からスクレイピングして CSV 化するスクリプト。

## なぜ作ったか

- 当初は `daidata.goraggio.com` を使う想定だったが、**CloudFront WAF が datacenter ASN を遮断**しているため、bot 側 EC2 から取得不能 (403)
- min-repo.com は residential アクセスなら普通に取れる → **ローカル Claude Code (先輩の Windows PC) で実行する** 設計に切替
- 関連 Issue: #22 (datacenter IP の WAF 遮断対策)

## 用途

- 7/7 上田ダイナム実戦の事前分析
- 直近 N ヶ月の機種別差枚 / バラエティ個別データ取得
- イベント日 (特定日 / スロセレ / 来店ライター) ラベル付与

## 制約

- **bot 側 EC2 では実行非推奨**: rate limit 観点と、長期的にはこの種のスクレイピングを bot に持ち込みたくない
- **min-repo の HTML 構造に依存**: テーブルの並び順 (`table[0]=summary` / `table[1]=by_machine` / `table[2]=variety`) を前提にしてる、構造変化したら正規表現が壊れる
- **イベント日カレンダーは手動メンテ**: `SLOSELE_DAYS` / `WRITER_VISIT_DAYS` は判明分のみ列挙、新しい取材日が判明したらコード内定数に追記

## Usage

```bash
# 依存
pip install requests

# 実行 (直近 2 ヶ月)
python scripts/dynam_ueda_parser.py \
    --start 2026-04-18 \
    --end 2026-06-17 \
    --out ./dynam_ueda_output
```

### 引数

| 引数 | 既定値 | 説明 |
|---|---|---|
| `--start` | (必須) | 取得開始日 YYYY-MM-DD (inclusive) |
| `--end` | (必須) | 取得終了日 YYYY-MM-DD (inclusive) |
| `--out` | `./dynam_ueda_output` | CSV 出力ディレクトリ |
| `--tag-pages` | `5` | min-repo タグページの何ページ目までスキャンするか (1 ページ約 40 件) |
| `--rate-limit` | `3.0` | リクエスト間隔 (秒)、min-repo への負荷配慮 |

## 出力 CSV

### `events.csv` — 日次サマリ

| 列 | 内容 |
|---|---|
| date | YYYY-MM-DD |
| event_type | イベント分類 (下記参照) |
| total_diff | 当日の総差枚 |
| avg_diff | 機種別平均差枚 |
| avg_g | 平均 G 数 |

### `by_machine.csv` — 機種別平均 (2 台以上設置機種)

| 列 | 内容 |
|---|---|
| date | YYYY-MM-DD |
| machine | 機種名 |
| avg_diff | 機種ごとの平均差枚 |
| avg_g | 平均 G 数 |
| win | 勝ち台数 |
| total | 設置台数 |
| payout_rate | 出率 (%) |

### `variety_units.csv` — バラエティ個別 (1 台設置機種)

| 列 | 内容 |
|---|---|
| date | YYYY-MM-DD |
| machine | 機種名 |
| unit_no | 台番 |
| diff | 差枚 |
| g | G 数 |
| payout_rate | 出率 (%) |

### `timeseries.csv` — 機種別時系列 (グラフ用)

| 列 | 内容 |
|---|---|
| date | YYYY-MM-DD |
| machine | 機種名 |
| avg_diff | 機種別平均差枚 (`by_machine` から派生) |

## イベント分類ラベル

- `special_7` : 7 のつく日 (7, 17, 27)
- `special_4` : 4 のつく日 (4, 14, 24)
- `special_5` : 5 のつく日 (5, 15, 25)
- `special_9` : 9 のつく日 (9, 19, 29)
- `slosele`   : スロセレ取材日 (コード内 `SLOSELE_DAYS` で管理、判明分のみ)
- `writer_visit` : 来店ライター来店日 (コード内 `WRITER_VISIT_DAYS` で管理)
- `normal`   : それ以外

複数該当時は `+` 区切り (例: `special_7+slosele`)。

## 既知の限界

- **全台明細フルダンプは取得不可**: 多台数設置機種は機種別平均値のみ (台番別差枚は min-repo では公開されていない)。バラエティ (1 台設置機種) は個別差枚あり。daidata なら多台数機種の台番別データもあるが、WAF 遮断で別途 proxy 設計が必要 (Issue #22)
- **イベント日カレンダーの自動取得は未実装**: スロセレ取材日は手動で `SLOSELE_DAYS` に追記してください

## ライセンス / マナー

- min-repo.com への rate limit は 3 秒間隔を守ってます
- 取得データの公開・再配布は行わない (個人利用に限る)

## 関連

- パーススクリプトの設計・検証: 2026-06-18 セッション (上田ダイナム稼働調査)
- 関連プロンプト: `scripts/PROMPT_local_claude_dynam_ueda.md`
- 関連 Issue: [#22 (WAF 遮断対策)](https://github.com/haroin57/discord-bot/issues/22)
