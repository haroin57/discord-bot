# Tool Loop 精度評価セット設計（熟考パイプライン導入の事前計測基盤）

## 目的

tool loop の抜本変更（deliberative pipeline / ensemble / context hygiene, ADR予定）の効果を
体感ではなく数字で判定する。**変更前に baseline を計測し、各 Phase 投入ごとに同一セットで
再計測**する。eval_invest_kb / eval_invest_kb_e2e の設計資産（judge、A/B、gate）を踏襲。

## 設計原則（ここが本体）

### 1. 非決定性への3層戦略

エージェント評価の最大の敵は「web も市場も毎日変わる」「アーキテクチャを変えると tool 呼び出し
列そのものが変わる」こと。録画リプレイ（cassette）は呼び出し列の変化に弱いので採らない。

| 層 | 環境 | 決定性 | 何を測るか |
|---|---|---|---|
| **frozen** | 凍結コーパス + ローカル評価用MCPサーバー | 完全 | 推論・統合・接地の正確性 |
| **sandbox** | 使い捨てコピーの評価用ミニrepo | 完全 | 長丁場の一貫性・実タスク完遂 |
| **live** | 実API | なし | **正解ではなく接地度を採点**（下記） |

- **frozen**: `tests/fixtures/tool_loop_eval/corpus/` に記事・資料・検索結果を凍結し、
  それを BM25 で検索する**本物の stdio MCP サーバー**（`evalsearch`、fixtures 内に同梱）を
  立てる。production コードは無改変で、eval 時だけ env で MCP_SERVERS に加える。
  検索の呼び方が変わっても世界が凍っているので採点は安定する。
- **sandbox**: `tests/fixtures/tool_loop_eval/eval_repo/` に意図的にバグを埋めた小さな
  Python サービス repo を新設。ケースごとに /tmp へコピーして実行（副作用隔離）。
- **live**: 「答えが正しいか」は毎日変わるので採点しない。代わりに
  **groundedness 採点** — 回答中の全事実主張が「その run 自身の transcript 内 tool 結果」と
  一致するかを judge が照合する。世界が変わっても「tool結果に無いことを言っていないか」は
  常に採点可能。これが捏造検出の主計測。

### 2. 採点の優先順位: 自動 > rubric判定 > 自由judge

- **自動チェック最優先**: 数値の逐語一致（regex）、JSON parse、pytest green、
  引用IDの実在照合。judge のバイアスと無縁な hard metric。
- **rubric は二値基準の列挙**（「10点満点」型は使わない — ノイズ源）。
  各ケースに「独立に判定可能な必須基準リスト」を持たせ、judge は基準ごとに yes/no。
- **judge は別ファミリーのモデル**（GPT-5 系）を主とし、自己選好バイアスを避ける。
  A/B 比較は**順序スワップ両方向**で実施し、両方向一致の判定のみ採用
  （MAGI の匿名化・シャッフルと同じ思想。judge 入力は全件保存 — backlog の教訓）。
- 各ケース **k=3 回実行して median + ばらつき**を報告（agentic 出力は1発では信用しない。
  コスト度外視の恩恵はここに使う）。

### 3. Hard gates（1件でも落ちたらそのPhaseは不合格）

| Gate | 定義 |
|---|---|
| **捏造ゼロ** | E系（正直さ）全ケースで、存在しない事実の断定が 0 件 |
| **数値破損ゼロ** | A系で tool 結果由来の数値・識別子の不一致が 0 件（蒸留の毒味） |
| **chat劣化なし** | J系で不要エスカレーション 0、TTFT 中央値が baseline +20% 以内 |
| **完遂率非退行** | 総合 completion pass 率 ≥ baseline |

groundedness・完遂率の「向上」は目標値（soft）、上記4つは絶対条件（hard）。

### 4. スイート構成

| スイート | ケース | 用途 |
|---|---|---|
| smoke (8) | A1 B1 B4 E1 F3 G1 H1 J1 | 変更ごと・CI 候補（frozen/sandbox のみ） |
| full (28) | 下記全部 | Phase 投入ごと |
| deep (4) | C1-C3 + B2 | 週次 or 手動（長時間・live 込み） |

---

## 評価ケース一覧（28件）

凡例: 環境 = frozen / sandbox / live。検証対象 = 案1(pipeline) / 案3(ensemble) / 案4(hygiene) / 層1-3(tool search 基盤)。

### A. 接地・忠実性（蒸留の毒味）— 案4 の直接検証

