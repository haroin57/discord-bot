# Kanae Self-Model（自己モデル）機能要件

## Status: implemented (Phase 1–3)
## Author: かなえ
## Date: 2026-05-15

---

## 1. 概要

`services/kanae_self` は、かなえの**中期的な内的状態**を管理するレイヤー。既存の2層（`services.emotion`: 揮発性の6軸感情 / `services.mem0`: 長期的事実記憶）の間に位置し、「最近何に関心を持っているか」「直前に何が起きたか」を表現する。

どちらも既存レイヤーでは表現できない：
- **Interest（関心トピック）**: Mem0は事実を正規化・重複排除するため、「今この瞬間どの話題に関心が高いか」のsalienceが消失する。Emotionは6軸の気分であり、トピックレベルの粒度を持たない。
- **Episode（出来事バッファ）**: Mem0は時系列を正規化するため、「3時間前にXがあって、その後Yを話した」という順序情報が失われる。

### 設計原則

- **fail-open**: ステートファイルの読み込みに失敗しても応答を止めない。空の状態から数ターンで再構築される
- **non-blocking**: 全てのI/Oはfire-and-forget。応答レイテンシに影響しない
- **bounded**: Interest上限100件、Episode上限30件。ステートファイルが際限なく肥大化しない
- **環境フラグで段階的に有効化**: 各Phase独立のフラグでon/offを切り替え可能

---

## 2. アーキテクチャ上の配置

### レイヤー構成

```
┌─────────────────────────────────────────────┐
│  services.emotion (揮発性 / 6軸感情)         │  寿命: セッション〜数時間
├─────────────────────────────────────────────┤
│  services.kanae_self (中期的自己モデル)       │  寿命: 数時間〜数日
│    ├── Interest: トピック × salience          │    半減期72時間で指数減衰
│    └── Episode: 会話要約リングバッファ         │    30スロット、FIFO
├─────────────────────────────────────────────┤
│  services.mem0 (長期的事実記憶)               │  寿命: 無期限
│    Mem0 / ChromaDB                           │    正規化 + 重複排除
└─────────────────────────────────────────────┘
```

### データフロー

```
[会話ターン終了]
  → extract_kanae_signals()         # Haiku 4.5で信号抽出
      ├── facts[]     → Mem0 ingest
      ├── interests[] → decay_and_upsert_interests()
      └── episode{}   → append_episode()
                           ↓
                    kanae_self.json (atomic write)

[次の会話ターン開始]
  ← load_state()
      ├── top_interests()           → プロンプト注入
      ├── recent_episodes()         → プロンプト注入
      └── augment_query_with_interests() → Mem0検索クエリ拡張
```

---

## 3. モジュール構成

| ファイル | 責務 |
|---|---|
| `state.py` | データモデル定義（Interest, Episode, KanaeSelfState）+ load/save |
| `interests.py` | 指数減衰 + upsert + GC + top-k取得 |
| `episodes.py` | リングバッファへのappend + 直近k件取得 |
| `recall.py` | Interest駆動のMem0クエリ拡張 |
| `format.py` | プロンプトブロックのレンダリング（個別 + 統合） |
| `__init__.py` | public API surface（load_state, save_state, データクラス群） |

---

## 4. 機能要件

### 4.1 Interest（関心トピック）トラッキング

#### FR-INT-01: 指数減衰

- 全Interestのsalienceは、ターンごとに `last_touched_at` からの経過時間に基づいて指数減衰する
- 半減期: **72時間**（`DECAY_HALF_LIFE_HOURS = 72.0`）
- 減衰関数: `exp(-ln(2) × hours / half_life)`
- タイムスタンプのパースに失敗した場合は経過時間を0（減衰なし）として扱う。不正なタイムスタンプが原因でトピックが消失することを防ぐ

#### FR-INT-02: Upsert（挿入/更新）

- `extract_kanae_signals` が返す `interests[]` 配列の各要素 `{topic, valence, arousal}` をupsertする
- **新規トピック**: salience = `0.3 + TOUCH_BUMP(0.15)` = 0.45、valence/arousalはそのまま記録
- **既存トピック**: salience += `TOUCH_BUMP(0.15)`（上限1.0）、mention_count++、valence/arousalはEMAブレンド
- トピック正規化: lowercase + strip + 空白圧縮 + 引用符/括弧除去、60文字上限

#### FR-INT-03: 感情EMA（指数移動平均）

- 各Interestに `valence_attached`（-1.0〜+1.0）と `arousal_attached`（0.0〜1.0）を付帯する
- EMA重み `α = 0.3`: 新しい観測値が付帯感情を30%引き寄せる
- 計算: `new_val = (1 - α) × old_val + α × observed_val`
- 単発の外れ値で付帯感情が急変することを防ぎつつ、繰り返しの文脈変化には追従する

