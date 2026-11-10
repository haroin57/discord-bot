# Mem0 alpha 第2段階改善の実装計画

作成日は2026-09-09です。基準は本番反映済みのcommit `b42b908ce681b21ee02e37a56620c5780e042fc0`、`mem0-alpha==0.3.1+botquality.1` です。この計画を作成した時点では実装ソースを変更していません。今回の候補版は `0.3.2+botquality.2` とします。

## 目的と境界

検索の取りこぼし、長期記憶の更新・再試行、履歴と出典、MCPからの利用可能性を改善します。前回実装済みのowner別保存、lease/ack、receipt、hydrated検索、時点検索を土台にします。既実装の再説明を追加機能として数えません。

本番サービス・認証・モデル・共有venv・dirtyなupstream checkoutは操作しません。本番再配備、既存記憶の一括再索引、collection改名、過去データの推測帰属・削除はこの工程に含めません。同サーバー内の認可された会話共有、adminだけの自動記録、botの人格・システムプロンプトは維持します。B4の抽出schemaには個別対象の取消を明示する説明を追加します。D4のrerank prompt文は変更しません。

## 全体走査

packageの全31ファイルを、検索、保存、履歴・ライフサイクル、API・認証設定に分けて走査しました。ホスト側のrecord/outbox、session履歴、context/preprocess/prompt、起動・停止、alpha bridge、MCPも確認しました。Herdrの既存レビュー用ペインで検索担当を実行し、他の領域を並行監査しました。

- 検索担当は `search`、`bm25_index`、`morpho`、`priming`、`format` とgraph検索呼出しを扱います。
- 保存担当は `ingest`、`extract`、`dedup`、`conflict`、`reinforce`、`client`、`attribute_registry`、`sanitize`、receipt/SQLite/domain lockを扱います。
- ライフサイクル担当は `history_index`、`history_embed`、`replay`、`graph`、`decay`、`fact_store` と削除・派生元の整合性を扱います。
- API担当は両MCP、`llm`、`settings`、`__init__`、`atomic_write`、auth全体、host alpha bridgeを扱います。認証変更は行いません。
- 主担当はdurable `batch`、bot記録・回復worker、起動・停止との接続、評価・統合を扱います。

監査証跡と欠陥再現は `/home/ubuntu/mem0-quality-next-20260909-iJ9fJX` に保存します。private temporary SQLite/JSON、実ソース、fake providerを使用し、実会話・認証・LLM・Chromaを使用しません。

## 実装項目と受入条件

### A. 保存の再開と待ち時間

- A1は、起動後・一時障害後のdurable batchを、新しい同ownerの発言なしで再開するworkerです。現在の許可済みownerだけを対象にし、未知ownerの旧入力は保持します。
- A2は、閾値未満の会話も設定した最大待ち時間でflushする機能です。enqueue時刻と発言時刻を区別します。owner/channel間の公平性、1回の処理数、周期、無効化設定を設けます。
- A3は、耐久backoffと本文を含まないqueue診断です。失敗を即時loop再実行せず、取消時もinput/leaseを回復可能に保持します。既存のclaim/renew/ack契約を再利用します。
- A4は、個別turnの元ID・作者・発言日時をホスト境界で失わないprovenanceです。batch IDとsource event IDを区別し、assistant文を独立したuser factへ昇格させません。
- 受入試験は、再起動回復、少量queueのage flush、許可されないownerの不処理、未知legacy保持、同時claim、provider失敗・backoff・回復、重複起動、取消、処理数上限です。

### B. 長文・訂正・重複処理

- B1は、300文字の同一prefixだけを根拠に異なる長い事実を同一視する経路の修正です。意味の異なる末尾を比較から消しません。上限は明示的に扱います。
- B2は、既存属性のlookup失敗を「競合なし」と扱って成功ACKする経路の修正です。検証できない場合は保留・再試行へ返します。
- B3は、128件のmetadata窓を越えた同一source eventの再送でmention数が再加算される問題の修正です。耐久receiptを使用し、別eventの再確認は保持します。
- B4は、set-like属性の明示的な取消・否定を、肯定と無条件に並存させる問題の修正です。作者・属性・scope・条件が一致する根拠だけを対象にし、曖昧な否定から旧事実を推測削除しません。
- B5は、抽出予算を超える入力が毎回同じ先頭chunkだけを再処理する問題の修正です。chunk予算を無効化せず、安定IDとinput hashに対応する進捗・抽出結果を保全し、確認済みのchunkの後続から再開できる構成を優先します。単一長文と複数turnの両方を扱います。
- B6は、同じ属性の連続訂正や部分archive後の再送が永続partialになる経路の修正です。現在の後継関係を再検証し、保存済みの新事実を失わせません。
- B7は、LLMの未知キーと内部制御キーを抽出schemaから分離する修正です。未知の `_predecessors` 等により正常な兄弟factを失う問題を再現済みです。外部生成値を内部の削除・receipt指示として採用しません。
- 受入試験は、長文末尾の数値/住所変更、lookup timeout、130event後の再送、明示取消と無関係な否定、処理予算をまたぐ後続chunk、保存成功後timeout、訂正鎖の再送です。

### C. 履歴・派生記憶・ライフサイクル

