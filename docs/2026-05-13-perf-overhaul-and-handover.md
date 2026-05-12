# Discord Bot パフォーマンス改善まとめ + 次担当（Codex）への引き継ぎ

- **作業日**: 2026-05-12 〜 2026-05-13
- **対象**: かなえ Discord Bot (`/home/ubuntu/discord-bot/`)
- **ベースの計画**: `~/.claude/plans/hashed-crunching-fountain.md`（8 並列の reviewer エージェントによる全コードベース監査結果 → Phase A〜F の修正プラン）
- **進め方**: 1 Phase = 1 PR、各 PR で `実装 → push → reviewer agent でレビュー → 指摘 follow-up commit → merge → master 同期` の閉ループ

---

## Part 1: 完了した修正

### マージ済 PR 一覧

| Phase | PR | Merge SHA | 主な改善 |
|-------|----|-----------|----------|
| A | #10 | `84b26cf` | 共有 render semaphore（OOM 防止）、scholarly MCP gate、async history load、`mask_code_blocks` 1 回化、redundant sanitize 削除 |
| C | #11 | `8097e7b` | mem0 dedup + raw insert を `asyncio.gather` 並列化、`AsyncAnthropic` singleton、legacy 経路の `_classify_for_metadata` 削除、`list_all` limit ChromaDB pushdown、MCP search `smart=False` デフォルト |
| E | #12 | `f9342fa` | requirements 分割（dev/prod）+ `pytest-xdist`、`integration` marker で重い test を default 除外、`weekly_mem0_compact.py` の O(n²) ループに 200 件 cap、autouse 状態リセット fixture |
| D | #13 | `9338299` | `_tail_jsonl` O(n²) byte 連結 → O(n) chunks join、`save_sessions` async offload + snapshot で dict-iteration race 回避、`LazyHistory`/`load_all_history` の dead code 削除、save error 自動 log |
| B (subset) | #14 | `16a06e4` | KaTeX をローカル `file://` に切替（CDN 撤去）、`waitUntil:'domcontentloaded'`、math は `page.goto('file://...')` で同一オリジン化、CJK font fallback（`Noto Sans CJK JP`）、`.attachignore` の mtime キャッシュ、`cleanup_attachments_async` 追加 |
| F | #15 | `7c2113d` | SDK ストリームの per-block log を DEBUG に降格、`SpooledTemporaryFile` を retry loop 外で 1 回確保 + 各 attempt で truncate、`RETRY_BACKOFF_BASE` 15s → 5s（env 上書き可・malformed guard 付き） |
| G | #16 | `cee7918` | `_SENTINEL_RE` 重複統合（`services/sentinel.py` 新設）、`context7` を `MCP_SERVERS_DOCS` に分離して `_DOCS_TRIGGERS` で gate、`cleanup_stale_attachments` を `bot.on_ready` の bg task 化（再接続ガード付き） |

### レビューサイクルで追加された主な follow-up

- Phase A: `_FACT_TEXT_BLOCKLIST` を `_SENTINEL_RE` 全パターンに拡張、`scholar` 単独トリガー削除（false positive 防止）
- Phase C: intra-batch dedup（同一テキストの fact が並列で両方 insert される race）、`AsyncAnthropic` singleton の `asyncio.Lock` 排他
- Phase D: `os.fchmod(fd, 0o600)` 既存ファイル遡及、`save_sessions` 前に `OrderedDict(sessions)` snapshot で GIL race 解消、`_on_save_done` で save error を `log.error` 化
- Phase B: CJK font fallback（Japanese tofu 回帰の修正）、math は `setContent` から `page.goto file://` へ変更（about:blank で `file://` sub-resource が CORS でブロックされる問題）
- Phase F: `CLAUDE_API_RETRY_BACKOFF_BASE` の malformed env で import crash しないよう try/except guard
- Phase G: `main.cleanup_stale_attachments` の dead import 削除、テストの ghost monkeypatch 削除、`_cleanup_scheduled` で `on_ready` 再接続時の重複起動防止

### Phase G で意図的に skip した項目

- **D4** (`config.py` import 時の `os.makedirs` × 6 + `load_system_prompt()`): 1 回限り ~5 ms のコストで、`SYSTEM_PROMPT` は `claude_sdk.py` / `claude_api.py` から module-level import されており、lazy 化すると 3 モジュールの indirection が必要。ROI 低と判断して延期。

---

## Part 2: Codex 担当範囲（さらなるパフォーマンス改善）

以下は **未着手 or 部分着手** の改善項目。優先度順。各項目は独立した PR にできるよう scope を切ってある。

### H1（最大の残課題）: Long-lived Puppeteer browser daemon

**現状**: `utils/table_renderer.py` / `utils/math_renderer.py` は呼び出しごとに `node <script>` をサブプロセスとして spawn し、その中で Chromium をフル起動 → スクリーンショット → 終了。Chromium 起動だけで **3-5 s / 回**。Phase B でローカル KaTeX + `domcontentloaded` 化したが、Chromium 起動コストは残っている。

