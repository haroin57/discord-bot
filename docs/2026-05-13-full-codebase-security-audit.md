# Discord Bot 全コードベース セキュリティ監査

**作成日**: 2026-05-13
**手法**: 10 並列 security-auditor エージェントで領域別走査
**前提**: 既存レビュー `docs/2026-05-13-security-review-and-remediation.md` の指摘事項（DGM 削除 / Web API channel 権限 / admin 自動添付 / admin resume wrapper / mem0ai CVE / Web API host bind）は対象外。あれらが触れていない領域に絞る。

## 重大度別サマリ

- Critical: 4 件 — 即対応必須（うち1件はトークン漏えい、即ローテーション）
- High: 12 件 — 本番運用前に必須
- Medium: 18 件 — 順次対応
- Low: 11 件 — 余力で対応

---

## Critical（即対応）

### C1. Discord Bot Token がリポジトリ作業ツリーに平文残存
**Location**: `scripts/toeic-reminder.sh.bak.20260508:7`
**Risk**: トークン `MTQ4OTE3...` がディスクに平文。`git add -f` 一回で履歴漏えい。
**Fix**:
1. **今すぐ** Discord Developer Portal で token reset
2. `rm scripts/toeic-reminder.sh.bak.20260508`
3. `find . -name '*.bak.*' -print` で他に残存していないか確認

### C2. `.env` のパーミッションが 0600 ではない
**Location**: `/home/ubuntu/discord-bot/.env`
**Risk**: `DISCORD_TOKEN`, `OPENAI_API_KEY`, `BRAVE_API_KEY` 等が同ホストの他プロセスから読める。
**Fix**: `chmod 600 /home/ubuntu/discord-bot/.env` → 起動時に `os.stat(.env).st_mode & 0o077` をチェックして 0 でなければ fail。

### C3. `<attached_file name="...">` 属性に閉じタグ注入が可能
**Location**: `services/prompt_builder.py:141-145`
**Risk**: filename に `"><injected_tag x="` を含めると XML 構造を破壊して trusted context に脱出できる。`sanitize` は `<`, `>`, `"` を落とさない。
**Fix**: filename を HTML escape（`<`, `>`, `"`, `&`）してから補間。もしくは `[<>"]` を含むファイル名を拒否。

### C4. `cap_text` が `<untrusted_user_input>` の中身を切り捨てる
**Location**: `services/prompt_builder.py:53-56, 176`
**Risk**: wrap 後に cap するため、limit が wrapper 内で truncate すると閉じタグが消えて wrapper が崩壊。以降の全テキストが untrusted 扱いから外れる。
**Fix**: cap を各 part に適用してから wrap、もしくは wrap 後の truncate でも閉じタグを再付与する。

---

## High（本番前必須）

### H1. Session が channel_id だけで keyed されており cross-user 流用可能
**Location**: `services/session.py:40-56`, `services/claude_sdk.py:394-408`
**Risk**: 同じ channel に複数ユーザーがいる場合、A が始めた session を B が resume できる。MCP コンテキスト・記憶を含む状態が漏れる。
**Fix**: `(channel_id, user_id)` 複合キー、もしくは複数 active user がいる channel では resume を拒否。

### H2. SDK stderr ログに API key が落ちる可能性
**Location**: `services/claude_sdk.py:583-590`
**Risk**: `.state/stderr_ch{channel_id}.log` が umask 依存のパーミッションで書き込まれ、Claude CLI の debug stderr には `ANTHROPIC_AUTH_TOKEN` がしばしば現れる。
**Fix**: `os.open(..., O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW, 0o600)` で書く。`sessions.json` のパターンと揃える。

### H3. 非 admin の `Read` ツールが絶対パスで RESTRICTED_CWD を回避
**Location**: `services/claude_sdk.py:362-372`
**Risk**: `cwd=RESTRICTED_CWD` は `Read` の絶対パス引数を制限しない。画像添付がある非 admin が `/home/ubuntu/discord-bot/.env` を読み出せる。
**Fix**: restricted モードでは `allowed_tools=[]` を必ず空に、画像は別途 base64 で prompt へ事前埋め込み。

### H4. Web API: leave_channel が認証チェックなし
**Location**: `web_api.py:315-320`（master 側、worktree でも修正未）
**Risk**: 未認証 sid（あるいは sid を推測した他クライアント）が任意ユーザーの room から該当 sid を退室させ得る。
**Fix**: 他イベントと同じく先頭で `if not self._sid_to_user.get(sid): return` ガード。

