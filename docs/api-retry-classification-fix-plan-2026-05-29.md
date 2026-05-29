# API リトライ分類バグ 修正プラン (2026-05-29)

## 概要

Anthropic API の `overloaded_error` が **HTTP 200 のストリーム途中** で返ってくるケースで、
`classify_anthropic_error` がこれを `api_exit`（永続エラー）に誤分類し、**リトライせずに即座にユーザーへエラーを返している**。
本来 529（混雑）相当として transient 扱いし backoff リトライすべきもの。

## 観測された事象（根拠）

5/28 17:51 / 17:53、channel=1491501258408726589（admin=先輩）の 2 件で発生。

```
httpx: POST /v1/messages?beta=true "HTTP/2 200 OK"
tool_loop: agent loop API error turn=0 category=api_exit:
  {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}, 'request_id': 'req_...'}
entry: api error attempt 1/4 type=api_exit ...
```

- HTTP レイヤは **200 OK**。Anthropic はリクエスト受理後、SSE ストリーム途中で `error` イベント（`overloaded_error`）を送出した。
- ログの `attempt 1/4` は誤解を招く表記で、実際には attempt 2 以降は走っていない（permanent 分類で即 return しているため）。
- ユーザーには「⚠️ 処理が中断されました。もう一度試してください。」が返った。

## 根本原因（コードパス）

anthropic SDK 0.88.0 の挙動を追跡:

1. `anthropic/_streaming.py:105-118` — ストリーム内 `error` イベント受信時、
   `self._client._make_status_error(err_msg, body=body, response=self.response)` を呼ぶ。
   この `response.status_code` は **200**（HTTP 自体は成功しているため）。
2. `anthropic/_client.py` の `_make_status_error` は status コードで例外クラスを振り分けるが、
   200 はどの分岐（400/401/403/404/409/413/422/429/529/>=500）にも該当せず、
   **素の `APIStatusError`（status_code=200, body に overloaded_error）** を返す。
3. `services/claude_api/errors.py:56` `classify_anthropic_error`:
   - `isinstance(exc, anthropic.APIStatusError)` → True
   - `status == 529`? → No（200 なので）
   - body の `_CONTEXT_PATTERNS` 一致なし
   - `status == 400`? → No
   - → **`errors.py:88` `return "api_exit"`** ← ここで誤分類
4. `services/claude_api/errors.py:108-109` `_is_transient` が見るのは `("rate_limit", "overloaded")` のみ。
   `api_exit` は transient ではない。
5. `services/claude_api/entry.py:472-476`
   `if not _is_transient(category): return _error_message(category)` → **即 return、リトライなし**。

**要点**: overloaded が「529 で来れば transient、200 ストリーム経由で来れば永続」という非対称が事故の本質。
分類は status コードだけでなく **body の `error.type` を見る**必要がある。

## 修正方針

`services/claude_api/errors.py` の `classify_anthropic_error` 内、`APIStatusError` 分岐を
status コード単独依存から **`body.error.type` 併用**へ変更する。

### 変更後の分岐ロジック（案）

```python
if isinstance(exc, anthropic.APIStatusError):
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    err_type = ""
    body_text = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            err_type = str(err.get("type", "")).lower()
            body_text = str(err.get("message", ""))
        else:
            body_text = str(err or "")
    else:
        body_text = str(body or "")
    body_lower = body_text.lower()

    # ストリーム内 error イベントは HTTP-200 の接続上で届くため、
    # SDK は status_code=200 の素の APIStatusError を投げ、本当の
    # 種別は body.error.type に入る。status だけでなく error.type で分類する。
    if status == 529 or "overloaded" in err_type or "overloaded" in body_lower:
        return "overloaded"
    if status == 429 or "rate_limit" in err_type:
        return "rate_limit"
    if any(p in body_lower for p in _CONTEXT_PATTERNS):
        return "context_overflow"
    if status == 400 or err_type == "invalid_request_error":
        return "invalid_request"
    # 汎用 5xx / api_error はサーバ側一時障害 → transient 扱いで 1 回はリトライさせる
    if (status is not None and status >= 500) or err_type == "api_error":
        return "overloaded"
    return "api_exit"
```

### 設計上の判断

- **`error.type` を一次情報にする**: status コードは HTTP トランスポート層、`error.type` は
  アプリ層。ストリーミングではこの 2 つが乖離する（200 + overloaded_error）。アプリ層を信頼する。
- **`api_error` / 5xx を transient に寄せる**: サーバ側一時障害は backoff で回復しうる。
  ただし `_is_transient` に乗るので `TRANSIENT_MAX_ATTEMPTS` で上限が効く（暴走しない）。
  `api_exit` は「分類不能で打ち切る」最後の砦として残す。
- **400 / invalid_request は据え置き**: 決定的エラーなのでリトライ無意味（既存方針どおり）。
  画像サイズ超過などはこちらで正しく弾く（直近コミット 5e14022 と整合）。

### 影響範囲

- 変更は `services/claude_api/errors.py` の 1 関数のみ。`entry.py` のリトライ制御は無変更で意図どおり動く。
- `_is_transient` / `_error_message` は変更不要（既に "overloaded" を扱える）。
- 既存の 529 / 429 ケースの挙動は不変（上位互換）。

## テスト方針

`tests/` に classify のユニットテストを追加:

1. `status_code=200` + `body={'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}` → `"overloaded"`
2. `status_code=529` → `"overloaded"`（既存維持）
3. `status_code=200` + `error.type='rate_limit_error'` → `"rate_limit"`
4. `status_code=400` + `error.type='invalid_request_error'` → `"invalid_request"`
5. `status_code=200` + context-overflow message → `"context_overflow"`
6. `status_code=200` + `error.type='api_error'` → `"overloaded"`（transient 化の確認）
7. 分類不能（空 body, status=200, 未知 type）→ `"api_exit"`（フォールバック維持）