**目的**: 1 つの長期常駐 Node プロセスが Puppeteer browser を保持し、レンダーごとに新規 page を開いて screenshot 撮影 → page だけ close。Chromium 起動コストを **初回のみ** に圧縮。

**アプローチ案**:
1. `utils/_render_daemon.js`（新規）: stdin から JSON-RPC（line-delimited JSON）を読み、`{id, htmlPath, outputPath, selector, pad, viewport}` を受け取って screenshot を撮り、`{id, ok, error?}` を stdout で返す。`browser` はモジュールスコープに保持。
2. `utils/browser_pool.py`（新規）: `BrowserDaemon` シングルトン。`async def render(...)` で daemon にリクエスト送信。idle timeout（デフォルト 5 分）でプロセスを shutdown して RAM 回収。
3. `table_renderer.py` / `math_renderer.py`: `asyncio.create_subprocess_exec(...)` を `await BrowserDaemon.render(...)` に置換。`_RENDER_SCRIPT` の埋め込み JS を `_render_daemon.js` に集約。
4. `utils/render_semaphore.py` の `render_sema` は daemon 内部の lock と二重化するので **削除可** （もしくは daemon の Lock を render_sema 経由で公開）。

**注意点**:
- VPS は 2 GB RAM。常駐 Chromium は ~300-500 MB の RSS を占める。idle timeout を**短め**（5 分 etc.）にしないとアイドル時の RAM 圧迫が問題。
- daemon クラッシュ時の自動再起動（child process exit → 次のリクエストで spawn し直し）。
- stderr のキャプチャと debug log（既存 `claude_sdk.py` の Spooled パターンが参考）。
- math と table で viewport / selector が違うので、リクエストペイロードでパラメータ化する。

**検証**:
- 表 + 数式が混ざった応答を Discord で送り、PNG が正しく生成されること
- idle で 5 分放置後、`pgrep -f _render_daemon.js` が消えていること
- 連続レンダーで Chromium が再起動されないこと（`pgrep chrome | wc -l` を before/after で観測）
- メモリ: `ps -o rss -p <daemon_pid>` で <600MB 確認

**期待効果**: per-render の wall-clock **3-5 s 削減**。1 応答に表 2 つ + 数式 3 つで合計 25 s → 5 s 程度に短縮。

---

### H2: config.py 重い import 時 I/O の遅延（旧 D4）

**現状**: `config.py` は module-level で `os.makedirs` 6 回、`load_system_prompt()` で `CLAUDE.md` を read。テスト collection でも毎回走り、開発サイクルが遅い側面。

**ROI**: 一回限り ~5 ms。優先度低。やるなら以下：

**アプローチ**:
- `os.makedirs(...)` を `_ensure_dirs()` 関数に括り出し、`main.py` / `main_async.py` の `main()` 内で呼ぶ。テスト側は `tmp_state_dir` fixture が既に monkeypatch で paths を差し替えているので、import 時 makedirs が不要になる。
- `load_system_prompt()` は callable のまま、`SYSTEM_PROMPT` を `_lazy_system_prompt()` のような遅延 getter に置換。`claude_sdk.py` / `claude_api.py` 側でアクセス時に load。

**注意**: 既存のテストが `SYSTEM_PROMPT` を直接参照していないか確認すること。

---

### H3: ChromaDB スキーマ / インデックス最適化

**現状**: Mem0 は ChromaDB の HNSW インデックスを使うが、デフォルト設定。dedup 検索（Phase C で並列化済）は引き続き embedding 計算 + HNSW 検索を fire する。

**アプローチ案**:
- `ef_search` / `M` などの HNSW パラメータを Mem0 config から制御できるか調査
- 古い記憶（tier=3 archive 候補）を別 collection に move し、ホット検索の対象から外す
- `mem0` の `MEM0_LLM_MODEL` を Sonnet 4.6 → Haiku 4.5 に揃える（既に揃っているかも）

**注意**: ChromaDB の検索品質に影響が出る可能性があるので、まず `weekly_mem0_compact.py` の `near_dup_pairs` 件数で品質を監視する仕組みが必要。

---

### H4: `pick_mcp_servers` のキーワード trigger を正規表現に統合

**現状**: `_WEB_TRIGGERS` / `_HEAVY_TRIGGERS` / `_MEMORY_TRIGGERS` / `_PAPER_TRIGGERS` / `_DOCS_TRIGGERS` をそれぞれ `any(kw in text for kw in TUPLE)` で走査。5 群 × 平均 10 キーワード = 50 個の substring scan。

**アプローチ**:
- 各群を 1 個の compiled `re.compile(r"|".join(map(re.escape, kws)))` にまとめる
- スキャン回数を 5 → 5（同じ）に保ちつつ、`re.search` のオフセット最適化で実質的に短縮
- もしくは `aho-corasick` で 1 回スキャン → どのトリガーが当たったかをまとめて返す

**ROI**: マイクロ秒オーダー。優先度最低。やる場合は `aho-corasick` 導入も検討。

---

### H5: aiofiles で JSONL append を完全 async 化

**現状**: `services/session.py:append_jsonl` は `os.open` + `os.fdopen` の sync I/O。`claude_api.py:333` から `asyncio.to_thread` 経由で呼ばれているので event loop は block しないが、worker thread を消費する。