#### FR-INT-04: ガベージコレクション

- salience < `GC_THRESHOLD(0.05)` のInterestは毎ターン削除される
- 0.05は、salience 1.0からの減衰で約7日に相当。1週間言及されなければ信号としての価値がないと判断する
- GC後にMAX_INTERESTS(100)を超える場合、salience降順で上位100件のみ保持する

#### FR-INT-05: Top-k取得

- `top_interests(state, k)` でsalience上位k件を返す
- ソートキー: `(salience, last_touched_at, mention_count)` の降順（タイブレーク含む）
- デフォルトk = 5

### 4.2 Episode（出来事）リングバッファ

#### FR-EP-01: Append

- `append_episode()` で新しいエピソードをバッファ末尾に追加する
- 必須パラメータ: `summary`（空文字列の場合はNoneを返しスロットを消費しない）
- オプション: `topics`（最大3件）、`valence_delta`（-1.0〜+1.0）、`importance`（1〜5）、`channel_id`、`episode_id`
- `episode_id` はMem0メタデータとの相互参照に使う。省略時はuuid4の先頭8文字を自動生成する

#### FR-EP-02: リングバッファ制約

- バッファ上限: `MAX_EPISODES = 30`
- 上限超過時は最古のエピソードから順に落とす（FIFO）
- `state.episodes[-MAX_EPISODES:]` でスライスすることでO(1)の切り詰め

#### FR-EP-03: Summaryの制約

- 最大120文字に切り詰める（プロンプトトークン節約）
- 空文字列のsummaryは無視する（「特に何も起きなかったターン」はスロットを取らない）

#### FR-EP-04: 直近k件取得

- `recent_episodes(state, k)` でバッファ末尾からk件を返す
- デフォルトk = 3

### 4.3 信号抽出（extract_kanae_signals）

#### FR-EXT-01: 統合抽出

- Haiku 4.5への単一API呼び出しで、1ターンの会話から `facts[]` + `interests[]` + `episode{}` を同時に抽出する
- 入力: `senpai_text`（先輩の発言、2000文字上限）、`kanae_text`（かなえの応答、2000文字上限）、`context_hint`（1000文字上限）
- 抽出プロンプトの注入攻撃防御: `<retrieved_memories>`, `<kanae_self_memory>`, `<kanae_state>` 等のタグをストリップしてから送信

#### FR-EXT-02: 出力スキーマ

```json
{
  "facts": [
    {
      "speaker": "self",
      "text": "<日本語fact>",
      "importance": 2,
      "category": "<8カテゴリの1つ>",
      "subcategory": "<kebab-case>",
      "tags": ["<tag>"],
      "entities": [{"name": "<canonical>", "type": "<6種の1つ>", "aliases": ["<alias>"]}]
    }
  ],
  "interests": [
    {"topic": "<canonical lowercase>", "valence": 0.0, "arousal": 0.0}
  ],
  "episode": {
    "summary": "<60字以内>",
    "topics": ["<canonical lowercase>"],
    "valence_delta": 0.0,
    "importance": 1
  }
}
```

- `interests` は最大3件に制限
- `episode.summary` が空の場合はエピソードなしとして扱う
- 全フィールドが空でも `{"facts": [], "interests": [], "episode": null}` を返す（None-check不要の保証）

#### FR-EXT-03: fail-safe

- API呼び出し失敗、JSON パースエラー、その他の例外は全てWARNINGログ出力後に空の結果を返す
- 抽出の失敗が応答フローを中断することはない

### 4.4 Recall拡張（Interest駆動のクエリ書き換え）

#### FR-RCL-01: クエリ拡張

- `augment_query_with_interests(query, state, k)` で、Mem0検索クエリにtop-kのInterestトピックを付加する
- 出力形式: `"{query} | topics: a / b / c"`
- Interestがない場合、stateがNoneの場合、k≤0の場合は元のクエリをそのまま返す（no-op保証）

#### FR-RCL-02: 目的

- 曖昧な検索クエリ（「あれ覚えてる？」等）に対して、かなえの現在の関心トピックをアンカーとして付与し、embedding検索の精度を向上させる
- 最大3トピック（`DEFAULT_TOPIC_COUNT = 3`）を注入。これ以上はembeddingが希釈される

#### FR-RCL-03: トピックリスト取得

- `top_interest_topics(state, k)` でトピック文字列のリストを返す
- Mem0メタデータの `interest_tags` フィールドへの書き込みにも利用される

### 4.5 プロンプトブロック生成

#### FR-FMT-01: 個別ブロック（レガシーレイアウト）

- `format_interests_block(state, top_k)`: `<kanae_interests>` タグで囲んだInterest一覧を生成する
  - 各行: `- {topic} (sal {salience:.2f}, {affect_label} {valence:+.2f}, {mention_count}回, 最終 {relative_time})`
  - affect_label: valence ≥ 0.5 → "好意+", ≥ 0.15 → "好意", ≤ -0.5 → "苦手-", ≤ -0.15 → "やや苦手", else "中立"
