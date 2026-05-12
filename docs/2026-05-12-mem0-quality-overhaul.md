# Mem0 記録品質改善 + リファクタリング作業録

- **作業日**: 2026-05-12
- **対象**: かなえ Discord Bot (`/home/ubuntu/discord-bot/`) の Mem0 長期記憶系統
- **計画書**: `~/.claude/plans/partitioned-wibbling-sifakis.md` (作業時点)
- **背景となった全件分析**: Mem0 dump 860件を Opus 4.7 で分類した結果が `/tmp/mem0_taxonomy.md` と `/tmp/mem0_classified.json` に保存済み

---

## 1. 課題と root cause

### 課題1: dedup肥大

Mem0 dump 860件中 **586件 (68%)** が重複候補グループ (`dup-*`) に属する状態だった。Mem0 内蔵 LLM fact-extractor が `Prefers ~` / `Likes ~` / `Has ~` といった英語ラベルを量産していて、「本人の輪郭」より「Mem0が解釈した先輩像」が肥大化していた。

### 課題2: 複数人混入

同一 user_id (先輩 Discord ID `573489206240870410`) に、お父さん (`b0nchichi` / `1037285234200297522`)・watabegg (`826096545514127400`)・マッチングアプリ相手 (`505737845986820116`) など他者の発言由来情報が混ざっていた。

#### root cause (調査で確定)

`bot.py` 旧 L862-867 が `services.session.load_history_channel()` で channel 全体の過去10ターンを取得し、 `add_memories` に渡す `msgs` の **system message** として流し込んでいた。

```python
msgs[0] = {"role": "system", "content": "[Recent conversation context]\n<channel 過去10ターン (他者発言含む)>\n[End of context]"}
msgs[1] = {"role": "user", "content": <送信者本人のテキスト>}
msgs[2] = {"role": "assistant", "content": <bot応答>}
↓ add_memories(msgs, mem0_user_id=<送信者ID>, metadata=...)
```

Mem0 の LLM fact-extractor は `role` を区別せず全テキストから抽出するため、 system message に紛れた他者発話まで **送信者 user_id の事実として保存**されていた。

### リファクタリング余地

1. `services/mem0_service.py` (552行) に OAuth管理 / fact-extractor / metadata分類 / search / rewrite / rerank が同居 (god-object)
2. `search_memories_smart` / `_rewrite_query` / `_rerank_memories` を実装済みだが `bot.py` で未使用
3. `_build_mem0_queries` が `bot.py` L470 と `services/prompt_builder.py` に同名重複
4. `_get_mem0()` が global singleton + lazy init + OAuth 副作用、Mock困難

---

## 2. 改善アーキテクチャ

```
bot.py
  ├ Recall path
  │   PromptBuilder.build_mem0_queries(raw_user_text)   ← 重複削除済 (P3)
  │     → search_memories_smart(uid, limit=5)            ← P3 で初稼働
  │         _rewrite_query (Haiku 4.5) で正準キーワード変換
  │         _rerank_memories (Haiku 4.5) で関連度再評価
  │     → format_memories_for_prompt
  │
  └ Ingest path
      add_memories(user_text=, assistant_text=, user_id=, context_hint=, metadata=)
        → extract_facts_for_self()  ← P2 で新設
            Haiku 4.5 で <user_text>/<assistant_text> から self-only fact 抽出
            <context_hint> は理解の助けのみ・fact化禁止 (LLM指示 + 正規表現ガードの二段防御)
        → filter: speaker=='self' AND importance>=3 AND not near-dup
            client-side near-dup: search_memories(score>=0.85) で既存メモリと衝突回避 (P2.5)
        → mem.add(messages=[fact], infer=False)
            Mem0 標準抽出はバイパス、日本語 fact を 1件ずつ raw insert
```

### ファイル分割 (P4)

