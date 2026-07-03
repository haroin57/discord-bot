# Deep Research モード — 機能要件・設計ドキュメント

- Status: draft (2026-06-24)
- 対象: discord-bot（佐藤かなえ）への Deep Research モード追加
- 関連: ADR-0028（起草予定）、`services/claude_tag.py`（スレッド分離パターンの先行実装）

## 概要

discord-bot は Claude Code ではなく、自前 harness で Anthropic Messages API を回す Python bot である。本ドキュメントは、ChatGPT Deep Research / Anthropic Claude Research / Claude Code CLI の multi-agent パターンに寄せた「Deep Research モード」（多段の自律的 Web 調査 → ソース読込 → 検証 → 引用付きレポート生成）の **機能要件と実装設計** を定義する。主眼は機能要件の詳細化。

実装は段階導入とする：まず単一エージェント反復型で End-to-End を通し（低リスク・約4xトークン）、その後 CLI 式 multi-agent（AgentDefinition レジストリ + 独立コンテキストの並列サブエージェント、約15xトークン）へ昇格する。配線（thread / job / 出力 / config）は両者で共有する。

## 実装状況（2026-06-24）

- **Phase 0（単一エージェント + 必須明確化）と Phase 1（CLI 式 multi-agent: planner → 並列 searcher → synthesizer）は実装済み**。`DEEP_RESEARCH_ENABLED=off` の裏で merge 済み。`DEEP_RESEARCH_MULTI_AGENT`（default true）で Phase 0/1 を切替、plan 失敗時は単一エージェントへ自動フォールバック。
- **設計からの逸脱（ADR-0028 に記録）**: 当初予定した `llm.py`（subscriber-shaped `research_create` 自作）は作らず、サブエージェントは **`run_agent_loop`（tool_loop.py）を直接呼ぶ**方式に変更した。これにより (1) `claude_semaphore` を取るのは entry.py 側なので直呼びで回避、(2) session/JSONL も entry.py 側なので `history=[]` 直呼びで構造的にセッション非汚染、(3) subscriber-shape（system 4-block / betas / output_config top-level / context_management / cch hook）を再実装せず再利用、が一度に満たされる。下記「モジュール構成」表の `llm.py` 行は未実装（不要化）。
- 実ファイル: `services/deep_research/{__init__,types,sources,prompts,registry,orchestrator}.py`。テスト: `tests/test_deep_research.py`（24 件、回帰込み 124 passed）。
- searcher のデフォルトモデルは `claude-sonnet-4-6`（lead/synthesizer は Opus）。subscription で Sonnet 4.6 が無効な場合は `DEEP_RESEARCH_SEARCHER_MODEL` を上書き。Haiku は adaptive-thinking で HTTP400 になるため使わない。全 searcher が失敗した場合は **lead model で1回自動リトライ**（searcher モデル無効の自己回復）。
- **Phase 2（citation 厳密化）/ Phase 3（堅牢化）も実装済み**: synthesizer は本文 + `[S#]` インライン引用のみ書き、**情報ソース一覧は機械収集分から決定的に付与**（URL 捏造を構造排除）。本文の `[S#]` を実在ソースと機械照合し、未対応引用に ⚠️ 注記。予算（時間 / トークン）超過で synthesizer をスキップして要約版に degrade。synthesizer の transient 失敗は1回リトライ。テスト 32 件（回帰込み 132 passed）。
- `DEEP_RESEARCH_ENABLED=true` で有効化済み。実機での End-to-End 検証（実際の `/research`）は要。

## レビュー反映（2026-06-24, ChatGPT レビュー → 実コードで裏取り済み）

外部レビューの指摘を実コードで検証し、妥当だった 6 点を本書に反映した（指摘の行番号はいずれも正確だった）。

