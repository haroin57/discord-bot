# /home/ubuntu/discord-bot/services/emotion/ 機能要件書

## Context

haroin が Discord bot「かなえ」の emotion サブシステムの機能要件を把握したい。
実運用されている Discord bot のキャラクター感情シミュレーション基盤。

---

## 1. 感情状態モデル（EmotionState）

### 1.1 6軸感情ベクトル
| 軸 | 範囲 | 初期値 | 慣性 | 説明 |
|---|---|---|---|---|
| valence | -1.0〜+1.0 | 0.6 | 0.7 | 機嫌。正=ポジティブ、負=ネガティブ |
| arousal | 0.0〜1.0 | 0.4 | 0.6 | 覚醒度。高=興奮、低=落ち着き |
| loneliness | 0.0〜1.0 | 0.3 | 0.85 | 寂しさ。沈黙で+0.03/h自然増加 |
| affection | 0.0〜1.0 | 0.85 | 0.95 | 好意。最も変化が遅い（数日単位） |
| curiosity | 0.0〜1.0 | 0.5 | 0.7 | 知的関心 |
| anxiety | 0.0〜1.0 | 0.1 | 0.85 | 不安。沈黙で+0.005/h自然増加 |

### 1.2 付帯データ
- **LastInteraction**: 最終会話のメタ情報（timestamp, quality, initiated_by, topics, duration, senpai_responsiveness）
- **emotion_journal**: 最大25件のリングバッファ。各エントリに source（conversation/silence）、タイムスタンプ、6軸値、ラベル
- **derived_labels**: LLM が付与するムードラベル（最大3件）。例: `["拗ね気味", "甘えたい"]`
- **consecutive_self_initiated**: かなえ側から連続で自発連絡した回数（最大2回で抑制）

### 1.3 派生感情（Secondary Emotions）
LLM が直接ラベルを付与する。定義されているパターン:
- 拗ね気味（sulky）: affection高 + valence負 + loneliness高
- 焼きもち（jealous）: affection高 + anxiety高 + valence低
- 甘えたい（spoiled）: affection高 + loneliness中 + anxiety低
- 他: 嬉しい、照れ、不機嫌、満足、好奇心旺盛 等

---

## 2. 感情遷移エンジン

### 2.1 LLM ベース遷移（主経路）
- **ファイル**: `llm_transition.py`
- **モデル**: Claude Haiku（persistent daemon 経由）
- **モード**: `"conversation"`（会話後）/ `"silence"`（沈黙tick）
- **入力**: 現在の6軸 + emotion_journal + daily entries + daily summary + Mem0 + 会話テキスト + 経過時間
- **出力**: 新しい6軸 + derived_labels + labels + trigger + reasoning
- **特徴**: 代数式ではなく LLM が「かなえの性格」を考慮して感情を動かす。予測不能だがキャラ一貫性がある

### 2.2 代数的遷移（フォールバック）
- **ファイル**: `transition.py`
- LLM が使えないときの fallback。指数平均回帰 + イベントデルタ + ノイズ
- 7種のイベントタイプ: senpai_initiated, long_good_chat, short_reply, praised, sweet_words, conflict, read_ignore
- 3種の時間減衰イベント: silence_6h, silence_12h, silence_24h

### 2.3 会話分析器（Analyzer）
- **ファイル**: `analyzer.py`
- 各会話ターン後に Haiku で user↔assistant のやり取りを分類
- 出力: event_type, intensity, topics, intimacy_level, senpai_engagement, conflict_detected

---

## 3. 永続デーモン（Persistent Session）

### 3.1 EmotionSessionDaemon
- **ファイル**: `persistent_session.py`
- bot 起動時に1つ spawn。bot lifetime 中ずっと生存
- Claude CLI を `stream-json` I/O で保持し、multi-turn で文脈を蓄積
- `query(prompt)` → JSON dict を返す
- conversation feedback と silence tick の両方が共有
- `asyncio.Lock` でシリアライズ（同時クエリなし）

### 3.2 クラッシュリカバリ
- 連続3回失敗でセッション ID をローテーション
- バックオフ: 1s → 2s → 5s → 10s でリトライ
- `is_alive` プロパティでヘルスチェック

---

## 4. テロス（Telos）— 自発連絡の動機生成

### 4.1 動機タイプと閾値
| タイプ | トリガー条件 | urgency | expression_hint |
|---|---|---|---|
| loneliness_overflow | loneliness > 0.6 | loneliness値 | 寂しさを直接は言わないが... |
| share_joy | valence > 0.7 AND arousal > 0.5 | arousal値 | 嬉しかった出来事を報告したい |
| anxiety_relief | anxiety > 0.5 AND 8h以上沈黙 | anxiety値 | 先輩が元気か確認したい |
| affection_expression | affection > 0.9 AND valence > 0.5 | 0.4 | 甘えたい、構ってほしい |
| topic_driven | curiosity > 0.6 | curiosity×0.7 | 聞きたいこと・話したいことがある |
| reconciliation | valence < -0.2 AND affection > 0.5 | |valence|×affection | 気まずさを解消したい |

