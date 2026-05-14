# Discord Bot メモリ安全性・効率改善プラン

- **作成日**: 2026-05-14
- **対象**: `/home/ubuntu/discord-bot` 全体
- **制約**: Ubuntu 2GB RAM、24/7 稼働
- **目的**: 長時間稼働下でのメモリリーク・OOM 回避と、メモリピークの抑制
- **調査手法**: サブエージェント (`Explore`) 3 体並列走査 + 検証 read

---

## 0. 全体方針

このプランは「現状の動作で困っているわけではないが、長時間稼働下で潜在リスクのある箇所を予防的に潰す」位置付け。
**今すぐ動作不能になる問題はゼロ**。修正は影響範囲の小さいものから段階的に投入する。

実装順序の原則:
1. **P0 (本セッション)**: 修正量が小さく、影響範囲が局所的なもの
2. **P1 (今週中)**: 設計的な小変更が必要なもの
3. **P2 (時間ができたら)**: 重い書き換えや、低頻度パスのもの

---

## 1. 調査結果の訂正（誤検知）

サブエージェントの初期報告に含まれていた以下 2 項目は、実コードを確認した結果**既に対処済み**だった。プランからは除外する。

| # | 当初報告 | 実態 | 検証 |
|---|----------|------|------|
| 誤1 | `_http_session.close()` 呼び出し漏れで OOM リスク | [bot.py:376-378](../bot.py#L376-L378) の `_patched_close()` 内で確実に close されている | `grep -n "_http_session.close" bot.py` |
| 誤2 | `_purge_cooldowns()` がどこからも呼ばれていない | [bot.py:682](../bot.py#L682) で `on_message` ハンドラから毎メッセージ呼ばれている | `grep -rn "_purge_cooldowns()" --include="*.py"` |

教訓: エージェントの `read excerpts` ベースの調査は、定義箇所しか見ずに「呼び出し漏れ」と早合点することがある。**呼び出しの有無は必ず grep で再検証する**。

---

## 2. 確認された問題リスト

| Rank | # | 場所 | 種別 | 想定リスク |
|------|---|------|------|-----------|
| P0 | 1 | `services/claude_api/entry.py` 画像 base64 化 | メモリピーク | 最大 ~106 MB 一時ピーク |
| P0 | 2 | `bot.py` `_channel_semas` LRU evict バグ | メモリ累積 | locked sema 過剰時に上限を破る |
| P1 | 3 | `bot.py` `_daily_quota` O(n) filter | GC 圧 | 高頻度パスで毎回 list 再構築 |
| P1 | 4 | `scripts/weekly_mem0_compact.py` Mem0 一括読み込み | メモリピーク | 10000 件 × ~2KB = ~20MB ピーク |
| P1 | 5 | `scripts/kanae_daily.py` × emotion daemon の state file 競合 | データ破損 | race condition |
| P2 | 6 | `services/vscode_sync/aggregator.py` `_buffers` unbounded | メモリ累積 | 10〜50 MB 漸増 |
| P2 | 7 | `scripts/ingest_channel_history.py` 履歴全件保持 | メモリピーク | 500 KB〜数 MB |
| P2 | 8 | `services/mem0/search.py` smart search siblings 候補膨張 | LLM コスト + メモリ | rerank ペイロード増 |

---

## 3. 詳細仕様

### P0-1: 画像 base64 化ピークの抑制

**場所**: [services/claude_api/entry.py:109-201](../services/claude_api/entry.py#L109-L201)

**現状**:
```python
_IMAGE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_IMAGE_MAX_COUNT = 8

# 各画像で:
with open(real, "rb") as fh:
    data = fh.read(_IMAGE_MAX_BYTES + 1)  # raw bytes
...
"data": base64.b64encode(data).decode("ascii"),  # 1.33x に膨張
```

**問題**:
- 1 画像あたり最悪 10 MB raw → base64 で 13.3 MB の str を生成
- それを最大 8 枚同時に保持 → **理論ピーク ~106 MB**
- さらに Anthropic SDK 側でも payload を持つため、実効ピークは倍近くになる可能性
- 2 GB 環境ではこれだけで瞬間的に 5% 以上のメモリを食う

**修正案**:

1. **上限を 10 MB → 3 MB に引き下げ**（最も即効）
   ```python
   _IMAGE_MAX_BYTES = 3 * 1024 * 1024  # 3 MB; base64 後でも 4 MB / 枚
   ```
   - Discord 添付画像は通常 1〜2 MB なので実害なし
   - 8 枚同時で最悪 32 MB ピーク（現状の 1/3）

2. **同時画像数を 8 → 4 に絞る**（補助）
   ```python
   _IMAGE_MAX_COUNT = 4
   ```
   - 1 メッセージで 5 枚以上画像を貼るユースケースは稀

3. **base64 化したら raw bytes 参照を切る**
   ```python
   encoded = base64.b64encode(data).decode("ascii")
   del data  # 明示的に GC させる
   blocks.append({"type": "image", "source": {..., "data": encoded}})
   ```
   - 関数スコープ終了でどうせ消えるが、`blocks` を build する間の重複保持を防ぐ

**検証**:
- 単体テスト: `tests/test_claude_api_entry.py` で `_IMAGE_MAX_BYTES` を一時的に低くして拒否動作確認
- 手動: 4 MB 画像を Discord に貼って rejected ログが出ることを確認

**影響範囲**: claude_api/entry のみ。挙動変更は「より大きい画像が rejected されるようになる」だけ。

**修正量**: 5 行

---

### P0-2: `_channel_semas` LRU evict バグ

**場所**: [bot.py:152-165](../bot.py#L152-L165)

**現状**:
```python
def _get_channel_sema(channel_id: int) -> asyncio.Semaphore:
    sema = _channel_semas.get(channel_id)
    if sema is None:
        sema = asyncio.Semaphore(1)
        _channel_semas[channel_id] = sema
    else:
        _channel_semas.move_to_end(channel_id)
    while len(_channel_semas) > MAX_CHANNEL_SEMAS:
        oldest_id, oldest_sema = next(iter(_channel_semas.items()))
        if oldest_sema.locked() or oldest_id == channel_id:
            break  # ← ここで evict が止まる
        _channel_semas.pop(oldest_id, None)
    return sema
```

**問題**:
- `break` ではなく、locked な oldest が出ただけで evict ループ全体を中断している
- 先頭が locked のままだと、後ろにある idle sema が evict されない
- 多数のチャンネルで並列に処理が走っている場合、`_channel_semas` のサイズが `MAX_CHANNEL_SEMAS=500` を超えて成長する可能性

**修正案**:

`break` を `continue` に変えるだけだと無限ループになるので、走査済みインデックスを進める方式に書き換える:

```python
def _get_channel_sema(channel_id: int) -> asyncio.Semaphore:
    sema = _channel_semas.get(channel_id)
    if sema is None:
        sema = asyncio.Semaphore(1)
        _channel_semas[channel_id] = sema
    else:
        _channel_semas.move_to_end(channel_id)

    if len(_channel_semas) > MAX_CHANNEL_SEMAS:
        # Evict idle entries from the head. Skip locked ones and the
        # entry we just touched. Bounded by the over-limit count so we
        # don't walk the whole dict every call.
        over = len(_channel_semas) - MAX_CHANNEL_SEMAS
        evicted = 0
        for oldest_id in list(_channel_semas.keys()):
            if evicted >= over:
                break
            if oldest_id == channel_id:
                continue
            oldest_sema = _channel_semas[oldest_id]
            if oldest_sema.locked():
                continue
            _channel_semas.pop(oldest_id, None)
            evicted += 1
    return sema
```

注意点:
- `list(...keys())` でスナップショットを取り、走査中の mutation を回避
- `over` 件 evict したら止めるので、最悪 1 関数呼び出しあたり O(MAX_CHANNEL_SEMAS) で済む
- locked が大量にあるケースでも、新規 sema が増える限り走査するため、いずれは収束する

**検証**:
- `tests/test_bot.py` に追加: 500 個の locked sema を作って 501 個目を要求し、`len(_channel_semas) <= MAX_CHANNEL_SEMAS + 1` 程度に収まることを確認
- 既存テストの `_get_channel_sema` 関連が壊れないか確認

**影響範囲**: bot.py の単一関数のみ

**修正量**: 15 行

---

### P1-3: `_daily_quota` の O(n) filter を deque + sentinel 化

**場所**: [bot.py:208-228](../bot.py#L208-L228)

**現状**:
```python
_daily_quota: OrderedDict[int, list[float]] = OrderedDict()

def _check_daily_quota(user_id: int) -> tuple[bool, int]:
    now = time.time()
    bucket = _daily_quota.get(user_id, [])
    bucket = [t for t in bucket if now - t < _QUOTA_WINDOW_S]  # ← O(n) 毎回
    if len(bucket) >= RESTRICTED_DAILY_QUOTA:
        ...
    bucket.append(now)
    _daily_quota[user_id] = bucket
    ...
```

**問題**:
- `RESTRICTED_DAILY_QUOTA` はせいぜい数十〜数百だが、毎メッセージで list comprehension が走り、新しい list が作られる
- GC pressure が地味に積もる
- 24h window なので、bucket 内の要素は時系列順 → 先頭から expired を捨てるだけで済むはず

**修正案**: `collections.deque` に置き換え、左から expired を popleft する:

```python
from collections import deque

_daily_quota: OrderedDict[int, deque[float]] = OrderedDict()

def _check_daily_quota(user_id: int) -> tuple[bool, int]:
    if RESTRICTED_DAILY_QUOTA <= 0:
        return True, -1
    now = time.time()
    cutoff = now - _QUOTA_WINDOW_S
    bucket = _daily_quota.get(user_id)
    if bucket is None:
        bucket = deque()
        _daily_quota[user_id] = bucket
    # popleft expired entries (deque is ordered by insertion)
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= RESTRICTED_DAILY_QUOTA:
        return False, 0
    bucket.append(now)
    _daily_quota.move_to_end(user_id)
    while len(_daily_quota) > MAX_COOLDOWN_ENTRIES:
        _daily_quota.popitem(last=False)
    return True, RESTRICTED_DAILY_QUOTA - len(bucket)
```

**メリット**:
- expired 削除が O(削除件数) ＝平均 1 回呼び出しあたり ~1 件
- 新規 list 確保なし
- 既存 API シグネチャ変更なし

**検証**:
- `tests/test_bot.py` の quota 関連テストをそのまま流す（deque は list と挙動互換）
- 追加: 1000 タイムスタンプを 30h 分仕込んで `_check_daily_quota` 1 回呼んだ後の bucket サイズが期待通り縮むことを確認

**影響範囲**: bot.py の `_check_daily_quota` のみ

**修正量**: 20 行

---

### P1-4: `weekly_mem0_compact.py` の Mem0 一括読み込みをチャンク化

**場所**: [scripts/weekly_mem0_compact.py](../scripts/weekly_mem0_compact.py) ~line 98

**現状**:
```python
raw = mem.get_all(user_id=args.user_id, limit=10000)
memories = raw.get("results", [])
for m in memories:
    # 全件をメモリに乗せたまま反復
```

**問題**:
- 1 件 ~2 KB の memory が 10000 件 → ~20 MB が compactor 実行中に常駐
- 単発 cron なので致命的ではないが、kanae_daily, emotion_tick との同時実行時にメモリ圧迫の可能性

**修正案**: pagination でチャンク処理する。

```python
PAGE = 500

async def iter_all_memories(mem, user_id: str):
    offset = 0
    while True:
        page = await asyncio.to_thread(
            mem.get_all, user_id=user_id, limit=PAGE, offset=offset
        )
        results = page.get("results", []) if isinstance(page, dict) else page
        if not results:
            return
        for m in results:
            yield m
        if len(results) < PAGE:
            return
        offset += PAGE
```

**事前確認が必要**:
- `mem0.Memory.get_all()` が `offset` をサポートしているか SDK ソースで確認（mem0ai のバージョンによる）
- サポートしていなければ、`search()` ベースの page 化に切り替え or 既存のまま据え置き

**検証**:
- compact 前後の memory 件数が一致することを確認（pagination 漏れ検出）
- メモリ profile: `mprof run scripts/weekly_mem0_compact.py --user-id <id>`

**影響範囲**: scripts/weekly_mem0_compact.py のみ。週次 cron で動作。

**修正量**: 30 行

---

### P1-5: `kanae_daily.py` × emotion daemon の state file 競合

**場所**: [scripts/kanae_daily.py](../scripts/kanae_daily.py) と [services/emotion/persistent_session.py](../services/emotion/persistent_session.py)

**現状**:
- bot.py 内で `EmotionSessionDaemon` が `_silence_tick_loop()` から `.state/kanae_emotion.json` を読み書きする
- 並行して `scripts/kanae_daily.py` も同じ state directory を触る（特に `kanae_daily.json`、`kanae_summary.json`）
- emotion state file は `services/emotion/lock.py` の file lock で守られているが、`kanae_daily.py` 側がこの lock を使っているかは要確認

**問題**:
- bot daemon が `_save_under_lock` で書き込み中に、cron 側が同じ state を読むと中途半端な JSON を読む可能性
- 厳密には `kanae_daily.json` と `kanae_emotion.json` は別ファイルなので影響は限定的だが、summary 共有部分があれば破損リスクあり

**修正案**:

1. `kanae_daily.py` が触るファイルを列挙し、`flock` が必要なものを特定
2. `services/emotion/lock.py` の `acquire_state_lock(state_dir, filename, timeout)` を kanae_daily.py 側でも使う
   ```python
   from services.emotion.lock import acquire_state_lock
   with acquire_state_lock(state_dir, "kanae_daily.json", timeout=5.0):
       daily = load_daily(state_dir)
       daily.entries.append(new_entry)
       save_daily(state_dir, daily)
   ```
3. atomic write も併用: `Path(tmp).replace(target)` で os-level atomicity を担保

**検証**:
- pytest で `kanae_daily.run()` と silence tick を並列実行し、state file が壊れないことを確認
- 既存の `tests/test_emotion_feedback.py` 流用可能

**影響範囲**: scripts/kanae_daily.py, services/emotion/daily.py

**修正量**: 30〜50 行

**注**: 実装前にまず**実際に書き込み競合する箇所があるか**を grep で確認すべき。なければ P2 降格。

---

### P2-6: vscode_sync `_buffers` の LRU 化

**場所**: [services/vscode_sync/aggregator.py](../services/vscode_sync/aggregator.py) ~line 84

**現状**:
```python
self._buffers: dict[str, _SessionBuffer] = {}
self._sessions_seen: set[str] = set()
```

**問題**:
- Claude IDE セッションごとに `_SessionBuffer` を保持
- flush 後も `_sessions_seen` (set) は成長し続ける
- 多数の新規セッションが立つと 10〜50 MB スケールで漸増

**修正案**:
- `_buffers` を `collections.OrderedDict` にして、サイズ上限到達時に LRU evict
- `_sessions_seen` を bounded set にする（例: 最新 1000 セッション ID のみ deque で保持し、`in` チェックに使う）

```python
from collections import OrderedDict, deque

MAX_BUFFERS = 100
MAX_SEEN = 1000

self._buffers: OrderedDict[str, _SessionBuffer] = OrderedDict()
self._sessions_seen: deque[str] = deque(maxlen=MAX_SEEN)
self._sessions_seen_set: set[str] = set()

# add に際して:
if session_id not in self._sessions_seen_set:
    if len(self._sessions_seen) == MAX_SEEN:
        evicted = self._sessions_seen[0]
        self._sessions_seen_set.discard(evicted)
    self._sessions_seen.append(session_id)
    self._sessions_seen_set.add(session_id)
```

**検証**:
- 1500 セッション ID 流し込んでも `_sessions_seen` が 1000 にとどまることを確認

**影響範囲**: services/vscode_sync/aggregator.py のみ

**修正量**: 30 行

---

### P2-7: `ingest_channel_history.py` のストリーム化

**場所**: [scripts/ingest_channel_history.py](../scripts/ingest_channel_history.py) line 40-87

**現状**:
```python
async for msg in channel.history(limit=limit, oldest_first=True):
    messages.append(msg)  # 全件メモリに蓄積

for i, msg in enumerate(messages):
    for j in range(i + 1, min(i + 5, len(messages))):
        ...
```

**問題**:
- 500 件のメッセージを全部メモリに乗せた上で O(n × 5) のペア探索
- Discord Message オブジェクトは添付・embed・reaction を抱えるため 1 件 ~1〜5 KB
- 500 件で 1〜2 MB、5000 件指定すると 10〜25 MB

**修正案**: `collections.deque(maxlen=N)` で sliding window を持ち、reply ペアを on-the-fly で yield する。

```python
from collections import deque

async def iter_pairs(channel, limit: int, target_user_id: int):
    window: deque = deque(maxlen=5)
    async for msg in channel.history(limit=limit, oldest_first=True):
        window.append(msg)
        if msg.author.id == target_user_id and len(window) >= 2:
            for later in list(window)[1:]:
                if _is_bot_reply(later, msg):
                    yield (msg, later)
                    break
```

**検証**:
- 既存の ingest 結果と新ストリーム版の結果が同じペア集合を返すこと

**影響範囲**: scripts/ingest_channel_history.py のみ

**修正量**: 50 行

---

### P2-8: smart search siblings の per_entity を 1 に絞る

**場所**: [services/mem0/search.py:254-319](../services/mem0/search.py#L254-L319)

**現状**: graph expansion で `top_seed=3`, `per_entity=3` → 最大 9 件の sibling を取得して rerank に渡す

**問題**:
- rerank ペイロードに 9 件追加される → LLM token cost 増、prompt size 増
- メモリ的には数 KB 程度なので、コストの方が問題

**修正案**:
```python
SMART_SEARCH_PER_ENTITY = 1  # was 3
```
- top seed 3 × per entity 1 = +3 件のシブリング。rerank ペイロード 1/3。
- ユーザー体験への影響は要 A/B 確認（精度低下するなら戻す）

**検証**:
- 既存の smart search テストが通ること
- 手動で「過去の話題を覚えてる？」系のクエリを投げて recall が落ちていないか確認

**影響範囲**: services/mem0/search.py のみ。設定変更レベル。

**修正量**: 1 行

---

## 4. 実装ロードマップ

| Phase | 含まれる項目 | 工数目安 | 順序 |
|-------|-------------|---------|------|
| Phase 1 (本セッション可) | P0-1 画像上限、P0-2 sema evict、P2-8 smart search | 1 時間 | 修正量小、即効性高 |
| Phase 2 (今週中) | P1-3 deque 化、P1-4 mem0 chunk | 2 時間 | テスト書き直しあり |
| Phase 3 (調査後) | P1-5 kanae_daily lock | 1〜3 時間 | 競合実態確認 → 実装 |
| Phase 4 (時間できたら) | P2-6 vscode_sync、P2-7 ingest stream | 2 時間 | 低頻度パスなので後回しでよい |

各 Phase 完了後に `git commit` を切り、メモリプロファイル (`memory_profiler` or `tracemalloc`) で改善前後を比較する。

---

## 5. 監視と継続改善

修正完了後、以下を継続監視する:

1. **bot プロセスの RSS 推移**
   - `ps -o rss= -p $(pgrep -f "python.*main.py")` を 1 時間毎に記録
   - 24h 稼働で RSS が monotonic に増え続けるなら未検出のリークあり

2. **`_channel_semas`, `cooldowns`, `_daily_quota` のサイズ**
   - bot に `/health` 系のエンドポイントがあれば、これらのサイズを露出
   - なければ `kill -USR1` ハンドラでダンプ

3. **画像処理ピーク**
   - `tracemalloc.start()` を debug mode で有効化し、画像処理関数の peak を記録

---

## 6. 補足: 検査済で問題なしの箇所

以下はサブエージェント調査で issue を立てたが、実コードを読んだ結果、現状で十分な品質と判断:

- `services/session.py:224-284` `_tail_jsonl` — 末尾チャンク読み、O(削除件数) で最適化済
- `services/emotion/persistent_session.py` — 単一プロセス常駐 + file source-of-truth で 2GB 環境に最適
- `services/mem0/client.py` embedder — OpenAI API 経由なのでプロセス内常駐なし
- discord.py `max_messages=200` — 既に default 1000 から削減済
- MCP subprocess は `AsyncExitStack` 管理、Puppeteer は `BrowserDaemon` 単一プロセス化済
- `utils/attachments.cleanup_stale_attachments()` 1h cleanup — 適切
- session save tasks の `_save_tasks` set — done callback で除去されており実害なし
- `services/claude_api/session_store.py` `_session_timestamps` — 30min TTL ありで実害なし

---

## 7. 関連 ADR / 過去のドキュメント

- `docs/2026-05-13-perf-overhaul-and-handover.md` — 過去のパフォーマンス改善履歴
- `docs/2026-05-13-sre-handover.md` — SRE 観点での既存改善
- `~/.claude/CLAUDE.md` — Puppeteer 同時 1 プロセス制約の出所
- `CLAUDE.md` (project) — 2GB RAM 制約の明記
