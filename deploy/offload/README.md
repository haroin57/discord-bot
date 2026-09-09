# オフロード構成の導入手順です。

実装は独立に有効化できます。既定値は従来のWrangler転送、ローカルKB、ローカル数式です。import軽量化・必要列のみの検索・キュー修正・state走査改善はコード更新で適用されます。稼働中プロセスへの反映には通常のリリース手順を使用します。

## Archive Workerを検証します。

```bash
cd workers/archive
npm ci --ignore-scripts
npm run types
npm run check
npm test
npm audit --audit-level=high
```

`wrangler.jsonc`のrootはstaging bucketを参照します。`production`は既存名`kanae-state-backup`を参照します。使用するアカウント、bucket、prefixがBot設定と一致するように設定します。異なるBotは異なるprefix・Worker名・tokenを使用します。既存Botのprefixを変える場合は、保存済み`r2://`参照の移行も別途扱います。

stagingへの配置コマンドは次のとおりです。tokenは32文字以上の乱数を秘密ファイルまたはsecret管理基盤で保持します。値をコマンド引数やログへ出力しません。

```bash
npx wrangler whoami
npx wrangler r2 bucket create discord-bot-archive-staging
npx wrangler secret put ARCHIVE_AUTH_TOKEN --env=''
npx wrangler deploy --env=''
```

Bot側の設定例です。token欄にはWorkerと同じ実値をsecret注入で設定します。

```dotenv
R2_ARCHIVE_TRANSPORT=http
R2_ARCHIVE_HTTP_URL=https://discord-bot-archive.example.workers.dev
R2_ARCHIVE_HTTP_TOKEN=<secret>
R2_ARCHIVE_HTTP_TIMEOUT_S=60
R2_ARCHIVE_BUCKET=discord-bot-archive-staging
R2_ARCHIVE_PREFIX=discord-bot/v1
```

- `/healthz`、`GET/HEAD/PUT /v1/objects?key=...`はBearer認証を要求します。公開bucket・一覧・削除APIは作成しません。
- PUTはSHA-256をR2側でも検証し、ACKのhash・sizeが一致した場合にspoolを削除します。Workerはrevision比較と条件付きPUTを行います。ローカルflusherも同じキーの順序を維持します。
- 1件64MiBまでHTTPを使い、それを超えるファイルは既存Wranglerを使います。大容量snapshotを扱うホストにはWranglerの認証を残します。HTTP障害時の通常ファイルをCLIへ自動退避しません。
- retryの公平性はキー単位です。破損payloadがあるキーの新しいsnapshotは追い越しません。キューを手動で削除して解消しません。
- downloadはtempfileへstream保存し、長さと提供されたSHA-256を検証してから置き換えます。旧WranglerオブジェクトにはSHA-256 metadataがない場合があり、その場合は長さ検査だけになります。download上限は1GiBです。
- 本番は`--env production`を明示して配置します。HTTP URL・bucket・prefix・tokenを一致させます。同じprefixのキューは単一ホストのflusherで管理します。ETagは内容に対応し、revisionそのものへの比較交換ではないため、同一内容への更新を含む任意の並行書き込みに対する分散ロックとしては使用しません。複数ホストからの書き込みには、世代別の不変オブジェクトと一元管理する参照先等の追加設計が必要です。
- 戻す場合は`R2_ARCHIVE_TRANSPORT=wrangler`とします。同じspool・bucket・prefixを継続使用できます。

Workerの開発依存はlockfileで固定しました。Miniflareの既知の依存脆弱性を避けるため、`sharp`を0.35.4へoverrideしています。Worker実行コードにnpmのランタイム依存はありません。

## Native KBを別ホストで起動します。

`knowledge_server`はLanceDBの読み取りサービスです。Discord、OAuth、Mem0、OpenAIの秘密鍵を必要としません。Gateway側でembeddingを生成し、1〜10件の1536次元ベクトルをまとめて送ります。検索上限は列あたり100件、request上限は1MiB、response上限は8MiBです。

同じモデルで作成した既存の`invest_kb`テーブルを移行先へコピーします。実行中の書き込み・cleanupと競合するコピーを避けるため、取り込みを停止するか、Lanceの固定versionに対応する整合したsnapshotを用意します。サービスは存在しないDBを空で作成せず、起動エラーにします。更新は移行先の取り込み処理でatomic replacementを実行するか、新しいsnapshotでサービスを入れ替えます。

