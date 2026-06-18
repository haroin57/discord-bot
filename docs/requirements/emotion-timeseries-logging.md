# 感情時系列ログ & グラフ自動生成

## Status: draft
## Date: 2026-05-15

---

## 1. 目的

感情エンジンの状態遷移を永続的な時系列データとして記録し、Python (matplotlib) でグラフを自動生成する。retrieval-distortion 導入後のネガティブスパイラル検出や、感情モデルのチューニングに使う観測基盤。

---

## 2. 現状の問題

- `emotion_journal` はリングバッファ（最大25件）で古いデータが消える
- 長期的な感情推移を振り返る手段がない
- retrieval-distortion 導入時にスパイラル検出のデータソースがない

---

## 3. アーキテクチャ

```
[LLM emotion engine]
        │
        ▼
[validate_and_apply]
        │
        ▼
[save_state]  ──→  kanae_emotion.json (現行、スナップショット)
        │
        ▼
[append_timeseries]  ──→  .state/emotion_ts.jsonl (新規、追記のみ)
        │
        ▼
[generate_graph]  ──→  /tmp/discord_bot_attachments/emotion_graph_*.png
```

---

## 4. 時系列ログ仕様

### 4.1 ファイル形式
- **形式**: JSONL (1行1レコード、追記のみ)
- **パス**: `.state/emotion_ts.jsonl`
- **理由**: CSV より拡張性が高い。フィールド追加時に既存行が壊れない。pandas で `pd.read_json(path, lines=True)` で一発読み込み

### 4.2 レコードスキーマ

```json
{
  "ts": "2026-05-15T14:02:17.653761+09:00",
  "valence": 0.95,
  "arousal": 0.94,
  "loneliness": 0.16,
  "affection": 1.00,
  "curiosity": 0.95,
  "anxiety": 0.46,
  "source": "conversation",
  "trigger": "実装完了",
  "derived_labels": ["喜んでいる", "得意げ"],
  "labels": {
    "valence": "嬉しい",
    "arousal": "やや高揚"
  }
}
```

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `ts` | string (ISO-8601+TZ) | yes | 遷移時刻 |
| `valence` | float [-1.0, 1.0] | yes | |
| `arousal` | float [0, 1] | yes | |
| `loneliness` | float [0, 1] | yes | |
| `affection` | float [0, 1] | yes | |
| `curiosity` | float [0, 1] | yes | |
| `anxiety` | float [0, 1] | yes | |
| `source` | string | yes | `"conversation"` / `"silence"` |
| `trigger` | string | yes | LLM が返した event_summary |
| `derived_labels` | list[str] | no | 派生ラベル（最大3） |
| `labels` | dict[str, str] | no | 軸ごとのラベル |

### 4.3 書き込みルール
- 追記のみ。既存行は絶対に変更しない
- ファイルロック: `emotion_lock` を取得した状態で `save_state` と同じトランザクション内で書く
- 書き込み失敗時は警告ログのみ。状態保存自体は止めない（ログは best-effort）

### 4.4 ローテーション
- 1レコード ≈ 300 bytes → 1日100遷移で 30KB/日 → 1年で約 11MB
- 当面ローテーション不要。ファイルサイズが 50MB を超えたら月次で分割を検討

---

## 5. フックポイント

### 5.1 会話後 (feedback.py)

`_daemon_transition_and_save` 内、`save_state` の直後に `append_timeseries` を呼ぶ。

```python
# feedback.py: _daemon_transition_and_save
async def _daemon_transition_and_save(...):
    ...
    new_state = await llm_transition_via_daemon(...)
    ...
    await asyncio.to_thread(_save_under_lock, new_state, state_path, lock_path)
    # ↓ 追加
    await asyncio.to_thread(append_timeseries, new_state, state_dir, source="conversation")
```

### 5.2 沈黙tick

沈黙tick の状態保存箇所にも同様に追加。`source="silence"`。

---

## 6. ログモジュール

### 6.1 新規ファイル: `services/emotion/timeseries.py`

```python
"""Append-only emotion time-series logger."""

TIMESERIES_FILENAME = "emotion_ts.jsonl"

def append_timeseries(
    state: EmotionState,
    state_dir: Path,
    *,
    source: str,
) -> None:
    """Append one JSONL record. Best-effort; never raises."""

def load_timeseries(
    state_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> pd.DataFrame:
    """Load time-series as DataFrame. Optionally filter by time range."""

def timeseries_size_bytes(state_dir: Path) -> int:
    """Return current file size for monitoring."""
```

