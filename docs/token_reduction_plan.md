# Claude API Token使用量最適化計画

最終更新: 2026-05-15 (実装適用済み)
対象ブランチ: refactor/tier-m
対象経路: `CLAUDE_BACKEND=api` (現行デフォルト)

## Summary

Claude API経路のtoken使用量を削減し、削減効果をusageログで観測可能にする。
SDK/CLI rollback経路 (`services/claude_sdk.py`) と `CLI_DEFAULT_SYSTEM_PROMPT`
は変更しない。CLIのmitmproxy捕捉に基づくbyte-shape-equal送信形態を維持する。

本計画の項目は2026-05-15に実装適用済み。未解決競合がある `web_api.py`,
`tests/test_bot.py`, `tests/test_web_api.py` は本計画の範囲外。

## 想定削減効果

| 項目 | warm時の概算 | cold時の概算 |
| --- | --- | --- |
| JSONL compact化 (full prompt → raw_user_text + context_snapshot) | -2000〜-8000 tokens/turn | 同左 |
| CLAUDE.md最適化 (グローバル+プロジェクト計約4000バイト削減) | キャッシュ作成は1回のみ | -1300 tokens (初回) |
| Mem0 smart search抑制 | Haiku呼び出し最大4回 → 0回 | 同左 |
| kanae_state 5分丸め | キャッシュ境界の安定化で~270 tokens/turn削減 | キャッシュ作成1回追加 |
| Mem0 smart search max_tokens削減 | smart search発火turnのみ-1500 tokens (output予約) | 同左 |
| Mem0 auto-record throttle | 短文連投時のHaiku ingestion呼び出しを抑制 | 同左 |

usageログ追加で実測可能になる。

## Implementation Changes (適用済み)

### A. usageログ追加

ファイル: [services/claude_api/tool_loop.py](services/claude_api/tool_loop.py)

