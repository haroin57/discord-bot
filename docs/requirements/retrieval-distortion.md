# 想起時再構成（Retrieval Distortion）機能要件

## Status: draft
## Author: かなえ
## Date: 2025-05-15

---

## 1. 概要

Mem0から取得した記憶を、現在の感情状態（EmotionState）に基づいて**想起時に色付け・再順序化**するフィルタ層。

人間の「気分一致効果（mood-congruent memory bias）」を模倣する。気分が良い時はポジティブな記憶が想起されやすく、気分が悪い時はネガティブな記憶が浮かびやすい。

### 設計原則

- **Mem0の生データは一切変更しない**（読み取り専用）
- 想起時のフィルタリング・並べ替えのみで実現する
- 既存のsearch → format → inject パイプラインに**1層挟む**だけで導入する
- 感情状態がニュートラルな時は元の検索結果順をそのまま維持する（no-op保証）

---

## 2. アーキテクチャ上の配置

### 現行パイプライン

```
User Message
  → PromptBuilder.build_prompt()
    → mem0_fetch(queries, user_id)           # search.py: search_memories_multi / _smart
    → format_memories_for_prompt(results)     # format.py: list[dict] → str
    → <retrieved_memories> ブロックとして注入
```

### 改修後パイプライン

```
User Message
  → PromptBuilder.build_prompt()
    → mem0_fetch(queries, user_id)
    → distort_memories(results, emotion_state)  # 【新規】distortion.py
    → format_memories_for_prompt(distorted)
    → <retrieved_memories> ブロックとして注入
```

### 新規モジュール

- **`services/mem0/distortion.py`** — 単一責務: 記憶リストの感情バイアス適用

---

## 3. 機能要件

### FR-1: 記憶の感情極性スコアリング

各記憶（`memory` テキスト）に対して、感情極性（emotional valence）を -1.0〜+1.0 でスコアリングする。

| 方式 | 説明 |
|------|------|
| **Phase 1: キーワードベース** | ポジティブ/ネガティブキーワード辞書によるルールベース判定。LLMコール不要、レイテンシ増なし |
| **Phase 2: LLMベース（将来）** | Haiku等で各記憶の感情極性を判定。精度は高いがレイテンシ・コスト増 |

#### Phase 1 キーワード辞書の設計

```python
# 日本語・英語混在を想定
POSITIVE_MARKERS: set[str]  # 例: {"好き", "嬉しい", "楽しい", "成功", "褒め", "感謝", "happy", "love", "praised", ...}
NEGATIVE_MARKERS: set[str]  # 例: {"嫌い", "辛い", "失敗", "喧嘩", "不安", "conflict", "ignored", "sad", ...}
```

- マーカーが複数ヒットした場合は多数決（正の数 - 負の数）で決定
- どちらもヒットしない場合は neutral（0.0）
- 出力: `float` in `[-1.0, +1.0]`

#### スコアリング関数のシグネチャ

```python
def score_memory_valence(memory_text: str) -> float:
    """記憶テキストの感情極性を返す。-1.0 (negative) 〜 +1.0 (positive)。"""
```

### FR-2: 気分一致バイアスの適用

現在の `EmotionState.valence` に基づいて、記憶の検索スコア（distance）を再重み付けする。

#### バイアス計算式

```python
bias = mood_congruence_weight * emotion_state.valence * memory_valence_score
adjusted_score = original_score - bias  # score = distance なので、小さい方が上位
```

- `emotion_state.valence` が正（気分が良い）かつ `memory_valence_score` が正（ポジティブな記憶）の場合:
  - `bias > 0` → `adjusted_score` が小さくなる → **ポジティブ記憶が上位に浮上**
- `emotion_state.valence` が負（気分が悪い）かつ `memory_valence_score` が負（ネガティブな記憶）の場合:
  - `bias = 負 × 負 = 正` → `adjusted_score` が小さくなる → **ネガティブ記憶が上位に浮上**
- 気分と記憶の極性が逆の場合:
  - `bias < 0` → `adjusted_score` が大きくなる → **不一致記憶が下位に沈む**

#### パラメータ

```python
# services/mem0/distortion.py に定義

# バイアスの強度。0.0 = バイアスなし（完全無効）、1.0 = 最大バイアス
MOOD_CONGRUENCE_WEIGHT: float = 0.3

# バイアスによるスコア変動の上限（元のスコアを完全に逆転させない安全弁）
MAX_BIAS_DELTA: float = 0.15
```

