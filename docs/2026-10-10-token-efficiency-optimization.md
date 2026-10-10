# トークン効率最適化の対照実験（進行中）

## 現在の状態

2026-10-10 23時台 JST に実行環境を修復し、実API検証と最適化を再開しました。Codex sandbox 内で認証付きAPIのHTTP 200と本番journalの読み取りを確認しました。同時query共有は実API対照60件、無待機pilot15件に続き、待機なしの反復実験180件で検証しました。同時重複の入力は50%減、group待ち時間の中央値は6.4%短縮しました。しかし、ユーザーの本番テスト5応答・検索4回では重複共有0件で、この機能による削減は発生していません。ユーザーのテスト終了を確認し、2026-10-11 00:09:43 JSTに追加query共有だけを従来実装へ戻しました。履歴方式も追加18件で全入力増加と品質不通過が残ったため採用していません。実装・測定資料は `agent/token-efficiency-20261010` にpush済みです。本番全体の前後削減率は引き続き未証明です。最新の証拠・本番状態・採否は末尾の追記を参照してください。

以下、末尾の再開結果までの記述は修復前の記録です。当時の実API合成対照実験は計60リクエストでした。内訳は初回のキャッシュ比較18件、記憶クエリ比較24件、複数ラウンド履歴比較18件です。未回収だった後半42件の回収・再集計では新しいAPIリクエストを送っていません。

2026-10-10 15:16 JSTに、直近5分間の新規agent-loopログがないことを確認してサービスを再起動しました。その時点のPIDは381858でした。有効mount namespaceのrequest_shape.pyとcli_compat.pyのSHA-256が変更済み作業ツリーと一致していました。その時点でDiscord/OAuth/Mem0/systemは正常でした。release_metadata_path_unsetによる既存のrelease check不成立は変わっていません。これらは再集計時点の稼働状況を保証する記録ではありません。

15:00以降の追加実測はOpus 5.5の2リクエストで、入力234,191（cache読み出し147,993、cache作成86,192、通常入力6）、出力2,928 tokensでした。これは再起動前の会話であり、最適化後の効果として計上しません。再起動後のhelper実測はまだありません。

## 実験設計

`scripts/experiment_token_efficiency.py` は公開用の合成会話だけを生成し、Opus 5.5、固定の12ツール、最終回答の厳密JSON判定で比較します。各条件に専用の会話markerを与えています。モデルの自動リトライは0、リクエストごとの出力上限は1,024 tokensです。変更前に8リクエスト、順序固定化後に10リクエストを実施しました。

初回に12往復の履歴を送信し、次回は以下の条件を比較しました。

- rollingは先頭1往復を落とし、末尾に1往復を追加します。
- appendは既存prefixを維持して末尾に1往復を追加します。
- tool_orderはappendの履歴でツールの広告順序を逆転します。負の対照として、本番の順序固定化を意図的に迂回します。
- canonical_orderは逆順の入力を本番ビルダーで正規化してから送信します。
- effort_changeはappendの履歴でlowからmediumへ変更します。

この実験はキャッシュ機構と短い事実参照の検証です。複雑な調査・画像・署名付きthinkingの継続・個人記憶の品質を網羅していません。system/toolsの一部は他条件と共有されるため、初回の完全なcold cacheは条件ごとに保証していません。次回リクエストの履歴キャッシュ維持を主要評価としています。

## 実測結果

数値はすべて2回目リクエストのprovider usageです。

| 条件 | 変更前cache作成／読み出し | 固定化後cache作成／読み出し | 厳密JSON品質 |
|---|---:|---:|---|
| rolling | 4,989／1,528 | 4,956／1,528 | 両実験で通過しました。 |
| append | 70／6,863 | 70／6,887 | 両実験で通過しました。 |
| tool_order | 7,005／0 | 5,453／1,528 | 再実験の負の対照1件が不通過でした。 |
| canonical_order | 未実施です。 | 70／6,935 | 通過しました。 |
| effort_change | 70／6,923 | 70／6,971 | 両実験で通過しました。 |

追加のcache作成だけを比較すると、rollingに対するappendは約98.6%減、順序変更に対するcanonical_orderは約98.7%減です。これは合成履歴の次回リクエストに限定した数値です。入力コンテキスト全体や本番請求額が同率で減るという意味ではありません。

18件の実験消費は通常入力72、cache読み出し49,859、cache作成74,607、出力471 tokensでした。品質は17/18件が通過しました。不通過は負の対照であり、最適化済みcanonical_orderの不通過ではありません。当時のharnessは失敗時の本文を保存していないため、JSON不正・余計なキー・値違いのどれかを後から分離できません。更新後のharnessには失敗分類を追加しましたが、過去の失敗を成功に再分類していません。

Opus 5.5の今回の経路ではlow→medium変更でも履歴キャッシュが維持されました。したがって、effort変更が必ずキャッシュを失わせると断定しません。モデル・経路・thinking設定ごとの再検証が必要です。公式資料もキャッシュ診断とthinking設定の検証を説明しています。[Cache diagnostics](https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics)、[Thinking troubleshooting](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting)。

生の測定数値は `docs/2026-10-10-token-efficiency-experiments.json` に保存しています。

## 回収した追加実験

### 記憶クエリの統合

公開用の合成クエリ6件に対して、従来の3段階展開と1回の統合展開を比較しました。固有名詞、数値、否定、別名、複数事実を含むケースです。

| 条件 | providerリクエスト数 | 通常入力 | 出力 | クエリ単位の所要時間中央値 | 補強後の品質判定 |
|---|---:|---:|---:|---:|---|
| parallel | 18 | 11,277 | 770 | 1,606.5 ms | 6/6件が通過しました。 |
| bundle | 6 | 3,371 | 1,402 | 5,508 ms | 5/6件が通過しました。 |

