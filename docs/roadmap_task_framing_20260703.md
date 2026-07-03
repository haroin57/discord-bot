# ロードマップ5タスク 課題定義（Plan前段階）— 2026-07-03

発注: haroin（`/goal`）。目的: 各タスクの「達成すべき課題」を明確にし、**Planの前段階**（問題定義フェーズ）として整理する。実装・詳細設計はまだ行わない。

不確定要素は SecondOpinion（GPT-5.5 / Gemini 3.1 Pro / DeepSeek V4 Flash の3社並列、subscription枠）で多角検討した。外部意見の生ログは `/tmp/discord_bot_attachments/so_{A..D}_*.txt`。

**重要な前置き**: 外部3社の意見が社内コード走査の結論と割れた箇所が2つある（リファクタ順序 / Mem0工数）。かなえの判断で片方に丸めず、両論併記する。

---

## 全体の温度感（先着の直感 → 検討後）

| # | タスク | リスク | 先行可否 | 検討後の結論の芯 |
|---|---|---|---|---|
| 1 | Mem0 再セカンドオピニオン | 低 | 即可 | v3の方向性は3社が妥当と評価。ただし**工数と未計上スコープ**を直せ |
| 2 | Playwright→ChatGPT Pro | 高（規約） | 要判断 | **3社満場一致でPlaywright却下。** Responses API + tier昇格へピボット |
| 3 | 大規模リファクタ | 中 | 院試後 | **3社一致で段階的・院試前は設計/テスト凍結貯め。** 順序は社内と割れる |
| 4 | 推論コストROI | 低 | 即可（ただし計測先行） | ROI判定の前に**計測基盤が欠落**。まず可視化 |
| 5 | Credential自動ローテ | 中（セキュリティ） | 即可 | **3社一致で「自動ローテ」は誤前提。監視+通知に再定義** |

---

## タスク1: Mem0アーキテクチャ再セカンドオピニオン

### 達成すべき課題
`docs/memory_redesign_plan.md` の v3プラン（3つの穴 = attribute / condition / scope の欠如をメタデータ拡張で塞ぐ、新ストレージ層なし、4.5日）を、**壊れていて検算できていなかったGPT-5経路が復活した今、再検算する**。v3を「GO」にできるか、未決事項に答えを出す。

### 現状
- v3はコード走査（services/mem0/ 5,469行）済みで精度が高い。既存実装（conflict分類/失効フィルタ/entityグラフ/dedup/reinforce）は活かす方針
- v3作成時はGPT-5が3回連続で応答取得失敗（OpenAI経路のバグ）→ DeepSeek単独意見で組んでいた
- SecondOpinion は codex(gpt-5.5) 経路で復活済み。今回3社から検算を取得

### 外部意見（3社の集約）
**方向性は3社とも「妥当」で一致。** ただし全社が同じ2つの補強を指摘（＝v3の未決事項に明確な答えが出た）:

1. **attribute語彙は統制語彙にすべき（3社一致・強い）**。自由文字列だと `weight`/`体重`/`body_weight` が分裂して決定的conflictルートがすり抜ける。「小さな統制語彙 + `custom:*` 逃がし」or「既存attribute一覧をLLMに提示して再利用強制」。→ **v3未決事項#1は『統制語彙』で確定してよい**
2. **conditionは検索時ハードフィルタ単独では過少（3社一致）**。「保存時に構造化 → 検索時はソフトフィルタ/スコア調整 → 注入時に自然文で条件明示」の三層。→ **v3未決事項#2の答え**