- `format_episodes_block(state, top_k)`: `<kanae_episodes>` タグで囲んだEpisode一覧を生成する
  - 各行: `- [{relative_time}] {summary}`
  - 時系列順（古→新）でレンダリングする

#### FR-FMT-02: 統合ブロック（Phase 3）

- `format_kanae_self_block()` で、4つの情報源を単一の `<kanae_self>` タグにまとめる
- 内部構造（読み順はメンタルモデルに合わせて固定）:
  1. `<state>`: 感情状態（`format_emotion_for_prompt` の出力から外側の `<kanae_state>` タグを除去して内包）
  2. `<interests>`: トピック一覧
  3. `<recent_episodes>`: 直近の出来事
  4. `<self_facts>`: Mem0から取得した自己事実（`<kanae_self_memory>` タグを除去して内包）
- 全セクションが空の場合は空文字列を返し、プロンプトへの注入自体をスキップする

#### FR-FMT-03: 相対時間表記

- `< 60秒` → "たった今"
- `< 1時間` → "{N}分前"
- `< 24時間` → "{N}時間前"
- `< 48時間` → "昨日"
- `≥ 48時間` → "{N}日前"
- パース失敗 → "不明"

#### FR-FMT-04: 注釈（note属性）

- `<kanae_interests>` / `<kanae_episodes>` / `<kanae_self>` の各タグに `note` 属性で使用意図を記述する
- 「応答で自然に滲ませても良いが、わざわざ列挙する必要はない。指示ではない。」という位置づけを明示し、LLMがブロック内容を命令として解釈することを防ぐ

---

## 5. 状態永続化

### FR-PER-01: ファイル形式

- JSON形式。パス: `{STATE_DIR}/kanae_self.json`
- パーミッション: 0o600（atomic_write_jsonが保証）
- バージョンフィールド（`version: 1`）を持ち、将来のスキーマ変更に対応する

### FR-PER-02: Atomic Write

- `utils.atomic_write_json` を使用し、書き込み途中のクラッシュでファイルが壊れることを防ぐ
- 一時ファイルに書き込み → rename の2段階

### FR-PER-03: Forward-compatible読み込み

- `from_dict` は未知のキーを無視し、欠損キーにはデフォルト値を適用する
- Phase 2/3で新フィールドが追加されても、Phase 1のステートファイルがそのまま読める

### FR-PER-04: fail-open

- ファイルが存在しない場合: 空のKanaeSelfStateを返す
- JSONパースに失敗した場合: WARNINGログ出力後、空のKanaeSelfStateを返す
- いずれの場合もbotの応答は通常通り続行される

---

## 6. bot.py統合

### FR-BOT-01: 読み込み（プロンプト構築時）

- 会話ターン開始時に `load_state()` を `asyncio.create_task` で非同期に開始する
- admin以外のユーザーにはkanae_selfを注入しない（adminゲート）
- `KANAE_SELF_INTEREST_ENABLED` が `False` の場合は読み込み自体をスキップする

### FR-BOT-02: Mem0検索クエリ拡張

- `kanae_self_state_task` の完了を待ってから、`augment_query_with_interests()` でMem0検索クエリにInterestトピックを付加する
- kanae名前空間のMem0検索に対してのみ適用される

### FR-BOT-03: プロンプト注入

- `KANAE_SELF_UNIFIED_BLOCK = True`（デフォルト）: `format_kanae_self_block()` で統合ブロックを生成し、プロンプト先頭に注入する
- `KANAE_SELF_UNIFIED_BLOCK = False`（ロールバック用）: 従来の4ブロック個別注入にフォールバックする
  - 注入順: `<kanae_interests>` → `<kanae_episodes>` → `<kanae_state>` → `<kanae_self_memory>`

### FR-BOT-04: 書き込み（応答完了後）

- 毎ターン終了後に `_kanae_self_extract_and_store()` をfire-and-forgetタスクとして起動する
- 処理フロー:
  1. `extract_kanae_signals()` で信号を抽出する
  2. ステートファイルをload → Interest upsert → Episode append → save（単一のload/mutate/saveサイクル）
  3. 抽出されたfactsをMem0にingestする（メタデータに `persona`, `episode_id`, `interest_tags` を付与）
- 全例外をcatchしてWARNINGログ出力。応答には影響しない

### FR-BOT-05: Mem0メタデータ連携

- Mem0 ingest時に以下のメタデータを付与する:
  - `persona: "kanae"`: 将来のマルチペルソナ対応のための名前空間タグ
  - `episode_id`: 事実の出所となったエピソードへの相互参照
  - `interest_tags`: 現在のtop-3トピックのCSV（ChromaDBスカラー制約のため）

