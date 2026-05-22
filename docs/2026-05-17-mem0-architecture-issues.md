# Mem0 アーキテクチャ課題と統合方針

**作成日**: 2026-05-17  
**ステータス**: 調査完了 → 実装待ち

---

## 1. 現状のデータフロー

### 1.1 user_id の3系統

| 書き込み元 | user_id | 用途 | 実データ量 |
|---|---|---|---|
| **MCP (Claude Code)** | `573489206240870410` (DEFAULT_USER_ID) | 先輩との対話記録（グローバル記憶） | ~860件 |
| **Discord bot → senpai path** | `str(message.author.id)` = `606671065616236544` | 先輩のDiscord会話から抽出した事実 | **要確認（空の可能性）** |
| **Discord bot → kanae path** | `KANAE_BOT_USER_ID` or `str(bot.user.id)` = `1489178684391817336` | かなえ自身の内的事実 | 数十件 |

### 1.2 MCP側の実装 (`mcp_mem0_server.py`)

```python
# L73
DEFAULT_USER_ID = "573489206240870410"

# 全ツール（search, add, list_all, delete）のデフォルト引数
user_id: str = DEFAULT_USER_ID
```

- Claude Codeから `mcp__mem0__search(query="...")` と呼ぶと、`user_id="573489206240870410"` で検索
- `mcp__mem0__add(...)` も同様にこのIDで保存
- **validation**: snowflake 15-22桁のみ受け付け（`_USER_ID_PATTERN`）。`"kanae_self"` のような文字列IDは弾かれる

### 1.3 Discord bot側の実装 (`bot.py`)

**先輩の記憶（senpai path）:**
```python
# L854
mem0_user_id = str(message.author.id)  # → "606671065616236544"

# L1591 (batch store)
await add_memories(
    user_text=summary_text,
    user_id=mem0_user_id,  # → "606671065616236544"
    ...
)
```

**かなえの記憶（kanae path）:**
```python
# L1607-1618
kanae_self_user_id = KANAE_BOT_USER_ID or str(bot.user.id)  # → "1489178684391817336"
await add_memories(
    user_id=kanae_self_user_id,
    subject="kanae",
    pre_extracted_facts=_batch_kanae_facts,
    ...
)
```

**検索時:**
```python
# L954 (senpai memories)
search_memories_smart(raw_user_text, mem0_user_id, limit=5)  # → "606671065616236544"

# L963-977 (kanae self memories)
search_memories_smart(q, str(bot.user.id), limit=7)  # → "1489178684391817336"
```

---

## 2. 問題点

### 問題1: MCP DEFAULT_USER_IDとDiscord bot user_idの不一致

| 経路 | 先輩の記憶に使うuser_id |
|---|---|
| Claude Code MCP | `573489206240870410` |
| Discord bot | `606671065616236544` |

**`573489206240870410`** は先輩の**旧Discord ID**または**別アカウントID**。現在のメインアカウントは `606671065616236544`。

結果:
- Claude Codeから保存した860件は `573489206240870410` に紐づいてる
- Discord botから保存した記憶は `606671065616236544` に紐づいてる
- **Discord botの初回ターンでMem0を検索しても、Claude Codeで保存した860件がヒットしない**（user_idが違うため）
- 逆に、Claude Codeからは Discord bot側で保存した記憶が見えない

### 問題2: かなえの記憶のアクセス制限

`mcp_mem0_server.py` の `_USER_ID_PATTERN` がsnowflake（15-22桁の数字）のみ受け付けるので:
- `kanae_self` → rejected
- `kanae_memory` → rejected
- `1489178684391817336` → **OK**（botのapplication ID）

Claude Codeからかなえの記憶を検索する場合、`user_id="1489178684391817336"` を明示的に渡す必要がある。

### 問題3: kanae_self state の二重管理

