# 2026-07-02 fable公開前 Discord Bot 改善候補バックログ

## 目的

2026-07-03 JST の fable 再公開前後に、重いリファクタリングと Discord bot としての機能性向上を集中的に進めるため、リポジトリ全体を20領域に分割して改善候補を棚卸しします。

本書は実装計画ではなく、巡回で見つかった改善余地のバックログです。実装時は各項目を issue または小さいPR単位へ分割します。

## 調査方法

- 20件のサブエージェントを領域別に起動して読み取り専用で巡回しました。
- 既存実装ファイルは変更していません。本書のみ更新対象です。
- VI/通知系巡回は長時間化したため、途中時点の結果を回収して採用しました。
- 既存の未コミット変更が複数ある状態で調査しました。
- 2026-07-02 JST に Codex で主要 P0/P1 項目を現行コードへ再照合しました。大半はコード上の再現条件を確認済みです。

## 巡回マップ

| No | 領域 | 主な対象 |
|---:|---|---|
| 1 | Discord入口 | `bot.py`, slash/prefix, job, UX |
| 2 | Claude API中核 | `services/claude_api/entry.py`, `tool_loop.py`, `client_factory.py` |
| 3 | Tool/MCP | `services/claude_api/tools.py`, `restricted.py`, `mcp_client.py`, `mcp_selector.py` |
| 4 | Session/JSONL | `services/session.py`, `session_store.py`, replay, compaction |
| 5 | Mem0書き込み | `services/mem0/ingest.py`, `extract.py`, `dedup.py`, `batch.py`, `graph.py` |
| 6 | Mem0検索/MCP | `services/mem0/search.py`, `bm25_index.py`, `history_index.py`, `mcp_mem0_server.py` |
| 7 | 投資KB | `services/invest_kb/`, `services/invest_kb_eval/`, `scripts/eval_invest_kb*.py` |
| 8 | 添付/レンダリング | `utils/attachments.py`, `math_renderer.py`, `table_renderer.py`, `browser_pool.py` |
| 9 | Web API/OAuth | `web_api.py`, `services/oauth_manager.py`, `services/rate_limit.py` |
| 10 | config/deploy | `config.py`, `deploy/`, `infra/`, systemd, cron |
| 11 | Security | `sentinel.py`, `restricted.py`, `structured_log.py`, `attachments.py`, redaction |
| 12 | Observability | `health.py`, `structured_log.py`, `correlation.py`, `job_state.py` |
| 13 | Tests/CI | `tests/`, `pyproject.toml`, `.github/workflows/`, requirements |
| 14 | Deep Research | `services/deep_research/`, `services/claude_api/agent_tool.py` |
| 15 | VSCode Sync | `services/vscode_sync/`, `tests/test_vscode_sync_*` |
| 16 | VI/通知 | `scripts/vi_*`, `orderbook_monitor.py`, scheduler群 |
| 17 | Frontend | `frontend/kanae-daily/` |
| 18 | Music | `services/music_analyzer/`, media attachment path |
| 19 | Backup/State | `main_async.py`, `runtime_state.py`, `scripts/backup_*.sh`, RUNBOOK |
| 20 | 全体設計 | 巨大ファイル、legacy、依存、型、モジュール境界 |

## 優先度

- P0: 情報漏えい、権限バイパス、データ損失、応答停止、復旧不能、CIでfable破壊を見逃す問題です。
- P1: 主要な品質、保守性、運用信頼性、Discord bot体験に大きく効く問題です。
- P2: 中期的に効く改善です。P0/P1 の後にまとめて処理します。
- P3: 余力対応、文書更新、微修正、見た目や運用快適性の改善です。

## 再レビュー反映メモ

- Web履歴の `api_messages` 露出、restricted alias gate、stdio MCPの全env継承、JSONL replay overflow、`main_task` 例外未伝播、backup整合性、Mem0 batch消失、任意添付残存、音楽解析ブロックはコード上の該当箇所を確認済みです。
- `restricted API key` 未設定時のadmin OAuth fallbackと、refusal診断captureのsystem/messages保存は、運用上の権限境界と情報境界に触れるためP0へ昇格します。
- Deep Researchの明確化/MAGI承認待ちは、設定値 `DEEP_RESEARCH_CLARIFY_TIMEOUT_S` が存在しますが、実際の `wait_for` 経路へ渡されていないため無期限待機として扱います。
- 投資KBのcitation relevance品質は改善候補として残しますが、EDINET等の上位化は評価ログで確認してから実装優先度を確定します。
- 本書更新ではコード変更を行いません。実装時は各行を小さいPRへ分割します。

