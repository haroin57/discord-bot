# Mem0 alpha 第2段階改善の結果

対象は2026-09-09の管理対象checkoutです。基準は `b42b908ce681b21ee02e37a56620c5780e042fc0` / `0.3.1+botquality.1`、候補は `0.3.2+botquality.2` です。ソース版・wheel版のalpha回帰は各497件、bot全体は3,846件が通過しました。全体監査を分担し、[実装計画](mem0-alpha-next-plan.md)を作成した後に実装しました。Herdrでは既存の検索レビュー用ペインを使用しました。

本番サービス、認証、モデル、共有venv、dirtyな元checkout、元upstream、既存記憶の一括再索引は変更していません。今回の成果は実装・検証した候補版です。本番への配備を意味しません。

## 実装した変更

- A1〜A4は、起動後のdurable batch回復、少量会話の経過時間flush、耐久backoff、公平なowner/channel巡回、個別turnの出典・作者・発言時刻です。現在の許可済みownerだけを処理し、不明ownerの旧入力は保持します。assistant文にuser作者を付けません。
- B1〜B7は、長文の末尾を失わない比較、属性lookup/分類障害の未完了判定、耐久confirmation ledger、明確な個別対象の取消、長文抽出の途中再開、連続訂正鎖の修復、抽出schemaと内部制御値の分離です。
- C1〜C11は、編集・遡及追加・遅延返信の履歴再処理、世代付きcheckpoint、旧writerとの誤帰属防止、後継cycle拒否、gistの耐久planと元事実の再検証、派生記憶の有効期間・支持関係、graph遅延再投影の拒否、semantic historyのstate/collection分離です。
- D1〜D7は、無効BM25/graph候補の上限付き補充、hop数の整合、coreの時間境界と支持喪失時の補充、日本語短語の抜粋、condition/scope/時刻の独立保持、最終返却結果だけのrecall強化、再索引ID衝突時の旧index保持、返却時の有効期限確認です。filters-only SDKのepisode条件も保持します。
- E1〜E6は、両MCPとhost保存の共通完了判定、検証付き出典入力、一覧・削除の障害区別、共通helper期限、ID/details/pagination/fact history、明示的なvector/hybrid/smart検索です。

botの人格・システムプロンプト、認証・モデルは変更していません。抽出には `operation=assert|retract` と原文由来の `member_key` の説明を追加しました。rerankの既存prompt文・JSON契約は維持し、候補文字列の抜粋と構造情報の保持を変更しました。

## 独立レビューで補強した事項

担当間で差分を再レビューし、次の問題を追加再現して修正対象へ戻しました。

- primary削除前にowner付き削除意図を永続化します。prepare失敗時はprimaryを削除しません。削除結果が不明な場合はfresh readで不存在を確認し、存在時は取消、確認不能時はpendingを保持します。
- public deleteとbatch I/Oは取消時に実際のworker終了を待ちます。dependent lockはsource lockを抜けてから取得します。
- lineageの伝播・修復・legacy探索は上限と耐久巡回順を持ちます。未伝播の支持状態は上限付き祖先確認で検証し、予算超過を支持ありと扱いません。
- 新しいlifecycle DBは0600で作成し、symlinkを拒否します。削除前intentとgist planはSQLite commit後のdirectory fsyncも確認します。
- gist生成待ち中に元事実の有効期間・作者・内容が変わった場合は、古いsnapshotを現在有効な記憶として保存しません。
- 不正な抽出フィールド、空の競合分類、同batch内の独立confirmation eventsを専用回帰へ追加しました。
- Python 3.10を対象に残すため、helper期限は `asyncio.wait_for` を使用します。取消とraw抽出の契約は維持します。

## 設定と利用方法

