# SecondOpinion MCP サーバー

Discord bot 内で実装した「他社モデル並列相談」(`get_second_opinions`) を、Claude Code
等の CLI から**サブエージェント**として呼べるように MCP パッケージ化したもの。

- 実体: `second_opinion_mcp_server.py`（リポジトリ root、FastMCP）
- 公開 tool: `second_opinion(question, context="")` 1 本
- 訓練データの違う 3 モデルに並列で第二意見を聞く。自分の推論の死角を補完する用途

## 経路（すべてサブスク枠優先 → API 直フォールバック）

| vendor | 一次（従量課金ゼロ） | フォールバック |
|---|---|---|
| GPT | codex CLI（ChatGPT サブスク枠, `gpt-5.5`） | OpenAI API（`OPENAI_API_KEY`） |
| Gemini | agy CLI（AI Pro サブスク枠, Gemini 3.1 Pro High） | Google 直 API（`SECOND_OPINION_GEMINI_KEY` / `GOOGLE_AI_API_KEY`） |
| DeepSeek | — | OpenRouter or OpenCode Zen（`deepseek-v4-flash`） |

`SECOND_OPINION_OPENAI_MODE` / `SECOND_OPINION_GEMINI_MODE` = `auto`（既定）/ `codex`(=agy 固定) / `api`。

実測: MCP 経由 3 社並列で **21 秒・全社サブスク枠グリーン**（2026-07-03）。

## 登録（Claude Code CLI）

```bash
claude mcp add second-opinion -- \
  /home/ubuntu/discord-bot/.venv/bin/python \
  /home/ubuntu/discord-bot/second_opinion_mcp_server.py
```

または `.mcp.json` / `~/.claude.json` の `mcpServers` に:

```json
{
  "mcpServers": {
    "second-opinion": {
      "command": "/home/ubuntu/discord-bot/.venv/bin/python",
      "args": ["/home/ubuntu/discord-bot/second_opinion_mcp_server.py"]
    }
  }
}
```

`.venv` の python を使うのが肝（`openai` / `mcp` / `python-dotenv` 依存が入っている）。

## 実装上の注意（ハマりどころ）

- **`stdin=DEVNULL` 必須**: MCP サーバーの stdin は JSON-RPC パイプ（開きっぱなし）。
  codex/agy をこれを継承したまま spawn すると、子が対話入力待ちで**丸ごとハングして
  300s タイムアウト**する。`agy_runner.py` / `codex_runner.py` の subprocess spawn は
  `stdin=asyncio.subprocess.DEVNULL` を指定する（回帰テストあり）。standalone だと
  親 python の stdin が即 EOF なので刺さらず、MCP 経由でだけ再現する厄介なやつ。
- **PATH 補強**: CLI から spawn される MCP サーバーは最小 PATH になり得る。サーバー
  起動時に `~/.local/bin`（agy）と volta の node bin（codex）を PATH 先頭に足している
  (`_augment_path`)。
- **API キーを .env / env に置かない**: `GEMINI_API_KEY` / `ANTIGRAVITY_API_KEY`（agy）、
  `OPENAI_API_KEY`（codex）を環境に置くと CLI が OAuth をバイパスして従量課金 API に
  流れる。runner 側で `_clean_env()` が除去するが、そもそも置かないのが前提。
- **副作用ガード**: codex/agy は承認ゲート無しの自律エージェント。prompt 先頭に
  「ファイル作成・コマンド実行・ネット送信を一切せず文章で答えるだけ」の `_AGY_GUARD`
  を付け、専用の空 workdir（`.state/{agy,codex}_workdir`）に隔離して実行する。

## 前提セットアップ（先輩の実機操作）

- codex CLI: `~/.codex/auth.json` が `auth_mode: "chatgpt"`（ChatGPT アカウントでログイン済み）
- agy CLI: AI Pro アカウントで OAuth ログイン済み（`agy --version` が通り認証済み）

どちらも切れていれば API 直フォールバックに落ちる（DeepSeek は常に API）。
