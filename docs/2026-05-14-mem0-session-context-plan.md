# Mem0 セッション/文脈中心化計画

**ステータス**: Draft (仕様詰め完了、実装未着手)
**先行 Phase**: [overhaul-implementation](../../discord-bot/docs/2026-05-14-mem0-overhaul-implementation.md) (Phase 1-4 完了済)

## Context

### すでに動いている短期文脈共有 (重複しないよう先に整理)

[services/session.py:append_jsonl](../services/session.py#L204) + [services/claude_api/entry.py:_resolve_session](../services/claude_api/entry.py#L341) で **bot 視点の短期文脈**は実現済:

- 同チャンネルの直近 turn を JSONL に保存 (MAX_MESSAGES_PER_CHANNEL × 2 = 40 件 tail)
- TTL 30 分以内なら同 session として継続 ([services/claude_api/session_store.py](../services/claude_api/session_store.py))
- resume 時に `prior_history` を Claude API の `messages[]` に積む ([entry.py:395](../services/claude_api/entry.py#L395))
- 結果: 先輩「あれの続き」→ Claude は messages[] 文脈から「あれ = 前ターンの○○」を解釈して**応答できる**

つまり「あれは〜」「さっきの〜」みたいな**短期指示語の解釈・応答**は本計画で改めて作る必要なし。

### 本計画が解決する別の 3 問題

1. **Mem0 検索クエリへの文脈付与** ([bot.py:686-694](../bot.py#L686-L694))
   - 現状 `search_memories_smart(raw_user_text)` は **raw user text 単独**で検索
   - 先輩「あれの続き」→ Claude の応答は文脈で動く (上記既存仕組み)
   - でも Mem0 検索 query = "あれの続き" のまま → **検索結果が的外れ**
   - 「あれ = Rust の話」を踏まえた Mem0 検索が欲しい

2. **過去 session の長期記憶化** (現状は jsonl tail rotation で消失)
   - 「先週話してた量子コンピュータの本」みたいな time-bound 参照
   - 既存 fact は単発で「○○について話した」という session-level 文脈が消える
   - session 終了時に Sonnet で要約 → Mem0 に永続保存

3. **fact ⟷ session の紐付け** (現状の Mem0 fact は session_id を持たない)
   - 「同じ会話の流れで出た fact 群」を取り出せない
   - graph layer の entity edge と組み合わせて session 1-hop / entity 1-hop の expansion 可能に

### 期待結果

- Mem0 検索が「query + 直近 N ターンの要約」で精度向上 (短期文脈 active な状態)
- 過去 session の要約が Mem0 fact として永続保存 (週/月単位の参照可能)
- fact が session_id で群化 (「同じ会話の流れで出た fact 群」を取り出せる)

---

## 仕様 (確定済)

| 項目 | 仕様 |
|---|---|
| **session の単位** | **topic 単位** (LLM で話題転換を動的検知) |
| **記憶の単位** | **2層構造**: fact (今まで通り単発) + session summary (新規、session 終了時に生成) |
| **既存 1126 fact の扱い** | **遡及して session_id を付与**: Discord 履歴から created_at + topic clustering で session 復元 |
| **検索クエリの文脈付与** | **する**: query string + 直近 N ターンの要約を embedding source に使う |

---

## アーキテクチャ

### データモデル

新規 metadata フィールド (fact-level):
- `session_id` (str, UUID): その fact が属する session
- `session_position` (int): session 内での発話順序 (1-N)

新規 fact 種別:
- `value_class="session_summary"` で識別
- text は Sonnet 生成の session 要約 (例: "Rust の trait と Generic について議論し、最終的に Generic 案で合意")
- metadata に `session_id` (1:1 で関連 fact 群を逆引き可)、`session_start_unix`, `session_end_unix`, `member_fact_count`

### Session boundary 検知

新規 `services/mem0/session_detector.py`:

```python
async def is_same_session(
    prev_turns: list[dict],   # 直近 N=5 turns
    new_turn: dict,            # 新発話
) -> bool:
    """LLM で『話題継続か』を判定。前 N=5 turns と new_turn を見比べる。"""
```

Sonnet に短い prompt で yes/no 判定。`max_tokens=500` (thinking 込み)。
判定が `no` (= 話題転換) のとき、**前の session を closure** → summary 生成、新 session 開始。

#### 短い fail-safe
- `idle_timeout=30min` で同じく session 終了 (TTL fallback)
- `max_session_turns=50` で強制 closure (暴走防止)

### Session 状態の永続化

`.state/mem0_sessions.jsonl` (新規):

```jsonl
{"session_id": "uuid", "user_id": "...", "channel_id": ..., "start_unix": ..., "end_unix": null, "turns": [{"role":"user","text":"...","ts":...}, ...]}
```

bot ターン毎に追記。session closure 時に `end_unix` set + summary 生成 → Mem0 に session_summary fact を投入。

### 既存 ingest フローへの統合

```
[現状]
turn → bot.py:_mem0_store()
     → add_memories(user_text, assistant_text)
     → extract_facts_for_self → 単発 fact

[改修後]
turn → bot.py:_mem0_store()
     → session_detector.is_same_session(prev_5_turns, new_turn)
        ├─ 継続 → 既存 session に append、add_memories は今まで通り (ただし fact metadata に session_id 付与)
        └─ 転換 → 前 session を close(summary 生成 + Mem0 投入)、新 session を open
     → add_memories(user_text, assistant_text, session_id=current_session_id)
```

### 検索パイプライン拡張

`services/mem0/search.py:search_memories_smart_v2`:

```python
async def search_memories_smart(
    query: str, user_id: str, *, limit: int = 5,
    recent_turns: list[dict] | None = None,  # NEW
):
    # 直近 N ターンを query に concat
    if recent_turns:
        context_summary = await _summarize_turns(recent_turns)  # 短い LLM 要約
        effective_query = f"{query}\n\n[直近の文脈] {context_summary}"
    else:
        effective_query = query
    # 既存パイプライン (rewrite → multi-search → graph_expand → reinforce + rerank)
    ...
```

加えて、検索結果に **session_summary fact** が含まれていたら、その session 内の関連 fact 全部を取りに行く 1-hop expansion を追加 (graph layer の entity expansion と同じ機構)。

---

## Phase 分け実装

### Phase A: Session detector + jsonl 永続化 (約 1 週)
- 新規 [services/mem0/session_detector.py](../../services/mem0/session_detector.py)
- 新規 [services/mem0/session_store.py](../../services/mem0/session_store.py) — jsonl の read/append/close
- LLM prompt 設計 + threshold tuning
- 既存 [bot.py:_mem0_store](../../bot.py) に session detection を組込み
- 単体テスト [tests/test_session_detector.py](../../tests/test_session_detector.py)

### Phase B: Fact に session_id 付与 (約 2 日)
- [services/mem0/ingest.py:add_memories](../../services/mem0/ingest.py) の signature に `session_id` を追加
- fact_meta に `session_id`, `session_position` を保存
- 既存テスト更新

### Phase C: Session summary 生成 + Mem0 投入 (約 3 日)
- session 終了時に Sonnet で要約生成 (`max_tokens=2000`)
- `value_class="session_summary"` で Mem0 に投入
- 関連 fact 群との 1:N 関係を metadata で保持
- 単体テスト

### Phase D: 検索クエリへの文脈付与 (約 3 日)
- [services/mem0/search.py:search_memories_smart](../../services/mem0/search.py) に `recent_turns` 引数追加
- `_summarize_turns` ヘルパー (短い LLM 要約、cache 可能)
- [bot.py:690](../../bot.py) で `recent_turns` を渡す (channel history から作る)
- 検索結果に session_summary が混じった場合の expansion ロジック

### Phase E: 既存 fact への session_id 遡及付与 (約 1 日 + LLM 1-2 時間)
- 新規 [scripts/backfill_session_ids.py](../../scripts/backfill_session_ids.py)
- 既存 1126 fact の `created_at_unix` を見て、時系列で近い fact 群を topic clustering (Sonnet)
- 結果として推定 session_id を metadata に追加 (`session_id_source="backfill"` で識別)
- session_summary fact も遡及生成 (任意)

### Phase F: docs + monitoring (約 1 日)
- 運用ドキュメント更新
- bot.log で session 開始/終了/summary 生成の頻度観察
- 1 週間運用してから判断: detection 精度の調整、summary 品質の評価

---

## 修正対象ファイル

| ファイル | 変更 | Phase |
|---|---|---|
| 新規 [services/mem0/session_detector.py](../../services/mem0/session_detector.py) | topic boundary 検知 | A |
| 新規 [services/mem0/session_store.py](../../services/mem0/session_store.py) | session jsonl 永続化 | A |
| 編集 [bot.py](../../bot.py) | _mem0_store に session 検知統合、検索に recent_turns 渡し | A, D |
| 編集 [services/mem0/ingest.py](../../services/mem0/ingest.py) | add_memories に session_id 引数 + metadata 保存 | B |
| 編集 [services/mem0/search.py](../../services/mem0/search.py) | recent_turns 文脈付与 + session_summary expansion | D |
| 新規 [scripts/backfill_session_ids.py](../../scripts/backfill_session_ids.py) | 既存 fact 遡及 | E |
| 新規 [tests/test_session_detector.py](../../tests/test_session_detector.py) | 単体テスト | A |
| 新規 [tests/test_session_store.py](../../tests/test_session_store.py) | 単体テスト | A |

---

## 既存実装との関係

| 既存 Phase | 関係 |
|---|---|
| Phase 1 (cleanup) | session_summary fact も `value_class` 持つので cleanup 経路で扱える。"transient" 判定で誤削除されないよう prompt 修正必要 |
| Phase 2 (Hebbian reinforcement) | session_summary が重複したら mention_count 上げる。fact-level reinforce は既存通り |
| Phase 3 (timestamp backfill) | created_at_unix を活用して session 復元 (Phase E の前提) |
| Phase 4 (graph) | session_summary fact も entity edge を持てる。検索時に session ⟷ fact ⟷ entity の 2-hop expansion が可能 |

---

## 検証

### Phase A 完了時
```bash
.venv/bin/python -m pytest tests/test_session_detector.py tests/test_session_store.py -v
# session 境界検知 + jsonl の write/read が確認できる
```

bot.log で:
```
session opened: id=<uuid> channel=<id> user=<id>
session closed: id=<uuid> turns=8 reason=topic_shift
```

### Phase C 完了時
- session_summary fact が Mem0 にロードされる
- `mcp__mem0__search "今週の会話"` で session_summary が hit する

### Phase D 完了時
- bot に「あれ、いいよね」と発話 → 直近 turn に Rust 話題があれば Rust 関連 fact が返る
- 検索 log で `recent_turns` を embedding source に使った跡が見える

### Phase E 完了時
```bash
sqlite3 .state/mem0_db/chroma.sqlite3 \
  "SELECT count(*) FROM embedding_metadata WHERE key='session_id';"
# 既存 fact の大半に session_id が付与されているか
```

### 統合検証 (Phase F)
1 週間運用して以下を観察:
- session 開始/終了頻度 (異常に多い/少ない場合は boundary 検知が荒い)
- summary 生成成功率
- 検索品質の体感 (「あれ」「さっき」系発話で関連 fact が引ける)

---

## オープン課題

1. **session 検知の LLM コスト**: 毎ターン 1 回 Sonnet で is_same_session を呼ぶと、現状の 3-5 LLM call/turn が 4-6 に増える。subscription quota 圧迫リスクあり。Haiku に下げる選択肢も
2. **session_summary の重複問題**: 同じ topic が複数 session で扱われた場合、summary 同士は cosine 高くなる。dedup 経路で reinforce か delete か判断必要
3. **recent_turns の取得**: bot.py で `fetch_channel_context` / `load_history_channel` がすでに直近 message を引いてくる。これを `_summarize_turns` の入力に使えば追加 fetch 不要
4. **session boundary の境界事例**: 「Rust の話 → ちょっと脱線して天気の話 → 戻って Rust」みたいな短期脱線をどう扱うか。閾値設計要
5. **既存 fact の session 遡及精度**: created_at が無い 700 件は遡及不可。`created_at_source="vector_match"` のものは精度が低い可能性
6. **session jsonl のサイズ**: 1 session 平均 5-10 turns、turn 200-500 chars。1日 30 sessions × 365日 → 約 5MB/年。問題ない規模
7. **同期 vs 非同期 session detection**: 検知に LLM 待ちが入ると bot 応答が遅くなる。fire-and-forget でやって 1 turn 遅れで boundary を decide する案あり

---

## 工数見積もり

| Phase | 工数 |
|---|---|
| A (detector + store) | 1 週 |
| B (session_id 付与) | 2 日 |
| C (summary 生成) | 3 日 |
| D (検索文脈付与) | 3 日 |
| E (既存 fact backfill) | 1 日 + LLM 数時間 |
| F (docs + monitoring) | 1 日 |
| **合計** | **約 2.5 週** |

各 Phase 完了後に検証 → 問題なければ次へ進む段階導入。Phase A+B+D で「リアルタイム文脈効果」は実感できる。Phase C は「過去 session の要約」が出るので運用後に効いてくる。
