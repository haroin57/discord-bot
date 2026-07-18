# discord-bot 全体リファクタリング実行計画

## 1. 基準状態

2026-07-14 に Herdr 上の Codex 10 プロセスで、ルート実行系、コマンド、utils、
サービス、Claude API、Mem0、Codex/Deep Research/Invest KB、運用、テスト、
`tool_loop.py` 並行作業を分担走査した。

- Python は約 99,177 行である。実装完了時の全pytestは3,545件成功、21件skipである。
- `services/claude_api/tool_loop.py` の並行リファクタリングは完了済みである。
  - 2,718 行から 1,694 行へ縮小した。
  - `run_agent_loop` 本文 1,184 行の SHA-256 は変更前後で一致した。
  - 対象 133 件、関連 421 件、全 2,902 件が成功し、21 件が skip、既存警告は 6 件である。
  - Ruff、構文 compile、consumer import、`git diff --check` が成功した。
- 監査担当はファイルを変更していない。監査開始前の未コミット差分を保持した。

### 1.1 基準識別子と保護manifest

- 基準commitは `7337564dcdb68ec49d7babe33d967b944a1563af` である。
- 既存のユーザー差分をcommitへまとめる権限は本作業に含まれないため、dirty working
  snapshotは `docs/2026-07-14-refactor-protected-manifest.sha256` を正本とする。
- manifestはPhase 0開始前から存在した追跡済み変更、未追跡ファイル、完了済み
  `tool_loop.py`分割を含む。検証コマンドは次のとおりである。

```bash
sha256sum -c docs/2026-07-14-refactor-protected-manifest.sha256
```

Phase 0をcommitする場合は、manifest対象を同一commitへ混在させず、所有者の確認後に
別commitとして固定する。rollbackはPhase 0所有ファイルだけを対象とする。

## 2. 変更禁止範囲と互換契約

Phase 0開始時は次の並行作業済みファイルを変更しない境界とした。後続の統合レビューで
`services/session.py`と`tests/test_session.py`だけを明示的な実装対象へ移し、manifestの
意図的な不一致として管理した。他の保護対象は不変である。

- `services/claude_api/tool_loop.py`
- `services/claude_api/loop_contracts.py`
- `services/claude_api/loop_observability.py`
- `services/claude_api/message_codec.py`
- `services/claude_api/request_shape.py`
- `services/claude_api/tool_display.py`
- `services/claude_api/tool_execution.py`
- `services/session.py`
- `tests/test_claude_api_tool_loop.py`
- `tests/test_tool_loop_refactor_compat.py`
- `tests/test_session.py`

次の契約を後続フェーズでも維持する。

1. `services.claude_api.tool_loop` の stable import、`AgentLoopResult`、
   `run_agent_loop` の完全シグネチャを維持する。
2. `SERVER_SIDE_FALLBACK_ENABLED`、fallback header、tool timeout、MCP result cap、
   distill の facade patch pointを wrapper から明示伝播する。
3. callback は要求通知、validation、permission gate、実行、status の現在順序を維持する。
4. `history` の in-place 更新、fallback boundary、thinking signature、tool-use/result IDを維持する。
5. bot/provider facade の戻り値 `(text, session, resumed)` と、
   `text_stream_callback` の累積スナップショット契約を維持する。

## 3. 優先リスク

### P0: 即時封じ込め対象

1. `services/claude_api/tools.py` の Bash は、外側 timeout または cancel 後も
   子プロセスグループを残し、副作用を継続できる。
2. `utils/math_renderer.py` は未信頼 LaTeX を inline script に埋め込み、
   `</script>` で JavaScript 境界を脱出できる。
3. `kanae_alarm_mcp_server.py` にシェル文字列への入力混入がある。
   ただし未追跡の他者所有ファイルであるため、引き渡しまで変更しない。
4. Mem0 は BM25 から正本の scope、validity、delete 状態を迂回できる。
   consolidation、entity merge、replay cursor にデータ永久欠損経路がある。
5. Oracle MCP の 8015 listener が認証なしで外部到達可能な運用状態である。
   firewall と unit の変更は本計画のコード変更と分けて実施する。

### P1: 次フェーズの境界修正

- free-code registry の availability/feature gate 迂回と `/fork` alias 競合。
- Discord command の初回 defer 不足と `/btw` の provider 非互換。
- Claude Agent の広告 capability と実 dispatcher の不一致。
- permissions 破損時 fail-open、MCP/CLI subprocess への環境変数過剰継承。
- session の二重所有、非順序保存、provider continuity の失効状態欠落。
- OAuth refresh の singleflight 不足、R2 spool の先頭飢餓。
- Deep Research の process-global busy flag、期限なし承認、citation 支持関係未検証。
- Invest KB の非原子的 delete/add と event loop 上の同期 I/O。
- 自動 deploy が dirty worktree を再起動でき、rollback 後も障害版へ戻せる。

