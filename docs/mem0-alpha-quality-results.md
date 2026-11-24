# Mem0 alpha 品質改善結果

記録日は 2026-09-09（JST）です。この文書は品質改善候補の実装と残余制約を記録します。
品質版は `0.3.1+botquality.1` です。Alpha のソース版・wheel 版は各 286 件のテストを通過しました。
この品質改善は本番未反映です。既存本番の alpha 有効化・性能改善とは別の変更です。

## 対象と来歴

- 計画は [mem0-alpha-quality-plan.md](mem0-alpha-quality-plan.md) に記録しました。
- 比較基準は commit `bd2808baa3d1e7700cb2591cc289abb4f6b240d6` の
  `mem0-alpha==0.3.0+botperf.1` です。品質版の識別情報は下記に記録しました。
- 修正対象は管理下の `vendor/mem0-alpha` と bot の呼び出し経路です。
  元の dirty な `/home/ubuntu/mem0-work/Mem0-alpha`、共有 venv、本番の記憶データは変更していません。
- 配布名は `mem0-alpha`、import 名は `llm_mem0` です。
  [package metadata](../vendor/mem0-alpha/pyproject.toml) と
  [PROVENANCE](../vendor/mem0-alpha/PROVENANCE.md) を正式な版・来歴の確認先にします。
  この vendor は既存のローカル変更を含む source-only snapshot であり、未改変の upstream commit ではありません。
- [vendor README](../vendor/mem0-alpha/README.md) は upstream の一般的な利用説明です。
  その認証・モデル例を本番の実効設定とみなさず、ローカル vendor から regular wheel を構築します。
  品質改善による版・checksum・provenance の更新と通常 wheel の構築を実施しました。

## 実装した変更

### 記録・保存・知識更新

- channel のバッチを実際の発言者単位で処理します。新規記録に author ID、安定した source ID、
  元の発言時刻を渡し、バッチの最後の呼び出し元へ全員の発言を帰属させません。
  Discord の再構築メッセージでは実メッセージ ID から時刻を復元します。
  セッション由来の ID を Discord メッセージ URL として捏造しません。
- バッチに耐久保存、lease、成功後の acknowledgement、再試行、request ID の重複処理を追加しました。
  保存中に追加された強制 flush、取消、再起動、fsync 失敗後の再試行を回帰対象にしました。
  空 dict、不定 status、部分失敗を完了扱いしません。上限超過を黙って切り捨てません。
- 全 turn と上限内の長文 chunk を抽出へ渡します。不正な個別要素を分離し、輸送・形式エラーと
  明示的な「保存対象なし」を区別します。出典・引用・発言者を抽出と保存へ引き継ぎます。
- ベクトルが近いだけで数値変更・否定・訂正を捨てないようにしました。
  作者、属性、scope、条件、有効期間を比較し、置換先の保存確認後に旧 fact を失効させます。
  同じ state を使う協調プロセスの関連更新をロックし、失敗・取消時の保存競合を回帰対象にしました。
  全バックエンドを跨ぐ完全な exactly-once transaction を保証する実装ではありません。
- ベクトル索引が未反映でも同一 fact の別 source event を耐久 receipt で再利用します。
  再送の強化回数を重複計上せず、ユーザーによる再確認の発言時刻と単なる参照を区別します。
  明示的な時刻と更新判定がある逆順の観測は履歴として保存し、新しい状態を失効させません。
  `asyncio.run` の全 Task 取消時も、実際の保存 worker の完了まで domain lock を保持します。
- 共通削除 API は ingest receipt を先に tombstone 化し、保存 commit 後 Timeout の再送による
  復活を防ぎます。削除失敗を本体の再取得で確認できた場合のみ元の receipt に戻します。
  削除の成否が不明な場合は復活させない側に保持し、再確認が必要なエラーを返します。
- 記憶の所有者修正は、同じサーバーの認可済み会話を共有する方針の変更ではありません。
  モデル、認証、collection 名、既存の会話アクセス権限はこの作業で変更していません。

### 検索・根拠・時刻

- lexical-only 候補を本体ストアから取得し直し、削除済み・期限外・未来・明示的な作者不一致を除外します。
  設定に基づく scope 判定後に最終件数を制限し、候補不足を上限付きの追加取得で補います。