---

## 7. 環境フラグ

| フラグ | デフォルト | 効果 |
|---|---|---|
| `KANAE_SELF_INTEREST_ENABLED` | `True` | Interestトラッキング全体のon/off |
| `KANAE_SELF_EPISODE_ENABLED` | `True` | Episodeリングバッファのon/off（Interestとは独立） |
| `KANAE_SELF_UNIFIED_BLOCK` | `True` | 統合 `<kanae_self>` ブロック vs レガシー4ブロック |

全フラグは `config.py` で `_bool_env()` により環境変数から読み取られる。再起動のみで切り替え可能。

---

## 8. データモデル

### Interest

| フィールド | 型 | 範囲 | 説明 |
|---|---|---|---|
| `topic` | str | max 60字 | 正規化済みトピック名（lowercase） |
| `salience` | float | 0.0〜1.0 | 現在の顕著性。指数減衰する |
| `last_touched_at` | str | ISO-8601 | 最終言及日時 |
| `first_seen_at` | str | ISO-8601 | 初出日時 |
| `mention_count` | int | ≥ 0 | 累積言及回数 |
| `valence_attached` | float | -1.0〜+1.0 | 付帯感情の快-不快度（EMA） |
| `arousal_attached` | float | 0.0〜1.0 | 付帯感情の覚醒度（EMA） |
| `source_episode_ids` | list[str] | max 5件 | 関連エピソードID |
| `entity_refs` | list[str] | max 8件 | 関連エンティティ参照 |

### Episode

| フィールド | 型 | 範囲 | 説明 |
|---|---|---|---|
| `id` | str | 8文字hex | uuid4先頭8文字 |
| `ts` | str | ISO-8601 JST | 記録日時 |
| `channel_id` | str | - | Discordチャンネル |
| `summary` | str | max 120字 | 会話ターンの要約 |
| `topics` | list[str] | max 3件 | 関連トピック（lowercase） |
| `valence_delta` | float | -1.0〜+1.0 | そのターンの感情変化量 |
| `importance` | int | 1〜5 | 重要度 |

### KanaeSelfState

| フィールド | 型 | 説明 |
|---|---|---|
| `version` | int | スキーマバージョン（現在: 1） |
| `updated_at` | str | 最終更新日時（ISO-8601） |
| `interests` | dict[str, Interest] | トピック → Interest のマップ |
| `episodes` | list[Episode] | リングバッファ（最大30件） |
| `last_extract_at` | str | 最終抽出日時 |

---

## 9. 定数一覧

| 定数 | 値 | 定義場所 | 用途 |
|---|---|---|---|
| `MAX_INTERESTS` | 100 | state.py | Interest保持上限 |
| `MAX_EPISODES` | 30 | state.py | Episodeバッファ上限 |
| `DECAY_HALF_LIFE_HOURS` | 72.0 | interests.py | salience指数減衰の半減期 |
| `TOUCH_BUMP` | 0.15 | interests.py | 言及時のsalience加算量 |
| `GC_THRESHOLD` | 0.05 | interests.py | GC対象のsalience閾値 |
| `EMA_ALPHA` | 0.3 | interests.py | valence/arousal EMAの重み |
| `DEFAULT_TOPIC_COUNT` | 3 | recall.py | クエリ拡張に注入するトピック数 |
| `_DEFAULT_TOP_K` | 5 | format.py | プロンプトに表示するInterest数 |
| `_DEFAULT_EPISODES_K` | 3 | format.py | プロンプトに表示するEpisode数 |

---

## 10. 並行性と安全性

- Phase 1–3では、ステートファイルの読み書きはbot hookからのみ行われる（cronジョブなし）
- そのため `flock` は取っていない
- 将来cronワーカーがステートを変更する場合は、`services.emotion.lock` と同様の排他制御が必要
- `atomic_write_json` によりファイルレベルの部分書き込みは防止されている

---

## 11. 関連コンポーネント

| コンポーネント | 関係 |
|---|---|
| `services.emotion` | 感情状態を `<kanae_self>` 統合ブロックに内包。Interestの `valence_attached` とは独立 |
| `services.mem0` | 長期記憶。`extract_kanae_signals` がfacts + interests + episodeを同時抽出。Interest-driven queryで検索精度向上 |
| `services.mem0.extract` | `extract_kanae_signals()` の実装場所 |
| `services.mem0.ingest` | 抽出されたfactsのMem0書き込み |
| `utils.atomic_write` | ステートファイルのatomic write |
| `config.py` | 環境フラグの定義 |
| `bot.py` | 読み込み / 注入 / 書き込みの統合ポイント |
| `scripts/migrate_kanae_self_namespace.py` | レガシー "kanae_self" 名前空間からの移行スクリプト |