## 4. 実行フェーズ

### Phase 0A: 局所安全修正

同一ファイルを複数担当が変更しない条件で、次を独立実装する。

1. Bash cancel cleanup と process-group 回収。
2. LaTeX inline script の JavaScript-safe escape。
3. pytest collection 時の `sys.modules` 汚染を fixture scope へ隔離。
4. `!purge 0` を拒否する境界修正。
5. Invest KB の英語 query gate を単語境界判定へ変更。
6. structured log の予約フィールド上書きを拒否。

各変更は専用回帰テスト、対象 Ruff、`git diff --check` を通す。

Phase 0Aの所有ファイルは次のとおりである。同一行の担当以外は変更しない。

| 担当 | 所有ファイル |
|---|---|
| Bash cleanup | `services/claude_api/tools.py`, `tests/test_claude_api_tools.py` |
| Math escape | `utils/math_renderer.py`, `tests/test_math_renderer.py` |
| Test isolation | `tests/test_mem0_batch_summary.py`, `tests/test_mem0_extract_format.py`, `tests/test_mem0_extract_salvage.py` |
| Purge boundary | `bot_commands/prefix.py`, `tests/test_prefix_commands.py` |
| Invest gate | `services/invest_kb/search.py`, `tests/test_invest_kb_search.py` |
| Log fields | `services/structured_log.py`, `tests/test_structured_log.py` |
| Plan/manifest | `docs/2026-07-14-repository-refactor-execution-plan.md`, `docs/2026-07-14-refactor-protected-manifest.sha256` |

import境界と共有テストの競合は、対象テストだけでなく全collectionと全pytestで検査する。

### Phase 0B: 運用封じ込め

1. Oracle MCP 8015 の外部 listener を閉じ、認証または loopback bind を適用する。
2. `auto_deploy.sh` に dirty-worktree fail-closed gate と `flock` を追加する。
3. rollback tag を deploy 対象として pin し、自動 deploy が上書きしない構造にする。
4. service 別 secret、systemd hardening、`/live` と `/ready` を分離する。

本番 unit、firewall、timer の変更はコードレビュー後に別作業として実施する。

### Phase 1: 契約と所有権の固定

1. `ProviderRequest`、`ProviderResult`、`TurnCallbacks` を型付き契約にする。
2. command の guard、response、state、service Protocol を分離する。
3. free-code に gate 強制付き dispatcher を追加し、`find()` を introspection 専用にする。
4. `SessionRegistry` を単一所有者とし、永続化を `SessionRepository` へ分離する。
5. config import 時の I/O を `RuntimeBootstrap` へ移し、immutable settings を導入する。
6. renderer/browser/background task の所有者に `close()` と cancel/await 契約を持たせる。

### Phase 2: データ整合性

1. Mem0 の正本を一つに固定し、BM25、graph、fact store を projection とする。
2. canonical write と projection 更新予定を同一 transaction の outbox に保存する。
3. delete/archive/reinforce/consolidate/merge/replay を application service 経由に統一する。
4. batch/replay を lease、ack、retry、dead-letter、処理済み cursor 方式へ移行する。
5. Invest KB に原子的 `replace_document()` と event-loop offload を導入する。

### Phase 3: God module 分解

1. `bot.py` を admission、context、prompt、model、publish、record に分割する。
2. Claude API を contracts/policy/transport/execution/workflow/composition root の依存方向へ整理する。
3. CLI/MCP subprocess を共通 runner に集約し、allowlist env、deadline、process-group cleanup を統一する。
4. Deep Research を run state、agent runner、evidence registry、Magi scheduler、Discord adapter に分割する。
5. utils を attachment policy/downloader/store、context gateway、render store、browser backend に分割する。

### Phase 4: テストと運用の再編

1. tests を unit、contract、characterization、integration、eval に分離する。
2. import 時の `sys.modules`、環境変数、ファイル I/Oを除去する。
3. tool-loop 旧 suite は state-machine characterization、compat suite は正式 facade 契約だけを所有する。
4. collection/contract、unit-fast、characterization、integration、legacy、offline eval を別 CI job にする。
5. dirty deploy、backup/restore rehearsal、staging smoke、rollback threshold を release gate にする。

## 5. 検証ゲート

各小変更は次の順で検証する。

1. 変更対象の専用テスト。
2. 所有サブシステムの関連テスト。
3. `ruff check` と `git diff --check`。
4. 保護manifestを `sha256sum -c docs/2026-07-14-refactor-protected-manifest.sha256`
   で検査し、既存差分と`tool_loop.py` snapshotが不変であることを確認する。
5. 全 pytest。
6. 独立レビューで blocking 指摘がないことの確認。

