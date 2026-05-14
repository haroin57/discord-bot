# 2026-05-14 CLI 通信キャプチャ解析報告

**目的**: 公式 claude CLI (2.1.92) が Anthropic API に送る通信を mitmproxy でキャプチャし、bot のリクエストとの差分から「overage (extra usage) 経路に乗らない原因」を特定する

**取得方法**:
- VPS で `mitmdump --listen-host 127.0.0.1 --listen-port 8888 -w <out>.mitm`
- `HTTPS_PROXY=http://127.0.0.1:8888 NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem claude -p "..." --model claude-opus-4-6`
- 2 ケース (VSCode 拡張内 / clean env)、両方とも 7d quota 100% rejected の状態で 200 OK + `overage-in-use: true` を獲得

---

## 1. 結論: overage 経路に乗る条件

レスポンスヘッダー (CLI / 素 CLI 共通、bot で観測されない値):

```
anthropic-ratelimit-unified-overage-status: allowed
anthropic-ratelimit-unified-overage-utilization: 0.0
anthropic-ratelimit-unified-overage-reset: 1780272000
anthropic-ratelimit-unified-upgrade-paths: overage
anthropic-ratelimit-unified-overage-in-use: true   ← これが true で extra usage 経路で課金
```

bot は同じ subscription 状態 (7d 100%) で **`overage-in-use: true` を引き出せていない** = 何らかのリクエスト要素が server から `bot` を「extra usage 適格な利用」と認識されていない。

---

## 2. 決定的な差分 (CLI → bot)

### 2.1 `system[1]` の identity text — **最重要候補**