1. **[高] 共有 MCP session の並行呼び出しガード**: `McpPool.call_tool`（`services/claude_api/mcp_client.py:356`）は spawn ロック（`_spawn_locks`）しか持たず、**ツール呼び出し自体には lock も timeout も無い**。並列 searcher が同一 server の stdio session を同時に叩くと protocol 混線・ハング・キャンセル不能になる。→ deep_research 側で **server-key 単位の `asyncio.Lock` レジストリ**を持ち、`pool.call_tool` を `async with server_lock[server]:` + `asyncio.wait_for(TOOL_CALL_TIMEOUT_S)` でラップ（`_guarded_call_tool`）。FR-4 / アーキテクチャ / 失敗モードに反映。
2. **[高] スラッシュコマンドのスレッド起点**: `claude_tag` は `msg.create_thread()`（`services/claude_tag.py:271`）= 既存メッセージ起点。スラッシュには起点メッセージが無く、ephemeral 応答にもスレッドは作れない。→ `defer(ephemeral=True)` 後、`interaction.channel.send(...)` で **非 ephemeral の起点メッセージ**を投稿し、その `origin.create_thread(name=...)` でスレッドを立てる。FR-1 に固定。
3. **[中] /abort のスコープ**: `abort_cmd`（`bot.py:703`）は `interaction.channel_id` を `cancel_channel_jobs`（`services/job_state.py:83`）に渡す。research job を `thread.id` で登録するため、**/abort は研究スレッド内で実行**する必要がある（親チャンネルからは止まらない）。FR-10 に明記。
4. **[中] 明確化の必須要件と段階の矛盾**: 明確化は必須（FR-2）だが旧 Phase 2 だった。→ **明確化を Phase 0 に前倒し**。`DEEP_RESEARCH_ENABLED` は Phase 3 完了まで OFF（内部検証のみ）とし、「出荷可能」=「OFF フラグ裏で merge 可能」と定義。`skip_clarify` は admin 用エスケープハッチ（default false）。
5. **[中] Web バックエンドの可用性**: brave / tavily は env key がある時だけ `MCP_SERVERS_WEB` に入る（`config.py:668-695`）。x-scraper は無条件だが X 専用で一般 Web 検索にならない。→ **/research の前提条件**として brave または tavily の少なくとも一方が利用可能かを検査し、無ければ拒否（on_ready でも warning）。FR-1 / FR-5 に反映。
6. **[補] research_create の request shape**: 既存の streaming agent loop（`services/claude_api/tool_loop.py:1108-1129`）に寄せる。`output_config` は **top-level kwarg**（extra_body に入れると SDK 0.88 が落とす → effort 消失 → refusal）、`context_management` は `extra_body`、`betas` / `metadata` / `thinking` / opus 判定の max_tokens も踏襲。→ `llm.py` の雛形を compaction.py ではなく tool_loop.py に変更。

## 前提調査で確定した事実

### 公式 CLI バイナリの正体（重要・事実訂正）

`/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe`（237MB ELF, v2.1.170）は **Rust ネイティブバイナリではなく、Bun v1.3.14 でコンパイルされた single-file executable**（JavaScript/TS バンドルを埋め込んだもの）である。`rustc` / `cargo` / `tokio` 文字列は同梱されている **ripgrep（BurntSushi、Rust 製）** に由来する。multi-agent ロジックは埋め込み JS 側にあり、`strings` / `grep -a` による文字列抽出で構造を完全に取得した。型定義 `sdk-tools.d.ts`（同梱、平文）でも裏取り済み。

### CLI の multi-agent 構造（claude.exe + sdk-tools.d.ts から抽出 → 本設計に反映）

