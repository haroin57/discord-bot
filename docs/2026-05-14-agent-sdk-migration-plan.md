# OAuth 継続戦略: mitmproxy で CLI 通信を byte-exact 模倣する

**作成**: 2026-05-14
**最終更新**: 2026-05-14
**方針**: 公式 Claude CLI / Claude Desktop が Anthropic API に送る通信を mitmproxy で完全キャプチャし、bot のリクエストを byte-exact に再現することで Anthropic 側の programmatic 識別を回避する
**ステータス**: Active

---

## 0. 方針サマリ

| 項目 | 決定 |
|---|---|
| 主系の認証 | **OAuth (claude.ai subscriber Bearer) を継続** |
| identity 偽装 | **公式 Claude CLI / Desktop の通信を mitmproxy で実測し、headers / body / params を byte-exact にコピー** |
| 動機 | bot を CLI / Desktop と区別不能にする = Anthropic から見て interactive 利用と区別がつかない |
| 観測対象 | `claude` CLI 実行 + Claude Desktop メッセージ送信のリクエスト |
| 反映先 | [services/claude_api/client.py](../services/claude_api/client.py), [services/claude_api/tool_loop.py](../services/claude_api/tool_loop.py), [services/oauth_manager.py](../services/oauth_manager.py) |
| 維持義務 | CLI / Desktop が version up したらキャプチャし直して bot を追随させる |

---

## 1. 背景

### 1.1 過去の mitmproxy 実績

[services/claude_api/client.py](../services/claude_api/client.py) のコメントに既に痕跡がある:

```python
# Identity prefix the real CLI sends as system[1] (mitmproxy capture
# 2026-05-13). Must be sent verbatim — server-side OAuth-subscriber
# gating appears to inspect this string.
CLI_IDENTITY_TEXT = "You are Claude Code, Anthropic's official CLI for Claude."
```

```python
# Pinned verbatim from the real claude-cli 2.1.92 against /v1/messages
# on model=claude-opus-4-6. Sending this block at system[2] (with
# cache_control={"type":"ephemeral","scope":"global"}) appears to
# be required for subscriber-tier identification — without it the
# server returns 429 with zero ratelimit-unified headers.
```

5/13 時点で部分的に mitmproxy 解析が走っていて、CLI default system prompt + Stainless headers + metadata.user_id ハッシュ等は既に反映済。今回はそれを **網羅的に / 6/15 想定で / 全 endpoint** やり直す。

### 1.2 なぜ byte-exact が必要か

Anthropic は public には識別ロジックを開示していないが、観測されている挙動:

- `x-app: cli` / Stainless ヘッダー値 / metadata.user_id JSON シリアライズ順 / cache_control の scope=global 等を「正しい順序・正しい値」で送らないと 429 / 403 が返る
- subscription tier の認識は **server-side で headers + body の組み合わせを hash 照合している** 可能性が高い (CLI バンドル内に salt 文字列が埋まっているのは過去調査済)
- 1 個でも値が違うと「Claude Code じゃない何か」と見なされ、grace 抜きで弾かれる
- 6/15 以降は「programmatic と interactive を分けて課金」するので、識別精度はさらに上がる想定

→ **正しい挙動 = 公式 CLI と完全に同じバイト列を送ること**

### 1.3 リスク

| リスク | 対応 |
|---|---|
| CLI バージョンが上がると identity が変わる | 月次で再キャプチャ、または bot.log で 429 増えたら再キャプチャ |
| Anthropic が「正規署名」を導入したら byte-exact では破れる | その時は Plan B (Agent SDK 経路) に切替 |
| キャプチャ作業のミスで `auth_token` 等の機密情報を commit | mitmproxy ダンプは `.gitignore` で除外、要点抽出のみコード化 |
| 動的な値 (session_id 等) を間違って固定 | §4 で動的/静的を分類済 |

---

## 2. mitmproxy セットアップ

### 2.1 ホスト