統合方式の入力は70.1％減、入力と出力の合計は60.4％減でした。一方、出力と所要時間は増えています。従来方式はSonnetとHaikuを併用し、統合方式はSonnetだけを使います。モデル別単価を適用していないため、請求額の改善とは扱いません。

元の必要語・語彙検索判定では両方式とも6/6件が通過していました。しかし統合方式のRust/asyncioケースでは、クエリにないDropとCancelledErrorの詳細を過去に議論した内容として生成していました。事後の対象限定チェックでこの1件を不通過にしました。このチェックは一般的な意味精度や実際の個人記憶のrecall精度を保証しません。品質と待ち時間の条件を満たさないため、統合方式は本番へ反映していません。

保存結果は `docs/2026-10-10-memory-query-experiment.json` です。保存済みproviderリクエストは24件です。クエリ単位の観測12件と混同していません。

### 複数ラウンドの履歴比較

従来のrolling方式とanchorを維持する追記方式を各9ラウンド、Opus 5.5で比較しました。全18件のusageが保存されています。

| 条件 | 通常入力 | cache読み出し | cache作成 | 全入力 | 出力 | 厳密JSON品質 | 拒否 |
|---|---:|---:|---:|---:|---:|---|---:|
| rolling | 36 | 0 | 37,116 | 37,152 | 250 | 5/9件が通過しました。 | 3 |
| anchored | 36 | 29,363 | 12,493 | 41,892 | 362 | 6/9件が通過しました。 | 0 |

追記方式はcache作成が66.3％減る一方、cache読み出しを含む全入力は12.8％増えました。先頭切り替えは8回から2回になりました。cache作成だけを減らした結果を、総トークン削減と扱いません。両方式とも品質不通過が残るため、本番採用を保留しています。

当時の実験には、rollingを全件実行した後にanchoredを実行する順序の偏りと、追加履歴だけが短くなる密度の偏りがありました。harnessを各ラウンドの交互実行へ変更し、初期履歴と追加履歴を同じ密度の合成テンプレートに揃えました。更新後の実API再実験は未実施です。当時の失敗本文は保存されていないため、JSON形式の失敗だけだったかを事後に断定していません。

保存結果は `docs/2026-10-10-replay-window-experiment.json` です。再集計は `scripts/analyze_token_efficiency_experiments.py` で再現でき、入力ファイルのSHA-256とモデル別usageを `docs/2026-10-10-token-efficiency-followup-summary.json` に保存しています。

## 同時重複共有の対照実験（オフライン）

`scripts/experiment_query_singleflight.py` は保存済みのparallel方式の応答を使用し、実際の共有実装と検索語展開のコードを通して呼び出し数を比較します。6つの合成クエリについて6条件を比較し、条件ごとにbaselineとsingleflightを実行しました。条件・クエリ・arm単位の観測は72件です。新規providerリクエストは0件です。独立した元の品質サンプルは6クエリ、18補助リクエストのままであり、再利用した応答を新しい品質サンプルと数えません。

| 条件 | 1ケース当たりのbaseline模擬呼び出し | singleflight模擬呼び出し |
|---|---:|---:|
| 1件だけの検索 | 3 | 3 |
| 同一user・同一queryを同時に2件 | 6 | 3 |
| 同一user・同一queryを同時に4件 | 12 | 3 |
| 異なるqueryを同時に4件 | 12 | 12 |
| 異なるuserを同時に4件 | 12 | 12 |
| 同一queryを逐次4件 | 12 | 12 |

両armでそれぞれ114件のconsumer応答が保存済みの検索表現と一致しました。共有対象以外の条件では呼び出し数が変わらず、完了後の応答を保持していないことも確認しました。これは模擬provider dispatch数と保存応答の保持を検証する実験です。新しい実モデル応答の品質、vector/graph検索のrecall、本番の同時重複率を計測した結果ではありません。

元のusageを呼び出し数に応じて加算した値は `counter_replay_not_live_usage` として分離しています。この値を現在の消費や請求額の削減と扱いません。結果は `docs/2026-10-10-query-singleflight-controls.json` に保存し、入力データ・実装・実験harnessのSHA-256を記録しています。テストから再実行した結果と保存artifactの一致も検査します。

## 実装した変更

### 補助LLMの計測

`services/token_usage.py` と `services/mem0/cli_compat.py` に成功レスポンスのusage4項目、model、taskのハッシュ、duration、文字数、stop_reasonを記録する経路を追加しました。本文・認証情報は記録しません。usageがない場合は `-` とし、0消費と扱いません。集計スクリプトはhelper使用量をagent-loopとは別に集計します。

対象は共通CLI helperを通るMem0、planner、judge、distillです。追加実装ではMem0のOpenAI互換fallbackのusageも別に記録します。prompt/completion/totalの報告値と、任意のcached/reasoningの内訳をそのまま保持し、内訳を総量へ二重加算しません。fallbackが実際に送信するモデル名を記録します。コード追加後の本番反映・計測は未実施です。

CLIと互換fallbackの失敗・キャンセル、clientを選べない場合も本文を含まない診断レコードを追加しました。失敗レコードを0消費として扱いません。不完全なusageは既知のcounterと不明項目を分けて保持します。成功応答のIDがあれば重複ログを除外し、IDがない場合は同じtaskという理由だけで統合しません。互換prompt総量、reasoningの内訳、Anthropic形式のcache4項目は異なる集計に分離します。

Mem0内部LLMの独自呼び出し、SDKの内部再試行、途中で切断してusageを取得できないリクエストの実消費はまだ全面計測できません。現時点で総使用量を完全計測したとは主張しません。

### ツールの広告順序固定化

`services/claude_api/request_shape.py` はツール名でソートしたコピーを送信します。入力リスト、個々のschema、ツール集合、権限を変更しません。順序だけが異なる同じ集合のcache prefixを安定化します。server集合自体の変更やschema変更によるmissは防止しません。