データ、session、provider、fallback、deploy を変更するフェーズは、staging smoke と
24〜72時間の観察を追加する。構造移動と挙動変更を同じ変更単位に混在させない。

## 6. 2026-07-14 実行結果

Phase 0BからPhase 4までを一気通貫で実装した。HerdrのCodex子プロセス10個を、領域別
走査、実装、独立再レビューへ使用した。各子プロセスは閉じずに完了状態で維持した。
最終運用レビューのHigh 3件、Medium 2件は子プロセスの同時利用上限到達後にルート担当が
実装し、障害注入テストを追加した。

### 6.1 完了した構造変更

- root runtimeを`RuntimeBootstrap`、構造化log lease、PID lock、所有task cleanupへ分離した。
- Discord botをadmission、context、prompt、model、publish、record、outboxへ分離した。
- command/free-codeをguard、response、state、service protocol、gate強制dispatcherへ分離した。
- providerをcontracts、policy、transport、execution、workflow、compositionへ分離した。
- CLI/MCP subprocessをallowlist環境、absolute deadline、PID/PGID/session/procfs generation fence、
  cancel後回収を持つ共通runnerへ統合した。
- Sessionをregistry/repository所有へ整理し、初回load、close、mutation境界を固定した。
- Mem0をcanonical stateとprojectionに分け、lease、ack、retry、idempotency、replay/batch回復を
  実装した。
- Deep Researchをrun state、agent runner、evidence registry、Magi scheduler、Discord adapterへ
  分離し、世代付きprocess cleanupと承認fail-closedを実装した。
- Invest KBへ原子的replace、削除整合性、event-loop offloadを実装した。
- attachment、browser、render、contextをpolicy/backend/store/gatewayへ分離した。
- CIをunit、contract、characterization、integration、eval、legacyへ分け、静的test isolationと
  release manifest gateを追加した。

### 6.2 運用High/Mediumの解消

- deploy、manual deploy、rollbackに同一ファイルシステムの`activation.json`を追加した。
  `old_sha`、`new_sha`、phase、再起動済みconsumerをatomic writeとfsyncで永続化し、
  HEAD比較やrollback pinより前に未完了transactionを再開する。SIGKILL後は再起動済みconsumerを
  飛ばし、未確認consumerを再実行して全active状態とexact release SHAを再検査する。
- generation publicationを外部root所有publisherだけに限定した。全`src`/`venv`通常ファイルの
  path、mode、SHA-256を`release-content.jsonl`へ固定し、metadata digestと起動時health rehashで
  改変をfail-closedにした。publisher未配置は非zeroで終了する。
- `snapshot_state.sh`はSQLite主DBの相対path集合をbackup前、backup後、candidate、最終時点で
  比較する。WAL/SHM/journal物理bytesは除外したまま、集合増減はattempt破棄となる。
- snapshotの全通常ファイル、全directory、manifest、rename元親、保存先親をfsyncし、
  rename前後の耐電断順序を固定した。
- systemdの`EnvironmentFile`後にBOT_DIR、consumer集合、ready port、release gate、rollback runner、
  publisherを固定し、script側allowlistも`/bin/true`注入とconsumer縮小を拒否する。

### 6.3 最終検証

- `ruff check .`: 成功。
- Ruff format: 保護manifest対象を除く変更Python 168ファイルを検査し、67ファイルを整形した。
- mypy: 設定対象3ファイルで成功。
- Bash `-n`: 全運用scriptで成功。
- systemd-analyze verify: prod/dev auto-deploy unitで成功。
- test isolation static check: 成功。
- collection-contract: 45件成功。
- unit-fast: 3,233件成功、21件skip。
- characterization: 153件成功。
- integration: 109件成功、既知警告1件。
- offline-eval: 4件成功。
- legacy: 登録なしの期待終了コード5。
- 全pytest: 3,545件成功、21件skip、既知警告2件。
- 運用・health・release gate対象: 障害注入を含む71件成功。
- `git diff --check`: 成功。
- `services/claude_api/tool_loop.py` SHA-256:
  `31bb2bce986d03beff2a3fd7b4f27da8c1affd570c51497a56ac80c09a141d45`で不変。
- 保護manifest SHA-256:
  `0efa099a6411787d1f67511e9a04f03d737e808d4c2900f776ef9d945fa5df03`で不変。
- 保護manifestの不一致は、承認済みSession実装の`services/session.py`と
  `tests/test_session.py`だけである。

### 6.4 リポジトリ外の未実行作業

本作業ではcommit、release manifest生成、root publisher配置、sudoers変更、service user/ACL/OAuth
credential/state移行、systemd install/daemon-reload、firewall変更、deploy、restartを実行していない。
これらは候補commit確定後に`deploy/OPERATIONS_SECURITY.md`と`deploy/RUNBOOK.md`に従って実施し、
staging smokeと24〜72時間の観察を通過させてからproductionへ進める。