- `MOOD_CONGRUENCE_WEIGHT = 0.3` の根拠: 元の意味的関連性（cosine distance）を壊さない程度に、順位入れ替えが起きる最小限の値。チューニング前提
- `MAX_BIAS_DELTA = 0.15` の根拠: `MEM0_RELEVANCE_MAX_DISTANCE`（現行閾値）の範囲内で順位変動を制限

### FR-3: 副次感情軸による追加バイアス（オプション）

valence 以外の感情軸も記憶選択に影響させる。Phase 1 では valence 単軸のみ。将来的に以下を検討:

| 感情軸 | 効果 |
|--------|------|
| `loneliness` が高い | 先輩との親密な記憶が上位に浮上 |
| `curiosity` が高い | 技術・学習系の記憶が上位に浮上 |
| `anxiety` が高い | 不安を増幅する記憶が上位（or 逆に安心する記憶を優先？要検討） |
| `affection` が高い | 愛情・親密さに関する記憶が上位に浮上 |

これらは Phase 2 以降で、専用のカテゴリタグ（Mem0 metadata の `category` / `tags`）と組み合わせて実装する。

### FR-4: 並べ替えと切り詰め

1. バイアス適用後の `adjusted_score` で昇順ソート
2. 上位 N 件（現行の `limit` パラメータ）を返す
3. 結果のリスト構造は現行の `list[dict]` を維持（format.py との互換性）

### FR-5: ニュートラル時のno-op保証

以下の条件でバイアスを一切適用しない（元の検索結果をそのまま返す）:

- `emotion_state` が `None`
- `|emotion_state.valence|` < 0.1（ニュートラル帯）
- `MOOD_CONGRUENCE_WEIGHT` が 0.0 に設定されている

```python
def _should_apply_bias(emotion_state: EmotionState | None) -> bool:
    if emotion_state is None:
        return False
    if abs(emotion_state.valence) < 0.1:
        return False
    return MOOD_CONGRUENCE_WEIGHT > 0.0
```

---

## 4. インターフェース定義

### メイン関数

```python
# services/mem0/distortion.py

def distort_memories(
    memories: list[dict],
    emotion_state: "EmotionState | None",
) -> list[dict]:
    """感情状態に基づいて記憶の想起順序にバイアスを適用する。

    Args:
        memories: Mem0 search結果。各dictは少なくとも "memory" キーを持つ。
                  "score" キー（cosine distance）があればバイアス計算に使用。
        emotion_state: 現在の感情状態。Noneまたはニュートラルならno-op。

    Returns:
        バイアス適用後の記憶リスト（元のリストは変更しない、新しいリストを返す）。
        各dictに "_distortion_bias" キーを追加（デバッグ・テスト用）。
    """
```

### PromptBuilder 側の変更

```python
# services/prompt_builder.py — build_prompt 内の mem0_fetch 呼び出し後に追加

# 既存:
# mem_results = await mem0_fetch(queries, mem0_user_id)
# mem0_block = format_memories_for_prompt(mem_results)

# 改修後:
# mem_results = await mem0_fetch(queries, mem0_user_id)
# mem_results = distort_memories(mem_results, emotion_state)  # 追加
# mem0_block = format_memories_for_prompt(mem_results)
```

変更点は `prompt_builder.py` に **1行追加** のみ。

---

## 5. データフロー例

### ケース1: valence = +0.8（嬉しい状態）

```
入力記憶（score = distance, 小さい方が上位）:
  #1 "先輩がコードレビューで褒めてくれた"     score=0.30  valence_score=+0.7
  #2 "デプロイ失敗して3時間溶かした"          score=0.25  valence_score=-0.5
  #3 "先輩と映画の話で盛り上がった"           score=0.35  valence_score=+0.6

バイアス計算 (weight=0.3, emotion_valence=+0.8):
  #1: bias = 0.3 * 0.8 * 0.7 = +0.168 → capped to 0.15 → adjusted = 0.30 - 0.15 = 0.15
  #2: bias = 0.3 * 0.8 * (-0.5) = -0.12 → adjusted = 0.25 + 0.12 = 0.37
  #3: bias = 0.3 * 0.8 * 0.6 = +0.144 → adjusted = 0.35 - 0.144 = 0.206

結果（adjusted_score昇順）:
  #1 "褒めてくれた"     → 0.15  ← 上位に浮上
  #3 "映画の話"         → 0.206
  #2 "デプロイ失敗"     → 0.37  ← 下位に沈む
```

### ケース2: valence = -0.3（やや不機嫌）