### 上限付き追記履歴の候補

`services/stable_replay.py` はプロセス内anchorから追記型の窓を維持し、余裕枠を超えた時だけまとめて先頭を切り替える候補実装です。最小保持行数は従来値を維持し、追加量を上限管理します。期限切れ・削除・履歴clearによってanchorが見つからない場合は従来のtailへ戻します。元の構造化行は変更しません。チャンネル数にはLRU上限があります。

追加prefixには行数上限に加えて100,000 bytesの上限を設けました。構造化行をUTF-8 JSONとして数え、超過時は従来のtailへ戻します。従来の最小保持部分を切り詰めません。この上限はモデルのトークン数そのものではなく、会話全体のコンテキスト上限を保証しません。最適な追加予算も未確定です。

作業ツリーの `services/session.py` に `CLAUDE_API_REPLAY_EXTRA_ROWS` のopt-in経路を追加しています。既定値0では従来の厳密なtail上限を維持します。稼働サービスのsession.pyは別のbind mountであり、この履歴候補は本番未適用です。作業ツリー全体を上書きして既存overlay固有のauthor/source識別や品質修正を失わせないことが条件です。

`scripts/build_token_efficiency_payload.py` は有効baselineの対象関数だけを差し替え、他の関数・クラスのASTが変わらないことを検査します。payload生成と検証は実施済みですが、候補のデプロイとrollbackの実運用確認は未実施です。

### 同時query共有の候補

`services/query_expansion.py` に、同じuser・完全一致するquery・同じモデル設定とpromptの場合だけ進行中の補助処理を共有する実装を追加しました。作業ツリーの従来Mem0検索と、deploy用Mem0α検索に接続しています。`MEM0_QUERY_SINGLEFLIGHT_ENABLED=1` で有効にします。既定値は0です。本番の検索overlayには未反映です。

共有するのはrewrite・HyDE・keywordの検索表現だけです。graph、記憶DB検索、rerank、権限判定、ファイル操作は共有しません。各consumerには独立したvariantsのリストを返します。1件のキャンセルは他consumerに伝播させず、最後のconsumerがキャンセルされた場合は補助処理もキャンセルして回収します。失敗も完了応答も後続検索には保持しません。終了理由を返さない現在のhelper JSON経路で、切れた応答を成功キャッシュとして保持する処理は追加していません。

イベントループごとの進行中キーは最大64件、1キーのconsumerは最大16件、queryはUTF-8で16,000 bytes以下です。上限超過や不完全なキーは従来の独立処理へ戻します。モデル・出力上限・HyDE設定・prompt・展開modeの変更は別のキーとして扱います。

`mem0_query_singleflight` ログはleader、joined、bypassと上限理由を記録します。query・user ID・prompt本文は記録しません。監査スクリプトもこの判断をprovider usageと別に集計し、joined数から消費削減量を推定しません。

### 同時共有の局所payload

`scripts/build_query_singleflight_payload.py` は、サービス構成が参照するperformance baselineを読み、既存の `_expand_queries` の2文だけを差し替えます。共有用の2関数・instance・依存importを追加し、モジュール全体のASTがこの許可差分と一致することを検査します。既存のモデル・prompt・ランキング・削除条件・他の関数やclassは変更しません。layoutや既存symbolが対応条件に合わない場合は生成を拒否します。品質条件を満たさなかったbundle方式は追加しません。

baselineに `os` importがないことを実際の全モジュール試験で検出し、必要なimportだけを追加するように修正しました。既存の異なる `os` bindingを上書きしません。生成済みpayloadは `/tmp/query-singleflight-payload/search.py` です。tmp削除後はmanifest記載のコマンドで再生成します。

baselineのSHA-256は `eacbe15dcb5a84c95feeaed50b4cbcf6f734e90125f3ae76725d1124b8e3f6e5`、payloadは `8de8e0b078f51b680b0a8609d1f243374bff55ee0304b33c3d1223057c8ba157` です。`docs/2026-10-10-query-singleflight-payload-manifest.json` に依存ファイル、再生成・対照コマンド、未デプロイ状態を保存しました。

`scripts/experiment_query_singleflight.py --implementation /tmp/query-singleflight-payload/search.py` で生成payloadの全モジュールを読み、同じ6条件を再実行しました。全条件の結果は作業ツリー候補と一致しました。結果は `docs/2026-10-10-query-singleflight-overlay-controls.json` です。provider呼び出しは模擬であり、ここでも新規モデルAPIリクエストは0件です。

本番反映には、実際のmount元がmanifestのbaselineと一致すること、処理中ジョブがないこと、baselineの退避、依存モジュールの配置を確認する必要があります。採用条件を満たした後にoverlayを反映し、`MEM0_QUERY_SINGLEFLIGHT_ENABLED=1` でサービスを再起動して計測します。戻す場合はflagを0にし、必要に応じて退避したbaselineへ復元して再起動します。このデプロイ・rollbackは実施していません。

### 品質判定と計測の補強

`scripts/token_experiment_quality.py` は厳密JSON判定を維持し、拒否、出力切れ、JSON不正、コードフェンス、schema違反、値違いを分離します。形式不正でも内容が正しい場合の判定は別項目にし、厳密判定を通過したとは扱いません。応答本文は保存せず、文字数とSHA-256を記録します。

記憶クエリharnessは開始したHTTPリクエスト数をカウントし、同時実行や失敗時も試行上限を超えないようにしました。再集計は不明なusageを完全計測済みと扱わず、空の観測から0消費や品質通過を断定しません。

## 検証