## 先に切る大きな束

| 束 | 優先度 | 対象 | 内容 |
|---|---:|---|---|
| 情報漏えいと権限境界 | P0 | `web_api.py`, `restricted.py`, `tools.py`, `mcp_client.py`, `client_factory.py`, `tool_loop.py`, `structured_log.py` | Web履歴の内部 `api_messages` 露出、restricted alias ACL、MCP env継承、restricted OAuth fallback、refusal capture、ログredactionを先に閉じます。 |
| 応答停止と復旧性 | P0 | `bot.py`, `main_async.py`, `backup_*.sh`, `deep_research`, `music_analyzer` | セマフォ解放漏れ、異常終了伝播、backup trap、無期限待機、音楽解析ブロックを処理します。 |
| fable API回帰固定 | P0/P1 | `entry.py`, `tool_loop.py`, `.github/workflows/test.yml` | context overflow再発、fallback/sticky model、fable回帰CIを先に固定します。 |
| Memory/RAG整合性 | P0/P1 | `services/mem0`, `mcp_mem0_server.py`, `invest_kb` | batch消失、delete索引掃除、BM25 archive漏れ、投資KB注入対策を処理します。 |
| Bot本体分割 | P1/P2 | `bot.py`, `utils`, `services/session.py` | intake、context、attachment、prompt、render、job lifecycleへ分割します。 |
| 運用基盤 | P1/P2 | `health.py`, `web_api.py`, deploy, backup, CI | live/ready、metrics、ChromaDB依存、backup復元手順、security gateを整備します。 |

## P0 候補

| 領域 | 項目 | 対象 | 改善案 |
|---|---|---|---|
| Web API | Web履歴返却で内部 `api_messages` が露出し得ます。 | `web_api.py`, `services/session.py` | `history` event返却前に表示用フィールドだけをwhitelistします。 |
| Security | restricted modeで `shell` / `read_file` などalias経由のACLバイパス余地があります。 | `restricted.py`, `tools.py`, `tool_loop.py`, `entry.py` | tool名をcanonical化してからdefault denyで判定します。 |
| Security/MCP | stdio MCPへ全環境変数を渡しています。 | `services/claude_api/mcp_client.py`, `config.py` | MCP別env allowlistに変更し、必要キーだけ渡します。 |
| Discord | チャンネルセマフォ取得後、main `try/finally` 前の例外で解放漏れが起き得ます。 | `bot.py` | `acquired` フラグ付き `try/finally` を取得直後から適用します。 |
| Claude API | `context_overflow` 時にsessionをclearしても、次回JSONL replayで同じ巨大履歴を再送します。 | `services/claude_api/entry.py` | overflow時に強制compactionまたは緊急短縮replayへ切り替えます。 |
| Claude API | restricted API key未設定時にadmin OAuthへfallbackします。 | `services/claude_api/client_factory.py` | fail closedを既定にし、明示env時のみfallbackします。 |
| Claude API | refusal診断captureがsystem/messages/headersを `/tmp/fable5_capture` に保存します。 | `services/claude_api/tool_loop.py` | env gate、redaction、TTL、state dir化を追加します。 |
| Main/SRE | `main_task` が先に終了しても例外を再送出せず、systemdで正常終了扱いになります。 | `main_async.py`, `discord-bot.service` | bot/web task異常終了を非ゼロ終了へ伝播します。 |
| Backup | `backup_mem0.sh` の `trap` が停止後に設定され、失敗時にbot停止が残り得ます。 | `scripts/backup_mem0.sh` | stop前にtrapを設定し、元のactive状態だけ復元します。 |
| Backup | `.state/` を稼働中に `rsync` し、SQLite/WAL/ChromaDB整合性が保証されません。 | `scripts/backup_state.sh`, `infra/systemd/chromadb-server.service` | 停止済みsnapshotまたはSQLite `.backup` に統一します。 |
| Mem0 | batch flush失敗時に未保存ターンが消え得ます。 | `services/mem0/batch.py` | `inflight` 退避、成功ack、失敗復元へ変更します。 |
| Mem0 | `delete_memory()` がMem0本体だけを削除し、BM25/graphに残り得ます。 | `services/mem0/client.py`, `bm25_index.py`, `graph.py`, `mcp_mem0_server.py` | 削除の単一入口で本体、BM25、graph edgeを同時処理します。 |
| Deep Research | 明確化/MAGI承認待ちが無期限で `_research_busy` を保持します。 | `services/deep_research/orchestrator.py`, `config.py` | 既存timeout設定を実待機経路へ接続し、lease、既定動作、管理者解除を追加します。 |
| Tests/CI | fable fallback系テストがCI固定列挙から漏れています。 | `.github/workflows/test.yml`, `tests/test_claude_api_tool_loop.py` | fable回帰ジョブを追加します。 |
| Music/Attachment | 任意添付と音声ファイルが処理後に残り、最大25MB級の一時ファイルが蓄積します。 | `bot.py`, `utils/attachments.py`, `services/music_analyzer/analyzer.py` | arbitrary attachment pathを追跡し、完了後削除またはTTL cleanupします。 |
| Music | 音楽解析がClaude呼び出し前に最大3ファイルを逐次解析し、応答停止要因になります。 | `services/music_analyzer/analyzer.py`, `bot.py` | 総時間上限、キャンセル伝播、バックグラウンドjob化を追加します。 |

