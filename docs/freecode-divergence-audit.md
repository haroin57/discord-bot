# free-code（Claude Code本家ソース）とbotツールコール実装の差分監査

**作成日**: 2026-07-12 ／ **対象**: `discord-bot/services/claude_api/`（直API tool loop） vs `free-code/src/`
**方法**: 両リポジトリを並列棚卸しし（file:line付き）、共通ツールの意味論・ループ機構・安全機構を突き合わせ
**前提**: free-codeは`tengu_*`フラグ・`USER_TYPE==='ant'`分岐を含む**Claude Code本家TSソースのコピー**。つまり本書は「本家仕様との差分」の監査である。Skillツールは2026-07-12に移植済み（本家`prompt.ts`原文＋一覧のdescription埋め込み化）。

---

## 1. ツール対応表

| free-code（本家） | bot | 備考 |
|---|---|---|
| Read/Write/Edit/Bash/Glob/Grep/TodoWrite/Skill/Agent(Task) | ✅ あり | 意味論差分は§2 |
| WebFetch / WebSearch | ❌ ビルトイン無し | brave-search等の**MCPで代替**（意図的。ただし§5-7参照） |
| NotebookEdit / LSP / REPL / AskUserQuestion / ExitPlanMode / Task*(バックグラウンドタスク管理) / Team* / Workflow / ToolSearch(ローカル版) | ❌ 無し | Discord botの用途外が大半。ToolSearchはbot側にserver-side Tool Search統合あり（entry.py:583-604、pause_turn対応） |
| （無し） | **Scratchpad / SecondOpinion** | bot独自。ScratchpadはCLIのnotepadに相当する自作 |
| permission UI（ask） | restricted二値＋per-serverゲート | 構造差。§3-4 |

## 2. 共通ツールの意味論差分（重要なものだけ）

### 2.1 Read — **最大の乖離点**
| 項目 | 本家 | bot |
|---|---|---|
| 行番号 | **cat -n形式で付与**（file.ts:288-316）＋stripLineNumber逆変換 | **付与しない**（生テキスト） |
| offset/limit | あり（続き読み可能） | **無し**（2000行/1MBで打ち切り、以降読めない） |
| サイズ超過 | 256KB/25kトークンの2段ゲート＋「offset/limitを使え」エラー | `[truncated at 2000 lines]`付与のみ |
| 退避 | maxResultSizeChars=**Infinity**（Read結果は絶対にディスク退避しない） | 同様に退避なし（一致） |
| PDF/notebook | pages指定・6型discriminatedUnion | 画像のみ（vision block） |

**影響**: 大ファイルの後半が原理的に読めない。またモデルは「Readは行番号付き」の事前学習を持つため、行番号無しはEditのold_string作成では有利（プレフィクス混入なし）だが、「N行目を見て」系の指示に弱い。**offset/limitの追加を推奨（工数小）**。

### 2.2 Write/Edit — **read-before-edit強制が無い**
- 本家: `readFileState`（パス→read時mtime）で (a)未Readの書込を拒否 (b)Read後に外部変更されたファイルへの書込を拒否（FileWriteTool.ts:198-216、FileEditTool.ts:275-306、TOCTOU再チェック＋書込後mtime更新まで）
- bot: **どちらも無し**。盲目Write/Editが通る
- その他細部: 本家はold==new拒否・空old_string＋新規ファイル=作成扱い・複数マッチ時の定型文言。botは対応不明瞭（tools.py:426周辺は素朴実装）
- **影響**: adminでコード編集させる用途では事故要因。ToolContextに`read_state: dict[path, mtime]`を足すだけで本家同等になる（工数小〜中）

### 2.3 Bash
| 項目 | 本家 | bot |
|---|---|---|
| timeout単位 | **ms**（既定120_000/最大600_000） | **秒**（既定120/最大600、クランプ） |
| 出力cap | 30,000字→**超過分はファイル退避**（64MB上限）＋パス返却 | 30,000B middle-cut（`[N bytes truncated]`）。退避なし（evidence indexが原文台帳として部分代替） |
| run_in_background | あり＋timeout時自動BG化 | **無し** |
| sandbox | sandbox-runtime＋excludedCommands＋dangerouslyDisableSandbox | 無し（admin全信頼・restrictedはBash自体deny — **意図的で妥当**） |
| 並列 | isReadOnly(input)判定で読み取り専用コマンドのみ並列可 | Bashは常に直列（CONCURRENCY_SAFE_TOOLS外） |

**注意（LOW-MED）**: timeout単位差。モデルが本家の癖で`timeout: 30000`（30秒のつもり）を渡すと、botでは30000秒→600秒にクランプされ**意図の20倍待つ**。schema descriptionに「秒」と明記するか、`>600`をmsと解釈するヒューリスティックを推奨。

