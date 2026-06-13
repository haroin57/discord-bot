# Claude Code CLI バイナリ精査 → Discord bot 改善点まとめ

> **実装状況 (2026-06-13)**: 第1波〜第3波（#1 #2 #3 #4 #5 #6 #7 #8 #10 #11 #12
> #13 #14 #15 #16 #17a/b/d #19）実装済み。#3/#7/#19 は ADR-0012 参照。
> 残: 任意/低優先（#9 #18 #20 #21 #22 #23 #24 #25, #17c）。
> 注: その後に試作した resume latch 復元 / `/fable` コマンドは haroin 指示で削除済み。
> compaction の summary call が subscriber 識別不足（metadata + CLI 形 system 欠如）で
> 毎回 429 になり圧縮が一度も成功していなかった問題は **2026-06-13 修正済み**
> （subscriber-shaped request 化、compaction.py）。

調査日: 2026-06-13 / 対象バイナリ: `claude.exe`（claude-cli 2.1.170, Bun ELF, not stripped）
方法: 埋め込み JS strings（`/tmp/claude_exe_strings.txt`, 760K 行）を 5 テーマで並列精査し、
bot の実装（`services/claude_api/`, `services/session.py`）と逐次比較。各指摘はバイナリの
実 identifier を根拠に持ち、bot の実コード行で裏取り済み。

凡例: 優先度 P0（実害/未対応の outage 経路）/ P1（信頼性・品質の明確な改善）/ P2（最適化・
観測性）。Effort S/M/L。

---

## エグゼクティブサマリ（横断トップ）

bot は ADR-0010 でリファレンス実装の多くを正しく再現済み（lossless replay、compact summary
anchor、refusal sticky fallback、DKf 非文字列 strip、text-invoke poison 対策）。残る重要ギャップは
**「長コンテキストを refusal まで放置している」構造**と、**resume の最後の hard-400 経路**、
そして**いくつかの実害バグ**。

実装すべき順（投資対効果順）:

1. **P0 / Bash timeout クランプのバグ修正**（S）— `min(timeout, DEFAULT)` で `timeout` 引数が
   上方向に死んでいる。表示と実挙動も食い違う。`tools.py:497`。
2. **P0 / resume の orphan tool_use prune（`Yy8` 相当）**（S）— sanitize で tool_result frame が
   落ちると tool_use が孤児化 → 次リクエストが 400 → `invalid_request` 永久エラーで会話消失。
3. **P0 / in-loop proactive compaction**（L）— ADR-0010 の根本原因。refusal を待たず、ターン内で
   トークン閾値を超えたら畳む。sticky-Opus はこれを 2〜3 倍コストで覆い隠しているだけ。
4. **P0 / 主ループ effort が `medium` 固定**（S）— CLI 既定は `high`、bot の compaction だけ `max`。
   主ループだけリファレンスより 1 段低い品質に固定されている。env 可変化。
5. **P0 / retry-after を sleep に反映していない**（M）— サーバが「45 秒待て」と言っても bot は
   ~3〜6 秒で再試行し budget を 20 秒で焼き切る。
6. **P0 / fallback ladder が 2 段不足**（M）— model-not-found(404) と overload(529) で Opus に
   逃げる経路が無い。モデル改名 = 無対応 outage。

P1 以降は各テーマ節を参照。

---

## 横断 優先度テーブル