VPS 上で完結させない。**ローカル開発機 (先輩の Mac/Windows) で実 Claude Desktop を動かしながらキャプチャ**するのが本筋。理由:

- VPS で Claude Desktop は動かない (GUI 必要)
- `claude` CLI を VPS で `mitmproxy` 越しに動かすことはできるが、それは「先輩の本物の OAuth トークンが入った CLI セッション」が必要
- バイト精度で見たいので Desktop vs CLI 両方やる

ローカルでやる場合の手順:

```bash
# 1. mitmproxy を導入 (Mac)
brew install mitmproxy

# 2. CA 証明書を OS のトラストストアに入れる
mitmproxy --listen-port 8080 --set confdir=~/.mitmproxy
# 起動後、ブラウザで http://mitm.it を開いて Mac 用証明書をダウンロード・インストール

# 3. mitmweb (UI 付き) で実行
mitmweb --listen-port 8080 --set block_global=false

# 4. Claude Desktop / CLI を 8080 経由で送信させる
# Mac の場合: システム設定 > ネットワーク > Proxy > Web Proxy (HTTP) / Secure Web Proxy (HTTPS) を localhost:8080
# または環境変数: HTTPS_PROXY=http://localhost:8080 NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem claude "test"
```

### 2.2 キャプチャ対象セッション

最低 5 ケース取る:

| ケース | 対象 | 確認したいこと |
|---|---|---|
| A | `claude "hello"` (一発質問) | 一番素のリクエスト |
| B | `claude -p "hello"` | programmatic flag が付くか (6/15 で billing 分かれる予兆) |
| C | `claude` (interactive) で 3 ターン会話 | session_id / resume の挙動 |
| D | Claude Desktop で同様に 3 ターン | Desktop vs CLI の差分 |
| E | tool 使用が走るリクエスト (`claude "Read package.json"`) | tools / cache_control の出方 |

ケースごとに mitmproxy で `.har` 形式 (mitmweb の Export As) または `.dump` 形式 (`mitmdump --writer ~/captures/A.dump`) で保存。

### 2.3 機密情報の取扱い

- ダンプには **OAuth Bearer token がそのまま含まれる**
- VPS や repo に commit しない
- ローカルで解析→要素抽出→bot コードに反映するワークフロー
- 抽出した「公開可能な構造」(ヘッダー名 / body 構造) のみ docs / コードに残す

---

## 3. 解析手順

### 3.1 mitmproxy ダンプから抽出するもの

1. **Request URL** — full path + query string
2. **Request method**
3. **Request headers** — 順序付きで全部 (`Host`, `Authorization`, `anthropic-beta`, `User-Agent`, `X-Stainless-*`, `X-Claude-Code-Session-Id`, `x-app`, `accept-language`, `sec-fetch-mode` 等)
4. **Request body (JSON)** — top-level keys の順序、ネスト構造、`cache_control` の位置、`metadata.user_id` の文字列内 key 順序
5. **Response status + headers** — 特に `anthropic-ratelimit-unified-*`、`x-ratelimit-*`、`anthropic-organization-id`、新規ヘッダー
6. **Response body の構造** — error 時の error.type / error.message

### 3.2 解析スクリプト

mitmproxy のダンプを Python で開いて diff を取る helper を作る:

```python
# scripts/mitm_to_bot_diff.py (新規)
"""Compare a captured CLI request against what services.claude_api would build.

Usage:
    .venv/bin/python scripts/mitm_to_bot_diff.py ~/captures/A.dump
"""
from mitmproxy import io
from mitmproxy.exceptions import FlowReadException

# 1. ダンプから /v1/messages? のリクエストを抽出
# 2. headers を normalize (大文字小文字、Authorization は redact)
# 3. body を json.loads して dict 化
# 4. services.claude_api.tool_loop._build_request_kwargs (新規 helper) と比較
# 5. diff を出力
```

これを repo に commit して、CLI バージョン上げる度に同じスクリプトで diff 取れるようにする。

