# Mem0 v4 実装Plan — 3つの穴をメタデータ拡張で塞ぐ

作成: 2026-07-03 かなえ（haroin発注）
系譜: v1(SQLite一次) → v2(ファイル一次) → v3(既存層強化に転換) → **v4(v3を実装可能な粒度に詳細化 + 3社/gpt-5.5-pro検算を統合)**
検算: gpt-5.5非pro / Gemini 3.1 Pro / DeepSeek V4 Flash（3社完了）+ gpt-5.5-pro（oracle, 回収中）

> **本Planの位置づけ**: v3(`docs/memory_redesign_plan.md`)の方針を、実コード走査(services/mem0/ 6ファイル精読)に基づく**実装可能な粒度**へ落とし込んだもの。§8にgpt-5.5-proの最上位評価を統合して確定する。

---

## 1. 確定した診断（コードで実証済み）

v3の「3つの穴」を実コードで確認した:

| 穴 | 実証箇所 | メカニズム |
|---|---|---|
| **(a) attribute欠如** | `dedup.py check_near_dup` は `search_memories` の embedding近傍(距離≤0.35)にしか矛盾判定を走らせない | 「体重60kg」→「体重58kg」のような数値更新は文面が離れ、距離ゲートを越えられず**矛盾検出に到達せず並置**される |
| **(b) condition欠如** | `conflict.py classify_conflict` は addition/update/correction を返すが、「朝/昼」等の**条件を保存する場所がない** | 時間帯依存の習慣が統合形に畳めず、単純並置または誤って update 判定される |
| **(c) scope欠如** | `ingest.py _ALLOWED_CALLER_META` に scope 概念なし。`search.py _is_fact_valid` は valid_to のみ判定 | 特定会話限定のメタ情報が global fact として保存され、後日無関係な場面で注入される |

## 2. 確定した設計判断（3社合意 → 採用）

| 論点 | v3の未決 | 3社の結論 | v4採用 |
|---|---|---|---|
| attribute語彙 | 自由 or 統制 | **統制語彙**（自由だと表記ゆれで決定ルートがすり抜ける、3社一致） | 小さな統制語彙 + `custom:*` 逃がし |
| condition評価 | 検索時 or 注入時 | **三層**（保存時構造化→検索時ソフト→注入時明示、3社一致） | 三層 |
| conflictキー | user×attribute | **+scope+condition**（過剰失効防止、3社一致） | `user + attribute + compatible_scope + compatible_condition` |
| 工数 | 4.5日 | **9.5日**（読み出し系/移行/評価が未計上、gpt/deepseek） | フェーズ分割で実計上（§4） |

## 3. アーキテクチャ原則（v3から継承）

- **新ストレージ層を作らない**。ChromaDB + mem0 OSS 1.0.11 の既存構成を維持
- 全変更を **config フラグでゲート**（既存挙動を壊さない、即ロールバック可）
- **決定的ルートは embedding に依存しない**（メタデータ完全一致検索）
- 既存の `get_episode_facts`（search.py:526）が `mem.get_all(filters={...})` パターン（SDK非対応時は全件+Pythonフィルタにフォールバック）を確立済み → attribute検索に流用

---

## 4. 実装フェーズ（詳細実装点）

### Phase A — attribute + 決定的conflictルート（穴a）★最優先

**A-1. attribute 抽出** — `services/mem0/extract.py`
- `_EXTRACT_PROMPT` に統制語彙セクションを追加し、facts スキーマに `"attribute"` フィールドを追加
- 統制語彙の初期セット（`weight` `height` `residence` `occupation` `affiliation` `relationship_status` `current_focus` `habit.<x>` `preference.<domain>` `possession.<x>` `custom:<free>`）
- `extract_full_signals` の facts validation（現状 speaker/text のみ）に `attribute` を通す（不明時は空文字列 = 決定ルート対象外）

**A-2. attribute メタデータ付与** — `services/mem0/ingest.py _insert_one`
- `fact_meta` に `attribute`（空文字列可、None不可＝ChromaDB制約）を追加

**A-3. 決定的conflictルート** — `services/mem0/ingest.py add_memories`
- dedup gather の後に新関数 `_attribute_conflict_check(mem, user_id, candidate)` を追加:
  - `attr = candidate.get("attribute")` が非空のとき、`mem.get_all(user_id, filters={"attribute": attr})`（get_episode_facts パターン, TypeError時は全件+Pythonフィルタ）で **valid な既存 fact** を取得
  - scope/condition が両立する既存 fact だけを対象に `classify_conflict(existing_text, candidate_text)` を実行
  - `update`/`correction` → 既存の `conflict_archive` 機構に (existing_id, ctype) を積む（既存の `_archive_outdated_facts` がそのまま処理）