| # | テーマ | 指摘 | CLI 根拠 | Effort | 優先度 |
|---|---|---|---|---|---|
| 1 | tool | Bash `timeout` が default にクランプ（引数が無効）+ 単位/上限未記述 | `BASH_DEFAULT_TIMEOUT_MS=120000` / `BASH_MAX_TIMEOUT_MS` 別物, schema `max 600000` | S | P0 |
| 2 | session | orphan tool_use prune（`Yy8`）不在 → resume hard-400 | `Yy8` id 突合 drop | S | P0 |
| 3 | context | in-loop proactive compaction 不在（境界でしか畳まない） | `xXf`/`su4`/`lm4`（20% buffer） | L | P0 |
| 4 | output | 主ループ effort `medium` 固定（CLI 既定 high、env 不可） | `output_config effort default high`, `CLAUDE_CODE_EFFORT_LEVEL` | S | P0 |
| 5 | resilience | `retry-after`/`unified-reset` を sleep に未反映 | `lqH max(retryAfter*1000,…)`, `v75`, `N75` | M | P0 |
| 6 | resilience | fallback ladder 2 段不足（404 / 529） | `tengu_api_model_not_found_fallback_triggered`, `tengu_api_opus_fallback_triggered`, `_fallback_last_resort` | M | P0 |
| 7 | context | compaction trigger が文字数ベース・output 予約ゼロ | `M4H` `window - min(maxOut,20000)`, `KC$ = window-13000` | M | P1 |
| 8 | context | cache breakpoint が 1 個（CLI は [-1]/[-2] 2 個 + fork pin） | `Def` `z.add(A)` + `z.add(w)` + `forkPointPinned` | S | P1 |
| 9 | context | 1h TTL を overage 時も無条件付与（CLI は overage で 5m に落とす） | `CIH` `isUsingOverage → return!1` | S | P1 |
| 10 | tool | timeout 時に partial stdout を捨てる（auto-background も無し） | `tengu_bash_command_timeout_backgrounded` | S | P1 |
| 11 | tool | tool input の schema 検証なし（`<tool_use_error>` 形式で返さない） | `InputValidationError` + `is_error:true` | M | P1 |
| 12 | tool | tool が逐次実行（CLI は concurrency-safe を並列） | `isConcurrencySafe` バッチ | M | P1 |
| 13 | tool | Bash 出力が head 切り（tail の失敗ログが消える） | 中央切り `slice(0,K)+slice(-_)` | S | P1 |
| 14 | session | `tool_use_mismatch` 400 を永久エラー化（recovery 無し） | `"tool_use ids were found without tool_result"` 専用分類 | S–M | P1 |
| 15 | session | thinking-only assistant frame prune（`WS$`）不在 | `tengu_filtered_orphaned_thinking_message` | S | P1 |
| 16 | resilience | slow-first-byte（TTFB）検知が stall と未分離 | `CLAUDE_SLOW_FIRST_BYTE_MS||30000`, `tengu_api_slow_first_byte` | M | P1 |
| 17 | obs | usage ログに request-id / 時間 / attempt が無い | `tengu_api_success {requestId, durationMs, ttftMs, attempt, …}` | S | P1 |
| 18 | resilience | 529 backoff が generic と同一（過少待機） | `RLH`, `Math.max(h??G75,V75)`, `tK5=3` | S | P1 |
| 19 | context | 3 つの circuit breaker のうち 1 つだけ（rapid-refill/prefix-overflow 無し） | `consecutiveRapidRefills`, `tengu_auto_compact_prefix_overflow` | M | P2 |
| 20 | session | 連続 user turn merge（`PS$`）不在 → cache prefix 不安定 | `RC8`/`MF4` consecutive-user merge | S | P2 |
| 21 | tool | unset-var `rm -rf $VAR/*` ガード無し | `rm -rf $UNSET/* → /*` 専用警告 | S | P2 |
| 22 | output | `max_tokens` のモデル別上限クランプ無し | `gqH(name,val,default,upperLimit)` | S | P2 |
| 23 | output | `task_budget`（累積トークン予算）未使用 | `output_config.task_budget`, beta `task-budgets-2026-03-13` | M | P2 |
| 24 | resilience | budget 枯渇時に分類済みでなく "unknown" を返す | （bot 内部 / ADR-0006 系） | S | P2 |
| 25 | context | time-based microcompaction（`context_hint` beta）未対応 | `context-hint-2026-04-09`, `tengu_time_based_microcompact` | M | P2 |