- **Agent tool 入力**: `{description, prompt, subagent_type?, model?, run_in_background?, isolation?, cwd?}`。`subagent_type` で **AgentDefinition レジストリ** から定義を解決（default `general-purpose`）。
- **AgentDefinition**: `{agentType, description, model, permissionMode, isolation, background, requiredMcpServers, getSystemPrompt({toolUseContext})}` の宣言的定義。
- サブエージェントは **ゼロコンテキストで起動**（埋め込みガイダンス: "brief like a smart colleague who just walked into the room"）。
- 結果は **最終メッセージのみ親に返す**（`maxResultSizeChars = 100,000`）。親コンテキストを汚さない。
- **再帰禁止**（`subagent_recursive_fork`）。model override 優先順位 = tool param > definition frontmatter > 親 inherit > main loop default（enum `[sonnet, opus, haiku, fable]`）。`requiredMcpServers`（最大30s 待機）。`run_in_background`（2秒で自動 background 化）。`isolation: "worktree"`（git worktree でコード編集を分離 — 研究では不要）。
- 付随: 非同期タスク管理（TaskCreate / TaskGet / TaskUpdate / TaskList / TaskOutput / TaskStop、依存 blocks/blockedBy 付き）、`EnterWorktree` / `ExitWorktree`、`SendMessage`（稼働中エージェントを name で addressing）。

### harness の制約（裏取り済み）

- 全 `call_with_tools` は `claude_semaphore = asyncio.Semaphore(CLAUDE_MAX_PARALLEL=2)` を通る（`services/claude_api/entry.py:181, :280`）。
- 一方 `api_client.beta.messages.create / stream`（`services/claude_api/compaction.py:503` の subscriber-shaped 形）は **この semaphore を通らない**。サブエージェントはこの素の create で実装し、専用 semaphore で律速する。
- MCP は `McpPool` を共有可能（`services/claude_api/mcp_client.py`）。8GB RAM のため **playwright（chromium ~500MB）は研究から除外**。

### 確定した設計判断

- **起動**: `/research <query>` スラッシュコマンド（admin only）。
- **明確化**: **常にスレッドで1回**、明確化質問を挟む（ChatGPT Deep Research 式）。
- **段階導入**: まず単一エージェント反復型 → CLI 式 multi-agent へ昇格（配線は共有）。

---

## 機能要件（詳細）

凡例: FR = 機能要件。各 FR に ChatGPT(CG) / Claude multi-agent(CL) / 公式CLI(CLI) の対応根拠を付す。

### FR-1 起動と入力
- `/research query:<str> [skip_clarify:bool=false] [depth:enum(quick|standard|deep)=standard]`。admin only（`is_admin`）。`DEEP_RESEARCH_ENABLED=false` 時は ephemeral で拒否。
- **前提条件**: 一般 Web 検索のため brave-search または tavily の少なくとも一方が `MCP_SERVERS_WEB` に在ること（env key 依存、`config.py:668-695`）。いずれも無ければ /research は拒否（x-scraper 単体は X 専用で一般 Web 検索にならない）。
- **スレッド起点**: `interaction.response.defer(ephemeral=True)`（3秒制限回避）→ `interaction.channel.send(...)` で **非 ephemeral の起点メッセージ**（例「🔬 Deep Research: <query>」）を投稿 → その `origin.create_thread(name=...)` で専用スレッドを立てる。ephemeral 応答や他者メッセージにはスレッドを張れないため、`claude_tag` の `msg.create_thread`（既存メッセージ起点）とは起点が異なる。以降の進捗・成果物は **すべてスレッド内** に出す。
- `depth` は予算プリセット（quick=2サブq / standard=4 / deep=5。検索深度・時間上限を連動）。

### FR-2 明確化フェーズ（必須）
- スレッド作成直後、軽量モデルで「この依頼に答えるため要確認の2-3問」を生成しスレッドに投稿。`bot.wait_for("message", check=同スレッド&同ユーザー, timeout=DEEP_RESEARCH_CLARIFY_TIMEOUT_S=180)` で **1回だけ** 回答待ち。
- 回答は `clarified_query` に統合。タイムアウト / `skip_clarify` 時は元 query で続行し「明確化スキップ」と明示。(CG: clarifying questions / CLI: zero-context briefing)

