# bird 相当機能の x-scraper 取り込み 実装計画

- **作成日**: 2026-05-18
- **作成者**: かなえ (Discord bot 側担当)
- **対象**: `/home/ubuntu/free-code/x-scraper/`
- **関連**: `/home/ubuntu/free-code/x-scraper/EXPANSION_PLAN.md`
- **Status**: draft（先輩レビュー待ち）

---

## 1. 背景

`@steipete/bird` (openclaw/openclaw 配下) を別 MCP として並走させる案を検討したが、
以下の理由から **bird は直接導入せず、既存 `x-scraper` MCP に bird 相当の read 系機能を取り込む** 方針とする。

- x-scraper は既に cookie 認証 + 内部 GraphQL 直叩きの基盤を持っている
- GraphQL ハッシュ自動抽出 (6h cache) と `ct0` CSRF 管理が既に実装済み
- `EXPANSION_PLAN.md` で Phase 1（read 拡充）が既に設計済みで、bird の read 系と機能が重複している
- MCP を 2 つに分けると discord-bot 側の `mcp_selector` 管理が複雑化する
- cookie / 認証 secret の取り回し場所が増える = 漏洩面積が増える

bird 自体は OSS 設計の参考にする（後述）。

---

## 2. スコープ

### IN
- bird の read 系のうち、kanae bot 運用に実用価値のある関数だけを x-scraper に追加
- 認証戦略の再設計（メインアカウント禁止、専用アカウント運用の明文化）
- discord-bot 側からの呼び出し導線（`mcp_selector` 経由）

### OUT
- write 系全般（post / reply / DM / like / retweet / follow / block）
- メインアカウント (`@haroin57`) の cookie 使用
- bird バイナリ・npm パッケージのサーバへの設置
- メディアアップロード、Spaces 連携

write 系は **discord-bot から発火しない**。X 上の書き込みは先輩本人が手で行う運用を維持する。
誤爆時の被害が大きすぎる（bot が勝手に post / DM 送信する事故、cookie 漏洩時の連鎖被害）。

---

## 3. 取り込む機能 (read-only)

`EXPANSION_PLAN.md` Phase 1 のうち、**bird との重複かつ kanae 運用に直接効くもの** を P0 として優先する。

| ID | MCP ツール名 | 用途 | bird 対応 | EXPANSION_PLAN.md |
|---|---|---|---|---|
| **F1** | `get_user_profile` | username → user_id 解決、プロフィール調査 | 〇 | 1.2 |
| **F2** | `get_user_tweets` | 「@xxx 最近何言ってる？」回答用 | 〇 | 1.3 |
| **F3** | `get_my_mentions` | haroin57 宛て言及の取得・要約 | 〇 | 新規 (bird 由来) |
| **F4** | `get_reply_tree` | 特定 tweet の会話展開 | 〇 | 新規 (bird 由来) |
| **F5** | `get_home_timeline` | フォロー中アカウントの最新ポスト | 〇 | 新規 (bird 由来) |
| F6 | `get_bookmarks` | あとで読む整理用 | 〇 | 4.4 |
| F7 | `get_following` / `get_followers` | フォロー関係調査 | 〇 | 1.1 |
| F8 | `get_list_tweets` / `get_list_members` | リスト分析 | 〇 | 1.5 |

**P0 = F1〜F5** を本実装計画のスコープとする。F6〜F8 は別計画。

---

## 4. 採用しない bird 機能

明文化しておく（半年後の自分が「やっぱり入れたい」と言い出した時の歯止め）。

| bird 機能 | 不採用理由 |
|---|---|
| `post` / `reply` / `quote` | 誤爆リスク、bot に書き込み権限を渡さない方針 |
| `dm send` / `dm read` | アカウント BAN リスク最大、プライバシー的にも不可 |
| `like` / `retweet` | 意図しない社会的シグナル発信、本人が手で行う |
| `follow` / `unfollow` / `block` / `mute` | 同上 |
| `bookmark add/remove` | read のみ採用（F6）、write は不採用 |
| media upload | post 系を採用しないため不要 |

---

## 5. アーキテクチャ