### 2.4 Grep / Glob
- 本家Grep: output_mode(content/files_with_matches/count)・-A/-B/-C・-n・-i・type・head_limit(既定250)・multiline。**bot: patternとglobのみ**、rg -n固定、1000件cap
- 本家Glob: 100件cap＋「絞り込め」メッセージ。bot: 1000件cap
- **影響**: botのGrepは常時content相当を返しトークン浪費しがち。head_limit/output_modeの移植は費用対効果が高い（工数小）

### 2.5 TodoWrite / Skill / Agent
- TodoWrite: ほぼ同等。bot独自の追加防御（100件・各500字）は妥当。本家の全完了時`verificationNudgeNeeded`はbotに無し（軽微）
- Skill: 移植済み。既知の意図的差分＝一覧をsystem-reminderでなくdescription埋め込み（skills追加はbot再起動反映）
- Agent: botはSUBAGENT_ENABLED＋adminのみ・並列4・専用budget（20turn/600s/800k）。本家のisolation(worktree)/team系は未移植（用途外）

## 3. ループ機構の差分

### 3.1 並列tool_use — **botは保守的すぎる**
- 本家: ターン内ブロックを「連続するconcurrency-safeバッチ」に**分割**し、safeバッチは並列（上限10）・非safeは単体直列（toolOrchestration.ts）。Streaming版はキュー＋エラー時の相互キャンセルまである
- bot: **全ブロックがsafe集合{Read,Glob,Grep,Agent}のときだけ**全並列、1つでも混ざると全直列（tool_loop.py:2388-2422）
- **影響**: Read×3＋Bash×1のターンで全直列になる。バッチ分割の移植で並列機会が増える（工数中）

### 3.2 結果サイズの扱い — 思想が異なる（どちらも一貫していれば可）
- 本家: ツール別`maxResultSizeChars`＋超過は**ディスク退避＋プレビュー＋パス**（toolResultStorage.ts）
- bot: builtin各自cap（middle-cut）＋MCP結果のみ20,000字cap＋distill蒸留＋evidence index台帳
- **確認済み（2026-07-12）**: .envで`DELIB_DISTILL_ENABLED=true`・`TOOL_SEARCH_ENABLED=true`（コード既定はfalseだが本番は有効）。蒸留＋台帳の組は本家のディスク退避＋プレビューと機能的に等価以上

### 3.3 ループ制御 — botの方が精緻（本家に無い防御が多い）
bot独自: RequestBudget（wallclock/output tokens、soft 0.8でwrap-up nudge、hardでcheckpoint離脱）、ToolLoopDetector（同一呼び出し3回警告/5回break、読み取り系は緩和）、text-invoke救済（3回）、空ターンnudge、refusal時のOpusフォールバック、幻覚保存検知。本家はmax_turns＋permission中心。**この領域は移植不要、むしろbotが上**。

### 3.4 permission
- 本家: deny/ask/allow＋PermissionMode＋Bash分類器＋hooksの多層。**askはインタラクティブUI前提**
- bot: restricted二値（非admin=ツール広告ゼロ＋hard deny 19種＋MCP bare名照合）＋per-serverゲート（realestate/nature-remo, fail-closed）
- **判定: 意図的で妥当**。Discordの無人ループでaskは成立しない。ただし本家の`dangerousPatterns`系（rm -rf等の破壊コマンド警告）だけはadmin側Bashに移植する価値あり（工数小）

## 4. bot独自機能（本家に無い・維持すべきもの）

Scratchpad（熟考P3）／SecondOpinion（熟考P4）／distill＋evidence index／loop_guard一式／MCP selectorのtier×keyword/semantic選定／STATIC_MCP_TOOL_SCHEMAS（未spawnサーバーの静的広告）／microcompact・proactive compaction／server-side Tool Search統合（400でauto-off）。いずれも直API・無人運用への適応で、本家との差分だが**退行ではない**。

## 5. 推奨アクション（優先度順）

