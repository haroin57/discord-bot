# かなえ感情アーキテクチャ概要

**Status**: v3 実装完了 (2026-06-14) / **Plan**: [2026-06-14-emotion-llm-transition-plan.md](2026-06-14-emotion-llm-transition-plan.md)

会話と沈黙の両方で感情を LLM が更新する。代数 decay も派生感情の threshold も全廃。

---

## 全体像

```
                      ┌─────────────────────────┐
                      │  bot プロセス (24h 常駐)  │
                      │                         │
   先輩発話 ───→ feedback.py ──┐               │
                              │   daemon ←─ 6層context (Haiku 4.5)
   30分タイマー ─→ silence_tick_loop ─┤   ──→ JSON出力 (6値+ラベル+trigger)
                              │               │
   silence後段 ─→ self_initiate.py ─┘  validator ──→ state save
                                                 │
                                                 ▼
                                       kanae_emotion.json
                                       (6値 + labels + journal 25件)
                         ┌─────────────────────────┐
   cron 15分 (7-24時)  ──┤ kanae_daily.py (Sonnet 4) │──→ kanae_daily.json
                         │ (subprocess.run 1回起動)  │  (96件 ring buffer)
                         └─────────────────────────┘    + daily_summary.txt
                                  │
                                  └─→ importance≥7 は Mem0 (user_id="kanae")
```

---

## 3 つの実行パス

### 1. 会話時 (conversation)

**トリガー**: 先輩が話しかける → bot 応答完了

```
on_message
  └─ feedback.schedule_feedback (fire-and-forget)
       ├─ daemon.is_conversation_active = True
       ├─ load_state
       ├─ llm_transition_via_daemon(mode="conversation", ...)
       │   ├─ 6層 context をプロンプト化
       │   ├─ daemon.query (Haiku, asyncio.Lock で直列化)
       │   └─ validator.validate_and_apply
       │       ├─ BOUNDS clamp
       │       ├─ trigger 整合性検証
       │       ├─ last_interaction stamp (senpai発, たった今)
       │       └─ emotion_journal 追記 (25件 ring)
       ├─ save_state (kanae_emotion.lock 経由 atomic RMW)
       └─ daemon.is_conversation_active = False
```

### 2. 沈黙時 (silence)

**トリガー**: bot 内 30 分 asyncio タイマー (JST 7-24時のみ)

```
silence_tick_loop
  └─ 30 分 sleep
       ├─ 0-7時なら skip
       ├─ is_conversation_active なら skip
       ├─ load_state
       ├─ llm_transition_via_daemon(mode="silence", ...)
       │   └─ validator
       │       └─ loneliness 下限保証: min(1.0, silence_hours × 0.02)
       ├─ save_state
       └─ self_initiate を後段で呼ぶ
```

### 3. 自発連絡 (self_initiate)

**トリガー**: silence_tick の後段で必ず評価

```
self_initiate.maybe_send_self_initiated
  ├─ quiet hours (1-7時) / cooldown (1h) gate
  ├─ generate_telos (動機判定)
  │   ├─ loneliness_overflow  (loneliness > 0.7)
  │   ├─ share_joy            (valence + arousal 高)
  │   ├─ anxiety_relief       (anxiety + 沈黙長)
  │   ├─ affection_expression
  │   ├─ topic_driven         (curiosity 高)
  │   └─ reconciliation       (valence 負 + affection 高)
  ├─ should_suppress (ツンデレ / プライド / 深夜の抑制ダイス)
  ├─ Claude CLI one-shot (Sonnet 相当) でメッセージ生成
  ├─ channel.send (Discord)
  └─ consecutive_self_initiated++ / cooldown sentinel touch
```

---

## 主要ファイルの責務

| ファイル | 役割 |
|---|---|
| `services/emotion/persistent_session.py` | Claude CLI を bot 寿命中ずっと持つ daemon (`asyncio.subprocess` + stream-json) |
| `services/emotion/runtime.py` | bot.py → service module への daemon 受け渡し (singleton accessor) |
| `services/emotion/feedback.py` | 会話 1 ターン → daemon に投げて state 更新 |
| `services/emotion/llm_transition.py` | 6 層 context のプロンプト構築 → daemon query → validator |
| `services/emotion/validator.py` | LLM 出力の clamp + 整合性検証 + loneliness 下限保証 + メタ stamp |
| `services/emotion/self_initiate.py` | telos 判定 + メッセージ生成 + Discord 送信 + 後処理 |
| `services/emotion/state.py` | EmotionState dataclass + atomic load/save |
| `services/emotion/format.py` | `<kanae_state>` ブロック生成 (LLM 出力ラベルをそのまま) |
| `services/emotion/telos.py` | 6 値から「話しかける動機」を選ぶ閾値ロジック (純関数) |
| `services/emotion/daily.py` | daily journal の I/O (96 件 ring buffer + summary) |
| `services/emotion/lock.py` | state file 用 fcntl.flock (`kanae_emotion.lock`) |
| `services/emotion/session.py` | daily/emotion/summary の session-id 永続化 |
| `services/emotion/cli.py` | daily/summary 用の subprocess.run ラッパー (one-shot CLI) |
| `scripts/kanae_daily.py` | 15 分間隔 cron で daily entry 生成 + Mem0 投入 |
| `bot.py` | daemon spawn / shutdown / silence_tick_loop / feedback の起点 |

