# Mem0 既存 fact への created_at 後付け計画

**作成**: 2026-05-14
**目的**: 日付メタデータが欠落している既存 Mem0 fact (推定約 1500 件) に、Discord 元メッセージから created_at を逆引きして付与する

---

## 1. Context

### 問題

今日の ingest_channel_history.py 完走分 (147 件) は metadata に `created_at` / `created_at_unix` を持つが、それより前に蓄積された約 1500 件は **日付メタデータゼロ**。

時系列クエリ (「先月の話」「最近の Rust の話」) が機能しないし、Phase 5 (weekly summary) や temporal decay も適用できない。

### Discord 側のデータ

`discord.Message.created_at` (datetime, UTC) は Discord メッセージごとに常に存在し、channel.history で全件取得可能。`limit=10000` なら 該当チャンネル `1490247632847896776` の全履歴を 1 リクエストで読み込める (実測: 1350 メッセージ取得)。

つまり **「fact ↔ 元 Discord メッセージ」の対応さえ取れれば created_at を後付けできる**。

### 既存 fact の構造

各 fact は ChromaDB の `kanae_memories` collection に保存され、metadata に以下を持つ:
- `category`, `subcategory`, `importance`, `tier`, `tags`
- `gate_version` (v1 = レガシー mem0 標準抽出 / v2 = P2 gate by services/mem0/extract.py)
- 一部に `channel_id` / `source` (history_ingest 由来分のみ)

**重要**: fact の `memory` テキストと、元の Discord `message.content` は **同じではない** (Sonnet 抽出で再構成されているため)。完全一致照合は不可、**意味類似度ベース** で対応付ける必要がある。

---

## 2. アプローチ比較

| 案 | 手段 | 精度 | コスト | 副作用 |
|---|---|---|---|---|
| A | 全 delete → 日付付き再 ingest | 低 (リアルタイム会話分が消える) | 中 ($0.05 + Sonnet) | データ損失 |
| B | JSON dump → 再 ingest 後 dump 戻し | 中 (リアルタイム分は日付なし固定) | 中 | 二度手間 |
| **C** | **Vector マッチングで Discord 元メッセージを逆引き → metadata 更新** | **中-高** | **低 ($0.01 程度 + 数分)** | **fact 本体に触らない** |
| D | LLM (Sonnet) で「この fact は何月の発話か?」を Discord 履歴をコンテキストに判定 | 高 | 高 ($5-10) | 過剰 |

**推奨: C (Vector マッチング)** — 既存 fact text を OpenAI embedding で vectorize し、Discord 全 user メッセージの embedding と cosine 比較、最近傍メッセージの `created_at` を metadata に書き込む。完全一致じゃなくても「会話の文脈にあった発話」を当てられる確率が高い。

---

## 3. 設計

### データフロー

```
[Step 1] Discord 履歴 fetch
  channel.history(limit=10000) → [Message(id, content, created_at)]
  user メッセージのみ抽出 (bot replies は除外) → 約 738 件

[Step 2] Discord メッセージ embedding
  各 content を OpenAI text-embedding-3-small で vectorize
  → np.array shape (738, 1536) を numpy で保持

[Step 3] 既存 fact のロード
  ChromaDB から user_id=573489206240870410 の全 fact (memory_id, memory_text, embedding, metadata)
  → 約 1400 件
  ※ embedding は既に ChromaDB に保存済なので OpenAI に再リクエストしない (cost 節約)

[Step 4] cosine マッチング
  各 fact embedding に対して Discord 全 embedding と内積取って top-1
  top-1 cosine が >= THRESHOLD (例 0.55) なら採用、未満なら null

[Step 5] metadata 書き込み
  cosine スコアが採用閾値を超えた fact に対し、
    metadata["created_at_unix"] = <matched message's unix timestamp>
    metadata["created_at"]      = <matched message's isoformat>
    metadata["created_at_source"] = "vector_match"
    metadata["created_at_score"] = <cosine>
  Mem0 SDK の update API or ChromaDB 直接 update

[Step 6] レポート
  - 採用: N 件 (推定 60-80%)
  - 未採用 (低 cosine): M 件 — リアルタイム会話分や別チャンネル発由来。created_at なしのまま
  - 上位/下位サンプルをログに出して目視確認
```

