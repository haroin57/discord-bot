# 残作業: 2026-05-13 セキュリティ監査の積み残し

**生成日**: 2026-05-13
**ブランチ**: `worktree-security-remediation` (master へ merge 予定)

10 並列エージェントで Critical 4 + High 12 + Medium 18 = 34 findings の修正を適用済。本書は merge 後に対応が必要な残作業を列挙する。

---

## 1. test_e2e_security.py の 13 件テスト更新（コード側 OK、テスト側が古い仕様）

セキュリティ強化で意図的に変えた挙動について、テストが古い期待値を持っているため失敗している。**コード側は正しい**ので、テストを新しいポリシーに合わせて更新する。

### 失敗テスト一覧

| クラス / テスト | 失敗原因 | 修正方針 |
|---|---|---|
| `TestPromptInjectionDefense::test_attack_payloads_neutralized` (6 parametrize) | C4 の `cap_text` 前置化で prompt 構造が変わった | assert 対象を「`<untrusted_user_input>` block 内にペイロードが封じ込められる」に変更 |
| `TestPromptInjectionDefense::test_nonadmin_header_does_not_claim_admin` | 同上 | header 出力位置の assert を新構造に合わせる |
| `TestPromptInjectionDefense::test_admin_request_not_sanitized` | 同上 | 同上 |
| `TestAdminVsRestricted::test_nonadmin_is_restricted` | H3 で `allowed_tools=[]` に統一 | 新挙動の assert に変更 |
| `TestAdminVsRestricted::test_admin_is_unrestricted` | MagicMock の `total_cost_usd` formatting エラー（既存 infra 問題） | mock に numeric 値返す `total_cost_usd=0.0` 設定 |
| `TestRestrictedSdkOptions::test_restricted_with_image_allows_read_only` | H3 で `Read` も非許可になった（仕様変更） | テスト名/挙動を反転: `test_restricted_with_image_has_no_tools` に変更し `allowed_tools == []` を assert |
| `TestRestrictedSdkOptions::test_admin_options_allow_full_access` | MagicMock formatting | 同上 mock 修正 |
| `TestMem0WriteGating::test_admin_does_write_memory` | 同上 | 同上 |

### 着手手順

```bash
cd /home/ubuntu/discord-bot
git checkout master
/home/ubuntu/discord-bot/.venv/bin/pytest tests/test_e2e_security.py -x --tb=short 2>&1 | head -80
# 失敗するテストを 1 件ずつ読んで、新しいセキュリティ仕様に合わせて assert を書き換える
```

---

## 2. test_e2e_security.py に元から存在した既存壊れテスト（監査前から失敗）

監査タスク開始前の初期 test run で既に失敗していたもの。私の変更とは無関係。

- `TestRateLimiting::test_nonadmin_cooldown_is_stricter`
- `TestRateLimiting::test_admin_has_lax_cooldown`
- `TestRateLimiting::test_nonadmin_daily_quota_enforced`
- `TestRateLimiting::test_admin_not_quota_capped`
- `TestPromptSizeCap::test_user_message_capped`
- `TestPromptSizeCap::test_final_prompt_capped`
- `TestCallDirectAttribution::test_attribution_not_in_system_prompt`

ただし `TestRateLimiting` 系 4 件は H6 (shared rate_limit module) の追加で挙動が変わる可能性がある。merge 後にもう一度確認、`services/rate_limit.py` の API に合わせて assert を更新する余地あり。

```bash
# 状況確認
/home/ubuntu/discord-bot/.venv/bin/pytest tests/test_e2e_security.py::TestRateLimiting -v
```

---

## 3. test_bot.py / test_claude_sdk.py / test_coverage_improvements.py / test_claude_api.py の既存壊れテスト

監査前から M 状態だったテストファイル群。`tests/test_bot.py::TestOnMessage` の `channel_busy` AttributeError 等、本作業範囲外。コード側を直すか、当該 attribute を bot.py に戻すかの判断は別途。

```bash
/home/ubuntu/discord-bot/.venv/bin/pytest tests/test_bot.py tests/test_claude_sdk.py tests/test_coverage_improvements.py tests/test_claude_api.py -v 2>&1 | grep -E "FAILED|ERROR" | head -30
```

---

## 4. **C1 — Discord bot token rotation（先輩の手動対応）**

監査で `scripts/toeic-reminder.sh.bak.20260508` に live token を検出。`worktree-security-remediation` ブランチには元から無いが、master の作業ツリーに残っている可能性あり。

### 手順

