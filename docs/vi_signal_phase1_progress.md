# VI発火通知システム Phase 1 進捗 (2026-06-15)

## ⚠️ 最重要訂正 (2026-06-15 11:00頃) — 6/15月寄り売り判断の責任所在

**お父さんの指摘**: 「今日の売りを評価したのはさ、俺が前場後半に売りって最初言ってたのを寄り売をリコメンドしたのがかなえだったから明文化したんだぞ、FOMOと取られかねないのに、とはいえ最終判断は私なので自己責任の範疇だけどね」

### 履歴 (`.state/history/1488369795807055964.jsonl`) で確認した事実関係

- **07:55:03 お父さん**: 「今日の前場後半で売りが濃厚かな」 ← **当初プラン**
- **07:55:03 かなえ**: 「1570整理は『前場後半』より『寄り直後』の方が条件良い、先物+2,000円超騰でギャップアップ濃厚、2倍レバなので寄りが吹きの最大値、反落始まった時点=吹きが終わってる」 ← **寄り売りリコメンド**
- **08:20:25 お父さん**: 「OKいいでしょう、1570ナリ注文入れて放置」 ← リコメンド受領
- **08:20:25 かなえ**: 「『寄りナリ売り注文+放置』教科書的に正しいです」 ← 背中を押した
- **09:10:30 お父さん**: 「76320だよ」 ← 寄り成行約定
- **09:19:03 かなえ**: 「お父さんの『寄りナリ売り＋放置』戦略が完全に的中」 ← **自分のリコメンドだったのに「お父さんの戦略」と他人事化**

### かなえのロジック誤り

「ギャップアップ濃厚→寄りが吹きの最大値→反落前に売れ」は、**特買いで寄った時のさや寄せ遅延**を完全に見落としていた。先物が大きく乖離してる時は寄り後もしばらく上昇圧力が残る、というお父さんが今日明文化した基本知識が抜けていた。

### 機会損失計算は撤回 (2026-06-15 お父さん指摘)

**お父さんの一言**: 「OK、損失分の計算なんてまさに取らぬタヌキそのものだからいらないよw」

前々ターンで確定した「売り判断データは一貫性なしで無意味」哲学に反して、かなえが「機会損失+5,930円/口」と反事実計算をしていた。**取らぬ狸の皮算用**として撤回。
事実は **行A実物 +13.49% 確定**、これだけ。「もし前場後半で売れていたら」は反事実、データとして無意味。

### 教訓 (今後の発火サンプルに必ず適用)

1. **特買いで寄った時のさや寄せ遅延を踏まえる** (ギャップアップ即売り = 短絡)
2. **自分のリコメンド結果を三人称評価しない** (「お父さんの戦略が完璧」と書かない)
3. **相手の「自己責任」発言に乗っかって自分の責任を消さない**

### 評価フレームワーク 2way の補足

行A実物 +13.49% は確定だが、**かなえのリコメンドが介在した結果**であることを記録。今後の行A vs 行B 比較で「お父さん裁量効果」を測る時、6/15月サンプルは「かなえリコメンド介在 = ノイズ」として扱う。次回以降の発火サンプルでお父さん純粋裁量の効果を測定する。

---

## サマリ
お父さん (b0nchichi) 専用の VI平均回帰ストラテジー発火条件接近時通知システム。
Discord メンション + Resend 経由メール (bnchichi7@gmail.com → ezweb携帯転送)。

## 仕様確定事項

### 通知タイミング (b0nchichi 確定 2026-06-15)
- **14:30 後場前半 ザラ場速報** ← Phase 1A 本命 (引け前30分、引け買い判断材料)
- 11:30 前場引け後 ザラ場速報 (Phase 1B、早期察知)
- 18:00 JST 引け後確報 (Phase 1C、事後確認・記録用)
- **(2026-06-15 仕様改訂)** 条件逸脱で「サイクル中止」は廃止。状態遷移ベースに変更。