### FR-3 計画立案（task decomposition）
- lead が `clarified_query` を **3〜5 個の自己完結サブ質問** に分解（`DEEP_RESEARCH_MAX_SUBAGENTS`）。各サブ質問は独立に調査可能な単位。依存があれば順序付け。(CG: planning / CL: lead decomposition / CLI: AgentDefinition spawn)
- 計画はスレッドに要約表示（v1 では閲覧のみ。編集 UI は作らない）。

### FR-4 並列探索（subagent fan-out）
- 各サブ質問に **searcher サブエージェント** を割当て、**独立コンテキスト** で並列実行。各 searcher は Web 検索ツール（brave / x-scraper / tavily）のみを持つ fresh-context エージェント。(CL: parallel subagents, isolated context / CLI: fresh zero-context subagent)
- searcher は「賢い同僚へのブリーフィング」形式の自己完結プロンプトで起動（会話履歴は渡さない）。内部で最大 `DEEP_RESEARCH_MAX_SEARCHES_PER_AGENT=4` 回の検索/読込ループ。
- **実並列度は専用 `Semaphore(DEEP_RESEARCH_MAX_PARALLEL=2)` で制御**（subscriber バケット保護）。`gather` で全 searcher を起こし、内側 semaphore で実走 2〜3。

### FR-5 ソース評価・取得
- 検索は snippet ベース + 必要に応じ tavily の本文抽出。playwright（chromium）は **使わない**（RAM / キャンセル不能リスク）。
- searcher に渡すツールは **Web + X + 金融 + 学術 を横断**（`DEEP_RESEARCH_MCP_KEYS`: brave-search / tavily / x-scraper / nikkei-indexes / yfj-quote / polymarket / scholarly のうち env で有効なもの）。`static_mcp_schemas.json` に在るもの（brave/x-scraper/nikkei/yfj/polymarket）は **静的広告 + lazy spawn**（searcher が実際に呼んだ時だけ起動して RAM 節約）、無いもの（tavily/scholarly）だけ eager spawn。brave/tavily がゼロなら起動前に拒否（FR-1 前提条件）。playwright(RAM)/mem0(個人記憶)/realestate(admin専用) は除外。LLM はサブ質問のドメインに応じてツールを選ぶ（プロンプトに使い分けガイド）。
- 多ソース裏取り基準（news-research 規範）: 2ソース以上で確認 → 事実、単一ソース → ⚠️ 明記。認証済みメディアは engagement 不問、個人未確認速報は低 engagement 除外。

### FR-6 検証（verification）
- **機械検証を主** とする（汎用 verification subagent は失敗しやすい — CL の知見）。各主張に紐づく source id（sid）が実在ソースに対応するかを `verify_citations` で機械照合。未対応の主張は ⚠️ または synthesizer に差し戻し。
- 「明確な成功基準」での検証（例: 各キー主張に 2+ sid）を synthesizer プロンプトで強制。

### FR-7 引用付き synthesis
- 全 searcher の最終要約 + `SourceRef` 一覧を **synthesizer（Opus, stream）** に渡し、構造化レポート生成。本文に **`[S3]` インライン引用**、末尾に `## 情報ソース`（`[S3] title — URL`）。(CG: inline citations + annotations / CL: citation agent / news-research Phase 3-4)
- レポート構造: タイトル → 3-5 カテゴリ（各3-5要点）→ 総合評価 → ソース一覧。数値は具体値で記述。

### FR-8 進捗表示
- スレッドの status anchor を **フェーズ単位** で throttle 編集（`🧭 計画中 → 🔍 探索 2/4・ソース18件 → 🧪 検証中 → 📝 執筆中`）。個別 searcher の tool 呼び出しは出さない（Discord rate limit 回避）。(CG: progress / CL: external memory checkpoints)

### FR-9 出力
- 最終レポートは `_send_segmented(anchor, report, [])` でスレッドに投下（2000字分割・table/math 画像化は既存パイプが処理）。超長文は `.md` ファイル添付も可。anchor を `✅ 調査完了` に。

