# emotion-llm-transition-plan.md を v3 (emotion 常駐 daemon 化) に改訂

## Context

ユーザーから「セッション resume が時間かかるなら立ち上げっぱなしにすればいい」という提案があり、議論の結果以下の方針で合意した:

- **emotion session のみ bot プロセス内で常駐**（`asyncio.subprocess.Popen` + `--output-format stream-json`）
- **daily / summary は cron + `subprocess.run` のまま**（15 分間隔だから起動コストは誤差、常駐させると VPS 2GB RAM を圧迫）
- **silence tick も bot プロセス内 asyncio タイマーに統合**（同じ daemon を共有して Claude セッション内記憶を分断させない）
- **`scripts/kanae_emotion_tick.py` は廃止**
- daemon 内 `asyncio.Lock` で会話 feedback と silence tick を直列化（旧 `kanae_emotion_session.lock` flock は撤去）

対象ファイル: `/home/ubuntu/discord-bot/docs/2026-06-14-emotion-llm-transition-plan.md`

## すでに反映済みのセクション

Plan mode 再突入前に以下を編集完了:

1. **Revision Log** — v3 エントリ追加
2. **§1.4 設計原則** — emotion session 常駐化 + daemon 内シリアライズの 2 行追加、fail-safe ファーストに daemon 再起動の文言追加
3. **§2.1 LLM 呼び出し方式** — セッション設計テーブルを「呼び出し方式」列付きに改訂、emotion 常駐 daemon の Python 擬似コード追加、メリット/トレードオフを刷新
4. **§2.2 システム構成図** — Emotion Engine ブロックを「bot プロセス内 daemon」に書き換え、silence tick を「bot 内 asyncio タイマー」に
5. **§4.1 2 つの呼び出しパス** — silence のトリガーを「bot 内 asyncio.create_task 30 分タイマー」に、`kanae_emotion_tick.py` 廃止を明記

## 残りの改訂対象（Plan 承認後に実施）

### §5 排他制御

- §5.1 lock 構造テーブルから `kanae_emotion_session.lock` を削除、「daemon 内 asyncio.Lock に置換」と注記
- §5.2 会話連投時の挙動を「daemon 内 asyncio.Lock で順次処理、CLI 起動コストが消えるため遅延短縮」に書き換え
- §5.3 cron 構成から `*/30 7-23 * * * kanae_emotion_tick.py` の行を削除

### §9 実装ファイル一覧

- 新規追加: `services/emotion/persistent_session.py` — emotion 常駐 daemon クラス（`asyncio.subprocess.Popen` + stream-json 読み取り + asyncio.Lock + health check + 自動再起動）
- 改修: `bot.py` — 起動時に daemon spawn、shutdown 時に terminate、30 分 silence タイマーを `asyncio.create_task` で起動
- 改修: `services/emotion/cli.py` — daily / summary 用の subprocess.run ラッパーに責務を絞る
- 削除: `scripts/kanae_emotion_tick.py`
- 削除予定の lock: `services/emotion/lock.py` の `emotion_session_lock` ヘルパー（state file 用の `emotion_lock` は残す）

### §10 cron 構成

- silence tick の cron 行を削除。daily と summary だけ残す
- 「silence tick は bot プロセス内で動く」「bot 停止中は silence tick も止まる（systemd auto restart が拾う）」を注記

### §11 データフロー図

- emotion 呼び出し経路を「会話 feedback / silence タイマー → bot 内 daemon → Claude CLI (stream-json)」に書き換え
- 「30 分ごと (cron)」を「30 分ごと (bot 内 asyncio タイマー)」に

### §12 Fail-safe

以下を追加:
| # | 障害 | 対応 |
|---|---|---|
| 18 | daemon プロセスのクラッシュ (returncode != None) | health check で検知、自動 `--resume` で再起動。記憶ロストなし |
| 19 | daemon stdin/stdout の deadlock or hang | query 単位で `asyncio.wait_for` で timeout、超過したら kill + 再起動 |
| 20 | bot 停止中の silence tick 喪失 | systemd auto restart で復帰、復帰後の最初の tick で hours_since_last を見て loneliness 下限保証が補完 |
| 21 | stream-json parse 失敗 (改行待ちで詰まる) | result メッセージタイプが来ない場合 30 秒 timeout → kill + 再起動 |

### §13 quota 消費見積もり

- emotion 沈黙の呼び出し回数は変わらない (34 回/日)
- emotion 会話の呼び出し回数も変わらない (~30 回/日)
- → トークン消費見積もりは変更なし
- 起動オーバーヘッドの記述だけ「常駐化により emotion 側はゼロ」と注記

### §15 実装順序

新しい Phase を追加:
- **P1 改訂**: `services/emotion/cli.py` (daily/summary 用) + `services/emotion/persistent_session.py` (emotion daemon) + `session.py` + `lock.py` 改修
- **P6 改訂**: silence パスの実装で `kanae_emotion_tick.py` を作る代わりに、bot.py の `on_ready` で `asyncio.create_task(silence_tick_loop())` を起動。30 分 sleep ループで daemon に query
- **P7 改訂**: 旧 `kanae_emotion_tick.py` を削除（既存ファイルが残っているはず）

### §16 Open Questions

以下を追加 / 修正:
- ~~連投時の feedback キューイング上限~~ → daemon 常駐で起動コスト消えるので、5 連投でも 5-10 秒程度。許容範囲。Open Question から削除
- **新規**: daemon プロセスの長時間動作信頼性 — 24h 以上連続稼働で memory leak / socket leak がないか、観察期間 1 週間で評価
- **新規**: stream-json の result メッセージ境界検出ロジック — Claude CLI 2.1.92 の実機検証が必要（result type の object が EOF/改行のどちらでマーカーになるか）

## 検証

emotion-llm-transition-plan.md を読み直して、以下を確認:
1. v3 改訂後の整合性: §1.4 / §2.1 / §2.2 / §4.1 / §5 / §9 / §10 / §11 / §12 / §15 / §16 の記述が矛盾なくつながっているか
2. 削除予定の `scripts/kanae_emotion_tick.py` への参照が全セクションで消えているか
3. `kanae_emotion_session.lock` への参照が全セクションで消えているか（state file 用の `kanae_emotion.lock` は残る）
4. daemon の health check / 再起動ロジックが fail-safe 表に反映されているか

## Critical Files

### 編集対象（Plan 承認後）
- [/home/ubuntu/discord-bot/docs/2026-06-14-emotion-llm-transition-plan.md](../../home/ubuntu/discord-bot/docs/2026-06-14-emotion-llm-transition-plan.md)

### 参考（読み取りのみ）
- [services/emotion/feedback.py](../../home/ubuntu/discord-bot/services/emotion/feedback.py) — 現行 feedback ループ
- [services/emotion/lock.py](../../home/ubuntu/discord-bot/services/emotion/lock.py) — 現行の flock 構造
- [scripts/kanae_emotion_tick.py](../../home/ubuntu/discord-bot/scripts/kanae_emotion_tick.py) — 廃止対象の cron スクリプト
- [bot.py](../../home/ubuntu/discord-bot/bot.py) — 常駐 daemon の spawn / shutdown を組み込む場所