| 設定 | 既定値 | 意味 |
| --- | ---: | --- |
| `MEM0_BATCH_RECOVERY_ENABLED` | true | botの回復workerを有効化します。 |
| `MEM0_BATCH_RECOVERY_INTERVAL_S` | 30 | 回復周期を秒で指定します。 |
| `MEM0_BATCH_MAX_AGE_S` | 600 | enqueueからの最大待ち時間です。0ではage flushを無効化します。 |
| `MEM0_BATCH_RECOVERY_MAX_BATCHES` | 2 | 1周期のbatch処理上限です。 |
| `MEM0_BATCH_RECOVERY_SCAN_FILES` | 100 | 1周期のqueue JSON読取上限です。 |
| `MEM0_BATCH_RETRY_BASE_S` / `MEM0_BATCH_RETRY_MAX_S` | 30 / 600 | background失敗の指数backoffです。 |
| `MEM0_HELPER_TIMEOUT_S` | 60 | 1回の補助LLM処理の期限です。0超〜600秒を受理します。 |

正常に進んだ長文continuationは障害backoffを増やさず、次の上限付き周期で継続します。未完了のbatchをACKしません。foregroundの「次の発言で再試行する」旧互換動作は保持します。各triggerで失敗は1回であり、backgroundが記録したbackoffはforegroundにも適用されます。すべての手動再送に待機を強制する変更ではありません。

長文のdirect呼出しで安定IDがない場合は、未保存の応答に `continuation_id` を返します。同じ入力・出典を、そのIDを `request_id` として再送します。同じIDで入力を変更すると拒否します。MCPはこの再試行方法を応答に含めます。

## 検証

| 最終検証 | 結果 | 証跡ファイル |
| --- | --- | --- |
| 基準版alpha回帰 | 286 passedです。 | `alpha-baseline.xml` |
| 確定sourceのalpha回帰 | 497 passedです。 | `alpha-source.xml` |
| 通常wheelのalpha回帰 | 497 passedです。 | `alpha-wheel.xml` |
| bot全体・default/legacy lane | 3,846 passed、480 skipped、既存のfork関連warning 2件です。 | `fullbot-final.xml` |
| 固定合成評価 | 基準・source・wheelとも32/32、回帰0件です。 | `synthetic-final-source.json`、`synthetic-final-wheel.json` |
| package整合性 | 37個のchecksumが一致し、wheel内の34 package filesがソースと同一です。 | `SNAPSHOT.sha256`、wheel本体 |
| 構文・静的検査 | package全33 Python moduleはPython 3.10文法検査を通過しました。変更moduleのRuff F/E9とdiff checkは通過しました。 | 下記の留意事項を参照します。 |

新規回帰は211件です。API59件、保存43件、lifecycle36件、回復35件、検索38件です。既存286件と合わせて497件となります。個別担当の途中成功数を重複加算していません。実行Pythonは3.13です。3.10については文法検査と互換API確認であり、3.10実環境で全依存を動かした試験ではありません。

Ruffの全域確認では、変更前から存在する `bot.py` の23件と、未変更 `morpho.py` のunused import 1件を確認しました。これらを修正済みとは報告しません。新規に発生したingestのunused import 2件は除去しました。全botの480 skipは既存条件とalpha専用laneの分離を含み、alphaは別途全497件を実行しています。

最初の全bot試行はcollection中の同時編集を既存guardが検出し、0件で停止しました。guardは変更せずsourceをfreezeして再実行しました。build用依存がruntime venvに存在しなかったため、監査dir内の専用build venvへ `setuptools==84.0.0`、`wheel==0.48.0`、`packaging==26.3` を導入しました。runtime/shared venvは変更していません。

既存の固定合成評価32ケースは基準版と候補版の両方が32/32であり、回帰はありません。この既存評価の改善率は0です。今回の改善は別途再現した欠陥に対する追加回帰で確認します。fake provider/LLM、架空入力、private SQLite/JSONを使用し、実LLMの意味理解、実embedding精度、本番遅延・費用を測定した結果ではありません。

証跡は `/home/ubuntu/mem0-quality-next-20260909-iJ9fJX` です。`retrieval-audit.md`、`ingestion-audit.md`、`lifecycle-audit.md`、`api-audit.md`、`host-audit.md` に基準再現を保存しています。`*-implementation.md`、`ingestion-results.md`、`*-independent-review.md` に担当実装と独立レビューを保存しています。欠陥の存在をassertする基準再現と、修正後に通過する受入回帰は区別します。