```
services/mem0/
├── __init__.py    # public API re-export (1.1 KB)
├── client.py      # _get_mem0 singleton + OAuth + get_all/delete housekeeping (4.5 KB)
├── ingest.py      # add_memories (P2 gate + legacy fallback) (5.3 KB)
├── search.py      # search/search_multi/search_smart + _rewrite + _rerank (7.3 KB)
├── extract.py     # extract_facts_for_self + _classify_for_metadata + prompts (6.9 KB)
└── format.py      # format_memories_for_prompt (497 B)

services/mem0_service.py   # legacy re-export shim (459 B, 元24KB)
services/mem0_service.py.bak.20260512-prefP4  # 旧ファイル全体バックアップ
```

依存方向（循環なし）:
```
__init__.py    →  全配下
ingest.py      →  client, extract, search
search.py      →  client
extract.py     →  oauth_manager (anthropic 直接)
client.py      →  oauth_manager
format.py      →  (依存なし)
```

---

## 3. Phase 別実装記録

### Phase 0: 観測パッチ

#### 目的
効果測定のbaseline取得。

#### 変更
- `services/mem0_service.py` `add_memories` 内: 抽出された fact の先頭60字を log.info
- `bot.py` `_mem0_store` クロージャ内: msgs の role/length 構成を log.info

#### ログサンプル
```
Mem0 ingest: uid=573489... msgs=2 [user=145,assistant=87]
Mem0: extracted 3 memories user=573489... | TOEIC対策を本格始動 | ...
```

---

### Phase 1: system-message 渡し撤去 ★最重要止血★

#### 目的
複数人混入の根本経路を遮断。

#### 変更
- `bot.py` の `caller_is_admin` ブロック (旧 L843-880):
  - `_ctx_for_mem0 = conv_context or ""` と `load_history_channel()` 呼び出しを `MEM0_LEGACY_CONTEXT` フラグで囲む (デフォルト false)
  - `_mem0_store` クロージャ内、 system message の append を同フラグでgate
  - 結果として msgs は `[user, assistant]` 2要素固定 (admin発話 + bot応答のみ)

#### フラグ
- `MEM0_LEGACY_CONTEXT` (default `false`)
  - `true` にすれば旧挙動 (channel history を system message に流す) に戻る
  - `.env` 編集 + `sudo systemctl restart discord-bot.service` で即時切替

#### 期待効果
- お父さん/watabegg/マッチングアプリ相手の発言が「先輩の事実」として保存される経路を遮断
- 「先輩がX (他者) について引用した」場合は引用テキスト自体が user_text に入るので、Mem0は「先輩が引用した」と認識しやすい

---

### Phase 2: 抽出ゲート + infer=False ★dedup根治★

#### 目的
`Prefers ~` / `Likes ~` の英語ラベル量産を遮断、日本語 self-fact だけ蓄積。

#### 新規実装
- `services/mem0/extract.py`:
  - `_EXTRACT_PROMPT`: `<context_hint>` (理解の助けのみ) / `<user_text>` / `<assistant_text>` の3ブロック構造を Haiku に渡す。出力契約は `{facts: [{speaker, text, importance, category, subcategory, tags}]}`
  - `extract_facts_for_self(user_text, assistant_text, context_hint)`: Haiku 4.5 (OAuth経由) 呼び出し、 LLM が `speaker:'self'` と判定したものだけ返却。さらに caller 側で `speaker == 'self'` の正規表現再フィルタ (二段防御)
- `services/mem0/ingest.py` の `add_memories`:
  - シグネチャを `(user_text=, assistant_text=, user_id=, context_hint=, metadata=)` に拡張
  - 旧 `(messages, user_id, metadata)` シグネチャも互換維持 (messages → user_text/assistant_text/context_hint を内部で抽出)
  - `MEM0_GATE_ENABLED=true` (default) なら:
    1. `extract_facts_for_self` で抽出
    2. `speaker == 'self'` & `importance >= MEM0_GATE_MIN_IMPORTANCE` でフィルタ
    3. (`MEM0_CLIENT_DEDUP_ENABLED=true` なら) `search_memories(score>=0.85)` で client-side dedup
    4. 各 fact を `mem.add(messages=[{role:user, content:fact}], user_id, metadata, infer=False)` で個別 raw insert
  - `MEM0_GATE_ENABLED=false` なら旧 Mem0 標準抽出ルート (フォールバック)