### 採用閾値 (`THRESHOLD`) の決定

事前 dry-run で cosine 分布を観察し:
- top-1 cosine >= 0.70 → ほぼ確実な対応
- 0.55 ≤ top-1 < 0.70 → グレーゾーン、採用するか判断
- < 0.55 → 別文脈、却下

推奨初期値 **0.55** で dry-run、結果を見て調整。

### `created_at_source` フィールドの意義

後で「この fact の created_at は本当か?」を識別するため。
- `"history_ingest"` — Discord 直接 (今日 147 件分)
- `"vector_match"` — 後付け、本計画で付与
- 空 — 未付与 (リアルタイム会話分が中心)

---

## 4. 修正対象ファイル

| ファイル | 変更 |
|---|---|
| 新規 [scripts/backfill_mem0_timestamps.py](../scripts/backfill_mem0_timestamps.py) | Discord 履歴 fetch + cosine マッチング + Mem0 metadata update |

bot コードの修正は不要。スクリプト単独で完結。

### スクリプト構成 (擬似コード)

```python
# scripts/backfill_mem0_timestamps.py

import discord, asyncio, json, numpy as np, sqlite3
from openai import OpenAI
from services.mem0.client import _get_mem0

CHANNEL_ID = 1490247632847896776
USER_ID = 573489206240870410
THRESHOLD = 0.55

async def run(dry_run: bool):
    # Step 1: Discord 履歴
    msgs = await fetch_user_messages(CHANNEL_ID, USER_ID, limit=10000)
    texts = [m.content for m in msgs]

    # Step 2: OpenAI で embed
    oai = OpenAI()
    emb_msgs = oai.embeddings.create(model="text-embedding-3-small", input=texts).data
    msg_vecs = np.array([e.embedding for e in emb_msgs])  # (738, 1536)
    msg_vecs /= np.linalg.norm(msg_vecs, axis=1, keepdims=True)

    # Step 3: 既存 fact ロード (ChromaDB から直接)
    facts = load_facts_with_embeddings(USER_ID)
    # [{id, memory, embedding, metadata}, ...]

    # Step 4: マッチング
    matched = 0
    for f in facts:
        if f["metadata"].get("created_at_unix"):
            continue  # 既に持ってる fact はスキップ
        v = np.array(f["embedding"])
        v /= np.linalg.norm(v)
        sims = msg_vecs @ v  # (738,)
        idx = int(np.argmax(sims))
        score = float(sims[idx])
        if score < THRESHOLD:
            f["match"] = None
            continue
        f["match"] = {
            "msg": msgs[idx],
            "score": score,
        }
        matched += 1

    # Step 5: metadata 更新 (dry_run なら出力のみ)
    if not dry_run:
        mem = _get_mem0()
        for f in facts:
            if not f.get("match"):
                continue
            new_meta = {**f["metadata"]}
            new_meta["created_at_unix"] = int(f["match"]["msg"].created_at.timestamp())
            new_meta["created_at"]      = f["match"]["msg"].created_at.isoformat()
            new_meta["created_at_source"] = "vector_match"
            new_meta["created_at_score"]  = round(f["match"]["score"], 3)
            mem.update(memory_id=f["id"], metadata=new_meta)

    # Step 6: レポート
    print(f"facts loaded: {len(facts)}")
    print(f"already had created_at: {sum(1 for f in facts if f['metadata'].get('created_at_unix'))}")
    print(f"matched (score >= {THRESHOLD}): {matched}")
    print(f"skipped (score < {THRESHOLD}): ...")
    sorted_by_score = sorted([f for f in facts if f.get("match")], key=lambda x: x["match"]["score"])
    print("\n--- bottom 5 (lowest score) ---")
    for f in sorted_by_score[:5]:
        print(f"  {f['match']['score']:.3f}  fact={f['memory'][:50]!r}  msg={f['match']['msg'].content[:50]!r}")
    print("--- top 5 (highest score) ---")
    for f in sorted_by_score[-5:]:
        print(f"  {f['match']['score']:.3f}  fact={f['memory'][:50]!r}  msg={f['match']['msg'].content[:50]!r}")
```

