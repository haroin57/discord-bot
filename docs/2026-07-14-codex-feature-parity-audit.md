# Codex Discord bot 機能要件再監査

- 監査日: 2026-07-14。
- 対象: `/home/ubuntu/discord-bot`の共有Discord処理、Claude経路、Codex app-server経路、Hermes identity serviceです。
- 判定: repositoryのテストで定義された機能要件は、既定レーンとMem0α別設定レーンを合わせて全件成功しました。

## 1. テスト実行結果

テストmoduleは120件、収集されたtest caseは2761件です。

- 既定レーン: `2740 passed, 21 skipped, 6 warnings`、37.67秒です。
- 既定レーンのskip 21件はすべて`MEM0_ALPHA_ENABLED=1`専用です。
- Mem0α別設定レーン: `21 passed`、6.67秒です。
- 二つのレーンを合わせ、収集された2761件をすべて実行して成功しました。
- warning 6件は既存LanceDB `table_names()`のdeprecation warningだけです。
- pytest終了時に発生していた未close `aiohttp.ClientSession`は、shutdownを持たないE2E test harnessが実sessionを生成したことが原因でした。非network session doubleへ修正後は発生していません。

## 2. 監査マトリクス

| 要件領域 | 主な証拠 | Codex botでの判定 |
| --- | --- | --- |
| Discord routing、mention、DM、role、queue、cooldown、quota | `test_bot.py`、`test_job_state.py`、`test_rate_limit.py` | 共有`on_message`経路として合格しました。queueの旧skipを実並行testへ置換しました。 |
| prompt trust boundary、injection防御、error masking | `test_e2e_security.py`、`test_sentinel.py`、`test_prompt_builder.py` | provider選択前の共有処理として合格しました。 |
| attachment download、MIME/size/path/symlink制限、画像 | `test_attachments.py`、`test_image_validator.py`、`test_claude_api_image_processor.py` | 共有download/validation後にCodex `localImage`へ渡す構成で合格しました。 |
| streaming、spinner、status、tool notice | `test_spinner_live.py`、`test_bot_ui_plan.py`、`test_bot_ui_scratch.py`、Codex live canary | Codex delta/completed itemを既存callbackへ投影し、実streamで確認しました。 |
| 長文分割、Markdown table、TeX画像 | `test_discord_helpers.py`、`test_table_renderer.py`、`test_markdown_parser.py`、`test_math_renderer.py` | `$...$`、`$$...$$`、`\(...\)`、`\[...\]`を検出し、Codex指定bot統合testと実PNG canaryで合格しました。 |
| provider選択、model/effort、restricted gate | `test_provider_router_codex.py`、`test_model_command.py`、Codex package tests | 管理者Codex、非管理者Claude固定、model overrideを確認しました。 |
| OAuth、model pin、thread resume、restart resume | Codex client/session/manager testsとlive canary | ChatGPT OAuth、`gpt-5.6-sol/openai`、同一thread、process restart resumeを確認しました。 |
| native tool、approval、MCP | Codex session tests、MCP status、3-turn live canary | MCP処理はnative `mcpToolCall`で実行し、Cloudflare turnのBash proxyは0件でした。 |
| memory、history、compaction、Mem0α | Mem0/history/sessionの各testとMem0α別lane | 既定laneとalpha laneの両方で合格しました。 |
| free-code、prefix/slash command、Claude tag、deep research | 各`test_free_code_*`、`test_claude_tag.py`、`test_deep_research.py` | bot全体の機能として維持されています。Claude固有agent loopを必要とする機能はClaude経路を使用します。 |
| health、shutdown、atomic state、archive、cleanup | `test_health*`、`test_main_async.py`、`test_atomic_write.py`、`test_r2_archive.py`、`test_render_cleanup.py` | 両systemd serviceの再起動、health HTTP 200、state分離を確認しました。 |

## 3. TeX回帰の原因と修正

従来parserはドル区切りだけを認識していました。Codexが標準的に出力する`\(...\)`と`\[...\]`は通常textへ落ち、`render_math_image`へ到達していませんでした。

修正内容は次のとおりです。

- 非stream parserで4種類のdelimiterを同じ`MathSegment`へ変換します。
- incremental parserで未閉鎖の`\(`と`\[`を保持し、raw TeXを一時表示しません。
- code fence内のdelimiterを画像化しません。
- 提示された行列形式を回帰fixtureにし、4区間、display 2件、inline 2件を検出します。
- Codex provider指定の`on_message` testで、`\[B=\begin{pmatrix}...\end{pmatrix}\]`が画像file replyへ到達することを確認します。
- production同等のsystemd hardening下で行列を406×156 PNGへ生成しました。

## 4. provider境界

テスト要件の合格は、Claude固有実装をCodex互換形式へ偽装したことを意味しません。bot全体として次の境界を維持します。

- Discord入力、queue、prompt構築、memory recall、attachment、表示、table/TeX、archive、healthは共有します。
- ClaudeとCodexはprovider routerの後で分離します。
- restricted user、deep research、Claude tagなどClaude固有loopを必要とする機能はClaude経路を維持します。
- Codexはnative thread、native tool、native MCPを使用します。
- Hermes identityだけは運用指定により`danger-full-access/never`です。Kanae serviceは従来の`workspace-write/untrusted`を維持します。

## 5. 実稼働確認

- `discord-bot.service`: enabled、active、health port 3002 HTTP 200です。
- `hermes-discord-codex.service`: enabled、active、health port 3004 HTTP 200です。
- Hermes用app-serverの実argvで`--dangerously-bypass-approvals-and-sandbox`を確認しました。
- MCPはローカル4サーバーとCloudflare 5サーバーの計9サーバーで、欠落、空tool、許可外serverなしです。
- Cloudflare Docs検索はnative MCP eventで完了し、そのturnのBash eventは0件です。

## 6. 判定上の制約

- 外部API、Discord、Cloudflare、OAuth accountの将来のavailabilityまではrepository testで保証しません。live canaryとhealth監視で補完します。
- LanceDB deprecation warning 6件は機能失敗ではありませんが、将来のlibrary更新前に`list_tables()`へ移行する保守対象です。
- Hermesの危険bypassは意図した運用設定であり、sandbox/approval相当の安全要件を満たすという判定ではありません。
