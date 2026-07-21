# health API 実装点検レポート（2026-07-21）

依頼: 「health のリンクがうまく機能していないと思うから点検してほしい」「apiの実装が正しいか点検して欲しい」
対象インシデント: 2026-07-21 01:20〜11:35:59 JST の約10時間15分、`https://health.haroin57.com/health` が HTTP 503 / status=degraded を返し続けた件。
点検のみ。bot コードの修正・再起動・設定変更は行っていない。

## TL;DR

- **真因**: OAuth access token の**夜間 idle 期限切れ**。2026-07-18 の grand refactor（`4240d02`）で
  `is_oauth_healthy()` が **ADR-0041 Fix A の供給ベース意味論から disk 期限チェックに置き換わる regression** が入り、
  「idle 中に access token が期限切れ → 誰も refresh しない → health が degraded 張り付き」が成立するようになった。
- **true positive か**: **実装 regression による偽 DOWN**。bot の供給能力は全期間無傷
  （復旧トリガーは 11:34:38 の先輩のメッセージ1通。refresh は1秒で成功し、11:34:50 に Anthropic API 200）。
  ADR-0041 Fix A の意味論のままなら 503 は一度も出ていない。
- **ADR-0041 の rotation 衝突（前例）とは別物**: 窓内に invalid_grant ゼロ、refresh 試行自体ゼロ。
  prod は専用 HOME（`/var/lib/kanae-bot/.claude/`）で衝突源も分離済み。
- **恒久対応しない限り毎晩再発する**: 条件は「最終 refresh + 8h（token 寿命）を過ぎて idle が続く」だけ。
  今夜も 19:34:41 JST（今朝の refresh 分の期限）以降 idle なら再発する。

## 1. 確定タイムライン（journalctl 実測）

| 時刻 (JST) | 事象 |
|---|---|
| 7/20 17:23:20 | 最後の OAuth refresh 成功（`expires in 28800s` = 8h） |
| 7/20 20:29 頃 | 最後の bot 活動。以降 idle（API 呼び出しなし = refresh 契機なし） |
| 7/21 01:18:20 | `expires_at`(01:23:20) − 5分バッファ を通過。`_credential_readiness` が `access_token_expired` を返し始める理論時刻 |
| 7/21 01:18:39 | 最初の 503（直前 01:17:59 まで 200。理論時刻の19秒後の probe で即失敗） |
| 7/21 01:20:59 | 新 probe の 3 回連続失敗成立 → DOWN 判定・通知（`FAIL_TO_DOWN=3` どおり） |
| 7/21 01:20〜11:34 | 503 継続（body 1026 bytes 一定 = **oauth 単独失敗**。他 subsystem は全期間 ok）。oauth のログ行はゼロ = refresh 試行自体が皆無 |
| 7/21 11:34:38 | 先輩がメッセージ送信（ch 1488369795807055964）→ Claude API 呼び出し発生 |
| 7/21 11:34:40〜41 | `ensure_valid_token()` → 「token EXPIRED」→ refresh **1秒で成功**（新期限 19:34:41 JST） |
| 7/21 11:34:50 | Anthropic API 200（メッセージ処理成功 = 供給能力の実証） |
| 7/21 11:34:59 / 11:35:59 | probe 200 ×2 連続 → UP 復帰通知（`OK_TO_UP=2` どおり。ダウン継続 10時間15分） |

数字の噛み合わせ: token 寿命 28800s、期限 01:23:20、5分バッファで 01:18:20 から fail、
probe 閾値 3 回で 01:20:59 DOWN — すべて分単位で実測と一致。**probe（新旧とも）は設計どおり動作**。

## 2. 真因の構造（3層）

### 層1 [High] `is_oauth_healthy()` の ADR-0041 regression — 直接原因

ADR-0041 Fix A（`d2696bb`, 2026-07-08）が定めた意味論:

```python
# ADR-0041 Fix A（旧実装）: 「実際にトークンを供給できるか」
acct = _acct()
if acct.access_token and not _is_expired(acct):
    return True   # 有効トークン保持中は健全
return acct.refresh_failure_count < _REFRESH_FAILURE_ALERT_THRESHOLD
```

grand refactor（`4240d02`, 2026-07-18 = インシデント3日前）で以下に置換された:

```python
# 現行（services/oauth_manager.py L209-217）: disk credential の静的検証のみ
def is_oauth_healthy() -> bool:
    ok, _ = _credential_readiness(_acct())
    return ok
```

`_credential_readiness()` は disk 上の `expiresAt <= now + 5min` で即 False を返す（L244-245）。
**refresh token の有効性も `refresh_failure_count` も一切見ない。**
旧意味論なら今回のケースは `refresh_failure_count=0 < 3` → healthy → 200 のままで、インシデント自体が発生しない。
regression は prod リリース `3bcd432`（PR #76 merge）に含まれることを ancestry で確認済み。