- helper計測・集計・Opus 5.5リクエスト等の35件が通過しました。
- 主API・熟考パイプラインの91件が通過しました。
- tool-loop・予算・ツール・session-storeの192件が通過しました。
- 追記履歴候補・既存session・従来tail上限の84件が通過しました。
- 最新の追記履歴・helper計測・集計・Opus 5.5リクエストの22件が通過しました。
- 回収結果の再集計、品質判定、追記容量上限、payload、usage集計、空の計測を成功扱いしない回帰テストの45件が通過しました。
- discord-bot内のサブエージェントのファイル権限・既存Agent・capabilityの32件が通過しました。ファイルI/Oテストは実ファイルを使い、実行環境のsocket制限によるthread wakeup停止を避けるためthread dispatcherだけを差し替えています。
- 同時共有・対照harness・usage監査の40件、Mem0αの関連11件、既存のrewrite/HyDE/keywordの15件が通過しました。同時共有ではuser・query・設定の分離、キャンセル、失敗、容量、ループ分離、graphの鮮度、完了応答を保持しない条件を検証しています。
- 局所payload、共有処理、対照harness、互換usage、失敗・キャンセル、既存usage監査の最新合同試験75件が通過しました。payloadの全AST保全、無効時のbaseline一致、graphの鮮度、cancel回収、追加import、既存symbolの非上書きも検査しています。
- Ruffと `git diff --check` が通過しました。

上記は別々の実行群です。新テストを追加した前後の群もあり、重複を除いたテスト総数を表すものではありません。

既存のthread I/Oを含むMem0試験はworker待ちで停止したため中断しました。同時共有の関連テストではproviderを模擬し、graphの同期dispatcherも差し替えています。これらをMem0全体の実環境試験が通過した証拠とは扱いません。

## 実行環境による未検証範囲（修復前の記録）

現在の実行環境ではsystem journalの他ユーザー・サービスのログを参照できません。サービスを指定した `journalctl` が空でも、本番消費が0である根拠にはなりません。ネットワークと外部ディレクトリへの書き込みも制限されています。この再集計では本番サービスを変更・再起動していません。

追加の接続確認では、認証情報を送らない `https://api.anthropic.com` へのHEAD試行が `Temporary failure in name resolution` で失敗しました。新しいモデルAPI試験は実施していません。読み取り可能なperformance baselineでは検索語展開が補助3処理を直接開始しており、同時共有の呼び出し経路はまだ存在しません。局所payloadの生成・オフライン検証は完了しましたが、実際のoverlay更新と本番検証は未実施です。

system journalの閲覧制限も再確認しました。`runtime_state.json` はsessionのtimestamps/stickyを保存するだけで、`sessions.json` にもusageは保存されません。`SessionInfo.last_usage` は意図的にruntime-onlyであり、読み取り可能な永続sessionファイルから本番の追加使用量を復元することはできません。ログが見えない状態を0消費と解釈しません。

サブエージェントのファイル操作はdiscord-botのソースへ実装済みです。親が許可するRead/Write/Edit/Glob/Grepだけを継承し、親のdeny/ask規則を維持します。restrictedな親には付与しません。作業ツリーのentry.pyでは親ToolContextとpermission gateを子へ渡しますが、サービス構成はentry.pyを別のoverlayからマウントします。この呼び出し箇所のoverlay更新とサービス再起動は未実施です。本セッション自体の権限変更ではありません。

## 次に必要な検証

1. 本番helperログから段階別の消費、出力切れ、再試行の実測を収集します。taskハッシュから固定promptを対応付けます。
2. 生成済みの追記履歴payloadを、採用条件を満たした場合に限り有効overlayへ反映し、rollbackを確認します。署名付きthinking、tool対、要約、再起動、年齢制限、プライバシー削除、急増コンテキストの対照を増やします。
3. 密度と順序を補正したharnessで、追加cache-read負担、全体token budget、失敗分類を含む複数ラウンド比較を行います。70 tokensの単発結果だけで長期最適と判断しません。
4. Mem0の統合方式は今回の品質・待ち時間では採用しません。生成済みの同時共有payloadについて、実APIでの共有・非共有のusageと本番の重複率を測定します。段階的追加や逐次queryの保持は、終了状態・鮮度・プライバシーを含めて検証します。固有名詞・数値・否定・複数事実のrecall品質を検証する前に管理者のrecallを無条件で止めません。
5. 本番前後の同じモデル・同等の依頼で、helper・main・検証・要約を含む使用量、成功率、recall精度、待ち時間を比較して採否を確定します。

## 完了条件の再監査（修復前の記録）

目標は「再度詳細に測定し、さまざまな対照実験を経てトークン利用効率を最適化する」です。ソース追加と模擬試験だけでは完了を証明できません。前節の5項目を現在の証拠へ対応付けた結果は次のとおりです。

| 項目 | 現在の証拠 | 未完了の理由 |
| --- | --- | --- |
| 1. 本番の段階別使用量を測定します。 | helper、互換fallback、失敗・キャンセル、不完全usageの計測と監査コードがあります。 | system journalの閲覧が拒否され、本番の段階別消費・出力切れ・再試行を回収できません。永続sessionにもusageはありません。 |
| 2. 品質・安全性を保った履歴候補の採否とrollbackを確認します。 | 局所payloadと従来経路の回帰試験があります。既存の実API比較は両armに品質不通過があります。 | 採用条件は成立していません。署名付きthinking、tool対、要約、再起動、年齢制限、プライバシー削除、急増履歴を含む候補の実環境比較とデプロイ・rollbackは未確認です。 |
| 3. 密度・順序を補正した複数ラウンド比較を実施します。 | 過去の18リクエストではcache作成が66.3％減る一方、全入力が12.8％増えました。補正後のharnessがあります。 | 補正後の実API再実験は未実施です。単発cache作成の削減を全体効率の改善とは扱いません。 |
| 4. 記憶検索の重複処理を品質と鮮度を保って削減します。 | 統合方式は品質・待ち時間で不採用です。同時共有は6条件のオフライン対照で正確な一致と分離を確認しています。 | 保存応答の再利用は新規の品質標本ではありません。本番の重複頻度、実APIのusage、実際のrecall精度は未測定です。 |
| 5. 本番前後の全経路の効率を比較して採否を確定します。 | 実APIの合成比較は計60リクエストあり、再集計可能な保存結果があります。 | 同じモデル・同等の依頼での本番前後のmain・helper・検証・要約の総量、成功率、recall精度、待ち時間の比較はありません。本番全体の削減率は未証明です。 |