### 3.3 抽出から bot コードへの反映

抽出した要素を以下の場所に反映:

| 要素 | 反映先 |
|---|---|
| `User-Agent` | [services/claude_api/client.py:USER_AGENT](../services/claude_api/client.py) |
| `anthropic-beta` 値 | [services/claude_api/client.py:MODEL_BETAS](../services/claude_api/client.py) |
| `x-app` / `X-Stainless-*` / `X-Claude-Code-Session-Id` / `accept-language` / `sec-fetch-mode` | [services/claude_api/entry.py:_make_api_client:cli_headers](../services/claude_api/entry.py) |
| OAuth bearer の送り方 (`Authorization: Bearer ...` vs `auth_token=`) | [services/oauth_manager.py:make_async_oauth_client](../services/oauth_manager.py) |
| Body の `system` 配列構造 (system[0], system[1], system[2] の cache_control) | [services/claude_api/tool_loop.py:_build_cached_system](../services/claude_api/tool_loop.py) |
| `metadata.user_id` の JSON serialization (key 順序、separator) | [services/claude_api/client.py:get_metadata](../services/claude_api/client.py) |
| `thinking` 設定 (`{"type": "adaptive"}` vs `{"type": "enabled"}`) | [services/claude_api/client.py:THINKING_CONFIG](../services/claude_api/client.py) |
| `extra_body.output_config` (`{"effort": "medium"}`) | [services/claude_api/client.py:OUTPUT_CONFIG](../services/claude_api/client.py) |
| `extra_body` 内の他のフィールド (Top-K, Top-P, system prompt の cache_control scope 等) | [services/claude_api/tool_loop.py:run_agent_loop:stream_kwargs](../services/claude_api/tool_loop.py) |
| `attribution_hash` の計算式 (前回調査で salt は判明) | [services/claude_api/client.py:get_attribution_header_value](../services/claude_api/client.py) |

### 3.4 動的に変えるべき値 vs 固定すべき値

| 値 | 静的/動的 | メモ |
|---|---|---|
| `_session_id` | **動的** (UUIDv4 を bot 起動毎に再生成) | 既に [client.py:45](../services/claude_api/client.py) で実装済 |
| `_device_id` (= `~/.claude.json` の `userID`) | **静的** (先輩の CLI が生成した値) | 先輩の Mac の `~/.claude.json` から取得して VPS の `~/.claude.json` に同期 (既に実装済) |
| `_account_uuid` (= `~/.claude.json` の `oauthAccount.accountUuid`) | **静的** | 同上 |
| Bearer token | **動的** (refresh するので) | [oauth_manager.py](../services/oauth_manager.py) で管理 |
| `X-Stainless-Package-Version`, `X-Stainless-Runtime-Version` | **半静的** | CLI version up で変わる。月次再キャプチャ |
| `User-Agent` | **半静的** | 同上 |
| `model` | **動的** (bot が選ぶ) | `claude-opus-4-6` 等 |
| `system` 配列の中身 | **混合**: system[0]/[1]/[2] は CLI default を verbatim、system[3] が bot 固有のシステムプロンプト | |

---

## 4. 6/15 想定の追加観察

6/15 以降に CLI / Desktop が送るリクエストに **新規ヘッダーや新規 body フィールドが追加される可能性が高い**。例:

- `anthropic-agent-sdk-credit-id: <uuid>` (推定)
- `anthropic-beta: agent-sdk-credit-2026-06-15` (推定)
- body に `billing_mode: "agent_sdk_credit" | "subscription" | "extra_usage"` (推定)

これらが追加されたら bot 側も同じヘッダーを送る必要がある (送らないと「古い CLI」と見なされて弾かれる可能性)。

### 4.1 監視タスク

5/14 → 6/14: 平均週 1 回、ローカルで Claude Desktop / CLI のリクエストをキャプチャして diff を取る。新規ヘッダー / body 変更があれば bot に反映。