---

## テーマ1: context management / compaction / cache

CLI の auto-compact 数式（復号済み, 1M window 基準）:
`KC$(H)=H-13000`（blocking 上限）/ `M4H=window-min(maxOut,20000)`（実効窓）/
`lm4=min(H-round(H*0.2),KC$)`（**proactive** 閾値, 20% buffer）。定数 `dm4=13000, om4=20000,
VKq=0.2, NKq=OI8=EKq=3, sX9=5`。proactive はオーバーフローする前のターンで先に畳む。

**P0 / in-loop proactive compaction 不在（#3）.** CLI は毎ターン `xXf()` を評価し、投影トークンが
proactive 床（`lm4`, 20% 余裕）を超えたら `su4` で会話を in-place 要約して同一セッションを継続。
bot は `compaction.py` を**セッション境界でしか**呼ばない（`entry.py` で retry loop 前、文字数
200K 閾値）。ループ内は server-side `clear_tool_uses`（150K, ツール結果のみ）だけで、長い 1 ターンは
Fable refusal → Opus へ落ちるまで放置。**これが ADR-0010 が追っている根本**。改善: `run_agent_loop`
内で前ターンの `usage.input_tokens` を見て proactive 床超過なら `compact_channel_history` を
ループ内発火し `messages` を再構築。L。

**P1 / trigger が文字数ベース・output 予約ゼロ（#7）.** CLI は token ベースで `min(maxOut,20000)`+
`13000` を窓から引く。bot は `HISTORY_COMPACT_TRIGGER_CHARS=200000`（文字）で JP/code だと
50〜65K token 相当 = 1M 窓に対し過保護、かつ char→token 比が 2〜4 倍ぶれる。改善: trigger を
token 化（`_log_turn_usage` の `input_tokens` を再利用）し output 予約分を引く。M。

**P1 / cache breakpoint が 1 個（#8）.** CLI `Def` は last と 2nd-to-last の 2 user index に마크 +
resume/fork anchor を pin（`forkPointPinned`）。bot `_inject_cache_breakpoints` は `user_indices[-2]`
の 1 個のみ。`[-1]` を打たないと次ターンの read 窓が前ターンの返信+tool結果まで伸びない。改善:
`[-1]` と `[-2]` の両方 + compact-summary anchor にも breakpoint。S。ADR-0010 の `cache_read=0`
カスケードに直撃の安い勝ち。

**P1 / 1h TTL を overage 時も無条件（#9）.** CLI `CIH` は `isUsingOverage` なら 1h を切って 5m に。
bot は 4 ブロックすべて `ttl:"1h"` 固定。overage 時は 2× write プレミアムを無駄払い。改善: 既存の
ratelimit state（`observe`/`should_block`）で overage を検知し 1h→5m。S。ただし ADR-0010 phase0 で
TTL は主犯ではないと判定済みなので最適化扱い。

**P2 / circuit breaker 3 種のうち 1 種だけ（#19）.** CLI は failure(`NKq=3`)/ rapid-refill(`OI8=3`,
「3 ターン以内に上限まで再充填が 3 連続 = thrashing」)/ prefix-overflow の 3 つ。bot は failure
（`MAX_CONSECUTIVE_COMPACT_FAILURES=3`、30 分 cooldown は CLI に無い独自）だけ。in-loop compaction
（#3）導入後に rapid-refill を足すべき（巨大ツール結果 1 個で毎ターン再 compaction する事故防止）。M。

**P2 / time-based microcompaction（#25）.** CLI は server `context_hint`（beta `context-hint-2026-04-09`）
で「20K 以上節約できる」と通知されたら古い tool_result body のみ placeholder 置換（keep recent 5）。
bot は未対応（`MODEL_BETAS` に無い）。chat bot で tool 重ループは稀なので低価値。M / P2 任意。

