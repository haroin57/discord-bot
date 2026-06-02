# Claude API 連続停止インシデント — 原因別 修正手順

- **日付**: 2026-06-01 (月)
- **症状**: bot が応答せず沈黙 → idle timeout (`cancel_reason="stream_idle"`, ~601s) を繰り返す
- **影響チャンネル**: `1488369795807055964`（本デバッグ会話そのもの）
- **調査者メモ**: 当初の「CLI バージョン不整合 / 認証トラブル」仮説は**いずれも主因ではなかった**。実ログで確定した 3 障害を、寄与度順に記載する。

---

## サマリ（寄与度順）

| # | 原因 | 寄与度 | 現状 | 根治の要否 |
|---|---|---|---|---|
| ① | context_overflow（履歴肥大 7.6M tokens） | **主犯** | 自己回復する設計済み | 予防策を足す |
| ② | OAuth `invalid_grant`（感情エンジン CLI との credential 自家中毒） | 副因 | **未対策**。再発する | **要根治** |
| ③ | refresh-cli-identity の mitmdump 失敗 | 無関係（赤いニシン） | 5/31 に1回 failed | 監視のみ |

**結論**: ①は設計通り自己回復する。恒久的な火種は②で、真因は当初の「bot↔mem0 race」ではなく、**bot 本体が起動する感情エンジン CLI が同一の `~/.claude/.credentials.json` を共有して無断 rotate する自家中毒**だった（後述）。③は今回の停止とは無関係。

---

## ① context_overflow — 履歴肥大（主犯）

### 何が起きたか
- 19:12:01、`ch=1488369795807055964` で `prompt is too long: 7,642,248 tokens > 1,000,000 maximum`。
- 上限の **7.6 倍**。18:50 に予兆の `413 request_too_large` も出ていた。
- 原因は、このデバッグ会話内で `journalctl` 生ログ・`bot.log`(9.9MB)・巨大 grep 結果を 1 セッションに積み続け、agent loop 内のツール結果が累積爆発したこと。

### 現状：既に自己回復する設計になっている
`services/claude_api/entry.py` の context_overflow ハンドラがセッションを破棄して短い JSONL tail から再開する：

```python
if category == "context_overflow":
    sessions.pop(channel_id, None)    # セッション破棄
    _clear_session_time(channel_id)   # 次ターンは短い履歴から再開
    return _error_message(category), None, False
```

- 実際 19:12 の overflow 後、19:14 に input 約1.5万 tokens で正常復帰している（ログで確認済み）。
- **=> コード修正は必須ではない。** 以下は再発予防（任意・推奨）。

### 修正（予防策）
1. **ツール結果の上限バイト制御**（推奨度：高）
   - agent loop に渡す前に、1 ツール結果が一定サイズ（例：実測で効くのは 32–64KB 程度）を超えたら末尾を切る／ファイルに退避してパスだけ渡す。
   - 特に `journalctl` / 大きな `Read` / `Grep` のヒット過多が危険。
2. **overflow 予兆での先回り trim**（推奨度：中）
   - 入力 token が上限の一定割合（例：80%）に達したらリトライを待たず履歴を縮小。
   - 413（request_too_large）を overflow と同じ経路に寄せて早期にセッションを畳む。
3. **運用ルール**（即効・コスト0）
   - デバッグで大量ログを撃つときは、生出力をチャンネルに流さず**ファイルに落としてパスで参照**する。今回の主犯はこれをやらなかったこと。

---

## ② OAuth invalid_grant — 感情エンジン CLI との credential 自家中毒（要根治）

### 何が起きたか
- 18:52:12、`/home/ubuntu/.claude/.credentials.json` が rotate（再生成）。
- 18:53、`invalid_grant: Refresh token not found or invalid` が 4 連続 → ログに `Bot will start returning 401 soon`。**rotate との時間差は約 1 分**。
- 19:05 以降は `token valid, subscription=max` に復帰。
- **=> 今回の停止の主因ではない**が、構造的に再発する火種。

### 当初の診断は誤りだった
最初に書いた「bot 本体 ↔ mem0 MCP の refresh token race」は**真因ではなかった**。`entry.py` のコメントが mem0 を名指ししていたため引きずられたが、実プロセスを確認すると別の犯人が出た。

### 真因：bot が起動する感情エンジン CLI が同一 credential を共有・無断 rotate
実測で確定した事実：

