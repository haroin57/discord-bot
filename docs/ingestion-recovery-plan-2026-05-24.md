# Ingestion 0件問題の復旧計画 (Phase A' / Phase B)

- 起票日: 2026-05-24
- 起票者: かなえ (bot)
- 関連: `/home/ubuntu/discord-bot/docs/mem0-zero-store-debug-2026-05-24.md`

---

## 0. 経緯 (Why this plan)

| 時刻 (5/24) | 出来事 |
|---|---|
| 00:00頃 | weekly_mem0_compact.py の cron 実行、レポートに「過去7日 新規ingestion 0件」表記 |
| 10:00 | namespace ミス起因の `stored 0 new memories` が判明 → 先輩 namespace `573489206240870410` 指定で 3件保存成功 |
| 10:15 | 先輩から「過去7日分のログを ingestion」依頼 |
| 10:30 | 初回診断で「bot.log 5/23 05:14 停止 = logging 壊れ」と誤判断 |
| 10:40 | 訂正: bot.log は pytest 専用、本番は journal 経由。**logging は壊れていない** |
| 10:45 | 方針を Phase A' (原因切り分け) + Phase B (Discord 経由再 ingestion) に再構築、先輩承認 |

**前提崩しを 2回やった**。今回の計画は実行前にチェックポイントを明示し、同じ失敗を避ける。

---

## 1. 計画の全体像

```
Phase A' : 原因切り分け (read-only, blast radius=0)
   ├─ Step 1  : weekly_mem0_compact.py 集計ロジック確認         [10分]
   ├─ Step 2  : batch state ファイル (state/mem0_batch/*.json) 確認 [5分]
   ├─ Step 3  : journal から Mem0 関連ログ抽出                  [15分]
   ├─ Step 4  : 仮説マトリックスのどれかに確定                  [10分]
   └─ Step 5  : 必要なら設定修正 (例: BATCH_SIZE 変更)          [15分]
        ↓
Phase B  : Discord API 経由で過去ログ再 ingestion
   ├─ Step 1  : ingest_channel_history.py に --since オプション追加 [30分]
   ├─ Step 2  : dry-run で対象数確認                            [10分]
   ├─ Step 3  : subject=self で本番投入                         [30-60分]
   ├─ Step 4  : subject=kanae で本番投入                        [30-60分]
   └─ Step 5  : Mem0 ダッシュボード or smart search で投入確認  [15分]
```

**実装規模**: Phase A' はコード変更ほぼゼロ。Phase B は `ingest_channel_history.py` に `--since` 追加 (推定30行) のみ。**新規スクリプト不要**。

---

## 2. Phase A': ingestion 0件問題の原因切り分け

### 2.1 仮説マトリックス

| ID | 仮説 | 検証コマンド | 該当時の対処 |
|---|---|---|---|
| **H1** | batch flush が trigger されてない (turn数 < BATCH_SIZE=10) | `cat state/mem0_batch/*.json` で turn数確認 | BATCH_SIZE 引き下げ or 強制 flush 機構追加 |
| **H2** | flush は呼ばれてるが add_memories まで到達してない | `journalctl _PID=2348895 \| grep "Mem0 batch ingest"` | bot.py:1802 周辺の例外調査 |
| **H3** | add_memories 到達してるが extract_full_signals が facts:[] 返してる | `journalctl _PID=2348895 \| grep -i "extract\|facts"` | extract.py のシステムプロンプト or Haiku モデル状態確認 |
| **H4** | facts 出てるが dedup 全弾き (kept=0) | `journalctl _PID=2348895 \| grep -E "kept=\|near_dup\|low_importance"` | dedup 閾値調整 or conflict.py のロジック確認 |
| **H5** | weekly_mem0_compact.py の集計バグ (実際は ingestion されてるが 0件と誤報) | `grep -A 20 "過去7日\|last_7d\|recent" scripts/weekly_mem0_compact.py` | 集計コード修正のみ、ingestion 自体は問題なし |
| **H6** | bot.py の `caller_is_admin` 判定が壊れてて、admin 認識されず ingestion path に入ってない | journal の `[rid=...] processing user=... admin=False` を確認 | ADMIN_USER_IDS 設定確認 |

### 2.2 検証フロー (5/24 10:50 〜)

#### Step 1: 集計ロジック確認 (最優先、これだけで H5 確定/否定可能)