```
discord-bot (kanae)
    │
    ├─ mcp_selector.py  ← 既存
    │       │
    │       └─ x-scraper MCP (1 つに集約)
    │              │
    │              ├─ [既存] search_x, get_tweet, get_x_trends, search_x_with_trends
    │              └─ [新規] get_user_profile, get_user_tweets,
    │                        get_my_mentions, get_reply_tree, get_home_timeline
    │
    └─ cookies/
            ├─ default.json          ← 既存 (検索・トレンド用、専用アカウントで運用)
            └─ kanae_reader.json     ← 新規 (read 専用、捨てアカ運用)
```

実装ファイル分割は `EXPANSION_PLAN.md` 末尾のディレクトリ案に従う：

```
x-scraper/src/news_curator/scrapers/
├── x_trends.py        # 既存
├── x_user.py          # 新規: F1, F2 (UserByScreenName, UserTweets)
└── x_social.py        # 新規: F3, F4, F5 (Mentions, TweetDetail-tree, HomeTimeline)
```

---

## 6. 実装ステップ

### Step 1: 認証・cookie 運用の整備（P0 前提条件）
- [ ] 専用 X アカウント `@kanae_reader_xxxx` 作成（先輩のメインとは完全分離）
- [ ] 該当アカウントで X にログインし、cookie を `cookies/kanae_reader.json` にエクスポート
- [ ] `cookies/default.json` との切替仕組みを `cookie_manager.py` に実装（`EXPANSION_PLAN.md` 3.2 相当の最小版）
- [ ] secret rotation 手順を `docs/cookie-rotation.md` に記載
- [ ] cookie ファイルが `.gitignore` 配下にあることを確認

### Step 2: ページネーション基盤
- [ ] `_paginated_fetch()` ヘルパー実装（`EXPANSION_PLAN.md` 1.4）
- [ ] `bottom_cursor` 抽出と次リクエスト渡しのループ
- [ ] limit / page_size パラメータの統一

### Step 3: F1 `get_user_profile`
- [ ] GraphQL `UserByScreenName` のハッシュ取得
- [ ] `_get_user_profile(username)` 実装 in `x_user.py`
- [ ] 返却モデル: `{ user_id, screen_name, name, bio, followers_count, following_count, created_at, verified, protected }`
- [ ] MCP tool 登録 (`mcp_server.py`)
- [ ] テスト: 既知の public アカウント (例: `@elonmusk`) で取得確認

### Step 4: F2 `get_user_tweets`
- [ ] GraphQL `UserTweets` のハッシュ取得
- [ ] `_get_user_tweets(username, limit, include_replies)` 実装
- [ ] 内部で F1 を呼んで username → user_id 解決
- [ ] limit デフォルト 40、最大 200
- [ ] テスト: 取得件数、reply の含み/除き

### Step 5: F3 `get_my_mentions`
- [ ] GraphQL `NotificationsTimeline` または `Mentions` 操作のハッシュ取得
- [ ] `_get_mentions(limit, since_id)` 実装 in `x_social.py`
- [ ] 認証 cookie が当該アカウントのものであることが前提（自分宛て取得）
- [ ] `kanae_reader.json` を使う運用と、`haroin57` 本人 cookie を使う運用の **どちらにするかは別途決定**
  - 案 A: read 専用 alt account → mentions は alt 宛てのみ
  - 案 B: haroin57 cookie で alt account から read → BAN リスクと利便性のトレードオフ
  - **デフォルトは A**（安全側）、運用で必要になれば B を検討

### Step 6: F4 `get_reply_tree`
- [ ] 既存 `get_tweet` (TweetDetail) のレスポンスに reply chain が含まれることを利用
- [ ] `_get_reply_tree(tweet_url, depth, limit)` 実装
- [ ] depth デフォルト 3、最大 5（無限再帰防止）
- [ ] レスポンスを木構造で返却: `{ tweet, replies: [{ tweet, replies: [...] }] }`
- [ ] テスト: 既知の議論ツイートで取得

### Step 7: F5 `get_home_timeline`
- [ ] GraphQL `HomeTimeline` または `HomeLatestTimeline` のハッシュ取得
- [ ] `_get_home_timeline(limit)` 実装
- [ ] limit デフォルト 50、最大 200
- [ ] 認証アカウントのフォローリストに依存するので、`kanae_reader` が何をフォローするかが結果を決める
- [ ] 運用注: `kanae_reader` で「ニュース・技術系アカウント」だけを意図的にフォロー