```bash
docker build -f deploy/offload/knowledge.Dockerfile -t discord-bot-knowledge:offload .
docker run --rm --name discord-bot-knowledge \
  --memory=768m --cpus=1 --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --env-file /etc/discord-bot-knowledge.env \
  --mount type=bind,src=/srv/discord-bot-kb,dst=/data/invest_kb,readonly \
  -p 127.0.0.1:8095:8095 discord-bot-knowledge:offload
```

環境ファイルは次の内容を設定します。modeは600にします。

```dotenv
KNOWLEDGE_AUTH_TOKEN=<32文字以上のsecret>
KNOWLEDGE_MAX_PENDING=4
KNOWLEDGE_DB_PATH=/data/invest_kb
```

別ホストからの接続はTLS終端または認証付きの専用トンネルを使用します。HTTPの許可先はliteral loopbackとlocalhostだけです。サービス内でもtokenを検査します。汎用reverse proxyを使う場合はAuthorizationを保持します。追加のCloudflare Access service-token headerはKBクライアントには実装していません。

Bot側を次の設定へ変更します。

```dotenv
INVEST_KB_VECTOR_BACKEND=http
INVEST_KB_HTTP_URL=https://knowledge.example.com
INVEST_KB_HTTP_TOKEN=<同じsecret>
INVEST_KB_HTTP_TIMEOUT_S=20
```

固定queryでローカルと比較し、結果が一致した後に対象Botを切り替えます。remote障害は例外で返し、既存の前処理がKB取得失敗を処理します。ローカルの空DBへ自動接続しません。native executorは1本で、待機を含めて最大4要求です。HTTP待機のキャンセル後もnative実行が終わるまで受付枠を保持します。戻す場合は`INVEST_KB_VECTOR_BACKEND=local`にし、最新データがローカルに存在する状態で再起動します。

`knowledge.service`は専用VM用テンプレートです。コードは`/opt/discord-bot`、DBは`/data/invest_kb`等の読み取り可能な配置にします。コンテナをCloudflare Containersへ直接置く場合には、ephemeral disk用のsnapshot取得処理を別途実装します。このDockerfileだけでCloudflareの永続ストア構成ができるものではありません。

## MCPの接続先を変更します。

`MCP_REMOTE_SERVERS`にJSONを設定します。JSONには接続先と環境変数名を記述します。secretそのものを埋め込みません。

```dotenv
MCP_REMOTE_SERVERS={"context7":{"url":"https://docs-mcp.example.com/mcp","headers_env":{"CONTEXT7_API_KEY":"CONTEXT7_API_KEY"}},"playwright":{"url":"https://browser-mcp.example.com/mcp","bearer_token_env":"BROWSER_MCP_TOKEN"}}
```

上記URLは置き換え用の例です。`headers_env`にはCloudflare Accessの`CF-Access-Client-Id`等も設定できます。bearer tokenを指定しない公開MCPも設定できます。`tavily`、`brave-search`、`google-maps`、`agentic-inbox`はローカルAPIキーがない構成でもremoteを設定できます。IoT・メール等のユーザー許可リストとtierは維持されます。不明なサーバー、秘密値の欠落、不正header、HTTPS以外の外部URLは起動時エラーにします。

接続前にinitialize・tools/list・tool名・inputSchemaを確認します。特にローカルのstatic schemaを公開するサーバーでは、同じサーバー名に異なるツールセットを接続しません。移行先MCPサーバー自体の実装は、この設定機能とは別です。

## 数式描画を選択します。

```dotenv
MATH_RENDER_BACKEND=cloudflare
CF_ACCOUNT_ID=<Browser Runの対象アカウント>
CF_BROWSER_RENDERING_TOKEN=<Browser Rendering権限のsecret>
KATEX_CDN_VERSION=0.16.22
```

remoteではローカルKaTeXのインストールが不要です。`trust:false`とHTML用のJSON escaping、PNG検証を維持しています。remote失敗時は文字出力に戻ります。ローカルChromiumへ自動退避しません。既存のQuick Actions制限を考慮し、render失敗率・待機時間・費用を測定します。戻す場合は`MATH_RENDER_BACKEND=local`です。ローカルの既存キャッシュ形式を維持しています。