```bash
cd /home/ubuntu/discord-bot

# weekly_mem0_compact.py の「過去7日 ingestion 0件」がどう計算されてるか
grep -B 2 -A 30 "ingest\|新規\|stored\|past.*day\|7" scripts/weekly_mem0_compact.py

# 最新 compact レポート
ls -lt /tmp/mem0_compact_*.md | head -1
cat $(ls -t /tmp/mem0_compact_*.md | head -1)
```

**判定**: 集計ロジックが「created_at_unix > now - 7day」で Mem0 を直接クエリしてる →
- 0件と出てる = 本当に過去7日新規追加なし → H1〜H4 のどれか
- 0件と出てない = H5 (集計バグ)

#### Step 2: batch state 確認 (H1 確定/否定)

```bash
cd /home/ubuntu/discord-bot

# batch state dir の中身一覧
ls -la state/mem0_batch/ 2>/dev/null || echo "no batch dir"

# 各 channel の turn 蓄積数
for f in state/mem0_batch/*.json; do
  ch_id=$(basename "$f" .json)
  count=$(python3 -c "import json; d=json.load(open('$f')); print(len(d.get('turns', [])))")
  facts=$(python3 -c "import json; d=json.load(open('$f')); print(len(d.get('kanae_facts', [])))")
  echo "channel $ch_id: turns=$count, kanae_facts=$facts"
done
```

**判定**:
- どの channel も `turns >= 10` (BATCH_SIZE) → flush trigger されるべき、H1 否定 → H2-H4 へ
- どの channel も `turns < 10` → **H1 確定**、過去7日「会話自体が少なすぎて batch flush に至ってない」可能性
- ファイル自体無い → ingestion path 自体に到達してない、H6 疑い

#### Step 3: journal 検証 (H2/H3/H4/H6)

```bash
# 過去7日分の bot journal を抽出
sudo journalctl _PID=$(cat /home/ubuntu/discord-bot/.state/bot.pid) \
    --since "7 days ago" > /tmp/bot_journal_7d.log
wc -l /tmp/bot_journal_7d.log

# Mem0 関連ログ全抽出
grep -E "Mem0|mem0|add_memories|batch ingest|kept=|extract" /tmp/bot_journal_7d.log \
    > /tmp/bot_mem0_logs.txt
wc -l /tmp/bot_mem0_logs.txt
head -50 /tmp/bot_mem0_logs.txt

# admin判定の確認 (H6)
grep "processing user=" /tmp/bot_journal_7d.log | head -20
# admin=True が出てれば OK、admin=False ばかりなら H6 確定

# extract 結果の確認 (H3)
grep -E "extract|facts=[0-9]" /tmp/bot_journal_7d.log | head -20

# dedup 結果の確認 (H4)
grep -E "kept=|near_dup|low_importance|dup=" /tmp/bot_journal_7d.log | head -20
```

**判定マトリックス**:

| ログに出ているもの | 出てないもの | 結論 |
|---|---|---|
| `Mem0 batch ingest: uid=... turns=N` | - | flush は走ってる |
| - | `Mem0 batch ingest` 一行も無し | **H2 確定**: bot.py:1802 が呼ばれてない |
| `kept=0` | - | **H3 or H4 確定** |
| `processing user=... admin=True` | - | admin 認識は OK |
| - | `admin=True` 一行も無し | **H6 確定**: 管理者として認識されてない |

#### Step 4: 仮説確定 → 修正

Step 1-3 の結果から H1〜H6 のどれか1つに確定。確定後の対処:

| 確定仮説 | 修正アクション |
|---|---|
| H1 | `MEM0_BATCH_SIZE=3` に下げる (環境変数で即時反映)、または bot restart 時に残 turn を強制 flush するロジック追加 |
| H2 | bot.py:1802 周辺の例外を journal で追跡 |
| H3 | services/mem0/extract.py のプロンプト見直し、Haiku モデルが extract できてない場合は Sonnet にフォールバック |
| H4 | services/mem0/dedup.py の near_dup 閾値を緩める、services/mem0/conflict.py のロジック確認 |
| H5 | scripts/weekly_mem0_compact.py の集計ロジックを修正、実際は ingestion されてるならスキップ |
| H6 | config.py の ADMIN_USER_IDS 確認、bot restart |

### 2.3 Phase A' のチェックポイント