## P1 候補

| 領域 | 項目 | 対象 | 改善案 |
|---|---|---|---|
| Discord | 完成済みprompt全体を末尾切断し、信頼境界タグが途中で切れます。 | `bot.py`, `services/prompt_builder.py` | component-aware budgetで部品単位に切り詰め、wrapperを必ず閉じます。 |
| Discord | 前処理taskが `/abort` 時に未awaitで残り得ます。 | `bot.py` | `TaskGroup` またはfinally cancel/gatherへ変更します。 |
| Discord | `on_message` が約1800行で責務過多です。 | `bot.py` | intake、context、attachments、prompt、render、job cleanupへ分割します。 |
| Discord | admin-only slash commandが全員に表示され、実行後に手動拒否します。 | `bot.py` | `app_commands.check` と `default_permissions` を導入します。 |
| Claude API | auth retry後も Agent dispatchの `api_client` が古い可能性があります。 | `services/claude_api/entry.py` | client再生成時に `_agent_dispatch["api_client"]` も更新します。 |
| Tool/MCP | MCP tool resultの出力上限と `isError` 反映が不足しています。 | `services/claude_api/mcp_client.py` | head-tail truncation、byte cap、error wrapperを追加します。 |
| Tool/MCP | 未広告MCPでもlazy spawn可能です。 | `mcp_client.py`, `entry.py` | 実行時にも広告済みtool/server allowlistを検証します。 |
| Tool/MCP | MCP起動、initialize、list_toolsにtimeoutがありません。 | `mcp_client.py` | サーバー別spawn timeoutと失敗隔離を追加します。 |
| Session | `(channel_id,user_id)` session keyが永続化時にchannel-onlyへflattenされます。 | `services/session.py`, `session_store.py` | versioned `sessions.json` でcomposite keyを保持します。 |
| Session | compact summaryがtail外に出るとreplayから消えます。 | `services/session.py`, `compaction.py` | 最新summaryをtailとは別に逆走査して合成します。 |
| Session | 実際のreplay有無と `actually_resumed` が不一致です。 | `entry.py`, `bot.py` | `bool(prior_history)` をsession freshnessと分離して返します。 |
| Mem0 | batch summaryが抽出入力上限で途中切断されます。 | `services/mem0/batch.py`, `extract.py` | chunk分割またはbatch sizeを入力上限から逆算します。 |
| Mem0 | 履歴由来の `created_at` などmetadataがallowlistで落ちます。 | `services/mem0/ingest.py` | 信頼caller用に時刻系metadataを明示許可します。 |
| Mem0 | `score=None` を距離0扱いし、dedupで誤dropし得ます。 | `services/mem0/dedup.py` | 不正scoreはskipまたは大距離扱いにします。 |
| Mem0 | soft archiveとentity graph relation expiryが連動しません。 | `services/mem0/ingest.py`, `graph.py` | source fact/event idを保存し、archive時にrelationもexpireします。 |
| Mem0 | BM25単独hitは `valid_to` metadataを見ず、archive済みfactを返し得ます。 | `search.py`, `bm25_index.py` | BM25索引に `valid_to` を持つか、本体検証を挟みます。 |
| Mem0 | MCP向けmemory出力が通常prompt用scrubと差分があります。 | `mcp_mem0_server.py`, `services/mem0/format.py` | MCP出力にも同じsanitizerを適用します。 |
| 投資KB | KB本文が未サニタイズで `<shared_investment_knowledge>` に入ります。 | `services/invest_kb/search.py`, `bot.py` | 投資KB用scrubとsentinel対象タグを追加します。 |
| 投資KB | Haiku前処理promptへ本文/クエリを三重引用符で直接埋めています。 | `services/invest_kb/haiku.py` | JSONエンコード済み入力とschema検証へ変更します。 |
| 投資KB | citation relevanceが低く、不適切sourceが上位化する可能性があります。 | `services/invest_kb/search.py`, `scripts/eval_invest_kb.py` | 評価ログで上位sourceを確認し、BM25/FTS hybrid、intent別filter、source優先度を導入します。 |
| 投資KB | 本番limit=3と評価limit=5が不一致です。 | `scripts/eval_invest_kb.py`, `bot.py` | 評価を本番設定に揃え、top idsとscoresを保存します。 |
| Attachment | text添付を `resp.read()` 後にサイズ判定しています。 | `utils/attachments.py` | chunk読みで上限超過時点で中断します。 |
| Attachment | arbitrary添付のdenylistがsanitize前拡張子に依存します。 | `utils/attachments.py` | sanitize後basenameから拡張子を取り直します。 |
| Rendering | table/math入力長、行数、clip寸法に上限がありません。 | `table_renderer.py`, `math_renderer.py`, `_render_daemon.js` | render前に文字数、行列数、clip最大値を制限します。 |
| Web API | Web経由は `user_id` と `author` を `call_with_tools` へ渡していません。 | `web_api.py`, `services/session.py` | Web側でも `(channel,user)` sessionへ統一します。 |
| Web API | WebとDiscordでrate limit台帳が分かれています。 | `web_api.py`, `services/rate_limit.py` | Web側も共有台帳へ寄せます。 |
| Web API | Web側は同一channelの `send_message` を直列化していません。 | `web_api.py`, `bot.py` | channel semaphoreをWeb APIにも導入します。 |
| OAuth | token refreshが排他されず、多重refreshが走り得ます。 | `services/oauth_manager.py` | refresh lockを追加し、lock内で期限を再確認します。 |
| Security | `sanitize_untrusted_text` 経路でNFKC/zero-width除去が一貫して使われていません。 | `services/sentinel.py`, `bot.py`, `prompt_builder.py` | sanitizerを `apply_sentinel_filters` に統一します。 |
| Security | admin添付本文がtrusted wrapper内に入ります。 | `utils/attachments.py`, `bot.py`, `prompt_builder.py` | 添付本文はadminでもdata/untrusted blockへ分離します。 |
| Security | structured logに横断的redactionがありません。 | `services/structured_log.py`, `vscode_sync/redactor.py` | 共通redactorを再帰的に適用します。 |
| SRE | `/health` がliveness/readinessを混在させ、degradedで503を返します。 | `services/health.py`, `web_api.py` | `/live`, `/ready`, `/health` に分離します。 |
| SRE | Mem0 healthがimport成功のみです。 | `services/health.py` | lightweight heartbeatまたは直近初期化失敗状態を返します。 |
| SRE | metrics endpointがありません。 | `web_api.py`, `job_state.py`, `tool_loop.py` | `/metrics.json` でTTFT、token、queue、rate-limitを返します。 |
| Deploy | 現行stdio MCPと旧systemd MCP unitが混在しています。 | `deploy/systemd`, `config.py` | 旧unitをdeprecated隔離し、runbookを現行方式へ更新します。 |
| Deploy | bot unitがChromaDB unitへ `After/Wants` を持ちません。 | `discord-bot.service`, `chromadb-server.service` | `Wants/After` または `ExecStartPre` heartbeatを追加します。 |
| Backup | ChromaDB正本パスとbackup対象が不一致です。 | `backup_state.sh`, `backup_mem0.sh`, `deploy/RUNBOOK.md` | `.state/mem0_db` 基準へ統一します。 |
| Deep Research | checkpointがなく、再起動後にplan/source/result/threadを復元できません。 | `types.py`, `orchestrator.py` | phaseごとにJSON/SQLite checkpointを保存します。 |
| Deep Research | 単一agent経路だけ最終レポート整形と引用検証を迂回します。 | `orchestrator.py`, `sources.py` | Phase 0も `build_final_report` を通し、機械収集ソース一覧とmissing SID注記を付与します。 |
| Deep Research | Agent toolの再帰設計と実行経路が不一致です。 | `agent_tool.py`, `registry.py` | 再帰実装まで子Agentから `Agent` を除外します。 |
| VSCode Sync | flush中の新規eventが次回flush予約されません。 | `aggregator.py` | `_do_flush()` 後にbuffer残存時は再スケジュールします。 |
| VSCode Sync | truncate後redactでsecret断片が漏れ得ます。 | `parser.py`, `redactor.py` | redact後truncate、またはsecret-aware truncateへ変更します。 |
| Frontend | `kanae-daily` APIがVite middleware依存で静的配信にありません。 | `frontend/kanae-daily/vite.config.ts` | 本番APIをWeb API側または小API serverへ移します。 |
| Frontend | 読み取り失敗時に絶対パスをUIへ表示します。 | `vite.config.ts`, `App.tsx` | UI向けエラーは短いcodeに限定します。 |
| Music | 25MB音声を全読みしbase64化し、fallback時に再base64化します。 | `music_analyzer/analyzer.py` | サイズ/尺/形式上限、base64再利用、必要なら切り出しを導入します。 |
| Overall | `bot.py`, `config.py`, `claude_sdk.py`, `claude_api/__init__.py` が巨大化/legacy混在しています。 | `bot.py`, `config.py`, `services/claude_sdk.py`, `services/claude_api/__init__.py` | rollback方針決定後、facade薄化と責務別分割を進めます。 |

