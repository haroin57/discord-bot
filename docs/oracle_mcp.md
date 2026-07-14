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

### 修正（2026-07-04 追加検証）: env だけでは MCP 経路で効かない → `~/.oracle/config.json` が正本
`ORACLE_DEFAULT_SEARCH=off`（run.js パッチ + launcher export）だけでは **MCP 経由の
consult で search が復活する**再発を確認した。原因は `dist/src/cli/runOptions.js:54`:

```js
const search = userConfig?.search !== "off";  // config 不在なら true を「明示」生成
```

MCP 経路（`mcp/utils.js` → `resolveRunOptionsFromConfig`）はこの行で undefined を
**明示的 `true` に実体化してから** run.js へ渡すため、run.js 側の「`search===true` の
時だけ opt-in」ゲートが「ユーザーが明示的に search を要求した」と誤認して素通りする。
CLI 直叩きは undefined のまま届くのでこの経路差で前回の概念検証をすり抜けていた。

**正本は `~/.oracle/config.json` の `{"search": "off"}`**。runOptions.js:54 がまさに
この userConfig を読むので、shared daemon / 野良 stdio / CLI の**全経路のデフォルトが
一括で off** になる。env とパッチはフォールバックとして残すが、実効的なスイッチは
この config ファイル。パッケージ更新でも消えない（インストール先ではなく HOME 配下）。

## 追補3（2026-07-04）: supergateway 切断時クラッシュのパッチ

oracle の shared daemon（`mcp-oracle-shared`, supergateway 3.4.3 で stdio→HTTP 中継）が
今日 2 回 exit-code で死んだ。systemd `Restart=always` で復帰するが、`KillMode=control-group`
のため**巻き添えで in-flight の background ジョブが孤児化**する。

### メカニズム
- gpt-5.5-pro consult は数分かかる。その間にクライアントが接続を切る（キャンセル/中断）
- 遅れて oracle 子プロセスが最終応答を返すと、supergateway が閉じた接続へ `transport.send()`
  しようとする。`dist/gateways/stdioToStatelessStreamableHttp.js:120` の send は **async
  なのに同期 try/catch しか無く**、`Error: No connection established for request ID: N` が
  **unhandled rejection として throw → node プロセスごと死亡**
- `dist/gateways/stdioToSse.js` の `session.transport.send()` も同型（`mcp-papermcp` が SSE 使用）

### パッチと永続化
| 場所 | 役割 |
|---|---|
| `infra/supergateway-patches/3.4.3/files/dist/gateways/` | パッチ済み 2 gateway の正本（git 管理）。async send の rejection を `.catch()` で捕捉しログに落とす |
| `infra/supergateway-patches/3.4.3/supergateway-3.4.3.diff` | 素との差分（レビュー用） |
| `scripts/supergateway_apply_patches.sh` | 正本をインストール先へ冪等コピー。node バージョン glob で全 image を走査し version 一致分のみ適用。oracle-patches と同じ自己修復パターン |
| `scripts/oracle_mcp_launch.sh` | oracle apply の直後に supergateway apply も実行 → 更新で剥がれても次回スポーンで自己修復 |

### 検証済み（2026-07-04）
- 再現テスト: consult 呼び出しを 0.5s で切断 → `No connection established for request ID: 77`
  が発生するも `Failed to send to StreamableHttp (async)` としてログ捕捉され、**unit は
  落ちなかった**（NRestarts=0 維持、起動時刻不変）。パッチ前は同一エラーで即死していた

## 追補4（2026-07-04）: search off でも gpt-5.5-pro が完走しない構造的理由

追補（search off）と追補3（クラッシュ）を両方入れた後で gpt-5.5-pro consult を再試行したら、
**search off（response の `tools: []` を API で確認済み）にもかかわらず 429 で失敗した**。
つまり「search を切れば pro が使える」という理解は**不完全**だった。

### 新しい失敗メカニズム（search とは無関係）
- 失敗レスポンスの構成: `reasoning: {effort: "xhigh", mode: "pro"}`, `max_output_tokens: 24000`,
  `tools: []`（search off 確定）
- ジョブは `in_progress` で **5分間** xhigh 推論を回し、その reasoning トークンが 60秒 TPM
  窓に蓄積（`Used 26007`）。最終生成の admission 予約（`Requested 35950` ＝ input + max_output）
  と合算で 50,000 を超過して 429
- effort は `dist/src/oracle/config.js` にモデル別ハードコード（pro = xhigh、env override なし）。
  下げると Pro の意味を失う。max_output を削っても reasoning 26k の窓占有が本質なので 50k の縁で
  綱渡りになるだけ
- **結論: gpt-5.5-pro[xhigh] は 50k TPM org では substantial output を構造的に返せない。**
  TPM 上限を上げる（OpenAI 申請＝オーナー操作）まで、実務では **gpt-5.5(非Pro) に逃がす**か
  SecondOpinion（サブスク枠）を使う。過去実績も非Pro 2/2 完走・pro 18連敗と整合

### 併せて発見: 非Pro consult の keepalive パーサbug
`gpt-5.5`(非Pro) consult が `Session ... failed: Unhandled response stream event:
{"type":"keepalive","sequence_number":N}` で落ちるケースを確認。oracle の Responses API
ストリームパーサが `keepalive` イベント型を未処理で例外化している疑い。未修正（要調査）。
当座は SecondOpinion 経由で外部モデル意見を採るのが確実（今日のブレストもそれで実施）。

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