| ストア | パス | 内容 |
|---|---|---|
| ローカルファイル | `{BOT_DIR}/.state/kanae_self.json` | interests, episodes ring buffer |
| Mem0 ChromaDB | user_id=`1489178684391817336` | かなえの抽出された事実 |

- `kanae_self.json` は起動時に `load_state()` で読まれ、毎ターン `save_state()` で書き戻される
- Mem0のかなえ記憶は `add_memories(subject="kanae")` で保存、`search_memories_smart(q, bot.user.id)` で検索
- 両者は**独立しており同期しない**。interests/episodesはファイルのみ、事実はMem0のみ
- これ自体は意図的な設計（interests = 揮発性の関心、Mem0 = 永続的事実）なので問題ではないが、「かなえの状態」を一箇所で把握できない

---

## 3. 統合方針（提案）

### Phase 1: user_id 統一（最小変更）

**MCPサーバーのDEFAULT_USER_IDを修正:**

```python
# mcp_mem0_server.py L73
# Before:
DEFAULT_USER_ID = "573489206240870410"
# After:
DEFAULT_USER_ID = "606671065616236544"
```

**既存860件のマイグレーション:**
```python
# スクリプトで573489206240870410 → 606671065616236544 に移行
# ChromaDBのuser_idフィールドを一括更新
```

**修正ファイル:**
- `mcp_mem0_server.py` L73: DEFAULT_USER_ID変更

### Phase 2: かなえ記憶へのアクセスパス追加

MCPサーバーにkanae専用のユーティリティを追加するか、`search` ツールのdocstringにbot IDを明記:

```python
# Option A: docstringに記載（最小変更）
# search() のdocstringに以下を追加:
# 「かなえ自身の記憶を検索する場合: user_id="1489178684391817336"」

# Option B: 専用エイリアス追加
@mcp.tool()
async def search_kanae(query: str, limit: int = 5) -> str:
    """Search kanae's own memories (internal state, music, self-facts)."""
    return await search(query=query, limit=limit, user_id="1489178684391817336")
```

**推奨: Option A**（コード変更最小、先輩がClaude Codeで `user_id` を明示的に渡せばよいだけ）

### Phase 3: 旧ID記憶の統合（オプション）

573489206240870410 に紐づいた860件を606671065616236544に移行するスクリプト:

```python
# scripts/migrate_mem0_user_id.py
import asyncio
from services.mem0.client import _get_mem0

async def migrate():
    mem = _get_mem0()
    old_id = "573489206240870410"
    new_id = "606671065616236544"
    
    # 1. old_idの全記憶を取得
    old_memories = mem.get_all(user_id=old_id)
    
    # 2. new_idに同内容を書き込み（infer=Falseで生挿入）
    for m in old_memories:
        mem.add(
            [{"role": "user", "content": m["memory"]}],
            user_id=new_id,
            metadata=m.get("metadata", {}),
            infer=False,
        )
    
    # 3. 確認後、old_idの記憶を削除（手動実行）
```

**注意**: dedup有効時に重複が発生する可能性あり。dry-runで確認してから実行。

---

## 4. 修正箇所サマリ

| Phase | ファイル | 行 | 変更内容 |
|---|---|---|---|
| 1 | `mcp_mem0_server.py` | L73 | DEFAULT_USER_ID → `606671065616236544` |
| 2 | `mcp_mem0_server.py` | search() docstring | kanae user_id `1489178684391817336` を明記 |
| 3 | `scripts/migrate_mem0_user_id.py` | 新規 | 旧ID→新IDマイグレーションスクリプト |

---

## 5. 未解決事項

- [ ] `573489206240870410` は何のIDか確認（先輩の旧アカウント？別bot？）
- [ ] 860件の中にkanae記憶と混在してるものはないか確認
- [ ] Phase 3マイグレーション時のdedup戦略（2-stage LLM判定を使うか、threshold緩めで一括か）
- [ ] Discord bot側の `mem0_user_id = str(message.author.id)` が正しく `606671065616236544` を生成してるか実環境ログで確認