## P2 候補

| 領域 | 項目 | 対象 | 改善案 |
|---|---|---|---|
| Discord | `/model` が任意文字列を受け付けます。 | `bot.py` | 許可モデル一覧、alias、autocompleteを追加します。 |
| Discord | empty promptより前に非admin quota/cooldownを消費します。 | `bot.py` | mention除去後の空判定をquota前へ移します。 |
| Discord | rich経路 `_send_segmented` の添付上限処理が他経路と不一致です。 | `bot.py`, `utils/attachments.py` | 10件単位分割または共通送信serviceへ統合します。 |
| Claude API | `x-client-request-id` がclient構築時に1回だけ生成されます。 | `client_factory.py` | stream呼び出しごとにUUIDを付与します。 |
| Claude API | `_last_context_tokens` が無制限dictです。 | `tool_loop.py` | LRUまたはsession連動削除を追加します。 |
| Claude API | Anthropic/httpx client lifecycleが明確ではありません。 | `oauth_manager.py`, `entry.py` | `AsyncExitStack` または共有poolでclose責務を明示します。 |
| Session | `role=channel` が多いと有効な会話tailが不足します。 | `services/session.py`, `bot.py` | 採用role行が十分になるまでbounded追加読みします。 |
| Session | tool_use/tool_result検証が隣接条件を見ていません。 | `services/session.py` | assistant直後のuser frameだけを対応先とします。 |
| Session | JSONL appendが1ターン2回fsyncでI/O過多です。 | `services/session.py`, `entry.py`, `bot.py` | user/assistant pair batch appendを検討します。 |
| Mem0 | extractor出口のschema検証が弱いです。 | `extract.py`, `ingest.py` | category、importance、tier、tags、entitiesを正規化します。 |
| Mem0 | `_claim_insert` が失敗時に残ります。 | `ingest.py` | 成功後commitまたは失敗時claim削除へ変更します。 |
| Mem0 | `reinforced_at` がmetadata scalar制約と矛盾します。 | `reinforce.py`, `ingest.py` | JSON文字列またはCSVへ変換します。 |
| Mem0 | query variantが多く、同一turn cacheや低価値variant除外が限定的です。 | `search.py` | variant数、LLM段数、cache、加点方式を明示します。 |
| History | 履歴索引に `author_id`, `guild_id`, `thread_id`, `message_id` がありません。 | `history_index.py`, `history_embed.py` | schema拡張と再索引を実施します。 |
| 投資KB | source_url/license/published_atをpromptや評価に出していません。 | `client.py`, `search.py` | URL、取得日、license、section、offsetを持たせます。 |
| 投資KB | chunkerは長文1文でmax_size超過し得ます。 | `chunker.py`, `ingest.py` | oversized unitの強制分割とstart/end保存を追加します。 |
| 投資KB | Discord注入経路でKB検索完了を待ち切ります。 | `bot.py`, `search.py` | `wait_for` とvector-only fallbackを追加します。 |
| Rendering | KaTeX探索候補にrepo local `node_modules/katex/dist` がありません。 | `math_renderer.py` | 探索候補と遅延解決を追加します。 |
| Rendering | SVG判定が粗く、空白/BOM付きSVGを拒否し任意XMLをSVG扱いします。 | `image_validator.py`, `attachments.py` | XML root検証へ変更します。 |
| Rendering | fenced codeのmaskがバッククォート3個のみです。 | `markdown_parser.py` | `~~~` とインデントcodeもmaskします。 |
| Web API | Web UIがstatusだけでdelta/tool/final eventsを受けていません。 | `web_api.py`, `entry.py` | Socket.IOに `message_delta`, `tool_invocation`, `message_final` を追加します。 |
| Web API | session verify URL、cookie名、internal invalidateが固定です。 | `web_api.py`, `config.py` | env化とshared secretを追加します。 |
| Observability | job_stateにphase履歴、終了理由、直近完了履歴がありません。 | `job_state.py`, `bot.py` | bounded historyとsnapshotを追加します。 |
| Observability | ログの多くがstructured fieldでなく文字列埋め込みです。 | `structured_log.py`, `bot.py` | `extra` field化へ段階移行します。 |
| Observability | status表示にBash commandやURL/queryが出ます。 | `status.py`, `web_api.py`, `bot.py` | status用redactionとWeb側throttleを入れます。 |
| CI | dev依存installが `|| true` で握り潰されます。 | `.github/workflows/test.yml`, `requirements-dev.txt` | 失敗時にCIを落とします。 |
| CI | `pip-audit` と `bandit` が非blockingです。 | `.github/workflows/security.yml` | finding allowlist化後にfail-fastへ移行します。 |
| VSCode Sync | `last_seen`, `ended`, `session_meta` が長期稼働で増えます。 | `daemon.py` | TTLまたはLRU上限を追加します。 |
| VSCode Sync | tailerが大きい追記を一括readします。 | `tailer.py` | 1 pollあたりの最大読込量を設けます。 |
| Deep Research | token/deadline budget判定が遅いです。 | `orchestrator.py` | planner/searcher/MAGI各段でbudget gateを入れます。 |
| Deep Research | 引用なし本文が成功扱いになります。 | `sources.py`, `tests/test_deep_research.py` | 警告、再生成、参考ソース候補へ分離します。 |
| Music | `.aac`, `.wma`, `.opus` をOpenAIへ既定 `mp3` として送ります。 | `music_analyzer/analyzer.py` | provider別対応形式表と変換/skipを追加します。 |
| Music | Gemini/OpenAI呼び出しに429/5xx retryがありません。 | `music_analyzer/analyzer.py` | Retry-Afterと指数backoffを追加します。 |
| Frontend | `kanae-daily` が最新mtimeを取得しているが停止検知を表示しません。 | `App.tsx`, `.state/kanae_daily.json` | 最新entry/mtime/停止判定バッジを表示します。 |
| Frontend | 新着判定で `prevTopTsRef` 未更新のままreturnします。 | `App.tsx` | 新着検知時にもrefを更新します。 |
| VI/通知 | schedulerのsystemd起動とbot on_ready起動が二重化し得ます。 | `vi_signal_scheduler.py`, `intervention_scheduler.py` | 起動経路を一本化、またはlockで二重実行を防止します。 |
| VI/通知 | subprocess実行とログ追記が個別実装です。 | `vi_signal_scheduler.py`, `intervention_scheduler.py` | 共通runnerを作ります。 |
| Dependencies | runtime requirementsにscript/ingest/dev寄り依存が混在します。 | `requirements.txt`, `requirements.lock` | runtime、ops、ingest、dev lockへ分けます。 |
| Typing | type/lint設定がありません。 | `pyproject.toml`, `requirements-dev.txt` | `pyright` または `mypy` をwarning運用で導入します。 |