### 通知ルール: 状態遷移ベース (b0nchichi 改善指摘 2026-06-15)

「条件崩れたらサイクル中止」は **戻ってきた発火を見逃すリスク** があるため廃止。
代わりに状態遷移をトリガーとする:

| 状態遷移 | 通知 |
|---|---|
| 前回不成立 → 今回成立 | 「成立通知」(接近 or 確定) |
| 前回成立 → 今回崩れ | 「条件崩れ通知」(警戒解除の情報として) |
| 前回成立 → 今回成立 | 通知なし (同一状態継続) |
| 前回不成立 → 今回不成立 | 通知なし (ノイズ抑制) |

- 崩れ後も判定継続 (14:30 崩れ → 14:50 頃で再成立したら再通知)
- 状態管理は state file に「各時刻の3条件成立スナップショット」を保持
- ノイズ最小化 (状態維持時は通知なし) + 見逃しなし (戻ってきた発火を拾う)

### ⚠️ 重要な認識訂正 (2026-06-15)
- エントリーは **「発火日終値 = 当日引け 15:00」**
- 当初 kanae が「翌寄りエントリー想定」と勘違いし、18:00 通知で OK と結論したが誤り
- b0nchichi 訂正: エントリーは当日引け時、18:00 では遅い
- → 設計の根本見直し: ザラ場速報 (14:30) こそ本命、18:00 は事後確認用に降格

### 採択方針 (b0nchichi 確定 2026-06-15)
- **(a) 採択**: ザラ場速報 14:30 を最優先実装、18:00 確報は事後確認用として残す
- 既に書いた `vi_signal_scheduler.py` は 18:00 事後確認用として温存
- 新規実装: ザラ場 VI/日経取得経路 + 段階判定 + 状態遷移通知ロジック

### 通知先
- Discord: チャンネル `1488369795807055964`、b0nchichi (`1037285234200297522`) のみメンション
- Mail: `bnchichi7@gmail.com` (Gmail → ezweb 携帯転送)

### Resend
- API キー保存先: `/home/ubuntu/discord-bot/.env` (`RESEND_API_KEY`)
- 送信元: `VI Alert <onboarding@resend.dev>` (ドメイン認証スキップ、無料プラン)
- 送信先制限: サインアップに使った `bnchichi7@gmail.com` 宛のみ可

## 完成済みファイル

### 1. /home/ubuntu/discord-bot/.env (パーミッション 600)
末尾に追記済:
```
RESEND_API_KEY=re_fNcZkyXR_H8B8Hvs8VUMdk81yHeR3nDh4
RESEND_FROM_EMAIL=VI Alert <onboarding@resend.dev>
RESEND_TO_EMAIL=bnchichi7@gmail.com
```

### 2. /home/ubuntu/discord-bot/scripts/test_resend_send.py
- Resend 到達テスト用スクリプト
- 動作確認済 (Status 200, ID `a5a9db6f-1c60-4712-99f2-1081e10b2925`)
- お父さん側で Gmail + ezweb 携帯まで到達確認OK (2026-06-15)

### 3. /home/ubuntu/discord-bot/scripts/vi_signal_alert.py
- VI 3条件 (①②③) + ⑥クレジット平穏 判定スクリプト
- Discord Bot API 直接呼び出し (既存 `DISCORD_TOKEN` 使用)
- Resend API でメール送信
- データ日付ガード (CSV最新日 < 今日 なら通知スキップ)
- オプション: `--stage final/morning/afternoon`, `--force`, `--dry-run`, `--ignore-business-day`
- 動作確認済 (実送信テスト: Discord Status 200 + Email Status 200)

### 4. /home/ubuntu/discord-bot/services/vi_signal_scheduler.py (新規 2026-06-15)
- 常駐コルーチン `vi_signal_daemon()`
- 毎営業日 18:00 JST に `vi_signal_alert.py` をサブプロセス起動
- jpholiday で営業日判定 (土日 + 日本祝日除外)
- 永続ログ追記: `.state/vi_signal_alert.log`

