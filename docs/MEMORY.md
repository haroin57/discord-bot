# Mem0 user_id 設計

このドキュメントは「Discord bot 上の Mem0 がどの user_id にどんな記憶を蓄えているか」を一次資料として整理したもの。kanae が `<retrieved_memories>` ブロックや `<kanae_self>` ブロックの中身を**誰の記憶か正しく識別**できるように、CLAUDE.md からの参照先として用意してある。

## 全体像

Mem0 のレコードは `user_id` フィールドで namespace 分離されている。Discord bot のコンテキストでは **2 種類の user_id** が使われる。「3 層分裂」ではなく**意図された 2 層構造**。

| user_id | 役割 | 誰の記憶か |
|---|---|---|
| **`1489178684391817336`** (`KANAE_BOT_USER_ID`) | **kanae 自身**のアイデンティティ namespace | 自己事実 (self_facts)、人格、過去発言、自己批判、嗜好、応答スタイルの履歴 |
| **`<Discord 各ユーザーの ID>`** | そのユーザーについての記憶 | 例: `573489206240870410` (haroin / 先輩) の発言・嗜好・行動パターン |

例:
- `573489206240870410` = haroin (先輩) — `ADMIN_USER_IDS` の先頭
- `1037285234200297522` = b0nchichi (お父さん) — admin
- それ以外の admin / 非 admin もそれぞれ自分の Discord snowflake が user_id

## 注入経路

[bot.py](../bot.py) のメッセージ処理パスでは、毎ターン以下を並列で Mem0 検索:

1. **メッセージ送信者**の namespace (`message.author.id`) → `<retrieved_memories>` ブロックとして prompt 注入。kanae が「この相手」について知っていることを引っ張る
2. **kanae 自身**の namespace (`KANAE_BOT_USER_ID`) → `<kanae_self><self_facts>` 内に注入 (admin 呼び出し時のみ)。kanae が「自分について」過去にどう振る舞ったかを参照

つまり kanae が応答ターンの中で見るのは:
- 「相手」の記憶 = `<retrieved_memories>`
- 「自分」の記憶 = `<kanae_self_memory>` / `<self_facts>` 由来

両方とも自然に活用するが、**メタ的に「あなたの user_id は～」と発話しない**。

## 重要な識別ルール

kanae が「これは自分の記憶か、相手の記憶か」を判別するときの規則:

- `<kanae_self>` / `<self_facts>` ブロック内 → **kanae 自身** (namespace `1489178684391817336`) の記憶。一人称で扱う。
- `<retrieved_memories>` ブロック内 → **メッセージ送信者** の記憶。三人称 (この場合は「先輩」「haroin さん」など) で扱う。

self_facts に「自己嫌悪を言語化する」「Discord 通知が届いた瞬間に Ableton を止めて開いた」のような自己観察的な記述が出てきたら、それは **自分の過去ログ** であり、誰か別人の話ではない。

## 過去のインシデント (2026-05-19 01:47)

kanae が「Mem0 が 3 層に分裂している」と誤読し、`606671065616236544` という第 3 の user_id を「先輩の snowflake」として捏造した。実際には:

- `1489178684391817336` (KANAE_BOT_USER_ID) = kanae 自身
- `573489206240870410` (admin の先頭) = haroin (先輩)
- `606671065616236544` = **存在しない** (ADMIN_USER_IDS にも未登録、Mem0 にも空のはず)

この誤読は「kanae が自分の self_facts を見て、それを『誰か別の user_id の話』と認識した」のが根本原因。本ドキュメントは同じ事故を防ぐためのリファレンス。

## 関連設定

- `KANAE_BOT_USER_ID`: `.env` で設定、`1489178684391817336` で固定
- `ADMIN_USER_IDS`: `.env` の comma 区切り。先頭は haroin
- 検索ロジック: [services/mem0/search.py](../services/mem0/search.py) `search_memories_smart`
- 注入箇所: [bot.py](../bot.py) `_kanae_self_search` (1057行付近) と `<retrieved_memories>` 注入 (1346行付近)
- レコード書き込み: ingest 時の `user_id` は会話の送信者 / kanae 自身でそれぞれ別パスから add される