## P3 候補

| 領域 | 項目 | 対象 | 改善案 |
|---|---|---|---|
| Discord | `command_prefix="!"` と手動prefix解析が併存します。 | `bot.py` | commands.Bot commandへ寄せるかprefix設定を削除します。 |
| Discord | `BOT_KEYWORDS` が部分一致です。 | `bot.py` | 行頭呼びかけ、句読点境界、channel allowlistを追加します。 |
| Claude API | docstringが現行JSONL保存仕様とずれています。 | `tool_loop.py` | 現行lossless replay仕様へ更新します。 |
| Mem0 | entity lookupの大小文字仕様とdocstringが不一致です。 | `graph.py` | 正規化列または `COLLATE NOCASE` を導入します。 |
| Mem0 | `services.mem0_service` legacy re-export参照が残っています。 | `services/mem0_service.py`, `bot.py` | `services.mem0` importへ移行し削除予定日を付けます。 |
| 投資KB | E2E A/B順序が固定です。 | `scripts/eval_invest_kb_e2e.py` | A/B順序をランダム化し、judge入力を保存します。 |
| Rendering | Puppeteer/Chrome候補が絶対パス中心です。 | `browser_pool.py` | env設定化とrender health checkを入れます。 |
| Web API | rate limit待ち秒数が切り捨てで0秒表示になり得ます。 | `services/rate_limit.py`, `web_api.py` | `ceil` と最低1秒表示へ変更します。 |
| Frontend | READMEがViteテンプレートのままです。 | `frontend/kanae-daily/README.md` | データパス、起動、本番構成、トラブル確認を追記します。 |
| VI/通知 | 日経CSV URLとVI proxy計算が分散しています。 | `scripts/vi_*`, `monthly_nikkei_vi_proxy.py` | 市場データ取得と計算を副作用なしmoduleへ分離します。 |
| State | `atomic_write.py` が親ディレクトリfsyncを行いません。 | `utils/atomic_write.py` | 高価値state書き込みでrename後dir fsyncを追加します。 |
| Git | `htmlcov/` と `bot.log*` がignore対象ではありません。 | `.gitignore` | ignoreへ追加します。 |