1. **Discord Developer Portal で token reset**: <https://discord.com/developers/applications> → Kanae bot → Bot → Reset Token
2. 新トークンを `/home/ubuntu/discord-bot/.env` の `DISCORD_TOKEN=` に書き込む
3. `chmod 600 /home/ubuntu/discord-bot/.env`（C2 と同時に実施）
4. `find /home/ubuntu/discord-bot -name '*.bak.*' -o -name '*.bak'` で残存ファイルを確認
5. 残ってたら削除: `rm <path>`
6. `systemctl restart discord-bot`（systemd 起動後）

---

## 5. **C2 — .env パーミッション 0600（先輩の手動対応）**

```bash
chmod 600 /home/ubuntu/discord-bot/.env
stat -c %a /home/ubuntu/discord-bot/.env   # 600 を確認
```

bot 起動時に自動検証する仕組みは未実装。手動で確認する。

---

## 6. systemd unit + cron 実機適用（先輩の手動対応）

`deploy/systemd/discord-bot.service` と `deploy/cron/run-iran-monitor.sh` を生成済。実機への適用は手動。

### 手順

```bash
# systemd
sudo cp deploy/systemd/discord-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now discord-bot
sudo systemd-analyze security discord-bot.service   # Exposure level: MEDIUM 以下を期待

# cron
crontab -e
# 既存の iran-monitor 行を以下に置き換え:
# 0 9 * * * /home/ubuntu/discord-bot/deploy/cron/run-iran-monitor.sh
```

---

## 7. Codex レビュー対応分との重複確認

`worktree-security-remediation` 上では Codex レビュー（`docs/2026-05-13-security-review-and-remediation.md`）の対応分も既に入っている:

- Web API channel permission checks
- WEB_API_HOST default 127.0.0.1
- isAdmin re-derive from ADMIN_USER_IDS
- admin attachment basename denylist
- admin resume untrusted wrap
- linked attachment host allowlist
- mem0ai CVE doc

master へ merge 後、これらの diff が既に master の作業ツリーに残っていないか確認:

```bash
cd /home/ubuntu/discord-bot
git diff HEAD -- web_api.py config.py utils/attachments.py | head -50
```

---

## 8. session.py 複合キー化に伴うセッション state クリア

H1 で `sessions` の key が `int` → `(int, int | None)` に変更。既存 `.state/sessions.json` は legacy `None` バケットに自動マイグレーションされるが、新 channel/user 組み合わせは別物として扱われる。

実機初回起動時は問題ないが、もし古い state を引き継ぎたくない場合は:

```bash
mv /home/ubuntu/discord-bot/.state/sessions.json /home/ubuntu/discord-bot/.state/sessions.json.bak
```

---

## 9. PR 分割（オプション）

現在は `worktree-security-remediation` に全部入りの大 PR。Codex レビュー指示通り領域別 6 PR に分割するなら、merge 後に master 上で `git log --oneline` を確認し、commit を logical group ごとに `git rebase -i` で... と言いたいところだが **rebase -i は CLAUDE.md で禁止**。

代替: 各領域別に新ブランチを切って `git cherry-pick` で commit を選別する。

```bash
# 例: prompt_builder hardening を別 PR にする
git checkout -b pr-prompt-builder master
git cherry-pick <prompt_builder commit hash>
```

---

## 10. Low 11 件（別 plan で順次）

Low severity 11 件は本作業のスコープ外。`docs/2026-05-13-full-codebase-security-audit.md` の Low セクションを参照。

主な内容:
- L1 main_async 起動エラーの .env path hint
- L2 tool_input_hint の絶対パスリーク
- L3 GITHUB_PERSONAL_ACCESS_TOKEN の MCP dict 在留
- L4 `strip_mention` の `count=1`
- L5 `bot.log` の世界読み取り可能性
- L6 `DEFAULT_USER_ID` ハードコード
- L7 iran-monitor の HTTP エラーボディログ欠落
- L8 `.attachignore` の hidden credential path 追加
- L9 dgm/llm.py の bypassPermissions（DGM 削除済なので自然解消）
- L10 `settings.json` の `deny` 空 / `mcp__happy` legacy entry
- L11 bot.py:393 の DEBUG 出力 PII

---

## verification まとめ

```bash
cd /home/ubuntu/discord-bot

# 私の変更分（新規/更新テスト）が全 pass することを確認
/home/ubuntu/discord-bot/.venv/bin/pytest \
  tests/test_attachments.py tests/test_web_api.py tests/test_session.py \
  tests/test_sentinel.py tests/test_rate_limit.py tests/test_mem0_extract_format.py \
  tests/test_mem0_search.py tests/test_discord_helpers.py -q

# DGM 残骸ゼロを確認
rg "from dgm|import dgm|DGM_|load_extension\([\"']dgm[\"']\)|get_dgm_client" --type py

# 新 deploy/ 構造を確認
ls -la deploy/systemd/discord-bot.service deploy/cron/run-iran-monitor.sh deploy/README.md
```
