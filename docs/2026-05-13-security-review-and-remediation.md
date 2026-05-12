# Discord Bot セキュリティ詳細レビューと修正案

**作成日**: 2026-05-13  
**前提**: DGM 関連実装は本番運用から削除する。  
**目的**: Discord で実運用する前提で、現在のコードベースに残るセキュリティリスクと修正方針を整理する。

---

## 概要

今回のレビューでは、Discord bot を実際に guild / Web UI / admin Claude mode と組み合わせて運用する前提で、攻撃経路を確認した。

最大の結論は次の 3 点。

1. **DGM は削除前提でよい**  
   DGM は生成コード実行、疑似 sandbox、slash command からのコード露出、DGM 専用 Claude bypass 実行を含むため、本番 bot と同居させるにはリスクが大きい。削除すれば、この領域の主要リスクはまとめて解消できる。

2. **DGM 削除後も Web API の権限設計は要修正**  
   Web API は Socket.IO 接続時に session token を検証するが、接続後の `channel_id` 操作に Discord 権限チェックがない。`main_async.py` で本番起動した場合、任意 channel の履歴取得や送信処理に到達し得る。

3. **admin Claude mode と自動ファイル添付は、prompt injection を前提に締め直す必要がある**  
   admin mode は強い tool 権限を持ち、応答中のファイルパスを自動添付する。未信頼 Discord テキストから秘密ファイルの添付につながる経路を閉じるべき。

---

## レビュー結果

### Critical: Web API が Discord チャンネル権限を検証していない

**対象**

- `web_api.py`
- `main_async.py`
- `tests/test_web_api.py`

**確認した実装**

- `web_api.py` の `start()` は `web.TCPSite(..., "0.0.0.0", port)` で bind する。
- `join_channel`, `get_history`, `send_message` はクライアントから渡された `channel_id` を受け取る。
- Socket.IO 接続時の session token 検証はあるが、個別 channel に対する Discord 側の `view_channel`, `read_message_history`, `send_messages` は検証していない。
- `join_channel` は認証済み sid かどうかの確認すらせず room に参加させる。
- 現行テストも、権限チェックなしで成功する挙動を前提にしている。

**想定される悪用経路**

1. 攻撃者が有効な Web session token を得る。
2. Discord 上では見えない channel ID を指定して `get_history` を呼ぶ。
3. JSONL 履歴が存在すれば取得できる。
4. `join_channel` で任意 channel room に参加し、他 Web UI tab 向け broadcast を盗聴できる。
5. `send_message` で任意 channel ID の Claude session を進められる。

**補足**

調査時点では `:3002` の外部 LISTEN は確認されなかった。したがって即時露出ではなく、`main_async.py` を本番起動した場合に顕在化する設計リスクとして扱う。

**修正案**

- Web API の bind host を設定化し、default を `127.0.0.1` にする。
- `channel_id` ごとに Discord channel を解決し、対象 user の権限を検証する共通 helper を追加する。
- `join_channel` は read 権限がない場合 room join させない。
- `get_history` は read 権限がない場合 JSONL を読まない。
- `send_message` は send 権限がない場合 Claude を呼ばない。
- `tests/test_web_api.py` は「権限なし成功」から「権限なし拒否」へ期待値を反転させる。

---

### Critical: DGM の疑似 sandbox は sandbox として不十分

**対象**

- `dgm/`
- `dgm_runner.py`
- `bot.py`
- `config.py`
- `services/claude_api.py`
- `tests/test_dgm.py`
- `tests/test_dgm_engine.py`

**確認した実装**

- `dgm/sandbox.py` は AST で import 文だけを制限する。
- 実行は `asyncio.create_subprocess_exec(sys.executable, script_path, ...)` で、bot と同じ OS ユーザーの Python subprocess として動く。
- `open()`, `__import__()`, `eval`, `exec`, builtins 経由の参照は import check では止まらない。
- CPU / memory limit はあるが、ファイル読み書きや bot ユーザー権限での side effect は止められない。
- `dgm/llm.py` は Claude Code SDK を `permission_mode="bypassPermissions"` で呼ぶ。
- `/dgm start` と `/dgm stop` は admin check があるが、`status`, `best`, `population`, `lineage`, `code`, `archive` は admin check がない。

**想定される悪用経路**

- 生成コードが `/home/ubuntu/.claude/.credentials.json`, `.env`, GitHub credential など bot ユーザーが読めるファイルへアクセスする。
- 生成コードがファイルシステムへ副作用を起こす。
- 非 admin が `/dgm code` などで生成コードや内部状態を閲覧する。

**修正案**

DGM は本番 bot から完全削除する。

- `dgm/` package を削除する。
- `dgm_runner.py` を削除する。
- `bot.py` の `bot.load_extension("dgm")` を削除する。
- `config.py` の `DGM_*` 設定を削除する。
- `services/claude_api.py` の DGM 専用 client cache を削除する。
- `tests/test_dgm.py` と `tests/test_dgm_engine.py` を削除する。
- `pyproject.toml` の DGM 関連コメントや test marker を整理する。

---