### H5. Web API: ログアウト時の session token 無効化なし
**Location**: `web_api.py:51, 105-108`
**Risk**: `_session_cache` の TTL が 300s。Next.js でログアウトしても最大 5 分間トークン有効。
**Fix**: `/logout` 経由で `invalidate_session(token)` を呼ぶ aiohttp route を追加、もしくは Next.js から内部 IPC でキャッシュ purge。

### H6. Quota double-spending（Discord と Web API でカウンタ分離）
**Location**: `web_api.py:53-55` vs `bot.py` module-level `_daily_quota`
**Risk**: 非 admin が Discord 経由で 30 件 + Web UI で 30 件、合計 60 件の API コール（quota 2 倍）。
**Fix**: `services/rate_limit.py` に singleton で集約し両 path で共有。

### H7. Mem0 MCP `delete` ツールが user_id 検証なし
**Location**: `mcp_mem0_server.py:212-220`
**Risk**: MCP 経由でメモリ UUID を渡すだけで削除可能。先輩名前空間のメモリを攻撃者が `list_all` で列挙→ `delete` 連打で全消去可能。
**Fix**: `user_id` 引数を必須化し、削除前にメモリ所有者と一致するか確認。

### H8. DM admin gate が `DMChannel` のみで `GroupChannel` を漏らす
**Location**: `bot.py:397`
**Risk**: 非 admin が bot を Group DM に追加すると admins-only gate を素通り。BOT_KEYWORDS が含まれれば guild 経路で非 admin Claude が呼べる。
**Fix**: `isinstance(message.channel, (discord.DMChannel, discord.GroupChannel))`

### H9. 横断スレッドコンテキスト `after_msgs` が permission check 無し
**Location**: `utils/discord_helpers.py:456-464`
**Risk**: `before_msgs` ループには `_requester_can_read` チェックがあるが `after_msgs` には無い。リンク解決時に閲覧権限のない admin 投稿が非 admin に漏洩。
**Fix**: 同じチェックを `after_msgs` にも適用、`break` ではなく `continue` で個別フィルタ。

### H10. KaTeX CDN フォールバックで任意 JS 実行
**Location**: `utils/math_renderer.py:129`
**Risk**: ローカル KaTeX が無いと `cdn.jsdelivr.net` を `--no-sandbox` Puppeteer で読込。CDN 改ざんや DNS hijack で任意 JS が full-trust browser context で動く（`file://` 経由で local FS アクセス可能）。
**Fix**: ローカル KaTeX 必須化、CDN フォールバック時は `RuntimeError`。