- vector distance、RRF、BM25 rank、graph の関連度を区別します。同一 ID の最良の根拠を保持し、
  重複行や同義 query による加点を抑えます。lexical-only 候補に架空の cosine 値を付けません。
- reranker の有効な空結果を尊重します。不正応答・輸送失敗と意図した棄却を区別し、
  重複・範囲外 index を採用しません。query 周辺と先頭・末尾・条件を入力に残します。
- tokenizer version 2 で短い識別子、バージョン、日付、数量、全半角、日本語の語形を扱います。
  原文は保持し、BM25 の診断値に tokenizer version と旧形式の行数を追加しました。
- graph の曖昧な alias、他の作者、無関係な過去話題からの候補混入を抑えました。
  core cache と priming に state・collection・作者・channel の境界を反映し、
  改訂版プロセス間では共有 revision によって core の無効化を伝播します。
- 同じ relation に複数の source fact を記録できます。一方の失効・削除で他の有効な根拠を消しません。
  source ごとの有効期間と失効記録を追加テーブルで管理し、遅延投影・再送で閉じた関係が復活する
  経路を防ぎます。既存 relation ID と既存 schema を破壊する移行は実施していません。
- `search_memories_at_time` に Unix 秒または timezone 付き ISO8601 を明示指定できます。
  各リクエストの時刻を分離します。自然言語から時刻を推測する機能ではありません。
  プロンプト表示は記録・確認・参照・有効期間を区別し、参照回数を再確認の証拠とみなしません。

### 履歴・統合・削除・API

- 履歴 replay は `(timestamp, row ID)` と `(channel, user)` の cursor を使い、
  同時刻の行を取りこぼさず、確認済みの入力まで進めます。不明作者を表示名から推定しません。
  新規履歴の source ID を安定化し、更新時には同じ source を再利用します。
- 履歴再索引は入力準備後の単一 SQLite transaction で FTS・metadata・cursor を置き換えます。
  入力 iterator や SQL の失敗では旧状態を保持します。成功時は再採番に伴って cursor をリセットします。
  本番での再索引は実行していません。
- gist は必要な要約の保存をすべて確認してから元記憶を失効させます。
  部分失敗の再試行では保存済みの要約を再利用します。schema/gist の派生元と条件を保持し、
  自動的な破壊的忘却は有効化していません。
- 共通の削除経路に graph・relation・BM25・fact projection・core cache の整理を接続しました。
  個別の副索引処理が失敗しても残りを試行します。fact projection の更新で元の時刻・失効状態・
  後継リンクを消さないようにしました。
- `get_all(limit=None)` を SDK へ明示して暗黙の件数制限を回避します。
  SQLite の親ディレクトリ作成、再初期化、設定変更の検出を改善しました。
  履歴 vector は初期化済みの provider/embedder を再利用します。
  MCP に上限付きの引数・時点指定・詳細表示を追加し、一覧取得では障害と空結果を区別します。

## 評価方法と結果

[評価 CLI](../scripts/eval_mem0_alpha_quality.py) と
[fixture の説明](../tests/fixtures/mem0_alpha_quality/README.md) が再現手順です。
架空の記憶を使う合成シナリオは 32 件です。実際の一時 SQLite FTS・graph と検索・保存 API を使い、
vector provider と LLM は決定的な fake に置き換えます。基準版と候補版は別の子プロセスで実行します。
本番データ、認証、`.env`、実 LLM、実 Chroma、ネットワークを使用しません。
実行時の監査ガードは防御の追加層であり、OS の sandbox と同等ではありません。

検索可能な正解があるケースの Recall@k、Precision@k、MRR、段階的関連度の nDCG@k を集計します。
正解なしのケースを検索指標の満点にせず、無回答の precision/recall で評価します。
別作者・別 scope・失効済み・削除済み・不明 ID の混入を個別に数えます。
保存失敗・再試行ケースでは返答だけでなく fake provider の保存状態も確認します。