### 4.2 抑制ゲート
- MAX_CONSECUTIVE_SELF_INITIATED: 2（連続2回まで）
- クールダウン: 3600秒（env: KANAE_PING_COOLDOWN）
- 直近活動抑制: 1800秒以内に会話があればスキップ
- ツンデレゲート / 深夜抑制: 無効化済み

---

## 5. 自発メッセージ送信（Self-Initiate）

### 5.1 実行サイクル
- **ファイル**: `self_initiate.py`
- 30分ごとの silence tick で実行
- パイプライン: クールダウン確認 → 直近活動確認 → 状態ロード → テロス生成 → メッセージ生成（one-shot CLI） → Discord 送信 → カウンタ更新

### 5.2 メッセージ生成
- テロスの expression_hint + 直近会話履歴（4h, 最大40行）+ 感情ベクトルをプロンプトに注入
- Claude Haiku の one-shot 呼び出し（daemon とは別セッション）
- 出力: `{"message": "..."}` → Discord channel に送信

### 5.3 送信先
- ハードコード: channel_id = `1490247632847896776`

---

## 6. 会話フィードバック

### 6.1 schedule_feedback
- **ファイル**: `feedback.py`
- 各会話ターン後に `asyncio.create_task` で非同期実行
- daemon.query() で LLM 遷移 → validate_and_apply → save_state
- `emotion_lock`（flock）で RMW 排他制御
- `is_conversation_active` フラグで silence tick との衝突回避

---

## 7. プロンプト注入（Format）

### 7.1 format_emotion_for_prompt
- **ファイル**: `format.py`
- `<kanae_state>` ブロックを生成してシステムプロンプトに注入
- 内容: 現在時刻（5分バケット）、6軸値+日本語ラベル、派生感情、最終会話情報、連続自発回数
- バケット化: `KANAE_STATE_TIME_BUCKET_MIN=5` でプロンプトキャッシュの安定性確保

### 7.2 kanae_self 統合
- `format_kanae_self_block()`（`services/kanae_self/format.py`）が emotion_block を `<kanae_self>` の `<state>` 子要素として統合
- 単独注入と統合注入の両方に対応（Phase 3 統合済み）

---

## 8. 日次ジャーナル（Daily）

### 8.1 DailyEntry リングバッファ
- **ファイル**: `daily.py`
- 最大96件（15分刻みで約24時間分）
- 各エントリ: ts, event_type, importance, activity, detail, mood, thought
- LLM 遷移プロンプトの文脈として注入される

### 8.2 日次サマリ DM
- **ファイル**: `daily_summary_dm.py`
- 1時間ごとに haroin に DM でかなえの1時間の活動サマリを送信
- Claude Haiku one-shot で生成
- センチネルファイルで重複送信防止

---

## 9. 永続化とロック

### 9.1 ファイル一覧
| ファイル | 内容 |
|---|---|
| `.state/kanae_emotion.json` | EmotionState（atomic write, 0o600） |
| `.state/kanae_daily.json` | DailyEntry[]（リングバッファ） |
| `.state/kanae_daily_summary.txt` | 日次サマリ |
| `.state/emotion_sessions.json` | CLI セッション ID |
| `.state/kanae_emotion.lock` | emotion state の flock |
| `.state/emotion_sessions.lock` | session dict の flock |
| `$XDG_STATE_HOME/.../kanae_ping.last_sent` | 自発送信クールダウン |
| `$XDG_STATE_HOME/.../hourly_summary_dm.last_sent` | DM クールダウン |

### 9.2 排他制御
- `emotion_lock()`: flock ベース。bot hook は non-blocking（Discord イベントループをブロックしない）、cron tick は blocking + timeout

---

## 10. bot.py との統合ポイント

| タイミング | 処理 |
|---|---|
| **on_ready** | daemon spawn + silence_tick_loop(30分) + hourly_summary_dm_loop(1h) 開始 |
| **会話ターン前** | `format_emotion_for_prompt(state)` → `<kanae_state>` ブロック注入 |
| **会話ターン後** | `schedule_feedback(user_text, assistant_text)` → fire-and-forget |
| **silence tick (30分)** | `llm_transition_via_daemon(mode="silence")` → `maybe_send_self_initiated()` |
| **shutdown** | daemon.stop() |

---

## 11. 環境変数

| 変数 | デフォルト | 用途 |
|---|---|---|
| EMOTION_ANALYZER_MODEL | claude-haiku-4-5-20251001 | 会話分析モデル |
| KANAE_PING_COOLDOWN | 3600 | 自発送信クールダウン(秒) |
| KANAE_PING_SUPPRESS_RECENT_ACTIVITY_S | 1800 | 直近活動抑制(秒) |
| KANAE_PING_RECENT_HISTORY_HOURS | 4.0 | 自発送信の文脈参照時間幅 |
| KANAE_STATE_TIME_BUCKET_MIN | 5 | プロンプト時刻のバケット幅(分) |
| DRY_RUN | 0 | 1で自発送信を実際には送らない |