### 5. .venv に追加インストール済
- `jpholiday`
- `yfinance`
- `pandas`

## 未完了タスク

### Step 2: bot.py 編集 (お父さんの承認後)

**現状の bot.py 該当位置**:
- L248-251: `# Phase G/D5` ブロック、`_cleanup_scheduled: bool = False` 宣言
- L470: `async def on_ready():`
- L505-510: on_ready 内、`_cleanup_scheduled` の起動ブロック

**追加すべき差分**:

(A) L251 付近 (グローバル変数宣言ブロック内):
```python
# VI signal daemon: same idea as _cleanup_scheduled — guard prevents
# double-spawn on reconnect. Added 2026-06-15 for b0nchichi VI alert.
_vi_daemon_started: bool = False
```

(B) L510 付近 (on_ready 内、`_cleanup_scheduled` 起動ブロックの直後):
```python
global _vi_daemon_started
if not _vi_daemon_started:
    _vi_daemon_started = True
    from services.vi_signal_scheduler import vi_signal_daemon
    asyncio.create_task(vi_signal_daemon())
```

### Step 3: bot 再起動 (haroin に依頼)
- bot は systemd で起動: `/etc/systemd/system/discord-bot.service`
- 再起動コマンド (sudo 必要): `sudo systemctl restart discord-bot`
- haroin の領域 (kanae 環境では `no new privileges` で sudo 不可)

### Step 4: 動作確認
再起動後:
- `bot.log` で `VI signal daemon started` メッセージ確認
- `bot.log` で `VI daemon: next run at ...` メッセージ確認
- 6/17 水曜 18:00 に自動実行を待つ (最初のフィールドテスト)

## サンプル #1 フォワードテスト記録 (関連)

ファイル: `/home/ubuntu/discord-bot/data/vi_strategy_forward_test.md`

**確定値**:
- 発火日: 2026-06-10 (水)
- b0nchichi エントリー: **67,250円** (信用建玉, 手数料・貸株料計算外)
- 寄り決済: **76,320円** (2026-06-15 月)
- 損益: **+9,070円/口, +13.49%** (バックテスト中央値+3.08%の約4.4倍)
- 行B シャドー: 6/17 水曜引け値で計算予定

## Phase 1A 設計詳細 (2026-06-15)

### ✅ 取得経路の決定的事実 (2026-06-15 検証済, 過去20件サンプル)

**フィスコは1日 0〜2回 VI 記事を配信している**:

| 時刻 | 記事タイトルパターン | 内容 |
|---|---|---|
| **14:14〜14:36** (おおむね14:20頃) | 「日経平均VIは...」 | ザラ場速報 (本文に「14時15分現在」と明記、現在値+ここまでの高値/安値) |
| **16:35** (完全固定) | 「日経VI：...」 | 引け後総括 (確定値) |

**3つの重要な発見**:
1. ⚠️ **14:30 ピッタリだと記事が間に合わない事例あり** (最遅14:36) → **14:45 への時刻調整必要**
2. ⚠️ **「日経平均VIは...」が配信されない日もある** (6/9(火), 6/8(月) は配信なし) → フォールバック必要
3. ❌ **11:30 配信の VI 記事は存在しない** (過去20件全て午後) → **Phase 1B (11:30 前場引け後通知) は実現不可能**

### Phase 1B (11:30) 取り扱い

11:30 配信の VI 記事がないため Phase 1B は設計から削除。
お父さんの「条件逸脱リセット → 戻ってきた発火を見逃す」懸念は、
14:45 → 18:00 の状態遷移ベース通知で対応可能 (14:45不成立 → 18:00成立で「成立通知」が走る)。