| 最終確認項目 | 状態 |
| --- | --- |
| 基準版と凍結候補版の 32 ケース比較・指標 | 13/32 → 32/32 です。19 件改善し、ケース・ランキングの回帰は 0 件です。 |
| alpha 回帰、bot bridge、記録・履歴の統合テストの最終件数 | ソース版 286 passed、wheel 版 286 passed です。 |
| 全 bot suite（`-m ''`） | 3846 passed、269 skipped、2 warnings、158.89 秒です。Alpha・任意モード等の skip 条件は維持しました。Alpha は別プロセスの専用 lane で検証しました。 |
| 静的検査 | 変更対象の Ruff と `git diff --check` が通過しました。 |
| wheel の構築、checksum、import origin、wheel 版の再評価 | 通常 wheel を独立ディレクトリへ導入し、31 package files のソース一致を確認しました。wheel 版も 32/32 です。 |
| 本番の精度・遅延・費用・実運用確認 | この工程では未実施です。 |

全 bot suite の警告 2 件は既存の fork テストにおける lance の fork 非対応警告と
multi-thread process の fork 非推奨警告です。テスト失敗ではありません。

| 合成評価指標 | 基準版 | 品質版・wheel |
| --- | --- | --- |
| Recall@k | 0.5917 | 1.0000 |
| Precision@k | 0.2583 | 0.4583 |
| MRR | 0.5750 | 1.0000 |
| nDCG@k | 0.5488 | 1.0000 |
| 無回答 precision / recall | 0.4444 / 0.3333 | 1.0000 / 1.0000 |
| 別作者 / 失効済み / 削除済み / 別 scope の混入数 | 2 / 5 / 1 / 2 | 0 / 0 / 0 / 0 |

Precision@k が 1.0 ではないため、「全検索結果が正解」という結果ではありません。
途中実行の結果は監査資料に残しますが、上記は凍結版・配布物の評価です。
この 32 件は小規模な欠陥回帰用の合成評価です。公開データセットによる benchmark ではなく、
LongMemEval を実行した結果でもありません。fake LLM の長文・複数 turn ケースは入力の伝達、
分割、解析、保存経路を検証します。実モデルの意味理解や自然な会話からの正しい抽出率は検証しません。
合成スコアを根拠に全体の関連度閾値や embedding モデルを調整していません。

### 旧 upstream テストとの互換性

元の dirty checkout の 7 テストファイルを変更せず、一時領域のコピーから凍結候補に対して
161 件を実行しました。結果は 145 passed、15 failed、1 skipped です。
最終ソースでも同じ結果を確認しました。全 upstream テスト通過とは扱いません。

不一致の内訳は、metadata 全体保持への変更 4 件、最新本体行の再取得を設定していない mock 2 件、
作者別・行 ID 付き replay cursor と state reset の旧 fixture 3 件、類似距離だけでの除去期待 1 件、
graph score を vector distance とする旧期待 2 件、MCP のツール一覧・エラー文字列 2 件、
legacy batch の一括 pop 契約 1 件です。PII パターンをコピーしない検証は 1 件 skip です。
旧 batch の 80 件は初回 50 件と保留 30 件に分かれ、2 回の ack 後に全件処理できることを確認しました。
期待値を満たすために安全性の修正を戻していません。新しい契約は Alpha 専用回帰で検証しました。
詳細は監査ディレクトリの `upstream-compatibility.md` と `upstream-compat-uJ1pF7/junit.xml` に記録しました。
ネットワークと認証情報へのアクセスを拒否した隔離実行であり、実 provider の動作検証ではありません。

## 残余制約と必要な移行

- 既存 FTS 行は自動で tokenizer version 2 に再変換されません。旧記憶の短語・数値検索へ適用するには、
  バックアップ、shadow index での比較、承認された明示的な再索引が必要です。
  再索引の transaction 化は、並行する本体データ更新との整合した snapshot まで保証しません。
- graph の `UNIQUE(canonical_name, user_id)` は維持しています。同名で型の異なる entity は
  誤統合を拒否しますが、完全に共存させる schema migration は未実施です。既存の誤統合も推測で分割しません。
- 旧 history vector の本文 hash 由来 ID は自動移行・削除しません。
  新しい source ID 由来の vector と重複して残る可能性があります。
  collection と embedding モデルは保持し、別途の監査・移行が必要です。
- 旧 cursor は新しい作者別 cursor に推測で移行しません。不明作者の履歴は自動再帰属しません。
  owner metadata がない古い検索行には provider/user-scoped projection を信頼する互換処理が残ります。
  旧データの作者の正しさやアプリ全体の guild 認可をこの合成評価だけで証明しません。