### High: admin 自動ファイル添付が秘密情報漏洩経路になる

**対象**

- `utils/attachments.py`
- `bot.py`
- `.attachignore`

**確認した実装**

- admin mode ではモデル応答中のファイルパスを抽出し、Discord に自動添付する。
- `ADMIN_FILE_DIRS` は `/home/ubuntu` と `/tmp` を許可している。
- `.attachignore` は repo 配下や `.env` などを一部ブロックするが、hidden credential すべてを網羅していない。
- 例として `/home/ubuntu/.claude/.credentials.json` は bot ユーザーから読める権限で存在し、`.attachignore` の `credentials.json` とは一致しない可能性がある。

**想定される悪用経路**

1. 非 admin が Discord に prompt injection 文字列を投稿する。
2. admin がその文脈を含む channel / thread で bot に依頼する。
3. Claude が秘密ファイルの path を応答に含める。
4. bot が admin mode の自動添付処理でファイルを Discord に添付する。

**修正案**

- `ADMIN_FILE_DIRS` から `/home/ubuntu` を外す。
- 添付可能ディレクトリを `/tmp/discord-bot-artifacts` のような専用成果物ディレクトリだけに限定する。
- `.attachignore` に加えてコード側 denylist を追加する。
- denylist 例:
  - `.env`
  - `.credentials.json`
  - `credentials.json`
  - `hosts.yml`
  - `id_rsa`
  - `*.pem`
  - `*.key`
  - `*.token`
  - `*.sqlite`
  - `*.db`
- 可能なら admin でも自動添付を廃止し、明示的な confirm step または専用 command に寄せる。

---

### High: admin Claude mode の権限が強すぎる

**対象**

- `services/claude_sdk.py`
- `bot.py`
- `.claude/settings.json`

**確認した実装**

- admin mode は `permission_mode="bypassPermissions"` で動く。
- `cwd` は `/home/ubuntu` を指す。
- `.claude/settings.json` を継承する。
- 初回 prompt は trusted header と untrusted wrapper を使うが、resume path は lean prompt として新規入力を渡す。

**想定される悪用経路**

- admin が非 admin の投稿、返信、forwarded message、message link、text attachment を含む文脈で bot に依頼する。
- 未信頼テキストが admin Claude session に入り、tool 実行やファイル参照に影響する。
- 自動ファイル添付と組み合わさると、秘密ファイル exfiltration につながる。

**修正案**

- admin resume path でも user text / reply / attachment text を `<untrusted_user_input>` 相当で包む。
- admin mode の `cwd` を repo root または専用 workspace に絞る。
- `bypassPermissions` を維持する場合でも destructive / file / network 系 tool の denylist を明示する。
- MCP は lazy selection を維持しつつ、記憶削除や外部送信系 tool は明示要求時だけ有効化する。

---

### Medium: Web API が verifier の `isAdmin` をそのまま信頼している

**対象**

- `web_api.py`

**確認した実装**

- Next.js `/api/session/verify` の返す `isAdmin` が、そのまま Web API の admin 判定に使われる。
- bot 側にも `ADMIN_USER_IDS` があるため、admin 判定の source of truth が分散している。

**リスク**

- Next.js verifier の不具合や設定ずれが、bot の admin 権限に波及する。

**修正案**

- verifier からは `discordId` だけを信頼する。
- admin 判定は bot 側 `ADMIN_USER_IDS` で再計算する。
- verifier の `isAdmin` はログ用参考値に留めるか削除する。

---

### Medium: message link 添付 download の host allowlist が抜けている

**対象**

- `utils/discord_helpers.py`

**確認した実装**

- 通常添付 download には host allowlist がある。
- message link 解決時の text attachment download は `attachment.url` を直接 `aiohttp` で取得する。
- `allow_redirects=False` とサイズ制限はあるが、防御層が通常添付側と揃っていない。

**修正案**

- linked attachment download でも既存の host allowlist helper を使う。
- `Content-Length` と streaming read cap を通常添付側と同じ方針に寄せる。

---

### Medium: `mem0ai` CVE が残存している

**確認結果**

`pip-audit --strict` で以下が残っている。

```text
Name   Version ID            Fix Versions
------ ------- ------------- ------------
mem0ai 1.0.11  CVE-2026-7597 2.0.0b2
```

**修正案**

- `mem0ai==2.0.0b2` を別 branch で互換性検証する。
- stable fix 待ちで据え置く場合は、理由、確認日、影響範囲を docs または TODO に残す。

---

## DGM 削除で解消するリスク

DGM を完全削除すると、次のリスクは直接解消する。

- 生成コードを bot と同じ OS ユーザーで実行するリスク。
- AST import check だけの疑似 sandbox を sandbox と誤認するリスク。
- DGM 生成コードがローカル secret や credential にアクセスするリスク。
- `/dgm code` などで生成コードや内部状態が Discord に露出するリスク。
- DGM 専用 Claude Code SDK 呼び出しが `bypassPermissions` で動くリスク。
- DGM の SQLite DB や archive / lineage 情報を保守し続ける運用負荷。

