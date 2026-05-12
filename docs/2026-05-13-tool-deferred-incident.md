# 2026-05-13 インシデントレポート：Discord bot が無言で応答する事象

## TL;DR

- **症状**：Discord bot が「`(No response from Claude)`」あるいは完全に無言で応答するターンが発生
- **頻度**：Mem0 などの MCP ツールを使用したターンで発生し得る
- **真因**：Claude Code CLI v2.1.92 の **"Deferred Tools / ToolSearch" 最適化** が agent loop を `result=""` で打ち切る仕様
- **対応**：CLI subprocess に `ENABLE_TOOL_SEARCH=false` を渡して機構自体を無効化 + ストリーム TextBlock を蓄積するフォールバック層を追加

## 1. 観測された事象

### 1-1. ユーザー視点

2026-05-13 02:08 (JST) 頃、Discord チャンネル `1490247632847896776` で先輩が下記のメッセージを送信:

> 俺くらいの粒度の人間はいくらでも居て、それが怖いところだと思うよ

これに対して bot は応答しなかった。次のターンで先輩が「Mem0 のバグかな、VPS サーバー入ってデバッグするわ」と再送し、ログ調査依頼がかかった。

### 1-2. bot.log で確認できる痕跡

```
2026-05-13 02:08:21,969 INFO Mem0: injected 5 memories for user ...0410
2026-05-13 02:08:21,969 INFO Request from user=573489206240870410 channel=1490247632847896776 admin=True len=667
2026-05-13 02:08:29,434 INFO [ch=1490247632847896776] 📡 system: subtype=init
2026-05-13 02:08:57,404 INFO [ch=1490247632847896776] ✅ done: turns=3 tools=2 elapsed=35.1s api=20.4s cost=$0.3526 tokens_in=7 tokens_out=753 error=False
2026-05-13 02:08:57,734 INFO Response via claude-opus-4-6 (restricted=False) len=0
2026-05-13 02:08:58,048 INFO Mem0 ingest: uid=573489206240870410 user_text=32 assistant=25
```

決定的なシグナル:

| 行 | 意味 |
|---|---|
| `len=0` | SDK が返した `response_text` が空文字列 |
| `assistant=25` | Discord に送られた文字列長 = `(No response from Claude)` の 25 文字 |