## 実装順序案

1. P0の情報漏えい、権限境界、restricted認証fallback、refusal capture、セマフォ/異常終了、backup整合性を先に処理します。
2. fable公開直後の回帰を固定するため、`tool_loop.py`、`entry.py`、fallback、compaction、text streaming、sticky modelのテストを先に固定します。
3. Memory/RAGは `batch.py` のack化、`delete_memory()` 索引掃除、BM25 archive対応、投資KB sanitizeの順で処理します。
4. `bot.py` 分割は、先に代表挙動テストを固定し、Attachment/ResponseRenderer/PromptContextの順で切り出します。
5. Deep ResearchとAgent toolは、無期限待機解消、checkpoint、引用検証統一、Agent再帰禁止の順で処理します。
6. SREは live/ready、metrics、ChromaDB依存、backup/restore runbook、security gate fail-fastの順で処理します。
7. P2/P3の依存分割、型検査、frontend、VI/通知、VSCode syncを後続の束として処理します。

## 確認事項

- fable公開直後はモデル挙動差分が出るため、fallback、tool loop、compaction、text streaming、sticky modelのテストを先に固定します。
- 本書は改善候補の列挙です。実装前に運用ログ、systemd有効状態、実backup配置、CI実行履歴を確認します。
- 投資KBのsource順位品質は評価ログで再確認してから、検索方式変更の範囲を決めます。
- 本書作成では既存実装ファイルに変更を加えていません。