| # | 項目 | 状態 | 根拠 |
|---|---|---|---|
| 1 | Readに`offset`/`limit`追加 | **✅ 対応済み(2026-07-12)** | 継続案内つき窓読み実装 |
| 2 | read-before-edit（readFileState相当） | **✅ 対応済み(2026-07-12)** | ToolContext.read_state。本家文言で拒否。**副産物: Editの2000行超切り詰め書き戻し（データ損失バグ）も発見・修正** |
| 3 | Grepに`output_mode`/`head_limit`(250)/`context`/`-i` | **✅ 対応済み(2026-07-12)** | 既定はfiles_with_matches（本家同値） |
| 4 | 並列tool_useのバッチ分割 | **✅ 対応済み(2026-07-12)** | 連続safeバッチ並列（Semaphore 10）＋非safe直列 |
| 5 | Bash timeout単位 | **✅ 対応済み(2026-07-12)** | 3600超はms解釈＋description明記 |
| 6 | Bash `run_in_background` | 未対応 | 長時間コマンド対策。次候補 |
| 7 | dangerousPatterns系 | **✅ 実質対応(2026-07-12)** | permission port（deny/askルール）が上位互換で吸収 |
| ~~8~~ | ~~DELIB_DISTILL_ENABLED確認~~ | ✅ 確認済み・有効 | §3.2に反映済み |

**追記（2026-07-12）**: free-codeのpermission ask実装を移植。`permissions.py`（ルールエンジン: deny/ask/allow、本家settings.json書式互換、複合コマンドのセグメント照合）＋`ask_bridge.py`（ContextVarでリクエストスコープ受け渡し）＋`permissions.json`（ホットリロード対応ルール、Git安全ルールをdenyに機械化）＋bot.pyのDiscordリアクション承認UI（✅/❌・120秒タイムアウト拒否）。tool_loopはasync gate対応。スモークテスト21項目全緑。

見送り（意図的差分として維持）: sandbox不採用／ask型permission／WebFetch・WebSearchのMCP代替／Read結果の行番号なし（Edit精度とのトレードオフで現状維持、offset追加時に再検討）／Team・Workflow系。

---
*棚卸しの詳細（両側のfile:line全リスト）は本監査の作成過程で採取済み。個別の移植に着手する際は該当セクションのfile:lineから本家実装を参照のこと。*

---

## compaction（コンテクストクリア）機構のfree-code同一化（2026-07-12）

調査で判明: botのcompactionは既に本家CLI移植ベース。機構だけ同一化し、Discord向けプロンプト拡張（金融数値verbatim・日本語要約・11節構成）と独自安全装置（Opus 4.8要約・バックグラウンド実行・30分クールダウン/rapid-refillブレーカー・RECOVERY_DIRECTIVE）は先輩判断で維持。

| 領域 | 本家 | bot旧 | 同一化後 |
|---|---|---|---|
| 発動閾値式 | window−min(max_out,20K)−13K（1M窓で967K・autoCompact.ts:71-92） | window−CLAUDE_MAX(64K)−45K≈891K | **本家式（1M→967K/200K→167K）**。RESERVED_SUMMARY_TOKENS/AUTOCOMPACT_BUFFER_TOKENSを新設 |
| in-loop proactive閾値 | boundaryと同一閾値・毎ターン | 97%(970K)・1req1回 | **boundary式へ委譲**（proactive_hard_threshold_tokens→compact_threshold_tokens）。1req1回capとrapid-refillブレーカーは安全装置として維持 |
| 要約後の保持 | フルcompactはverbatim tail残さない | 50K chars tail | **KEEP_CHARS既定0**（re-anchorが並行append分をtailに含めるので会話は失われない）。verbatim行はtail有時のみ |
| microcompact | gap60分/keep5/COMPACTABLE_TOOLSのみ | gap30分/keep3/全tool_result | **gap60/keep5/COMPACTABLE_TOOLSフィルタ**。緊急(boundary背景中)のみ全対象=bot拡張 |
| 手動/compact指示 | /compact <指示>→Additional Instructions注入 | /compact有・指示非対応 | **focus引数追加**（custom_instructions注入・手動はauto_continue=False） |
| 継続文言 | auto時「Continue the conversation...」(prompt.ts:361) | 無し | **auto時のみ原文追加**（wrap_summary auto_continue） |
| 要約リクエスト | thinking disabled（compact.ts:1329） | adaptive+effort medium(4.4-8.2分) | **thinking disabled・output_config削除・max_tokens 48K→20K**（実測: end_turn/出力716tok/数秒） |
| PTLリトライ | 古い側切って3回(compact.ts:262) | 事前600K head-cutのみ | **PTL/413で入力半減×3回リトライ追加** |

意図的残差（bot環境で本家と揃えられない）: post-compactファイル復元（channel横断readFileStateが無い）、cached-MC/サーバサイドcontext editing（cache-editing API・ant-only）、_flatten_rows_for_summary（JSONLストレージ形式依存）。

検証: 単体19項目全緑（閾値式・microcompactフィルタ3モード・wrap文言・custom注入）。Opus実弾1回=thinking disabledでend_turn・summary出力・金融数値1570保持を確認。全変更env上書き可でロールバック容易。
