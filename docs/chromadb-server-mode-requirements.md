# ChromaDB サーバーモード化 要件定義

> 作成日: 2026-05-16
> 起因: 2026-05-16 のbot本体プロセス SIGSEGV + mem0-mcp subprocess SIGSEGV 多発インシデント
> 目的: ChromaDBへの並行アクセスによるインデックス破損の構造的解決

## 1. 背景と問題

### 1.1 発生インシデント (2026-05-16 07:27 〜 07:38)

bot本体プロセスが連続SIGSEGVクラッシュ、再起動ループに陥った。`mem0-mcp` subprocess も `mcp__mem0__search` 実行時にSEGV。原因調査の結果、以下が判明:

- `chroma.sqlite3` の `embeddings_queue` 最大 seq_id: 9358
- HNSW segment の `max_seq_id`: 9220 (138件遅れ)
- HNSWバイナリインデックス (`data_level0.bin` 等) の最終更新: 04:55 (3時間以上前で停止)
- sqlite には 3558 件の embedding metadata、HNSW には推定 4067 ベクトル → orphan ベクトル多数

### 1.2 根本原因

ChromaDB 1.5.9 は **Rust binding ベースの埋め込み型 HNSW** を使用。これが**マルチプロセス/マルチスレッド同時書き込みに対して安全でない**。今回のシナリオ:

- bot本体プロセス: `services.mem0.client._get_mem0()` 経由で直接 ChromaDB を保持・書き込み (`add_memories()`)
- `mcp_mem0_server.py` (MCP subprocess): 同じく直接 ChromaDB を保持
- **2プロセスが同一の `mem0_db/` ディレクトリ (sqlite + *.bin) を同時に開いている**
- 片方が書き込み中にもう片方が読み書きすると、 sqlite の WAL は進むが HNSW の永続化が間に合わず破損

### 1.3 過去にも顕在化していなかった理由

- 旧来の bot本体側は `_should_auto_record_mem0()` で時間ベース throttle (50秒間隔) → 書き込み頻度が低かった
- 2026-05-16 朝デプロイの変更1 (Mem0 10ターンバッチ書き込み) で bot本体からの ChromaDB 書き込みサイズと頻度が変化
- MCP subprocessの読み込み中に bot本体が大きなバッチ書き込みを実行 → 並行アクセス顕在化

## 2. 解決方針

ChromaDB を **HTTPサーバーモード (chromadb run)** で常駐起動し、bot本体と MCP subprocess の両方を**HTTPクライアント経由**でアクセスさせる。これにより:

- **書き込みプロセスは ChromaDB サーバー1つ** → HNSW への並行書き込みが構造的に発生しない
- bot本体と MCP は HTTP/REST 経由でしか操作できない → ファイル直接アクセスゼロ
- ChromaDB が自前で書き込みキュー・永続化タイミングを管理 → 中途半端な状態は起きにくい

## 3. 機能要件

### 3.1 ChromaDB サーバープロセス

#### 3.1.1 起動・常駐
- **常駐プロセス**: `chromadb run --host 127.0.0.1 --port 8765 --path /home/ubuntu/discord-bot/.state/mem0_db` を systemd サービスとして起動
- **ライフサイクル**: discord-bot.service より先に起動 (依存関係 `After=chromadb.service`)
- **自動再起動**: `Restart=on-failure` でクラッシュ時自動復帰、`RestartSec=5`
- **ログ**: systemd journal に出力、loglevel info

#### 3.1.2 ネットワーク制約
- **ローカル接続のみ**: `--host 127.0.0.1` でlocalhost binding、外部からアクセス不可
- **認証**: 同一ホスト内のみのアクセスなので認証は不要 (将来 `--token` 認証を追加可能)
- **ポート**: 8765 (既存サービスとの競合確認済み)

#### 3.1.3 永続化
- **データパス**: 既存の `/home/ubuntu/discord-bot/.state/mem0_db/` を使用 (リビルド後の正常データから開始)
- **WAL設定**: ChromaDB のデフォルトWAL設定で運用 (sqlite WAL mode auto)
- **永続化タイミング**: ChromaDB 内部設定に従う (デフォルト即時flush)

#### 3.1.4 リソース制約
- **メモリ上限**: systemd `MemoryHigh=512M`, `MemoryMax=768M` (現状の bot本体 + MCP の合算より小さく抑える)
- **CPU上限**: 制限なし (検索/挿入の応答性優先)

### 3.2 クライアント側変更

#### 3.2.1 services/mem0/client.py