- 明示的な時点検索は追加しましたが、事実の有効時刻とシステムが知った時刻を独立管理する完全な
  bitemporal model ではありません。32 ケースの時刻評価は現在の有効性が対象であり、
  過去時点の意味的な回答精度は未検証です。代名詞の自動復元や新しい多段推論 planner も対象外です。
- 追加候補取得には予算上限があり、全記憶からの完全探索を保証しません。core revision の即時反映は、
  同じ state/collection を使う改訂版と更新 hook の協調が前提です。旧版、外部からの直接更新、
  revision の保存失敗では従来の TTL が残ります。
- 属性・scope・条件・競合検出の設定を無効にした経路では、それらを必要とする保護を同等には保証しません。
  候補検索で見つからない古い競合や、時刻・型・出典の欠けた旧データを推測で上書きしません。
  graph の複数 source 保護は新しい協調実装が前提であり、過去に失われた根拠は復元しません。
- replay の実行排他は同一 event loop 内です。複数プロセスで同じ履歴を同時 replay する場合や、
  同じ cluster を同時統合する場合の distributed lease は未実装です。
  派生知識を作った後の訂正に応じた全面的な再評価や、部分 archive の crash repair も残ります。
- 副索引の整理は本体と単一の分散 transaction ではありません。raw 履歴からの将来の再取り込みまで
  禁止する永続的な忘却 tombstone はありません。全件列挙はメモリ使用量が増え、snapshot pagination ではありません。
- 実 LLM の抽出・要約・条件判断、現実の日本語会話、長期記憶の意味的な更新、実 embedding の検索品質、
  provider 障害時の挙動、実データ量での遅延と費用は別途検証が必要です。
  fake provider の保存失敗テストは実ストアの crash durability を証明しません。
- legacy `pop_batch` の破壊的な全件取得契約は維持しません。既定では 50 件の lease を返し、
  残りを保持して明示 ack を待ちます。新しい bot 呼び出しは claim/renew/ack を使用します。
  独立した旧 API 利用者は呼び出し側の移行が必要です。

## 配布物と再現

- wheel は `mem0_alpha-0.3.1+botquality.1-py3-none-any.whl` です。
  SHA-256 は `361b0432f8f494566a44878673dfb0def98c77170740740a19378f26af2abe5d` です。
- 評価対象の package source SHA-256 は
  `4fd1a87f6fce60869a82bd4b36f6dec09f2ead9dcf9ca38d7d99ba8e72bb3beb` です。
- `SNAPSHOT.sha256` 自体の SHA-256 は
  `d9064572685ad626f8206953d454ef0dfdb0647d85ddacdb3b059b49816e0243` です。
  同 manifest の対象 34 ファイルはすべて検証済みです。
- ソース・wheel の評価 JSON、wheel、監査資料は
  `/home/ubuntu/mem0-quality-20260909-WSRU0n` に保存しました。
  wheel の import は同ディレクトリの `wheel-install-K77kVU/llm_mem0` から行い、共有 venv の
  editable package を変更していません。新たな依存ダウンロードは行っていません。

```bash
MEM0_ALPHA_ENABLED=1 PYTHONPATH="$PWD/vendor/mem0-alpha:$PWD" PYTHON_DOTENV_DISABLED=1 \
  python -m pytest -q tests/unit/test_mem0_alpha*.py tests/test_mem0_alpha_parity.py
python -B scripts/eval_mem0_alpha_quality.py \
  --baseline-ref bd2808baa3d1e7700cb2591cc289abb4f6b240d6 --check
```

## 反映前の条件

この候補の検証結果に加えて、運用側の反映可否を判断する必要があります。
既存の配備ゲートを省略しません。既存 namespace と実効設定を確認し、復旧可能な本体記憶・
SQLite/WAL・履歴・バッチ状態のバックアップを確保します。旧 artifact と設定を保持し、
記憶を書き込む処理の停止・再開順序と rollback の互換性を確認します。
FTS 再索引、同名 entity の schema 移行、旧 history vector ID の移行は通常のコード反映と分離します。
追加した `mem0_ingest_receipts.sqlite3`、relation source テーブル、各 lock ディレクトリも
state のバックアップ対象に含めます。旧実装との混在書き込みによる rollback は保護対象外です。
この文書は本番変更、旧データの削除、認証変更、公開 push の実施記録ではありません。