検証済み非ギャップ: `scope:"global"` の system block 付与、failure breaker 閾値=3、`[-2]` の打ち方の
合理性、reactive→Opus ルーティング（bot の sticky-Opus と同思想）、要約モデルを Opus+max にする
意図的逸脱 — いずれも正しい/妥当。

---

## テーマ2: API resilience（fallback / retry / rate limit）

CLI は **2 層**の retry を持つ: (a) SDK transport 層 `min(0.5·2^n,8)s·(1−rand·0.25)`、
(b) agent-loop 層 `min(500·2^(n−1),cap)ms + jitter` + `retry-after` override + 529 専用の分単位床。
bot は 1 層（`entry.py` ループ + `compute_backoff`）のみで SDK の `max_retries` は既定放置。
復号定数: `K75=500`(base), `_75=60000`(retry-after-too-long), `z75=30000`(persistent wait chunk),
`G75=1800000`/`V75=600000`(overload 床), `tK5=3`(overload→fallback 前 retry 数), `QI6=6h`(reset 上限),
slow-first-byte 既定 30000ms。

**P0 / retry-after を sleep に未反映（#5）.** CLI `lqH` は `max(retryAfter*1000, computedBackoff)` で
サーバ指定待ちが長ければ常に勝つ。さらに `unified-reset` を `QI6=6h` 上限で読む。bot `entry.py:599` は
`compute_backoff(attempt)` のみで `retry-after` を**無視**（ユーザ向け文言にだけ使う）。429 で
`retry-after: 45` でも ~3〜6 秒で再試行し同じ壁に当たり、4 attempt を ~20 秒で焼く。改善:
`_anthropic_error_headers(exc)` から `retry-after`/`unified-reset` を取り `sleep = max(retry_after,
compute_backoff(attempt))`。ただし chat bot なので数分上限を超えるなら即 rate_limit 文言を返す。M。

**P0 / fallback ladder が 2 段不足（#6）.** CLI は 3 トリガー: model-not-found(404)即 fallback、
overload(529)を `tK5=3` 回 retry 後に opus fallback、その他 non-retryable で last-resort fallback。
bot のモデル swap は **refusal だけ**。404（モデル改名/廃止）は `api_exit` 永久エラー、529 連発は
Fable を 4 回叩いて諦め（Opus を試さない）。改善: `classify_anthropic_error` に `model_not_found`
追加、`entry.py` の except で overload ほぼ枯渇時 or 404 検知時に Opus へ 1 回逃がす。M。

**P1 / slow-first-byte 検知が stall と未分離（#16）.** CLI は `CLAUDE_SLOW_FIRST_BYTE_MS||30000` の
専用タイマで「接続したが最初の chunk が来ない」を検知し `tengu_api_slow_first_byte` を per-attempt
記録。bot の `StallWatchdog` は最初のイベントで heartbeat リセットされ TTFB stall を中間 stall と
区別できない。改善: `stream` 開始前に monotonic タイマを張り、初回イベントで解除、超過時 WARN +
任意で abort-retry。M。（テーマ5 でも独立に検出 = 確度高い）

**P1 / 529 backoff が generic と同一（#18）.** bot の `compute_backoff` は overloaded と rate_limit が
同一・`MAX_BACKOFF_S=60` cap。CLI は 529 を `tK5=3` quick retry 後に fallback/throw、待つ場合は分単位床。
chat bot で分単位床は不適切（UX）なので CLI 値そのままは**非推奨**だが、現状は過少待機。改善:
overloaded に rate_limit より少し大きい base/cap（例 base 5s, cap 90〜120s）。S。