- `_build_config()` の `vector_store.config` を**HTTPクライアント設定**に変更:
  ```python
  "vector_store": {
      "provider": "chroma",
      "config": {
          "collection_name": "kanae_memories",
          "host": "127.0.0.1",
          "port": 8765,
      },
  }
  ```
  ※ `path` を削除して `host` + `port` 指定 → mem0/ChromaDB Python clientがHTTPモード起動
- 起動時のChromaDBサーバー到達性チェックを追加 (5秒タイムアウトの ping)
- サーバー未起動時はログに警告を出して `_mem0_instance = None` (現状の挙動と同じ)

#### 3.2.2 services/mem0/batch.py

変更なし。`add_memories()` 経由で HTTPサーバーを叩く。

#### 3.2.3 mcp_mem0_server.py

- 上記 `client.py` の変更がそのまま効く (同じ `_get_mem0()` を呼ぶため)
- 追加コード変更なし

#### 3.2.4 既存実装の不具合修正 (PoCで発覚、2026-05-16)

server モード切り替えの過程で `services/mem0/client.py` に2つの致命的バグが残っている。**bot 再起動前に必ず修正すること**。

##### A. ログ参照の KeyError (`client.py:175`)

現状コード:
```python
log.info(
    "Mem0 ready — llm=%s (OAuth) embedder=%s store=chroma(%s)",
    cfg["llm"]["config"]["model"],
    cfg["embedder"]["config"]["model"],
    cfg["vector_store"]["config"]["path"],   # ← server モードでは path キーが存在せず KeyError
)
```

server モードの `vector_cfg` には `path` キーがないため、bot 起動直後に `KeyError: 'path'` で `_get_mem0()` の except 節へ落ち、`_mem0_instance = None` になる。結果として Mem0 全機能が無効化される。

修正案 (1行):
```python
store_id = cfg["vector_store"]["config"].get("path") \
    or f'{cfg["vector_store"]["config"].get("host")}:{cfg["vector_store"]["config"].get("port")}'
log.info("Mem0 ready — llm=%s (OAuth) embedder=%s store=chroma(%s)",
         cfg["llm"]["config"]["model"], cfg["embedder"]["config"]["model"], store_id)
```

##### B. collection_name と本番データのミスマッチ (`client.py:113`) + env override 追加

現状コード:
```python
vector_cfg = {
    "collection_name": "kanae_memories",   # ← 本番 ChromaDB サーバーには存在しない
    "host": chroma_host,
    "port": chroma_port,
}
```

PoC 検証時点で 8765 サーバーに存在するコレクション:
- `kanae_memories_safe` (206件、リビルド済み正常データ)
- `mem0migrations` (0件)

このまま bot を起動すると `get_or_create_collection("kanae_memories")` が**空の新規コレクションを作成**し、リビルド済みの 206 件を完全に見失う。

**採用修正 (オプション1: client 側 + env override)**:
```python
collection_name = os.environ.get("MEM0_COLLECTION_NAME", "kanae_memories_safe")
vector_cfg = {
    "collection_name": collection_name,
    ...
}
```
- デフォルトは `kanae_memories_safe` (リビルド済みの 3695件)
- `.env` に `MEM0_COLLECTION_NAME=kanae_memories_infer` を1行追加して bot 再起動すれば infer rebuild 完了後の新コレクションに即時切替可能。env行を消せば safe にロールバック
- 影響: コード2-3行、即時ロールバック可
- リスク: 過去ログ/スクリプトで `kanae_memories` を直接参照している箇所がないか確認 (PoC時点でゼロ)

**代替案 (オプション2: サーバー側でrename)**:
- ChromaDB の `modify` API で `kanae_memories_safe` → `kanae_memories` にrename
- リスク: 1.5.9 の rename 実装が HNSW segment ID 不変であることを事前検証する必要、ダウンタイム発生
- 推奨度: 低 (オプション1で十分)

##### C. ChromaDB 再起動後の bot 側再接続 (要件4.2の補強)

`_get_mem0()` は `_init_attempted = True` フラグで再初期化を抑止しているため、ChromaDB が落ちて systemd で復帰しても bot 側は接続をリトライしない。要件4.2の「ChromaDB クラッシュ時 bot 本体は生存」は満たすが、Mem0 操作は永続的に死んだ状態になる。

修正案:
- `_init_attempted` を撤廃し、`_mem0_instance is None` のときだけ再構築 (失敗キャッシュは別タイマーで管理)
- もしくは `_init_attempted` の reset を SIGUSR1 / 起動時 / N分後のいずれかでトリガ

### 3.3 移行戦略

