# tool_loop.py 段階的分割計画

## 目的

`services/claude_api/tool_loop.py` を、現在の振る舞いと import 経路を維持したまま責務別モジュールへ分割する。

- 現在の行数: 2,718 行。
- `run_agent_loop`: 1,184 行、33 引数、87 分岐、39 await。
- 変更前ベースライン: `tests/test_claude_api_tool_loop.py` は 124 件成功。
- 変更前の既定全テスト: 2,893 件成功、21 件スキップ、既存警告 6 件。
- `run_agent_loop` の引数、状態遷移、履歴更新順、callback 順序は変更しない。
- 目標は `tool_loop.py` を約 1,500〜1,700 行へ縮小することとする。

## 保護対象

作業開始時点で未コミット変更が存在する。特に以下はユーザーの進行中変更として保持する。

- `services/claude_api/tool_loop.py` の fallback continuation 修正。
- `tests/test_claude_api_tool_loop.py` の fallback 回帰テスト。
- `services/session.py` と `tests/test_session.py` の lossless replay 修正。
- その他の既存追跡済み変更と未追跡ファイル。

`.mcp.json` の内容は参照せず、今回の変更対象にも含めない。

## 互換性契約

以下を変更不能な契約として扱う。

1. `services.claude_api.tool_loop` から実利用されているシンボルと、request shape が従来 module global として公開していた依存名を再 export する。
2. `services.claude_api.__init__`、`entry.py`、`side_question.py`、`verifier.py`、deep research 呼び出し元の import 経路を維持する。
3. `run_agent_loop` のシグネチャと `AgentLoopResult` のフィールドを維持する。
4. API request の model、max_tokens、system、messages、betas、metadata、thinking、output_config、extra_body、headers を維持する。
5. assistant/tool result の block 順序、署名、fallback boundary、tool-use/tool-result ID を維持する。
6. history、progress、StallWatchdog、RequestBudget、ToolLoopDetector、evidence の更新順を維持する。
7. callback の呼び出し順と例外の fail-open/fail-closed 方針を維持する。
8. `tool_loop` モジュール上の設定値を monkeypatch する既存テストを維持する。
9. ログメッセージと logger 名 `services.claude_api.tool_loop` を維持する。

抽出した private helper 同士の任意の再 binding は互換対象としない。例えば codec 内部の `_block_to_dict` を差し替える場合、抽出後の所有者である `message_codec._block_to_dict` を patch する。既存テストが利用する設定値 patch 点は `tool_loop` wrapper から明示伝播する。

## 今回の抽出境界

### 1. `loop_contracts.py`

以下を移動し、`tool_loop.py` から同一オブジェクトを再 export する。

- `AgentLoopResult`
- `_model_display`
- `_fallback_alert`
- `_CHECKPOINT_NOTE`
- `_AUTO_RESUME_NOTE`
- `AUTO_RESUME_DIRECTIVE`
- `_build_checkpoint_result`

### 2. `message_codec.py`

以下の純粋な履歴・block 変換を移動する。

- `_block_to_dict`
- `_response_blocks_to_dict`
- `_serialize_search_result_content`
- `_join_final_text`
- `_EMPTY_CONTENT_PLACEHOLDER`
- `_sanitize_content_blocks`
- `_TOOL_SEARCH_BLOCK_TYPES`
- `_SERVER_TOOL_RESULT_TYPES`
- `strip_tool_search_blocks`
- `_has_unpaired_server_tool_use`
- `_normalize_history_for_api`

未コミットの fallback continuation 実装は、意味変更せず現在の working tree から移動する。`services/session.py` の session 固有 self-heal とは統合しない。

### 3. `request_shape.py`

以下を移動する。

- `REFUSAL_FALLBACK_HEADER`
- `_build_cached_system`
- `_mark_cache_breakpoint`
- `_inject_cache_breakpoints`
- `_build_stream_kwargs_impl`

`SERVER_SIDE_FALLBACK_ENABLED` の monkeypatch 互換性を維持するため、`tool_loop.py` に `_build_stream_kwargs` wrapper を残し、現在値を明示引数として実装へ渡す。package facade に存在する同名の legacy helper は統合しない。

### 4. `tool_display.py`

以下の表示専用処理を移動する。

- `_TOOL_DETAIL_INLINE_CAP`
- `_TOOL_DETAIL_FIELD_CAP`
- `_cap_inline`
- `_format_tool_invocation_for_display`

現在はテスト中心の private API だが、削除せず再 export する。

### 5. `tool_execution.py`

以下を移動する。

- `_truncate_head_tail`
- `_cap_mcp_result_content`
- `_validate_tool_input`
- `_run_one_tool_inner` の実装本体。
- `_RescuedToolUse`