### FR-10 予算・上限・キャンセル
- `DEEP_RESEARCH_TOTAL_TIME_BUDGET_S`（既定1200=20分）/ `DEEP_RESEARCH_TOTAL_TOKEN_BUDGET`（累積 input+output）/ `MAX_SUBAGENTS` / `MAX_SEARCHES_PER_AGENT`。超過時は **中断ではなく、そこまでの findings で synthesis に degrade**。
- `/abort` で中断（job は `thread.id` をキーに `JobState` 登録）。`abort_cmd`（`bot.py:703`）は `interaction.channel_id` を見るため、**/abort は研究スレッド内で実行**すること（親チャンネルからは止まらない）。CLI の `run_in_background` / `TaskStop` に相当。任意拡張: 親チャンネルからも止めたい場合は job を親 channel_id でも引けるよう二重登録 or thread→親の逆引きを追加。
- **再帰禁止**（CLI 準拠）: searcher は subagent を spawn できない（spawn ツールを与えない）。

### FR-11 状態分離（セッション非汚染）
- Deep Research は `call_with_tools` / `sessions` / JSONL を **一切使わない**。中間状態（plan / findings / sources / budget）は orchestrator のメモリ（`ResearchState`）に保持。巨大 tool_result が通常チャットのセッションを汚染しない（`claude_tag` のスレッド分離より強い、構造的分離）。

### FR-12 モデル使い分け（CLI の override 優先順位を踏襲）
- lead（計画）+ synthesizer = `DEEP_RESEARCH_LEAD_MODEL`（Opus 4.8, effort=high）。searcher = `DEEP_RESEARCH_SEARCHER_MODEL`（Sonnet）。clarify 判定 = Haiku/Sonnet。tool param > definition > 親 inherit の優先順位を `run_subagent` で実装。

---

## アーキテクチャ（CLI の AgentDefinition を骨格に採用）

CLI の「宣言的 AgentDefinition レジストリ + fresh-context subagent + bounded result」をこの harness に移植する。サブエージェントは `api_client.beta.messages.create / stream`（compaction.py:503 の subscriber-shaped 形）の **自前ミニループ** で実装し、`claude_semaphore` を通さず専用 semaphore で律速する。

```
/research → thread + anchor + JobState(thread.id)            [bot.py]
  → run_deep_research(orchestrator)                          [services/deep_research/]
     0. 前提条件チェック: brave-search または tavily が利用可能か。無ければスレッドに拒否を出して終了
     1. make_api_client(restricted=False) ×1
        + AsyncExitStack + McpPool(brave/tavily/x-scraper のうち有効なもの) eager spawn  (playwright除外)
        + server_locks: dict[str, asyncio.Lock]  (同一 server への並行呼び出し直列化)
     2. [FR-2] clarify: clarify-judge agent → 質問 → bot.wait_for(timeout)
     3. [FR-3] plan: lead(Opus) → 3-5 subqueries
     4. [FR-4] gather(run_subagent(SEARCHER_DEF, briefing=subq) for subq in plan)
               under Semaphore(MAX_PARALLEL); 各 searcher は fresh context, web tools only
     5. [FR-5/6] parse_sources(機械抽出, sid採番, dedup) + verify_citations(機械照合)
     6. [FR-7] run_subagent(SYNTHESIZER_DEF, stream=True) → 引用付きレポート
     7. [FR-9] _send_segmented(anchor, report) ; anchor→✅
     finally: stack.aclose()(MCP SIGTERM) + kill_orphan_mcp_processes ; unregister_job
```

**AgentDefinition（CLI 構造の移植）** — `services/deep_research/registry.py`:

```python
@dataclass
class ResearchAgentDef:
    agent_type: str                  # "planner" | "searcher" | "synthesizer" | "clarifier"
    model: str                       # CLI: definition frontmatter model
    effort: str                      # "high" | "medium"
    system_prompt: str               # CLI: getSystemPrompt()。news-research レシピを転記
    mcp_tool_keys: list[str]         # 許可 MCP ツール(searcher のみ web)。CLI: permissionMode 絞り込み
    required_mcp: list[str]          # CLI: requiredMcpServers(spawn 前に pool 準備待ち)
    max_tool_turns: int              # searcher の検索ループ上限
    stream: bool                     # synthesizer=True(heartbeat 必須)
```