#### 3.3.1 段階的移行
1. **Phase A**: ChromaDB サーバー起動 (systemd unit作成、手動起動でテスト) — 2026-05-16 時点で手動起動済 (PID 1395522)
2. **Phase B**: クライアント設定をフラグで切り替え可能に (`CHROMA_MODE=server|embedded`)
   - 2026-05-16 時点で実装済。**デフォルトは `server` で先行投入されている** (`services/mem0/client.py:107`)
   - 当初の文書では「デフォルト `embedded`」と記載していたが、インシデント直後にserverを既定にして緊急対応した経緯がある。**ドキュメントを実装に合わせる方針で確定** (ロールバックは Phase B-rollback として 3.3.2 に分離)
3. **Phase C**: 手動テストで server モード動作確認 (search + add の往復、データ整合性確認) — PoC スクリプト `/tmp/poc_chroma_raw.py` / `/tmp/poc_mem0_from_config.py` で完了 (2026-05-16)
4. **Phase D**: `CHROMA_MODE=server` で bot 再起動 — **3.2.4 A/B の修正完了が前提条件**
5. **Phase E**: 数日運用して問題なしを確認後、`embedded` モードのコードを削除

#### 3.3.2 ロールバック手順
- ChromaDBサーバーが起動できない/接続エラー時: `CHROMA_MODE=embedded` を `.env` に書いて bot再起動
- 完全ロールバック: 旧 `client.py` を git checkout して bot再起動

### 3.4 監視・運用

#### 3.4.1 ヘルスチェック
- ChromaDB サーバー側の `/api/v2/heartbeat` を毎分 pingする systemd timer
  - 注: `/api/v1/heartbeat` は ChromaDB 1.5.9 で **410 Gone** を返す (PoCで実機確認)。v2 のみ使用
  - 期待レスポンス: `{"nanosecond heartbeat": <unix_ns>}` JSON
- 失敗時は journalctl にエラー出力 (PagerDuty/Slack通知は不要、journal で十分)

#### 3.4.2 メトリクス取得
- ChromaDB API レスポンス時間を bot.py の Mem0 search ログに追加 (`mem0_search_latency_ms`)
- これにより search遅延の傾向監視が可能

#### 3.4.3 バックアップ
- 毎日 03:00 JST に `mem0_db/` 全体を `mem0_db.bak.YYYY-MM-DD` に rsync コピー
- 保持期間: 7日
- 実装: cron + `systemctl stop chromadb.service && rsync && systemctl start`

## 4. 非機能要件

### 4.1 性能
- 起動時間: ChromaDB サーバー単体で 30秒以内
- 検索レイテンシ: HTTP overhead で +10〜30ms (許容範囲)
- 投入レイテンシ: HTTP overhead で +10〜30ms (許容範囲)

### 4.2 信頼性
- bot本体クラッシュは ChromaDB に影響しない (独立プロセス)
- ChromaDB クラッシュ時は bot本体側で Mem0 操作が `Connection refused` で失敗するが、 bot本体は生存
- ChromaDB systemd 自動再起動で5秒以内に復帰
- **ただし bot 側で自動再接続するには 3.2.4 C のリトライ実装が必須**。現状の `_init_attempted` フラグ方式では ChromaDB 復帰後も bot プロセスを再起動しない限り Mem0 が死んだままになる (PoCで発覚)

### 4.3 セキュリティ
- 127.0.0.1 binding のみ、外部公開なし
- mem0_db のディスク権限は ubuntu:ubuntu 0700 を維持

## 5. 実装タスク

| # | タスク | 影響ファイル | 見積もり | 状態 |
|---|---|---|---|---|
| 1 | `chromadb-server.service` systemd unit 作成 | `/etc/systemd/system/chromadb-server.service` | 30分 | 手動起動済、unit化未 (root権限要) |
| 2 | `services/mem0/client.py` の `_build_config` を HTTP モードに変更 | `services/mem0/client.py` | 15分 | **済 (2026-05-16)** |
| 3 | `CHROMA_MODE` env 切り替えフラグ実装 | `services/mem0/client.py`, `config.py` | 30分 | **済 (2026-05-16)** |
| 4 | ヘルスチェック実装 (起動時 ping) | `services/mem0/client.py` | 20分 | **済 (2026-05-16)** `_ping_chromadb_if_server_mode()` |
| 5 | 検索レイテンシログ追加 | `services/mem0/search.py` | 15分 | **済 (2026-05-16)** `mem0_search_latency_ms` |
| 6 | バックアップ cron スクリプト | `scripts/backup_mem0.sh`, `/etc/cron.d/mem0-backup` | 20分 | 未着手 (root権限要) |
| 7 | 動作確認 (search + add + bot 1メッセージ往復) | テストのみ | 30分 | PoCレベル完了、bot実機未 |
| 8 | ドキュメント更新 (architecture.md, README) | `docs/` | 15分 | 未着手 |
| **9** | **3.2.4 A: `client.py:175` のログKeyError修正** | `services/mem0/client.py` | **5分** | **済 (2026-05-16)** |
| **10** | **3.2.4 B: `collection_name` を `kanae_memories_safe` に変更** | `services/mem0/client.py:113` | **5分** | **済 (2026-05-16)** |
| **11** | **3.2.4 C: ChromaDB復帰時の自動再接続実装** | `services/mem0/client.py` (`_init_attempted` → `_init_failed_at` cooldown) | 30分 | **済 (2026-05-16)** |