| | 値 |
|---|---|
| 現在の bot ([services/claude_api/client.py:51](../../services/claude_api/client.py#L51)) | `You are Claude Code, Anthropic's official CLI for Claude.` |
| CLI 実測 (両ケース共通) | `You are a Claude agent, built on Anthropic's Claude Agent SDK.` |

過去コメントに `server-side OAuth-subscriber gating appears to inspect this string` と書かれていて、Anthropic が string match で identity を見ている可能性が極めて高い。

### 2.2 `system[0]` billing header — **重要候補**

| | 値 |
|---|---|
| bot 構造 ([client.py:212](../../services/claude_api/client.py#L212)) | `x-anthropic-billing-header: cc_version=2.1.92; cc_entrypoint=cli; cch=00000;` |
| CLI 実測 (素 env) | `x-anthropic-billing-header: cc_version=2.1.92.a35; cc_entrypoint=sdk-cli; cch=00000;` |
| CLI 実測 (VSCode 拡張) | `x-anthropic-billing-header: cc_version=2.1.92.353; cc_entrypoint=claude-vscode; cch=00000;` |

- `cc_version`: bot は suffix なし `2.1.92`、CLI は **4 文字 hex suffix** (`a35`, `353`)。`get_attribution_header_value` の hash 計算と関係している可能性
- `cc_entrypoint`: bot は `cli`、CLI は `sdk-cli` (素) / `claude-vscode` (拡張内)。**`cli` という値は今の CLI が送らない値になっている**

### 2.3 `User-Agent`

| | 値 |
|---|---|
| bot ([client.py:22](../../services/claude_api/client.py#L22)) | `claude-cli/2.1.92 (external, cli)` |
| CLI 素 env | `claude-cli/2.1.92 (external, sdk-cli)` |
| CLI VSCode 拡張 | `claude-cli/2.1.92 (external, claude-vscode, agent-sdk/0.2.140)` |

### 2.4 `anthropic-beta` ヘッダー

| | 値 |
|---|---|
| bot ([client.py:28-38](../../services/claude_api/client.py#L28-L38)) | `claude-code-20250219,oauth-2025-04-20,context-1m-2025-08-07,interleaved-thinking-2025-05-14,redact-thinking-2026-02-12,context-management-2025-06-27,prompt-caching-scope-2026-01-05,advanced-tool-use-2025-11-20,effort-2025-11-24` (9 個) |
| CLI 実測 (両ケース共通) | `claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,context-management-2025-06-27,prompt-caching-scope-2026-01-05,advanced-tool-use-2025-11-20,effort-2025-11-24` (7 個) |

bot が **不要な 2 つ** を送っている: `context-1m-2025-08-07`, `redact-thinking-2026-02-12`。これらが server-side gate で「古い CLI」と判定される要因になっている可能性。

### 2.5 `x-client-request-id` ヘッダー

| | 値 |
|---|---|
| bot | 送っていない |
| CLI | リクエスト毎に UUID v4 を生成 (`7a6c0c22-897b-41b2-97c0-f52df931a23c`) |

ログ追跡 ID。server-side で「真っ当な client は毎回付ける」と判定している可能性は中程度。

### 2.6 body の `extra_body` フィールド

| | 値 |
|---|---|
| bot ([tool_loop.py:237](../../services/claude_api/tool_loop.py#L237)) | `extra_body={"output_config": OUTPUT_CONFIG}` |
| CLI 実測 | `extra_body: null` (送っていない) |

bot が **送ってはいけないフィールド** を送っている。`output_config` はもう CLI 側で消えた可能性。

---

## 3. その他差分 (影響度小)

| 項目 | bot | CLI | コメント |
|---|---|---|---|
| `anthropic-dangerous-direct-browser-access: true` | 送ってる? | 送ってる | 要確認 |
| `Accept: application/json` | OK | OK | 一致 |
| `anthropic-version: 2023-06-01` | OK | OK | 一致 |
| `accept-encoding` | OK | `br, gzip, deflate` | bot も同じ想定 |
| `metadata.user_id` 構造 | `{device_id, account_uuid, session_id}` 順 | 同じ | 一致 |
| `thinking: {"type": "adaptive"}` | OK | OK | 一致 |
| `max_tokens: 64000` | OK | OK | 一致 |

---

## 4. 推奨修正 (優先度順)

### Priority 1: system[1] identity text の更新

```python
# services/claude_api/client.py:51
CLI_IDENTITY_TEXT = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
```

### Priority 2: cc_entrypoint と User-Agent

```python
# services/claude_api/client.py:17, 22
CC_ENTRYPOINT = "sdk-cli"  # was "cli"
USER_AGENT = f"claude-cli/{CC_VERSION} (external, {CC_ENTRYPOINT})"
```

### Priority 3: anthropic-beta の整理

```python
# services/claude_api/client.py:28-38
MODEL_BETAS = [
    "claude-code-20250219",
    "oauth-2025-04-20",
    # "context-1m-2025-08-07",       ← 削除
    "interleaved-thinking-2025-05-14",
    # "redact-thinking-2026-02-12",  ← 削除
    "context-management-2025-06-27",
    "prompt-caching-scope-2026-01-05",
    "advanced-tool-use-2025-11-20",
    "effort-2025-11-24",
]
```

### Priority 4: extra_body の output_config を外す

```python
# services/claude_api/tool_loop.py:229-238
stream_kwargs = dict(
    model=model,
    max_tokens=max_tokens,
    system=cached_system,
    messages=messages,
    betas=extra_betas,
    metadata=get_metadata(),
    thinking=THINKING_CONFIG,
    # extra_body={"output_config": OUTPUT_CONFIG},  ← 削除
)
```

### Priority 5: x-client-request-id を毎回付与

```python
# services/claude_api/entry.py:_make_api_client 内 cli_headers
import uuid as _uuid
cli_headers["x-client-request-id"] = str(_uuid.uuid4())
```

ただし client は使い回されるので、リクエスト毎に変える必要あり → httpx の request hook で動的注入が綺麗。

### Priority 6: cc_version suffix を動的化

CLI 実測 `2.1.92.a35` のような **4 文字 hex suffix** がリクエスト毎に変わっている。これは現状の bot の `get_attribution_header_value` でも一部実装されているが、形式が違う可能性。要追加調査。

---

## 5. 検証手順

1. Priority 1-4 を bot に反映してから bot 再起動
2. 先輩がもう一度発話
3. journalctl で `rate-limit observed` ログを確認:
   - `anthropic-ratelimit-unified-overage-in-use: true` が含まれれば **解決**
   - 引き続き `overage-in-use` が無く `status: rejected` だけなら、まだ何か差分あり → Priority 5-6 や他のヘッダーを反映
4. キャプチャを再度取り直して bot リクエストと完全 diff を取る (`scripts/mitm_to_bot_diff.py` を作成予定)

---

## 6. キャプチャファイル

- `docs/captures/A_one_shot/flow_001_*.json` — VSCode 拡張内で `claude -p` 実行
- `docs/captures/B_clean_env/flow_001_*.json` — env クリアして素の `claude -p` 実行
- 両方 **redact 済**: `Authorization: Bearer ***REDACTED***`, `set-cookie: ***REDACTED***`
- 生 `.mitm` ファイルは `.gitignore` で commit から除外

---

## 7. 重要: 漏洩リスク

- mitmproxy ダンプには OAuth Bearer Token (`sk-ant-oat01-...`) が **平文で含まれていた**
- ダンプ → JSON 化 → redact ツールでマスクする pipeline を経たので docs/captures/ 配下は安全
- 生 `.mitm` は `/tmp/cli_capture_*.mitm` に残っているので解析完了後に削除推奨:
  ```bash
  rm /tmp/cli_capture_A.mitm /tmp/cli_capture_B_clean.mitm
  ```

---

## 8. 次のアクション

[ ] 先輩判断: 上記 Priority 1-4 を反映する修正をやっていいか
[ ] 反映後、再度 mitmproxy キャプチャを取って bot リクエストとの完全 diff
[ ] `scripts/mitm_to_bot_diff.py` を作成して以降のキャプチャ毎に自動 diff