6/15 当日: 6/15 朝、最初に Claude Desktop を起動して数メッセージやり取りし、新しいヘッダー値を キャプチャする。差分を当日中に bot へ反映 → デプロイ。

### 4.2 自動化

理想的には bot 自体が「リクエスト送るたびにレスポンスヘッダーをチェック → 未知のヘッダーがあれば 先輩 DM に警告」を実装。実装場所: [services/claude_api/ratelimit.py:observe](../services/claude_api/ratelimit.py)。

```python
KNOWN_HEADERS = frozenset({
    "anthropic-ratelimit-unified-status",
    "anthropic-ratelimit-unified-5h-status",
    # ... 既知の全部
})

def observe(headers):
    # ... 既存処理
    unknown = {k for k in headers if k.startswith("anthropic-") and k not in KNOWN_HEADERS}
    if unknown:
        log.warning("unknown anthropic-* headers detected: %s", sorted(unknown))
        # bot.py 側で先輩 DM に転送
```

---

## 5. 短期タスク (5/14 - 6/14)

### Phase 1: 既存実装の audit (5/14 - 5/17)

`services/claude_api/client.py`, `tool_loop.py`, `entry.py`, `oauth_manager.py` 内で「mitmproxy capture 2026-05-13」「pinned verbatim」「from real claude-cli」とコメントされてる箇所を全部抜き出し、それぞれが今でも CLI と一致してるか確認する。

確認方法:
- 先輩のローカル Mac で mitmproxy 起動
- `HTTPS_PROXY=http://localhost:8080 NODE_EXTRA_CA_CERTS=$HOME/.mitmproxy/mitmproxy-ca-cert.pem claude "test"` を実行
- mitmweb で /v1/messages リクエストを開く
- bot コードの該当箇所と diff

### Phase 2: 解析スクリプト整備 (5/17 - 5/20)

`scripts/mitm_to_bot_diff.py` を作成し、ダンプ → bot 生成リクエストの自動 diff を取れるようにする。

### Phase 3: 月次再キャプチャ運用 (5/20 以降)

- 毎月 1 日 (または `claude --version` が変わった日) に先輩がローカルでキャプチャ
- diff スクリプトで差分確認
- 差分があれば bot 側を更新

### Phase 4: 6/15 直前の総点検 (6/10 - 6/14)

6/15 の policy 変更でリクエスト構造が変わる可能性に備えて、6 月最初の週に集中的にキャプチャ + diff + 反映を実施。

### Phase 5: 6/15 当日 (6/15)

- 朝一で Claude Desktop からメッセージ送信し新ヘッダーをキャプチャ
- 差分を即日 bot に反映 + デプロイ
- bot.log で 24h 観察、何か anomaly あれば即対応

### Phase 6: Plan B 維持 (継続)

§5 (旧 docs) で書いた Agent SDK Plan B はそのまま温存。Anthropic から完全ブロックされた場合の salvage 経路として `feature/agent-sdk-fallback` ブランチで保持。

---

## 6. 既存 mitmproxy 反映ポイントの再点検リスト

[services/claude_api/](../services/claude_api/) 配下から「mitmproxy / CLI verbatim」由来の値を抜き出した一覧:

### 6.1 [client.py](../services/claude_api/client.py)

| 項目 | 行 | 現在値 | 再点検 |
|---|---|---|---|
| `USER_AGENT` | (CC_VERSION 由来) | claude-cli 2.1.92 相当 | CLI version up したか確認 |
| `MODEL_BETAS` | top-level | interleaved-thinking-2025-05-14 等 | beta 値の追加削除を確認 |
| `THINKING_CONFIG` | `{"type": "adaptive"}` | adaptive | adaptive のままか確認 |
| `OUTPUT_CONFIG` | `{"effort": "medium"}` | medium | 値変更ないか確認 |
| `CLI_IDENTITY_TEXT` | system[1] verbatim | "You are Claude Code, Anthropic's official CLI for Claude." | 変更ないか確認 |
| `CLI_DEFAULT_SYSTEM_PROMPT` | `cli_default_system_prompt.txt` | 5/13 時点の verbatim | 6/15 までに再キャプチャで diff 確認 |
| `_session_id` | 動的 (UUID v4) | OK | — |
| `_user_account_cache` | `~/.claude.json` 由来 | OK | 先輩のローカル `~/.claude.json` と VPS 側で device_id 一致しているか確認 |
| `get_metadata` の JSON シリアライズ順序 | device_id → account_uuid → session_id | OK | CLI 側のキー順序が変わってないか確認 |
| `get_attribution_header_value` の SALT | KA7/jBY (CLI bundle 由来) | OK | CLI バンドル version up で SALT が変わってないか確認 |

