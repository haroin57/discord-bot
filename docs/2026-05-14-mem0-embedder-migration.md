# Mem0 Embedder 移行計画: `text-embedding-3-small` → `text-embedding-3-large`

## Context

bot の Mem0 検索品質を向上させるため、OpenAI 埋め込みモデルを上位版に切り替える。
ChromaDB の collection は次元固定 (1536 dim) のため次元変更には DB 全削除→再構築が必須だが、**先輩判断: JSON dump で全 fact を吐き出してから `mem.add(infer=False)` で再挿入すれば抽出 LLM を経由せず embedding だけ 3-large で再生成できる**。データ損失ゼロ。

期待される結果:
- Mem0 検索/dedup の cosine 類似度判定が高精度化
- 既存 1527 fact (リアルタイム会話蓄積分含む) を全件保全
- 再 ingest のため、新規 fact ソース (該当チャンネル履歴) も一緒に投入

---

## Map — 現状調査

| 項目 | 値 | 出典 |
|---|---|---|
| 現在の embedder | `openai/text-embedding-3-small` (1536 dim) | [services/mem0/client.py:108-115](../../services/mem0/client.py#L108-L115) |
| 全 fact 数 | **1527 件** (先輩: 1421, お父さん: 99, その他: 7) | `chroma.sqlite3` (9.3MB) |
| 抽出経路別 | v2 (新 P2 gate): 561件 / v1 (mem0 標準 + リアルタイム会話): 約 966件 | embedding_metadata table の `gate_version` |
| 復元可 | scripts/ingest_channel_history.py で **過去チャンネル履歴** | [scripts/ingest_channel_history.py](../../scripts/ingest_channel_history.py) |
| 復元不可 | bot.py の `_mem0_store()` 経由で蓄積された **リアルタイム日常会話** (約 1000件) | [bot.py:1015 周辺](../../bot.py) |
| mem0 SDK `dimensions` バグ | [#4153](https://github.com/mem0ai/mem0/issues/4153) 未修正 (Feb 2026) | OpenAI API への dimensions パラメータが正しく伝搬しないバグ報告中 |

---

## 推奨フロー

### Phase 0: 事前バックアップ + JSON dump (10 分)

ChromaDB を物理コピーで丸ごとバックアップ + 全 fact を JSON dump。後者は Phase 3 で再挿入のソースになる。

```bash
sudo systemctl stop discord-bot      # 書き込み停止

# 1) 物理コピー (保険)
cp -a /home/ubuntu/discord-bot/.state/mem0_db \
      /home/ubuntu/discord-bot/.state/mem0_db.bak-3small-$(date +%Y%m%d-%H%M)

# 2) JSON dump (Phase 3 の再挿入ソース)
.venv/bin/python <<'EOF'
import json
from services.mem0 import client as mc
mem = mc._get_mem0()
out = {}
total = 0
for uid in ['573489206240870410', '1037285234200297522', '826096545514127400']:
    facts = mem.get_all(user_id=uid).get('results', [])
    out[uid] = facts
    total += len(facts)
json.dump(out, open('/home/ubuntu/discord-bot/.state/mem0_export.json', 'w'),
          ensure_ascii=False, indent=2)
print(f'exported {total} facts → mem0_export.json')
EOF
```

成功条件: `mem0_export.json` に **1527 件** (先輩 1421 + お父さん 99 + その他 7) が出ること。各 fact は `{id, memory, metadata, ...}` を持つ。

### Phase 1: ChromaDB リセット + 設定切替 (3 分)

```bash
# DB 削除
rm -rf /home/ubuntu/discord-bot/.state/mem0_db

# .env に embedder model を追加
# (mem0 SDK の dimensions バグを避けるため、デフォルト 3072 dim で進める。
#  1536 dim 縮小は mem0 SDK 修正後に再検討)
echo "MEM0_EMBEDDER_MODEL=text-embedding-3-large" >> /home/ubuntu/discord-bot/.env

# 確認
grep MEM0_EMBEDDER /home/ubuntu/discord-bot/.env
```

### Phase 2: bot 起動で新 collection 作成 (1 分)

bot を起動するだけで `_get_mem0()` が初期化され、3072 dim の新 collection が ChromaDB に作られる。

```bash
sudo systemctl start discord-bot
sleep 10
sudo journalctl -u discord-bot --since "30s ago" | grep "Mem0 ready"
# "Mem0 ready — embedder=text-embedding-3-large" を確認
sudo systemctl stop discord-bot   # Phase 3 のため再停止
```

### Phase 3: JSON dump から 1527 fact を `infer=False` で再挿入 (10-20 分)

`scripts/restore_mem0_from_json.py` を新規作成して、`.state/mem0_export.json` の全 fact を新 collection に投入。`infer=False` を渡すことで Mem0 SDK の抽出 LLM を経由せず、**embedding (3-large) のみ走らせる**。metadata はそのまま引き継ぐ。

```python
# scripts/restore_mem0_from_json.py (新規)
"""Restore Mem0 facts from a JSON dump produced by Phase 0.

Uses mem.add(messages=..., infer=False) so the LLM-based extractor is
bypassed and only the embedder runs. Each fact's metadata is preserved
verbatim (category / subcategory / importance / tier / tags / created_at).
"""
import asyncio, json, sys
sys.path.insert(0, '/home/ubuntu/discord-bot')
from services.mem0 import client as mc

async def main():
    mem = mc._get_mem0()
    data = json.load(open('/home/ubuntu/discord-bot/.state/mem0_export.json'))
    inserted = errors = 0
    for uid, facts in data.items():
        for f in facts:
            text = f.get('memory') or f.get('text') or ''
            metadata = f.get('metadata') or {}
            if not text:
                continue
            try:
                await asyncio.to_thread(
                    mem.add,
                    [{'role': 'user', 'content': text}],
                    user_id=uid, metadata=metadata, infer=False,
                )
                inserted += 1
                if inserted % 50 == 0:
                    print(f'... {inserted} inserted')
            except Exception as e:
                errors += 1
                print(f'FAIL {text[:40]}: {e}')
    print(f'done: inserted={inserted}, errors={errors}')

asyncio.run(main())
```

実行:

```bash
.venv/bin/python scripts/restore_mem0_from_json.py 2>&1 | tee /tmp/mem0_restore.log
```

成功条件: `inserted=1527, errors=0`。OpenAI 3-large の embedding コール 1527 回 (約 $0.05)。LLM 呼び出しゼロなので 429 リスクなし。

### Phase 4: チャンネル履歴 ingest で新規 fact 追加 (60-90 分)

JSON dump 復元後、**新規** に該当チャンネルの fact を抽出するための ingest:

```bash
.venv/bin/python -u scripts/ingest_channel_history.py \
  --channel-id 1490247632847896776 \
  --user-id 573489206240870410 \
  --limit 10000 --batch-size 8 --delay 10.0 \
  > /tmp/ingest_3large.log 2>&1 &
INGEST=$!
nohup bash -c "while ps -p $INGEST > /dev/null; do sleep 60; done; sudo systemctl start discord-bot" > /tmp/ingest_3large_watchdog.log 2>&1 &
```

Phase 3 で復元した fact と重複する場合は dedup (cosine ≥ 0.95 hard_drop / 0.80-0.95 Sonnet judge) で自動的に drop される。完走後 watchdog が bot 再起動。

### Phase 5: 検証 (20 分)

```bash
# DB 再構築確認: Phase 3 復元 (1527) + Phase 4 新規 (期待 50-100 件) で 1600 前後
sqlite3 /home/ubuntu/discord-bot/.state/mem0_db/chroma.sqlite3 \
  "SELECT count(*) FROM embeddings;"

# Mem0 検索が機能するか smoke
.venv/bin/python -c "
import asyncio
from services.mem0.search import search_memories
async def main():
    r = await search_memories('プログラミング言語の好み', '573489206240870410', limit=5)
    for x in r: print(x.get('memory', '')[:80], x.get('score'))
asyncio.run(main())
"

# bot の Discord 会話で「以前話したこと覚えてる?」を試して reasonable な応答か確認
```

成功条件:
- `embeddings` テーブル件数 ≥ 1527 (Phase 3 復元分が完全に入った証拠)
- search で cosine score が 3-small より高い相関を示すこと
- bot が「過去に話した topic」を引っ張れること

### Phase 6 (任意): rerank LLM を Opus に格上げ — コスト試算込みの判断

#### Mem0 LLM call の発火頻度 (実装確認)

[bot.py:686-694](../../bot.py#L686-L694):
- `if len(raw_user_text) >= MEM0_MIN_QUERY_LENGTH (20)` で `search_memories_smart` を asyncio.create_task で発火
- **20 字以上の発話が来るたびに毎ターン Mem0 検索が走る**

[services/mem0/search.py:191-210](../../services/mem0/search.py#L191-L210) `search_memories_smart`:
1. `_rewrite_query` (LLM 1 call): query → 3-5 canonical keywords
2. `search_memories_multi` (OpenAI embedding × 4-6): top-20 candidates 集める
3. `_rerank_memories` (LLM 1 call): 20 候補 → top-5 を LLM judge で選ぶ

つまり **1 Mem0 検索 = LLM 2 call (rewrite + rerank)**。

加えて Mem0 ingest 経路 ([bot.py:1015 周辺](../../bot.py)) で毎ターン:
- `extract_facts_for_self` (LLM 1 call)
- `llm_judge_same_fact` (LLM 0-N call、グレーゾーン候補数)

**合計: 1 ターン Discord 会話 = 3-5 LLM call (検索 + ingest)**

#### CLI 互換化後の token 量 (1 LLM call あたり)

CLI 互換化により全 Mem0 LLM call の input は:

| 構成要素 | tokens | cache_control |
|---|---|---|
| system[0] billing header | ~30 | ephemeral, scope=global ✅ |
| system[1] identity | ~25 | なし |
| system[2] CLI default prompt | **~2900** | ephemeral, scope=global ✅ |
| system[3] task prompt | ~30 | なし |
| user message (prompt 本体) | ~100 (rewrite) / ~750 (rerank) / ~400 (extract) | なし |
| **合計** | **~3000-3700** | system[0] + system[2] が prompt cache 対象 |

#### prompt caching 効果

`cache_control: scope=global` で system[0] と system[2] が cache 対象 (5 分 TTL)。bot 会話は数十秒〜数分間隔なので **大半は cache hit**:

- cache write: 通常 input の 1.25x ($)
- cache read: 通常 input の 0.10x ($)
- 90% discount で読み取り

#### Sonnet vs Opus 価格 (per M tokens)

| | input (normal) | cache read | output |
|---|---|---|---|
| Sonnet 4.6 | $3 | $0.30 | $15 |
| Opus 4.6 | $15 | $1.50 | $75 |
| **倍率** | **5x** | **5x** | **5x** |

#### 1 LLM call あたりコスト (cache hit 前提)

`rerank` 1 call の試算 (input ~3700 tokens: cached 2930 + non-cached 770, output ~50 tokens):

| モデル | input ($) | output ($) | **call cost** |
|---|---|---|---|
| Sonnet | 2930×0.30 + 770×3.00 = $0.0032 | 50×15 = $0.00075 | **$0.0040** |
| Opus | 2930×1.50 + 770×15 = $0.0160 | 50×75 = $0.00375 | **$0.0198** (≈ 5x) |

`rewrite` / `extract` / `dedup judge` も近い水準 (call あたり Sonnet $0.003-0.005, Opus $0.015-0.025)。

#### 月次見込みコスト (1 日 100 ターン会話の場合)

| 切替パターン | call/day | Sonnet/月 | Opus/月 | 差分 |
|---|---|---|---|---|
| rerank のみ Opus | 100 | $14 (rest is Sonnet) | $59 (rest is Sonnet) | **+$45/月** |
| rerank + rewrite Opus | 200 | $26 | $116 | +$90/月 |
| 全 Mem0 LLM Opus (extract / dedup 含む) | 300-500 | $40-65 | $200-325 | +$160-260/月 |

#### 課金経路

- **Mem0 経路の LLM call は CLI 互換化済 (本セッション)** → OAuth subscription quota が使われる
- subscription Max20x の月 $200 quota 内で吸収できる範囲なら **追加課金ゼロ**
- 範囲を超えたら overage (claude.ai Extra Usage) で API rate billing に流れる

#### 判断材料まとめ

| 選択 | 月次追加コスト (overage 想定) | 期待品質向上 |
|---|---|---|
| **rerank だけ Opus** | +$45 | Mem0 検索 top-5 の relevance 精度が向上 (体感差は中程度) |
| rerank + rewrite Opus | +$90 | query→canonical変換も Opus、検索 hit rate も改善 |
| 全 Mem0 Opus | +$160-260 | extract の fact 抽出も精緻化、dedup の境界判定も改善 |
| **Sonnet 維持** | $0 | 現状維持 |

**実装**: env で対応モデルだけ Opus に切替:

```bash
# rerank のみ Opus に
echo "MEM0_RERANK_MODEL=claude-opus-4-6" >> .env

# rewrite も Opus に (任意追加)
# echo "MEM0_REWRITE_MODEL=claude-opus-4-6" >> .env

sudo systemctl restart discord-bot
```

#### 観察方法 (Phase 6 実施後)

```bash
sudo journalctl -u discord-bot --since "1 day ago" | grep "anthropic.com.*200 OK" | wc -l
# 1日の API call 数。500+ なら全 Opus 化はコスト過大、200以下なら全切替の余地あり
```

bot.log でターン頻度を観察してから上記表のどのレベルにするか決める。

embedding 切替で得られる精度向上 (MTEB +2.3pt) は実用上ささやか。検索品質の本丸は **rerank 段階**。

[services/mem0/search.py:142-148](../../services/mem0/search.py#L142-L148) の `_rerank_memories` で使う MEM0_RERANK_MODEL を Sonnet → Opus に上げると、検索結果の relevance 判定がより正確になる:

```bash
echo "MEM0_RERANK_MODEL=claude-opus-4-6" >> .env
sudo systemctl restart discord-bot
```

これは Phase 2-4 が落ち着いてから別タスクで検討。

---

## ロールバック条件

| トリガー | 復旧方法 |
|---|---|
| Phase 1 で `rm -rf` 後に問題発覚 | `mv /home/ubuntu/discord-bot/.state/mem0_db.bak-3small-* /home/ubuntu/discord-bot/.state/mem0_db` + .env から `MEM0_EMBEDDER_MODEL` 行を削除 + bot 再起動 |
| Phase 3 で `infer=False` が想定通り動かない (LLM 呼出が走る等) | Phase 4 をスキップして即 Phase 0 の bak へロールバック。`mem.add(infer=False)` の挙動を別途検証 (mem0 SDK v1.1 では対応している想定だが要実機確認) |
| Phase 4 履歴 ingest が 429 連発 | 既存の並列度/delay 調整 (現セッションで実証済の `batch=8, delay=10` が安全圏) |
| 検索精度が逆に悪化 | Phase 5 で気づいたら Phase 0 の bak ディレクトリから戻す |

---

## コスト試算

- Phase 3 再 embedding (1527 件 × 3-large): 約 **$0.05**
- Phase 4 履歴 ingest 中の Sonnet (extract/dedup/rewrite/rerank): subscription quota で OAuth 経路 → 追加課金なし (overage 経路に乗ること前提)
- Phase 4 中の embedding (新規 fact 数十件): $0.01 未満
- 合計実質コスト: **$0.05 + サブスク内**

---

## 修正対象ファイル

- 編集: [.env](../../.env) — `MEM0_EMBEDDER_MODEL=text-embedding-3-large` を追記
- 新規: [scripts/restore_mem0_from_json.py](../../scripts/restore_mem0_from_json.py) — JSON dump を `mem.add(infer=False)` でループ挿入
- (任意, Phase 6): [.env](../../.env) — `MEM0_RERANK_MODEL=claude-opus-4-6`

---

## オープン課題

1. **`infer=False` の SDK 挙動確認**: mem0 SDK v1.x で `mem.add([{role,content}], infer=False)` を呼ぶと「LLM 抽出をスキップして与えた content をそのまま fact として保存」する想定。実機検証が必要 (Phase 3 で実行)
2. **mem0 SDK `dimensions` バグ ([#4153](https://github.com/mem0ai/mem0/issues/4153))**: 3072 dim のままだと ChromaDB サイズが約 2 倍 (9MB → 18MB)。`dimensions=1536` で 3-large の matryoshka 機能を使えれば dim 同じで精度向上だが、mem0 SDK が API に正しく渡さない不具合あり。修正待ち
3. **3-large + 3072 dim での dedup 挙動**: cosine 0.95 / 0.80 のしきい値が現状 1536 dim 向け。3072 dim だと cosine 分布が変わる可能性。Phase 5 検証時に dedup の hit rate を観察
4. **タイミング**: 現在 (14:50 JST) 並列 8 ingest が動いてる (PID 1099508)。**完走を待ってから Phase 0 に入る** (進行中の DB を完走前に消すと、いま走ってる ingest 分の embedding コストが無駄になる)