**割れ／新規指摘（要判断）**:
- **工数4.5日は過少（GPT・DeepSeek）**。読み出しパイプライン（BM25+vector+HyDE+graph+rerank）へのscopeフィルタ/conditionマッチング挿入、既存データのバックフィル、評価セットが未計上。DeepSeekは**9.5日**（実装4.5 + 読み出し3 + 未決設計1 + テスト1）と再見積もり。← **v3の"4.5日・無停止"は楽観の疑い。要精査**
- conflictキーは `user × attribute` だけでは過剰失効。`user + attribute + compatible_scope + compatible_condition` にすべき（朝の好みと夜の好みを誤って矛盾扱いしない）
- 抽出失敗時のフォールバック（attributeが取れないfactを全部`unknown`にすると衝突）
- 既存900件超の汎用factが**誤注入され続ける** → lazy migration or 再抽出が要る
- scopeに時間的TTL（「今日の機嫌」等の一時state）の概念が要るか
- ChromaDBのメタデータスキーマ変更が本当に無停止か（コレクション再作成要否）

### Plan前段階の次アクション
- [ ] v3を改訂: 未決事項#1=統制語彙、#2=三層condition で確定
- [ ] 工数を「新規適用（先行）」と「既存バックフィル（フェーズ2）」に分割し、読み出し系改修を明示計上して再見積もり
- [ ] conflictキー設計を `user + attribute + scope + condition` に拡張
- [ ] 回帰評価セット（属性更新/条件付き習慣/局所scope混入）の定義 ← これがないと効果測定不能
- [ ] Phase A着手のGO判断（低リスクなので先行枠）

---

## タスク2: OpenAIの人のPlaywrightでChatGPT Proにタスクを投げる方式の導入

### 達成すべき課題
ChatGPT Pro($200/月)の最上位モデル（gpt-5.5-pro / o3-pro 等）へプログラムからアクセスする手段を決める。当初案はPlaywrightでWeb UIをブラウザ自動操作する方式。

### 現状（調査）
- **「元OpenAIの人が作った」該当ツールは特定できず**。機能が近い候補（cbusillo/chatgpt-automation-mcp 等）は**全てstar数一桁でアーカイブ済み・メンテ停止**
- OpenAI利用規約はWeb UIのプログラム的自動化を**明示的に禁止**、複数アカBAN事例あり
- **重要な発見**: `gpt-5.5-pro` / `gpt-5-pro` は**Responses API(`/v1/responses`)で公式提供済み**（Web調査で確認）。ただしtier4-5の利用実績が要る。codex CLIがこれらで400を返すのは**tier不足の可能性が高い**
- 共有リンク(claude.ai/share/...)は認証壁でアクセス不可 → 中身未確認

### 外部意見（3社の集約）
**3社満場一致で「選択肢2（Responses API）を取れ、Playwrightは却下」。confidence全社high。**
- 規約違反（検出回避を前提にした設計は"設計上の欠陥"）＋ $200枠を失うリスク・リターンの破綻
- UI変更で壊れ続け、候補ツールがアーカイブ済み=自前保守
- 低頻度のSecondOpinion用途にAPI従量課金は微々たる額。「tier制限は時間・実績・代替で解ける問題、規約違反は解けない問題」（GPTの決め手）

**留保（正直に記載）**: DeepSeekのみ「gpt-5.5-proがResponses APIで実在するか未確認（訓練カットオフが古い）」と留保。ただし社内Web調査で実在を確認済みなので、この留保はDeepSeekの知識鮮度の問題。

### Plan前段階の次アクション
- [ ] **Playwright方式は不採用**で確定（3社+社内調査が一致、これ以上の検討不要）
- [ ] OpenAI の現在のusage tierを確認（console → tier4/5到達条件と現状）
- [ ] tier到達までの暫定策: 安価モデルで利用実績を積む or codex gpt-5.5継続で凌ぐ
- [ ] tier到達後、Responses API経由でgpt-5.5-proをSecondOpinionの最上位経路として組み込む設計
- [ ] （先輩へ確認）「元OpenAIの人のツール」の具体名/URLが手元にあれば再調査。なければこのピボットで進める

---

## タスク3: discord bot 大規模リファクタリング