- これは embedding dedup と**並行して走る決定ルート**。距離ゲートを越えられない数値更新をここで捕捉
- config: `MEM0_ATTRIBUTE_ENABLED`, `MEM0_ATTRIBUTE_CONFLICT_ENABLED`

**テスト**: `test_mem0_ingest.py` に「距離0.5のペアでも同一attributeなら conflict archive される」ケース、`test_mem0_extract_*` に attribute 抽出

### Phase B — condition（穴b）
- `extract.py`: facts に `"condition"`（`time:morning|time:night|context:work|...` の構造化文字列、空可）を追加
- `ingest.py _insert_one`: `fact_meta` に condition 付与
- `format.py`（注入時）: condition 付き fact を自然文で明示（「朝は〜」）
- A-3 のキー両立判定に condition を組み込む（別 condition は矛盾扱いしない）
- config: `MEM0_CONDITION_ENABLED`

### Phase C — scope（穴c）
- `extract.py` or `ingest.py`: 会話メタ情報（発言者訂正・その場限りの指示）を自動判別して `scope` 付与（`global` / `conversation:<channel_id>` / `meta`）
- `ingest.py`: `_ALLOWED_CALLER_META` に scope 追加、`_insert_one` で付与
- config: `MEM0_SCOPE_ENABLED`

### Phase R — 読み出し系改修（3社指摘・別計上）
- `search.py _is_fact_valid` に scope フィルタを相乗り（`conversation:<id>` は現在の channel_id 以外で除外）。呼び出し側から現在の channel_id を渡す配管が必要
- condition マッチング: 現在の時間帯コンテキストで condition 付き fact のスコアを調整（ソフト）or 注入時プロンプト表現に委ねる
- config: `MEM0_SCOPE_FILTER_ENABLED`（default False → 検証後 True）

### Phase M — 既存データ移行（3社指摘・別計上）
- `scripts/mem0_backfill_attributes.py`: 既存 fact を Haiku で再分類し attribute/scope をバックフィル（dry-run付き、バッチ、無停止）
- 新規適用を先行し、既存は段階移行（誤注入の継続を止める）

### Phase E — 回帰評価セット（3社指摘・別計上）
- `tests/fixtures/mem0_eval/`: 属性更新 / 条件付き習慣 / 局所scope混入 のケース
- `scripts/eval_mem0_memory.py`: 上記ケースで before/after のヒット・矛盾解消率を測定（効果検証。これ無しでは4.5日実装しても効果不明）

### Phase D — 手続き店（v3から）
- `scripts_registry.md`: 成果物レジストリ（path/用途/実行例/依存データ源）

### Phase F — 即時修正パス（v3から）
- 会話中のユーザー訂正を検知 → 既存 `_archive_outdated_facts` を即時呼び出し（夜間を待たない）

---

## 5. config フラグ設計（config.py:1217-1331 の MEM0 セクションに追加）

```python
MEM0_ATTRIBUTE_ENABLED          = _bool_env("MEM0_ATTRIBUTE_ENABLED", False)  # Phase A: 段階リリース
MEM0_ATTRIBUTE_CONFLICT_ENABLED = _bool_env("MEM0_ATTRIBUTE_CONFLICT_ENABLED", False)  # A-3 決定ルート
MEM0_CONDITION_ENABLED          = _bool_env("MEM0_CONDITION_ENABLED", False)  # Phase B
MEM0_SCOPE_ENABLED              = _bool_env("MEM0_SCOPE_ENABLED", False)      # Phase C
MEM0_SCOPE_FILTER_ENABLED       = _bool_env("MEM0_SCOPE_FILTER_ENABLED", False)  # Phase R 読み出し
```

全て default False → 実装・テスト後に段階的に True 化。既存挙動は無変更のまま入る。

## 6. テスト計画

| ファイル | 追加テスト |
|---|---|
| test_mem0_extract_*.py | attribute/condition/scope の抽出・統制語彙マッピング・空フォールバック |
| test_mem0_ingest.py | 決定ルート（距離遠でも同一attribute→archive）、scope/condition キー両立 |
| test_mem0_conflict.py | attribute経由 classify_conflict 呼び出し |
| test_mem0_search.py | scope フィルタ（conversation scope の除外）、condition スコア調整 |

## 7. ロールバック・段階リリース

- 各フェーズ独立フラグ → 問題があればそのフラグだけ False
- attribute/condition/scope は**追加メタデータ**なので、フラグOFFなら既存の検索・dedup は完全に元の挙動
- `MEM0_HIDE_ARCHIVED=false` の既存ロールバック（archive済み fact 復活）はそのまま有効