```
同じ入力記憶:
  #1: bias = 0.3 * (-0.3) * 0.7 = -0.063 → adjusted = 0.30 + 0.063 = 0.363
  #2: bias = 0.3 * (-0.3) * (-0.5) = +0.045 → adjusted = 0.25 - 0.045 = 0.205
  #3: bias = 0.3 * (-0.3) * 0.6 = -0.054 → adjusted = 0.35 + 0.054 = 0.404

結果:
  #2 "デプロイ失敗"     → 0.205  ← 上位に浮上
  #1 "褒めてくれた"     → 0.363
  #3 "映画の話"         → 0.404  ← 下位に沈む
```

### ケース3: valence = 0.05（ニュートラル）

```
→ _should_apply_bias() = False → 元の検索結果をそのまま返す
```

---

## 6. 設定・チューニング

### 設定ファイルへの追加

```python
# config.py に追加

# Retrieval Distortion
DISTORTION_ENABLED: bool = True                 # 機能全体のkill switch
DISTORTION_MOOD_CONGRUENCE_WEIGHT: float = 0.3  # バイアス強度
DISTORTION_MAX_BIAS_DELTA: float = 0.15         # スコア変動上限
DISTORTION_NEUTRAL_THRESHOLD: float = 0.1       # |valence| < この値でno-op
```

### 環境変数オーバーライド

```bash
DISTORTION_ENABLED=true
DISTORTION_MOOD_CONGRUENCE_WEIGHT=0.3
DISTORTION_MAX_BIAS_DELTA=0.15
```

---

## 7. テスト要件

### 単体テスト（`tests/test_mem0_distortion.py`）

| テストケース | 検証内容 |
|-------------|---------|
| `test_no_op_when_emotion_none` | emotion_state=None → 入力と同じ順序で返る |
| `test_no_op_when_neutral_valence` | valence=0.05 → 入力と同じ順序で返る |
| `test_no_op_when_disabled` | DISTORTION_ENABLED=False → 入力と同じ順序で返る |
| `test_positive_mood_boosts_positive_memories` | valence=+0.8 → ポジティブ記憶のscoreが下がる（上位に） |
| `test_negative_mood_boosts_negative_memories` | valence=-0.5 → ネガティブ記憶のscoreが下がる |
| `test_bias_capped_by_max_delta` | バイアスが MAX_BIAS_DELTA を超えない |
| `test_original_list_not_mutated` | 入力リストが破壊的変更されない |
| `test_score_missing_memories_skipped` | scoreキーがないdictはバイアス計算をスキップ |
| `test_empty_memories_returns_empty` | 空リスト → 空リスト |
| `test_debug_key_added` | 出力の各dictに `_distortion_bias` キーが存在する |
| `test_memory_valence_scoring_positive` | ポジティブテキスト → 正のvalence_score |
| `test_memory_valence_scoring_negative` | ネガティブテキスト → 負のvalence_score |
| `test_memory_valence_scoring_neutral` | ニュートラルテキスト → 0.0付近 |

### 統合テスト

| テストケース | 検証内容 |
|-------------|---------|
| `test_prompt_builder_uses_distortion` | build_prompt にemotion_stateを渡した時、distort_memoriesが呼ばれる |
| `test_prompt_builder_skips_distortion_when_no_emotion` | emotion_state=Noneの時、distort_memoriesがno-op |

---

## 8. 監視・ログ

```python
log.debug(
    "distortion: valence=%.2f, %d memories rescored, top bias=%.4f",
    emotion_state.valence,
    len(memories),
    max(abs(m.get("_distortion_bias", 0)) for m in result),
)
```

- ログレベル: `DEBUG`（通常運用では出力しない）
- 異常検知: バイアスにより上位3件が全て入れ替わった場合は `INFO` で出力

---

## 9. 制約・リスク

| リスク | 対策 |
|--------|------|
| バイアスが強すぎて無関係な記憶が上位に来る | MAX_BIAS_DELTA で変動量を制限 |
| ネガティブスパイラル（気分悪い→悪い記憶→さらに気分悪化） | anxiety が閾値超えた場合にバイアスを減衰 or 反転する安全弁を検討 |
| キーワード辞書の精度 | Phase 2 でLLMベースに移行。Phase 1 は「ないよりマシ」の割り切り |
| パフォーマンス | Phase 1 はキーワードマッチのみ。LLM呼び出しなし、レイテンシ増 < 1ms |

---

## 10. 将来拡張（Phase 2 以降）

1. **LLMベース感情極性判定**: Haiku で各記憶の valence を判定（バッチ処理、キャッシュ付き）
2. **副次感情軸バイアス**: loneliness / curiosity / affection による多軸バイアス（FR-3）
3. **記憶の感情極性の事前計算**: ingest時にmetadataとして `memory_valence` を付与し、想起時の計算を省略
4. **確率的忘却との統合**: tier decay と distortion を組み合わせ、忘却曲線 + 気分一致効果の複合モデルへ