---

## 5. Mem0 SDK の `update` API 確認

`mem.update(memory_id=..., metadata=...)` が metadata 部分更新できるか実機検証必要。
- できる: そのまま使う
- できない (metadata 全置換 or fact 全更新が要る): ChromaDB を直接叩く (`collection.update(ids=[id], metadatas=[new])`)

事前にスクリプト内で 1 件だけ試して確認。

---

## 6. 実行手順

### Phase A: dry_run (15 分)

```bash
sudo systemctl stop discord-bot   # token 競合回避

.venv/bin/python -u scripts/backfill_mem0_timestamps.py --dry-run \
  > /tmp/backfill_dryrun.log 2>&1

# レポート確認
tail -50 /tmp/backfill_dryrun.log
```

判断ポイント:
- 採用率 (matched / total) は 60% 超えか
- bottom 5 (低スコア採用) の対応付けが目視で妥当か
- THRESHOLD を上下調整

### Phase B: 本番実行 (15-20 分)

```bash
.venv/bin/python -u scripts/backfill_mem0_timestamps.py \
  > /tmp/backfill_real.log 2>&1
```

完了後 bot を起動:

```bash
sudo systemctl start discord-bot
```

### Phase C: 検証 (5 分)

```bash
# created_at 付与済みの件数
sqlite3 /home/ubuntu/discord-bot/.state/mem0_db/chroma.sqlite3 <<'EOF'
SELECT count(*) FROM embedding_metadata
WHERE key='created_at_unix';
EOF
```

期待: 元 147 (history_ingest) + 約 800-1000 (vector_match) = 1000 件前後

---

## 7. リスク・課題

| リスク | 対応 |
|---|---|
| **`mem.update()` API が想定通り動かない** | ChromaDB 直接更新パスを実装、または fact を delete → add で代替 (ただし fact_id 変わる) |
| **誤マッチング**: cosine 0.55 で「全然違う日のメッセージ」に紐付くケース | `created_at_source: vector_match` フラグで識別、後で score 別に再評価可能 |
| **採用率が低い**: 例えば 30% しかマッチしない | リアルタイム会話分の fact が多いため。ingest_channel_history で過去全チャンネルを ingest してから再実行する選択肢 (Phase D) |
| **OpenAI embedding コスト**: 738 メッセージ × 平均 200 tokens = 約 150K tokens × $0.02/M = **$0.003** | 無視できる |
| **Discord rate limit**: history fetch で 1 req | 問題なし |
| **bot 停止時間**: Phase A+B で約 30-40 分 | 先輩判断 |

---

## 8. オープン課題

1. **THRESHOLD の初期値**: 0.55 で運用するか、dry_run 結果見て決める
2. **created_at_source による検索バイアス**: vector_match で付与した created_at を「概算」と扱う UI / 検索ロジックが必要か検討
3. **他チャンネルの履歴を含めるか**: 該当チャンネル `1490247632847896776` 以外で蓄積された fact (お父さん経由など) はマッチング対象外。必要なら `--all-channels` 相当を追加
4. **embedder 切替 (3-large) と並行する場合の順序**: 先に backfill (3-small 環境) → 後で embedder 切替、もしくは embedder 切替 → backfill (3-large 環境)。前者推奨 (今ある cost が小さいうち)
5. **再現性**: 同じ fact を再 backfill しても結果が同じになる (deterministic)。実行ログだけは履歴として残す

---

## 9. 次のアクション

1. このドキュメントを先輩レビュー
2. OK なら `scripts/backfill_mem0_timestamps.py` を実装
3. Phase A (dry_run) を bot 停止して走らせる
4. レポート見て THRESHOLD 調整 → Phase B 本番
5. Phase C 検証 → bot 復帰