- [ ] Step 1 完了: 集計ロジック確認、H5 確定/否定
- [ ] Step 2 完了: batch state 確認、H1 確定/否定
- [ ] Step 3 完了: journal 確認、H2-H4/H6 確定/否定
- [ ] Step 4 完了: 仮説 1つに確定
- [ ] Step 5 完了: 必要な設定修正実施 (or 修正不要と判定)

**ここまで read-only or 軽微な設定変更のみ。Phase B 着手前に必ず先輩へ報告**。

---

## 3. Phase B: Discord API 経由で過去7日分 ingestion

### 3.1 前提

- 既存スクリプト `scripts/ingest_channel_history.py` を使う (新規実装不要)
- 引数で動作制御可能、`--all-channels` `--user-id` `--limit` `--dry-run` `--concurrency` `--subject` 完備
- Discord API → 会話ペア組立 → `services.mem0.ingest.add_memories` の pipeline 内蔵

### 3.2 期間フィルタの実装が必要

**問題**: 既存スクリプトは `--limit N` (最新N件) のみ、日付フィルタなし。「過去7日」を取りたい場合に冗長分も取れる。

**選択肢**:

| 案 | 内容 | 工数 | 採用 |
|---|---|---|---|
| A | `--limit 500` で多めに取って dedup に任せる | 0分 | × (`history_ingest` source の facts が「7日以前のもの」も入り、解析時に「会話 vs 過去」の区別が消える) |
| B | `ingest_channel_history.py` に `--since YYYY-MM-DD` オプション追加 | 30分 | **採用** |
| C | fetch_channel_messages の戻り値を呼出側で post-filter | 15分 | × (スクリプト全体を再実装するなら B と同じ手間) |

#### B 案の実装ポイント

`scripts/ingest_channel_history.py:331` 周辺に追加:

```python
parser.add_argument(
    "--since",
    type=str,
    help="Only ingest messages created at or after this date (YYYY-MM-DD)",
)
```

`fetch_channel_messages` の `channel.history()` 呼び出しに `after=` を渡す:

```python
# discord.py の channel.history は after に datetime を取る
after_dt = None
if since_str:
    after_dt = datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)

async for msg in channel.history(
    limit=limit, oldest_first=True,
    after=after_dt,  # ← 追加
):
    ...
```

これで `--since 2026-05-17 --limit 500` が「5/17以降の最新500件」になる。

### 3.3 実行計画

#### Step 1: --since オプション実装 (30分)

`scripts/ingest_channel_history.py` を Edit で改修。テストは `--dry-run` で確認。

#### Step 2: dry-run

```bash
cd /home/ubuntu/discord-bot

# 過去7日 (2026-05-17 以降) のメッセージを dry-run
python3 scripts/ingest_channel_history.py \
    --all-channels \
    --user-id 573489206240870410 \
    --since 2026-05-17 \
    --limit 500 \
    --dry-run \
    --subject self \
    2>&1 | tee /tmp/ingest_dryrun.log

# 件数集計
grep "DRY-RUN" /tmp/ingest_dryrun.log | wc -l
grep "Channel.*done" /tmp/ingest_dryrun.log
```

**判定**: 出力件数が想定通りか確認。**100-500件想定**、これより極端に少ない/多い場合は --since が効いてない可能性。

#### Step 3: subject=self 本番投入

```bash
python3 scripts/ingest_channel_history.py \
    --all-channels \
    --user-id 573489206240870410 \
    --since 2026-05-17 \
    --limit 500 \
    --concurrency 2 \
    --subject self \
    2>&1 | tee /tmp/ingest_self_$(date +%Y%m%d_%H%M).log
```

**注意点**:
- `--concurrency 2` で開始 (Mem0 client は max_retries=0 で 429 fail-open、saturation 注意)
- 429 が頻発したら `--concurrency 1 --delay 2.0` に下げて再実行 (dedup 効くので重複投入 OK)

#### Step 4: subject=kanae 本番投入

```bash
python3 scripts/ingest_channel_history.py \
    --all-channels \
    --user-id 573489206240870410 \
    --since 2026-05-17 \
    --limit 500 \
    --concurrency 2 \
    --subject kanae \
    2>&1 | tee /tmp/ingest_kanae_$(date +%Y%m%d_%H%M).log
```

- subject=kanae は内部的に `KANAE_BOT_USER_ID` (= `1489178684391817336`) namespace へ書き込む
- bot reply 無しの先輩メッセージは pairing 段階で drop される

#### Step 5: 投入確認