つまり SDK が空文字列を返し、bot 側のフォールバック分岐 [services/claude_sdk.py:635](../services/claude_sdk.py#L635) で `"(No response from Claude)"` が送信された。

### 1-3. Claude Code SDK セッション JSONL に残った実際のモデル出力

`/home/ubuntu/.claude/projects/-home-ubuntu/66925435-573b-4551-b3f0-5ec0afa84f67.jsonl` の該当ターンを抜粋:

```jsonc
// 1. ToolSearch を呼んで mcp__mem0__add のスキーマを取得
{"type":"assistant","timestamp":"2026-05-12T17:08:32.991Z",
 "message":{"content":[{"type":"tool_use","name":"ToolSearch",
   "input":{"query":"select:mcp__mem0__add","max_results":1}}],
   "stop_reason":"tool_use"}}

// 2. ToolSearch の結果
{"type":"user","timestamp":"2026-05-12T17:08:33.092Z",
 "message":{"content":[{"type":"tool_result",
   "content":[{"type":"tool_reference","tool_name":"mcp__mem0__add"}]}]}}

// 3. 同一 message id (msg_012BZBkV...) の chunk #1：text
{"type":"assistant","timestamp":"2026-05-12T17:08:42.373Z",
 "message":{"id":"msg_012BZBkVmXQKKxMX5sBJ8saR",
   "content":[{"type":"text",
     "text":"それ、本当にそうですか？\n\n「俺くらいの粒度の人間はいくらでも居る」って言いますけど..."}],
   "stop_reason":null}}

// 4. 同一 message id の chunk #2：tool_use（deferred tool）
{"type":"assistant","timestamp":"2026-05-12T17:08:47.675Z",
 "message":{"id":"msg_012BZBkVmXQKKxMX5sBJ8saR",
   "content":[{"type":"tool_use","name":"mcp__mem0__add", ... }],
   "stop_reason":"tool_use"}}

// 5. mcp__mem0__add の結果
{"type":"user","timestamp":"2026-05-12T17:08:55.153Z",
 "message":{"content":[{"type":"tool_result",
   "content":"{\"result\":\"stored 0 new memories for user=573489206240870410\"}"}]}}

// 6. ← ここで JSONL 終了。assistant の追加発話なし。
```

ポイント:

- **モデルは「それ、本当にそうですか？...」という Discord に届けるべき本文を生成していた**（chunk #3）
- それと**同じ API message** の中で deferred tool（`mcp__mem0__add`）も呼び出した（chunk #4、同一 `msg_012BZBk...` id）
- tool_result が返った後（02:08:55）、Claude Code CLI はモデルに再質問せず**わずか 2 秒で agent loop を終了**（02:08:57 の `done`）
- その結果、ユーザーに届くべき本文「それ、本当に〜」が捨てられた

## 2. 原因の解剖

### 2-1. 直接原因：[services/claude_sdk.py:514](../services/claude_sdk.py#L514) の単純な代入

修正前のコード:

```python
elif isinstance(msg, ResultMessage):
    response_text = msg.result if hasattr(msg, "result") else str(msg)
```

`ResultMessage.result` だけを参照しており、ストリーム中に流れてきた `AssistantMessage.TextBlock.text` を無視していた。`msg.result` が `""` または `None` のとき、ストリーム中の有用なテキストごと捨てる構造。

### 2-2. 真因：Claude Code CLI v2.1.92 の "Deferred Tools / ToolSearch" 最適化

Claude Code CLI は、MCP サーバー（`mem0`、`brave-search` 等）のツールスキーマをプロンプトに upfront でロードする代わりに、以下の遅延ロード機構を採用している:

1. CLI 起動時、MCP ツールは**名前のみ**（例：`mcp__mem0__add`）モデルに通知される
2. モデルはスキーマが必要になったら、まず `ToolSearch` 組み込みツールでスキーマを取得する
3. そのうえで実際の MCP ツールを呼び出す
4. **`ToolSearch` 経由で取得した "deferred tool" を呼び出すと、CLI は agent loop を打ち切る**

CLI バイナリ `/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js` から抽出した該当箇所:

```js
// deferred tool 実行後の result message 構築
yield {
  type: "result",
  subtype: "success",
  is_error: false,
  duration_ms: ...,
  num_turns: ...,
  result: "",                       // ← ハードコードで空文字列
  stop_reason: "tool_deferred",     // ← 通常の end_turn ではない
  deferred_tool_use: { id, name, input },
  ...
};
```

モード判定式（`function j47()` を復元）:

```js
function j47() {
  if (truthy(process.env.CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS)) return "standard";
  let q = process.env.ENABLE_TOOL_SEARCH;
  let K = q ? parseAutoPrefix(q) : null;
  if (K === 0)   return "tst";
  if (K === 100) return "standard";
  if (falsy(q))  return "standard";   // ← ENABLE_TOOL_SEARCH=false で無効化可能
  return "tst";                        // ← デフォルトは ToolSearch 有効
}
```

つまり deferred tool が agent loop の最終アクションになった瞬間、CLI は「`result=""` で打ち切って終了」してしまう。モデルにツール結果を踏まえた最終応答を生成させる機会を奪う仕様になっている。

### 2-3. なぜ本文が捨てられたか

Anthropic API の仕様上、1 つの assistant message に `text` ブロックと `tool_use` ブロックを混在させることができる（実際 chunk #3, #4 は同じ `msg_012BZBk...` id を共有）。

- モデルは「ユーザーに返したい本文 + 副作用としてのメモリ書き込み tool_use」を 1 つのメッセージにまとめて出力した
- Claude Code SDK は streaming のために `text` 部分と `tool_use` 部分を 2 つの `AssistantMessage` Python オブジェクトに分割して yield した
- CLI はこの message の最終 stop_reason が `tool_use` で、かつ deferred tool だったため `tool_deferred` 経路で終了し、`result=""` を返した
- bot 側は `msg.result` だけ見ていたため、`text` 部分の存在に気付かず捨てた

### 2-4. なぜ「Mem0 のバグ」に見えたか

偶然の重なり:

1. このターンで mem0 の `add` を呼んだ → 直前のログ行に `Mem0` キーワードが多く残った
2. `Mem0 gate: facts=1 kept=0 skip(low=0,other=0,dup=1)` も同時に記録された（重複判定で skip）
3. ユーザーから見ると「mem0 を呼んだら無言になった」という体験

実際は mem0 の動作はすべて正常で、CLI の deferred tool 終了パスが諸悪の根源だった。

## 3. 対応

### 3-1. 第1層：CLI subprocess 環境変数で機構を無効化（恒久対策）

[config.py](../config.py) に定数を追加:

```python
CLAUDE_CLI_ENV: dict[str, str] = {
    "ENABLE_TOOL_SEARCH": "false",
}
```

[services/claude_sdk.py](../services/claude_sdk.py) の `ClaudeCodeOptions` 構築箇所（restricted/admin 両方）で `env=` に注入:

```python
# restricted mode
restricted_env: dict[str, str] = {**CLAUDE_CLI_ENV}
if RESTRICTED_ANTHROPIC_API_KEY:
    restricted_env["ANTHROPIC_API_KEY"] = RESTRICTED_ANTHROPIC_API_KEY
    restricted_env["ANTHROPIC_AUTH_TOKEN"] = ""
options = ClaudeCodeOptions(..., env=restricted_env)

# admin mode
options = ClaudeCodeOptions(..., env={**CLAUDE_CLI_ENV})
```

これにより:
- MCP ツールのスキーマが system prompt に upfront 配置される
- モデルは `ToolSearch` を経由せず直接 MCP ツールを呼び出せる
- tool_result 後も agent loop が継続し、`stop_reason: "end_turn"` で正常終了する
- `msg.result` が常に最終応答テキストで埋まる

### 3-2. 第2層：ストリーム TextBlock 蓄積（defense-in-depth）

将来の CLI 仕様変更や他の `result=""` 経路（`error_max_turns` 等）に備えた保険として、[services/claude_sdk.py](../services/claude_sdk.py) に以下を追加:

```python
# 初期化
stream_text: str = ""

# AssistantMessage の TextBlock ハンドラ内
elif isinstance(block, TextBlock) and block.text:
    stream_text = block.text  # 最後の TextBlock を保持
    ...

# ResultMessage 受信時
elif isinstance(msg, ResultMessage):
    result_text = getattr(msg, "result", None)
    if not result_text and stream_text:
        log.warning(
            "[ch=%d] ResultMessage.result empty (subtype=%s); "
            "falling back to stream TextBlock (%d chars)",
            channel_id, getattr(msg, "subtype", "?"), len(stream_text),
        )
        result_text = stream_text
    response_text = result_text or ""
```

第1層が破られたとき `warning` ログで検知できる。

### 3-3. テスト更新

[tests/test_e2e_security.py](../tests/test_e2e_security.py) の 2 ケース（`test_restricted_injects_api_key_env_when_configured`、`test_restricted_env_empty_without_config`）で、env dict に `ENABLE_TOOL_SEARCH=false` が常時含まれることをアサート。

## 4. トレードオフ

### 4-1. トークンコスト

2026-05-13 実測（同一プロンプト `just say ok`、MCP=`mem0`）:

| 項目 | ToolSearch on（旧挙動） | ToolSearch off（新挙動） |
|---|---|---|
| `result` フィールド | 状況により空 | 常に最終テキスト |
| `stop_reason` | `tool_deferred` 経路あり | `end_turn` 確定 |
| `cache_creation_input_tokens` (cold) | 23,267 | 32,719（+40%） |
| `cache_read_input_tokens` (warm) | 32,719 | 32,719（同等） |

cold-start のトークン量が +40% 増えるが、Discord bot は同チャンネルで会話が継続すれば warm cache に乗るため実コスト増は限定的。1 日あたり推定 +$0.05 未満。

### 4-2. レイテンシ

`ToolSearch` 1 ターン分（typically 1-2s）が削減される。むしろ高速化。

### 4-3. 副作用

`ENABLE_TOOL_SEARCH=false` は CLI の "experimental beta" 1 つを止めるだけで、他の機能（thinking、prompt caching、MCP 自体）には影響しない。

## 5. 検証方法

### 自動テスト

```bash
cd /home/ubuntu/discord-bot
.venv/bin/python -m pytest tests/test_e2e_security.py::TestRestrictedConcurrencyPool \
  tests/test_claude_api.py tests/test_coverage_improvements.py -v
```

→ 既存 72 件パス、新規アサート 2 件パスを確認済み。

### 手動 E2E

1. bot 再起動
2. Discord で「以前話した話題について教えて」など `_MEMORY_TRIGGERS` を含む発話
3. bot.log を tail:
   ```bash
   tail -f /home/ubuntu/discord-bot/bot.log | grep -E "len=0|tool_deferred|Response via|falling back"
   ```
4. 合格条件:
   - `len=0` が出ない
   - `Response via ... len=N` で N>0
   - `falling back to stream TextBlock` の warning が出ない（出たら第1層が破られたサイン）

### CLI 単体検証（実施済み）

```bash
ENABLE_TOOL_SEARCH=false claude --print --verbose --output-format=stream-json \
  --mcp-config /tmp/test_mcp.json --permission-mode bypassPermissions 'just say ok'
# → result: "ok", stop_reason: "end_turn"  ✓
```

## 6. ロールバック手順

問題発生時:

1. `config.py` の `CLAUDE_CLI_ENV` を `{}` に変更
2. bot 再起動

これで CLI のデフォルト挙動（ToolSearch 有効、deferred tools あり）に戻る。第2層のフォールバックは残るので、空応答事故は warning ログ付きで救済される。

## 7. 学び・予防策

### 7-1. SDK の戻り値を単一フィールドに依存させない

`ResultMessage.result` のような「ラップされた最終テキスト」が CLI 側の都合で空になる可能性を想定すべきだった。ストリーム中の生データ（`TextBlock`）を併用するのが堅牢。

### 7-2. CLI 仕様の experimental flag には注意

Claude Code CLI は `tengu_glacier_2xr` のような GrowthBook feature flag 経由で挙動を変えることがある。SDK 利用者から見えないところでデフォルトが変わるリスクを認識し、重要な挙動は env var で固定する。

### 7-3. ログに観測フックを残す

`len=0` パターンを定期監視するアラートを仕込むと、似た事象の早期検知ができる。第2層の `warning` ログがその役目を兼ねる。

## Appendix: 関連ファイル一覧

| ファイル | 役割 |
|---|---|
| [config.py](../config.py) | `CLAUDE_CLI_ENV` 定数定義（line 102 付近） |
| [services/claude_sdk.py](../services/claude_sdk.py) | env 注入、stream_text フォールバック |
| [tests/test_e2e_security.py](../tests/test_e2e_security.py) | env アサート更新 |
| [bot.log](../bot.log) | 該当ターンのログ（行 45011-45023） |
| `/home/ubuntu/.claude/projects/-home-ubuntu/66925435-573b-4551-b3f0-5ec0afa84f67.jsonl` | Claude Code SDK セッション JSONL |
| `/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js` | CLI v2.1.92 バイナリ（minified） |
