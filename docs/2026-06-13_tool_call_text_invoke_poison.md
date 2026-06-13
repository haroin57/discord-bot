# ツール呼び出し失敗の調査: text-form `<invoke>` poison

調査日: 2026-06-13 / 対象: channel 1488369795807055964 / rid=b013ea8a
対象メッセージ: Discord msg 1515020696009310250（送信者: b0nchichi / お父さん）

## 結論（一行）

レート制限ではない。Fable 5 が refusal → Opus 4.8 にフォールバックし、その Opus が
ツール呼び出しを **本物の `tool_use` ブロックではなく text ブロック内の生 XML**
（`course\n<invoke name="brave_news_search">...`）として出力したため、tool_loop が
実行対象（`btype == "tool_use"`）を一つも見つけられず、生テキストがそのまま Discord
に送信された。

## 実データで確定した事実

時系列（journalctl, rid=b013ea8a）:
- 00:50:56 リクエスト開始。prior_turns=188, tools=23, admin=True。
- 00:51:14 Fable 5 が HTTP 200 / `stop_reason=refusal` / category=None / output_tokens=2。
- 00:51:14 `claude-opus-4-8` へフォールバック。
- 00:51:21 Opus が HTTP 200 / `stop_reason=end_turn` / output_tokens=113。
- 00:51:21 `response generated via refusal fallback model=claude-opus-4-8`。

レート制限は否定（00:51:14 / 00:51:21 とも `status=allowed`, 5h_util=0.12→0.13,
7d_util=0.50, HTTP 429 なし）。overage-status=rejected / out_of_credits は出ているが
通常枠 allowed なので本件の直接原因ではない。

失敗ターンの保存構造（`.state/history/1488369795807055964.jsonl` の該当 assistant 行）:
- `content`（display 文字列）: `初値、確認します。お父さん少々お待ちを。\n\ncourse\n<invoke name="brave_news_search">\n<parameter ...>`
- **`api_messages`（replay の正本）: `[{role:assistant, content:[{type:"text", text:"...course\n<invoke ...>"}]}]`**
  → tool_use ブロックは**ゼロ**。text ブロック一個だけ。

tool_loop の実行ゲート: `services/claude_api/tool_loop.py` は `btype == "tool_use"` の
ブロックのみ実行（L184, L933 で `!= "tool_use"` を skip）。text ブロック中の `<invoke>`
は構文上ただの文字列なので素通りし、Discord にそのまま出る。

## チャッピー分析との差分（重要）

チャッピーの方向性は正しいが、poison の重さを取り違えている。

1. **「履歴に生テキストが残っている」→ 実際は api_messages（replay 正本）が汚染**。
   channel の assistant 行のうち 9 行が display に text-form `<invoke>` を持ち、
   **そのうち 5 行は api_messages にも tool_use ではなく text として保存**している。
   resume のたびにこの壊れた形式が**構造ごと**モデルへ再投入される。display 文字列
   だけの問題ではない。

2. **「模倣した可能性が高い」→ 自己 poison ループとして確定**。
   同 channel の過去 assistant 行（L698）でモデル自身が
   「さっきから三回も `course` で書き出す癖が出てる。正しいのは `function_calls`」
   と明言している。few-shot 的に自分の壊れた過去出力を真似て再生産する、ADR-0008
   （no-response poison loop）と同型の構造。対象が refusal ではなく**ツール呼び出し
   形式**である点だけが違う。

3. **refusal→フォールバックは引き金であって根因ではない**。
   poison された履歴を食えば Fable でも Opus でも text-invoke を出しうる。今回は
   たまたま Fable refusal で Opus に渡ったが、原因は履歴汚染。

4. **ツール名の非正規化はチャッピーの指摘どおり**。モデルは `brave_news_search`
   と無印で書いたが、広告名は `mcp__brave-search__brave_news_search`（`mcp__...`
   prefix 欠落）。ただし今回は text なので名前解決まで到達すらしていない（副次要因）。

## 未確定

- `course` が何由来か。`<function_calls>` 開きタグ位置に `course` が入っている。
  コードベースに `function_calls` / `course` の置換は無い（grep 0 hit）ので bot の
  sanitize 由来ではなく、モデル出力時点の生成物。tokenizer 往復の劣化か、モデルの
  混乱か。本件の失敗自体（text vs tool_use）には影響しないので保留。

## 対策（**3 点すべて実装済み 2026-06-13**）

共通検出/除去（単一の真実源）: `services/claude_api/errors.py`
- `contains_text_tool_call(text)`: 構造パターン（`<invoke name="…"` + `<parameter`
  または `</invoke>`、もしくは `<function_calls>`）でのみ検出。単なる言及
  （`` `<invoke name="X">` `` のような prose）は弾かない＝誤検出を避ける。
- `strip_text_tool_call(text)`: 最初の XML opener から末尾までを除去し、手前に
  残った劣化 opener トークン（`course` / `function_calls`）も落とす。自然文の
  prefix は保持。

1. **是正リトライ + 送信前ガード**（fix 1）: `services/claude_api/tool_loop.py`。
   turn が text-form tool call を出して real tool_use ゼロなら、是正用 user turn
   （正規 tool_use 形式・`mcp__` 完全名の指示）を 1 回だけ追加して再プロンプト
   （`MAX_TEXT_INVOKE_CORRECTIONS=1`）。是正枠を使い切ったら turn_text から生 XML を
   strip して返す（Discord にも生 XML を出さない）。
2. **replay 除染**（fix 2）: `services/session.py` `_sanitize_replay_messages` /
   display fallback で、過去 assistant 行の text ブロック・display 文字列から
   text-invoke を除去。全消去なら行を drop。
   → 実データ検証: 該当 channel（188 msgs）replay から `<invoke name=` / `course`
   形式が完全消失、自然文は保持を確認。
3. **書き込み時ガード**（fix 3）: `services/session.py` `_prepare_api_messages` /
   `build_compact_history_entries` で persist 直前に同じ除去。text-invoke のみの
   turn は api_messages から drop し、発生源を断つ。
4. **テスト**: `test_claude_api_errors.py`（検出/除去 6）、`test_session.py`
   （persist/replay 除染 3）、`test_claude_api_tool_loop.py`（是正リトライ・枯渇時
   strip・real tool_use 誤検出なし 3）。全 1924 passed。

補足: 既存の `_sanitize_replay_messages`（DKf パリティ: 非文字列 text ブロック除去）
だけではこの poison は落ちない（text は文字列として正常）ため、上記の text-invoke
検出を別途統合した。

## 関連

- ADR-0008（no-response poison loop / refusal capture）— 同型の自己 poison。
- ADR-0010（Fable refusal / cache cascade）— refusal→フォールバック経路。