既存テストの変更は契約を強めるためのものです。旧MCPの未知dict/非dictを成功とする2件を未完了assertへ変更しました。public delete取消の2件は実worker完了までtaskが終了しないことを追加しました。旧receipt fsync失敗の1件は、新しいprepare障壁より後のreceipt障壁へ失敗注入を限定し、従来のtombstone確認を維持しました。失敗を隠すskip追加は行っていません。

## 新しい永続状態と配備条件

- `mem0_quality_journal.sqlite3` は抽出進捗、cached signals、結果、confirmation event ledgerを保持します。`extraction_job_locks/` を使用します。
- `mem0_lifecycle.sqlite` はconsolidation plan、lineage、削除意図、探索cursor、伝播queue、修復順序を保持します。`consolidation_locks/` を使用します。
- 既存history SQLiteには変更journal、fingerprint/revision、generation、checkpointを追加します。batch JSONにはenqueue時刻・channel名・backoffを追加します。
- 配備時は新DBのWAL/SHMとlockを含めてbackup/診断範囲を確認します。稼働中SQLiteを単にコピーして整合backupと呼びません。
- packageは34個のPython/package-dataファイルです。`SNAPSHOT.sha256` を更新し、通常wheelとしてprivate検証しました。共有venvへinstallしていません。
- 古いコードは新journal・pending jobを理解しません。旧版のプロセスをそのまま再開するだけのrollbackは安全と扱いません。writer停止、整合backup、pending状態の確認、互換性監査が必要です。

検証済みwheelは監査dirの `wheels/mem0_alpha-0.3.2+botquality.2-py3-none-any.whl` です。SHA-256は `c2a2cb3437268c4c4d140bf99647c1a778c041782de17fc346d483234679258c` です。manifest自体のSHA-256は `5841783535f5137df9f8d2dfc92cbebd5cd87b5fddaa21d06af9c85121fbcb3e` です。import originは同監査dirの `wheel-install/llm_mem0/__init__.py`、installed versionは `0.3.2+botquality.2` でした。editable installは使用していません。

git commit/pushと本番反映は未実施です。配備時は今回の新規ファイルを含めた確定commit、各botの実効import origin、新stateのbackup範囲を確認します。

## 残余制約

- rerank全候補の合計入力budgetは未追加です。各候補の上限はありますが、基準の内部200候補合成入力は191,161文字でした。共通deadlineだけで総入力を制限したとは扱いません。
- 取消したprovider読取threadを強制停止する機能はありません。書込workerのdrainとは別です。共有executor上限はありますが、取消直後の別検索と実threadが重なる場合があります。
- queueの `max_files` はJSON読取数の上限です。directory内のファイル名列挙とsortは全件です。
- legacy lineage探索はSDKにoffsetがないためprefix取得です。1ページ最大250、新規探索は1回最大4 source jobs、prefix最大10,001行です。10,000行上限を越える場合はpendingで残し、別の運用確認を要求します。並行した一覧変更に対するsnapshot paginationは保証しません。
- legacyの由来不明作者、旧semantic vector namespace、旧FTS tokenization、過去に128件窓から消えたconfirmation eventは推測復元しません。承認された再索引・移行は別工程です。
- journal/ledgerを無期限の再送保証と両立したまま自動削除するretention方針は未実装です。容量管理は別の明示操作が必要です。
- 非hashableな `tier` を含む壊れたprovider入力によるformatter障害は監査R12として残っています。通常のscalar metadataとは異なる入力です。
- 短い抜粋、literal memberに基づく取消、独立/必須supportは限定契約です。任意の言い換え・代名詞・皮肉や派生知識の意味を完全に理解する保証はありません。
- providerとSQLite群を跨ぐ分散transaction、非協調旧writer、完全bitemporalは実装していません。明示的な時点検証とpending修復を、それらの代替保証とは扱いません。