- 汎用 `async def run_subagent(api_client, pool, defn, briefing, state, *, server_locks, model_override=None, watchdog) -> SubAgentResult`: fresh messages で create ループ。tool_use → **`_guarded_call_tool(pool, server_locks, name, args)`**（`McpPool.call_tool`(mcp_client.py:356) は spawn ロックしか持たないため、deep_research 側が **server-key 単位 `asyncio.Lock`** を1枚かけ、さらに `asyncio.wait_for(TOOL_CALL_TIMEOUT_S)` で包む。並列 searcher が同一 server session を同時に叩いても混線・ハングしない）→ `parse_sources` で `state.sources` 登録 → tool_result 追記 → end_turn で **bounded な最終要約**（maxResultSizeChars 相当のキャップ）。**親へは最終要約のみ** 返す（CLI 準拠）。
- `server_locks: dict[str, asyncio.Lock]` は orchestrator が1個生成し、全 searcher で共有する（同一 server への直列化が目的）。

---

## モジュール構成 `services/deep_research/`

| ファイル | 責務 |
|---|---|
| `types.py` | `SourceRef(sid,title,url,source_type,snippet)`, `SubAgentResult(subq,summary_md,cited_sids,ok,error)`, `ResearchState(query,clarified_query,plan,sources,results,started_at,tokens_used,phase)` |
| `llm.py` | subscriber-shaped `research_create(api_client,*,model,system_text,messages,tools,effort,stream,watchdog)`。**request shape は `tool_loop.py:1108-1129` に準拠**: `output_config` は **top-level kwarg**（extra_body に入れると SDK 0.88 が落とし effort 消失→refusal）、`context_management` は `extra_body`、`betas`/`metadata`/`thinking`/opus 判定 max_tokens も踏襲。system 4-block prefix を一元付与（欠けると 429）。stream 時 event 毎 `watchdog.heartbeat()` |
| `registry.py` | `ResearchAgentDef` + 各定義（planner/searcher/synthesizer/clarifier）+ `run_subagent()`（CLI 式 fresh-context ループ） |
| `sources.py` | `parse_sources(tool_name, raw_json, state)`（brave/tavily/x-scraper の URL 機械抽出 + sid 採番 + dedup）、`verify_citations(state)` |
| `orchestrator.py` | `DeepResearchDeps`（bot/send_segmented/get_http_session/track_task。**sessions は渡さない**）、`run_deep_research(channel, user, query, deps, *, skip_clarify, depth)`。thread/anchor/job/clarify/plan/fan-out/synthesis/出力/MCP cleanup の配線 |

Phase 0（単一エージェント版）は `orchestrator.py` 内で「1つの大きな searcher 定義に全レシピを載せて1回 `run_subagent`」で実現し、Phase 1 で planner / 並列 searcher / synthesizer に分割する（配線・types・llm・sources は共通）。

### bot.py への追加（既存パターン準拠、on_message 非改変）
- `from services import deep_research`。`config` から `DEEP_RESEARCH_*`。
- `@bot.tree.command(name="research", ...)` + `@app_commands.describe(...)`（`/abort` 等の既存スラッシュと同形）。admin gate → `defer(ephemeral)` → `DeepResearchDeps` 構築 → `_track_task(deep_research.run_deep_research(...), name="deep_research")`。
- `/abort` は **研究スレッド内で実行すれば** 無改修で有効（job は `thread.id` 登録、`abort_cmd` は `interaction.channel_id` を見る）。親チャンネルからの abort は対象外（FR-10）。