- Claude トークンを使う全プロセス（bot 本体・mem0 MCP・各レンダーデーモン）の `OAUTH_NO_REFRESH` が **`<unset>`**。`oauth_manager.py` に `OAUTH_NO_REFRESH = os.environ.get("OAUTH_NO_REFRESH","0")=="1"` という singleflight ゲートはあるが、**誰にもデプロイで適用されていない**（紙のゲート）。
- `claude --print --model haiku`（感情エンジンの常駐 CLI）が独立プロセスとして稼働。その**親 PID は bot 本体**。起動元は `services/emotion/persistent_session.py` の `_launch()`。
- `_launch()` の subprocess 起動が親 env を丸ごと継承している：

```python
return await asyncio.create_subprocess_exec(
    *cmd,
    env=dict(os.environ),   # CLAUDE_CONFIG_DIR も HOME も上書きしていない
    cwd=self.cwd,
)
```

- `CLAUDE_CONFIG_DIR` / `HOME` を上書きしないため、感情 CLI は bot 本体・mem0 と**寸分同じ `~/.claude/.credentials.json`** を読み書きする。
- CLI は期限が近いと自分の判断で credential を rotate する。その瞬間、bot 本体がメモリに保持していた旧 refresh token が無効化 → `invalid_grant`。
- **構造**: bot が自分の感情状態更新のために CLI を起動し、その CLI が bot 本体のメイン API 認証を撃ち落とす自家中毒。18:52 rotate → 18:53 invalid_grant の 1 分差がこれと整合する。

### 修正（根治）— A / B のトレードオフ
**A 案：credential を物理分離（推奨）**
- `persistent_session.py` の `_launch()` の env に、感情 CLI 専用の設定ディレクトリを与える：
  ```python
  env = dict(os.environ)
  env["CLAUDE_CONFIG_DIR"] = "/home/ubuntu/.claude-emotion"  # 専用 credential
  ```
- 数行で済み、credential の取り合いが**原理的に消える**。
- **代償**: 感情 CLI 用に別ログインが必要 ＝ 同一 Max サブスクの二重利用になり規約上グレー。実害（同一アカウントの別セッション扱いで許容されるか）は**要確認**。

**B 案：refresh 権の一元化**
- 全プロセスが同じ credential を読むが、refresh する責務を 1 プロセスに固定し、他は「読むだけ」にする。`OAUTH_NO_REFRESH=1` を bot 本体以外（mem0・感情 CLI）に実際に配線する。
- **問題**: CLI に refresh を止めさせる手段（env でのトークン直渡し等）が CLI 側に存在するか**未確認**。CLI の内部トークン管理挙動に依存するため脆い。
- ここは当初 haroin が依頼した「Claude Code CLI のリクエスト方式・トークン管理のリバースエンジニアリング」と直結する。CLI 挙動を読まないと B 案は設計できない。

**判断**: A 案優先（確実・数行・依存が少ない）。B 案は CLI 解析の確証が取れてから。

### 確認タスク（実装前）
- A 案：`CLAUDE_CONFIG_DIR` 分離時に感情 CLI が別ログインで動くか、サブスク二重利用が規約・実運用で問題ないか。
- B 案：claude-cli が「refresh せず渡された token を使うだけ」のモードを持つか（env / フラグ）。
- `oauth_manager.py`：singleflight ゲートを実際に有効化する場合の配線箇所。
- `entry.py` の対症療法（auth 失敗 → reload → 1 回リトライ）は A/B どちらでも保険として残してよい。

---

## ③ refresh-cli-identity / mitmdump 失敗（無関係・監視のみ）

### 何が起きたか
- `refresh-cli-identity.service` が 5/31 04:38 に `mitmdump not listening` で failed。
- ただし 5/29・5/30 の refresh は `changed: false` で HTTP 200 verify OK。CLI identity（`CC_VERSION=2.1.156`）は**既に安定適用済み**で、実インストールの claude-cli 2.1.156 とも一致。
- 失敗は 15 時間前の 1 回きりで、今日の連続停止（19 時台）とは時間的にズレる。
- **=> 今回のインシデントとは切り離してよい。**

### 修正（監視のみ）
1. **mitmdump 起動の前提チェック**（推奨度：中）
   - refresh スクリプト先頭で mitmdump の listen を確認し、未起動なら起動 or skip。失敗時に identity を壊さない（`changed:false` を維持）。
2. **failed 状態のアラート**（推奨度：低）
   - `refresh-cli-identity.service` の failed を検知して通知。放置すると将来 identity が古くなった時に効いてくる。

### 確認タスク
- `systemctl cat refresh-cli-identity.service` の `ExecStart`。
- `scripts/` 配下の identity / capture / refresh スクリプト本体と mitmdump 起動手順。

---

## 当初仮説が外れた理由（記録）