なお refactor 後の実装は「standby の破損を active の健全性に波及させない」目的（docstring）自体は正しく達成しており、
壊れたのは active 側の期限切れの扱いだけ。**ADR-0041 は Status: accepted のまま実装だけが逸脱**している状態で、
`tests/test_oauth_manager.py::test_is_oauth_healthy_rejects_expired_disk_credential` が逸脱後の挙動をテストで固定してしまっている
（ADR の supply-semantics を pin する regression テストが存在しなかったことが、refactor で無言に意味論が反転できた理由）。

### 層2 [Medium] idle 中に token を refresh する主体が存在しない

refresh の契機は (a) 起動時 `_bootstrap_oauth()`、(b) API 呼び出し時 `ensure_valid_token()` の2つだけ。
定期 refresh ループは存在しない（コードベース全体を確認済み）。token 寿命は 8h なので、
**最終利用から 8h を超える idle（≒毎晩）で disk credential は必ず期限切れになる**。
層1の判定がそれを「不健全」と報告するため、夜間 idle → 朝の初回メッセージまで degraded が構造的に確定する。

### 層3 [Medium] severity 無分別の 503 マッピング × probe の「DOWN」語彙

- bot 側: 5 subsystem（discord / oauth / mem0 / system / release）の **AND** で `status` を決め、
  ok 以外は一律 HTTP 503（`health_server.py` L91）。
- probe 側: 「HTTP 200 かつ body.status == "ok"」以外は一律失敗 → 「🔴 かなえ DOWN 判定」+ 30分毎の再通知。

この契約だと discord gateway が完全に生きていても（今回 latency ~166ms で全期間正常）、
oauth の期限切れだけで「サービス DOWN」として10時間通知され続ける。
**「readiness degraded」と「サービス DOWN」の区別が経路のどこにも存在しない**のが監視意味論としての問題。
通知にはどの subsystem が落ちたかも含まれない（reason は `HTTP 503, status=degraded` のみ）。

## 3. true positive 判定

**偽 DOWN（実装 regression 起因）と判定する。**

- 「disk の access token が期限切れ」という事実の報告としては正確（その意味では嘘は言っていない）
- しかし監視の目的である「サービス供給能力」の指標としては誤報:
  - refresh token は全期間有効（11:34:41 に一発成功が証拠）
  - Discord gateway は全期間接続維持
  - 窓内に受信メッセージは無く、**実際に失敗したユーザー影響はゼロ**。最初の実トラフィックが自力復旧のトリガーになった
- 判定根拠の要約: 「10時間の間、直せる者が誰も呼ばれなかっただけで、壊れてはいなかった」

## 4. oauth_manager L901 防御（stale failure clearing）の検証

`_load_account_from_disk()` の「新しい credential 検知で `refresh_permanent`/`refresh_failure_count` をクリア」する防御（ADR-0041 Fix B）は、
**今回は対象外のシナリオであり、正しく非発火**。

- 窓内に `invalid_grant` ログ: 0 件 / refresh 失敗: 0 件 / `clearing stale refresh-failure state` ログ: 0 件
- この防御は「rotation 衝突で張り付いた permanent failure」を解除するもの。今回は failure 状態が一度も発生していない
- つまり「機能しなかった」のではなく「守備範囲外」。防御自体の欠陥は今回の証拠からは検出されず

## 5. 実装レビュー（severity 付き）

### High

- **H-1: `is_oauth_healthy()` が ADR-0041 Fix A と矛盾**（層1のとおり）。
  さらに内部矛盾として、`prepare_and_failover_to_secondary()`（L1114-）は standby の `access_token_expired` を
  「refresh すれば使える」と明示的に扱う（docstring: *an expired but refreshable standby remains usable*）。
  **期限切れ ≠ 使用不能とコードベース自身が認めているのに、active の health 判定だけがそれを適用していない。**

### Medium

- **M-1: idle 時の proactive refresh 不在**（層2のとおり）。恒久対応しなければ毎晩再発する。
- **M-2: severity 無分別の degraded → 503 → 「DOWN」通知**（層3のとおり）。
- **M-3: ADR 契約の regression テスト欠落**。health 系テスト（29 passed を確認）は全て `_check_oauth` を patch しており、
  oauth×health の統合意味論は `test_oauth_manager.py` 側のみ。そこが regression 後の挙動を pin している。
  「active token が期限切れでも refresh 経路が健全なら /health は ok」という ADR-0041 の契約を encode したテストがどこにも無い。

### Low

- **L-1: `redundancy_ok` が恒常ノイズ**。standby にも `_credential_readiness` を適用するため、
  使われていない standby の access token は自然に期限切れ → `redundancy_ok: false` が定常状態になる
  （点検時点の実レスポンスでも `standby_credential_reason: "access_token_expired"` を確認）。
  切替時に refresh する設計（H-1 参照）なので、期限切れだけなら冗長性は失われていない。シグナルとして死んでいる。