今回の再確認では、保存済みの追加実験集計が現行analyzerの出力と完全一致しました。同時共有payloadをbaselineから再生成し、baseline・payload・依存モジュールのhashとmanifestの一致、および2つの対照artifactの全条件一致を確認しました。関連75件が通過しました。これらの確認で新規providerリクエストは送っていません。

認証情報を送らないAnthropicへのHEAD確認は、引き続きDNSの `Temporary failure in name resolution` で失敗しました。journalにはsystemログを閲覧できない旨が明示されました。overlayの書き込みとサービス再起動も現在の実行環境では許可されません。直前のgoal作業では計測経路と局所payloadの追加により進捗がありましたが、同じ外部制約は複数のgoal継続にわたって解消していません。

残作業を進めるには、実APIへ接続できる環境、本番ログの読み取り経路、および管理者によるoverlay適用・再起動の経路が必要です。制約を迂回する変更や、模擬応答を実測と見なす追加試験では完了条件を満たしません。

## 実行環境修復後の再開結果

### 接続・ログ・適用経路の復旧

ホストのDNSとTLS接続自体は正常でした。DNS解決失敗はCodexのsandboxでネットワークが無効だったことが原因でした。対象プロジェクトだけに `.codex/config.toml` を追加し、`sandbox_workspace_write = { network_access = true }` としました。グローバルのCodex設定は変更前へ戻しています。元のsandbox権限を保持したまま対象プロジェクトの外部通信を有効にした構成です。以前のセッションが起動時設定を保持している場合は、再読込・再起動が必要です。

`bwrap` はUbuntu提供の `bubblewrap 0.11.0-2` を導入し、通常ユーザーでnetwork namespaceのloopback作成とCodex sandboxの起動・書き込みを確認しました。AppArmorの全体無効化やuser namespace制限の全体解除は行っていません。

`ubuntu` を `systemd-journal` グループへ追加し、既存セッションでも参照できるように該当machine-idのjournalディレクトリへ読み取りACLとローテーション用default ACLを設定しました。23:22 JSTのCodex workspace-write sandboxで、認証付き利用状況APIがHTTP 200となり、Kanaeのsystem journalを読み取れました。証拠は `docs/2026-10-10-token-efficiency-environment.json` です。認証情報やログ本文は証拠JSONへ保存していません。

修復直後はDNSエラーではなく利用枠100%による429が残っていました。quota endpointが示した19:19:59 JSTのリセット以降、再確認時には利用率6%となり、以下の実モデル実験を完了しました。利用枠・支払い設定・アカウント・failover設定は変更していません。検証後の利用率は9%でした。

### 本番の使用量回収

修復前から動作していた計測ログを回収しました。以下は前後比較ではなく、最適化適用前の観測です。

| 観測時間（JST） | main Opus件数 | main全入力 | main出力 | Haiku helper件数／入力／出力 | Sonnet helper件数／入力／出力 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 15:20:49–17:35:51 | 196 | 34,736,741 | 179,929 | 34／56,031／8,651 | 48／198,602／24,851 |
| 19:49:54–19:53:47 | 5 | 325,536 | 4,020 | 5／4,056／970 | 8／33,536／3,118 |

最初の時間帯のmain cache読み出しは33,668,120、cache作成は1,068,193、通常入力は428 tokensでした。cache読み出しを含む入力はモデルへ渡したコンテキスト量であり、そのまま請求額とは扱いません。mainの同一リクエストIDとhelperの同一応答IDを重複除外し、agent-loopの合計を重ねて加算していません。

task hashと有効overlayの固定promptを対応付けました。最初の時間帯ではHyDE 11件、keyword 11件、rewrite 11件、rerank 11件、judge-lite 12件、出典確認付き記憶抽出26件です。judge-liteは12件中4件がmax_tokensでした。抽出のtask hash `c0146a77eea75ce8` はquality overlayの `alpha/llm_mem0/extract.py` にある固定system promptと一致し、入力173,862・出力24,462 tokensでした。出典確認や記憶抽出を、消費が大きいという理由だけで停止していません。

`scripts/audit_token_usage.py` はtask hash別の内訳も出力するようにしました。model別とtask別は同じ応答の別内訳であり、両方を足しません。保存先は `docs/2026-10-10-token-usage-resumed-production.json` と `docs/2026-10-10-token-usage-later-production.json` です。不明usage・失敗・SDK内部再試行は0消費と扱いません。

### 同時query共有の実API比較と採用

公開合成クエリ6ケースを各2consumerで同時実行し、caseごとにarm順を入れ替えて比較しました。モデル・prompt・出力上限は従来値を保持しました。最大60リクエスト、自動再試行0、同時provider処理上限6とし、実験の連続dispatchには1秒の間隔を設けました。HTTP呼び出しをシリアル化した比較ではありません。

| 同時2検索の条件 | provider件数 | 全入力 | 出力 | 入力＋出力 | consumer品質 |
| --- | ---: | ---: | ---: | ---: | --- |
| baseline | 36 | 22,554 | 1,575 | 24,129 | 12/12通過 |
| singleflight | 18 | 11,277 | 799 | 12,076 | 12/12通過 |