### Step 8: discord-bot 側統合
- [ ] `services/mcp_selector.py` に新ツール 5 つを認識させる
- [ ] kanae の prompt に「ユーザー Discord 上で @xxx の最近のツイートを聞かれたら F2 を使う」等のヒント注入（必要なら）
- [ ] tool 呼び出しの correlation_id ログ確認

### Step 9: 検証
- [ ] 単体: 各ツールが期待スキーマで返ることを pytest で確認
- [ ] 統合: discord-bot から「@_kanae_xxx の最近のツイート見せて」等のメッセージで動作確認
- [ ] cookie 切れ時のエラーメッセージが明確であること
- [ ] rate limit 到達時の挙動（429 → 待機 / エラー返却）

### Step 10: ドキュメント
- [ ] `x-scraper/README.md` 更新（新ツール 5 つ）
- [ ] `discord-bot/docs/mcp-tools.md` 更新（あれば）
- [ ] 本ドキュメントを `accepted` に更新、または別 ADR 起こし

---

## 7. セキュリティ要件

1. **メインアカウント禁止** — `@haroin57` の cookie は本 MCP に**絶対に置かない**。Step 5 案 B を採用する場合のみ例外、要 ADR 起票。
2. **cookie ファイル 0600** — `chmod 600 cookies/*.json` 強制。リポジトリへの commit を `.gitignore` で禁止。
3. **secret rotation** — 専用アカウントの cookie は 3 ヶ月ごとに再ログイン → 再エクスポート。手順をドキュメント化。
4. **ログから token を除外** — `auth_token` / `ct0` がログに出力されないこと。既存 `structured_log.py` の redaction ルール確認。
5. **rate limit 既存値の踏襲** — read 系は 1-2 秒間隔。`EXPANSION_PLAN.md` BAN 対策ガイドラインに準拠。
6. **bird バイナリは設置しない** — `@steipete/bird` の npm パッケージや homebrew を本サーバに入れない。サプライチェーン経路を増やさない。

---

## 8. 受け入れ基準

- [ ] F1〜F5 すべて MCP ツールとして登録され、discord-bot から呼べる
- [ ] cookie が期限切れ時に明確なエラーメッセージが出る
- [ ] mainline の `search_x`, `get_tweet` 等が既存挙動を維持している（regression なし）
- [ ] `kanae_reader` アカウントが BAN されていない（実装〜運用 2 週間時点）
- [ ] discord-bot 経由で「@_kanae_xxx の直近 20 件取って要約して」が動作する

---

## 9. リスクと対応

| リスク | 影響 | 対応 |
|---|---|---|
| `kanae_reader` 専用 alt が BAN される | F3, F5 が機能停止 | read のみ・人間的なアクセス頻度に抑制、UA 維持 |
| X 側で GraphQL ハッシュ変更 | 全ツール一時停止 | 既存の自動抽出機構が拾う、手動 fallback パターン用意 |
| cookie ファイル漏洩 | 専用 alt 乗っ取り | mainline 影響なし。専用 alt は捨てる前提で再生成可 |
| haroin57 cookie をうっかり置く | アカウント BAN・乗っ取り直撃 | ファイル名規約（`*_reader.json` のみ許可）、pre-commit hook で検出 |
| rate limit に引っかかる | 429 連発、機能停止 | sliding window レートリミッター（EXPANSION_PLAN 3.3）の先行実装を検討 |

---

## 10. Open Questions

- **Q1**: Step 5 で `haroin57` 本人 mentions を取りたい場合、案 A（alt account 宛てのみ）で本当に運用足りるか？
  → 先輩レビュー待ち
- **Q2**: F4 `get_reply_tree` の depth デフォルト、3 で十分か？5 必要か？
  → 実装後に運用で調整
- **Q3**: 本計画と並行して `EXPANSION_PLAN.md` Phase 1 全体を進めるか、F1-F5 のみ切り出すか？
  → 本計画は F1-F5 のみスコープ、Phase 1 全体は別計画

---

## 11. 参考

- bird: https://github.com/openclaw/openclaw (read/write 機能の一覧調査用)
- x-scraper 既存拡充計画: `/home/ubuntu/free-code/x-scraper/EXPANSION_PLAN.md`
- 関連 ADR: 起票予定（採用構成の正式記録）

---

## 改訂履歴

| 日付 | 版 | 変更 | 担当 |
|---|---|---|---|
| 2026-05-18 | 0.1 | 初版 draft | かなえ |