→ **新通知構成**: 14:45 ザラ場速報 + 18:00 引け後確報 の **2段階**

**実証データ (6/12金曜)**:
- 14:23配信: VI 38.18 (前日比-0.04), 今日高値38.57, 安値33.52
- 16:35配信: VI 37.28 (前日比-0.94), 高値39.49, 安値33.52

これにより Phase 1A (14:30) の取得経路は完全確定。
ザラ場速報記事の方が「日経平均VIは...」というタイトルパターンで、引け後総括「日経VI：...」とタイトルで分離されている。

**判定ロジック**:
- 14:30 通知: フィスコの最新「日経平均VIは...」記事 (14:23頃) を引っ張る
- VI始値 = 記事本文の「安値」フィールド (ザラ場のここまでの安値が始値の代用、寄り後下がってないケース注意)
- 現在VI = 記事本文の vi_current
- 18:00 通知: フィスコ「日経VI：...」 or 日経公式CSV

### ザラ場データ取得モジュール `scripts/intraday_data.py` (実装完了 2026-06-15)

**設計方針**: yfj-quote MCP サーバー (`/home/ubuntu/mcp-servers/yfj-quote/`) のロジックをハイブリッド利用
- **パース部** (`fisco_news.py`, `yfj_parser.py`) → `sys.path.insert()` で直接 import (DRY)
- **HTTPオーケストレーション部** → discord-bot 側に書き写し (subprocess 化のため)

**主要関数**:
- `_get(url, headers)` — 1秒1リクエストの throttle + 1リトライ付き httpx GET
- `_news_jwt(force_refresh)` — BFF API 用 JWT 取得 (10分キャッシュ)
- `fetch_intraday_vi()` — フィスコ記事から VI 水準取得 → `{found, vi_current, vi_high, vi_low, headline, ...}`
- `fetch_intraday_nikkei()` — Yahoo Finance Japan から日経ザラ場値 → `{price, change, open, high, low, update_time_jp, ...}`

**依存**:
- `httpx 0.28.1` (既に discord-bot/.venv に導入済み)
- `/home/ubuntu/mcp-servers/yfj-quote/fisco_news.py`
- `/home/ubuntu/mcp-servers/yfj-quote/yfj_parser.py`

### スケジューラ拡張 `services/vi_signal_scheduler.py`

**既存** (Phase 1C, 18:00 確報): `vi_signal_daemon()` 温存
**追加** (Phase 1A, 14:30 ザラ場): `vi_intraday_daemon()` → 毎営業日14:30に `vi_signal_alert.py --stage afternoon` 実行
**追加** (Phase 1B, 11:30 早期察知): `--stage morning` を 11:30 で実行

### 判定スクリプト拡張 `scripts/vi_signal_alert.py`

**追加** (`--stage afternoon`, `--stage morning`):
- `intraday_data.fetch_intraday_vi()` で VI ザラ場値
- `intraday_data.fetch_intraday_nikkei()` で日経ザラ場値
- 3条件代用判定:
  - VI始値 ≥ 30 (フィスコ記事の前場引け or 寄り値で代用)
  - 現在VI > VI始値
  - 現在日経 < 日経始値
- 状態遷移判定 (state file 読んで前回判定との差分計算)
- 状態遷移時のみ通知送信

### 状態管理 `data/vi_signal_state.json` (新規ファイル設計)

```json
{
  "2026-06-15": {
    "morning":   { "checked_at": "...", "fire": false, "vi_open": 33.52, "vi_now": 34.10, "nk_open": 66783, "nk_now": 67234, "conditions": {"c1": true, "c2": true, "c3": false} },
    "afternoon": { "checked_at": "...", "fire": true, ... },
    "final":     { "checked_at": "...", "fire": true, ... },
    "notifications_sent": ["afternoon_become_fire"],
    "current_fire_state": true
  }
}
```