### config.py 追加（`STREAM_*` 近傍、env override）
`DEEP_RESEARCH_ENABLED`（default off。user 向け有効化は Phase 3 後）/ `_MAX_PARALLEL=2` / `_MAX_SUBAGENTS=4` / `_MAX_SEARCHES_PER_AGENT=4` / `_TOTAL_TIME_BUDGET_S=1200` / `_TOTAL_TOKEN_BUDGET=2_000_000` / `_CLARIFY_TIMEOUT_S=180` / `_LEAD_MODEL`(=CLAUDE_MODEL) / `_SEARCHER_MODEL`(Sonnet) / `_SEARCHER_IDLE_TIMEOUT_S=240` / `_SYNTH_IDLE_TIMEOUT_S=600`。

起動時チェック: `DEEP_RESEARCH_ENABLED` かつ brave/tavily いずれも無効なら on_ready で warning（`/research` は実行時に前提条件で拒否）。

---

## 再利用する既存資産

| 用途 | 参照 |
|---|---|
| subscriber-shaped streaming create の雛形（request shape の正本） | `services/claude_api/tool_loop.py:1108-1129`（`output_config` top-level + `extra_body.context_management`）。非 stream の単発 create は `compaction.py:487-512` |
| system 4-block / metadata / betas / thinking | `services/claude_api/client.py`（`get_attribution_header_value` / `CLI_IDENTITY_TEXT` / `CLI_DEFAULT_SYSTEM_PROMPT` / `get_metadata` / `MODEL_BETAS` / `THINKING_CONFIG`） |
| 複数 API client（OAuth） | `services/claude_api/client_factory.py:make_api_client(restricted=False)` |
| MCP プール / spawn / dispatch / 孤児掃除 | `services/claude_api/mcp_client.py`（`McpPool` / `mcp_tools_to_anthropic` / `_dispatch_tool_call` / `kill_orphan_mcp_processes`） |
| フェーズ別 idle 監視 | `services/claude_api/stall.py:StallWatchdog(idle_timeout_s=...)` |
| thread / anchor / status / 出力 | `services/claude_tag.py`（`_ensure_thread` / throttled `_safe_edit`）、`bot.py:_send_segmented`（:567） |
| ジョブ登録 / 中断、fire-and-forget | `services/job_state.py`、`bot.py:_track_task`（:284） |
| MCP web ツール名 / スキーマ | `services/claude_api/static_mcp_schemas.json`、`config.py:MCP_SERVERS_WEB` |
| 調査レシピ（キーワード設計 / 裏取り / レポート構造） | `~/.claude/skills/news-research/SKILL.md`（planner/searcher/synthesizer の system へ転記） |

---

## 想定失敗モードと対策（壊れ方まで設計）

- **idle timeout 誤発火**（StallWatchdog 300s, stall.py）: フェーズ別 watchdog（searcher 240 / synth 600）+ synthesis は **stream で heartbeat**。non-stream 禁止。
- **subscriber バケット枯渇 / 429 がメイン応答へ波及**: create は `claude_semaphore` を通さない代わり、専用 `Semaphore(MAX_PARALLEL=2)` で **必ず** 律速 + 全体トークン上限。searcher は指数バックオフ retry、失敗は `ok=False` で部分成功 synthesis。
- **MCP RAM 爆発**: 1プール共有、eager は brave/x/tavily のみ、**playwright 完全除外**。
- **MCP 並行 session 混線 / ハング**: `McpPool.call_tool`（mcp_client.py:356）は呼び出しロック無し。並列 searcher は **server-key 単位 `asyncio.Lock` + `asyncio.wait_for(TOOL_CALL_TIMEOUT_S)`**（`_guarded_call_tool`）で同一 server への同時呼び出しを直列化。重い / 未知 MCP を足さない。全体時間上限で degrade（onion-search 前例）。
- **Web バックエンド不在**: brave / tavily が env 未設定だと一般 Web 検索不能。`/research` 前提条件で brave or tavily の可用性を検査し、無ければ起動拒否 + on_ready warning（FR-1）。
- **citation 欠落 / 捏造**: 機械抽出が一次（parse_sources）、LLM は sid 参照のみ、`verify_citations` で機械照合。
- **Discord rate limit**: 進捗は phase + カウントのみ、1.5s throttle。最終は `_send_segmented`。
- **JSONL セッション汚染**: `call_with_tools` / `sessions` / JSONL を一切通さない（FR-11）。
- **clarify 無応答**: `CLARIFY_TIMEOUT_S` で打ち切り、既定スコープ続行。
- **/abort 時 MCP リーク**: `finally` で `stack.aclose()` + `kill_orphan_mcp_processes`。`CancelledError` は finally 後に再 raise。
- **Haiku で adaptive thinking 400**: searcher は Sonnet 固定（Haiku 使用時のみ `thinking` を外す分岐）。

