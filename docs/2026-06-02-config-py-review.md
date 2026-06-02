# config.py レビュー (2026-06-02)

対象: msg3 タグ漏れ事故後の `config.py` 修正一式
ベースコミット: HEAD (`b762baf revert(model)` 後)
レビュー時点: 2026-06-02 13:08:48 起動の PID 604332 (claude-opus-4-7 稼働中)
関連: `2026-06-02-msg3-tag-leak-model-revert-rootcause.md`

## 総評

**5つの変更すべて筋通っている。本質的な反対意見なし。**

特筆: 全変更にコメントで「なぜ」「いつ観測」「incident ID」が併記されており、3 ヶ月後に読み返しても文脈が再構成できる。exemplary。

---

## 変更一覧と評価

### ① identity labels (`HAROIN_USER_ID` / `B0NCHICHI_USER_ID` / `USER_ROLE_LABELS` / `role_label_for()`)

**評価: ○**

- ID で「呼称・口調」を決定論的に分離する仕組み。先輩 (haroin) のみ後輩口調、それ以外の admin (b0nchichi 等) は丁寧語、という運用ルールをコード側で担保。
- 整合性確認: `.env` の `ADMIN_USER_IDS` に両 ID とも含まれており実態 OK。
- `role_label_for()` は ID 不一致時に `""` を返す defensive 設計。

### ② `CLAUDE_MODEL` コメント追加

**評価: ○**

- `b762baf` revert を将来の自分が巻き戻さないよう、経緯と「config が single source of truth」の意図を明記。
- `entry.py` / `claude_sdk.py` 側も `MODEL = CLAUDE_MODEL` で config から import する形に統一済み。**ハードコード撲滅構造として完璧**。

### ③ `STREAM_FIRST_EVENT_TIMEOUT_S=120` / `MAX_RETRIES=1`

**評価: ○**

- inter-event idle timeout (300s) と first-event hang timeout (120s) を**独立した概念**として分離。
- retry 1 回固定。outer retry とのスタック増殖を回避。

### ④ `MAX_TOOL_RESULT_CHARS=200000`

**評価: ○**

- head + tail truncate (中央 omit) は dump 系の異常検出として正しい設計。エラーログは末尾に出ることが多いので tail を残すのは妥当。
- 50k tokens 相当 = 1M context window の 1/20。比率も健全。

### ⑤ `MEM0_CLIENT_DEDUP_THRESHOLD` 0.85 → 0.20

**評価: ○**

- distance semantic の修正 (類似度ではなく距離なので「小さいほど類似」)。`ingest.py` の警告コメントと整合。
- `MEM0_DEDUP_TWO_STAGE=True` がデフォルトのため、このクライアント側 dedup はそもそも defensive only という但し書きも丁寧。

---

## 残課題 (任意)

### A. SSoT 違反: `daily_summary_dm.py:35` で `HAROIN_USER_ID` を別途ハードコード ✅ **解消済み (13:35 JST)**

**実施した修正**:
- `services/emotion/daily_summary_dm.py:35` のハードコードを削除
- `from config import HAROIN_USER_ID` に置換 → config が真の SSoT に
- 検証: `daily_summary_dm.HAROIN_USER_ID == config.HAROIN_USER_ID == 573489206240870410` で一致、import 通過、構文 OK

**残ハードコードの精査結果** — 動作経路上の SSoT 違反は **0 件**:

| 箇所 | 判定 | 理由 |
|---|---|---|
| `mcp_mem0_server.py:81 DEFAULT_USER_ID` | 触らない | 別プロセス。MCP server の namespace fallback で「先輩 identity」ではない |
| `scripts/*.py` | 触らない | argparse default / docstring。CLI 運用 script で本体経路ではない |
| `tests/test_*.py` | 触るべきでない | テスト fixture はむしろハードコードすべき (config 値依存にすると config 変更でテストが汚染される) |
| `.claude/worktrees/agent-ac8e688b5069db84d/*` | 触らない | 別 worktree の古い snapshot |

**初出時の課題記述** (履歴として):

> ```python
> # services/emotion/daily_summary_dm.py:35
> HAROIN_USER_ID = 573489206240870410   # ← config から import すべき
> ```
> config を SSoT にした思想と矛盾。今は値が一致しているので動作するが、誰かが config 側だけ変更した場合に静かに壊れる。

### B. 起動時 sanity check 不在

`HAROIN_USER_ID` / `B0NCHICHI_USER_ID` が `ADMIN_USER_IDS` に含まれていない場合、`role_label_for()` は黙って `""` を返す。

現状 `.env` が正しいので問題は表出していないが、env diff のある環境にデプロイした際「先輩なのに後輩口調にならない」が起きうる。

**修正案** (`load_dotenv` 後の位置に置く):
```python
assert HAROIN_USER_ID in ADMIN_USER_IDS, \
    "HAROIN_USER_ID must be in ADMIN_USER_IDS"
assert B0NCHICHI_USER_ID in ADMIN_USER_IDS, \
    "B0NCHICHI_USER_ID must be in ADMIN_USER_IDS"
```

assert を避けるなら warn ログでも可。

---

## msg3 タグ漏れとの関係 (重要)

**この config 変更だけでは msg3 タグ漏れは直らない。**

- config 側の貢献: `CLAUDE_MODEL=claude-opus-4-7` を固定し、4-8 を意図せず再導入する事故を防ぐ。
- 本命の対策: `tool_loop.py` 側の `_strip_text_tool_calls()` + parser (4-8 を使う未来の自分のための防御層)。

現状は **config (4-7 固定) + tool_loop (parser による text-format tool call 救済) の二重防御**。これは適切な設計。

---

## 検証ログ

- systemd: 13:08:48 起動、active (running)、PID 604332、エラーなし
- 起動時の self-test: OAuth / session / emotion daemon すべて正常
- 起動中モデル: claude-opus-4-7 (`.env` 上書きなし、config デフォルトが反映)