この同一利用者・同一queryの同時2検索という対照では、provider件数と入力は50%減、入力＋出力は約50%減でした。共有した2consumerの応答は各ケースで完全一致し、variantsは独立したリストでした。全応答がend_turnかつ有効JSONで、usage4項目を取得できました。必要語、公開合成データの語彙検索、指定した誤った仮説の混入チェックを通過しています。個人記憶の意味検索精度を全面保証するものではありません。

完了済み応答を保持しない逐次2検索も別に確認し、6リクエストを使用しました。したがって、全60件のうち同時対照が54件、逐次対照が6件です。artifactの `completed_provider_calls` と各groupの `calls` は同じ応答なので二重加算していません。

待機なしの追加pilotは1ケース・15リクエストです。実際に6/3 providerリクエストを並列処理し、共有・逐次・品質の判定を通過しました。baseline 1,199 msに対し共有1,277 msで、78 ms遅い観測でした。1ケースから速度改善や系統的な劣化を断定しません。1秒間隔を設けた本試験のgroup中央値5,960/3,035.5 msも本番の速度改善とは扱いません。provider処理時間の中央値は976.5/1,001.5 msでした。

証拠は `docs/2026-10-10-query-singleflight-live.json` と `docs/2026-10-10-query-singleflight-live-unpaced.json` です。この50%は重複のある合成対照に限定した入力削減であり、本番全体の削減率や請求額の削減率ではありません。

### 密度・順序補正後の履歴再実験

合成履歴の密度とarmの順序を補正したharnessで、9ラウンドずつ18リクエストを完了しました。

| 条件 | 全入力 | cache読み出し | cache作成 | 出力 | 厳密品質 | 意味品質 | 拒否／コードフェンス |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| rolling | 52,200 | 0 | 52,164 | 178 | 5/9 | 7/9 | 2／2 |
| anchored | 57,480 | 37,176 | 20,268 | 234 | 6/9 | 8/9 | 1／2 |

追記方式はcache作成が61.1%減る一方、全入力が10.1%増え、厳密品質にも不通過が残りました。cache作成の削減を全トークン削減とは扱わず、履歴overlayは変更していません。署名付きthinking等の追加採用試験は、この候補が採用条件を満たさないため本番反映の根拠にしていません。証拠は `docs/2026-10-10-replay-window-live-complete.json` です。

利用枠復旧前の13成功応答と4件の429試行も別artifactへ残しました。13成功は不均衡な7/6 armの部分結果で、最終18件の比較へ混ぜていません。429のusageは不明です。checkpointと全体試行上限、長いRetry-Afterを待たずに再試行しない条件を追加し、障害時にも途中結果を失わないようにしました。

再集計は `scripts/analyze_token_efficiency_resumption.py --query docs/2026-10-10-query-singleflight-live.json --unpaced-query docs/2026-10-10-query-singleflight-live-unpaced.json --replay docs/2026-10-10-replay-window-live-complete.json` で再現できます。集計と入力SHA-256は `docs/2026-10-10-token-efficiency-resumption-summary.json` に保存しました。

### 最小overlayの本番適用と実rollback

新しいgenerationは `/var/lib/kanae-claude-performance/20261010-token-efficiency1` です。6ケース両armの実API品質、usage、dispatch数、応答共有、逐次非保持、payload/依存hashの一致を検査する採用gateを通した後に配置しました。全体の作業ツリーは本番へコピーしていません。

systemd drop-in `90-token-efficiency.conf` は既存36個のmountのうち検索moduleのsourceだけを差し替え、依存moduleのmountを1個追加しました。author/source識別、他の品質・性能overlay、認証情報、永続記憶を維持しています。新しいfeature flagは対象サービスだけで1とし、実装の既定値0は維持しています。両generationのroot所有・非書込権限とhashを検査し、別のoverlay変更や直近5分のモデル活動を検出した場合は適用を拒否するdriverを用意しました。

| 操作（JST） | PID | 有効search hash | flag | 必須subsystem |
| --- | ---: | --- | ---: | --- |
| 23:20:04 初回適用 | 464611 | `8de8e0b0…` | 1 | 正常 |
| 23:20:43 rollback | 465271 | `eacbe15d…` | 0 | 正常 |
| 23:20:50 再適用 | 465430 | `8de8e0b0…` | 1 | 正常 |

各操作で生きているprocessのmount namespaceのsearch hashとfeature flagを確認しました。最終適用では依存moduleのhashも一致しました。必須subsystemはDiscord/OAuth/Mem0/systemです。既存の `release_metadata_path_unset` による全体healthのdegraded/HTTP 503は残っています。新しい障害と混同していません。

最終PIDのmount namespaceにあるmoduleでも6条件のオフライン対照を再実行し、各arm114 consumerの保存応答一致と、user/query分離・逐次非保持を確認しました。これは読み取り可能な有効コードの対照で、実DB検索の独立したrecall標本ではありません。適用直後の観測では新しい本番モデル依頼がなかったため、自然トラフィックでのjoined率や全体削減率はまだ計測できていません。空ログを削減成功と扱いません。

適用・rollbackの証拠は `docs/2026-10-10-query-singleflight-activate-verification.json`、`docs/2026-10-10-query-singleflight-rollback-verification.json`、`docs/2026-10-10-query-singleflight-running-controls.json` です。payload manifestも実デプロイ済みへ更新しました。

次回以降のrollbackは、管理者が `sudo .venv/bin/python /var/lib/kanae-claude-performance/20261010-token-efficiency1/apply_token_efficiency_rollout.py rollback --dry-run` で構成を検査した後、`--dry-run` を外して実行できます。対象サービス・generationを固定したdriverであり、別のoverlay変更があれば上書きを拒否します。再適用は同じdriverの `activate` です。

### 追加回帰試験と残作業

