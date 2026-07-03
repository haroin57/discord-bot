結論：  
**争点1=A案寄り、争点2=初期はCogs非採用、争点3=(b)、争点4=今すぐ最小導入** が妥当です。

---

## 争点1: 分解の順序

### 裁定
- **A案が正しい。**
- ただし正確には、**bot.py側で `MessageContext` / `ClaudeGateway` / `DiscordGateway` などの境界を先に固定し、`claude_api` 内部の本格分解はその後**。
- `claude_api` には薄い façade/adapter を先に作るが、`entry.py` や `tool_loop.py` の深い再設計は後回し。

### 根拠
- **稼働中botのリスク最小化**  
  `claude_api` の retry/resilience/streaming は挙動差が障害に直結しやすい。先に内部を触ると、失敗時に「呼び出し側の問題か内部挙動変更か」が切り分けにくい。
- **bot.py側は“非機能変更”で刻みやすい**  
  on_message から、前処理・フィルタ・プロンプト部品生成・ログ整形などの純粋関数を抽出するのは比較的レビュー可能。
- **中間状態が健全**  
  境界を固定すれば、古い `claude_api` を black box として使い続けられる。途中で止まっても「bot.pyの責務が減り、API内部は従来通り」という安定状態になる。
- **一人+AI開発では“二つの巨大変更”を避けるべき**  
  bot orchestration と retry/tool loop を同時に動かすと、AIの誤変更検出が難しくなる。

### 実行時の注意
- 最初は**挙動変更禁止**。抽出・移動・命名・境界固定だけに限定する。
- characterization test は、内部mockではなく、**入力message→LLM呼び出し→streaming応答→記録** の観測可能な結果を押さえる。
- P0のセマフォ解放漏れ・batch消失などは例外。生存リスクがあるならリファクタ順序とは別に先にhotfix。

---

## 争点2: bot.py分割でCogsを採用すべきか

### 裁定
- **初期リファクタではCogsを採用しない。**
- **素のモジュール分割 + `setup_hook` での登録**を基本にする。
- Cogsは後で、独立性の高いコマンド群だけに限定して導入してよい。

### 根拠
- **12個のslash command + 2イベントではCogsの恩恵が小さい**  
  extension lifecycle や Cog class 構造を入れるほどの規模ではない。
- **今回の主問題はon_messageの巨大orchestration**  
  Cogs化しても、ビジネスロジックをCog methodに移すだけなら問題は解決しない。
- **素の関数/サービスの方がテストしやすい**  
  Discord依存を薄いadapterに閉じ込め、core処理は普通のasync関数としてテストできる。
- **AIエージェントにとっても明示的な依存の方が安全**  
  Cogsは `self.bot` や lifecycle hook に状態が散りやすい。

### 実行時の注意
- `setup_hook` では、各コマンドモジュールに `register_commands(bot.tree, ctx)` のような明示的登録関数を持たせる。
- slash command の名前、権限、guild/global sync の挙動を変えない。特に本番global syncは慎重に。
- もしCogsを使う場合も、Cogは**Discord adapter層のみ**。記憶、LLM、永続化、権限制御の本体はCog外に置く。

---

## 争点3: モジュールレベルmutable状態28個の扱い

### 裁定
- **(b) サブシステム毎の小さなstate class数個に分割**が現実的。
- ただし、配線用に薄い `AppContext` / `RuntimeContext` が各stateを参照するのは可。
- **単一巨大 `BotState` は避ける。頻繁に触るものだけクラス化して残り容認、も最終形としては避ける。**

### 根拠
- **単一BotStateは新しいgod objectになる**  
  Discord状態、会話状態、Claude retry状態、batch状態、rate limit状態が一箇所に混ざると、レビューもresetも難しい。
- **一部だけ容認はテスト隔離を壊す**  
  conftestでresetできるものと隠れglobalが混在すると、順序依存・並列実行不具合が残る。
- **サブシステム単位なら所有権が明確**  
  例：`ConversationState`, `ClaudeClientState`, `BatchState`, `RateLimitState`, `DiscordRuntimeState`。
- **asyncio単一プロセスでも競合は起きる**  
  awaitを跨げばタスクが interleave する。lock/semaphore/cache/batchは所有者を明確にした方が安全。

### 実行時の注意
- 各stateに `reset()` / `close()` / `cancel_background_tasks()` 相当を持たせ、conftestから新規生成または確実に初期化する。
- 移行期間は旧module globalを薄いwrapperにして、新contextへ委譲する。新しいmutable globalは禁止。
- state classをDIコンテナ化しない。`dataclass` + 明示的なlock + 小さなserviceで十分。

---

## 争点4: ruff/mypy導入タイミング

### 裁定
- **今すぐ導入。**
- 方針は、**最小ruffをP0ゲート化 + mypyはbaseline/新規変更ファイル限定**。
- 構造変更後にまとめて導入する案は不利。

### 根拠
- **AIエージェントの暴走検知として価値が高い**  
  存在しないimport、名前変更漏れ、未定義変数、await漏れ周辺のミスを早期に捕まえられる。
- **後入れだと巨大diffに埋もれる**  
  構造変更後に初めてlint/type errorを見ると、原因切り分けが困難。
- **baseline方式なら既存負債を一気に背負わずに済む**  
  既存120k行を完全型付けする必要はない。新しい境界DTO/Gatewayから型を付けるだけでも効果が大きい。
- **characterization testと役割が違う**  
  testは挙動差検知、ruff/mypyは機械的破損検知。両方必要。

### 実行時の注意
- 最初のruffは事故検知系に限定。大規模formatやstyle修正をリファクタPRに混ぜない。
- mypyは `MessageContext`、Gateway Protocol、state class、新規service層を重点対象にする。既存legacy領域はbaselineまたはignoreでよい。
- changed filesだけだと依存先の型破壊を見落とすので、新設core packageは常時mypy対象にする。

---

## 総合実行順序

1. 本番rollback手順、DB/記憶/batchのバックアップ、P0 hotfix方針を先に確定。  
2. on_message主要経路とClaude streaming/retry境界のcharacterization testを追加。  
3. 最小ruff + scoped mypyをCI/ローカルゲートに入れる。  
4. `MessageContext`、結果event、`DiscordGateway`、`ClaudeGateway`、記憶/永続化gatewayを定義。  
5. `bot.py` から純粋関数、prompt構築、前処理、記録処理を小分け抽出。  
6. slash commandはCogs化せず、通常モジュールへ移し `setup_hook` で登録。  
7. mutable globalを、触る順にサブシステムstateへ移し、conftestで生成/reset可能にする。  
8. 安定後、固定済み `ClaudeGateway` の裏側で `entry.py` retry/resilience と `tool_loop.py` を段階分解。  
9. 互換wrapperと残存globalを削除し、ruff/mypyの範囲を少しずつ拡大。

<!-- INCOMPLETE: Response did not complete: Rate limit reached for gpt-5.5-pro in organization org-lrOxKXvqmMx06oSJFCavv715 on tokens per min (TPM): Limit 50000, Used 25749, Requested 24917. Please try again in 799ms. Visit https://platform.openai.com/account/rate-limits to learn more. -->