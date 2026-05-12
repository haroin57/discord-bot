# Discord Bot 全コードベース SRE レビュー

**作成日**: 2026-05-13
**手法**: 10 並列エージェントで領域別 SRE 観点走査
**前提**: 2GB RAM Ubuntu VPS、single-tenant、systemd 起動、Discord guild 1 + DM、Web UI 同居

## サマリ（重大度別）

- 🔴 Critical: 9件 — 本番運用前に必須対応
- 🟠 High: 11件 — 早期対応推奨
- 🟡 Medium: 16件 — 計画的対応
- 🔵 Low/Info: 14件 — 余力で

---

## 🔴 Critical（本番投入前に必須）

### SRE-C1. SIGTERM ハンドラなし → Claude subprocess がゾンビ化
**Location**: [main_async.py:125-126](main_async.py#L125), [services/claude_sdk.py:511-601](services/claude_sdk.py#L511)
**Risk**: `systemctl stop` / `systemctl restart` 時に in-flight Claude CLI subprocess（Node.js + chromium + MCPs）がそのまま残る。CancelledError でも `kill_orphan_claude_cli()` は呼ばれない（`except CancelledError` ブロックなし）。再起動のたびに RAM が圧迫されていく。
**Fix**: `main_async.main()` で `loop.add_signal_handler(SIGTERM, ...)` を登録、`bot.close()` 前に `kill_orphan_claude_cli()` を呼ぶ。`call_with_tools` の `async for` を `try/finally` で囲み CancelledError 時にも kill。

### SRE-C2. systemd `StartLimitBurst` 未設定 → クラッシュループ無制限
**Location**: [deploy/systemd/discord-bot.service:13-14](deploy/systemd/discord-bot.service#L13)
**Risk**: `Restart=on-failure RestartSec=10s` のみ。`ADMIN_USER_IDS` parse fail（M11 で追加した RuntimeError）や `.env` 設定ミスで起動時例外 → systemd が永遠に 10s 間隔で再起動を試行 → CPU と journald 圧迫。
**Fix**: `StartLimitBurst=3 StartLimitIntervalSec=60s StartLimitAction=none` を追加、`OnFailure=alert@%n.service` で通知。

### SRE-C3. `sessions.json` が non-atomic write → 破損時に全 session 喪失
**Location**: [services/session.py:129-135](services/session.py#L129)
**Risk**: `O_TRUNC` で開いて `json.dump` 中に OOM kill / SIGKILL されると 0 byte または partial file になる。`load_sessions` は `JSONDecodeError` を silent catch して空 dict を返す → 全 channel の Claude session resume ID 喪失。
**Fix**: tmp file に書いて `fsync` → `os.replace()` で atomic rename。

### SRE-C4. `.state/` と ChromaDB のバックアップ皆無
**Location**: `.state/`, ChromaDB persistence dir
**Risk**: ディスク障害・誤 `rm -rf` で過去会話履歴・Mem0 (860+ memories)・session 全消失。`weekly_mem0_compact.py` は read-only でバックアップ機能なし。
**Fix**: 週次 cron で `rclone copy .state/ gdrive:discord-bot-backup/$(date +%F)/`、ChromaDB も同様に rclone or rsync。

### SRE-C5. `status_callback` 例外で retry 4周ループ
**Location**: [services/claude_sdk.py:531,544,546,556,570,572,703](services/claude_sdk.py#L531)
**Risk**: Discord disconnect で `status_callback` が例外を吐くと outer `except` で classified `unknown` → 4 回再試行（each retry が full backoff + budget 消費）→ 最終的に misleading error。partial response が捨てられる。
**Fix**: `_safe_status` ラッパで `try/except Exception: pass`。

### SRE-C6. Web UI reconnect storm で verify_session 1000-fanout
**Location**: [web_api.py:131-181](web_api.py#L131)
**Risk**: `_session_cache` lookup に mutex なし。Next.js 再デプロイ等で 1000 client が同時 reconnect すると同一 token で 1000 並列 HTTP call が localhost:3001 に飛ぶ → Next.js auth endpoint 雪崩。
**Fix**: per-token `asyncio.Lock` で in-flight dedup。

### SRE-C7. /health エンドポイント皆無
**Location**: [web_api.py](web_api.py)
**Risk**: Web API port 3002 は `/internal/invalidate_session` と Socket.IO のみ。Bot がデッドロックしても TCP 接続は応答するので外形監視で検知不能。
**Fix**: `app.router.add_get("/health", _handle_health)` を追加、`{discord_connected, mem0_ok, uptime_s, last_claude_call_ts}` を返す。/ready で OAuth 残り時間も。

### SRE-C8. Mem0 store 失敗が DEBUG 埋葬
**Location**: [bot.py:892](bot.py#L892), [web_api.py:536](web_api.py#L536)
**Risk**: `log.debug("Mem0 store failed (non-fatal): %s", exc)` — ChromaDB crash / OAuth expiry mid-session で記憶パイプラインが何時間も silent fail しても誰も気づかない。
**Fix**: WARNING に昇格、連続 N 回失敗で ERROR に escalate（circuit breaker）。

### SRE-C9. OAuth token refresh 失敗が silent degrade
**Location**: [services/oauth_manager.py:189,212,228](services/oauth_manager.py#L189)
**Risk**: refresh token が revoke されると 30 分ごとに ERROR ログのみ、`ensure_valid_token()` は古い token を返し続けて 401 連発まで気づけない。
**Fix**: refresh 3 連続失敗で startup healthcheck 失敗扱い → /health が degraded を返すように。

---

## 🟠 High

### SRE-H1. `bot.tree.sync()` が reconnect 毎に発火
**Location**: [bot.py:280](bot.py#L280)
**Risk**: Discord global command sync は 200 req/day rate limit あり。slash command がない現状は無駄な call。
**Fix**: `_synced` フラグで 1 回のみに gate。または slash command がないなら sync 自体を削除。

### SRE-H2. CI/CD 皆無
**Location**: `.github/` 不在
**Risk**: 手動 `git pull` + `systemctl restart` のみ。bad commit → 手動 revert で TTR ≥ 数分。test 自動実行なし。
**Fix**: GitHub Actions で test + lint + deploy（git pull + systemctl restart）。せめて `make deploy` script。

### SRE-H3. 構造化ログなし
**Location**: [main_async.py:17](main_async.py#L17), 全モジュール
**Risk**: plain text format、grep が文字列マッチ頼り。`user_id=1234` の event 横断追跡が偶然動いてるだけ。
**Fix**: `python-json-logger` or `structlog` 導入、`request_id = uuid4()` を on_message → call_with_tools → mem0 まで thread。

### SRE-H4. trace/correlation ID 皆無
**Location**: 全モジュール
**Risk**: `bot.py user=...` / `claude_sdk.py ch=...` / `mem0/ingest.py user=...` が独立、3 時間前のエラー再現に timestamp cross-ref 必要。
**Fix**: SRE-H3 と同時に request_id 導入。

### SRE-H5. Stream stall detection が `claude_api.py` のみ、SDK path に無し
**Location**: [services/claude_sdk.py:511](services/claude_sdk.py#L511)
**Risk**: SDK 内部 HTTP stream が stall すると `CLAUDE_TIMEOUT=1800s` 全消費するまで気づけない。
**Fix**: `async for msg in query(...)` を `asyncio.wait_for` で囲み、`STREAM_STALL_THRESHOLD_S=30` を都度 reset するロジックを移植。

### SRE-H6. npx MCP server 未 pin（registry-trust ギャップ）
**Location**: [config.py:262-300](config.py#L262)
**Risk**: `npx -y @upstash/context7-mcp` 等が pinning なし。registry compromise や silent update で MCP subprocess context 任意コード実行。`@playwright/mcp@latest` は明示 latest。
**Fix**: `--package pkg@x.y.z` 化、または `npm install -g` 後に binary path 直接参照。

### SRE-H7. anthropic SDK が requirements に明示されてない
**Location**: [requirements.txt](requirements.txt), [pyproject.toml](pyproject.toml)
**Risk**: `claude-code-sdk` の transitive 依存だけで anthropic version が決まる。bypassPermissions は SDK 概念で API ではないので、SDK 任意 upgrade で破壊的変更。
**Fix**: `anthropic>=x.y` を明示 floor、`claude-code-sdk==x.y.z` で exact pin。

### SRE-H8. 2GB RAM ピーク 1.9GB 接近、`MemoryMax=` 未設定
**Location**: [deploy/systemd/discord-bot.service](deploy/systemd/discord-bot.service)
**Risk**: 2 admin CLI + playwright MCP + browser daemon + ChromaDB 同時で ~1.93GB → kernel OOM killer が無作為対象を選ぶ（chromium sandbox の oom_score_adj 次第で systemd-journald が落ちる可能性も）。
**Fix**: `MemoryMax=1700M` を service ファイルに追加して cgroup 制御に固定。playwright MCP と browser daemon を mutex 化。

### SRE-H9. `requirements.lock` なし、Python 3.13 unpinned
**Location**: [requirements.txt](requirements.txt)
**Risk**: 全 dep が `>=x.y,<z.0` 範囲指定。新 VM での `pip install -r` は最新 minor を引く → mem0ai / chromadb 1.x の breaking change を踏む。`.python-version` ないので apt update で venv 破壊。
**Fix**: `pip-compile` で `requirements.lock` 生成、`.python-version` 追加。

### SRE-H10. `send_message` が client disconnect で cancel されない
**Location**: [web_api.py:486-491](web_api.py#L486)
**Risk**: Web UI で submit 直後にタブ閉じ → Claude API call は完走、token と quota を消費。
**Fix**: `asyncio.create_task` で wrap、`_sid_to_user[sid]["tasks"]` で track、disconnect handler で cancel。

### SRE-H11. attachment 11個送信で Discord rejected
**Location**: [bot.py:932-934](bot.py#L932)
**Risk**: `reply(..., files=[main] + attach_files[:10])` で最大 11 ファイル → Discord limit 10 → 400 Invalid Form Body。silent fail。
**Fix**: `attach_files` を `[:9]` で cap、または rendered images をグループ分割して複数 message に。

---

## 🟡 Medium

### SRE-M1. `/tmp/{mermaid,table,math}_renders` クリーンアップなし
[utils/attachments.py:528](utils/attachments.py#L528) - `cleanup_stale_attachments` は `ATTACHMENT_DIR` のみ。content-addressed PNG が永久蓄積。`MATH_RENDERS_DIR` は `.html` も削除されない。
**Fix**: 全 render dir も同じ 1h cleanup ループに含める。

### SRE-M2. JSONL 同時書き込みで破損リスク
[services/session.py:207-214](services/session.py#L207) - `O_APPEND` は 4KB 以下しか atomic 保証なし。長い assistant reply で `json.dumps` が PIPE_BUF 超 → 別 coroutine の write と interleave → `_tail_jsonl` で silent skip され data loss。
**Fix**: per-channel `asyncio.Lock` で append シリアライズ。

### SRE-M3. JSONL に `fsync` なし、partial-write を silent skip
[services/session.py:213-214](services/session.py#L213), [services/session.py:254-265](services/session.py#L254) - `_tail_jsonl` で `JSONDecodeError: continue` だが何が落ちたか log なし。
**Fix**: `flush + fsync` 追加、partial line skip 時に `log.warning("corrupted JSONL line: %s", e)`。

### SRE-M4. retry 増幅: per-channel budget (M5 で追加) と DEFAULT_MAX_ATTEMPTS 5 のミスマッチ
[services/claude_sdk.py:468](services/claude_sdk.py#L468) - 外側 loop が 4 周なのに `DEFAULT_MAX_ATTEMPTS=5` が dead code。M5 budget 3 が同 loop で発火するので最大 4 SDK attempt。設定値と実態が乖離。
**Fix**: `DEFAULT_MAX_ATTEMPTS` を loop range と整合、または使われてないなら削除。

### SRE-M5. Discord fetch_channel/fetch_member の rate limit 未対策
[web_api.py:209,235](web_api.py#L209) - `_check_channel_permission` で per-request fetch。1000 reconnect 雪崩で Discord 429。
**Fix**: `cachetools.TTLCache` で `(guild_id, user_id)` を 60s キャッシュ。

### SRE-M6. Mem0 store task が untrack で GC-killable
[web_api.py:538](web_api.py#L538), [bot.py:892](bot.py#L892) - `asyncio.create_task(_mem0_store())` の参照を保持せず → Python が weak-ref で持つだけなので、GC が早期に走ると silent cancel。
**Fix**: `WeakSet` で track、failure を WARNING で log。

### SRE-M7. ChromaDB lock 競合 — compact と書き込み
[scripts/weekly_mem0_compact.py:98](scripts/weekly_mem0_compact.py#L98) - `mem.get_all(limit=10000)` が live insert と被ると SQLite WAL writer 数秒 stall → MCP latency spike → Claude SDK 側 timeout。
**Fix**: bot 低トラフィック窓に cron 移動、もしくは ChromaDB を HTTP server mode に。

### SRE-M8. mem0ai CVE-2026-7597 未解消、期限なし
[requirements.txt:7](requirements.txt#L7) - inline TODO comment。期限切らないと無期限放置。
**Fix**: 月 1 で beta 互換性検証、期限付き issue 化。

### SRE-M9. segmented send (`_send_segmented`) で Discord 5/5s rate limit 接触
[bot.py:303-390](bot.py#L303) - 5 table + 5 math = 10 連続 send で per-channel send bucket 枯渇。discord.py は 429 で auto-retry するが per-channel semaphore が stall。
**Fix**: batch 化、もしくは 1 message に複数 attachment 集約。

### SRE-M10. Sessions schema migration 文書化なし
[services/session.py:111-112](services/session.py#L111) - H1 で composite key 化したが disk format は legacy のまま（互換維持）。rollback 時の挙動が文書化されてない。
**Fix**: [deploy/README.md](deploy/README.md) に1行追記。

### SRE-M11. external venv MCP の version untracked
- `/home/ubuntu/free-code/x-scraper/.venv/bin/python`, `/home/ubuntu/scholarly-mcp/.venv/bin/python` — 別 repo・別 venv で out-of-band。
**Fix**: `deps/external-mcps.md` で期待 version 明記、startup で version 確認。

### SRE-M12. ChromaDB 1.x が `<2.0` のみ pin
[requirements.txt](requirements.txt) - 1.x 内 minor で API churn 既往。
**Fix**: lock file で freeze。

### SRE-M13. weekly_mem0_compact.py に flock なし
[scripts/weekly_mem0_compact.py](scripts/weekly_mem0_compact.py) - 現状 read-only で実害なしだが将来 write 拡張時に race。
**Fix**: 防御的に flock 入れる。

### SRE-M14. PII 漏えい多数点
[bot.py:398](bot.py#L398), [utils/attachments.py:418,380,395](utils/attachments.py#L418), [web_api.py:321](web_api.py#L321), [services/claude_sdk.py:539](services/claude_sdk.py#L539) - message content / attachment filename / CDN URL / discord_id / tool inputs が log に。
**Fix**: `LOG_MESSAGE_CONTENT=false` env gate、CDN URL の query param strip、`_redact_secrets` 拡張。

### SRE-M15. Admin 認証 bypass attempt が INFO 止まり
[bot.py:424](bot.py#L424), [web_api.py:213](web_api.py#L213) - 権限拒否時に WARNING ログがない経路あり。
**Fix**: 全 permission deny path で WARNING 統一。

### SRE-M16. DEFAULT_USER_ID が 2 ファイルでハードコード
[mcp_mem0_server.py:73](mcp_mem0_server.py#L73), [scripts/weekly_mem0_compact.py:42](scripts/weekly_mem0_compact.py#L42) - 同期ズレで orphan 記憶発生リスク。
**Fix**: [config.py](config.py) に集約、env で override。

---

## 🔵 Low / Info

- **SRE-L1**: cooldowns/quota が in-memory only。restart で `COOLDOWN_SECONDS` 分の quota bypass 窓ができる。
- **SRE-L2**: `_session_timestamps` in-memory only。restart 後の最初の message は必ず fresh session（無駄な token）。
- **SRE-L3**: 例外パスで `status_msg = "⏳ 考え中..."` が orphan する ([bot.py:843 vs 956](bot.py#L843))。
- **SRE-L4**: Bot presence/activity 未設定。`discord.Game("Claude SDK")` で ops が「online but unhealthy」を識別できるように。
- **SRE-L5**: `bot.tree.sync()` が 0 command を sync。slash 不在なら呼び自体削除。
- **SRE-L6**: prometheus metrics 皆無。最低限 `claude_call_duration_seconds` / `claude_call_cost_usd_total` / `mem0_search_duration_seconds` / `rate_limit_blocks_total` / `oauth_refresh_total` の 5 系統だけでも入れたい。
- **SRE-L7**: render_semaphore.py が dead code。削除 or browser_pool に組み込み。
- **SRE-L8**: `LimitNOFILE=` 未設定。現状 200-250 FD で 1024 デフォルトに余裕あり。
- **SRE-L9**: swap 未設定の VPS だと zero buffer。OOM 即死だが、swap thrash よりは良い。
- **SRE-L10**: BOT_KEYWORDS hot-reload なし。`.env` 変更で再起動必須を runbook 化。
- **SRE-L11**: SIGHUP handler なし、config reload なし。`ExecReload=` を placeholder で持つだけでも。
- **SRE-L12**: Discord API v10 deprecation 監視なし。discord.py 3.x への upgrade パス。
- **SRE-L13**: playwright `@latest` で Chromium が浮動。
- **SRE-L14**: Disaster Recovery checklist 不足（[deploy/README.md](deploy/README.md)）。VM wipe 後の復旧手順、ChromaDB path、credentials restore など追記。

---

## 推奨対応順

### 即時（本番運用前 must）
1. SRE-C1 SIGTERM handler
2. SRE-C2 StartLimitBurst
3. SRE-C3 sessions.json atomic write
4. SRE-C4 backup cron
5. SRE-C7 /health endpoint
6. SRE-C8/C9 Mem0/OAuth 失敗の log level + alert

### 早期（1-2 週間）
7. SRE-C5 status_callback safe wrapper
8. SRE-C6 verify_session lock
9. SRE-H1 sync once
10. SRE-H5 stream stall detection
11. SRE-H6 npx MCP pin
12. SRE-H8 MemoryMax=
13. SRE-H10 send_message cancel
14. SRE-H11 attachment count cap

### 中期（1 ヶ月）
15. SRE-H2 CI/CD
16. SRE-H3/H4 structured logging + request_id
17. SRE-H7/H9 lock file + pin anthropic
18. Medium 系一括

### 長期
19. Low/Info を順次
20. monitoring stack（Prometheus exporter + Grafana）の構築

---

## 出口基準（本番投入チェックリスト）

- [ ] systemd unit に `StartLimitBurst=3` + `MemoryMax=1700M`
- [ ] sessions.json と JSONL の atomic write 確認
- [ ] `.state/` + ChromaDB の週次バックアップ動作確認
- [ ] /health endpoint 設置、外形監視追加
- [ ] OAuth refresh failure と Mem0 store failure が WARNING で log されてること
- [ ] requirements.lock commit
- [ ] npx MCP server の version pin
- [ ] 2GB RAM での peak scenario 実機検証（playwright + browser daemon + ChromaDB 同時）
- [ ] runbook（deploy / rollback / DR）整備