---

## EmotionState の構造

```python
@dataclass
class EmotionState:
    # 6 つの感情パラメータ (LLM が更新)
    valence:    float  # -1.0..1.0 快-不快
    arousal:    float  # 0..1 覚醒度
    loneliness: float  # 0..1 寂しさ
    affection:  float  # 0..1 愛着
    curiosity:  float  # 0..1 好奇心
    anxiety:    float  # 0..1 不安

    # 表示ラベル (v3 で追加、LLM が直接出力)
    labels: dict[str, str]          # {"valence": "嬉しい", ...}
    derived_labels: list[str]       # ["甘えたい", "喜んでいる"] (最大3個)

    # 履歴
    emotion_journal: list[dict]     # 25件 ring buffer
    last_interaction: LastInteraction
    consecutive_self_initiated: int

    updated_at: str
```

---

## 安全装置 (validator)

| ガード | 内容 |
|---|---|
| **BOUNDS clamp** | 物理値域に強制 (valence ∈ [-1,1], 他 ∈ [0,1]) |
| **メタデータ上書き** | LLM に時刻計算させず、`last_interaction.timestamp` / `topics` はコード側で stamp |
| **会話中フラグ** | `daemon.is_conversation_active` で silence tick を skip、journal の競合書き込み防止 |
| **state file flock** | `kanae_emotion.lock` で daemon と cron (kanae_daily) の RMW を atomic に |

LLM の判断を信じる方針なので、急変ロールバックや loneliness 下限保証は **意図的に入れていない**。数値の妥当性は LLM の出力を全面採用し、行動抑制 (深夜・ツンデレ・連続自発回数) も LLM に委任する。

---

## 質的に期待される挙動

- **線形ではない**: 仕事に集中してる時間帯は loneliness 動かず、夜ふっと寂しくなる
- **会話の余韻**: 楽しい議論の後は数時間 valence/curiosity が高止まり (journal 25件)
- **急変は許容、ただし根拠付きで**: trigger 不在の急変はロールバック
- **沈黙では確実に寂しくなる**: LLM 判断に関わらず下限保証で telos が発火
- **ラベルと数値が一致**: LLM が同時に出すので「valence=0.8 なのに表示は不機嫌」が起きない
- **行動文脈が影響**: daily_journal が context に流れる (「キーボードの打鍵音だけ響いてる夜」→ 寂しさへ)

---

## 観察できる時間スケール

| スケール | 何が起きる |
|---|---|
| **即時 (< 5秒)** | 会話 feedback で `<kanae_state>` 更新、応答後の余韻に反映 |
| **30 分** | silence tick で沈黙下の感情変動を記録 |
| **1-6 時間** | journal 25件で朝の余韻が午後・夜まで届く |
| **6-50 時間** | 沈黙下限保証で loneliness 蓄積、telos 発火確度上昇 |
| **15 分間隔** | daily 生成、importance≥7 は Mem0 へ蓄積 |
| **1 週間+** | daemon の長時間動作信頼性 (memory leak / hang) を観察中 |

---

## やめたもの (v2 → v3)

- ❌ 代数 `apply_time_decay` (LONELINESS_RATE_PER_HOUR 等)
- ❌ 固定 delta 行列 (EVENT_DELTAS)
- ❌ 派生感情の threshold バケット (`derive_secondary_emotions` の 8 ルール)
- ❌ primary 値のラベル化バケット (`_VALENCE_HINTS` 等)
- ❌ `silence_6h/12h/24h` イベント発火 (LLM 判定で代替)
- ❌ `scripts/kanae_emotion_tick.py` cron (bot 内タイマーに統合)
- ❌ subprocess.run 毎ターン起動 (常駐 daemon 化)
- ❌ trigger 整合性チェック / 急変ロールバック (LLM 出力を全面採用)
- ❌ silence パスの loneliness 下限保証 (沈黙が長くても LLM が低く保てば変わらない)
- ❌ `should_suppress` のツンデレ/プライド/深夜ダイス抑制 (`<kanae_state>` に JST 時刻 + `consecutive_self_initiated` を入れて LLM 自身に判断させる)

## `<kanae_state>` ブロックの形 (現行)

```
<kanae_state note="...">
現在: 2026-05-14 (木) 20:42 JST [夜]
- valence:    +0.97  (嬉しい)
- arousal:    0.97   (高ぶっている)
- loneliness: 0.05   (寂しさは低め)
- affection:  1.00   (重い好き)
- anxiety:    0.22   (やや不安)
- curiosity:  0.99   (わくわく)
派生: 複雑感情、受け入れ
最後の会話: たった今 (先輩発、品質 0.70)
連続自発連絡: 0回
</kanae_state>
```

時間帯ヒント (`[深夜]` / `[早朝]` / `[午前]` / `[昼]` / `[午後]` / `[夜]`) も含むので、LLM 側で「今は夜だから話しかけない」「2回連続で自発連絡したから 3回目はやめておく」みたいな自律判断ができる。