削除後に確認すべきこと。

- `dgm/` package が存在しない。
- `dgm_runner.py` が存在しない。
- `bot.load_extension("dgm")` が残っていない。
- `DGM_*` config が残っていない。
- DGM 専用 tests が test collection から消えている。
- Discord slash command sync 後に `/dgm` group が消える。

---

## DGM 削除後も残る修正項目

優先度順に並べる。

1. **Web API channel 権限チェック**
   - Discord の channel permission を Web API 操作ごとに検証する。
   - bind host default を `127.0.0.1` にする。

2. **admin 自動ファイル添付の allowlist 縮小**
   - `/home/ubuntu` 許可を廃止する。
   - 専用 artifact dir のみ許可する。
   - hidden credential denylist を追加する。

3. **admin Claude resume prompt の untrusted wrapper 徹底**
   - admin resume path でも新規 user input を明示的に未信頼データとして扱う。

4. **Web API admin 判定の source of truth 統一**
   - `ADMIN_USER_IDS` で再計算する。

5. **linked attachment download の allowlist 適用**
   - 通常添付と同じ防御層に揃える。

6. **`mem0ai` CVE の扱い決定**
   - beta upgrade 検証または stable 待ちの明文化を行う。

---

## 実装方針

### 1. DGM 削除

削除対象。

- `dgm/`
- `dgm_runner.py`
- `tests/test_dgm.py`
- `tests/test_dgm_engine.py`

整理対象。

- `bot.py`
  - DGM extension load を削除する。
- `config.py`
  - `DGM_*` 設定を削除する。
- `services/claude_api.py`
  - DGM 専用 client cache と `get_dgm_client()` を削除する。
- `pyproject.toml`
  - DGM 関連コメントや marker があれば削除する。

### 2. Web API hardening

- `WEB_API_HOST` を追加し、default を `127.0.0.1` にする。
- Web API の start は `WEB_API_HOST` を使う。
- sid に紐づく Discord user id から guild member を解決する。
- 操作ごとに必要 permission を検証する。
- 権限がない場合は `error` event を返し、処理を中断する。

### 3. 自動ファイル添付 hardening

- admin file attachment の許可ディレクトリを専用 artifact dir に限定する。
- denylist をコード側にも持つ。
- path 抽出後、realpath、allowlist、denylist、size、regular file を順番に検証する。
- 将来的には自動添付ではなく明示 confirm に移行する。

### 4. admin Claude hardening

- admin resume path でも未信頼入力を wrapper に入れる。
- admin `cwd` を `/home/ubuntu` から狭める。
- dangerous tool は明示 deny または明示 opt-in にする。

### 5. 周辺防御の統一

- Web API admin 判定は `ADMIN_USER_IDS` を source of truth にする。
- linked attachment download に host allowlist を適用する。
- `mem0ai` CVE の対応方針を記録する。

---

## テスト計画

### Web API

対象: `tests/test_web_api.py`

追加・更新するシナリオ。

- 権限なし user は `join_channel` できない。
- 権限なし user は `get_history` できない。
- 権限なし user は `send_message` できず、Claude が呼ばれない。
- read 権限あり / send 権限なし user は history 取得のみ成功する。
- send 権限あり user は `send_message` できる。
- verifier が `isAdmin=True` を返しても、`ADMIN_USER_IDS` 外なら non-admin 扱いになる。

### Attachments

対象: `tests/test_attachments.py`

追加・更新するシナリオ。

- `/home/ubuntu/.claude/.credentials.json` は添付候補にならない。
- `/home/ubuntu/.config/gh/hosts.yml` は添付候補にならない。
- 専用 artifact dir 配下の通常ファイルだけ添付候補になる。
- symlink escape は拒否される。
- denylist basename は許可ディレクトリ配下でも拒否される。

### Discord helpers

対象: `tests/test_discord_helpers.py`

追加・更新するシナリオ。

- linked text attachment は許可 host のみ download される。
- 不許可 host は filename placeholder のみになり、本文は inline されない。
- oversized attachment は読み込まれない。

### Claude SDK / E2E security

対象:

- `tests/test_claude_sdk.py`
- `tests/test_e2e_security.py`

追加・更新するシナリオ。

- admin resume path でも未信頼 user input が wrapper / sanitize される。
- restricted mode の既存防御が退行しない。
- admin mode の `cwd` と tool policy が期待値通りになる。

### DGM 削除確認

実行する確認。

```bash
rg "from dgm|import dgm|DGM_|bot.load_extension\\(\"dgm\"\\)" .
pytest
```

期待値。

- docs を除いて DGM import / config / load hook が残らない。
- test collection が DGM 不在で通る。

---

## 前提

- DGM は隔離運用せず、今回の方針では完全削除する。
- Web API は残す。ただし default localhost bind と Discord permission check を必須にする。
- admin の利便性より、Discord 経由の秘密情報漏洩防止を優先する。
- `mem0ai` は beta upgrade の互換性確認をしてから採用可否を決める。
- 既存の `docs/2026-05-13-perf-overhaul-and-handover.md` は変更しない。

