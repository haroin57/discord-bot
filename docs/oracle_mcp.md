# Oracle MCP — gpt-5.5-pro を Responses API 直で叩く

steipete/oracle (v0.15.0) を MCP として導入。**gpt-5.5-pro / gpt-5.5** 等を
OpenAI **Responses API 直**で叩ける（codex CLI では 400 で弾かれた最上位 Pro モデルに、
tier 十分なら正規に届く）。ChatGPT の Web 自動化（ブラウザ）モードも oracle は持つが、
**本構成では使わない**（規約クリーンな API 直経路のみ）。

## 実測（2026-07-03）
- `gpt-5.5`: Responses API 直で 2-3秒、$0.023/回、tier 十分
- `gpt-5.5-pro`: 同じく受理（tier OK）。ただし Pro は server-side reasoning で
  **10-15分（最大60分）**かかり、`api background` で detach → 後で回収する

## 構成
- 本体: `npm install -g @steipete/oracle`（Node 24+、volta の node を使用）
- ランチャー: `scripts/oracle_mcp_launch.sh`
  - PATH に `~/.volta/bin` を足す（CLI spawn 時の最小 PATH 対策）
  - `.env` から `OPENAI_API_KEY`（と `GOOGLE_AI_API_KEY`→`GEMINI_API_KEY`）を実行時注入
    （キーを `.mcp.json` に直書きせず git に乗せない）
- 登録: `.mcp.json` の `mcpServers.oracle`

## 公開ツール（oracle-mcp）
- `consult` — Oracle session を実行（`prompt`, `model`, `files` 等）。**これがメイン**。
  例: `consult(prompt="...", model="gpt-5.5-pro", files=["services/foo.py"])`
- `chatgpt_image` — ChatGPT browser 画像生成（browser モード、本構成では未使用）
- `project_sources` — ChatGPT Project の Sources 管理
- `sessions` — session 履歴の照会（`ORACLE_HOME_DIR`、CLI と共有）

## 使い分け（SecondOpinion との役割）
- **SecondOpinion** (`second_opinion_mcp_server.py`): GPT-5.5 + Gemini + DeepSeek の
  **3社並列・即答**（数十秒）。訓練データの違うモデルで死角を補完する用途
- **Oracle** (`consult`): **gpt-5.5-pro 単体・深考**（10-15分、大きいファイル文脈 + server-side
  search）。難問を1モデルにじっくり考えさせる用途

## CLI 直接利用
```bash
export PATH="$HOME/.volta/bin:$PATH"
oracle -m gpt-5.5-pro -p "質問" -f path/to/file   # Pro は --wait で張り付き
oracle session <slug>                             # detach したジョブの回収
```

## 注意
- gpt-5.5-pro は課金（Responses API 従量）。SecondOpinion の codex(gpt-5.5) は
  サブスク枠でコストゼロ。**深考が要る難問だけ oracle の Pro を使う**方針で ROI を守る
- tier 制限: 現状 tier は gpt-5.5 / gpt-5.5-pro とも通過を確認済み

## ローカルパッチ（2026-07-03, v0.15.0 の dist を直接修正）

### 症状（パッチ前）
- MCP/CLI から gpt-5.5-pro に投げると `Request too large ... TPM: Limit 50000, Requested 55801` で恒久失敗
- 失敗した background response が **queued のまま TPM 予約を保持**し、後続リクエストまで
  `Used 44k+` で連鎖失敗（60秒待っても回復しない）。クライアントは10分ポーリングし続ける
- slug が2語以下だと `Custom slug must include between 3 and 5 words.` で即死

### 根本原因
1. consult MCP ツールに `maxOutput` パラメータが**存在せず**、`max_output_tokens` 未指定で送信。
   OpenAI の TPM 会計は「入力推定 + max_output_tokens（未指定時はモデル最大出力 ≈48k）」を
   予約するため、TPM 50k の org では入力サイズに関係なくほぼ即死
2. CLI 側にも env/config でのデフォルト解決が無い（`--max-output` フラグのみ）
3. `normalizeCustomSlug()` が語数不足を throw（切り詰め側は自動なのに不足側は救済なし）

### 当てたパッチ（`~/.volta/tools/image/packages/@steipete/oracle/lib/node_modules/@steipete/oracle/dist/`）
| ファイル | 内容 |
|---|---|
| `src/oracle/run.js` | `ORACLE_MAX_OUTPUT_TOKENS` env をデフォルト値に。`ORACLE_TPM_LIMIT` env 設定時は「入力推定 + margin(2000) + 出力」が TPM 内に収まるよう `max_output_tokens` を自動クランプ（最低 4000 を割る入力は分かりやすいエラーで即死、リトライ地獄を防止） |
| `src/mcp/types.js` | `consultInputSchema` に `maxOutput`（int, positive, optional）追加 |
| `src/mcp/tools/consult.js` | ツールスキーマに `maxOutput` 公開 + runOptions へ配線 |
| `src/mcp/utils.js` | `mapConsultToRunOptions` が `maxOutput` を受理 |
| `src/sessionManager.js` | slug 語数チェックを撤去（slugify の切り詰め/フォールバックのみで受理） |
| `scripts/oracle_mcp_launch.sh`（本リポジトリ） | `ORACLE_TPM_LIMIT=50000` / `ORACLE_MAX_OUTPUT_TOKENS=24000` をデフォルト export |

### 検証（2026-07-03）
- 単体: 入力31,456tok → max_output 16,544 に自動クランプ（合計ちょうど50,000）/
  env 未設定時は上流と同一挙動 / 入力過大時は PromptValidationError で即死
- 実弾: 争点裁定プロンプト（入力4.98k）が queued → in_progress へ正常遷移（パッチ前は恒久 429）

### 運用上の注意
- **`npm update -g @steipete/oracle` でパッチは消える**。更新後はこの表を見て再適用するか、
  upstream に `maxOutput` MCP パラメータ + TPM クランプが入ったか確認する
- TPM 上限が上がったら `.env` で `ORACLE_TPM_LIMIT` を上書き（launch.sh のデフォルトより優先）
- **TPM 50k のうちは gpt-5.5-pro には `search: false` を渡すこと**（consult パラメータに既存）。
  oracle のデフォルトは search 有効で、Pro の background 実行はサーバー側で web_search を
  挟んだ多ターンを回す。**各ターンが TPM 会計に載るため、実行途中で自分のトークン消費に
  轢かれて `status=failed`（Used 49k / try again in 20s）になる**のを実測済み（2026-07-03）。
  コードレビュー・設計判断など検索不要なタスクでは search off が安全。web 検索が要る調査は
  SecondOpinion（サブスク枠）か gpt-5.5（非Pro, 短時間）に逃がす
