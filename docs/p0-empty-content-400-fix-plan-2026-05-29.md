# P0 修正プラン: 400 invalid_request（空 content の user message）

- **日付**: 2026-05-29
- **ステータス**: approved（実装待ち）
- **発見元**: ログ監査ワークフロー（再起動 12:37 JST 以降の21分窓）

## Context
マルチターン会話の agent loop が空 content の user message を Anthropic Messages API に送り、
`400 invalid_request_error 'user messages must have non-empty content'` を起こしている。
21分窓で 7会話中2会話が最終応答を返せず、ユーザーには誤った「画像が大きすぎる可能性があります」
エラーが表示された。**Opus 4.8 移行とは無関係の既存ガード漏れ**で、effort=max +
interleaved-thinking 構成で「思考だけして tool を呼ばないターン」が出ることで露出した。

## 根本原因（コードで確認済み）
- `tool_loop.py:660` で `stop_reason != "tool_use"` は早期 return。740行に到達するのは
  `stop_reason == "tool_use"` のときのみ。
- `tool_loop.py:673-675` は tool_use ブロックだけ拾うため、「思考だけで tool_use ブロックが
  0個のターン（pause turn）」では `tool_results=[]` のまま。
- `tool_loop.py:740` `history.append({"role":"user","content":tool_results})` で空 content が混入。
- `_normalize_history_for_api`（`tool_loop.py:381-386`）は検証なしで素通し → `[]` が API へ。
- `entry.py:472-476` は invalid_request を non-transient と判定し即 return（リトライなし）。
- **resume 経路でも再発する**: 過去に空 append された履歴が JSONL に保存され、
  `load_api_replay_channel`（`session.py:386`）→ `_tail_jsonl`（`session.py:364`）で
  検証なしに再生される。`messages.156`（prior_turns=150）のケースはこれが濃厚。

## 設計

### 修正1【必須・最終防衛ライン】空 content sanitizer
`tool_loop.py:381` の `_normalize_history_for_api` を拡張。content が空（空文字列 `""` または
空リスト `[]`）の message を**ドロップせず**（tool_use/tool_result ペアリングを壊さない）、
最小プレースホルダ（例: `[{"type":"text","text":"(続けてください)"}]`）に置換する。
- in-loop と resume の両経路が必ずここを通るため、これで 400 は構造的に不可能になる。
- 既に JSONL に保存済みの空メッセージ（resume 経路）への唯一の防御。

### 修正2【loop の明示的ガード】思考だけ pause turn を「継続 + 連続上限」で処理
`tool_loop.py:740` の append 前に `tool_results` が空かを判定し、空なら（= pause turn）:
- 連続カウンタ `consecutive_empty` をインクリメント（ループ先頭で 0 初期化）。
- 上限 `MAX_CONSECUTIVE_EMPTY_TURNS`（=2）未満なら、空 content の代わりに継続 user message
  `[{"type":"text","text":"続けてください。"}]` を append して次ターンへ（`continue`）。
  → モデルに思考の続きをさせ、応答を取りに行く。
- 上限到達なら、既存の早期 return パターン（`tool_loop.py:660-670`）で `text_across_turns`
  を返して打ち切り（無限思考ループ防止）。
- `tool_results` が非空なら従来通り append し、`consecutive_empty` を 0 にリセット。

### 修正3【P1・関連】errors.py の誤文言分離
`errors.py:100` の invalid_request 文言「⚠️ リクエストにエラーがありました。画像が大きすぎる
可能性があります。」を、image 起因と一般 invalid_request に分離。誤誘導を解消。

## 検証
- `tests/test_claude_api_tool_loop.py` の `TestRunAgentLoop` に追加:
  1. 思考だけターン（tool_use 0個）で空 content を送らず、継続 user message を入れて次ターンへ進むこと
  2. 思考だけターンが連続上限に達したら打ち切って蓄積テキストを返すこと
  3. `_normalize_history_for_api` が空 content（`""`/`[]`）をプレースホルダ化すること（resume 経路含む）
- 既存テスト（`test_tool_use_then_end` / `test_max_turns_exhausted` 等）が壊れないこと。
- `pytest tests/test_claude_api_tool_loop.py tests/test_claude_api_entry.py` 緑 → 修正後 bot 再起動
  → ログで 400 invalid_request が再発しないことを確認。

## 触らないもの
- emotion / 404 修正（完了済み）。stall 閾値（P2、別途・要先輩確認）。