### 6.2 [entry.py:_make_api_client](../services/claude_api/entry.py)

| ヘッダー | 現在値 | 再点検 |
|---|---|---|
| `x-app` | `cli` | 変更ないか |
| `User-Agent` | USER_AGENT 由来 | 同上 |
| `X-Claude-Code-Session-Id` | `_session_id` | OK |
| `X-Stainless-Lang` | `js` | 変更ないか |
| `X-Stainless-Package-Version` | `0.80.0` | CLI 内の Stainless version 上がってないか |
| `X-Stainless-Runtime` | `node` | 変更ないか |
| `X-Stainless-Runtime-Version` | `v24.14.1` | Node version 上がってないか |
| `X-Stainless-OS` | `Linux` | mitmproxy で Mac から取った場合は `MacOS` になるはず → VPS は Linux なので一致でよい |
| `X-Stainless-Arch` | `x64` | 同上 |
| `X-Stainless-Timeout` | `600` | 変更ないか |
| `accept-language` | `*` | 変更ないか |
| `sec-fetch-mode` | `cors` | 変更ないか |

### 6.3 [tool_loop.py:run_agent_loop](../services/claude_api/tool_loop.py)

| 要素 | 現在値 | 再点検 |
|---|---|---|
| `stream_kwargs.model` | bot 設定 (`claude-opus-4-6`) | CLI が送るモデル名と一致するか |
| `stream_kwargs.system` | `_build_cached_system` の出力 (system[0..3]) | CLI の system 配列構造と完全一致するか |
| `stream_kwargs.betas` | `MODEL_BETAS` | CLI と同じ順序か |
| `stream_kwargs.metadata` | `get_metadata()` | byte-exact |
| `stream_kwargs.thinking` | `THINKING_CONFIG` | OK |
| `stream_kwargs.extra_body.output_config` | `OUTPUT_CONFIG` | OK |
| `extra_body` 内のその他 (temperature, top_p, top_k) | bot 側で送ってない | CLI が送っているかキャプチャで確認、送っていれば追加 |
| `tools` 配列の構造 | builtin + MCP | CLI と structure が同じか (tool name / description / input_schema) |

### 6.4 [oauth_manager.py](../services/oauth_manager.py)

| 要素 | 現在値 | 再点検 |
|---|---|---|
| `Authorization: Bearer <token>` | `auth_token=` (anthropic SDK が自動付与) | header 名・値の format が CLI と一致するか |
| `anthropic-beta: oauth-2025-04-20` | OAuth クライアント default | CLI も同じ beta を送っているか |
| `_sanitize_outgoing_request` hook | Python-specific Stainless ヘッダーを strip | hook 後の最終 header list が CLI と完全一致するか |
| `prime_subscriber_session` の `/api/oauth/profile` + `/api/oauth/claude_cli/roles` | 1 回だけ叩く | CLI が起動時に何回叩くかキャプチャで確認 |

---

## 7. 検証チェックリスト

### Phase 1 完了時 (5/17 目標)

- [ ] ローカル Mac で mitmproxy + Claude Desktop / `claude` CLI でキャプチャ取得
- [ ] §6 のテーブル全項目を実測値と diff 取って一致確認
- [ ] 不一致があれば bot コードを更新
- [ ] 更新後に bot を再起動して動作確認