- C1は、source編集と過去時刻の追加を旧cursorが永久に見落とす問題の修正です。発言時刻ではなく変更の世代も追跡し、安定source IDで処理します。
- C2は、同時発言が挟まった場合も対応source IDでuser/assistantを組にする修正です。別人のassistant応答を推測結合しません。
- C3は、再索引中のreplayが旧世代cursorを新世代へcommitする競合の修正です。世代付きcheckpointで古いcommitを拒否します。
- C4は、旧writerが再利用したFTS row IDと古い作者metadataの誤結合の防止です。row内容のfingerprint等で検証し、一致しない作者を推測しません。既存DBは破壊しません。
- C5は、2段以上のsupersession cycleの拒否です。並行更新でも循環をcommitしません。
- C6は、gist生成待ち中に再びLiveになった元記憶をarchiveする競合の修正です。処分直前に元事実の状態を再確認します。
- C7は、gistの部分archiveによって残りが件数閾値を下回っても再試行できる修復経路です。
- C8は、未来の元事実から現在有効なschemaを生成する経路の修正です。派生記憶の有効期間は根拠と整合させます。
- C9は、元事実の削除で根拠を失った派生記憶を引き続き有効扱いする経路の修正です。複数根拠が残る派生記憶は保持します。
- C10は、削除・失効済みsourceの遅延graph projectionがedgeを復活させる経路の修正です。
- C11は、semantic historyのstable IDとfilterへstate/collectionのnamespaceを反映する修正です。existing collection改名や旧rowの一括削除は行わず、由来が不明なlegacy rowの扱いを明記します。
- 11項目は実SQLiteとfake providerによる再現を作成済みです。各再現を修正後のacceptance regressionへ移します。

### D. 検索の正確さ

- D1は、BM25候補が失効・削除・scope検証で落ちた後、後続の有効候補が補充されない問題の修正です。取得予算を上限付きに保ちます。
- D2は、graphのhop設定と実探索の1段差を解消する修正です。循環・作者・scope・候補件数の上限を保持します。
- D3は、未来開始のcore記憶が有効になってもTTL中に出現しない問題の修正です。時間境界でcacheを適切に無効化し、他scopeへ伝播させません。
- D4は、日本語の短語を含む長文抜粋と、condition・scope・時刻の独立した入力予算です。既存prompt文・model・最終formatterの表示契約は変更しません。
- D5は、最終返却結果だけをrecall強化する修正です。core優先mergeで落ちた候補は強化せず、core/bundleの除外は維持します。
- D6は、再索引入力の同一IDを一意化する修正です。同一内容はまとめ、異なる本文・ownerのID衝突では旧indexを維持して失敗を返します。本番reindexは実行しません。
- D7は、独立レビューで再確認した、rerank待機中の有効期限越境に対する最終返却時の時刻検証です。明示as_ofは指定時刻を維持します。D3/C9も補強し、cached coreから支持を失った行が落ちた場合は再取得して次点を補充します。任意provider更新との分散transactionは保証しません。
- 検索の追加再現16件を監査記録に確定しました。graph siblingの上限付き補充とfilters-only providerのepisode条件保持も補助修正へ含めます。非hashable tierの防御、返却時刻snapshot、rerank合計入力budget、取消後の実thread寿命は別項目として結果に実施範囲を記録します。共通deadline E4と競合する独自timeoutは追加しません。
- 受入試験は、有効候補が先頭windowの外にあるケース、hop 1/2/3・cycle、valid_from/valid_to境界、混在owner/scope、reranker空結果と取消です。

### E. MCPとhelperの機能露出

- E1は、両MCP addのok/legacy成功/partial/error/unknown判定の統一です。未完了を「記憶済み」と報告しません。
- E2は、optionalなrequest ID、source ID/作者/時刻、channel、有効期間を型・件数・長さ付きで受理する機能です。provenanceを認可主体の変更には使いません。
- E3は、host list/deleteの障害と不存在・権限違反を分離する修正です。既存strict APIと現在のowner確認を再利用します。
- E4は、model/authを変えないhelper deadlineです。raw completionも同じ制限を通し、caller cancellationを維持します。providerの永続commitを放棄するtimeoutには使用しません。
- E5は、host listのID/details/上限付きpaginationとfact historyの露出です。出力サイズを制限し、系譜不明と障害を区別します。
- E6は、vector/hybrid/smartの明示検索modeです。未指定時の既存smart/default挙動は保持し、hybridは追加LLMなしで呼び出せます。
- 受入試験は、両tool schema/実呼出しmetadataの一致、partial/unknown等のmatrix、invalid time/NaN/反転区間、outage、ID表示、ページ境界、helper timeout/取消、hybridのLLM呼出し0件です。

## 所有権と実装順序

1. 主担当がこの計画を記録し、担当境界と共通helper/APIを確定します。
2. 検索、保存、履歴、APIを独立ファイルで並行実装します。`batch.py`、`settings.py`、bot worker統合、package版・checksumは主担当が所有します。`extract.py`は保存担当、`llm.py`はAPI担当で共通raw completion契約を連絡します。
3. 各担当の新規回帰を先に確認し、全alpha lane・全bot laneで統合します。テスト失敗を隠すskip追加やassertion緩和は行いません。
4. 通常wheelをprivateディレクトリに構築し、source/hash/import originの一致を確認して同じ回帰を実行します。shared venvへinstallしません。
5. 合成評価を基準版と比較し、新ケースの改善と回帰を記録します。実LLMの意味理解・本番精度・費用を未測定のまま改善率として報告しません。
6. 最終差分・残余制約・移行と復旧条件を結果文書に記録します。本番反映は別工程です。

## 評価設計の参考

[LongMemEval](https://arxiv.org/abs/2410.10813) の抽出・複数session・時刻・知識更新・無回答という評価区分を、ケースの抜けを確認する参考にします。[Mem0論文](https://arxiv.org/abs/2504.19413) の抽出・統合・検索の分離も参照します。今回のテストは独自の合成欠陥回帰であり、これら公開benchmarkを実行した結果ではありません。
