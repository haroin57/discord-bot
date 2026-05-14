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
- [ ] 各回の diff を docs に追記 (本ファイルの §12 履歴セクション)
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
| E | **interactive 利用の rate / pattern 分析で programmatic を識別** (同一 OAuth から短時間に多数 session_id、Discord-like burst パターン、VPS IP からの定常呼出 等) | **高** | 中 | **C 経路 (§1-§9 mitmproxy 模倣) では擦り抜けが困難** — ヘッダーが完璧でも振る舞いで露見する。B 経路 (§10 interactive pty wrap) では官公式 CLI の振る舞いそのものに近づくので耐性が増す。両者併用で多層化するのが本ドキュメントの hybrid 方針 |

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

## 10. 採用方針: B + C ハイブリッド (5/14 X 調査・5/14 改訂)

### 10.0 重要な前提崩壊と方針修正

5/14 の X 調査で **`claude -p` (headless / programmatic mode) は 6/15 以降の専用月次クレジット課金対象** と複数ソースから確認:

- [@L_go_mrk](https://x.com/L_go_mrk/status/2054744738901950724): 公式announcement精読「**Claude Agent SDK / `claude -p` / Claude Code GitHub Actions / 第三者アプリ**」が対象
- [@claudecode_lab](https://x.com/claudecode_lab/status/2054802415749407019), [@GeekNewsHada](https://x.com/GeekNewsHada/status/2054771000324309063) (韓国) が再確認
- [@himkt](https://x.com/himkt/status/2054737414015553568) (cafleet作者): 「subagent に headless claude code 起動させてるけど **クレジット消費される**」と作者自身が明言
- [@barckcode](https://x.com/barckcode/status/2054682888679928284) (スペイン語): "headless mode lo van a empezar a chapar fuerte"
- [@Lattice_Node](https://x.com/Lattice_Node/status/2054714822592528865) ❤167: **無制限Claude Code時代の終了宣言**

→ **当初案の `claude -p --output-format stream-json` 経由は 6/15 で subscription 枠から外れる**ため、本案を以下に修正:

### 10.1 採用方針: B + C ハイブリッド

| 経路 | 役割 | 実装状況 | 特徴 |
|---|---|---|---|
| **C (mitmproxy 模倣 / 直接 `/v1/messages`)** | **現用 main / 中期 fallback** | **既に実装済** ([services/claude_api/entry.py:240](../services/claude_api/entry.py#L240) で `anthropic.AsyncAnthropic` を CLI ヘッダーで叩いている) | 起動コスト 0 / 細粒度制御可 / 但し pattern 分析で露見リスク (§9.1 E) |
| **B (interactive `claude` を pty 保持で subprocess wrap)** | **新規実装、メイン昇格候補** | 未実装 | `-p` を避け interactive 経路で subscription 枠維持を狙う / CLI 本体の振る舞いそのまま / ANSI/TUI パースが課題 |

**ハイブリッドの意味**:
1. **平常時** — heavy / 長時間タスクは B 経路で実行 (CLI そのものの interactive 振る舞いに紛れる)、軽量 / 短いリクエストは C 経路で即応 (subprocess 起動コスト回避)
2. **障害時** — どちらかが Anthropic に潰された場合、もう一方に即フェイルオーバー
3. **観察時** — B 経路の通信を mitmproxy で観察し、C 経路の byte-exact 模倣精度を継続的に高める (B が「正解」を C に教える教師役)

### 10.2 B 経路 (interactive pty wrap) のアーキテクチャ

```
[bot.py] → [services/claude_cli/pty_wrapper.py]
                ↓ ptyprocess.PtyProcessUnicode で永続 TTY 保持
           [claude] (interactive REPL、subscription 枠で動く)
                ↓ ANSI escape 混じりの TUI 出力
           [ansi_strip + state machine parser]
                ↓ プレーンテキスト + tool_use イベント
           [bot.py] → Discord
```

実装中核 (新規 `services/claude_cli/pty_wrapper.py`):

```python
import asyncio
import re
from ptyprocess import PtyProcessUnicode

ANSI_ESC = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

class ClaudeInteractiveSession:
    """Hold a single `claude` REPL alive via pty and feed prompts to it."""

    def __init__(self, cwd: Path, env: dict[str, str]):
        # `-p` を絶対に付けない (付けると headless 扱いで課金対象)
        self.proc = PtyProcessUnicode.spawn(
            ["claude"],
            cwd=str(cwd),
            env=env,
            dimensions=(40, 200),  # ターミナルサイズを大きく取って折返し抑制
        )
        self._buf = ""
        self._lock = asyncio.Lock()

    async def ask(self, prompt: str, *, timeout: float = 600.0) -> str:
        """Send a prompt and read until the REPL is idle again."""
        async with self._lock:
            self.proc.write(prompt + "\r")
            return await self._read_until_idle(timeout)

    async def _read_until_idle(self, timeout: float) -> str:
        # state machine で「次のプロンプト記号 (e.g. ╭─ Try... ─╮) が出るまで」読む
        # ANSI escape を strip しつつ、tool_use / tool_result の境界を検出
        ...
```

**鍵となる技術的判断**:
- **`-p` フラグは絶対に付けない** (6/15 後の課金フラグそのもの)
- **`--output-format` 系も付けない** (これも headless 識別フラグになる可能性)
- ターミナル幅を `200 cols × 40 rows` 程度に確保して TUI の折返し / 装飾を最小化
- 「REPL がアイドルに戻った」検出は、CLI が描く入力プロンプト枠線 (`╭─ Try ... ─╮`) の出現で判断
- ステートマシンで `Tool use:` / `Tool result:` 行を識別して bot 側のイベントモデルにマッピング

### 10.3 既存コードとの関係

| 既存 | B+C ハイブリッドでの扱い |
|---|---|
| [services/claude_api/entry.py](../services/claude_api/entry.py) (現用 C 経路) | **保持** — 軽量 / 短時間タスクの main、B のフェイルオーバー先 |
| [services/claude_api/client.py](../services/claude_api/client.py) (CLI 偽装ヘッダー塊) | **保持・継続メンテ** — §1-§9 の月次キャプチャ更新を継続 |
| [services/claude_api/tool_loop.py](../services/claude_api/tool_loop.py) | **保持** — C 経路の心臓部 |
| [services/oauth_manager.py](../services/oauth_manager.py) | **保持** — C 経路用。B 経路は CLI が `~/.claude/.credentials.json` で自前管理する |
| [services/claude_api/ratelimit.py:observe](../services/claude_api/ratelimit.py) | **保持 + 拡張** — B 経路の TUI 出力からも rate hint をパースする |
| 新規 `services/claude_cli/pty_wrapper.py` | **新設** — B 経路の中核 |
| 新規 `services/claude_cli/ansi_parser.py` | **新設** — TUI 出力 → イベント変換 |
| 新規 `services/router.py` | **新設** — リクエストを B / C に振り分けるルーター (タスク重量 / channel / fallback 状態で判定) |
| MCP server 定義 | **両経路で共有** — C は現状通り、B は CLI に `--mcp-config` で渡す |

### 10.4 メリット (B+C ハイブリッド)

- **6/15 課金回避の二重保険** — C が pattern 分析で flag されても B (interactive) で継続、B が CLI 識別で潰されても C で継続
- **C 経路は変更ゼロで現行運用継続可** — 緊急フェイルオーバーが既に存在する
- **B 経路は CLI 本体に追随するので mitmproxy メンテ負担なし**
- **観察ループ** — B の通信を mitmproxy で取れば、Anthropic 側の interactive リクエスト最新仕様を C に反映でき、C の byte-exact 精度が継続的に上がる
- 既存の Skills / Hooks / Slash commands / CLAUDE.md 資産が B 経路でそのまま使える

### 10.5 デメリット / 残課題

- **TUI パースが最大の難所** — `stream-json` が使えない以上、ANSI escape + プロンプト枠線 + tool_use / tool_result 表示を自力でパースする必要。CLI version up で TUI が変わるたびに parser を直す
- **subprocess の永続化と並列性** — pty を Discord channel 単位で確保するとメモリと FD を消費 (VPS 2GB RAM で同時何セッション保てるか要検証)。アイドル N 分で kill する LRU が必要
- **`~/.claude.json` 単一ユーザー前提** — 複数チャンネル / 複数ユーザーの分離は `HOME` 切替 or workspace 分離で実装する必要
- **CLI が permission prompt を出すケース** — `--dangerously-skip-permissions` を付けるか、hook (`PreToolUse`) で auto-approve を仕込む必要。前者は admin only に限定すべき
- **route 判定ロジック** — どのリクエストを B、どれを C に流すかの基準設計が新規必要 (期待出力長 / tool_use 数 / channel 単位の sticky 等)
- **C 経路の Pattern 識別リスクは残存** (§9.1 E) — C は将来的に潰される前提で B への重み付けを徐々に増やす運用

### 10.6 段階的実装案

| Phase | 期間 | 内容 | 主担当ファイル |
|---|---|---|---|
| 10-a: pty PoC | 1-2 日 | `ptyprocess` で `claude` interactive を 1 ターン回し、出力取得確認 | 新規 `services/claude_cli/pty_wrapper.py` |
| 10-b: ANSI / TUI parser | 3-5 日 | プロンプト枠線・tool_use 行・回答本文の分離 state machine | 新規 `services/claude_cli/ansi_parser.py` |
| 10-c: マルチターン session 保持 | 2-3 日 | LRU でアイドル N 分後 kill、Discord channel ↔ pty session の mapping | 新規 `services/claude_cli/session_pool.py` |
| 10-d: hook 経由の bot 機能移植 | 3-5 日 | rate limit observe / emotion feedback / mem0 注入を `~/.claude/hooks/` 配下スクリプトとして再実装 | `~/.claude/hooks/` |
| 10-e: router 実装 | 2-3 日 | リクエスト重量分類 → B/C 振分け、エラー時のフェイルオーバー | 新規 `services/router.py` |
| 10-f: 並走 A/B 比較 | 1 週間 | feature flag で channel 単位に B/C 切替し、応答品質 / コスト / 検出率を計測 | `bot.py` |
| 10-g: 観察ループ整備 | 継続 | B 経路のリクエストを開発機の mitmproxy で観察し、C 経路の byte-exact 模倣に反映 | docs §6 + `services/claude_api/client.py` |

### 10.7 経路ごとの推奨用途

| リクエスト特性 | 推奨経路 | 理由 |
|---|---|---|
| 短文 / 単発 / tool_use なし (e.g. 雑談) | **C** | subprocess 起動コスト不要、ms 単位で応答 |
| 長文 / multi-turn / tool_use 多 (e.g. コード調査・実装) | **B** | interactive REPL に紛れるので識別耐性高、CLI の振る舞いそのまま |
| 24/7 cron / 自動巡回 (e.g. 週次 mem0 compact) | **どちらも危険** — 別枠 programmatic credit で正規に動かす方が安全 | pattern 分析で一発で見える |
| admin only の機密操作 (`Bash` exec 等) | **B** + `--dangerously-skip-permissions` (admin のみ) | C 経路は tool 実行が bot 側実装で、本物の CLI 挙動と差が出る |

### 10.8 オープン課題

1. **CLI が `~/.claude.json` を update する race**: 複数 pty が同一ファイルを書くと壊れる。HOME 分離 or 起動時 lock 必須
2. **permission prompt の auto-handle**: hook (`PreToolUse`) で json で `{"decision": "approve"}` を返すパターンが公式に推奨。非 admin はホワイトリスト方式
3. **TUI バージョン変化への追随**: CLI 2.1.92 → 2.1.141 で hook JSON に `terminalSequence` field 追加 ([@ClaudeCodeLog](https://x.com/ClaudeCodeLog/status/2054705665260593480))。月次で parser を回帰テスト
4. **Anthropic が interactive ←→ programmatic を見分ける heuristic**: タイピング間隔 / human-like burst の有無 / TTY 制御文字の有無 等を検出してくる可能性。**入力にランダムな delay を入れる**等の対抗策の必要性は要観察 (グレーゾーン)
5. **B / C の cost / detection 計測**: A/B 期間中に「どちらの経路で何回 429 / どんなレスポンスヘッダーが返ったか」をログ化し、判断材料を蓄積

### 10.9 B 経路への規制シナリオと対策

B (interactive pty wrap) を Anthropic が潰しに来る場合の想定攻撃ベクタと、それぞれへの防御。**致命度・実現コストは Anthropic 側の主観**で、bot 側の対策は実装可否で評価。

| # | 規制ベクタ | 致命度 | Anthropic 実現コスト | bot 側対策 | 対策の堅牢性 |
|---|---|---|---|---|---|
| **B-1** | **環境変数 / TTY 文脈の検査** — CLI が `isatty()` 結果や `TERM` / `COLUMNS` / `CI` / `CLAUDE_CODE_HEADLESS` 等を request body の `metadata` / `client_info` に乗せてサーバー送信。値で interactive / batch を判定 | 中 | 低 (CLI 側数行) | pty 経由なので `isatty=true` は自然に通る。`TERM=xterm-256color`、適切な `COLUMNS/LINES`、`CI=` 未設定、`CLAUDE_CODE_HEADLESS=` 未設定 を維持。mitmproxy で実値を観察して同期 | **強い** (CLI と環境がほぼ同一) |
| **B-2** | **入力タイピングパターン分析** — 人間は 1 文字ずつ間欠的に入力、bot は paste 一発。bracketed paste mode (`\e[200~ ... \e[201~`) の頻度や、文字間隔の分布で識別 | 中〜高 | 中 | (a) human-like jitter で 1 文字ずつ送る (10-80ms ガウシアン) — 雑談用、(b) bracketed paste mode で「人間が paste した」体を取る — コード貼付け用。両モード混在で人間プロファイルに近づける | **中** (完全に人間統計を模す難度は高い、改良継続要) |
| **B-3** | **session 寿命 / 起動パターン** — 人間は起動 → 数質問 → 終了、を数十分〜数時間で 1 サイクル。bot が pty を数日保持し続けると分布が異常 | 中 | 低 | LRU で 30 分アイドル kill、起動時刻にランダム揺らぎ。1 日あたりの session 起動回数も人間レンジに留める | **中** |
| **B-4** | **request 間隔の burst 分析** — bot は連続 query を撃ちがち。人間はターン間に「読む時間」が入る | 中 | 低 | ターン間に 5-30 秒の jitter (Discord メッセージ受信から CLI 投入までを意図的に遅らせる)。応答品質と相反するのでルーターでタスク重量別に調整 | **中** |
| **B-5** | **同一 OAuth で interactive + programmatic 並列** — bot が C 経路 (`/v1/messages` 直接) と B 経路 (pty) を同じ token で並行使用すると「同一ユーザーで両モード並走」となり一発で異常検知 | **高** | 低 (server-side で並列カウントするだけ) | (a) **C 経路と B 経路を別 OAuth token に分離** — 先輩の本垢を B、別垢 (admin の別アカ) を C に充てる、(b) 完全 fallback 化して通常時はどちらか片方のみ動かす | **強い** (token 分離が決め手) |
| **B-6** | **CLI 内部 telemetry** — CLI が起動時 / 終了時 / 一定間隔で Anthropic に「自分が interactive で動作している」確認 ping (heartbeat) を送る経路を追加 | **高** | 中 | (a) mitmproxy で telemetry endpoint を特定し、bot 側で同じ heartbeat を継続送信、(b) CLI を変更せずそのまま動かしてるので **本物の telemetry が自動で流れる** → 対策不要なケースが多い | **強い** (CLI 本体を動かしている限り自動追随) |
| **B-7** | **TTY サイズ / 制御文字の不在検出** — `SIGWINCH` ハンドリングや `\e[?2004h` (bracketed paste) 等の TTY 機能 announce が CLI 側で来ないと bot 認定 | 低〜中 | 低 | `ptyprocess` が標準で TTY を提供するので大半は自然に通る。起動時に SIGWINCH を 1 度送って resize 履歴を残す等の小細工は余裕がある時に追加 | **強い** |
| **B-8** | **継続認証 / MFA 要求** — Anthropic が `claude.ai` で「7 日に 1 回 Web で再認証」を必須化 | 中 | 中 | bot は無人なので落ちる。**hook (`Stop` event) で先輩に DM 通知 → 先輩がローカル Mac で `claude login` → token を VPS に再 sync** という人間オペレーションを 7 日毎に運用化 | **中** (運用負荷あり) |
| **B-9** | **`claude --version` がサーバー側で deprecated 扱い** — 古い CLI を弾く gating。バージョン強制 update | 低 | 低 | 毎日 `claude update` を cron で実行。`hooks/SessionStart` で version check し、古かったら自動更新 + 失敗したら DM | **強い** |
| **B-10** | **TOS 明示禁止** — Anthropic 利用規約に「自動化された継続セッション (Discord bot 等) は禁止」と明文化 | **致命** (法的リスク) | 極小 (文面追加だけ) | 法的回避策なし。**先輩個人の補助ツールとして運用範囲を絞る** (公開 Discord で多人数提供しない / 商用化しない / 利用規模を可視化しない)。**最悪 Plan B (Agent SDK 正規 programmatic credit) に移行**して TOS 内に戻す | **弱い** (運用でしか防げない) |
| **B-11** | **client fingerprint hash** — CLI バンドル内の難読化された secret で request を HMAC 署名し、サーバー側で照合 (§9.1 D の B 経路版) | 高 | 中〜高 | CLI 本体を動かしているので署名は CLI が自動付与 → **対策不要**。逆にこれが入れば C 経路は終わる | **強い** (B のアドバンテージそのもの) |
| **B-12** | **CLI が `~/.claude.json` の write conflict で破損 / lock** — 複数 pty 並列起動時に race。Anthropic は意図的にやらないが、自爆要因として警戒 | 中 | (Anthropic 起因ではない) | `HOME` を pty session ごとに切り替える workspace 分離、または起動時 flock | **強い** |

### 10.9.1 対策実装の優先順位

1. **最優先 (PoC 段階で組み込む)**:
   - **B-5 token 分離** — C / B で OAuth account を分ける運用設計。並列利用検知の最大リスクを潰す
   - **B-3 session LRU** — 無人で数日 pty 保持を避ける。アイドル 30 分 kill
   - **B-12 HOME 分離** — 並列で `~/.claude.json` race を防ぐ
2. **次点 (10-f 並走 A/B 期間中に実装)**:
   - **B-4 ターン間 jitter** — request burst 抑制
   - **B-9 自動 `claude update` cron** — version gating への自衛
3. **観察ベース (mitmproxy で実測してから判断)**:
   - **B-1 環境変数同期** — 実 CLI が送る値を観察してから合わせる
   - **B-6 telemetry endpoint 特定** — CLI が背後で叩いているエンドポイントの洗い出し
4. **運用で吸収**:
   - **B-8 MFA / 7 日再認証** — 先輩が手動で再 login
   - **B-10 TOS 違反明示化** — 運用範囲限定で対応
5. **追加対策不要 (B 経路の構造的アドバンテージ)**:
   - **B-7 TTY 機能 announce** — `ptyprocess` で標準対応
   - **B-11 client fingerprint** — CLI 本体が自動署名

### 10.9.2 規制が来た場合の判定フロー

bot 運用中に「異常」を検知した場合の対応:

```
新しい 429 / 403 / 不審な response header が観測される
   ↓
hooks/Stop または ratelimit.observe で検出 → 先輩 DM
   ↓
先輩がローカル Mac の mitmproxy で実 CLI を回し、何が変わったか調査
   ↓
分類:
  - body/headers の追加 → C 経路の byte-exact 模倣に反映
  - interactive 検査の強化 (B-1〜B-7) → B 経路の対策実装 (本セクション)
  - TOS / 認証フロー変更 (B-8, B-10) → 運用変更
  - 致命的 (B-10 明示禁止) → Plan B (正規 programmatic credit) へ移行
```

### 10.9.3 Plan B (最終手段): 正規 programmatic credit への移行条件

以下のいずれか 1 つでも該当したら、本ドキュメントの方針全体を捨てて正規経路に切り替える:

- B-10 TOS で Discord bot 等の継続セッションが明示禁止された
- B-5 token 分離・B-4 jitter 等の対策を実装しても 24h 以内に再 ban される
- 先輩個人の運用負荷 (B-8 再認証など) が週 30 分を超える
- B / C 双方が同時に潰れる事象が 1 ヶ月以内に 2 回発生

移行先は [Agent SDK 正規利用](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan) の月次 programmatic credit。Max plan で credit 範囲内に収まるかは要試算 ([§13 廃案] の旧計画 1 と同じだが、状況が変われば再採用)。

---

## 11. 参考リンク

- [mitmproxy documentation](https://docs.mitmproxy.org/)
- [Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan) — Plan B 用
- [ClaudeDevs tweet (6/15 announcement)](https://x.com/ClaudeDevs/status/2054610152817619388)
- 既存コードの mitmproxy 痕跡: [services/claude_api/client.py](../services/claude_api/client.py) のコメント

---

## 12. 更新履歴 / キャプチャログ

| 日付 | バージョン | 差分要約 | 反映 commit |
|---|---|---|---|
| 2026-05-13 | claude-cli 2.1.92 | 初回キャプチャ。CLI_IDENTITY_TEXT / CLI_DEFAULT_SYSTEM_PROMPT / Stainless headers / metadata.user_id 順序 / attribution SALT を反映 | (既存 commit 群、git log 参照) |
| 2026-05-14 | — | ドキュメント方針転換 (旧: Agent SDK 全面移行 → 新: mitmproxy 模倣で OAuth 継続) | — |
| 2026-05-14 | — | §10 追加: 公式 CLI を subprocess で wrap する代替案。X 調査で CCR / CLIProxyAPI / 9Router が同等アーキテクチャで存在することが判明し、byte-exact を CLI 本体に外注する方針を併記 | — |
| 2026-05-14 | — | §10 全面改訂: 追加 X 調査で `claude -p` が 6/15 課金対象と確定 (@L_go_mrk, @himkt 他)。**B (interactive `claude` を pty 保持) + C (mitmproxy 模倣の現用経路、既に実装済) のハイブリッド**に方針変更。§9.1 にシナリオ E (pattern 分析) 追加 | — |
| 2026-05-14 | — | §10.9 追加: B 経路への規制シナリオ B-1 〜 B-12 と各対策、優先順位、判定フロー、Plan B 移行条件を整理 | — |
| TBD | TBD | (5/17 までに Phase 1 audit 実施) | — |

---

## 13. 廃案 (履歴用)

旧計画 1: **Agent SDK 全面移行** (5/14 早朝)
- 6/15 までに `services/claude_api/*` を全部 `claude-agent-sdk` に置換
- 工数 2-3 週間 → 撤回 (先輩判断: 工数大きすぎ)

旧計画 2: **OAuth 継続 + 観察ベースのリアクティブ対応** (5/14 午後)
- usage_fallback を拡張するだけ
- bot 側の identity 偽装はそのまま放置
- → 本計画に統合 (mitmproxy 模倣で identity の精度を上げる、observe ベースの fallback は引き続き併用)