---

## 段階的実装順序

- **Phase 0 — 単一エージェント + 必須明確化で End-to-End**: config（`ENABLED=off`）→ `bot.py` `/research` + 非 ephemeral 起点メッセージ + thread/anchor/job + Web バックエンド前提条件チェック → `types.py` / `llm.py`（tool_loop 準拠 shape）/ `sources.py` → **FR-2 明確化（`bot.wait_for` + timeout）** → orchestrator が「全レシピ載せ 1 searcher を1回 `run_subagent`」→ `_send_segmented` 出力。**「明確化 → 引用付きレポートがスレッドに出る」を確認**。明確化は必須要件のため Phase 0 に含める。
- **Phase 1 — CLI 式 multi-agent**: `registry.py`（AgentDefinition + `run_subagent` 汎用化 + `_guarded_call_tool` + 共有 `server_locks`）→ planner(Opus) → 並列 searcher（専用 semaphore, 共有 pool）→ synthesizer(Opus stream)。orchestrator を multi-agent に差し替え。
- **Phase 2 — citation 強化**: `verify_citations`（sid 機械照合）、レポート末尾ソース一覧、単一ソース ⚠️ 注記。
- **Phase 3 — 堅牢化**: フェーズ別 watchdog + stream heartbeat、time/token budget degrade、429 retry / 部分成功、`/abort` の MCP cleanup 負荷試験、進捗 throttle 仕上げ。

各 Phase は `DEEP_RESEARCH_ENABLED=off` の裏で merge 可能（=「出荷可能」の意味）。**user 向け有効化（`ENABLED=on`）は Phase 3 完了後**。これにより「明確化必須」要件が、機能が有効なときは常に満たされる。

---

## 検証（end-to-end）

- **自動テスト** `tests/test_deep_research.py`: `parse_sources`（brave/tavily/x-scraper JSON → SourceRef + dedup）、`verify_citations`（欠落 sid 検出）、`run_subagent`（`research_create` を patch し fresh-context・bounded result・再帰なしを検証）、orchestrator happy path（`make_api_client` / `McpPool` / `research_create` を patch し、thread 生成・`_send_segmented` 呼び出し・JSONL/sessions 不使用を assert）。`bot.wait_for` をモックし clarify timeout 分岐。
- **手動**: admin が `/research query:"日経VIの平均回帰戦略の現状"` → スレッド生成 → 明確化質問 → 回答 → 計画表示 → 探索進捗 → 引用付きレポート。`/abort` で中断 → MCP プロセス残留なし（`pgrep -f mcp` 確認）。通常チャンネルの JSONL が無変更であることを確認（セッション分離）。
- **コスト計測**: 1回の `/research` の累積トークン（`ResearchState.tokens_used`）をログ出力し、約4x（Phase0）/ 約15x（Phase1）の想定と突合。

---

## 主要変更ファイル

- `services/deep_research/{types,llm,registry,sources,orchestrator}.py`（新規）
- `bot.py`（`/research` スラッシュ + import。on_message 非改変）
- `config.py`（`DEEP_RESEARCH_*`）+ `.env.example`
- `tests/test_deep_research.py`（新規）
- `.claude/adr/0028-deep-research-mode.md`（新規）