今回の実行群では関連112件が通過しました。広いsession・compaction・tool-loop・Mem0互換回帰群は291件通過、21件skipでした。最新の実APIharness・再集計・helper計測・payload回帰群は48件通過でした。途中結果保持・長いRetry-After・集計の二重加算防止・不明usage拒否・task別の応答ID重複除外も検証しました。これらは別の実行群であり、重複を含む数値を総テスト数として加算していません。

本番と同じMem0α構成の21件では、変更前と候補の両方で19件通過、同じ2件が不通過でした。2件は履歴APIの引数を古い固定リストと比較する既存テストで、quality overlayの任意keyword-only `author_id` / `source_id` の追加を許容していません。出典・利用者識別の機能を除去して通す変更はしていません。候補による新しい不通過はありませんでした。Ruffとdiff whitespace検査も通過しました。

| 完了条件 | 修復後の到達点 | 残る検証 |
| --- | --- | --- |
| 本番の段階別使用量 | journal復旧、main 201件・helper 95件の使用量回収、固定task hashの抽出段階まで対応付け | SDK内部再試行・切断時消費、未観測compaction等の完全な計測 |
| 履歴方式の採否 | 補正後18件を実測し、入力増加と品質不通過で不採用 | 別の改善候補を採用する場合の品質・署名付きthinking等の検証 |
| 複数ラウンド比較 | 密度・順序を補正した実API比較と失敗分類を完了 | 本番の複雑な依頼や長期窓へ一般化しないこと |
| 記憶検索の重複削減 | 実API75件、既存6条件、最小overlay適用、実rollbackと再適用を確認 | 自然トラフィックの同時重複率、実DB/vector/graphのrecall品質と待ち時間 |
| 本番全経路の効率 | 前の使用量を回収し、変更後の監査経路を利用可能にした | 同等の依頼でのmain/helper/検証/要約の前後総量、成功率、recall、待ち時間の比較 |

以前のDNS・journal・管理者適用経路の制約は解消済みです。現在の残作業はその制約ではなく、代表的な本番前後の観測不足です。実際のDiscord投稿を新規に送って測定対象を作る操作や、支払い設定の変更は行っていません。したがって、目標全体を完了とは判定していません。

## 削減率・性能の再測定と実トラフィック観測

### 待機なしの反復対照

最大HTTP同時数6、自動再試行0、dispatch間隔0秒で、公開query 6ケースを3回実行しました。各回はbaseline 36件、singleflight 18件、逐次非保持6件の計60件で、追加180件すべて完了し、失敗・429はありませんでした。同じ6ケースの反復であり、18種類の独立したquery標本ではありません。

| 同時2consumerの条件 | provider件数 | 全入力 | 出力 | 入力＋出力 | group待ち時間中央値 |
| --- | ---: | ---: | ---: | ---: | ---: |
| baseline | 108 | 67,662 | 4,753 | 72,415 | 1,352.5 ms |
| singleflight | 54 | 33,831 | 2,320 | 36,151 | 1,266.0 ms |

同時重複の全入力は50.0000%減、入力＋出力は50.0780%減でした。group待ち時間の中央値は6.3956%短縮し、18ペアのlatency比の中央値は0.9351でした。全応答のJSON・end_turn・公開ケースの必要語・誤仮説排除、dispatch数、応答共有とリスト独立性、逐次非保持の条件を通過しました。各arm36 consumerと逐次6 consumerは各3回の合計で、同じ応答を追加加算していません。

公開合成query expansionのHTTP比較であり、Discord全体の待ち時間、個人記憶のrecall、請求額、将来のtail latencyを保証しません。証拠は `docs/2026-10-10-query-singleflight-performance-{1,2,3}.json` です。`scripts/assess_token_efficiency_performance.py` で再集計でき、待機のある比較・失敗がある比較・payload hashの違い・対応しないケースを拒否します。

### ユーザーが送信した実際の本番依頼

ユーザーの「今実際に応答を投げかけてテストしているので観測してほしい」に応じて、既存journalを読み取り、投稿やAPI依頼を追加せずに観測しています。下記は23:57 JST時点のスナップショットであり、テスト完了や最終採用の判定ではありません。

- 観測したmainの完了API呼び出しは23:47:10–23:57:19 JSTの9件。4つのcorrelation groupに分かれ、end_turn 4件、tool_use 5件でした。mainの通常入力26、cache読み出し1,445,580、cache作成667,149、全入力2,112,755、出力10,171 tokensです。
- API処理時間の中央値17,446 ms、最大24,322 ms、最初の応答までの中央値3,544 ms、最大9,488 msでした。完了リクエスト9件すべてに測定値があります。Discordへの最終送信までの時間ではありません。
- query共有の判断はleader 3件・joined 0件です。この時点の検索では重複provider処理を削除していません。対照の50%削減をこの本番群へ適用しません。
- helper失敗、main/helperの拒否や出力上限到達、compaction応答はこの群では記録されていません。未完了・未記録の処理を成功や0消費と扱いません。応答の内容品質はusageだけでは評価できません。

証拠は `docs/2026-10-10-token-usage-user-production.json` と `docs/2026-10-10-token-efficiency-user-performance-assessment.json` です。初期の空観測 `token-usage-performance-window.json` と `token-efficiency-performance-assessment.json` は上書きせず、時点を分離しています。公開する集計には会話本文、利用者・channel ID、認証情報、correlationの実値を含めていません。

前の本番ログは異なる依頼・履歴長・cache状態の群です。本番全体の前後削減率は対応する分母がなく、`whole_production_reduction_percent` をnullとして保持します。空観測を0%とせず、重複0の観測を一般化もしません。本番テストで効果が得られない場合は、ユーザーの指示に従い、処理中の依頼を避けたidle windowで追加singleflight overlayを戻します。

### Git保存の範囲と回帰試験