各API turnで `input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, `output_tokens`, `stop_reason` をINFOで記録する。
tool use複数turnの場合は最後にrequest単位合計も記録する。`usage` がない応答や
mockではログを省略し処理は継続する。

新規ヘルパー: `_USAGE_FIELDS`, `_usage_number`, `_log_turn_usage`,
`_log_total_usage`。`run_agent_loop` 内で `usage_totals` をlocal stateとして保持。

### B. JSONL compact保存

ファイル:
- [services/session.py](services/session.py)
- [services/claude_api/entry.py](services/claude_api/entry.py)
- [config.py](config.py)

- user contentにはfull promptではなく `raw_user_text` のcompact版だけを保存する。
- `<retrieved_memories>`, `<kanae_self_memory>`, `<kanae_state>`, trusted header,
  untrusted wrapperは通常contentから除外する。
- 既定上限はuser 2,000文字、assistant 3,000文字 (環境変数で上書き可)。
- Mem0文脈は別フィールド `context_snapshot` に保存する。`<untrusted_user_input>`
  より前にある `<retrieved_memories>` と `<kanae_self_memory>` だけを抽出し、
  合計2,000文字上限。`<kanae_state>` と `<kanae_daily>` は保存対象外。
- API replay専用loader `load_api_replay_channel()` を追加し、tail内の最新
  `context_snapshot` だけを1回materializeする。
- 通常の `load_history_channel()` は変更しない (Web履歴表示互換のため)。
- short follow-upで現在turnに新しいMem0 blockがない場合、直近snapshotを
  保存時に引き継ぐ (`build_compact_history_entries`)。

### C. Mem0 smart search条件化

ファイル:
- [services/mem0/search.py](services/mem0/search.py)
- [services/mem0/__init__.py](services/mem0/__init__.py)
- [bot.py](bot.py)

- `should_use_memory_llm_mode(query, *, enabled)` を `search.py` に追加。
  `search_memories_smart` のシグネチャと既定値は変更しない。
- 通常発話は `rewrite=False, rerank=False` で呼ぶ。
- 「前に話した」「覚えてる」「記憶」「思い出」「remember」「recall」など
  明示的な記憶検索意図がある時のみ `rewrite=True, rerank=True`。
- 通常記憶検索とkanae自己記憶検索の両方に同じmodeを渡す。
- ベクトル検索、alias expansion、graph expansionは常時実行する。

### D. (撤回) builtin tools条件付き広告

2026-05-15に一度導入したが、フォローアップ会話で `_needs_builtin_tools` が
頻繁に false 判定して tool を消失させ admin の hot path を壊したため撤回。
詳細は「採用しなかった候補と理由」を参照。adminへは常時 builtin tools を
advertise する元の挙動に戻した。restricted は引き続き tools なし。

### E. CLAUDE.md最適化

ファイル:
- `~/.claude/CLAUDE.md` (グローバル, 13,875 → 9,338バイト)
- `/home/ubuntu/discord-bot/CLAUDE.md` (プロジェクト, 5,935 → 6,442バイト)
- 合計: 19,810 → 15,780バイト (約20%削減)

グローバル削除/移動:
- Why / Map セクション (Claude Code固有のメタ説明)
- Markdown→PDF→Drive ワークフロー
- Token最適化ルール
- 並列Agent運用
- Workflows (Research/Plan/Implement/Review/Record)
- 「babysitしない」「CLAUDE.md短く保つ」 (CLI運用メタ指示)
- 画像・ファイルパスのルール (プロジェクトに統合)
- 感情状態トラッキング (プロジェクトに移動)
- Rules内の連結を「設計・実装ルール」「記憶ルール」に再編

グローバル維持:
- 重要な指示 (忖度禁止、教育目的)
- 行間を読む
- かなえ人格、基本情報
- Git安全ルール
- 設計・実装ルール (Plan Mode + ADR)
- 記憶ルール (Mem0階層化詳細を含む)
- 調べ物ルール
- レートリミット2x時間帯

プロジェクト変更:
- 「Discord特有のキャラクター調整」セクションに重複部分を整理
- 感情状態トラッキングを追加 (グローバルから移動)
- Discord出力ルールはほぼそのまま維持

### F. Mem0 MCP schema常時広告 + lazy spawn

ファイル:
- [services/claude_api/mcp_client.py](services/claude_api/mcp_client.py)
- [services/claude_api/entry.py](services/claude_api/entry.py)
- [services/claude_api/__init__.py](services/claude_api/__init__.py)

- `STATIC_MCP_TOOL_SCHEMAS["mem0"]` を `mcp_client.py` に追加。
  `mcp__mem0__search` と `mcp__mem0__add` の2つ。
- `static_mcp_tools_to_anthropic(keys)` を追加。
- `entry.py` に `ALWAYS_ADVERTISED_MCP_KEYS = {"mem0"}` を定義。
- admin API経路では `static_mcp_tools_to_anthropic(always_advertised_mcp_keys)`
  を必ず `advertised_tools` に合流させる。
- Mem0 MCP processは `pool.spawn_eager()` には含めない (eager_keysでフィルタ)。
- `mcp__mem0__*` がtool_useで呼ばれた時に `McpPool.call_tool()` がlazy spawnする
  (既存実装)。
- `active_mcp_keys` は `chosen_mcp_keys | always_advertised_mcp_keys` で送る。

### G. Mem0自動記録 throttle

ファイル:
- [config.py](config.py)
- [bot.py](bot.py)

- 環境変数 `MEM0_AUTO_RECORD_ENABLED=true`, `MEM0_AUTO_RECORD_INTERVAL_S=300`,
  `MEM0_AUTO_RECORD_MIN_CHARS=80` を追加。
- `_last_mem0_record_at: dict[int, float]` をモジュールレベルに保持。
- `_should_auto_record_mem0(channel_id, user_text)` でゲート判定。
- `_is_force_record_request(user_text)` で「覚えて」「remember this」等の明示
  要求を検出。これは throttle bypass。
- `_mark_mem0_recorded(channel_id)` で記録時刻を更新。
- 通常の `_mem0_store()` と kanae自己記憶 `_kanae_self_store()` の両方を同じ
  ゲートで制御する。

### H. かなえの記憶判断基準

ファイル: `/home/ubuntu/discord-bot/CLAUDE.md`

「長期的に有用な事実だけ記録」ではなく「かなえが興味を持ったこと」軸に変更。
感情的に印象に残ったこと、繰り返し出てくる話題、個人的に意味があること、
将来の会話継続に役立ちそうなことを `mcp__mem0__add` で記録する。明示的に
「覚えて」と言われたこと、ユーザーの好み、進行中のプロジェクト、小さな
個人的詳細も対象。credentials、secrets、「覚えるな」と言われた内容は記録しない。

### I. kanae_state timestampを5分丸め

ファイル:
- [services/emotion/format.py](services/emotion/format.py)
- [config.py](config.py)

- `_format_now_jst()` 内で `now.minute` を `KANAE_STATE_TIME_BUCKET_MIN`
  (既定5) でfloorする。
- 同一bucket内で `<kanae_state>` ブロックがbyte-identicalになり、上流の
  prompt cache境界が安定する。
- 1分単位に戻したい場合は `KANAE_STATE_TIME_BUCKET_MIN=1` を環境変数で指定する。

### J. Mem0 smart search max_tokens削減

ファイル: [services/mem0/search.py](services/mem0/search.py)

- `_rewrite_query` の max_tokens を `MEM0_REWRITE_MAX_TOKENS` (既定500) に下げる。
  出力は `{"queries": [...]}` の小さいJSON。
- `_rerank_memories` の max_tokens を `MEM0_RERANK_MAX_TOKENS` (既定400) に下げる。
  出力は `{"top": [...]}` の小さいJSON。
- 既定2000はoutput予約見積もりが過大。

## Interfaces

新規追加した環境変数:

| 環境変数 | 既定 | 用途 |
| --- | --- | --- |
| `COMPACT_HISTORY_USER_CHARS` | 2000 | JSONL user content上限 |
| `COMPACT_HISTORY_ASSISTANT_CHARS` | 3000 | JSONL assistant content上限 |
| `COMPACT_HISTORY_CONTEXT_SNAPSHOT_CHARS` | 2000 | context_snapshot上限 |
| `KANAE_STATE_TIME_BUCKET_MIN` | 5 | kanae_state timestamp丸め単位 (分) |
| `MEM0_AUTO_RECORD_ENABLED` | true | 自動記録の有効化 |
| `MEM0_AUTO_RECORD_INTERVAL_S` | 300 | 自動記録の最小間隔 |
| `MEM0_AUTO_RECORD_MIN_CHARS` | 80 | 自動記録の最小文字数 |
| `MEM0_REWRITE_MAX_TOKENS` | 500 | smart search rewriteのmax_tokens |
| `MEM0_RERANK_MAX_TOKENS` | 400 | smart search rerankのmax_tokens |

JSONL: 既存の `role`, `content` に加えて、API replay専用の任意フィールド
`context_snapshot` を追加した。Web履歴表示では露出させない。

`search_memories_smart()` のシグネチャは変更していない。呼び出し側で
`rewrite` と `rerank` を制御する。

## 採用しなかった候補と理由

### builtin tools 条件付き広告 (一度入れたが撤回)

2026-05-15 に `_needs_builtin_tools(raw_user_text)` でadminでも純チャット
扱いなら builtin tools schema を送らない設計を入れたが、フォローアップ
発言で頻繁に tool 不在状態を引き起こした。例:

- ターン「最初の20ページだけ読んでみて」(15文字) → 「ファイル読み取りの
  ツールが使えない」
- ターン「20〜40ページも読んでいいよ」(15文字) → 同上

「読んで」「ページ」「画像」「PDF」「PNG」など、自然なファイル参照語が
語彙にカバーされておらず、admin の自由文で穴を埋めるのが非現実的だった。

cache を考慮した実コストも warm時 約 68 tokens/turn しかなく、UX 損失
(運用中に tool が頻繁に消える) と引き換えにする価値がない。

撤回後の挙動: adminへは常時 builtin tools を advertise。Mem0 MCP schema
常時広告と context_snapshot 復元はそのまま残す。

### Thread context LRU cache

[utils/discord_helpers.py:31](utils/discord_helpers.py#L31) に
`_thread_context_cache: TTLLRUCache` が既に実装されており、
`fetch_thread_context()` 内で使われている。新規実装は不要。

### CLI identity text / system block への cache_control 追加

[services/claude_api/tool_loop.py:62-77](services/claude_api/tool_loop.py#L62-L77)
のコメントに、mitmproxy 2026-05-13で捕捉した実CLIのbyte-shape-equal送信形態が
記録されている。system[0] (attribution)、system[1] (CLI identity)、system[3]
(caller prompt) に `cache_control` を追加するとCLIと送信形態が異なり、
subscriber-tier gatingが壊れる可能性がある。token削減効果より事故リスクが大きい。

### CLI_DEFAULT_SYSTEM_PROMPT を省略・scope変更

subscriber-tier識別に必須としてmitmproxyで検証済み。token削減のために触ると
Pro plan 認識が壊れる可能性がある。「SDK/CLI rollback経路は変更しない」前提に
反する。

### API_SYSTEM_PROMPT 分離・新規prompt files

当初は `prompts/api_common.md` と `prompts/discord_bot.md` をAPI経路専用に
新設する案だったが、内容が短すぎて人格・Discord出力ルールの細部が落ちると
判断。代わりに既存 `~/.claude/CLAUDE.md` と `/home/ubuntu/discord-bot/CLAUDE.md`
を直接最適化する方針に切り替えた (項目E)。SDK rollback経路と共通の
SYSTEM_PROMPT を使い続ける。

### Retry時のtool_result削除

Anthropic APIは `tool_use` と `tool_result` のペア整合を要求する。片方だけ
削除すると400エラーになる。安全な実装が困難。

### Tool schema のDSL圧縮

JSON schemaはAPI契約。圧縮不可。

### Attribution header の HTTP化

subscriber-tier識別に関わる。CLI byte-shape互換と同じ理由で保留。

## Assumptions

- 既存JSONL履歴の書き換えは行わない。compact保存は新規行から適用する。
- `context_snapshot` はAPI replay専用で、Web APIの履歴表示には露出させない。
- `search_memories_smart` のシグネチャと既定値は変更しない。
- `CLI_DEFAULT_SYSTEM_PROMPT` は加入者判定互換のため変更しない。
- prompt文面の圧縮は保守的に行い、人格、安全ルール、Discord出力ルールを維持する。
- `MAX_PROMPT_CHARS`、画像上限、添付上限、`CLAUDE_MAX_TOKENS_OPUS` の調整は
  本計画の範囲外。
- 未解決競合がある `web_api.py`、`tests/test_bot.py`、`tests/test_web_api.py`
  は本計画の範囲外。

## 適用後の検証

- usage logの実測値で削減効果を確認する。
- warm時のcache_read_input_tokens比率を観測する。
- short follow-upでMem0 recall continuityが維持されているか会話品質で確認する。
- 「覚えて」発言で `_is_force_record_request` がthrottleをbypassすることを
  確認する。