`anthropic.APIStatusError` をモックで構築（`response` に status_code、`body` に dict）して検証する。

## 別件メモ（本バグとは独立、要フォローアップ）

### 1. emotion daemon の refusal ロック

`services.emotion.persistent_session` の daemon が **2026-05-26 15:11 頃から refusal ループに陥っている**。
入力を jailbreak と誤認して
`"I will not respond... This conversation is permanently closed... 23rd iteration"`
を返し続けており、5/27 以降だけで **220 回** `failed to parse result JSON` を記録。
毎回 `daemon returned None` → previous state 維持にフォールバックしているため
WARNING 止まりで応答自体は止めていないが、**感情状態が更新されなくなっている**。
daemon セッションの再起動／プロンプト境界の見直しが必要。

### 2. 「処理が途中で止まる」配信問題（rid=6072977f / 5/29 13:10）

並列調査ワークフロー（delivery-path / stall-status / log-forensics の 3 finder）で特定。

#### 観測

- rid=6072977f, channel=1496666613448048732, user=573489206240870410（先輩, admin）
- 13:10:26 `claude_api_usage turn=1 output_tokens=3313 stop_reason=end_turn` → API は **単一 turn・tool 呼びなしで正常完了**
- 13:10:26 → 13:10:42 の間に **約 16 秒の空白**（ログ行なし）
- 13:09-13:15 の窓に **WARNING / ERROR / traceback は皆無**。CancelledError も AnthropicError もなし。

#### 配信経路（実コード確認済み）

end_turn のみのターンでは、プロンプト本文は **ストリーム中に `_incremental_send` でライブ配信**される
（最終ブロックではない）:

1. `services/claude_api/tool_loop.py:628-635` — ターンごとに `incremental_text_callback(turn_text)` を 1 回呼ぶ。
   今回は単一ターンなので **3313 token ぶんの全文が 1 回**で渡る。
2. `bot.py:1631-1647` `_incremental_send` — `split_message()`（`utils/discord_helpers.py:72`, 2000 字上限）で分割し、
   `for chunk in chunks: await message.channel.send(chunk)` を **ペーシング一切なしのタイトループ**で送信
   （`bot.py:1637-1641`）。全 chunk 送信後に `_live_sent_any["v"]=True`。
3. `bot.py:2042` — `_live_sent_any` が True かつ rich content（表/数式）なしなら最終ブロックは何もしない。

output_tokens=3313 ≈ 5,000〜9,000 字 → **3〜5 個の chunk** に分割され、連続送信される。

#### 根本原因（確度: 中〜高）

**ペーシングのない複数 chunk 連続送信が Discord のチャンネル単位 send レートリミットbucket に当たり、
chunk 2..N の配信が約 16 秒かけてバラバラに到着する。**
discord.py は bucket 枯渇時に**事前 sleep（無音・ログなし）**、もしくは 429 で `retry_after` sleep
（ログは `discord.http` ロガーのみ、`bot` には出ない）するため、**エラーログが一切残らない**。
ユーザーは chunk 1 だけ表示された状態で残りを待つことになり、「途中で止まった」ように見える。

log-forensics finder の独立検証: 配信 abort の証跡ゼロ、trace は既知正常 rid（6be704a7 / 9eed335d）と一致。
→ **配信は完了している。メッセージが失われたのではなく、長文が ~16 秒かけて分割到着して固まって見えた**のが実態。

#### 正直な留保（logs で確定できない点）

- サーバログからは「ユーザーの Discord クライアントに全 chunk が届いたか」を直接確認できない。
  `_incremental_send` は **chunk 単位の送信ログを一切出していない**ため、成功・部分失敗・throw のいずれでも
  trace は同じ見た目になる（"trace ends there" は何も証明しない）。
- 16 秒の空白を「送信スロットリング」と断定したが、emotion daemon 復帰後の LLM 呼び出し（13:10:42 完了）が
  一部を食っている可能性は排除しきれない。

#### 潜在バグ（今回は未発火だが要修正）

`_incremental_send` が chunk k で例外を投げると、`_live_sent_any=True`（`bot.py:1642`）に到達する前に
re-raise（`bot.py:1647`）→ tool_loop が WARNING だけ出して握る → `_live_sent_any` は False のまま →
最終ブロック `bot.py:2081` の `else` 枝が **response_text 全文を再送** → **既に送った chunk 1..k-1 が重複**する。

#### 修正方針

1. **chunk 単位のデバッグログ追加**（最優先・低リスク）: `_incremental_send` に
   `chunk i/N len=...` を出す。これがないと「止まった」報告を今後も診断できない。
2. **部分失敗時の重複再送を防ぐ**: 送信済み chunk 数を記録し、re-raise ではなく**未送信分のみ**を送る、
   もしくは `_live_sent_any` を chunk 1 送信後に True にして最終ブロックの全文再送を抑止する。
3. **長文の「固まって見える」UX を解消**: 全 chunk 送信完了まで `⏳`/`📝 送信中` ステータスを残すか、
   chunk 間に短い `asyncio.sleep`（数百 ms）+ 進捗表示を入れて、staggered 配信が無音で止まって見えないようにする。
4. （任意）長文は分割連投ではなく**ファイル添付**に切り替える閾値を設ける（CLAUDE.md の「30 行超はファイル」方針と整合）。

#### 確証を上げる次の一手

修正 1（chunk ログ）を入れて再現を待てば、「全 chunk 送信されたが遅かった」のか
「途中で本当に落ちた」のかが一発で切り分く。現状の最有力は前者。