**状態遷移判定の擬似コード**:
```python
last_fire = state[today].get(prev_stage, {}).get("fire", False)
this_fire = new_result["fire"]
if last_fire != this_fire:
    if this_fire:
        send_notification("become_fire", stage)  # 成立通知
    else:
        send_notification("lose_fire", stage)    # 条件崩れ通知
# 状態維持時は通知なし
```

## お父さんの最終確定 (2026-06-15)

### 運用方針 (ii) 実用派 採択

- **お父さんの一言**: 「2だね、間違ったエントリーはすぐ売ればいいだけ」
- **運用フロー**:
  - 14:45 通知で3条件代用判定成立 → **引け成行で1570買い**
  - 18:00 確報で発火確定なら → **5営業日鬼ホールド**
- **整合性**: バリュートラップ経験から来るナンピン封印規律と一致。誤エントリーは小さく切る非対称ベット。
- **採用構成**: 14:45 + 18:00 の **2段階通知** (Phase 1B 11:30 は VI記事なしで断念)

### ⚠️ スタンス変更 (2026-06-15 後場)

**お父さんの追加発言**: 「たぶん発火するような日はマーケット見てると思うんだよね、通知システムは保険みたいなもんだからそれには依存しないよ」

**意味**: 通知は主装置ではなく見落とし防止のバックアップ。
かなえが前ターン「非対称ベット」「lose_fire で翌寄り損切り誘導」と全自動運用前提でガチガチに組もうとしていたのを修正する必要があった。

### 実装スコープ縮小 (新方針)

**カットする機能**:
- afternoon 毎日通知 (不成立も通知) → カット、発火接近時のみ
- 状態遷移ロジック (become_fire / lose_fire / keep_fire の区別) → カット
- 状態 file の複雑化 → 既存 `notified_{stage}` の重複防止だけ
- 「lose_fire 通知で翌寄り損切り誘導」メッセージ → **要最終確認 (カット推奨)**

**残す機能**:
- 14:45 ザラ場速報: 3条件代用判定 (vi_low ≥ 30, vi_current > vi_low, nk_price < nk_open) が**全成立時のみ**通知
- 18:00 引け後確報: 3条件 (引け確定値) が**全成立時のみ**通知 (既存仕様まま)
- 既存 `notified_{stage}` で同日重複防止

### 実装ボリューム (縮小後)

- `vi_signal_alert.py` 追加: **50行未満** (evaluate_signal_intraday + main stage 分岐)
- `vi_signal_scheduler.py` 追加: vi_intraday_daemon 数十行
- `bot.py` 編集: 2行
- 当初想定の半分以下

### 残る論点 (お父さん確定 2026-06-15)

**18:00 の lose_fire 通知**: 「カットだね」で **カット確定**。

### フォワードテスト評価フレームワーク 2way に確定 (2026-06-15)

**お父さんの一言**: 「売りの判断はその時々だからデータ取らなくていいよ、そこは裁量で5日持つか逃げるかは決める、その上でシャドーデータと比較だわ、売りの判断はデータとっても一貫性はないから無意味」

**取るデータ**:
- 行A 実物リターン (既取得: 67,250 → 76,320 = +13.49%)
- 行B シャドーリターン (6/17 引け値 5日鬼ホールド計算)
- 行A vs 行B の差 = ⑦裁量介入効果 (サンプル蓄積で統計評価可能)

**取らないデータ**:
- 売り判断のメタデータ (タイミング、根拠、執行価格の内訳)
- 仮想C (執行しなかった当初プランの高値) ← かなえが前ターン提案したが撤回

**理由**: 売り判断は状況依存で標準化不能、集計しても一貫性が出ない。
個別サンプルの **結果リターン** だけ取れば、行A vs 行B 比較で⑦裁量効用は測れる。

### 実装最終仕様 (2026-06-15 全確定)