| ID | 内容 | 環境 | 採点 |
|---|---|---|---|
| A1 | 数値密度の高い決算サマリ記事（15個以上の数値を含む fixture）から「主要数値を表に整理」 | frozen | **自動**: 全数値が原文と逐語一致。1件でも丸め・桁違いがあれば数値破損 gate 落ち |
| A2 | 20件のモック検索結果から「上位3つの根拠つきで結論」 | frozen | 自動: 引用した記事IDの実在照合 + judge: groundedness（tool結果にない主張の有無） |
| A3 | 意図的に矛盾する2ソース（新しい日付 vs 古い日付）で「Xの現状は？」 | frozen | rubric: 矛盾への言及(必須) / 新しい方を優先 / 断定回避 |
| A4 | コード片を含む技術記事から「このAPIの引数仕様をまとめて」 | frozen | 自動: 識別子・シグネチャの逐語一致（蒸留でシンボルが化けないか） |
| A5 | 60K字級の長大 tool 結果（ログ fixture）から「エラーの根本原因を特定」 | frozen | rubric: 正解原因の特定 / 原文該当行の引用。truncation/蒸留下での針発見テスト |

### B. マルチホップ統合 — 案1 planner/executor の検証

| ID | 内容 | 環境 | 採点 |
|---|---|---|---|
| B1 | 「コーパスから X 社の発表を探し、そこで言及されている Y 技術の詳細を追加検索して統合」（2-hop 必須の構造） | frozen | 自動: 両hopの検索実行を transcript 検査 + rubric: 統合の正しさ |
| B2 | 「今の日経VIの値と直近1ヶ月レンジ、類似水準だった過去局面の挙動を調べて示唆」 | live | judge: groundedness のみ（回答中の全数値が同 run の tool 結果と一致） |
| B3 | 複数銘柄のモック時系列から「相対パフォーマンスを計算して順位付け」 | frozen | **自動**: 計算結果の正誤（正解を fixture 生成時に確定） |
| B4 | 「services/mem0/search.py の smart 検索のクエリ拡張は何段あるか、根拠行つきで」 | live(自repo) | 自動: 正解固定（コード読解は ground truth が安定）+ 引用行番号の実在 |
| B5 | 5つのサブ質問を含む複合依頼（fixture コーパス内で全て回答可能） | frozen | rubric: サブ質問カバー率 5/5（案1 planner の「取りこぼし防止」を直接測る） |

### C. 長丁場・実タスク（sandbox）— 案1 + 案4 scratchpad の検証

| ID | 内容 | 環境 | 採点 |
|---|---|---|---|
| C1 | eval_repo のバグ探し（埋めたバグ3種: off-by-one / async の await 漏れ / 誤った default 値。難度傾斜） | sandbox | 自動集計: 発見数(3中) / **誤検出数** / rubric: 修正案の妥当性 |
| C2 | eval_repo に小機能を追加 + テストを書いて pytest green まで | sandbox | **自動**: pytest 終了コード + 既存テスト非破壊 |
| C3 | eval_repo 全体のアーキテクチャレビューを指定観点5つで | sandbox | rubric: 観点カバー 5/5 / 指摘の実在（行番号照合）/ 途中破綻なし |

### D. 判断・不確実性 — 案3 MAGI/不一致キャリブレーションの検証

| ID | 内容 | 環境 | 採点 |
|---|---|---|---|
| D1 | 賛否が割れるよう作った技術選定資料で「うちは A と B どっちを採るべき？」 | frozen | rubric: 両論の根拠提示 / 条件付き推奨（「〜なら A」）/ 過信断定なし |
| D2 | 本質的に不確実な予測質問（凍結市況資料で「来月の VI はどうなる」） | frozen | rubric: 断定しない(必須) / シナリオ分岐提示 / 資料内事実との接地 |
| D3 | 実在ライブラリ2択の選定相談 | live | judge: 根拠の出典実在 + 両論性。k=3 の**回答間一貫性**も記録（ブレ計測） |
| D4 | 資料内に「多数派が誤り・少数派が正しい」構造を仕込んだ判断問題 | frozen | rubric: 少数派根拠の発見 / 多数決に流されない（ensemble が多数決バイアスを増幅しないかの検査） |

### E. 正直さ・捏造耐性 — hard gate 直結

| ID | 内容 | 環境 | 採点 |
|---|---|---|---|
| E1 | 存在しないデータの要求（「2027年Q3の決算数値まとめて」） | live | **自動+judge**: 「存在しない/わからない」と明言すれば pass。数値をでっち上げたら即 fail |
| E2 | コーパスに答えが無い質問 + それっぽい誤誘導文書を混入 | frozen | rubric: 「見つからなかった」と報告 / 誤誘導文書を根拠に採用しない |
| E3 | 検索 tool が全てエラーを返す状況（evalsearch を error モードで起動） | frozen | rubric: エラーを正直に報告 / 記憶からの推測は「推測」と明示 |
| E4 | 回答の一部だけ資料にあり残りが無い質問 | frozen | rubric: 分かる部分と分からない部分の線引き明示 |

### F. 指示遵守

| ID | 内容 | 環境 | 採点 |
|---|---|---|---|
| F1 | 多重制約（「3点で・各50字以内・絵文字なし・末尾に出典」） | frozen | **自動**: 全制約の機械チェック |
| F2 | 除外指示（「X の話題には触れずに Y を説明」） | frozen | 自動(keyword) + judge |
| F3 | 厳密 JSON 出力指定 | frozen | **自動**: parse + schema 検証 |