## 追補5（2026-07-10）: stateless gateway の子プロセス永久リーク — 「非常に遅い」の根本原因と修正

MCP 経由の oracle が全体に激遅くなった件の調査・修正（ADR-0043）。

### 根本原因（3段の複合、全て実測で確定）
1. **リーク**: supergateway の stateless streamable HTTP モードは **HTTP POST 1件ごとに**
   `oracle_mcp_launch.sh → volta shim → node oracle-mcp` のフルチェーンを spawn する設計。
   ADR-0037 の killTree は `transport.onclose` / `onerror` にしか配線されておらず、
   **正常完了時は onclose が発火しない** → 正常系では誰も子を殺さない。
   journal 実測（7/8〜7/10）: spawn 20 / Reaping 0 / Child exited 0。node サーバー約20本が永久常駐。
   しかも 20 本全部が Claude セッション接続時の initialize / tools/list / resources/list 由来
   （tools/call は 0 件）。「~104MB の子を各セッションに持たせない」ための shared daemon が、
   リクエスト単位で 104MB 子を無限量産していた
2. **スロットリング増幅**: リークで cgroup が MemoryHigh=300M に張り付き
   （RSS 302MB + **swap 881MB** ≈ 1.18GB、`memory.events high`=2,741、pgscan_direct 145万）。
   以降の全 spawn / 応答が direct reclaim + swap thrash の中を走る。
   実測: コールドスタート 1.18s（無圧）→ 1.97s（実経路）。tools/list 1回で high +193
3. **正のフィードバック**: リクエストが来るほどリークが増え、圧が上がり、さらに遅くなる

### 当てた修正（正本は全て infra/ 配下、apply スクリプトの自己修復レールに乗せた）
| ファイル | 変更 |
|---|---|
| `supergateway .../stdioToStatelessStreamableHttp.js` | `res.on('close', () => killTree('res-close'))` を handleRequest 前に追加。res close（応答完了 or 切断）を stateless POST のライフサイクル終端として子グループを必ず回収。`express.json({limit:'8mb'})`（デフォルト100kbだと長い consult prompt が 413 死する地雷） |
| `supergateway .../stdioToStatefulStreamableHttp.js` | **防衛的パッチ（新規、現構成では未使用）**。stateful は「セッション毎1子spawn + 素の `child.kill()`」で ADR-0037 と同型の孫孤児化バグ + async `transport.send()` unhandled rejection が未対策と判明。detached spawn + killTree + async catch + body limit を移植。**無防備な `--stateful` 切替は禁止**（このパッチ済みでも子はセッション数ぶん常駐するので、consult 頻度が低い現状は stateless 維持が合理的） |
| `oracle .../node_modules/openai/lib/responses/ResponseAccumulator.{js,mjs}` | **追補4の keepalive パーサ bug 修正**。`assertNever()` が未知 stream イベント（keepalive 等）で throw → 非Pro consult がセッションごと死んでいた。警告のみ残して無視に変更（switch 後に snapshot を返すため状態は壊れない）。oracle 0.15.0 同梱の **openai@6.45.0 前提**。diff: `infra/oracle-patches/0.15.0/openai-6.45.0-keepalive.diff` |
| `/etc/systemd/system/mcp-oracle-shared.service` | MemoryHigh 300M→**384M** / MemoryMax 400M→**512M**。修正後の定常は gateway ~30MB のみ、リクエスト中ピーク実測 146MB/本。384M で同時2本まで throttling なし |

- `supergateway-3.4.3.diff` は**再生成済み**（旧 diff は 7/4 の async-send 分のみで ADR-0037 の killTree hunks が未収録だった。現在は stateless/sse/stateful 3ファイルの全パッチを収録）

### 検証（2026-07-10、全て green）
- restart 後: cgroup procs=1、MemoryCurrent 37MB（修正前 302MB + swap 881MB）
- tools/list ×2 → 1.34s / 1.19s（修正前 1.97s）。各応答直後に
  `Reaping child group ... reason=res-close` → `Child exited: signal=SIGTERM`、procs が 1 に復帰
- consult dryRun 1.44s 完走
- **実弾 gpt-5.5（非Pro）consult 3.97s で completed**（追補4時点では keepalive throw で死んでいた経路）
- keepalive 単体テスト: `accumulateResponse({type:'keepalive'}, snap)` が throw せず snapshot 維持（CJS/MJS 両方）
- consult 込みの MemoryPeak 183MB < high 384M、`memory.events high`=0、swap 0
- mcp-papermcp（SSE 経路）: 影響なし（ActiveState=active, NRestarts=0）

### 運用ノート
- 毎リクエスト ~1.2s の spawn 税は stateless の構造上残る（consult は分単位なので誤差、
  セッション接続時の tools/list 等も体感影響なし）。気になったら stateful 化を再検討
  （その場合もパッチ済みファイルが正本に有り、`--stateful --sessionTimeout <ms>` を ExecStart に足すだけ。
  ただしセッション毎 ~105MB 常駐と MemoryHigh の再見積もりが必要）
- oracle バージョンアップ時は keepalive パッチ（openai SDK 配下）も上の手順で新 dist へ移植すること。
  同梱 openai のバージョンが変わっていたら upstream が keepalive 対応済みか先に確認