### H11. systemd ユニットが no-sandbox
**Location**: `/etc/systemd/system/discord-bot.service`
**Risk**: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`, `ProtectHome` 全て未設定。bot プロセス侵害 → `ubuntu` home 全体（SSH key, OAuth token）へアクセス可能。
**Fix**: 最低限
```
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/ubuntu/discord-bot /home/ubuntu/.local/state/discord-bot
```

### H12. iran-monitor cron が Claude tool 出力を Discord に転送
**Location**: `scripts/iran-monitor.sh:59-80`
**Risk**: brave/x-scraper の untrusted web 出力を Claude 経由で Discord に投稿。Indirect prompt injection で bot に追加コマンドを誘発できる可能性。
**Fix**: `--no-tools` または明示 tool allowlist。専用サービスアカウントで cron 実行。

---

## Medium

### M1. SENTINEL_RE / POWER_PHRASE_RE が Unicode 同形・ゼロ幅で bypass 可能
**Location**: `services/sentinel.py:35-40`
**Fix**: `unicodedata.normalize("NFKC", text)` を sanitize 前段で適用。zero-width chars (`[​-‏­]`) を除去。

### M2. Mem0 search rerank prompt injection
**Location**: `services/mem0/search.py:90-165`
**Risk**: 保存メモリが Haiku rerank prompt に未エスケープで再注入。`{}` での `KeyError` DoS と stored prompt injection の両面。
**Fix**: `query` を長さ制限 + `{`/`}` をエスケープ。candidates ブロックを untrusted データと明示する区切りで囲む。

### M3. MCP keyword smuggling via 合成 prompt
**Location**: `services/claude_sdk.py:378`, `config.py:364`
**Risk**: `pick_mcp_servers(raw_user_text or prompt)` の or fallback で、reply chain や thread context に紛れた untrusted な `playwright`, `記憶` などで HEAVY/MEMORY tier が誤起動。
**Fix**: `raw_user_text` のみを使用、fallback を削除。

### M4. settings.json の integrity 未検証
**Location**: `.claude/settings.json`
**Risk**: bot ディレクトリへの write が可能になるシナリオで attacker が `deny` を空にして tool 制限を解除できる。
**Fix**: 起動時に owner / mode を確認、`deny` に最低限 `Bash` 等を含める。

### M5. Retry 増幅で per-user quota 消費
**Location**: `services/claude_sdk.py:437-438, 600-602`
**Risk**: 非 admin が rate_limit を意図的に踏ませて `TRANSIENT_MAX_ATTEMPTS` 回再試行を強制、quota 浪費。
**Fix**: per-channel retry budget を短期 TTL カウンタで管理。

### M6. Web API CORS wildcard 防止なし
**Location**: `web_api.py:41-46`
**Risk**: 運用者が `WEB_API_CORS_ORIGINS=*` 設定すると credential check が無効化。
**Fix**: 起動時に `*` / 空文字を含む設定は `RuntimeError`。

### M7. Channel ID 列挙 via エラー文字列の差異
**Location**: `web_api.py:168-175 vs 195-199`
**Risk**: `"channel not found"` vs `"not a guild member"` で channel 存在を side-channel 検出。
**Fix**: 全失敗パスを `"permission denied"` に統一。

### M8. Mem0 が Web API `get_history` 経由で cross-user leak
**Location**: `web_api.py:344-350`, `services/session.py:206-208`
**Risk**: channel JSONL に過去 admin の assistant turn が含まれ、その中の `<retrieved_memories>` block が他ユーザーに見える。
**Fix**: history 返却時に `<retrieved_memories>` 系 block を strip。または Mem0 注入版と canonical transcript を分離保管。

### M9. Web API permission cache が role 変更を 300s 反映しない
**Location**: `web_api.py:105-108`
**Risk**: `isAdmin` を含めて 300s キャッシュ。`ADMIN_USER_IDS` を絞った直後でも 5 分間旧 admin が動く。
**Fix**: `discordId` のみキャッシュ、`is_admin` はリクエストごとに `ADMIN_USER_IDS` から再計算（既に worktree で部分実装済み、キャッシュ entry にも反映必要）。

### M10. BOT_KEYWORDS 部分文字列マッチ
**Location**: `config.py:35`, `bot.py` keyword判定
**Risk**: 短い keyword を env で設定するとほぼ全 guild メッセージが Claude へ流れる。プライバシー + コスト DoS。
**Fix**: 最低 3 文字を強制、可能なら word-boundary regex。

### M11. ADMIN_USER_IDS が malformed で silent 空集合に
**Location**: `config.py:38-40`
**Risk**: `"haroin,owner"` のような誤設定で admin 0 名。失敗が起動時に見えない。
**Fix**: `_raw_ids` 非空かつ parse 結果が空なら `RuntimeError`。

### M12. STATE_DIR / stderr ログのディレクトリ権限が umask 依存
**Location**: `config.py:66-68`, `services/claude_sdk.py:584-586`
**Fix**: `os.makedirs(..., mode=0o700, exist_ok=True)`、`os.open(..., O_NOFOLLOW, 0o600)`。

### M13. MCP server 起動パスの owner / mode 未検証
**Location**: `config.py:264-275`
**Risk**: `/home/ubuntu/free-code/x-scraper/.venv/bin/python` などが他ユーザー書込み可能だと bot プロセスで任意コード実行。
**Fix**: 起動時に各 MCP binary path の owner と mode を検証、失敗なら設定から除外。

### M14. image_paths が prompt に sanitize 無しで補間
**Location**: `services/prompt_builder.py:134-138`
**Risk**: パスに改行や sentinel テキストを含めれば trusted-context block を合成可能。
**Fix**: 各 path を `sanitize` 経由で補間、改行は明示的に reject。

### M15. format_memories の `_FACT_TEXT_BLOCKLIST` が POWER_PHRASE_RE を継承していない
**Location**: `services/mem0/format.py:15-23`
**Risk**: 過去に保存された power-phrase（`ignore previous instructions` 等）が `<retrieved_memories>` に素通り。
**Fix**: `_scrub_fact_text` 内で POWER_PHRASE_RE も適用。

### M16. ReDoS: build_mem0_queries / IMAGE_PATH_RE / FILE_PATH_RE
**Location**: `services/prompt_builder.py:69-83`, `utils/attachments.py:83-93`
**Fix**: 入力を事前 cap、`[\w./_-]{1,512}` のように上限付き quantifier。

### M17. Mem0 ingestion classifier `_classify_for_metadata` が XML breakout strip なし
**Location**: `services/mem0/extract.py:16-35`
**Risk**: ユーザー content の `}` 注入で classifier 出力 JSON を捏造、metadata を書き換え可能。
**Fix**: `_strip_extract_breakout_tags` を classify path にも適用、`{`, `}` を escape。

### M18. JSONL session ファイルパスに整数バリデーション無し
**Location**: `services/session.py:133-134`
**Risk**: 文字列 channel_id を渡す経路が将来できると path traversal。
**Fix**: `_history_file` 冒頭で snowflake range（10^15..10^19）を assert。

---

## Low

### L1. main_async 起動時の `.env` パスヒントが stdout
**Location**: `main_async.py:107-108` — `print` でファイル名を露出
**Fix**: DEBUG log level に降格。

### L2. tool_input_hint が絶対パスを Discord status へリーク
**Location**: `services/claude_sdk.py:284-314, 489-493`
**Fix**: dirname を除去してから `status_callback`。

### L3. GITHUB_PERSONAL_ACCESS_TOKEN が `MCP_SERVERS` dict に永続在留
**Location**: `config.py:422-428`
**Fix**: header の lazy resolve、env passthrough のみ。

### L4. `strip_mention` が `count=1` で 1 回しか除去しない
**Location**: `bot.py:463`
**Fix**: `count=1` を削除して全置換。

### L5. `bot.log` が world-readable
**Location**: ログ出力設定
**Fix**: ログファイル mode 0o600。

### L6. DEFAULT_USER_ID = "573489206240870410" のハードコード
**Location**: `mcp_mem0_server.py:73`
**Fix**: `.env` 経由で渡す、デフォルトを `None` にして必須化。

### L7. iran-monitor の HTTP エラーボディログ欠落
**Location**: `scripts/iran-monitor.sh:80`
**Fix**: 非 2xx 時に `-w "%{http_code}" -o file` でレスポンス body も保存。

### L8. `.attachignore` に `/home/ubuntu/.claude/*` などが含まれない
**Fix**: hidden credential path を明示的に追加（既に worktree のコード側 denylist で hidden 系はカバー済みだが defence-in-depth として）。

### L9. dgm/llm.py の `bypassPermissions` 起動が `mcp_servers={}` を渡していない
**Location**: `dgm/llm.py:38`
**Note**: DGM は削除予定なので本件は削除作業で自然解消。

### L10. `settings.json` の `deny` が空、`mcp__happy` は legacy 残骸
**Fix**: `deny` に最低限の禁止 tool を入れる、`mcp__happy` allow を除去。

### L11. bot.py:393 で message content を DEBUG ログに 200 文字出力
**Risk**: 本番で LOG_LEVEL=DEBUG にすると PII / トークンが log file に残る。
**Fix**: `log.isEnabledFor(logging.DEBUG)` ガード + 既知 token プレフィックス redaction。

---

## 優先対応順（推奨）

1. **即時（今日中）**: C1（token rotation + bak 削除）、C2（.env chmod 600）
2. **今週**: C3, C4, H1, H2, H3, H4, H7, H8（コードの脆弱性で運用前に塞ぐべきもの）
3. **本番運用前**: H5, H6, H9, H10, H11, H12 + Medium 群
4. **その後**: Low 群を順次

## メモ

- Codex のレビューでカバー済みの 6 項目（DGM 削除、Web API channel perm、admin 自動添付、admin resume wrap、mem0ai CVE、Web API host bind）は本書では扱っていない
- 既に worktree branch `worktree-security-remediation` で実装済の項目は本書の指摘範囲とは別
- DGM 関連の懸念（L9）は DGM 削除タスクで自然解消する
