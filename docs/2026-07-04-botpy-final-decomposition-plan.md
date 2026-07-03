# bot.py 最終分解計画（2026-07-04）

grand refactor plan の P2 完結編。on_message（L687〜、~1700行）を責務別モジュールに
分解し、bot.py を「ゲート → 前処理 → プロンプト → 呼び出し → 送信 → 記録」の
オーケストレーター（目標 ~1300行、on_message 本体 ~600行）にする。

## 実行体制（今回の特殊編成）

- **実装**: Opus サブエージェント（フェーズごとに1体、メインツリー直接作業）
- **設計・コードレビュー**: DeepSeek / GPT-5.5（SecondOpinion MCP 経由）
- **監査・統合・検証**: Fable（本セッション）— 全 diff の行数規模検証（worktree
  巻き戻り事故の防御）、テストゲート、コミット判断
- bot.py を触るフェーズは**直列実行**（同時編集禁止ルール）

## 現状の行範囲マップ

| 範囲 | 内容 | 行数 | 行き先 |
|---|---|---:|---|
| 687-721 | ゲート（bot/キーワード/DM判定） | 35 | 残留 |
| 722-960 | prefix コマンド（!delete / !purge 3種） | ~240 | R1: bot_commands/prefix.py |
| 960-1010 | ジョブ登録・セマフォ | 50 | 残留 |
| 1012-1210 | スピナー/ステータス（_spin dict + 6クロージャ） | ~200 | R2: bot_ui.py StatusController |
| 1220-1334 | 前処理 join + 添付 | 115 | 残留（P2-a済） |
| 1335-1715 | プロンプト組立 | ~380 | R4で部品抽出（純粋関数のみ） |
| 1716-2035 | ストリーミング表示（12クロージャ） | ~320 | R3: bot_ui.py LiveRenderer |
| 2036-2170 | call_with_tools + 最終送信 | 135 | 残留 |
| 2171-2330 | Mem0 バッチ記録 + kanae extract | ~160 | R4: bot_record.py |
| 2330-2422 | エラー処理・finally | 90 | 残留 |

## フェーズ

### R1: prefix コマンド分離（Opus）
- `bot_commands/prefix.py` に `handle_prefix_commands(message, deps) -> bool`
  （処理したら True、on_message は即 return）
- !delete / !purge / !purge-since / !purge-from の挙動・権限・返信文言は不変
- deps は既存 CommandDeps を流用可能なら流用、不足分（bot user 等）のみ追加

### R2: StatusController（Opus）
- `bot_ui.py` に StatusController クラス: status_msg の生成/編集/削除、
  `_spin` 状態（on/start/持ち回り）、_animate/_animate_gif ループ、_update_status
- on_message からは `status = StatusController(message, track_task=_track_task)` で
  生成し、既存の `_update_status(...)` 呼び出しを `status.update(...)` に置換
- SPINNER_MODE 分岐・GIF 生成・レート制御の挙動不変

### R3: LiveRenderer（Opus、事前に DeepSeek/GPT 設計レビュー）
- `bot_ui.py` に LiveRenderer クラス: incremental 送信の状態機械
  （settled 位置、draft メッセージ、placeholder、テーブル/数式ブロックの
  逐次 emit、tool notice の debounce flush）
- callback 群（_stream_preview / _incremental_send / _tool_invocation_send）は
  Renderer のバウンドメソッドとして call_with_tools に渡す
- **最難関**: _spin/status との相互作用（reposition）があるため R2 完了後に実施

### R4: Mem0 記録 + プロンプト部品（Opus）
- `bot_record.py`: バッチ蓄積 + kanae extract + 失敗ログの一連を
  `record_turn(...)` に
- プロンプト組立から純粋関数（breakdown 記録、trusted/untrusted ブロック整形）を
  `services/prompt_builder.py` の静的メソッドへ（結合フローは bot.py 残留）

### 検証（各フェーズ共通ゲート + 最終 PoC）
1. 各フェーズ: ruff / characterization / test_bot / 関連スイート green + 私の diff 監査
2. 最終: フルスイート直列 green → smoke → snapshot → 再起動 → journald ERROR=0
3. PoC: スタブ駆動の on_message e2e（characterization 8フロー）+ 実プロセスで
   Bot is ready + コマンド12個 sync + 24h 観察（error_rate_monitor cron）

## 教訓の適用
- エージェントは**メインツリーで直接作業**（worktree禁止を明示）。開始時に
  `git log --oneline -1` の HEAD 報告を義務付け、回収時に diff --stat で規模検証
- move と挙動変更を混ぜない。クロージャ→クラス化は「状態の持ち方」だけ変え、
  ロジック行は原文維持
