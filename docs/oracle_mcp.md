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

## 追補（2026-07-04）: search デフォルト off パッチ — 「運用ルール」から「デフォルト安全」へ

上記の search:false ルールを consult 呼び出し時に渡し忘れ、cf-radical-offload-design
系 4 セッションが連続 429 死した。忘れると死ぬルールは仕組みで封じるべきなので、
デフォルトを反転するローカルパッチを追加した。

### 実測で確定した失敗メカニズム（概念検証 2026-07-04）
1. oracle は Responses API の **background モード**でジョブを投げる（ローカルを
   切っても OpenAI 側で走り続ける。MCP ツール呼び出しをクライアント側で拒否/中断
   してもセッションは開始済みになり得る — 実際に -4 は拒否後も running だった）
2. search 有効だと、サーバー側で web_search を挟む多段ラウンドが回る。**各ラウンドの
   admission 予約（入力累積 + max_output クランプ値）が同一 1分 TPM 窓に積み上がる**
3. 実測の数字: round1 予約 20,362 + 中間消費 10,570 = Used 30,932 の状態で
   round2 が 20,362 を要求 → 50,000 を 1,294 超過して 429 → **background ジョブは
   pacing せず恒久 fail**（usage は null のまま = 課金上は未生成でも窓は占有される）
4. 検証: search 無しの同一ブリーフ（6.4k 入力）を background 直叩き →
   3分で完走（reasoning 9,149 + 本文 12,000）。**search の有無だけで生死が分かれる**
5. 歴代セッション 22 件の成績: gpt-5.5-pro の実プロンプトは **18 連敗**
   （完走は一文プロンプト 1 件のみ）。gpt-5.5(非Pro) は 2/2 完走

### パッチ内容
| ファイル | 変更 |
|---|---|
| `src/oracle/run.js` | `searchEnabled` を `ORACLE_DEFAULT_SEARCH=off` なら opt-in（`search===true` のみ有効）に反転 |
| `scripts/oracle_mcp_launch.sh` | `ORACLE_DEFAULT_SEARCH=off` をデフォルト export |

- 検索が本当に必要な consult だけ `search: true` を明示（そのときは gpt-5.5 非Pro か、
  入力を小さくして TPM 窓に多段ラウンドが収まるようにする）
- **npm update で run.js のパッチは消える**（上の表と同じ運用）
- 中断したい background ジョブは `POST /v1/responses/{resp_id}/cancel` で殺せる
  （resp_id はセッションの output.log に記録される）。放置すると窓を占有し続け
  後続セッションまで連鎖死する

## 追補2（2026-07-04）: パッチの永続化 — 自己修復機構

「npm update でパッチが消える」問題を仕組みで解決した。**手動再適用は不要になった。**

### 構造
| 場所 | 役割 |
|---|---|
| `infra/oracle-patches/<version>/files/` | パッチ済みファイルの正本（バージョン別、git 管理）。0.15.0 は 5ファイル: run.js / consult.js / types.js / utils.js / sessionManager.js |
| `infra/oracle-patches/<version>/oracle-<version>.diff` | 素の npm 版との差分（レビュー用文書。適用には使わない） |
| `scripts/oracle_apply_patches.sh` | 正本をインストール先へ冪等コピー。volta shim を `volta which` で実体解決。**バージョン不一致なら適用せず警告**（壊れた適用より無パッチ優先） |
| `scripts/oracle_mcp_launch.sh` | MCP スポーンのたびに apply を実行 → npm update で剥がれても**次回起動で自己修復** |

### 検証済み（2026-07-04）
- 素の run.js に戻す（npm update 相当）→ apply で復元・marker 復活・モジュールロード OK
- 冪等（差分ゼロなら無出力・no-op）
- launcher 経由のフルチェーン起動 OK

### oracle をバージョンアップする時の手順
1. `npm update -g @steipete/oracle` → 新バージョン確認
2. `npm pack @steipete/oracle@<new>` で素を取得し、旧 diff（infra/oracle-patches/<old>/*.diff）を参考に同等パッチを新 dist へ移植
3. `infra/oracle-patches/<new>/files/` に移植済みファイルを置き、素との diff も保存
4. launcher 起動（または apply 手動実行）で適用確認 → commit