**アプローチ**:
- `aiofiles` を導入し、`append_jsonl` を `async def` 化
- `bot.py` / `claude_api.py` から `await append_jsonl(...)` で直接 await
- worker thread pool の負荷削減

**ROI**: hot path に占める割合は小さい。チャンネル数が増えたときに効いてくる。

---

### H6: discord.py の `bot.get_channel` キャッシュ確認

**現状**: `bot.py` 内で `bot.get_channel(...)` がどこから呼ばれているか確認していない。discord.py 自体は内部キャッシュを持つので冗長な O(n) ルックアップは無いはずだが、念のため pyspy 等でプロファイリング。

**ROI**: 不明（プロファイリング次第）。

---

### H7: image attachment download に HTTP keep-alive を確認

**現状**: `utils/attachments.py` の `_download_one` は `aiohttp.ClientSession` を共有しているはず（bot.py の `get_http_session`）。各画像 DL で TLS 握り直していないか確認。

**アプローチ**:
- `_resolve_one_link` 内で **新しい** `aiohttp.ClientSession()` を作っているコードがあれば、共有セッションに変更
- セッションの connector を pre-warm（DNS / TLS handshake を起動時に済ませる）

**ROI**: 添付の多い応答で初動を 100-300 ms 短縮可能。

---

### H8: mem0ai 2.0 stable 移行（CVE 解消も同時）

**現状**: `requirements.txt` に `mem0ai>=1.0,<2.0` + `# TODO: bump to >=2.0 once stable lands (fixes CVE-2026-7597, currently only in 2.0.0b2)`。

**アプローチ**:
- 2.0 stable リリース後、`<2.0` 上限を解除して `pip install -U` 検証
- API 互換性に変更があれば `services/mem0/client.py` の `Memory.from_config(cfg)` をアップデート
- 2.0 系で並列性能 / 検索精度の改善があれば測定

**前提**: mem0ai 2.0.0 stable のリリース待ち（現時点で beta のみ）。

---

## Part 3: 効果測定の標準手順（共通）

各 H 項目を実装後、以下で検証：

1. **before / after マイクロベンチ**
   ```bash
   python -c "import timeit; print(timeit.timeit('target_func()', setup='from mod import target_func', number=100))"
   ```
2. **Discord smoke**: 実機 bot にテストメッセージを送り、応答 latency 観察
3. **メモリ**: `ps -o rss -p $(pgrep -f discord-bot)` を 1 分おきに 10 分取得し、リーク・増加傾向確認
4. **systemd journal**: `journalctl -u discord-bot.service -f --since '5 min ago'` でエラー有無確認
5. **pytest**: `pytest -n auto --tb=line -q --ignore=tests/test_claude_api.py` で回帰チェック（既存の pre-existing failure は除外）

---

## Part 4: コード規約（Codex 向け）

- **言語**: 日本語コメントは既存 codebase に合わせて適宜使用。新規 docstring は英語推奨。
- **テストポリシー**: 重い test は `@pytest.mark.integration` で marker 付与、デフォルトの `pytest` から除外。
- **PR フロー**: 既存運用と同じ `feat/perf-prX-...` ブランチ命名、`gh pr create --base master`、レビューサイクル後に `gh pr merge --squash --delete-branch`。
- **コミットメッセージ**: Conventional Commits 風（`perf(phase-h): ...` / `fix(phase-h): ...`）。Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com> は不要、Codex は自分の identity で OK。
- **依存追加時**: `requirements.txt`（prod）と `requirements-dev.txt`（test）を区別し、必ず upper bound を付ける（`>=X.Y,<X+1`）。CVE 修正の場合は inline コメント。
- **secret 取り扱い**: 既存の `.gitignore` (`.bak.*` / `.credentials.json` / `*.pem` / `*.key`) を守る。直近のセキュリティ作業 (`docs/2026-05-12-security-overhaul.md`) を一読推奨。

---

## Part 5: 前提知識ファイル

Codex が context として読むべきもの：

```
docs/2026-05-12-mem0-quality-overhaul.md   # Mem0 アーキ理解
docs/2026-05-12-security-overhaul.md       # セキュリティ修正の経緯
docs/2026-05-13-perf-overhaul-and-handover.md  # この文書
CLAUDE.md                                  # プロジェクト指針
config.py                                  # 全 env var / MCP 階層
services/mem0/                             # Mem0 モジュール群
services/claude_sdk.py                     # SDK 呼び出しのホットパス
services/claude_api.py                     # 直接 API 呼び出しのホットパス
bot.py                                     # Discord event handler 全般
utils/{table,math}_renderer.py             # Phase H1 の主戦場
utils/render_semaphore.py                  # 共有 semaphore
```

---

## Part 6: pip-audit 現状

```
$ pip-audit --strict
Found 1 known vulnerability in 1 package
Name   Version ID            Fix Versions
------ ------- ------------- ------------
mem0ai 1.0.11  CVE-2026-7597 2.0.0b2
```

残 1 件は mem0ai 2.0 stable 待ち（H8）。
