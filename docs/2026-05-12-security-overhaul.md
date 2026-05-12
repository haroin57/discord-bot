# Discord Bot セキュリティ修正まとめ

**作業日**: 2026-05-12
**ひとことで**: 8 並列のセキュリティ監査エージェントが見つけた問題を、PR 8 本に分けて潰した。

---

## 何が問題だったか

監査で出た主な指摘：

- **致命的**: shell スクリプトに Discord bot の token がベタ書きされていて git にも push 済み
- **重大**: ライブラリの既知 CVE が 11 個、`/tmp` 系の TOCTOU、テーブル描画の XSS、Mem0 への prompt injection 経路
- **中程度**: 「Use the Read tool」という文字列を含むだけで非 admin がツール権限をもらえる、エラーメッセージで内部パスが Discord に漏れる、state ファイルが world-readable
- **軽め**: 依存に上限ピンなし、Discord ID がフルで log に出る、など

---

## 何をやったか（PR 8 本）

| PR | やったこと | ひとこと |
|----|-----------|----------|
| **PR1** secrets hygiene | shell から token 撤去、`.env` 化、`.gitignore` 強化、起動時の token validation 追加 | まず止血 |
| **PR2** CVE 更新 | `cryptography` / `urllib3` / `python-multipart` / `pytest` を CVE 修正版へ。全 dep に上限ピン | pip-audit を 11 → 1 件に |
| **PR3** エラー文の sanitize | 例外をそのまま Discord に返さず汎用文言に。log の Discord ID を末尾 4 桁だけに | サーバ側 log は full traceback 残す |
| **PR4** Mem0 prompt injection 対策 | XML タグ breakout 防止、`metadata` 上書きを allowlist 化、recall 時に sentinel を再 strip | extract / format / recall の三層防御 |
| **PR5** Renderer の HTML escape | table を `bleach` で sanitize、caption を `html.escape`、KaTeX を `trust:false` に | XSS in Puppeteer (--no-sandbox) を遮断 |
| **PR6** `/tmp` と state ファイル安全化 | `flock` 採用、log を `~/.local/state/discord-bot/` 配下に、書き込みを `0o600` + `O_NOFOLLOW` | 既存ファイルにも `fchmod` で遡及適用 |
| **PR7** ツール権限の引き締め | Read tool 許可を「prompt の文字列」ではなく明示 flag に。MCP の `user_id` を regex で検証、`limit` を 500 で頭打ち | 文字列マッチによる権限 bypass を排除 |
| **PR8** 防御強化のダメ押し | `reply_context` を builder 内部でも sanitize、forwarded message の作者名も sanitize、`@everyone` を全送信で抑止 | 単発の echo で全員 ping を防ぐ |

---

## 進め方のルール

**PR 1 本ごとに、以下のループ**:

```
実装 → push → security-reviewer agent に詳細レビュー依頼
     → 指摘を follow-up commit で潰す
     → merge → master を pull → 次の PR へ
```

これで各 PR にレビューの跡が残るので、後から「なぜこの修正が入ったか」が PR ページだけで追える。

レビューで追加修正したのは計 9 件（whitespace token も reject、`fchmod` で既存ファイル遡及、cache に template version 入れて bust、metadata allowlist に `source` 追加…など）。

---

## 結果

- **pip-audit**: 11 件 → 1 件（残るのは mem0ai 2.0 stable 待ち、コード内に `TODO` コメント）
- **token hardcode**: working tree から消滅、`.gitignore` で再侵入防止
- **prompt injection / XSS**: 入口・保存・出口の 3 段防御
- **権限 bypass**: 文字列依存をやめて明示 flag 化

---

## 残ってる手動作業

PR でできない／確認待ちのもの：

1. **token を rotate** — Discord Developer Portal で旧 token を無効化して新規発行
2. **git history scrub** — `git filter-repo` で過去 commit の token も消す（強制 push になるので要承認）
3. **systemd hardening** — VPS の `/etc/systemd/system/discord-bot.service` に `NoNewPrivileges` 等を追加
4. **pip upgrade を他環境にも** — 本作業 VPS では `25.0 → 26.1.1` 済み
5. **mem0ai 2.0 stable リリース後** — `requirements.txt` の上限を解除

---

## メモ

- `PromptBuilder.sanitize()` や fact-text の scrubber を **idempotent** に作っていたので、防御層を増やすコストが低かった
- 一方、`_SENTINEL_RE` が `bot.py` と `prompt_builder.py` で重複しているのは今回も解消できず（同じ regex を 2 箇所で更新する負債が残った）
- 詳細な PR 単位のファイル一覧や commit SHA が必要なときは GitHub の PR #2〜#9 を参照