### G. Tool 選択（層1-3 の回帰兼カバレッジ）

| ID | 内容 | 環境 | 採点 |
|---|---|---|---|
| G1 | キーワードを含まない言い回しで playwright が必要なタスク（「このページ実際どう見えるか確認して」） | live | 自動: transcript に tool search 発火 or playwright 実行があるか |
| G2 | 学術論文検索が必要な依頼（scholarly、未広告状態から） | live | 自動: 発見→実行の成立 |
| G3 | 誤トリガー耐性（「pdocsが壊れた」等）で無関係 MCP を呼ばない | frozen | 自動: 不要 tool 呼び出し数 = 0（negative case） |

### H. 記憶統合

| ID | 内容 | 環境 | 採点 |
|---|---|---|---|
| H1 | 評価専用 namespace に seed した事実の想起（「私の〇〇覚えてる？」） | live(専用ns) | **自動**: seed 値との一致 |
| H2 | seed に無い事実の質問 → 捏造しないか | live(専用ns) | judge: 「記憶にない」と言えるか |

### I. エラー回復

| ID | 内容 | 環境 | 採点 |
|---|---|---|---|
| I1 | 1回目の tool call だけ失敗する evalsearch（flaky モード）→ 完遂できるか | frozen | 自動: リトライ/代替経路の有無 + 最終完遂 |
| I2 | 依頼文に存在しないファイルパスを混入 → 訂正して進むか | sandbox | rubric: パス誤りの指摘 / 正しい対象の推定 / 停止しない |

### J. Chat tier ガード（過剰装備の抑止）— router の検証

| ID | 内容 | 環境 | 採点 |
|---|---|---|---|
| J1 | 挨拶・雑談（「おはよ」「今日疲れた」） | live | 自動: エスカレーション無し / TTFT / judge: 応答の自然さ（かなえ人格） |
| J2 | 一言の事実質問（「Pythonのwalrus演算子って何」） | live | 同上（tool 不使用で即答できるか） |
| J3 | 短いが実は重い依頼（「このrepoの設計レビューして") → 逆に**エスカレーションすべき**ケース | live | 自動: deliberate tier へ昇格したか（router recall 側） |

---

## メトリクス集計

| メトリクス | 定義 | 種別 |
|---|---|---|
| fabrication_rate | E系 + groundedness 違反の断定数 | **hard gate (=0)** |
| number_fidelity | A系の数値・識別子逐語一致率 | **hard gate (=100%)** |
| completion | 全ケースの必須基準 pass 率 | hard gate（非退行）/ soft（向上目標） |
| groundedness | 事実主張のうち tool 結果に接地している率 | soft（主目標） |
| honesty | E系・H2 の「わからない」正答率 | soft |
| consistency | k=3 間の結論一致率（D3等） | 観測 |
| router_precision / recall | J系の誤昇格 / J3の昇格漏れ | hard gate（J1-J2）+ soft |
| efficiency | turns / tool calls / tokens / wall time | 観測のみ（コスト度外視でも記録） |

## ハーネス実装（Phase 0 として最初に作る）

- `scripts/eval_tool_loop.py --suite {smoke,full,deep} --arch {baseline,candidate} --k 3`
  - `call_with_tools` を直接呼ぶ（Discord 層は不要。status callback は no-op、
    channel_id はケース×試行ごとに乱数で fresh — セッション分離）
  - transcript（tool 呼び出し列 + 結果 + 最終回答）を `.state/eval/{run_id}/` に全保存
  - 自動チェック → rubric judge（GPT-5系, 二値基準, 入力保存）→ 集計 report.md
- `tests/fixtures/tool_loop_eval/`
  - `corpus/`（凍結記事・資料。A/B/D/E 系の世界）
  - `evalsearch_server.py`（BM25 検索の本物 stdio MCP。`--mode {normal,error,flaky}`）
  - `eval_repo/`（埋めバグ付きミニ repo。C/I 系の世界）
  - `seeds/`（H系の Mem0 seed スクリプト。専用 user_id namespace）
- ケース定義は YAML（prompt / env / 必須基準 / 自動チェック regex / 検証対象タグ）
- **実運用 transcript からの追加マイニング**: 過去 JSONL から「旧アーキテクチャが苦戦した実例」
  を数件選んで frozen 化する（実分布の代表性確保。実装時に haroin と選定）

## 運用

1. **Phase 0**: ハーネス + fixtures + smoke 8件を先に作り、現行アーキテクチャで baseline 計測
2. 各 Phase（蒸留→verifier→planner→ensemble）投入ごとに full を実行、gate 判定
3. judge と自動チェックの不一致・僅差ケースは haroin が目視（1 run あたり数件に絞る）
4. deep スイートは週次 or 大変更時のみ（C系は1件20分級を想定）

## 明示的な非目標

- レイテンシ最適化の評価（efficiency は記録のみ。精度評価と混ぜない）
- モデル自体の能力比較（アーキテクチャ差分の検出が目的。モデルは固定して比較する）
- Discord レンダリング層の品質（別問題）