```bash
# 直近 ingestion の確認 (smart search で過去7日のトピック検索)
# Discord 上から: @kanae 過去7日のUKGの話を整理して
# → retrieved_memories ブロックに新規 facts が出てくれば成功

# あるいは python から直接
python3 -c "
import asyncio
from services.mem0.search import search_memories
async def main():
    results = await search_memories('UKG 道玄坂', '573489206240870410', limit=10)
    for r in results:
        print(r.get('memory', '')[:80], '|', r.get('metadata', {}).get('source', ''))
asyncio.run(main())
" | head -20
```

**判定**: `metadata.source == 'history_ingest'` が出てくれば成功。

### 3.4 Phase B のチェックポイント

- [ ] Step 1 完了: --since オプション実装、ユニットテスト or dry-run で動作確認
- [ ] Step 2 完了: dry-run で対象件数が想定範囲内
- [ ] Step 3 完了: subject=self 投入完了、エラー件数 < 5%
- [ ] Step 4 完了: subject=kanae 投入完了、エラー件数 < 5%
- [ ] Step 5 完了: 投入結果を Mem0 検索で確認、新規 facts が retrievable

---

## 4. リスクと緩和策

| リスク | 影響 | 緩和策 |
|---|---|---|
| 429 saturation で大量 fail | 投入完了しない | `--concurrency 1 --delay 2.0` で再実行、dedup が効くので重複は問題なし |
| Phase A' の修正で bot が起動失敗 | bot ダウン | 環境変数のみで修正できる範囲に留める (例: `MEM0_BATCH_SIZE=3`)、コード修正は別 PR |
| Phase B 投入中に namespace 誤指定 | 不要な namespace に汚染 | --dry-run を必ず先に実施、--subject を取り違えない (self ≠ haroin namespace 取り違えに注意) |
| `--since` 実装ミスで7日以前も投入 | 古い facts が新規扱い | dry-run で件数確認、想定の 3倍を超えてたら停止 |
| ingestion 中に bot crash | 通常会話が止まる | 並行実行可能、bot は別 process、影響なし |

---

## 5. 残るオープンクエスチョン

1. **Phase A' 後の根本対処**: 例えば H1 (BATCH_SIZE) なら、`MEM0_BATCH_SIZE=3` に下げるか、それとも flush trigger の機構自体を「N分経過 OR N turn 蓄積」のどちらか早い方に変更するか
2. **過去7日より前のログ**: 今回は7日に絞るが、5/17 以前の会話も Mem0 に入ってない可能性。今回完了後に「次に何日まで遡るか」の判断が要る
3. **compact レポートの仕様変更**: 集計バグ (H5) があった場合、レポート修正だけでなく「ingestion 健全性のリアルタイム監視」もスコープに入れるか
4. **kanae 側 facts の人格汚染チェック**: 過去ログから kanae facts を投入すると、私が「過去発言として認識する内容」が大量に増える。会話ペアの自動 extract で品質チェック無しに投入していいかは要判断

---

## 6. 関連ファイル一覧

| ファイル | 役割 |
|---|---|
| `services/mem0/batch.py` | turn 蓄積、`BATCH_SIZE=10` |
| `services/mem0/ingest.py` | `add_memories` core entry |
| `services/mem0/extract.py` | Haiku による fact 抽出 |
| `services/mem0/dedup.py` | 重複判定 |
| `services/mem0/conflict.py` | 矛盾検出 |
| `services/mem0/client.py` | Mem0 API client (max_retries=0) |
| `scripts/ingest_channel_history.py` | Discord 経由 backfill (今回主役) |
| `scripts/weekly_mem0_compact.py` | weekly compact レポート |
| `bot.py:1802-1837` | リアルタイム batch flush ロジック |
| `state/mem0_batch/{channel_id}.json` | batch 蓄積 state |
| `mcp_mem0_server.py:196` | MCP 経由 add (手動 add のエントリ、`stored N new memories` を返す) |

---

## 7. 完了定義 (Definition of Done)

- Phase A' で 0件問題の原因が H1〜H6 のどれか1つに確定し、設定修正済み
- Phase B で過去7日分の会話ペアが Mem0 に投入され、smart search で retrievable
- 1週間後 (5/31) の compact レポートで「新規 ingestion 件数 > 0」が確認できる
- 本ドキュメントに最終結果を追記 (どの仮説が正解だったか、修正内容、投入件数)

---

## 8. 履歴

- 2026-05-24 10:50 初版作成 (かなえ)