### Phase 2 完了時 (5/20 目標)

- [ ] `scripts/mitm_to_bot_diff.py` 作成
- [ ] ダンプを食わせて自動 diff 出力できる

### Phase 3-4 完了時 (5/20 - 6/14)

- [ ] 週次再キャプチャを 4 回以上実施 (5/20, 5/27, 6/3, 6/10)
- [ ] 各回の diff を docs に追記 (本ファイルの §10 履歴セクション)
- [ ] CLI version up を検知したら即更新

### Phase 5 完了時 (6/15)

- [ ] 6/15 朝に再キャプチャ
- [ ] 新規ヘッダー / body フィールドを反映
- [ ] 24h 観察で programmatic 識別による拒否が起きていない

### Phase 6 (継続)

- [ ] feature/agent-sdk-fallback ブランチを別途維持 (旧 docs の §5)

---

## 8. オープン課題

1. **mitmproxy on Linux VPS で claude CLI を動かす権限**: VPS 上で `claude` CLI を mitmproxy 越しに動かせれば、ローカル Mac が不要になる。検証要
2. **`X-Stainless-OS` の値が VPS (Linux) と Mac で違う**: Mac から取ったキャプチャをそのまま VPS に持ってきても VPS は Linux なので、OS フィールドは VPS 環境に合わせる (現状そうなっている)
3. **新規ヘッダー検知の自動 DM**: §4.2 で書いたが、現時点で未実装
4. **CLI version up の検知**: `claude --version` を定期実行して変化があれば 先輩 DM、というスクリプトを cron に入れる価値あり
5. **mitmproxy ダンプの保管**: 先輩のローカル Mac の `~/captures/` に置くだけで OK、commit しない

---

## 9. Anthropic 対策シナリオ分析 (5/14 追記)

Anthropic が bot の OAuth 利用を潰しに来る場合の想定シナリオと、各シナリオに対する耐性を評価する。

### 9.1 シナリオ一覧

| # | 対策 | 致命度 | 実現コスト (Anthropic 側) | 現 bot の耐性 |
|---|---|---|---|---|
| A | **redirect_uri の厳格化** (認可サーバーで registered URI と exact match) | 低〜中 | 低 | **影響なし** — bot は claude.ai の callback URL をそのまま使い authorization code を横取りする実装。redirect_uri パラメータ自体は正規値を送っているため exact match に通る |
| B | **token audience 検証** (JWT `aud` クレームで API ゲートウェイが `aud: "api.anthropic.com"` を要求) | **高** (対策あり) | **高** | **CLI トークン横取りで回避可能** — JWT の `aud` は署名付きで改竄不可だが、Claude Code CLI が正規に取得したトークンは API 呼び出し用の正しい audience を持つ。CLI で認証 → そのトークンを bot が流用すれば aud 検証を通過できる (§9.2.1 参照) |
| C | **client_id / client_secret のローテーション強化** | 中 | 低 | リバースエンジニアリングで追随可能だがいたちごっこ。CLI バイナリの難読化 + ローテーション頻度次第で運用コスト増大 |
| D | **リクエスト署名 (HMAC / mTLS)** | 高 | 中〜高 | CLI バイナリに埋め込まれた secret でリクエストに署名する方式。secret 抽出はデコンパイルで可能だが、ローテーション + obfuscation 併用で実質的にブロック可能 |

### 9.2 高リスクシナリオ: token audience (B)

最も危険だが**回避策が存在する**のが B (token audience 検証)。

#### 攻撃の仕組み

- OAuth で発行される JWT に `aud: "claude.ai"` が含まれる
- API ゲートウェイ (`api.anthropic.com`) が `aud: "api.anthropic.com"` を要求する validation を入れる
- トークン自体が署名済みなので bot 側で `aud` を差し替える手段がない

#### 9.2.1 回避策: CLI トークン横取り