#### フラグ
| 変数 | default | 役割 |
|------|---------|------|
| `MEM0_GATE_ENABLED` | `true` | ゲート有効/旧ルート切替 |
| `MEM0_GATE_MIN_IMPORTANCE` | `3` | 保存 fact の最小 importance |
| `MEM0_GATE_MODEL` | `claude-haiku-4-5-20251001` | ゲート LLM モデル |
| `MEM0_CLIENT_DEDUP_ENABLED` | `true` | search 経由 near-dup スキップ |
| `MEM0_CLIENT_DEDUP_THRESHOLD` | `0.85` | near-dup スコア閾値 |

#### 検証結果
sandbox user_id で `add_memories` 呼出:

入力会話:
```
system: 他の人: ashitabo関連でwatabeggさんと連絡取りたい  ← context_hint
user:   ashitaboのrecruitmentThread機能、明日マイグレーション流す予定
assistant: 了解、pnpm db:migrate で本番反映ですよね先輩
```

結果:
- 抽出 facts: 1件
- 保存: `imp=4 cat=project/ashitabo-migration gate_version=v2`
- text: `"ashitaboのrecruitmentThread機能について、明日マイグレーションを実行する予定がある"`
- tags: `['ashitabo', 'recruitmentThread', 'migration', 'db']`
- system message の「watabeggさん」発言は **fact 化されず** (期待通り)

---

### Phase 3: smart search 切替 + 重複コード解消

#### 目的
- `search_memories_smart` を初実戦投入
- `_build_mem0_queries` の bot.py / prompt_builder.py 重複を解消
- `add_memories` を新シグネチャで直呼び (shim 経由廃止)

#### 変更
- `bot.py` L33: import から `search_memories_multi` 削除、 `search_memories_smart` 追加
- `bot.py` L170-207 (旧): `_EN_STOP_WORDS` と `_build_mem0_queries` を削除 (37行減)、 `PromptBuilder.build_mem0_queries` に集約
- `bot.py` (旧 L599-602): `_build_mem0_queries(...)` → 直接 `search_memories_smart(raw_user_text, mem0_user_id, limit=5)` に変更 (queries 構築ロジックは smart 内部の rewrite で代替)
- `bot.py` (旧 L840-880): `_mem0_store` 内の msgs 構築を撤廃、新シグネチャで直呼び:
  ```python
  await add_memories(
      user_text=raw_user_text,
      assistant_text=response_text,
      user_id=mem0_user_id,
      context_hint=ctx_hint,  # MEM0_LEGACY_CONTEXT=true のときのみ非空
      metadata={"channel_id": ..., "channel_name": ...},
  )
  ```

#### フラグ
- `MEM0_SMART_SEARCH` (default `true`)
  - `false` で `search_memories_multi` ベースに戻る (rewrite/rerank はスキップ、 LLM 呼び出しコスト削減)

#### 期待効果
- recall ヒット率改善 (canonical キーワード変換で表記揺れ吸収)
- bot.py が 938 行 → 905 行に圧縮
- `_build_mem0_queries` の重複保守コスト解消

---

### Phase 4: ファイル分割 (機械的リファクタリング)

#### 目的
god-object 解消、テスト容易性向上。動作変更なし、純粋な機械リファクタ。

#### 変更
- `services/mem0_service.py` (552行) を `services/mem0/{__init__, client, ingest, search, extract, format}.py` の6ファイルに分割
- 旧 `services/mem0_service.py` は **互換 re-export shim** (459B) として残置 — `weekly_mem0_compact.py` が `_get_mem0`/`_refresh_mem0_client_token` の private symbol を import しているため
- バックアップ: `/home/ubuntu/discord-bot/services/mem0_service.py.bak.20260512-prefP4`

#### Public API (`from services.mem0 import ...`)
- `add_memories`
- `search_memories`, `search_memories_multi`, `search_memories_smart`
- `get_all_memories`, `delete_memory`
- `format_memories_for_prompt`
- `extract_facts_for_self`