**合計見積もり: 約3時間 + 追加40分 (タスク9-11)**

**実装進捗 (2026-05-16 PoC + コード修正完了)**:
- 完了: タスク 2, 3, 4, 5, 9, 10, 11 (コード変更ぶんすべて、テストも追加して通過)
- 残り: タスク 1 (systemd unit、root権限要)、タスク 6 (バックアップcron、root権限要)、タスク 7 (bot実機での1メッセージ往復)、タスク 8 (architecture.md / README更新)
- 次の手: **bot を再起動して server モードでの動作確認 (タスク7)** が最短の検証経路

## 6. 完了条件

1. `systemctl status chromadb-server.service` で active (running)
2. bot.py から `search_memories()` が正常応答 (新しいデータが見える)
3. `add_memories()` で投入したデータが直後の search でヒット
4. bot本体プロセス再起動後も ChromaDB データが永続している
5. bot本体クラッシュをシミュレートしても ChromaDB は無傷
6. 1日連続稼働してSEGV/破損なし

## 7. 既知のリスク・トレードオフ

| リスク | 影響 | 緩和策 |
|---|---|---|
| ChromaDB Python HTTPクライアントの互換性問題 | mem0/Memoryクラスが server モードを認識しないケース | Phase A の手動テスト段階で確認 |
| HTTPオーバーヘッドによる軽微な性能低下 | +10〜30ms per call | 許容範囲内、batch化で吸収 |
| サーバープロセス自体のクラッシュ | 一時的にMem0操作不可 | systemd 自動再起動 + bot本体の graceful degradation |
| ポート競合 (8765) | サーバー起動失敗 | デプロイ前に `ss -tnp` で確認 |

## 8. 補足: 今回の修復で得たデータ

リビルド前バックアップ (`mem0_db.bak.2026-05-16-segv`) には以下を保持:

- 3558件の applied embeddings (sqlite + 古いHNSW)
- 137件の unapplied queue entries (queue内BLOB)
- 計3695件のテキスト+metadata は `/tmp/mem0_extract.json` に抽出済み

### 8.1 現在の本番サーバー状態 (2026-05-16 PoC時点)

8765 サーバーには以下のコレクションが存在:
- `kanae_memories_safe`: **206件 (リビルド済み正常データ)**
- `mem0migrations`: 0件 (mem0内部メタテーブル)

`kanae_memories` コレクションは存在しない。

### 8.2 データ移行方針

当初は `scripts/rebuild_mem0_into_server.py` で `/tmp/mem0_extract.json` を server に再投入する想定だったが、PoCで本番側に `kanae_memories_safe` 206件が既に投入済みであることが判明したため、**スクリプト実装は不要**。代わりに以下のいずれかで対応:

| 方式 | 作業内容 | リスク | 推奨度 |
|---|---|---|---|
| **client側 rename** | `services/mem0/client.py:113` の `collection_name` を `kanae_memories_safe` に変更 | コード1行、即ロールバック可、参照箇所の grep だけ確認 | ★★★ |
| サーバー側 rename | ChromaDB `modify` API で `kanae_memories_safe` → `kanae_memories` にrename | 1.5.9 の rename が HNSW segment ID 不変か事前検証必要、ダウンタイム | ★ |
| データ再投入 | `rebuild_mem0_into_server.py` を実装して `/tmp/mem0_extract.json` から再投入 | 3695件投入のコスト + 重複検出ロジック実装 + 既存 206件との突合 | ★ |

**決定: client側 rename を採用** (3.2.4 B と同一の修正)。

### 8.3 残存差分の確認

`/tmp/mem0_extract.json` (3695件) と `kanae_memories_safe` (206件) の件数差 3489件は、リビルド時に dedup / 古い記憶削除で減らした正常な結果か、データロスかを別途検証する必要あり。これは要件外の追加調査タスク。
