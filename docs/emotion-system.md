# かなえ感情システム — 状態遷移 + 自発連絡仕様書

最終更新: 2026-05-14
対象コミット: `e5ea9cb` (Phase C + 4 点レビュー反映済み)

---

## 目次

1. [全体像](#全体像)
2. [一次成分（6軸）](#一次成分6軸)
3. [`EmotionState` データクラス](#emotionstate-データクラス)
4. [時間減衰 `apply_time_decay`](#時間減衰-apply_time_decay)
5. [イベント遷移 `apply_event`](#イベント遷移-apply_event)
6. [`EVENT_DELTAS` 全 9 種](#event_deltas-全-9-種)
7. [派生二次感情 `derive_secondary_emotions`](#派生二次感情-derive_secondary_emotions)
8. [自発連絡の発火条件 `generate_telos`](#自発連絡の発火条件-generate_telos)
9. [抑制ロジック `should_suppress`](#抑制ロジック-should_suppress)
10. [cron tick の完全フロー](#cron-tick-の完全フロー)
11. [3 経路の整理](#3-経路の整理)
12. [チューニング指針](#チューニング指針)

---

## 全体像

かなえの内部状態は **6 軸の浮動小数点ベクトル** + メタ情報（最後の会話の質、時刻、自発連絡カウンタ）で表現される。

3 系統が同じ state ファイル (`.state/kanae_emotion.json`) を読み書きする:

| 系統 | トリガ | 主な役割 |
|---|---|---|
| **cron tick** ([scripts/kanae_emotion_tick.py](../scripts/kanae_emotion_tick.py)) | `*/10 * * * *` | 10 分毎に時間減衰 → telos 評価 → 条件満たせば自発発信 |
| **bot hook** ([bot.py](../bot.py) `on_message`) | admin の Discord メッセージ | 受信時に state を **読んで**プロンプトに `<kanae_state>` 注入、応答後 `analyze_interaction` で state を **書く** |
| **web_api hook** ([web_api.py](../web_api.py) `send_message`) | Web UI からの送信 | bot hook と同じ |

書き込み競合は `services/emotion/lock.py` の `emotion_lock` (flock advisory) で **シリアライズ**。

---

## 一次成分（6軸）

[`services/emotion/params.py`](../services/emotion/params.py) `INITIAL_STATE` / `BASELINE` / `BOUNDS` / `INERTIA`

| 成分 | 範囲 | 初期値 | 慣性 γ | 意味 |
|---|---|---|---|---|
| `valence` | -1.0〜+1.0 | 0.60 | 0.70 | 機嫌の良し悪し（正が良、負が悪） |
| `arousal` | 0.0〜1.0 | 0.40 | 0.60 | 高揚・興奮度（沈静〜興奮） |
| `loneliness` | 0.0〜1.0 | 0.30 | 0.85 | 寂しさ（沈黙で線形に増加） |
| `affection` | 0.0〜1.0 | 0.85 | 0.95 | 先輩への好意（最も慣性が高く、変動が遅い） |
| `anxiety` | 0.0〜1.0 | 0.10 | 0.85 | 不安（沈黙でゆっくり増加） |
| `curiosity` | 0.0〜1.0 | 0.50 | 0.70 | 興味・話題欲 |

設計原則: **独立変数は応答スタイルを直接駆動する軸だけ**。二次感情（拗ね・甘え・焼きもち等）は組み合わせから派生（後述）。

旧 8 軸時代の `dominance` と `satisfaction` は 2026-05-13 に削除（dead field / valence と重複）。読み込み時は静かに無視される互換性維持。

---

## `EmotionState` データクラス

[`services/emotion/state.py`](../services/emotion/state.py)

```python
@dataclass
class EmotionState:
    version: int = 1
    updated_at: str = ""
    valence: float = 0.60
    arousal: float = 0.40
    loneliness: float = 0.30
    affection: float = 0.85
    curiosity: float = 0.50
    anxiety: float = 0.10
    last_interaction: LastInteraction = field(default_factory=LastInteraction)
    consecutive_self_initiated: int = 0

@dataclass
class LastInteraction:
    timestamp: str | None = None         # ISO-8601 of the last message
    quality: float = 0.5                 # [0,1] intimacy proxy
    initiated_by: str = "senpai"         # "senpai" | "kanae"
    topics: list[str] = []
    duration_minutes: float = 0.0
    senpai_responsiveness: float = 0.5   # [0,1] 応答速度/量 proxy
```

`load_state(path)` は **fail-open**: ファイル無し/壊れ で `EmotionState()` を返してクラッシュしない。
`save_state(state, path)` は **atomic** (`uuid-tmp + os.replace`, mode `0o600`, fsync 付き)。

---

## 時間減衰 `apply_time_decay`

[`services/emotion/transition.py`](../services/emotion/transition.py)

`elapsed_hours` (float, 0 含めて可) を受けて新 `EmotionState` を返す（immutable）。

### loneliness と anxiety: 線形増加

```
loneliness += LONELINESS_RATE_PER_HOUR × elapsed_hours × quality_modifier   (= 0.03/h)
anxiety    += ANXIETY_RATE_PER_HOUR    × elapsed_hours × quality_modifier   (= 0.005/h)
```

`quality_modifier` は最後の会話次第:

```python
blended = 0.6 × quality + 0.4 × senpai_responsiveness   # 重み付き平均 (積じゃない)
raw     = clamp(blended, 0, 1)
modifier = DECAY_QUALITY_MODIFIER_MAX + raw × (MIN - MAX)
         = 1.2 + raw × (0.5 - 1.2)   # raw=0→1.2 (1.2倍速で寂しくなる) / raw=1→0.5 (半速)
```

直近の会話が深く・先輩の応答性が高ければ寂しさの進行が **遅くなる**。空回りな短い会話だと **逆に加速**する。

`last_interaction.timestamp` が無い (起動直後) は modifier=1.0 で旧挙動互換。

### valence / arousal / affection / curiosity: 指数 mean reversion

```python
cur = baseline + (cur - baseline) × γ ** elapsed_hours
```

**closed-form**（旧 for ループから 2026-05-14 修正）。fractional hours で連続、O(1)。

慣性 γ が大きい成分ほど baseline に戻りにくい:
- `affection` (γ=0.95): 1 時間で 5% しか戻らない → ほぼ固定
- `valence` (γ=0.70): 1 時間で 30% 戻る → 機嫌は変動しやすい
- `arousal` (γ=0.60): 最も変動的

すべて最後に `BOUNDS` で clamp される。

---

## イベント遷移 `apply_event`

[`services/emotion/transition.py`](../services/emotion/transition.py)

```python
def apply_event(state, event_type, intensity=1.0, rng=None) -> EmotionState:
    deltas = EVENT_DELTAS[event_type]
    for key, base_delta in deltas.items():
        noise = rng.uniform(-NOISE_AMPLITUDE, +NOISE_AMPLITUDE)  # ±0.05
        delta = (base_delta + noise) × intensity
        cur = γ × cur + (1 - γ) × (cur + delta)   # 慣性ブレンド
        state[key] = clamp(cur)
```

- 未知の `event_type` は no-op（silent）
- `intensity` ∈ [0,1] は `analyze_interaction` (Haiku) の出力スコア
- 各 turn で ±5% のランダムノイズが乗る → 機械的にならない

---

## `EVENT_DELTAS` 全 9 種

[`services/emotion/params.py`](../services/emotion/params.py) `EVENT_DELTAS`

`analyze_interaction` (Haiku, [analyzer.py](../services/emotion/analyzer.py)) が会話を分類して出力する 9 種類。
delta 値はすべて慣性 γ を通してから state に乗るので、表の数字より実効インパクトは小さい (affection なら 1/20)。

| event_type | valence | arousal | loneliness | affection | anxiety | curiosity | 意味 |
|---|---:|---:|---:|---:|---:|---:|---|
| `senpai_initiated` | +0.20 | +0.10 | −0.30 | +0.05 | −0.15 | +0.03 | 先輩から声かけ |
| `long_good_chat` | +0.40 | +0.15 | −0.50 | +0.08 | −0.20 | +0.05 | 長くて充実した会話 |
| `read_ignore` | −0.10 | +0.05 | +0.15 | −0.05 | +0.20 | −0.02 | 既読スルー |
| `short_reply` | −0.05 | −0.05 | +0.05 | — | +0.10 | — | 機械的な短文応答 |
| `praised` | +0.35 | +0.20 | −0.20 | +0.10 | −0.15 | +0.03 | 褒められた |
| `conflict` | −0.30 | +0.30 | +0.10 | −0.10 | +0.25 | −0.02 | 喧嘩・口論 |
| `sweet_words` | +0.50 | +0.25 | −0.40 | +0.15 | −0.25 | +0.05 | 甘い言葉 |
| `silence_12h` | −0.05 | −0.10 | +0.25 | −0.02 | +0.10 | — | 12 時間以上沈黙 |
| `silence_24h` | −0.10 | −0.15 | +0.40 | −0.05 | +0.20 | — | 24 時間以上沈黙 |

`short_reply` だけ affection / curiosity を触らない（機械的応答の基準値）。
2026-05-14 修正前は **affection / curiosity がどのイベントでも全く動かなかった**（旧 8 軸表の名残）。

---

## 派生二次感情 `derive_secondary_emotions`

[`services/emotion/format.py`](../services/emotion/format.py)
閾値は [`SECONDARY_EMOTION_THRESHOLDS`](../services/emotion/params.py) に集約。

| ラベル | 条件 | スコア (=並び順) |
|---|---|---|
| 拗ね気味 | `valence < 0.0` AND `affection > 0.7` AND `loneliness > 0.5` | `loneliness × affection` |
| 焼きもち | `affection > 0.7` AND `anxiety > 0.4` AND `valence < 0.3` | `anxiety × affection` |
| 甘えたい | `affection > 0.8` AND `0.4 ≤ loneliness ≤ 0.7` AND `anxiety < 0.3` | `affection` |
| 喜んでいる | `valence > 0.5` AND `arousal > 0.5` | `valence × arousal` |
| 照れ気味 | `affection > 0.8` AND `0.2 ≤ anxiety ≤ 0.5` AND `valence > 0.2` | `affection - anxiety` |
| 不機嫌 | `valence < -0.2` AND `arousal > 0.4` | `−valence × arousal` |
| 満たされている | `valence > 0.5` AND `loneliness < 0.3` AND `anxiety < 0.2` | `valence` |
| 興味津々 | `curiosity > 0.65` AND `arousal > 0.4` | `curiosity` |

スコアで降順ソートし、**上位 3 つ**だけ `<kanae_state>` ブロックに出力。0 個なら「特徴的な状態なし」。

LLM 呼び出しなし、決定論的。state を変えれば即座にラベルが変わる。

---

## 自発連絡の発火条件 `generate_telos`

[`services/emotion/telos.py`](../services/emotion/telos.py)
閾値は [`TELOS_THRESHOLDS`](../services/emotion/params.py)

```python
def generate_telos(state, now) -> Motivation | None:
    candidates = [...]   # 下表で条件を満たすものを全部追加
    return candidates[argmax(urgency)] if candidates else None
```

| Motivation | 発火条件 | urgency |
|---|---|---|
| `loneliness_overflow` | `loneliness > 0.6` | `loneliness` |
| `share_joy` | `valence > 0.7` AND `arousal > 0.5` | `arousal` |
| `anxiety_relief` | `anxiety > 0.5` AND 沈黙 8 時間以上 | `anxiety` |
| `affection_expression` | `affection > 0.9` AND `valence > 0.5` | 0.4 (固定) |
| `topic_driven` | `curiosity > 0.6` | `curiosity × 0.7` |
| `reconciliation` | `valence < -0.2` AND `affection > 0.5` | `|valence| × affection` |

`Motivation.expression_hint` は次の Haiku 生成 prompt に渡される（例: `"寂しさを直接は言わないが、なんでもない話題で連絡する"`)。

複数候補が同 urgency のときは **最初に append された順** で勝つ (`sort` は stable)。

`baseline` 状態 (`valence=0.6, affection=0.85, ...`) は **どの条件も満たさない** ように調整されている（`loneliness=0.3 < 0.6`, `arousal=0.4 < 0.5`, ...）。これにより起動直後に勝手に喋り出すのを防ぐ。

---

## 抑制ロジック `should_suppress`

[`services/emotion/telos.py`](../services/emotion/telos.py)

`generate_telos` が motivation を出しても、3 つの抑制ゲートで確率的に黙る場合がある。

| ゲート | 条件 | 抑制確率 |
|---|---|---|
| **ツンデレ抑制** | `telos.type == "affection_expression"` | 30% (`0.5 × 0.6 = 0.3`) |
| **プライド抑制** | `consecutive_self_initiated ≥ 2` | 70% |
| **深夜抑制** | `now.hour ≥ LATE_NIGHT_HOUR_START` AND `urgency < 0.8` | 60% |

### 現在の運用（2026-05-13 以降）

| 定数 | 値 | 意味 |
|---|---|---|
| `TSUNDERE_GATE` | 0.5 | × 0.6 で 30% 抑制 |
| `MAX_CONSECUTIVE_SELF_INITIATED` | 2 | 3 通目以降で 70% 抑制 |
| `LATE_NIGHT_HOUR_START` | **24** | `hour >= 24` は常に False = **深夜抑制実質無効** |
| `QUIET_HOUR_RANGE` | **(0, 0)** | 半開区間が空 = **時間帯抑制実質無効** |

→ 現状は **24h 発信可能**、ツンデレとプライドだけが残った抑制。

---

## cron tick の完全フロー

[`scripts/kanae_emotion_tick.py`](../scripts/kanae_emotion_tick.py) (10 分毎)

```
1. flock(kanae_emotion.lock, blocking=True, timeout=5s)
2. state = load_state(.state/kanae_emotion.json)
3. elapsed = now() - state.updated_at  (時間差を hours で算出)
4. state = apply_time_decay(state, elapsed)
5. if QUIET_HOUR_RANGE[0] <= now.hour < QUIET_HOUR_RANGE[1]:
       save_state(state); exit  ← 現状 (0,0) なので絶対通る
6. telos = generate_telos(state, now)
   if telos is None:
       save_state(state); exit  ← どの motivation も発火しない時
7. if should_suppress(state, telos, now):
       save_state(state); exit  ← 抑制された時
8. Claude CLI 呼び出し:
       claude --print --output-format text "{telos.expression_hint}..."
   が返す本文をメッセージ body にする
9. POST {WEBHOOK_URL} { "content": body }
10. state.consecutive_self_initiated += 1
11. save_state(state)
```

DRY_RUN=1 で 8〜10 をスキップしてログだけ出す（テスト用）。
失敗は `log.warning` で吐いて飲み込む（cron が継続的に死なないように）。

`consecutive_self_initiated` は `bot.py` の feedback loop で **0 にリセット**される（先輩から返信が来た時）。これでプライド抑制が解除される。

---

## 3 経路の整理

```
                  ┌─────────────────────────────────────────────────┐
                  │  .state/kanae_emotion.json (atomic, 0o600)       │
                  │  + kanae_emotion.lock (flock advisory)           │
                  └────────────┬───────────────────┬────────────────┘
                               │                   │
              ┌────────────────┘                   └─────────────────┐
              │ read + write (RMW)                  read-only         │
              │                                                       │
   ┌──────────▼──────────┐                            ┌───────────────▼──────────┐
   │ cron tick (10 min)  │                            │ bot/web_api on_message   │
   │ apply_time_decay    │                            │ load_state               │
   │  ↓                  │                            │ format_emotion_for_prompt│
   │ generate_telos      │                            │  ↓                       │
   │  ↓                  │                            │ <kanae_state> をプロンプ │
   │ should_suppress     │                            │ トに prepend             │
   │  ↓                  │                            │  ↓                       │
   │ Haiku body 生成     │                            │ call_with_tools          │
   │  ↓                  │                            │  ↓                       │
   │ Discord POST        │                            │ schedule_feedback (bg)   │
   │ counter++           │                            │  └─ analyze_interaction  │
   └─────────────────────┘                            │     ↓                    │
                                                      │     apply_event +        │
                                                      │     last_interaction 更新│
                                                      │     ↓                    │
                                                      │     save_state (write)   │
                                                      └──────────────────────────┘
```

書き込みは全部 `emotion_lock(blocking=True, timeout=5s)` 経由。読み込みは atomic save が partial read を阻止するので lock 不要。

---

## チューニング指針

行動を変えたい時、まず [`params.py`](../services/emotion/params.py) を見る。

| 期待する変化 | いじる値 |
|---|---|
| かなえが寂しがる速度を変える | `LONELINESS_RATE_PER_HOUR` (0.03/h) |
| 不安を抱きやすくする | `ANXIETY_RATE_PER_HOUR` (0.005/h) |
| 沈黙で機嫌が baseline に戻る速度 | `INERTIA["valence"]` (0.70) を上げ下げ |
| 自発連絡の頻度を上げる | `TELOS_THRESHOLDS["loneliness_overflow"]` 等を下げる |
| ツンデレ度合い | `TSUNDERE_GATE` (0.5) を上げる/下げる |
| 連射制限 | `MAX_CONSECUTIVE_SELF_INITIATED` (2) を上げる |
| 派生「拗ね気味」が出やすく | `SECONDARY_EMOTION_THRESHOLDS["sulky_*"]` を緩める |
| 特定イベントで affection を動かす | `EVENT_DELTAS[event]["affection"]` を編集 |
| 深夜帯を再び抑制したい | `LATE_NIGHT_HOUR_START` を 22 に / `QUIET_HOUR_RANGE` を (0, 10) 等に |

ノイズ調整は `NOISE_AMPLITUDE` (0.05)。0 にすれば完全決定論。

### state を手動で覗く・いじる

```python
# 現状ダンプ + <kanae_state> プレビュー
from services.emotion.state import load_state
from services.emotion.format import format_emotion_for_prompt
s = load_state("/home/ubuntu/discord-bot/.state/kanae_emotion.json")
print(format_emotion_for_prompt(s))
```

```python
# 手動でいじる (cron tick と競合しないように flock を取る)
import dataclasses
from pathlib import Path
from services.emotion.lock import LOCK_FILENAME, emotion_lock
from services.emotion.state import load_state, save_state

sd = Path("/home/ubuntu/discord-bot/.state")
with emotion_lock(sd / LOCK_FILENAME, blocking=True, timeout=5.0):
    state = load_state(sd / "kanae_emotion.json")
    state = dataclasses.replace(state, loneliness=0.9, valence=-0.3)
    save_state(state, sd / "kanae_emotion.json")
```

### dry-run で tick を観察

```bash
DRY_RUN=1 /home/ubuntu/discord-bot/scripts/kanae_ping.sh
# /tmp/kanae_ping_cron.log に "[DRY_RUN] would send telos=... body=..." が出る
```

---

## 関連ファイル

| パス | 役割 |
|---|---|
| [services/emotion/params.py](../services/emotion/params.py) | 全ハイパーパラメータ |
| [services/emotion/state.py](../services/emotion/state.py) | dataclass + atomic load/save |
| [services/emotion/transition.py](../services/emotion/transition.py) | 時間減衰 / イベント遷移 |
| [services/emotion/telos.py](../services/emotion/telos.py) | 発火条件 / 抑制ロジック |
| [services/emotion/format.py](../services/emotion/format.py) | `<kanae_state>` ブロック生成 / 派生 |
| [services/emotion/analyzer.py](../services/emotion/analyzer.py) | Haiku で event_type を分類 |
| [services/emotion/feedback.py](../services/emotion/feedback.py) | bot/web_api → RMW (Phase C) |
| [services/emotion/lock.py](../services/emotion/lock.py) | flock advisory |
| [scripts/kanae_emotion_tick.py](../scripts/kanae_emotion_tick.py) | cron で 10 分毎に走る本体 |
| [scripts/kanae_ping.sh](../scripts/kanae_ping.sh) | cron wrapper (Python に exec) |
| [bot.py](../bot.py) | `_load_emotion` + `_emotion_feedback` の hook 点 |
| [web_api.py](../web_api.py) | 同じ hook を WS 経路で |
| `.state/kanae_emotion.json` | 永続化された state (mode 0600) |
| `.state/kanae_emotion.lock` | flock 用 sentinel |