## 8. gpt-5.5-pro（最上位モデル）評価（2026-07-03 統合済み）

Responses API 直・effort=medium で回収（TPM 50k 制約下、oracle は effort 制御不可のため直叩き。total 21.5k tokens）。**結論: 診断も解法も構造的に妥当、進めてよい。既存スタック活用の範囲内で OK**（3社と一致）。ただし5点の追加提案:

1. **subject_entity_id**: 「先輩の体重」と「母の体重」「猫の体重」は同じ `weight` でも別 slot。決定ルートのキーに subject が要る（3社も見落とし）
2. **typed value_json**: 「60kg / 60キロ / 132lb」を canonical 化（type/value/unit/polarity）。この層がないと最終的に LLM 判定頼みが残る
3. **attribute registry with cardinality/conflict_policy**: single_value / multi_value / temporal / set_like。趣味・アレルギー・家族（多値・追加型）を single 扱いすると追加を更新で消す
4. **condition compatibility（完全一致でなく same/disjoint/overlap/subset/superset）**: 「朝」「平日の朝」「午前中」は完全一致しないが重なる。disjoint のみ addition、それ以外は conflict 判定へ
5. **source と applicability scope の分離**: 「前は60kg今は58kg」は発話元は特定会話だが適用は global。scope=conversation を雑に付けると global 訂正が会話に閉じ込められる

**追加リスク（要留意）**: condition=null の意味（unconditional/unknown/not_extracted の混在、backfill は unknown）/ condition の default+exception（普段コーヒー・夜紅茶、specific 優先）/ cross-attribute conflict（likes_coffee vs avoids_caffeine）/ multi-value 誤更新 / LLM backfill 汚染（schema_version・backfill_confidence・shadow mode）/ memory poisoning（facts/instructions/permissions 分離）/ 注入時の condition flattening（注入時も condition/scope を明示）

**最終形の方向**: `structured fact layer（正本・truth maintenance）+ Chroma/vector（想起）+ LLM（抽出補助）` の分離が理想（embedding は数値・否定・時制・条件に弱く、Chroma は truth maintenance DB ではない）。ただし**今の規模（個人 bot・数千〜数万 fact・single writer・非医療/法務）なら現行案の延長で十分**。structured fact layer（小さな SQLite/Postgres fact table）を薄く足すのが最も費用対効果が高い（+数日〜2週間）。event-sourcing は監査/replay/rollback が要るとき、KG-first は multi-entity 推論が主目的のとき、CRDT は不要（semantic conflict には効かない）。

### 本Planへの反映（2026-07-03）
- **即反映（低コスト・実装済み）**: (5) source/scope 分離 → `extract.py` の scope プロンプトを「適用範囲」に明確化し「訂正は global」を明示。(1) subject → attribute は先輩本人の属性にのみ付与（他者は空）とプロンプトに明示
- **v5 実装済み（2026-07-03 同日追加）**:
  - (3) **cardinality registry**: `services/mem0/attribute_registry.py` — weight=temporal / residence 等=single は更新型で archive、habit.*/preference.*/possession.*/custom:* は set_like（追加型）で archive しない（趣味・アレルギーの追加を更新で消さない）
  - (4) **condition compatibility**: 完全一致でなく same/disjoint/subset/superset/unknown 判定。disjoint のみ addition、それ以外は conflict 判定へ（「朝」と「平日の朝」の重なりを見逃さない）
  - (5) **cross-attribute conflict group**（Bundle 1 T4）: `CONFLICT_GROUPS` + `get_conflict_group()`。決定ルートが「同一 attribute or 同一 group」で照会（likes_coffee vs avoids_caffeine 型）。archive 判断は「消される側=既存」の cardinality 基準。初期は空枠で運用観測ペアを追加
  - **Bundle 1 T1（Phase R 配線）**: search.py 6関数 + bot.py 3呼び出し点に `current_channel_id` を伝搬。`get_core_memories` は scope 判定をキャッシュ取り出し後に実施（channel 間キャッシュ汚染防止）。graph expand / episode bundle 経路にも `_is_fact_valid` を適用（archived・他会話 scope のリーク遮断）。ingest の dedup 検索にも channel を配線
  - **Bundle 1 T2（flattening 防止）**: `format.py _fact_meta_suffix` が `[条件: time:night]` `[この会話限定]` `[メタ情報]` を注入行に付与。凡例にも追記
  - **Bundle 1 T3（poisoning ガード）**: `_EXTRACT_PROMPT` に「bot への運用指示・ルール変更・権限要求は fact 化しない／本人の事実の更新・訂正は fact」を明記
  - テスト: `test_mem0_attribute.py` 91 tests / mem0 全体 538 passed