**P2 / sticky pin が revert しない（#（補））→ 訂正 (2026-06-13).**
初稿の「CLI は次の成功ターンで元モデルに戻る」は**誤読**だった。2.1.170 の SDK schema 記述で確認:
`direction` は常に `"retry"` で **swap はセッション永続**（"revert" and "sticky" are retained in
the enum for SDK-consumer compat and are no longer emitted）。つまり bot の sticky pin は CLI と
同挙動で正しい。本当のギャップは **resume 横断の latch 復元**: CLI はフォールバック時に
`model_refusal_fallback` system 行を transcript に書き、resume 時に後ろから走査して latch を復元する
（`B1A`/`m1A`, `tengu_refusal_fallback_resume_latch` action=relatch）。例外は fork
（`neutralizedByFork`）と明示的モデル指定（`latch_reset`）のみで、compaction では無効化しない。
一度実装した（assistant 行 `refusal_fallback_model` stamp + `load_refusal_latch` + relatch +
`/fable` 解除コマンド）が、17件完了時点への巻き戻し指示で**削除済み**。再実装する場合はこの設計を再利用可。

**P2 / budget 枯渇時に "unknown" を返す（#24）.** `entry.py` の per-channel budget（3/60s）枯渇時に
分類済みでなく generic "内部エラー" を返す（ADR-0006 が attempt 枯渇路で直したのと同じ masking が
budget 路に残存）。改善: 枯渇でも overloaded/rate_limit 分類文言を返す。S。

検証済み非ギャップ: equal-jitter backoff、refusal-capture 診断、ratelimit ヘッダ観測 — いずれも堅実、
一部 CLI より先行。`x-is-refusal-fallback` は CLI と同意味（retry leg で立てるのはむしろより正確）。

---

## テーマ3: tool execution（bash / tool_result / 並列）