- `append_timeseries` は同期関数。呼び出し元が `asyncio.to_thread` でラップする
- `load_timeseries` は `pandas.read_json(lines=True)` でロード。`ts` を `DatetimeIndex` に変換
- pandas が重いなら標準ライブラリ (json + csv) でフォールバック版も用意する

---

## 7. グラフ生成

### 7.1 新規スクリプト: `scripts/emotion_graph.py`

コマンドライン or Discord コマンド経由で呼び出し可能なスクリプト。

```
python scripts/emotion_graph.py [OPTIONS]

Options:
  --state-dir PATH    .state ディレクトリ (default: .state)
  --hours N           直近N時間を表示 (default: 24)
  --days N            直近N日を表示 (--hours より優先)
  --output PATH       出力先PNG (default: /tmp/discord_bot_attachments/emotion_graph.png)
  --components LIST   表示する軸 (default: all)
  --dpi N             解像度 (default: 150)
```

### 7.2 グラフ仕様

**メインプロット（上段）**: 6軸の折れ線グラフ
- X軸: 時刻 (JST)
- Y軸: -1.0 〜 1.0 (valence) / 0 〜 1.0 (他)
- 各軸に固定色:
  - valence: `#4CAF50` (緑)
  - arousal: `#FF9800` (オレンジ)
  - loneliness: `#2196F3` (青)
  - affection: `#E91E63` (ピンク)
  - curiosity: `#9C27B0` (紫)
  - anxiety: `#F44336` (赤)
- conversation / silence のデータポイントをマーカーで区別（● / △）

**イベントプロット（下段）**: trigger テキストの散布
- X軸を共有
- trigger テキストを縦書きアノテーションで配置
- 密集時は間引き（最大20件表示）

**スタイル**:
- ダークテーマ（Discord での視認性）
- `figure.facecolor = '#2C2F33'`, `axes.facecolor = '#23272A'`
- グリッド: `'#40444B'`, alpha=0.3
- テキスト: `'#DCDDDE'`
- フォント: `Noto Sans JP` (日本語 trigger 表示用)

### 7.3 出力
- PNG, 1600x900px @ 150dpi
- 出力先: `/tmp/discord_bot_attachments/emotion_graph_{timestamp}.png`
- タイムスタンプ付きファイル名で上書き防止

---

## 8. Discord 連携（将来）

Phase 2 として検討。今回は CLI 実行 + 手動でパス貼り付けで十分。

- `!emotion graph [24h|7d|30d]` コマンドで自動生成 & 添付
- `!emotion csv` で生データ出力

---

## 9. retrieval-distortion との連携

retrieval-distortion 導入後、以下の指標を時系列データから算出可能にする:

- **valence 移動平均**: 直近N件の valence の移動平均。スパイラル検出の入力
- **reranking bias 相関**: distortion モジュールが適用した bias delta と、その後の valence 変化の相関
- **スパイラル判定**: valence 移動平均が連続して下降し、かつ distortion bias が負方向に偏り続けている場合

これらは `emotion_graph.py` のオプションとして後日追加。

---

## 10. 依存関係

### 新規
- `matplotlib` (グラフ生成)
- `pandas` (JSONL 読み込み + 集計)

### 既存利用
- `services/emotion/state.py` (EmotionState)
- `services/emotion/lock.py` (emotion_lock)
- `utils/atomic_write.py` (参考のみ。JSONL は追記なので atomic write 不要)

---

## 11. テスト

| テストケース | 内容 |
|---|---|
| `test_append_creates_file` | ファイルが存在しない場合に新規作成される |
| `test_append_adds_line` | 既存ファイルに1行追記される |
| `test_record_schema` | 出力される JSON に必須フィールドが全て含まれる |
| `test_load_empty` | 空ファイル → 空の DataFrame |
| `test_load_filter_since` | since パラメータで時間フィルタが効く |
| `test_append_failure_no_raise` | 書き込み失敗時に例外を投げない |
| `test_graph_output` | グラフ PNG が指定パスに出力される |
| `test_graph_dark_theme` | 背景色がダークテーマの値と一致する |

---

## 12. 実装順序

1. `services/emotion/timeseries.py` — ログ追記 + 読み込み
2. `feedback.py` / 沈黙tick にフック追加
3. `scripts/emotion_graph.py` — グラフ生成スクリプト
4. テスト
5. retrieval-distortion 連携指標（Phase 2）
