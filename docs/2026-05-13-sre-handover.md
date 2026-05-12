# Operator Handover — SRE Remediation PR (2026-05-13)

---

## 1. 手動手順（PR では自動化されていない）

- **Token rotation** — `docs/2026-05-13-security-handover.md` C1 を参照（Discord Developer Portal → Reset Token → .env 更新 → restart）
- **`.env` 権限**: `chmod 600 /home/ubuntu/discord-bot/.env`
- **systemd unit インストール**:
  ```bash
  sudo cp deploy/systemd/discord-bot.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl restart discord-bot
  ```
- **cron エントリ追加** (`crontab -e`):
  ```
  0 4 * * 0 /home/ubuntu/discord-bot/scripts/backup_state.sh          # SRE-C4 バックアップ
  0 9 * * 0 /home/ubuntu/discord-bot/scripts/weekly_mem0_compact.py   # 既存 Mem0 compact
  0 9 * * * /home/ubuntu/discord-bot/deploy/cron/run-iran-monitor.sh  # iran-monitor（既存文書参照）
  ```
- **バックアップ初回確認**:
  ```bash
  bash /home/ubuntu/discord-bot/scripts/backup_state.sh
  ls ~/.local/state/discord-bot/backups/
  ```
- **`/health` 疎通確認**: `curl 127.0.0.1:3002/health | jq`
- **systemd ハードニング確認**: `systemd-analyze security discord-bot.service`（Exposure ≤ MEDIUM を期待）

---

## 2. PR が変更した内容（Critical + High 修正）

- **SRE-C1** — `main_async.py` に `SIGTERM` ハンドラを追加し、終了時に in-flight Claude subprocess を kill する
- **SRE-C2** — `discord-bot.service` に `StartLimitBurst=3 StartLimitIntervalSec=60s` を追加してクラッシュループを 3 回で打ち切る
- **SRE-C3** — `session.py` のセッション保存を tmp → `fsync` → `os.replace()` の atomic write に変更
- **SRE-C4** — `scripts/backup_state.sh` を新規追加し `.state/` と ChromaDB を rclone で週次 Google Drive バックアップ
- **SRE-C5** — `claude_sdk.py` の `status_callback` 呼び出しを `_safe_status` ラッパで包み、例外が retry ループに漏れないようにした
- **SRE-C6** — `web_api.py` の `verify_session` に per-token `asyncio.Lock` を追加し、同時 reconnect fanout を防止
- **SRE-C7** — `web_api.py` に `/health` エンドポイントを追加（`discord_connected / mem0_ok / uptime_s / last_claude_call_ts` を返す）
- **SRE-C8** — Mem0 store 失敗のログを `DEBUG` → `WARNING` に昇格、連続失敗で `ERROR` にエスカレート
- **SRE-C9** — OAuth refresh 3 連続失敗で `/health` が `degraded` を返すよう変更
- **SRE-H1** — `bot.py` の `tree.sync()` を `_synced` フラグで初回のみに限定
- **SRE-H3/H4** — `python-json-logger` 導入、全モジュールに `request_id` (`rid=XXXX`) を付与した構造化 JSON ログ
- **SRE-H5** — `claude_sdk.py` の `async for` ループに `asyncio.wait_for` でストリーム失速検出を追加
- **SRE-H6** — `config.py` の `npx` MCP 呼び出しを `--package pkg@x.y.z` 形式でバージョン固定
- **SRE-H7** — `requirements.txt` に `anthropic>=x.y` floor と `claude-code-sdk==x.y.z` exact pin を追加
- **SRE-H8** — `discord-bot.service` に `MemoryMax=1700M` を追加して cgroup でメモリ上限を設定
- **SRE-H10** — Web API の `send_message` を `asyncio.create_task` で管理し、disconnect 時にキャンセル
- **SRE-H11** — `bot.py` の attachment リストを `[:9]` で上限 9 に制限（Discord 10 枚制限に対して安全マージン）

---

## 3. デプロイ後検証チェックリスト

- [ ] `journalctl -u discord-bot --since "5 min ago"` に `ERROR` がないこと
- [ ] `curl 127.0.0.1:3002/health | jq` で `"status": "ok"` が返ること
- [ ] メッセージを送信し、ログに `rid=XXXX` が JSON 形式で出力されること
- [ ] `sudo systemctl restart discord-bot` 後に `ps auxf | grep node` で孤立 `node` プロセスがないこと（SIGTERM ハンドラ確認）
- [ ] `.env` を一時的に壊して起動し、systemd が 3 回リトライ後に停止すること（`StartLimitBurst` 確認）
- [ ] `systemctl status discord-bot | grep Memory:` で `MemoryMax=1700M` が表示されること

---

## 4. 未対応フォローアップ

### Medium 16件（計画的対応）

- M1: `/tmp/{mermaid,table,math}_renders` に 1h cleanup を追加
- M2: JSONL 同時書き込みに per-channel `asyncio.Lock` を追加
- M3: JSONL に `fsync`、partial line skip 時に `log.warning`
- M4: `DEFAULT_MAX_ATTEMPTS` とループ範囲の整合
- M5: `_check_channel_permission` に 60s TTL キャッシュ追加
- M6: Mem0 store task を `WeakSet` で追跡し早期 GC を防止
- M7: `weekly_mem0_compact.py` の cron を低トラフィック窓に移動、または ChromaDB を HTTP server mode に
- M8: `mem0ai` CVE-2026-7597 — 月次 beta 検証スケジュール化
- M9: segmented send の batch 化またはバースト制限
- M10: sessions.json schema migration を `deploy/README.md` に記載
- M11: 外部 venv MCP のバージョンを `deps/external-mcps.md` で追跡
- M12: `requirements.lock` で ChromaDB 1.x を freeze
- M13: `weekly_mem0_compact.py` に防御的 `flock` 追加
- M14: ログへの PII 漏えい（message content / CDN URL / user ID）を `LOG_MESSAGE_CONTENT=false` gate で制御
- M15: 全 permission deny パスで `WARNING` ログを統一
- M16: `DEFAULT_USER_ID` を `config.py` に集約し env override 可能に

### Low / Info 14件（余力で）

- L1: cooldown/quota を in-memory 以外で永続化
- L2: `_session_timestamps` の永続化（restart 後の fresh session 問題）
- L3: 例外パスで orphan する `⏳ 考え中...` ステータスメッセージ
- L4: Bot presence/activity 設定（`discord.Game` で health 可視化）
- L5: slash command がないなら `tree.sync()` 呼び自体を削除
- L6: Prometheus metrics 最低 5 系統追加（`claude_call_duration_seconds` 等）
- L7: `render_semaphore.py` dead code を削除または `browser_pool` に統合
- L8: systemd unit に `LimitNOFILE=` を設定
- L9: swap 未設定 VPS での OOM 対策検討
- L10: `BOT_KEYWORDS` hot-reload runbook 化
- L11: `SIGHUP` ハンドラ追加、`ExecReload=` placeholder 設定
- L12: Discord API v10 deprecation 監視、discord.py 3.x upgrade パス検討
- L13: `@playwright/mcp@latest` をバージョン固定
- L14: DR checklist 強化（VM wipe 後の ChromaDB / credentials 復旧手順）