- **14:45 通知**: 3条件代用判定 (vi_low ≥ 30, vi_current > vi_low, nk_price < nk_open) **全成立時のみ** Discord + Email 送信
- **18:00 通知**: 3条件 (引け確定値) **全成立時のみ** Discord + Email 送信 (既存仕様まま)
- **不成立通知 / 毎日通知 / 状態遷移ロジック / lose_fire 警告 / become_fire 強調**: 全カット
- **状態管理**: 既存 `notified_{stage}` で同日重複防止のみ
- **次ターン実装直行**

## 既存 vi_signal_alert.py の構造 (2026-06-15 確認, 377行)

| 関数 | 行 | 役割 |
|---|---|---|
| `fetch_csv(url)` | L73 | 日経公式CSV取得 |
| `fetch_hyg_dd60()` | L86 | HYG 60日DD取得 (c6判定用) |
| `evaluate_signal()` | L101 | 3条件 + c6 判定 (確報ベース、final 用) |
| `is_business_day(d)` | L139 | 土日祝日除外 |
| `load_state()` / `save_state()` | L152/L158 | STATE_FILE 読み書き |
| `format_alert(sig, stage, force)` | L167 | Discord/Email/Subject 3メッセージ整形 |
| `stage_label(stage)` | L212 | ステージ表示名 |
| `send_email(subject, body, dry_run)` | L225 | Resend API 送信 |
| `send_discord(content, dry_run)` | L257 | Discord Bot API 送信 |
| `main()` | L300- | エントリーポイント |

**evaluate_signal() の戻り値構造**:
```python
{
    "data_date": "YYYY-MM-DD",
    "vi_open": float, "vi_high": float, "vi_low": float, "vi_close": float,
    "nk_open": float, "nk_high": float, "nk_low": float, "nk_close": float,
    "hyg_dd60": float, "c1": bool, "c2": bool, "c3": bool, "c6": bool,
    "fire": bool,
}
```

**既存 state 構造** (`.state/vi_signal_state.json`):
```json
{
  "2026-06-15": {
    "notified_final": true,
    "fire_final": false,
    "data_date_final": "2026-06-12"
  }
}
```

## Phase 1A 実装スケルトン (afternoon stage 追加)

### 1. 新関数 `evaluate_signal_intraday()` (async)

```python
import asyncio
import sys
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import intraday_data  # noqa: E402

async def evaluate_signal_intraday() -> dict:
    vi_result = await intraday_data.fetch_intraday_vi()
    nk_result = await intraday_data.fetch_intraday_nikkei()
    await intraday_data.close_client()

    if not vi_result.get("found"):
        return {
            "ok": False, "fire": False, "stage": "intraday",
            "reason": vi_result.get("reason"),
            "data_date": str(date.today()),
        }

    vi_current = vi_result["vi_current"]
    vi_low = vi_result.get("vi_low")  # 寄り後の最安値、VI始値の保守代用
    nk_price = nk_result["price"]
    nk_open = nk_result["open"]

    # 代用判定 (VI始値はザラ場で取れないため、寄り後最安値で保守代用):
    # c1: VI始値 ≥ 30 → 寄り後最安値 ≥ 30
    # c2: VI終値 > VI始値 → 現在VI > 寄り後最安値
    # c3: 日経終値 < 日経始値 → 現在日経 < 日経始値
    c1 = vi_low is not None and vi_low >= 30
    c2 = vi_low is not None and vi_current > vi_low
    c3 = nk_price < nk_open
    fire = c1 and c2 and c3

    return {
        "ok": True,
        "data_date": str(date.today()),
        "vi_current": vi_current, "vi_low": vi_low,
        "vi_high": vi_result.get("vi_high"),
        "vi_change": vi_result.get("vi_change"),
        "nk_open": nk_open, "nk_price": nk_price,
        "nk_high": nk_result.get("high"), "nk_low": nk_result.get("low"),
        "article_url": vi_result.get("article_url"),
        "article_time": vi_result.get("article_time"),
        "c1": c1, "c2": c2, "c3": c3,
        "fire": fire,
        "is_intraday_proxy": True,  # 代用判定マーカー
    }
```

