## 重大な指摘（あれば）

- **本番安全策が薄すぎる。現状は「リファクタ計画」であって「稼働中 bot の移行計画」ではない。**  
  P0-e の rollback RUNBOOK と「フェーズごと24時間観察」だけでは不足。P1 前に **P0.25: Deploy Safety / Observability** を挿入すべき。最低限、以下が必要。
  - versioned release / git tag / symlink など即時コード rollback 手段
  - `.state/` の停止中バックアップ・復元リハーサル
  - staging bot token + dev guild での smoke
  - 本番 canary 相当：特定 channel/user のみ新経路、または shadow 実行
  - feature flag：旧 sender / 旧 session / 旧 provider へ戻せること
  - correlation id 付き structured log、失敗率・応答遅延・429・Mem0 write error・未処理例外の監視
  - rollback 判断基準

- **`bot.log.1` を git untrack するだけでは甘い。情報漏えいインシデント扱いが必要。**  
  Discord ログ、プロンプト、個人情報、API 応答、場合によっては token 断片が入っている可能性がある。`git rm --cached` だけでなく、secret scan、履歴 purge 要否判断、token rotation、remote/private repo の露出確認まで P0 に入れるべき。

- **6〜10週間はかなり楽観的。フルスコープなら 12〜18週間を見た方がよい。**  
  計画上の単純合算でも P0〜P5 は下限約8週、上限約11週超。さらに各フェーズ24h観察、CI赤化、既存 mock テストの置換、systemd 本番確認、AI エージェントの手戻りを入れると 6週は現実的でない。  
  6〜10週に収めたいなら、第一リリースは **P0/P0.5/P1の一部/P2まで** に切り、P3〜P5 は次期に分離すべき。

- **P1 が「低リスク」ではない。特に P1-a と P1-d は危険。**  
  retry-after、error category、tool_loop observe、`services/claude_sdk.py` 削除は挙動差が障害に直結する。  
  `claude_sdk.py` 削除を P1 でやるのは、P2 の `ClaudeGateway` 導入より先になっており順序が悪い。まず gateway で境界を固定し、旧 `claude_sdk` を backend として呼ぶ。その後、flag 付きで claude_api 直結へ切替、観察後に削除、が安全。

- **P0-b の未コミット26ファイル整理を P0 に入れるのは危険。**  
  「リポジトリ衛生」と言いつつ、`oauth_manager` や `entry` など基盤コードを含むなら実質機能開発・挙動変更である。P0 の安全網構築前に巨大な不確定要素を混ぜている。  
  原則は、今の production 相当を tag 化し、未コミット変更は別ブランチに退避。refactor base に入れるかは別途判断すべき。

- **`@pytest.mark.legacy` 運用は CI を空洞化するリスクが高い。**  
  AI エージェント開発では「壊れたら legacy に逃がす」が起きやすい。  
  ルールとして、legacy 件数の増加禁止、対応する characterization/contract test へのリンク必須、full suite の nightly 実行、削除レビュー必須、を入れないと false green になる。

- **P2 の state class 化は 1PR=状態群では粗すぎる。**  
  module global 28個の移行は、単なる移動ではなく concurrency model の変更。lock 粒度、lock 順序、`await` 中の lock 保持禁止、cancellation 時の release、semaphore の二重解放、session/mem0 書き込みの exactly-once を明文化すべき。  
  `AppContext` も放置すると新しい god object になる。所有者、寿命、reset/close、注入経路を先に設計する必要がある。

- **P4 が遅すぎ、かつ束ねすぎ。**  
  セッション split-brain と LLM fallback 不整合は、現状すでに運用リスク。これを P3 後まで放置すると、前半のリファクタ中ずっと地雷を抱える。  
  少なくとも「書き込み経路の単一化」「provider facade」「failover 時の Mem0 再初期化/同期」はもっと前に薄く入れるべき。P4 は session 統合と provider 統合を別マイルストーンに分離すること。

- **PR ±500行目安は、この計画の複数箇所で破綻する。**  
  `MessageContext` 導入、`MessageSender` 統合、command 分割、`tool_loop.py` 分解、`config.py` パッケージ化は、実際には import 修正・テスト修正込みで膨らむ。  
  「500行」よりも、機械的 move と意味変更を絶対に混ぜない、という基準にした方が現実的。

- **Non-goal のうち「運用基盤は並行トラック」は判断が甘い。**  
  `/live` `/ready` 相当、smoke script、systemd rollback、状態バックアップは並行ではなく前提条件。稼働中 bot の大規模リファクタでは、運用基盤は non-goal にできない。

## 中程度の指摘

- **P0.5 の characterization test は 3〜5日では足りない可能性が高い。**  
  `on_message` 1,873行相当を Discord stub + LLM + Mem0 + streaming でゴールデン化するのは、それ自体が小規模リファクタ。まずは top 5〜8 フローに絞るべき。例：通常応答、LLM失敗、Mem0 recall/write、添付あり、返信文脈あり、同一チャンネル同時投稿、長文分割、permission/HTTPException。