**P0 / Bash timeout クランプのバグ（#1）.** `tools.py:497` `timeout_s = min(timeout_s,
BASH_DEFAULT_TIMEOUT_S)` で **`timeout` 引数が default(120s) より上に効かない**。`tool_loop.py:360`
は要求値で `(timeout={hint}s)` と表示するので表示と実挙動が食い違う。さらに CLI は default(120000ms)と
max(600000ms)が**別 env**で schema にも `max 600000` と明記、bot の schema は `{"type":"number"}` のみで
**単位（ms/秒）も未記述**（bot は秒として `wait_for` に直渡し）。改善: `BASH_MAX_TIMEOUT_S`（CLI 既定
600s）を新設し**そこに**クランプ、schema に description と単位を明記。S。実害バグ + 契約の嘘。

**P1 / timeout 時に partial stdout を捨てる + auto-background 無し（#10）.** CLI は timeout で hard-kill
せず background 化し `{stdout, backgroundTaskId, assistantAutoBackgrounded}` を返す
（`tengu_bash_command_timeout_backgrounded`）+ `run_in_background` param + `BashOutput` polling。bot
`tools.py:509-515` は `killpg(9)` + raise で**timeout 前の partial stdout を破棄**。`npm test`/`pip
install` が出力ゼロで復帰不能。改善(1): timeout 時に buffer 済み stdout を非エラー tool_result として
`[command timed out after Ns; partial output above]` 付きで返す（S）。改善(2): `run_in_background` +
`BashOutput`（プロセスレジストリが要る, L, P2 任意）。

**P1 / tool input の schema 検証なし（#11）.** CLI は zod で dispatch 前検証し失敗時
`<tool_use_error>InputValidationError: …</tool_use_error>` + `is_error:true` を返す（モデルが 1 ターンで
自己修正可能）。bot は schema 検証ゼロ、各 handler の ad-hoc raise を `tool_loop.py:1041` が generic
catch（`is_error` は立てる = 良い、が文言が `str(exc)` で field 不明、`<tool_use_error>` 形式でもない、
未ガードの MCP tool は深いトレースバックが漏れる）。改善: dispatch 前に `input_schema` で検証し
CLI 形式の文字列を返す。schema は `BUILTIN_TOOLS` に inline 済みなので配線中心。M。

**P1 / tool が逐次実行（#12）.** CLI は `isConcurrencySafe()` でバッチを判定し read-only 群（Read/Glob/
Grep）を並列 dispatch。bot `tool_loop.py:984` は `for block ... await execute_tool` で逐次。モデルが
Read を 5 個出す（漫画レビュー等）と直列で 5 倍レイテンシ。改善: builtin に `concurrency_safe` を付与し、
ターンの tool_use が全部 safe なら `asyncio.gather`、そうでなければ現状の逐次。tool_use_id 順は保持。M。
低リスク・多 Read ターンで大きな UX 改善。

**P1 / Bash 出力が head 切り（#13）.** CLI は中央切り（head+tail 保持, 中間を要約）。bot
`tools.py:517` は `[:BASH_OUTPUT_CAP_BYTES]`(100KB)で **head のみ保持 = tail を破棄**。test/build の
"FAILED"/traceback は tail にあるので失敗がモデルから隠れる。さらに stdout/stderr 各 100KB = 最大
200KB（CLI 既定 30KB の ~7 倍 = token sink）。改善: stdout を中央切りに、cap も CLI 寄り(30KB)に。S。

**P2 / unset-var `rm -rf $VAR/*` ガード（#21）.** CLI は未定義変数の filesystem-root 展開
（`rm -rf $UNSET/* → /*`）を専用警告し明示承認を要求。bot はコマンド検査ゼロ（admin は設計上無制限
だが、これは**モデルの事故**でありオーナー意図ではない。bot env に `HOME` allowlist あり = `~` 消滅
リスク）。改善: `rm -rf` で引数に未定義 env var の `$VAR/` 展開を含む場合のみ refuse-and-explain
（silent block ではなくオーナーが再発行できる形で）。広い「自分が作っていないファイル」追跡は不要。S。

検証済み非ギャップ: text-invoke poison 対策（今日の修正, CLI より厚い防御）、`is_error` 返却、
プロセスグループ隔離 + SIGKILL、empty-turn/pause-turn 処理 — いずれも堅実。

---

## テーマ4: session / transcript / resume

CLI deserialize パイプライン（`Wt6`）順序: `XS$`(retracted drop)→`jKf`(attachment 正規化)→
`DKf`(非文字列 text strip)→permissionMode 正規化→**`Yy8`(orphan tool_use prune)**→
**`WS$`(orphan thinking prune)**→**`PS$`(whitespace-assistant prune + 連続 user merge)**→
`JKf`(interrupt 検知)→末尾 user continuation inject。bot は `DKf` + leading-assistant drop だけ実装、
`Yy8`/`WS$`/`PS$`/interrupt 検知を欠く。

**P0 / orphan tool_use prune（`Yy8`）不在（#2）.** CLI `Yy8` は全 tool_use id と tool_result id を
突合し、tool_result の無い tool_use だけの assistant message を drop（API 400 の proactive ガード）。
bot は id 突合ゼロ。「各リクエストの展開は 1 行に収まる」不変条件に依存するが、これは
`_sanitize_replay_messages` 自身が破る: tool_result-only frame が strip で空になり drop されると
（session.py の `if not filtered: continue`）、同じ行の**残った tool_use が孤児化** → 次の Messages
API が 400 → bot はこれを `invalid_request` = 永久エラー扱いで会話を捨てる。改善: `load_api_replay_
channel` 末尾に `Yy8` 相当の最終スイープ（組み立て後の messages で id 集合を作り、未対応 tool_use の
みの assistant を drop）。~20 行・純関数。S。**resume に残る唯一の hard-400 経路**。

**P1 / `tool_use_mismatch` 400 recovery 不在（#14）.** CLI は該当 400（`"tool_use ids were found
without tool_result blocks immediately after"`）を専用分類し offending `toolu_…` を辿って recoverable
扱い。bot は全 400 を `invalid_request`（永久・no retry）に潰す。改善: `classify_anthropic_error` で
この 3 種の pairing 400 文字列を検出し `tool_use_mismatch` 分類を新設、`entry.py` でその分類時は
session drop + history TTL クリアで次ターン clean reload。S–M。#2 と defense-in-depth。

**P1 / thinking-only assistant prune（`WS$`）不在（#15）.** CLI は thinking/redacted_thinking のみの
assistant frame を（同一 message.id に実内容が無ければ）drop。bot は thinking+signature を verbatim
persist するので、thinking のみの frame が単独 replay されると Fable で 400 or 退行継続を誘発。改善:
`_sanitize_replay_messages` に「thinking 系のみの assistant frame を drop」を追加（bot は単一行モデル
なので cross-id 共有判定は不要）。S。

**P2 / 連続 user turn merge（`PS$`）不在（#20）.** CLI は隣接 user を `RC8` で merge。bot は
`[発言者:]` 付き top-level user と tool_result user frame を出し、間の assistant 行が
`is_non_persistable_reply` で drop されると user が隣接しうる（API は許容するので 400 ではないが、
CLI 正規化からの乖離 + **cache prefix 不安定** = ADR-0010 の cache 論題に直結）。改善: 組み立て後に
連続 user を coalesce。S。

**P2 / interrupt 検知（`JKf`）.** CLI は末尾が中断 user なら `interrupted_prompt` 分類し synthetic meta
（`CLAUDE_CODE_RESUME_PROMPT || "Continue from where you left off."`）や末尾 user 後に
`"No response requested."` を inject、末尾 dangling tool_use には synthetic tool_result
（`[Tool result missing due to internal error]`）を inject。bot は `run_agent_loop` が自前で user prompt
を append するので「末尾が空 user」は構造的に起きにくく、continuation 機構は**不要**。末尾 dangling
tool_use は #2 の drop で中和済み。P2（主に「CLI の continuation 機構を別途再実装しない」確認のため）。

検証済み非ギャップ: `DKf`、compact-summary anchor（`o27`/`X0$`/`keep_tail_rows`）、leading-assistant
drop（test 済み）、retracted drop（`is_non_persistable_reply` で同等の終状態）、permissionMode（bot に
該当 field 無し）、`jKf`/sidechain DAG（bot は flat append log なので非該当）。

---

## テーマ5: prompt / output / observability

**P0 / 主ループ effort `medium` 固定（#4）.** CLI API 契約: `output_config.effort` 既定は `high`
（省略時と等価）、env `CLAUDE_CODE_EFFORT_LEVEL` で `low/medium/high/max/x-high/ultracode` 上書き可。
bot `client.py:60` `OUTPUT_CONFIG = {"effort":"medium"}` は module 定数・全ターン送出・env hook 無し。
**bot 自身の compaction は `HISTORY_COMPACT_EFFORT=max`（env 可変）で正しくやっている**ので、主ループ
だけが外れ値（CLI 既定より 1 段低い品質に固定）。改善: `config.py` で env 可変化、既定 `high` で
CLI に合わせる。`medium` の「予測可能なレイテンシ」トレードオフは現実だが、**埋もれた定数ではなく
tunable にすべき**。S。リファレンス比で回答品質に直結。
※ 直近 haroin が「bot を medium に」と指示済みなので、env 既定値は要確認（指示尊重なら既定 medium
のまま env で上書き可能にする形が無難）。

**P1 / usage ログに request-id / 時間 / attempt が無い（#17）.** CLI `tengu_api_success` は
`{requestId, firstAttemptRequestId, durationMs, durationMsIncludingRetries, ttftMs, attempt,
messageCount, cachedInputTokens, uncachedInputTokens, costUSD, stop_reason, timeSinceLastApiCallMs,
promptCategory}`。bot `_log_turn_usage`/`_log_total_usage` は 4 token + stop_reason のみ。改善（価値順）:
(a) **request-id**（`stream.response.headers["request-id"]`）を毎ターン記録 = refusal/エラーの
サーバ突合が今は不可能なので最高レバレッジ S。(b) `durationMs`+`ttftMs`（monotonic で dispatch と
初回 block）S。(c) `costUSD`（subscriber tier だが token×公開レートの推定で「高コスト channel」可視化）
M/P2。(d) `messageCount`（`len(messages)`、履歴肥大検知）S。

**P1 / slow-first-byte（#16 と同一）.** テーマ2 と重複検出。確度高い。S–M。

**P2 / `max_tokens` のモデル別上限クランプ無し（#22）.** CLI は `gqH(name,val,default,upperLimit)` で
env 値をモデル上限にクランプし overflow 時 `max_output_tokens` を上げるヒントを出す。bot
`CLAUDE_MAX_TOKENS` は raw、上限超えると 400。改善: モデル既知上限にクランプ。S。現状値は健全なので
低優先。

**P2 / `task_budget`（累積トークン予算）未使用（#23）.** CLI `output_config.task_budget:{type:"tokens",
total:N}`（最小 20000, beta `task-budgets-2026-03-13`）でモデルに「全 agentic loop の残トークン」を
カウントダウン提示し自己抑制させる。bot は `max_turns`/`MAX_CONSECUTIVE_EMPTY_TURNS` でしか縛らず、
多数の安いターンでトークンを浪費しても気づけない。改善: 重い research ターンで `task_budget` を渡す。
beta 追加 + `clear_tool_uses` edit との非干渉検証 + ADR 推奨。M / P2。

検証済み非ギャップ: `cli_default_system_prompt.txt`（CLI の refusal block を 1:1 コピー済み）、
`THINKING_CONFIG={"type":"adaptive"}`（CLI schema 一致, `display` は真に optional）、`_block_to_dict` の
signature 往復、`MODEL_BETAS` の effort/cache/context beta、refusal 分類ログ（ADR-0008）。`auto_dream`/
`CLAUDE_MEMORY_STORES`（bot は Mem0 が上位互換）、`CLAUDE_CODE_SUBAGENT_MODEL`/`tengu_agent_*`
（単一エージェント Discord responder なので非該当）、system prompt 拡張（かなえ persona 希釈リスク）—
いずれも対応不要。

---

## 推奨実装順序

**第1波（実害バグ・S, 即やる）:** #1 Bash timeout クランプ修正、#2 `Yy8` orphan tool_use sweep、
#17(a) request-id ログ、#13 Bash 出力中央切り、#8 dual cache breakpoint。いずれも S で独立。

**第2波（信頼性・S〜M）:** #5 retry-after を sleep 床に、#6 fallback ladder（404/529→Opus）、
#11 tool input schema 検証、#14 `tool_use_mismatch` recovery、#15 `WS$` thinking prune、
#16 slow-first-byte、#10 Bash timeout partial 返却。

**第3波（品質・要 ADR）:** #3 in-loop proactive compaction（L, ADR-0010 の本丸）+ #7 token trigger +
#19 rapid-refill breaker をセットで、#4 effort env 可変化（haroin の medium 指示と整合確認）、
#12 並列 tool 実行。

**任意/低優先:** #9 TTL overage gating、#18 529 backoff、#20 連続 user merge、#21 rm ガード、
#22 max_tokens clamp、#23 task_budget、#24 budget エラー分類、#25 microcompaction。

## 関連
- ADR-0010（Fable refusal / cache cascade）— #2/#3/#7/#8/#9 が直結。
- ADR-0008（poison loop）— #2/#14/#15 が同じ「interrupted artifact が resume を壊す」系。
- ADR-0006（overloaded masking）— #24 が同じ masking の残存。
- docs/2026-06-13_tool_call_text_invoke_poison.md — text-invoke 対策（CLI より厚い、対応済み）。