元のbranch `agent/fable-failover-readiness-fix` の作業ツリー・indexと無関係な未commit変更を保持し、base `2979f7c7533dd65bffc0632f8fc316d0b6bfe722` から別worktreeの `agent/token-efficiency-20261010` を作成しました。query共有、usage監査、公開実験、payload/rollback手順、現行API互換性の依存、結果資料と関連テストだけを保存します。サーバーの認証情報・状態ディレクトリ・machine固有のCodex権限設定は含めません。

sessionはbaseからopt-in履歴候補の最小差分を再構成しており、別のprivacy/work機能を巻き込みません。candidate flagと履歴候補flagはコード上の既定値0です。`deploy/performance/mem0_alpha/search.py` は検証済み最小payloadを生成するためのtemplateで、ファイル全体を無条件に本番へコピーする運用ではありません。新branchのpushはmaster/default release gateを動かさず、本番の候補flagを有効化しません。

push用worktreeで、helper・usage・singleflight・payload・公開品質・API互換・session・replay cap・compaction・tool-loopの回帰400件が通過しました。journal監査に追加した欠損latency・ID重複・model分離・helper除外のテストも含みます。API latencyは完了main応答だけを対象にし、欠損を0msに置き換えません。

## 実テスト終了後の最終判断（2026-10-11 00:09 JST）

ユーザーから「テスト終了・最後の応答も完了しました」を受領しました。00:06:30 JSTまでを固定した最終観測は、23:47:10–00:03:36 JSTのmain完了API 14件・end_turn 5件・tool_use 9件です。5つのcorrelation groupがすべてend_turnへ到達しました。ただしusageログからDiscordへの送信成功や回答内容の意味品質を全面保証することはできません。

| 測定対象 | 重複除外済みAPI件数 | cacheを含む全入力 | 出力 |
| --- | ---: | ---: | ---: |
| main Opus 5.5 | 14 | 3,441,100 | 13,255 |
| helper Haiku 4.5 | 13 | 23,666 | 3,502 |
| helper Sonnet 4.6 | 18 | 75,696 | 9,705 |

合計は45完了API、全入力3,540,462・出力26,462 tokensです。helperの生ログ35件から、同一応答IDの重複4件を除いた31件を計上しました。model別とtask別を追加加算していません。固定task別ではrewrite・keyword・HyDE・rerankが各4件、judge-lite 5件、出典確認付き記憶抽出10件です。query expansionの3段階は計12 provider依頼、入力7,492・出力492 tokensでした。

query共有はleader 4件・joined 0件です。今回省けたquery expansion provider依頼は0件で、観測した検索4回の呼び出し削減は0/12（0%）でした。この0%は当該機能の呼び出し削減であり、異なる本番群との前後総トークン削減率ではありません。query expansionの入力＋出力は今回測定したmain/helper総量の約0.22%で、同時重複対照の50%を全体へ適用しません。

完了main APIの処理時間は中央値14,721 ms・最大24,322 ms、最初の応答までの中央値3,832.5 ms・最大9,488 msでした。一方、stream stall警告2件と、記憶保存が不完全としてbatchを再試行用に保持した警告5件がありました。これらを成功・0消費と扱わず、Discord全体の待ち時間や記憶の永続化品質が問題ないとは結論しません。前の15:20–23:20 JSTのログにもstream stall警告17件、記憶batch再試行保持16件が存在しました。観測時間・依頼内容・履歴長が違うため、新旧警告率や因果関係は計算していません。

証拠は `docs/2026-10-11-token-usage-before-rollback.json`、`docs/2026-10-11-token-efficiency-performance-assessment.json`、`docs/2026-10-11-token-efficiency-operational-observations.json` です。streamの未完了・記録されない再試行等のusageは不明で、完了応答のAPI durationはそれらを含む全体待ち時間ではありません。全体の前後削減率は引き続きnullです。

### 追加singleflightの撤回

合成同時重複の品質・速度比較は通過しましたが、今回の本番運用では対象となる重複がなく削減が出ていません。ユーザーの「効果的でなかったり実際の性能に影響をきたしている場合、前の実装に戻す」に従い、追加singleflightだけを撤回しました。少数の観測から将来の重複率や性能悪化を断定した判断ではなく、今回の代表的なテストで効果が得られなかったことに基づく保守的な採否です。

最後のhelper活動00:04:19 JSTから5分以上の静穏を確認し、root所有generation・構成hash・既存mount維持のdry-run検査を通したdriverで、00:09:43 JSTに対象サービスだけをrollbackしました。稼働PIDは477943、実mount namespaceのsearch SHA-256は従来の `eacbe15dcb5a84c95feeaed50b4cbcf6f734e90125f3ae76725d1124b8e3f6e5` と一致し、`MEM0_QUERY_SINGLEFLIGHT_ENABLED=0` です。必要なDiscord/OAuth/Mem0/systemは正常で、NRestartsは0です。旧品質・性能overlay、利用者・出典識別、他サービス、認証情報、履歴方式を変更していません。

実rollback証拠は `docs/2026-10-11-query-singleflight-final-rollback-verification.json` です。既存のrelease metadata未設定によるhealth degraded/503は残り、記憶保存の再試行問題とAPI遅延の根本修復もこのrollbackだけで完了とはしません。候補のgenerationと合成実験資料は再評価できるよう保持しています。本番再適用は新しい実運用証拠に基づいて判断してください。

### Gitと最終回帰

実装・公開実験・測定結果の初回commit `3dbd254d91f61fce99fad6f1ae69127f3ae1acd4` を `origin/agent/token-efficiency-20261010` へpushし、remote refとの一致を確認しました。masterへmergeしたり、別サービスへdeployしたりしていません。最終観測とrollbackの証拠、警告を消費量と混同しない監査・テストを追加保存します。push用worktreeの最新回帰は404件通過、Ruffとdiff whitespace検査も通過しました。元の作業ツリーのtracked statusおよび全tracked差分SHA-256は保存した修正前スナップショットと一致していました。