- **「既存テストは負債」という表現は危険。**  
  mock 強結合でも、実装の暗黙仕様を大量に含んでいる。削除前に「そのテストが守っていた仕様」を棚卸ししないと、仕様を捨てることになる。

- **Discord 固有リスクが薄い。**  
  `setup_hook` への移動、slash command 登録、command tree sync、interaction の 3秒 ACK/defer、ephemeral response、DM/thread 権限、Discord rate limit は明示的に検証対象へ入れるべき。

- **systemd と CI の差異確認が抽象的。**  
  env file、working directory、venv、権限、SQLite/ChromaDB WAL、journald、cron/script 系 entry point、MCP/web API からの import を smoke 対象にする必要がある。grep だけでは足りない。

- **mem0 固定クエリ30本は良いが、評価条件が未定義。**  
  LLM rewrite/HyDE が絡むなら非決定性が出る。固定 DB、固定 seed、LLM 応答 cache、top-k recall/MRR、許容差、手動レビュー基準を定義すべき。「同点以上」はノイズで PR を止める可能性がある。

- **`config.py` → `config/` は最後に回すほど import cycle が顕在化しやすい。**  
  re-export で互換維持する方針は良いが、`config.py` と `config/` は同時存在できないため、一発 rename になる。事前に import graph と import side effect を確認した方がよい。

- **deliberate tier 凍結ルールが弱い。**  
  「新規ファイル追加のみ可」でも既存 import や runtime path を変えれば衝突する。feature flag で deliberate tier を本番から切れる状態、または明確な public interface freeze が必要。

- **retry/tool 実行まわりは副作用の重複が怖い。**  
  retry loop 分解時に、ツール実行、Mem0 write、session append、usage telemetry が二重実行されない保証が必要。idempotency key か、少なくとも契約テストで失敗シーケンスを固定すべき。

- **ruff/mypy の baseline 運用は腐りやすい。**  
  「変更ファイルのみ fail」は CI 実装が難しく、ファイル移動で既存違反が新規扱いになりがち。baseline の更新ルール、除外期限、対象パッケージの拡大計画を明文化した方がよい。

## 軽微な改善

- PR テンプレートに以下を追加：挙動変更有無、feature flag、rollback 手順、state 変更有無、smoke 結果、監視すべき log/metric、legacy test 追加数。

- 各フェーズ完了ではなく、**リスクの高い PR ごと**に観察期間を設定する。state/session/fallback/mem0 は 24〜72h、純粋 move は短縮でよい。

- `scripts/backup_state.py`、`scripts/restore_state.py`、`scripts/smoke_discord.py`、`scripts/check_import_compat.py`、`scripts/list_mutable_globals.py` を用意すると AI エージェント運用に効く。

- prompt snapshot は日時、乱数、user id、message id、token budget の非決定要素を正規化する。

- deprecation log は rate limit しないと本番 log を汚す。

- import 層のルールを `import-linter` 等で軽く入れると、`bot.py` 再肥大化や `config` 逆依存を防げる。

- AI エージェント向けに「同時編集禁止ファイル」「大規模 grep 置換禁止」「move PR でロジック変更禁止」を明文化する。

- `mem0_service.py` や `claude_sdk.py` の削除前に、systemd unit、cron、manual scripts、MCP server、README のコマンド例まで検索対象に含める。

## 計画の強み

- God module、重複、状態、テスト負債を数字で把握しており、診断の質は高い。

- `bot.py` の境界固定を `claude_api` 深掘りより先にする判断は妥当。失敗時の切り分けもしやすい。

- Cogs やフル DI コンテナを避けた判断は現実的。1人+AI 開発では過剰抽象化を避ける方が正しい。

- `P0.5` として characterization / contract / mem0 evaluation を入れたのは良い。既存 mock テストだけに頼らない方針は正しい。

- 単一巨大 `BotState` を避け、小さな state class + `reset()/close()` に寄せる設計方針は良い。

- shim / re-export を一世代挟む原則は妥当。特に稼働中 bot では重要。

- split-brain、LLM failover、asyncio race、systemd 差異、deliberate tier 衝突をリスクとして認識している点は評価できる。

- 総評として、構造リファクタの方向性は良い。ただし現状のまま P1 以降へ進めるのは危険。**運用安全策、rollback、canary、legacy test 統制を先に補強すれば、実行可能性は大きく上がる。**

<!-- INCOMPLETE: Response did not complete: Rate limit reached for gpt-5.5-pro in organization org-lrOxKXvqmMx06oSJFCavv715 on tokens per min (TPM): Limit 50000, Used 20297, Requested 36596. Please try again in 8.271s. Visit https://platform.openai.com/account/rate-limits to learn more. -->