Claude Code CLI で正規に OAuth 認証すると、CLI 用の audience (= API 呼び出しに有効な `aud`) を持つトークンが発行される。**このトークンを bot が流用すれば、aud 検証を通過できる**。

仕組み:
1. Claude Code CLI で `claude login` → 正規 OAuth フローで認証
2. CLI がトークンを `~/.claude/` 配下に保存
3. bot の `oauth_manager.py` が CLI のトークンファイルを読み取り、Bearer として使用
4. トークンは正規クライアントが正規に取得したものなので、`aud` は API 呼び出しに有効

残存リスク:
- **トークン保存場所・形式の変更**: CLI バージョンアップでパスや形式が変わったら追従が必要
- **refresh token ローテーション**: CLI が refresh するタイミングと bot の使用タイミングが競合する可能性。bot 側でも refresh ロジックを持つ必要あり
- **scope 制限**: CLI トークンに MCP 専用・conversation 専用等の scope が入ったら、一部 API が使えない可能性
- **同時セッション制限**: 同一トークンで CLI と bot が同時にリクエストすると、Anthropic が「1 トークン 1 セッション」制限を入れた場合に片方が弾かれる

ただし B の実装は Anthropic にとってもコストが大きい:

- 既存の OAuth フロー全体 (claude.ai Web / Desktop / CLI / Extensions) に影響
- audience-aware な token issuance に認可サーバーの改修が必要
- 既存ユーザーのトークンが一斉に無効化されるため migration period が必要

→ **短期的 (6/15) に来る可能性は低い**。中期的 (2026 Q3-Q4) には十分あり得る。来ても CLI トークン横取りで回避可能。

### 9.3 対応方針

| 時間軸 | 方針 |
|---|---|
| 5/14 - 6/15 | 現行 mitmproxy 模倣 (本ドキュメント §1-§8) で走る |
| 6/15 当日 | A/C/D レベルの変更はキャプチャ → 即日反映で対応 |
| B が来た場合 | **CLI トークン横取り (§9.2.1)** で回避。CLI で `claude login` → トークンを bot が流用。それも塞がれたら Plan B (Agent SDK fallback) に切替 |
| 中期保険 | Agent SDK 正規利用への段階的移行を並行検討 (Max plan の API credit 枠内で運用可能か試算) |

---

## 10. 参考リンク

- [mitmproxy documentation](https://docs.mitmproxy.org/)
- [Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan) — Plan B 用
- [ClaudeDevs tweet (6/15 announcement)](https://x.com/ClaudeDevs/status/2054610152817619388)
- 既存コードの mitmproxy 痕跡: [services/claude_api/client.py](../services/claude_api/client.py) のコメント

---

## 11. 更新履歴 / キャプチャログ

| 日付 | バージョン | 差分要約 | 反映 commit |
|---|---|---|---|
| 2026-05-13 | claude-cli 2.1.92 | 初回キャプチャ。CLI_IDENTITY_TEXT / CLI_DEFAULT_SYSTEM_PROMPT / Stainless headers / metadata.user_id 順序 / attribution SALT を反映 | (既存 commit 群、git log 参照) |
| 2026-05-14 | — | ドキュメント方針転換 (旧: Agent SDK 全面移行 → 新: mitmproxy 模倣で OAuth 継続) | — |
| TBD | TBD | (5/17 までに Phase 1 audit 実施) | — |

---

## 12. 廃案 (履歴用)

旧計画 1: **Agent SDK 全面移行** (5/14 早朝)
- 6/15 までに `services/claude_api/*` を全部 `claude-agent-sdk` に置換
- 工数 2-3 週間 → 撤回 (先輩判断: 工数大きすぎ)

旧計画 2: **OAuth 継続 + 観察ベースのリアクティブ対応** (5/14 午後)
- usage_fallback を拡張するだけ
- bot 側の identity 偽装はそのまま放置
- → 本計画に統合 (mitmproxy 模倣で identity の精度を上げる、observe ベースの fallback は引き続き併用)
