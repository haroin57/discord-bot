# msg3 タグ漏れ / ストリームハングが「直っていない」根本原因

- **Date**: 2026-06-02 (JST)
- **Author**: かなえ (調査)
- **Status**: fixed (2026-06-02) — 下部「## 修正実施」参照
- **対象プロセス**: `discord-bot.service` PID 601425（2026-06-02 12:16:10 JST 起動）
- **関連**: `b762baf revert(model)`, `ffe7124 Opus 4.8 migration`, 未コミットの stall 修正パッチ

---

## TL;DR

「Opus 4.8 が不安定だから 4.7 に revert する」という前提で再起動したが、**bot は今も `claude-opus-4-8` で動いている。**

理由は2段:

1. **モデル revert が効いていない。** 5/29 のコミット `b762baf` は 3つの MODEL 定数（`config.py` / `entry.py` / `claude_sdk.py`）を正しく 4-7 に戻していた。ところが**今回の未コミット stall 修正パッチが、その 3つすべてを 4-8 に巻き戻している**（worktree diff で `-4-7 / +4-8` を3ファイルとも確認）。12:16 の再起動でこの worktree が乗ったので、実機は 4-8。
2. **タグ漏れ（msg3）は 4.8 固有の挙動。** 4.8 はツール呼び出しを native な `tool_use` ブロックではなく `<function_calls><invoke>` の**テキストとして**断続的に吐く。revert が効いていない以上、根本原因の 4.8 がそのまま残り、症状が出続ける。

緩和コード（`_strip_text_tool_calls` + テキスト形式ツール呼び出し nudge）は**正しくデプロイされ発火もしている**が、1回 nudge して諦める設計（`MAX_CONSECUTIVE_EMPTY_TURNS = 2`）なので、4.8 が次ターンも同じテキスト形式で吐くと**ループが打ち切られ、ツール実行が失われた切れた応答**になる。これが「直ってない」の正体。

**一言で**: revert したつもりが、stall 修正パッチに 4.8 が相乗りして全モデル定数を巻き戻した。緩和は症状を隠すだけで、根本（4.8）には触れていない。

---

## 症状（observable）

- 先輩・お父さん向けの応答に、ツール呼び出しが**テキストとして漏れる**（`<invoke name="...">` や、その直前の `count` 等の残骸）。
- ツールが実行されず、応答が途中で切れる／薄くなる（例: Andrew Left 調査の1回目が空振り → 2回目で成功、というリトライ往復）。
- 私自身の発話も同じ事故を起こした（「皮肉なことに今のが msg3 そのもの」）。

---

## 実行経路（確定）

ログに `services.claude_api.tool_loop` の `claude_api_usage` が出ている時点で、**direct-API 経路（tool_loop.py の `run_agent_loop`）が現用**。経路は以下で確定:

```
bot.py:44   from services.claude_sdk import call_with_tools
                  │
                  ▼
claude_sdk.py:827  _backend()  →  os.environ.get("CLAUDE_BACKEND", "api")  # .env に上書き無し = "api"
claude_sdk.py:832  shim: backend=="api" なら services.claude_api.call_with_tools へ委譲
                  │
                  ▼
claude_api/entry.py:　call_with_tools()
entry.py:422        run_agent_loop( ... , model=MODEL , ... )
entry.py:94         MODEL = "claude-opus-4-8"   ← 実機が読むのはこの定数
```

**重要**: `entry.py` は `config.CLAUDE_MODEL` を import すらしていない（自前の `MODEL` 定数を持つ）。
つまり **`config.py` の `CLAUDE_MODEL` を書き換えても現用経路のモデルは変わらない。** `config.CLAUDE_MODEL` を使うのは旧 `__init__.py:354`（`_stream_once` / `call_direct`、no-tools 経路）だけで、tool ループには無関係。

---

## 根本原因 1: モデル revert が stall パッチに巻き戻された

モデルを決める**ハードコード定数が3か所**に重複している:

| ファイル | 定数 | b762baf（revert, 5/29） | HEAD | **worktree（現在・実機）** |
|---|---|---|---|---|
| `config.py` | `CLAUDE_MODEL` | 4-7 | 4-7 | **4-8** |
| `services/claude_api/entry.py` | `MODEL` | 4-7 | 4-7 | **4-8** ← 現用経路 |
| `services/claude_sdk.py` | `MODEL` | 4-7 | 4-7 | **4-8** |

`b762baf` のコミット内容（正しく3つとも 4-7 へ戻している）:

```
commit b762baf  revert(model): roll back MODEL to claude-opus-4-7 (staged rollback)
 config.py                    | 2 +-
 services/claude_api/entry.py | 2 +-
 services/claude_sdk.py       | 2 +-
```