- **「CLI バージョン不整合（2.1.156 を名乗り 2.1.92 の salt）」**: `CC_VERSION` は実インストール 2.1.156 と一致。salt が 2.1.92 由来という裏取りはできておらず、**断定は誤りだった**。
- **「認証で即弾かれている」**: 認証失敗なら 401/403 が即返る。実症状は ~601s 沈黙後の idle timeout で、overflow / stall と整合する。auth は 18:53 のスナップショットでは劣化していたが主因ではない。

---

## 推奨アクションの優先順位

1. **②の根治**（A案＝感情 CLI の credential 物理分離を優先 / B案＝refresh 権の一元化）← 唯一の恒久的火種
   - 注：当初書いた「プロセス間ロック」は無効。CLI は bot 側の Python ロックに参加しないため、ロックでは取り合いを止められない。
2. **①の予防**（ツール結果のサイズ制御／大量ログはファイル退避）
3. **③の監視**（mitmdump 起動チェック＋failed アラート）

## 未確定 / 次に確認すること

- A案：`CLAUDE_CONFIG_DIR` 分離で感情 CLI を別 credential にした際、Max サブスク二重利用が規約・実運用で許容されるか（②の根治方針を左右）。
- B案：claude-cli / mem0 MCP を「refresh せず渡された token を読むだけ」にできるか（env・フラグの有無）。CLI 側は要リバースエンジニアリング。
- `oauth_manager.py` の singleflight ゲート（`OAUTH_NO_REFRESH`）を実際に配線する箇所。現状は全プロセス `<unset>` で死んでいる。
- `entry.py` の正確な行番号は本ドキュメント作成時点で再確認が必要（自己回復ハンドラ・auth リトライの所在）。

---

## 追記 (2026-06-02): 3 症状の真因と実装した修正

当初の doc は ①②③（停止の背景要因）に絞っていたが、別途 haroin が挙げた 3 つの「見える症状」の真因を実ログ + コードで確定し、修正を実装した。詳細は `.claude/adr/0004-claude-api-stall-and-tool-call-leak-fixes.md`。

| 症状 | 真因 | 修正 |
|---|---|---|
| **msg1 timeout** (18:27) | 上流 SSE ストリームハング（turn-3 POST が 200 後イベント0で 600s 沈黙、idle watchdog が満了まで待った） | **初動(time-to-first-token) timeout + 1回自動リトライ**。`STREAM_FIRST_EVENT_TIMEOUT_S=120` 無音で stream を捨て再POST、枯渇で clean timeout。`tool_loop.py` のストリーム消費を再構造化。 |
| **msg2 途中停止** (18:49) | 413 `request_too_large` が `api_exit` 分類で self-heal 経路に乗らず session も畳まない | **413 → context_overflow 分類**（`errors.py`）。entry.py 既存ハンドラで session 破棄→短縮再開。 |
| **msg3 タグ漏れ+停止** (6/2 10:44) | **Opus 4.8 がツールコールを native tool_use ではなくテキスト形式 XML (`<function_calls>/<invoke>`) で出力**。pause turn 扱いで XML が Discord に漏れ、処理停止。tools=23 で advertise 済み＝モデル挙動の揺らぎ | **テキスト形式ツールコールを検知して出力から除去 + native tool_use で呼び直す是正 nudge**（`tool_loop._strip_text_tool_calls`）。併せて `MAX_CONSECUTIVE_EMPTY_TURNS` を 1→2 に復帰（早期停止の元）。 |

**msg3 についての訂正**: 当初「scaffold タグ（`<trusted_user_input>` 等）が漏れた」と読み、出力側 scrubber を入れかけたが、実ログの漏洩本文は `<invoke>`（ツールコールのテキスト形式）であり scaffold タグではなかった。scrubber は撤回し、真因（tool_loop がテキスト形式ツールコールを処理しない）を除去した。

**①②③ の実装状況**:
- **①** 予防として `MAX_TOOL_RESULT_CHARS=60000` のツール結果サイズ上限を `tool_loop._truncate_tool_result` に実装。
- **②** A案を実装。`persistent_session._launch()` が `EMOTION_CLAUDE_CONFIG_DIR`（既定 `/home/ubuntu/.claude-emotion`）に `.credentials.json` がある時だけ分離、無ければ警告してフォールバック。**手作業: `CLAUDE_CONFIG_DIR=/home/ubuntu/.claude-emotion claude login` を1回**。
- **③** mitmdump precheck・identity 破損ガードは既存で OK。failed アラート（systemd `OnFailure` → Discord/journal 通知）を実装・install 済み（`scripts/notify_refresh_cli_failure.sh` + `deploy/systemd/`）。テスト: `sudo systemctl start refresh-cli-identity-failure-notify.service`。