#### Private (旧shim経由でアクセス可)
- `_get_mem0`, `_refresh_mem0_client_token` (client.py)
- `_classify_for_metadata` (extract.py)
- `_rewrite_query`, `_rerank_memories` (search.py)

#### 検証結果
| Check | Result |
|---|---|
| `from services.mem0_service import ...` (legacy path) | pass |
| `from services.mem0 import ...` (new path) | pass |
| `import bot` | pass |
| `import mcp_mem0_server` | pass |
| `/tmp/verify_p2_gate.py` | pass |
| `/tmp/verify_mem0_phases.py` | pass |
| `scripts/weekly_mem0_compact.py` | pass |
| `discord-bot.service` 再起動後 | `active` |

---

### Phase 5: weekly_mem0_compact.py 監視拡張

#### 目的
退行を週次で自動検知。Phase 1 の止血が破られていないかを継続監視。

#### 追加実装 (`scripts/weekly_mem0_compact.py`)

1. **Gate version 分布**: `metadata.gate_version` を `v2` / `v1` / `missing` で集計
2. **Other-speaker contamination 検知**: 正規表現で「お父さん|watabegg|b0nchichi|お母さん|お父様|父親」の主語位置出現を全 memory text にスキャン → **2件以上で exit code 2** (1件は引用文脈の可能性を残す)
3. **Intra-week near-duplicate pairs**: 直近1週間の新規 memory 同士に対し SequenceMatcher で `ratio >= 0.85` を検出、表示
4. **Exit code**: alert 検知時に非ゼロを返す → cron が systemd 通知できる

#### cron 登録 (P1の作業で既登録)
```
0 9 * * 0 /home/ubuntu/discord-bot/.venv/bin/python /home/ubuntu/discord-bot/scripts/weekly_mem0_compact.py >> /tmp/mem0_compact_cron.log 2>&1  # weekly Mem0 housekeeping
```

#### 実行結果 (2026-05-12 初回)
```
wrote /tmp/mem0_compact_20260512.md  total=860 untagged_new=0 growing=30
archive_cand=0 gate_v2=0 other_speaker=0 near_dup=2  ok
```
- `other_speaker=0` → 退行なし ✓
- `gate_v2=0` → P2 デプロイ後の新規 ingestion まだなので想定内
- `near_dup=2` → 既存 860 件内 (touched ではない)

---

## 4. 環境変数フラグ一覧

`.env` 編集 + `sudo systemctl restart discord-bot.service` で即時切替可能。

| 変数 | default | Phase | 役割 |
|------|---------|-------|------|
| `MEM0_LEGACY_CONTEXT` | `false` | P1 | `true` で旧 channel-history 流入挙動 |
| `MEM0_GATE_ENABLED` | `true` | P2 | `false` で Mem0 標準抽出ルート |
| `MEM0_GATE_MIN_IMPORTANCE` | `3` | P2 | 保存 fact の最小 importance (1-5) |
| `MEM0_GATE_MODEL` | `claude-haiku-4-5-20251001` | P2 | ゲート LLM モデル |
| `MEM0_CLIENT_DEDUP_ENABLED` | `true` | P2.5 | client-side near-dup スキップ |
| `MEM0_CLIENT_DEDUP_THRESHOLD` | `0.85` | P2.5 | near-dup スコア閾値 |
| `MEM0_SMART_SEARCH` | `true` | P3 | `false` で rewrite/rerank 無効化 |

---

## 5. 変更ファイル一覧 (VPS)

### 新規

```
services/mem0/
├── __init__.py     1.1 KB
├── client.py       4.5 KB
├── ingest.py       5.3 KB
├── search.py       7.3 KB
├── extract.py      6.9 KB
└── format.py       497 B
scripts/weekly_mem0_compact.py  (P5 拡張版)
docs/2026-05-12-mem0-quality-overhaul.md  (この文書)
```

### 更新

```
bot.py                          938 → 901 行 (重複削除)
services/mem0_service.py        24 KB → 459 B (re-export shim 化)
config.py                       MEM0_* フラグ7個追加 (L347後)
```