- **Bundle 2 実装済み（同日）**:
  - **T6 backfill 硬化**: `scripts/mem0_backfill_attributes.py` — Haiku に confidence(high/low) を自己申告させ **low は --apply で書かない**。apply 時は `schema_version=1` + `backfill_confidence` を metadata に残す（backfill 由来行の特定・巻き戻し用）。`--shadow out.jsonl` で判定だけ書き出して人が確認 → --apply の運用。condition は backfill では推定しない（既存 fact の condition は unknown ≠ unconditional）
  - **T7 typed value**: `extract.py _normalize_value`（quantity のみ受理）→ `ingest.py` が `value_json` として保存（`MEM0_TYPED_VALUE_ENABLED`）。決定ルートに `_compare_typed_values_shadow` — 両者 quantity なら same/changed/unit_mismatch を `[ATTR_VALUE_COMPARE]` でログ（**挙動には影響しない観測**。classify_conflict との一致率を見て置換判断）
- **Bundle 3 実装済み（同日）**:
  - **T8a fact_store**: `services/mem0/fact_store.py`（`STATE_DIR/mem0_facts.sqlite`、graph.py パターン）。attribute 付き fact の**導出インデックス**（正本は Chroma のまま、壊れたら backfill で再構築可）。supersedes/superseded_by 連鎖 + valid 期間で属性履歴を保持。`MEM0_FACT_STORE_ENABLED` で決定ルートの照会が mem.get_all(全件走査) → SQL lookup に切替（store 空なら Chroma フォールバック）。ingest 成功時 record / archive 時 supersede / backfill --apply 時に初期投入
  - **T8b 評価セット**: `tests/fixtures/mem0_eval/scenarios.json`（4類型×3: 属性更新/条件付き習慣/scope混入/多値保護）+ `scripts/eval_mem0_memory.py`（オフラインは classify スタブで決定的、`--live` で実 Haiku）。**オフライン 12/12 green**。`tests/test_mem0_eval_harness.py` で CI ゲート化
- **残る将来項目**: condition compatibility の overlap 判定強化（現在は同軸プレフィックスのみ）、typed value の単位換算（kg⇔lb）、`[ATTR_VALUE_COMPARE]` 観測後の classify 置換、CONFLICT_GROUPS への実運用ペア追加。いずれも運用データ待ち

---

## 9. 実装進捗（2026-07-03）

3社が全員一致で「必須」と評価した核心（Phase A/B/C）を先行実装。全フラグ default
False なので**既存挙動は1ビットも変わらない**（有効化して初めて動く）。

| Phase | 状態 | 実体 |
|---|---|---|
| **A** attribute + 決定ルート | ✅ 完了 | config フラグ / extract.py `_normalize_attribute` + 統制語彙プロンプト / ingest.py `_attribute_conflict_check` + `_get_facts_by_attribute`（embedding非依存） |
| **B** condition | ✅ 完了 | extract.py `_normalize_condition` / ingest.py condition メタ付与 + 決定ルートの condition 両立判定（別条件は矛盾扱いしない） |
| **C** scope | ✅ 完了 | extract.py `_normalize_scope` / ingest.py scope 付与（conversation は channel_id 紐付け）/ search.py `_is_fact_valid` に scope フィルタ（誤注入防止） |
| **R** 読み出し配線 | 🔶 core済/配線残 | `_is_fact_valid(current_channel_id=)` の口は実装済み。bot.py recall パスからの channel_id 配線は後続（`MEM0_SCOPE_FILTER_ENABLED` default False なので無効化中は無影響） |
| **M** backfill | ✅ 骨格 | `scripts/mem0_backfill_attributes.py`（既存 fact を Haiku 再分類→attribute 付与、dry-run 既定・--apply で実適用）。実行は先輩が dry-run 確認後 |
| **E** 評価拡充 | 🔶 一部 | 決定ルートの回帰は `test_mem0_attribute.py`(51 tests) でカバー。実データ効果測定は運用後 |

テスト: `tests/test_mem0_attribute.py` 51 passed / mem0 全体 498 passed（回帰ゼロ）。

**有効化手順（段階リリース）**: `.env` に `MEM0_ATTRIBUTE_ENABLED=true` →
様子見 → `MEM0_ATTRIBUTE_CONFLICT_ENABLED=true` → `MEM0_CONDITION_ENABLED` →
`MEM0_SCOPE_ENABLED` → (bot.py 配線後) `MEM0_SCOPE_FILTER_ENABLED`。各段でログ観測。