### 達成すべき課題
肥大化した中核ファイル群を責務分割し、保守性・テスト性を上げる。**制約: 8/18院試まで新規大規模着手を避ける（レビュー・設計貯めは可）、bot常時本番稼働で停止不可。**

### 現状（社内コード走査）
- bot.py **3,060行**（19コマンド+イベント+セッション+セキュリティ+レンダリングが混在／god object）
- tool_loop.py **2,090行**、entry.py **1,379行**、config.py **1,373行・248定数**（161ファイルが `from config import`）
- テストは2,537件だが claude_api/mem0 に偏り、**bot.py本体は薄い**
- subprocess実行が25ファイル、LLM呼び出しが11ファイルに散在（エラー処理/timeout/ログが不統一）

### 外部意見 vs 社内調査（★割れている★）
**進め方は3社一致**: 「段階的にやれ。院試前は実コード変更を凍結し、ADR・設計・テスト拡充のみ」。ビッグバンは全社が却下（本番稼働+テスト薄で障害検知不能、院試前の緊急デバッグは致命的）。

**順序は社内調査と外部3社で逆**:
- 社内Explore: 「**bot.py分割が一番効く**（効果最大・実感しやすい）」→ bot.py first
- 外部3社: 「**bot.pyは最後**。依存の末端（config.py層化 + subprocess/LLM wrapper統一）から中心へ。bot.pyは最難関・最高リスクなのでテストと基盤が整ってから」→ bot.py last

→ これは **「効果最大(社内)」vs「リスク最小(外部)」の対立**。本番稼働+院試制約下では**リスク最小（外部）を採るのが筋**、とかなえは判断（bot.py分割を最初にやると設定・副作用・外部呼び出しの境界が曖昧なままファイルだけ増える、というGPTの指摘が的確）。

**外部が提案するADR群**（院試前に貯める）:
- 方針ADR（本番互換・段階移行・小PR分割）／モジュール境界定義／subprocess・LLM統一wrapper方針／config.py層化方針／分割の受け入れ条件（挙動維持・テスト追加・即ロールバック可）

### Plan前段階の次アクション（院試前=設計のみ）
- [ ] リファクタ方針ADRを起こす（`.claude/adr/`）: 段階移行・小PR・本番互換・ロールバック前提
- [ ] 目標アーキテクチャ図（パッケージ構成・依存方向）とモジュール境界定義
- [ ] bot.py / entry.py 周辺の**結合テスト追加案**をリストアップ（実装は院試後）
- [ ] 実装順序を確定: **① config.py層化 + subprocess/LLM wrapper統一 → ② tool_loop executor分離 → ③ entry.py段階抽出 → ④ bot.py責務分割（最後）**
- [ ] 実コードのリファクタ着手は8/18以降

---

## タスク4: 推論コストと出力のROI検討

### 達成すべき課題
どのLLM経路がコスト対効果に見合っているかを判定し、削減/強化の優先順位を出す。

### 現状（社内調査）— ★ROI判定の前に計測基盤が欠落★
- 従量課金でコスト発生: Opus 4.8（メイン応答・毎ターン）、Sonnet 4.6（Mem0抽出・約50秒周期）、Haiku 4.5（Mem0検索/rerank・毎ターン）、embedding、music解析、SecondOpinionのAPI直フォールバック
- コストゼロ（サブスク枠）: Claude OAuth本体、codex(gpt-5.5)、agy(Gemini)
- **計測済みはメイン応答(Opus)のトークンのみ**。`SessionInfo.last_usage` で /context 表示可
- **非計測（不可視）**: Mem0（毎ターン走るのに月$15-20相当がログなし）、Deep Research/MAGI（実装済み・1回$10-100+の可能性・計測ゼロ）、music解析

### 結論
**現状のデータではROI判定は不可能**（Opus=月$30-50としか言えず、Mem0の月$15-20が隠れている）。削減対象の優先付けができない。**まず計測基盤（トークン/コストのロギング）が先行タスク。**