### バックアップ (rollback 用)

```
services/mem0_service.py.bak.20260512-prefP4     旧 24KB の全体
services/mem0_service.py.bak.20260512-1746       P2 デプロイ前
mcp_mem0_server.py.bak.20260512-1746             smart search 改修前
mcp_mem0_server.py.bak.20260512                  format フッタ削除前
~/.claude/CLAUDE.md.bak.20260512-1747            CLAUDE.md 旧版
```

---

## 6. 運用ガイド

### 定常運用
1. 週1で自動 cron 実行 (`0 9 * * 0`) → `/tmp/mem0_compact_<date>.md` 生成
2. exit code 2 (other_speaker contamination 2件以上) なら cron alert を確認
3. `gate_v2` カウントが増えていけば P2 ゲートが正常稼働中

### 退行を発見したら
1. `journalctl -u discord-bot.service` で起動エラー確認
2. `.env` で問題の Phase をフラグで off にして restart
3. それでもダメなら該当 backup を `cp` で復元 + restart

### Phase 1 退行検知の見方
weekly レポートの `Other-speaker contamination` セクションが空(0件)ならOK。1件出ても引用文脈の可能性あり、 2件以上なら exit 2 で alert。

### Phase 2 効果測定
- `journalctl -u discord-bot.service | grep "Mem0 gate"` で `facts=N kept=M skip(...)` ログを確認
- `kept / facts` 比率が高ければゲートが過剰に絞ってる可能性 (importance閾値見直し)
- `kept` が常に 0 ならゲート空振り、 `MEM0_GATE_ENABLED=false` でフォールバックも検討

---

## 7. 既存860件の扱い

**全 Phase を通じて既存 860 件は触らない方針**。

- Phase 1: 過去の混入記憶はそのまま (Cold tier に降格、検索精度に影響あり)
- Phase 2: 過去の `Prefers ~` 等の英語ラベルはそのまま
- 将来必要なら別途 `_classify_for_metadata` の bulk 再分類スクリプト (この作業で実装した `/tmp/apply_mem0_metadata.py`) を流用して整理可能

---

## 8. 参考リンク・関連ファイル

### 設計関連
- 計画書: `~/.claude/plans/partitioned-wibbling-sifakis.md` (作業当時のローカル) — Phase 別計画
- taxonomy 設計書: `/tmp/mem0_taxonomy.md` — 8 top-level / 38 subcategory の定義
- 分類結果: `/tmp/mem0_classified.json` — 既存 860 件への分類タグ

### 検証スクリプト (`/tmp/`)
- `verify_p2_gate.py` — P2 抽出ゲートの動作確認 (sandbox uid)
- `verify_mem0_phases.py` — P2 classify / P3a rewrite / P3b smart search
- `apply_mem0_metadata.py` — 既存 860 件への bulk metadata 適用 (再利用可能)

### コード本体 (`/home/ubuntu/discord-bot/`)
- `services/mem0/` — 分割後の主要モジュール群
- `services/mem0_service.py` — legacy re-export shim
- `bot.py` — Discord bot 本体、 ingest/recall 経路
- `services/prompt_builder.py` — `PromptBuilder.build_mem0_queries` の正典実装
- `scripts/weekly_mem0_compact.py` — 週次監視バッチ
- `config.py` — 環境変数フラグ定義 (L347後)

---

## 9. 作業中の特記事項

- VPS の OAuth (`~/.claude/.credentials.json`) を Anthropic SDK 経由でBearer auth に paッチして Haiku 4.5 を呼んでいる。 `oauth_manager.init()` + `ensure_valid_token()` のセットを extract/rewrite/rerank の各エントリで idempotent に呼ぶ実装
- `_classify_for_metadata` は P2 で extract_facts_for_self に役割が吸収されたが、 `MEM0_GATE_ENABLED=false` フォールバック時の旧ルート用に残置
- discord-bot.service の再起動は本作業中に複数回実施 (1回最大 ~4秒の応答停止)。 systemd の `Restart=always` 設定で自動復帰