`_run_one_tool`、`_run_one_tool_inner` の互換 wrapper、`_attempt_text_invoke_rescue` は `tool_loop.py` に残す。これにより `DELIB_DISTILL_ENABLED`、`MCP_TOOL_RESULT_CHARS`、`TOOL_CALL_TIMEOUT_S` の既存 monkeypatch 到達性、evidence/distill の更新順、rescue 経路を維持する。

### 6. `loop_observability.py`

以下を移動する。

- hallucination 検出用定数群。
- `_detect_write_hallucination`
- `_USAGE_FIELDS`
- `_usage_number`
- `_last_context_tokens`
- `get_last_context_tokens`
- `_context_tokens`
- `_log_turn_usage`
- `_log_total_usage`
- `_observe_response_headers`

mutable state は新モジュール側だけが所有し、`tool_loop.py` 側に複製しない。refusal request capture は既存の private counter と一時ファイル副作用を保つため、今回は `tool_loop.py` に残す。

## 今回移動しない領域

- `run_agent_loop` の状態機械。
- 単一ターンの stream drain。
- stall retry と non-streaming fallback。
- text invoke rescue の履歴手術。
- safe/unsafe tool batch の編成。
- proactive compaction。
- MCP sticky promotion。
- `kill_orphan_claude_cli`。

単一ターン stream 抽出は、partial text、thinking、Scratchpad、fallback 検出、TTFT、usage、stall recovery の共有状態を DTO 化する必要がある。純粋移動ではなく制御フロー変更になるため、今回の範囲から除外する。

## 実装順序と検証ゲート

### Phase 0: ベースライン固定

- 対象テスト 124 件と既定全テスト 2,893 件の成功を記録する。
- `git status --short`、`git diff --check`、対象行数を記録する。
- frozen smoke eval を 1 回実行し、環境上完走可能な場合だけ前後比較ゲートに採用する。

### Phase 1: 契約と request shape

- `loop_contracts.py` と `request_shape.py` を追加する。
- `tool_loop.py` に再 export と設定値伝播 wrapper を置く。
- import identity、`run_agent_loop` シグネチャ、checkpoint、cache、fallback request shape のテストを実行する。

ロールバック条件は import cycle、同名 facade helper の挙動変化、request kwargs 差分である。

### Phase 2: message codec

- `message_codec.py` を追加し、現在の working tree 実装を移動する。
- fallback block の順序、response-only field 除去、thinking signature、server tool pairing、tool-search strip の既存テストを実行する。

ロールバック条件は block 列、辞書内容、履歴正規化結果の差分である。

### Phase 3: 表示、実行、観測

- `tool_display.py`、`tool_execution.py`、`loop_observability.py` を追加する。
- `tool_loop.py` から再 export する。
- tool validation、permission gate、timeout、result cap、usage、hallucination、callback の既存テストを実行する。

ロールバック条件は monkeypatch 不達、tool 実行回数・順序差、usage state 差、logger 名差である。

### Phase 4: 統合検証

- 関連 suite を実行する。
- 既定全テストを実行する。
- `ruff`、構文 compile、`git diff --check`、import smoke を実行する。
- `tool_loop.py` の行数とシンボル構成を確認する。
- frozen smoke eval が変更前に完走した場合、同じ条件で変更後評価を実行する。
- 独立した子エージェントに差分、循環依存、互換 shim、テスト結果をレビューさせる。

## テストコマンド

```bash
.venv/bin/pytest -q tests/test_claude_api_tool_loop.py
.venv/bin/pytest -q \
  tests/test_claude_api_tool_loop.py \
  tests/test_claude_api.py \
  tests/test_claude_api_entry.py \
  tests/test_claude_api_stall.py \
  tests/test_claude_api_loop_guard.py \
  tests/test_claude_api_compaction.py \
  tests/test_session.py \
  tests/test_checkpoint_auto_resume.py \
  tests/test_btw_command.py \
  tests/test_delib_pipeline.py \
  tests/test_agent_tool.py
.venv/bin/pytest -q
.venv/bin/ruff check services/claude_api tests/test_claude_api_tool_loop.py
```

## 完了条件

- 対象テストと既定全テストが変更前と同数以上で成功する。
- 新規の skipped、xfail、warning を追加しない。
- `run_agent_loop` のシグネチャと状態機械を変更しない。
- 既存 import と private re-export が維持される。
- `tool_loop.py` が約 1,500〜1,700 行になる。
- ユーザーの既存差分と対象外ファイルを変更しない。
- `git diff --check` と限定 lint が成功する。

## 別作業として扱う既知事項

調査中に以下を確認したが、純粋な構造リファクタリングに混ぜない。

- fallback 境界より前の client tool-use が未加工 `final_msg.content` から実行対象へ再抽出される可能性。
- 破損した fallback endpoint が辞書以外の場合の sanitizer 例外。
- module docstring と現在の structured JSONL 保存仕様の不一致。
- incremental callback 使用時の result text に関する docstring と実装の不一致。
- refusal capture の `/tmp` 書き込みと reset 方針。

これらは許容差分を個別に定義した修正として扱う。