（このタスクは調査結論が明確なためSecondOpinionは呼んでいない。判断が割れる論点が出たら追って相談する）

### Plan前段階の次アクション
- [ ] 計測を実装すべき経路の優先順位: ① Mem0（毎ターン・最頻）② SecondOpinion API直分 ③ Deep Research/MAGI（高単価）
- [ ] 相関ID（job_id/user_id/channel_id）付きのトークン・コストロギング設計（かなえのSRE的作法に合わせる）
- [ ] 1-2週間データを溜めてからROI分析を開始（計測なしの最適化は禁止）
- [ ] 低リスクなので計測基盤は先行着手可

---

## タスク5: CLIのCredential自動ローテーションスクリプト実装

### 達成すべき課題（★再定義された★）
当初要求は「CLI credentialの自動ローテーション」。だが調査の結果、**この前提自体が部分的に誤り**だった。

### 現状（社内調査）
- **自動ローテ（refresh token更新）が原理的に可能なのはAnthropic OAuthのみ**で、既に完全実装済み（dual-account failover含む）
- codex(~/.codex/auth.json) / agy(libsecret): 失効検知の仕組みすらなく、subprocess失敗で気づく→手動re-login
- 固定トークン（DISCORD_TOKEN / OPENAI_API_KEY / GOOGLE_AI_API_KEY 等）: **原理的にローテ不可**、失効時HTTP40xで気づく→手動再発行
- 致命度: Anthropic OAuth と DISCORD_TOKEN の失効はbot本体停止（致命的）。codex/agyはSecondOpinion専用でfail-open（非致命）

### 外部意見（3社の集約）
**3社満場一致でスコープを再定義**: 「自動ローテーション」ではなく **「失効の事前監視 + 通知」** が正しい責務。confidence全社high。
- 固定キーは自動更新不能。ヘッドレスで再発行を自動化するのはアンチパターン（システムが脆くなる）
- 優先度（3社ほぼ一致）:
  1. **DISCORD_TOKEN の失効監視+通知**（致命的なのに検知なし = 最大の穴）
  2. **Anthropic OAuth** の refresh監視・失敗通知・failover検証の強化（既存機構の堅牢化）
  3. 固定APIキー群の401/403検知 + 低頻度ヘルスチェック
  4. codex/agy の失効検知+通知
  5. **codex/agyの自動re-loginは原則実装しない**（fail-openで足りる、保守コストに見合わない）

### Plan前段階の次アクション
- [ ] スクリプトの責務を「自動ローテ」→「**credential健全性監視 + 期限管理 + 通知**」に再定義
- [ ] credentialごとに「更新可能/検査のみ可能/手動対応必須」を分類する台帳を作る
- [ ] systemd timer で定期ヘルスチェック（各credentialに軽量認証チェック）→ 失効前/失効時に通知（メール or Discord DM）
- [ ] 最優先はDISCORD_TOKEN監視（致命的なのに現状ノーガード）
- [ ] codex/agy自動re-loginは実装しない方針で確定

---

## 横断メモ

- **roadmap項目6（Codex/DeepSeek/Geminiをサブエージェント起動）は実質達成済み**: 本日 SecondOpinion をMCP化（`second_opinion_mcp_server.py`）してCLIから3社サブスク枠で叩ける。この課題定義自体がその成果物の実戦投入（4論点×3社を21秒×4で回した）
- **意見が割れた2点は先輩の判断待ち**: (1)リファクタ順序=効果最大(bot.py first)かリスク最小(末端first)か、(2)Mem0工数=v3の4.5日か外部の9.5日か
- 各タスクの外部意見生ログ: `/tmp/discord_bot_attachments/so_A_playwright.txt` / `so_B_mem0_v3.txt` / `so_C_credential.txt` / `so_D_refactor.txt`