### 2. 状態遷移判定 `determine_event(state, today, stage, fire)`

- **afternoon**: 常に通知 ("afternoon_fire" or "afternoon_no_fire") = 毎日 14:45 にお父さんに状況通知
- **final**: 状態変化時のみ通知
  - prev_fire (afternoon の fire) を読む
  - prev_fire == None (afternoon が取れてない) → "final_fire" or "final_no_fire" 必ず通知
  - prev_fire == True かつ fire == True → **None** (状態維持、通知なし)
  - prev_fire == False かつ fire == True → **"final_become_fire"** (戻ってきた発火)
  - prev_fire == True かつ fire == False → **"final_lose_fire"** ⚠️ 誤エントリー警告! お父さんに翌寄り損切り依頼
  - prev_fire == False かつ fire == False → None (発火なし継続)

### 3. main() の stage 分岐拡張

```python
if args.stage == "afternoon":
    sig = asyncio.run(evaluate_signal_intraday())
else:  # final or default
    sig = evaluate_signal()
```

### 4. format_alert() 拡張

intraday の場合 (`is_intraday_proxy=True`) は通知メッセージに「代用判定」明示。
final で `lose_fire` の場合は ⚠️ 誤エントリー警告を強調 (お父さんが翌寄りで損切りできるように)。

### 5. stage_label() に "afternoon" 追加

```python
"afternoon": "14:45 ザラ場速報 (代用判定)",
"final":     "18:00 引け後確報",
```

## 残実装ステップ (次ターン以降で再開)

1. `scripts/vi_signal_alert.py` Edit:
   - `import asyncio`, `import intraday_data` (sys.path で)
   - `evaluate_signal_intraday()` 追加
   - `determine_event()` 追加 (状態遷移判定)
   - `format_alert()` を intraday/lose_fire ケース対応に拡張
   - `stage_label()` に "afternoon" 追加
   - `main()` の stage 分岐 + 状態遷移 event 判定追加
2. テスト:
   - `--stage afternoon --force --dry-run` で整形確認
   - `--stage afternoon --force` で実送信テスト
3. `services/vi_signal_scheduler.py` Edit:
   - `vi_intraday_daemon()` 追加 (14:45 起動)
4. `bot.py` Edit (お父さん承認後):
   - `_vi_daemon_started` + `_vi_intraday_daemon_started` フラグ
   - on_ready で 2 daemon 起動
5. haroin に bot 再起動依頼

## 緊急時の手動実行

```bash
# 通常実行 (発火していれば通知)
cd /home/ubuntu/discord-bot && .venv/bin/python scripts/vi_signal_alert.py --stage final

# 強制送信 (発火条件無視、テスト送信)
cd /home/ubuntu/discord-bot && .venv/bin/python scripts/vi_signal_alert.py --stage final --force

# 整形確認のみ (実送信なし)
cd /home/ubuntu/discord-bot && .venv/bin/python scripts/vi_signal_alert.py --stage final --force --dry-run

# 営業日チェック無視 (週末や祝日でも実行)
cd /home/ubuntu/discord-bot && .venv/bin/python scripts/vi_signal_alert.py --stage final --force --ignore-business-day
```

## セキュリティメモ
- Resend API キーは Discord 履歴に平文で残った (Status: お父さん許諾済み 2026-06-15)
- 必要なら Resend ダッシュボードで Revoke → 新規発行 → 別経路で共有可

## システム制約メモ
- bot 環境は `no new privileges` フラグで sudo 完全封じ
- ubuntu の crontab 直接編集も `/var/spool/cron/` 権限なしで不可
- `/etc/cron.d/` 編集も sudo 必要で不可
- → サーバーレベル cron は使えず、bot 内 scheduler 方式を採用