- **L-2: `_check_discord` の latency エッジ**（health.py L276-279）。discord.py の `bot.latency` は heartbeat ack 前に
  `float('nan')` を返しうる。`nan` は truthy なので `int(nan * 1000)` が ValueError → 外側 except で
  `ok: False, reason: "cannot convert float NaN to integer"` という紛らわしい degraded になる。
  また `latency == 0.0` は falsy なので `gateway_latency_ms: None` になる（こちらは表示だけの問題）。
- **L-3: probe 通知に落ちた subsystem 名が無い**（kanae-cf-workers 側、記載のみ）。
  body は取得済みなので `subsystems` から `ok: false` のキーを拾って通知文に含められる。

### 問題なしと確認した点

- `/health` = `/ready` の alias 関係: 実装・テストとも一貫。`/live` は依存に触れず 200（実機確認済み）
- ブロッキング: 全チェックを `asyncio.to_thread` で退避。イベントループ非ブロックはテストで担保
- タイムアウト予算: 最悪ケース ≈ mem0 heartbeat 2s + ファイル IO ≪ probe の 10s。キャッシュ無しだが 2 probe/分では問題ない
- 例外処理: 各 `_check_*` は broad except で fail-closed、snapshot 全体失敗も 503 fallback（`_handle_ready`）。妥当
- release 検証: bind 後にバックグラウンド実行、fail-closed。設計どおり
- probe の状態機械: フラップ抑制・KV write-on-change・通知失敗の自己回復（`last_alert_at === null` リトライ）を含め堅実。
  今回のタイムラインと分単位で整合し、**probe 側に齟齬なし**

## 6. 修正提案（提案のみ・未実施）

優先度順。1 だけで再発は止まる。

1. **[H-1 対応] `is_oauth_healthy()` を ADR-0041 Fix A の供給意味論に戻す**。
   例: 有効な in-memory token → True。期限切れなら `refresh_permanent` / `refresh_failure_count >= 閾値` のときだけ False
   （= 「refresh すれば供給できる」状態は健全）。`_credential_readiness` は detail 表示と startup/standby 検証用に温存。
   併せて **ADR-0041 の契約を pin する regression テストを追加**し、`test_is_oauth_healthy_rejects_expired_disk_credential` は
   意図を明確にして書き直す（disk 検証は `credential_ok` フィールドの検証として残せばよい）。
2. **[M-1 対応] proactive refresh keepalive**（任意だが推奨）。
   例: 30分毎に `_is_expired()` なら refresh する軽量バックグラウンドタスク（`refresh_lock` 経由、`OAUTH_NO_REFRESH` の親プロセス限定）。
   disk credential が常に新鮮になり、subprocess（`OAUTH_NO_REFRESH=1` の読み手）にも都合が良い。
   ※ 1 を入れれば health 誤報は消えるので、これは「朝一メッセージの refresh 1秒」を消す UX 改善が主目的。
3. **[M-2 対応] severity 2値化**。bot 側: `status ∈ {ok, degraded, critical}` とし、HTTP 503 は critical
   （discord 死亡 / snapshot 崩壊）のみ。degraded は 200 + body で表現。
   probe 側: fetch 失敗・503 → 🔴 DOWN、200 + degraded → 🟠 劣化通知（別 emoji・落ちた subsystem 名入り）。
   ※ probe 側変更は kanae-cf-workers なので本レポート記載に留める。両側の同時変更が必要（片側だけだと契約が割れる）。
4. **[L-1 対応] `redundancy_ok` の意味論修正**。standby の `access_token_expired` は「切替時に refresh 可能」なので
   redundancy 喪失と扱わない（missing / unreadable / insecure / invalid_grant 既知のみ false）。
5. **[L-2 対応] latency の NaN ガード**。`latency is not None and math.isfinite(latency)` でガードして
   `int()` 変換する。

## 7. スコープ外の観察（別件・要否は先輩判断）

- **7/20 00:00〜09:54 にも別パターンの 503 フラッピング**（計 363 probe 失敗 ≈ 3h 分）。
  body サイズが 3 クラスタ（932-935 / 984-988 / 1024-1027）に分かれ = 複数 subsystem の混合要因。
  別プロセス（PID 2303932、restart 前の旧リリース）での事象で、今回の単一要因パターンとは別物。未深掘り。
- 旧 probe は UA `Go-http-client/2.0` で毎分 :40 秒、新 probe は `kanae-health-probe/1.0` で毎分 :59 秒に到達。
  並走は既知仕様のとおり。01:21:01 に Discordbot/2.0 の /health アクセスあり（DOWN 通知内 URL の unfurl と推定、実害なし）。
- `refresh-cli-identity.service` が failed 状態（health とは独立の別件）。

---
点検実施: 2026-07-21 12:31〜 JST / 実行内容: journalctl 解析・コード読解・git 履歴追跡・pytest（health 系 29 passed）・実機 curl。
点検時点では bot 本体・kanae-cf-workers とも未変更。

**追記 (2026-07-21 午後)**: §6 の ①④⑤ を branch `agent/fix-health-oauth-semantics` で実装（ADR-0052）。
②③ は引き続き提案のまま（先輩の設計判断待ち）。