ところが**未コミットの worktree diff**（今回の stall 修正パッチ）が3つとも巻き戻している:

```diff
# config.py
-CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")
+CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

# services/claude_api/entry.py
-MODEL = "claude-opus-4-7"
+MODEL = "claude-opus-4-8"

# services/claude_sdk.py
-MODEL = "claude-opus-4-7"
+MODEL = "claude-opus-4-8"
```

このパッチは `ffe7124 feat(api): Opus 4.8 migration` 系列を基点に作られており、stall 修正と一緒に 4.8 移行分を連れ戻している。12:16:10 の再起動でこの worktree がプロセスに乗ったため、**実機は 4.8。先輩の「4.7 に revert した」認識と実機がズレている。**

> この時点で `git stash` も `git checkout` もしないこと（CLAUDE.md の git 安全ルール）。stall 修正の他の差分（strip / nudge / first-event timeout / 413→context_overflow / per-tool-result cap など 1003 insertions）は残したまま、モデル定数だけ直す必要がある。

---

## 根本原因 2: 緩和（strip + nudge）が症状隠しに留まる

`tool_loop.py` の緩和は設計通り動いているが、**4.8 の根本挙動を是正できない**。

### 動作している部分

- `_strip_text_tool_calls`（tool_loop.py:477）が `<function_calls>` / `<invoke>` / `<parameter>` のテキストを応答から除去 → ユーザー視認の XML 漏れを抑制。
- 同時に `had_text_tool_call=True` を返し、pause-turn ハンドラ（tool_loop.py:~936）が**テキスト形式専用の nudge**（「その形式は実行されない、正式な tool_use で今すぐ実行しろ」）を送る。
- ログでも発火を確認（後述 rid=fb65733f）。

### 足りない部分

- `MAX_CONSECUTIVE_EMPTY_TURNS = 2`（tool_loop.py:423）= **nudge は実質1回だけ**。4.8 が次ターンも同じテキスト形式で吐くと `consecutive=2` で打ち切り → `returning accumulated text`。
- 結果、**モデルが意図したツール呼び出しは実行されず、漏れを削り取った後の中途半端なプロンプトだけが残って返る。** これがユーザーから見た「応答が薄い / 切れてる / 直ってない」。
- `_strip_text_tool_calls` は XML タグは消すが、タグ外に出た素のトークン（例の `count` のような語）までは消さない。タグの外に漏れた残骸は視認され得る。

つまり緩和は「漏れを隠す」だけで「ツールを実行し直して正しい答えを出す」ところまでは到達していない。

---

## 証拠（ログ / journalctl）

### 現プロセス 601425（= 修正コード入り）で 4.8 のテキスト形式漏れが再発・打ち切り

チャンネル `1488369795807055964`（デバッグ会話）, rid=fb65733f:

```
12:36:18  turn=5  stop_reason=tool_use
12:36:18  pause turn (text-format tool call) ch=1488369795807055964 turn=5 consecutive=1 → nudging
12:36:27  turn=6  output_tokens=358  stop_reason=tool_use
12:36:27  pause turn limit reached ch=1488369795807055964 turn=6 consecutive=2 → returning accumulated text
```

→ **修正コードは動いている**（`pause turn (text-format tool call)` は新コードにしか無い文言）。にもかかわらず 4.8 が turn5→6 で連続してテキスト形式を吐き、1回の nudge で是正できず打ち切り。

### 旧プロセス 584339（再起動前, MAX=1）で Andrew Left の漏れ

チャンネル `1489297911895294042`（お父さん）, rid=13dc5c6f:

```
12:15:59  turn=2  stop_reason=tool_use
12:15:59  pause turn limit reached ch=1489297911895294042 turn=2 consecutive=1 → returning accumulated text
```

→ 旧プロセスは `MAX_CONSECUTIVE_EMPTY_TURNS=1`（当時の値）で nudge ゼロ即打ち切り。1回目の調査が空振りした往復の正体。

### プロセス境界

```
584339  11:40 起動（再起動前 / 旧コード）
601425  12:16:10 起動（先輩パッチ適用後 / 現用）   ← ExecMainStartTimestamp と一致
```

### モデル定数の現状（worktree = 実機）

```
config.py:85              CLAUDE_MODEL ... "claude-opus-4-8"
claude_api/entry.py:94    MODEL = "claude-opus-4-8"     ← 現用経路が読む
claude_sdk.py:84          MODEL = "claude-opus-4-8"
```

---

## 推奨対応

### A. 即時（根本原因1 — これだけで先輩の当初意図が実現）

3つの MODEL 定数を 4-7 に戻す。`git checkout`/`stash` は使わず、**該当行のみ手で編集**（stall 修正の他差分を保全するため）:

- `config.py` `CLAUDE_MODEL` のデフォルトを `claude-opus-4-7`
- `services/claude_api/entry.py:94` `MODEL = "claude-opus-4-7"`
- `services/claude_sdk.py:84` `MODEL = "claude-opus-4-7"`

または最小侵襲なら `.env` に `CLAUDE_MODEL=claude-opus-4-7` … **だが現用経路（entry.py）は `config.CLAUDE_MODEL` を読まないので .env だけでは効かない。** entry.py / claude_sdk.py のハードコードを直すのが必須。

再起動後の検証:
```
journalctl -u discord-bot --since "<restart>" | grep -E "pause turn \(text-format|claude_api_usage"
```
→ `pause turn (text-format tool call)` が出なくなれば 4.8 固有挙動が消えた証拠。

### B. defense-in-depth（根本原因2 — 4.8 を将来また使うなら必須）

- `MAX_CONSECUTIVE_EMPTY_TURNS` を上げて nudge 回数を増やす、**もしくは**より本質的に、`had_text_tool_call` を検知したら漏れた `<invoke>` XML を**パースして native tool_use を合成・実行**する（モデルの意図を捨てずに拾う）。後者が筋がいい。
- `_strip_text_tool_calls` の取りこぼし（タグ外に出た素トークン）をログ化して観測する。

### C. 構造（再発防止 — 今回の事故の本丸）

- **MODEL 定数の三重複を1つに集約する。** `config.CLAUDE_MODEL` を単一の真実源にし、`entry.py` / `claude_sdk.py` はそれを import する。今は entry.py が config を無視して自前定数を持つため、「config を直せば revert できる」という直感が裏切られ、revert が半端に適用される温床になっている。
- モデル revert は ADR 化し、「3定数 + .env を同時に触る」ことをチェックリストにする。

---

## 補足: なぜ「先輩は revert した」のに 4.8 なのか（時系列）

1. **5/29 18:06** `b762baf` で 3定数を 4-7 へ revert（正しい）。HEAD は 4-7。
2. **6/2** stall 修正パッチを作業ツリーで作成。基点が 4.8 移行系列だったため 3定数を 4-8 に巻き戻し（未コミット）。
3. **6/2 12:16:10** 再起動 → worktree が乗り、実機 4-8。
4. msg3 タグ漏れ（4.8 固有）が継続。緩和は発火するが 1-nudge で打ち切り → 「直ってない」。

revert の判断は正しかった。実装が worktree の巻き戻しで無効化されただけ。

---

## 修正実施 (2026-06-02)

### 根本原因1 — モデルを 4-7 に戻し、定数を1つに集約（推奨A + C）
`git checkout`/`stash` は使わず、3定数を `config.CLAUDE_MODEL`（単一の真実源、`.env` 上書き可、既定 `claude-opus-4-7`）に集約:
- `config.py`: `CLAUDE_MODEL` の既定を `claude-opus-4-7` に。コメントで「entry.py/claude_sdk.py はこれを import」と明示。
- `services/claude_api/entry.py`: `from config import CLAUDE_MODEL` を追加し `MODEL = CLAUDE_MODEL`（現用経路が読む定数）。
- `services/claude_sdk.py`: 同様に `MODEL = CLAUDE_MODEL`。
- 検証: 3モジュールとも `MODEL == "claude-opus-4-7"` を実値で確認。今後 config / `.env` を直せば現用経路にも効く（三重複の温床を解消）。

### 根本原因2 — テキスト形式呼び出しを「実行」に格上げ（推奨B）
nudge して諦める設計を、**パースして代理実行**に変更（`tool_loop.py`）:
- `_parse_text_tool_calls()` を追加。`<invoke name>`/`<parameter name>` を `(tool_name, input)` に復元（値は可能なら JSON デコードで型を合わせる）。
- agent loop で「native tool_use が0個 かつ テキスト形式を検知」した場合、パース→`can_use_tool` ゲート→`execute_tool` で実行→結果を user メッセージで返して `continue`（`consecutive_empty` もリセット）。これでモデルの意図を捨てず、打ち切りも起きない。
- パース不能（未閉じ等）の時だけ従来の是正 nudge にフォールバック。

### テスト
`_parse_text_tool_calls` のユニット、テキスト形式呼び出しが「漏れず・実行され・結果が返る」統合テストを追加。tool_loop/errors/stall は **88件 green**。

### 再起動後の検証手順
```
sudo systemctl restart discord-bot.service
journalctl -u discord-bot --since "<restart>" | grep -E "live model|claude_api_usage"
# entry.MODEL=claude-opus-4-7 で動いていること、
# "pause turn limit reached" での打ち切りが起きず "executing N text-format tool call(s)" が出ることを確認
```