## Schedulerをtimerへ切り替えます。

`vi-signal-scheduler.timer`は平日18:00 JST、`intervention-scheduler.timer`は30分ごとです。VIは既存の営業日判定を追加で実行し、判定処理が失敗してもforward記録を試行します。`Persistent=false`により起動直後に過去の通知を追い掛けて送りません。

既存serviceの権限・credential設定は保持し、対応する`service.d/oneshot.conf`をdrop-inとして適用します。旧常駐serviceを停止・自動起動解除し、drop-inとtimerを配置してdaemon-reload後、timerだけを有効化します。旧Cloudflare cron・旧scheduler・新timerを同じ通知先へ重複有効化しません。これらの運用変更はこの作業では実行していません。

drop-inの実行ファイルパスは既存のrelease配置を前提にします。VIは480秒、介入は180秒で実行を打ち切り、`KillMode=control-group`により子プロセスも対象にします。起動用Pythonは`sys.executable`を使い、release環境に存在しない`src/.venv`を参照しません。ログは`BOT_STATE_DIR`へ保存します。戻す場合はtimerを停止し、drop-inを外して既存常駐serviceへ戻します。

## 再検証します。

```bash
.venv/bin/python scripts/audit_offload.py --runtime --output /tmp/offload-inventory.json
.venv/bin/python scripts/check_test_isolation.py
.venv/bin/python scripts/run_test_lane.py unit-fast --workers 2
.venv/bin/python scripts/run_test_lane.py collection-contract --workers 2
.venv/bin/python -m pytest tests/integration/test_knowledge_offload.py -m integration -q
.venv/bin/ruff check .
.venv/bin/mypy
```

この作業ではunit laneが3,341件通過・21件skip、contract laneが50件通過しました。新しいnative HTTP integrationの3件も通過しました。後続の局所修正は対象テストで再検証しています。Workerの7件の実行テスト、TypeScript検査、dry-run bundle、npm auditが通過しました。型検査は既存3ファイルと新規境界5ファイルが対象です。

専用Dockerイメージをビルドし、512MiBのメモリ制限と読み取り専用root・KBマウントで起動しました。合成データを使い、healthの200応答、実LanceDBの2つのvector列の検索結果、未認証要求への401応答を確認しました。検証用コンテナは削除しました。この試験は起動と接続の検証であり、本番コーパスの最大メモリを保証するものではありません。本番のegress、実請求、検索p95、描画品質、外部MCPのschema一致は展開時の計測対象です。

## 2026-09-09の本番配置です。

本番用の変更は現在の稼働リリースから分岐した`deploy/offload-20260909`に統合しました。作業ツリーに含まれる別作業の変更は取り込みません。既存のMem0α、画像処理の非同期I/O、画像単位のロックを保持します。

Archive Workerは既存バケットのあるアカウント`8574821c4ec0b24f61d59c5a15dc31f4`を`production.account_id`に固定しました。接続先は`https://discord-bot-archive-production.haroin57.workers.dev`です。同アカウントはFreeプランのため、Paid専用のCPU上限指定は外し、プラン既定の実行上限を使用します。

Native KBは既存の`drawingsearch` SSH接続先に配置します。rootは`/opt/discord-bot-knowledge`、KBは`/var/lib/discord-bot-knowledge/kb-snapshot-20260909`です。本番KBのversion 21・4,423行をコピーし、検索結果を照合しました。`knowledge-production.service`が別VMのlocalhost:8095で提供し、`knowledge-tunnel.service`が本番ホストのlocalhost:8095へSSHで転送します。外部向けの検索ポートは開放しません。

取り込み処理の書き先は従来のローカルKBです。新しい資料を取り込んだ場合は、version固定または書き込み停止で整合したsnapshotを作成し、別ディレクトリへ転送して、行数・検索結果の照合後に検索サービスの`KNOWLEDGE_DB_PATH`を変更して再起動します。更新の自動同期は今回の配置には含めません。以前のsnapshotを保持することで戻せます。

Botの秘密値はroot専用の`/etc/discord-bot/credentials/offload.env`からsystemdが読み込みます。KB VMには専用の検索tokenだけを配置します。Context7はローカル2.2.5とremoteのtool名・入力型・必須引数を照合し、`query-docs`の実呼び出しを確認しました。
