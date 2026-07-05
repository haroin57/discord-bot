# Compaction 改修計画 — u1「Claude Code の compact」記事を踏まえて

- 作成: 2026-07-05 / 起案: かなえ / 依頼: haroin
- 状態: **計画案（未着手・要承認）**

## 1. 背景

u1 の X Article（`x.com/i/article/2073286645626060800`、本文は `/tmp/discord_bot_attachments/u1dl/テキスト.txt` に取得済み）の要旨:

- Claude Code 標準 compact（手動 `/compact` + 90–95% 自動）は会話履歴を LLM で自然文要約し、要約＋直近ターン＋system で再構築する。
- 要約は「作業ログ（何をやったか）」であって「作業指示（次に何をすべきか）」ではない。**判断構造（なぜその案を却下したか／今どのフェーズか／確立した原則）が薄まる**。非可逆。
- 結果、圧縮後に構造的な事故: ①検証前デプロイ ②却下済みアプローチの再実行 ③設計原則の失念 ④タスク目的の取り違え。
- 記事の対策（CLI前提）: (1) 圧縮前に判断構造を state file へ構造化保存する skill、(2) PostCompact→UserPromptSubmit の2段 hook で圧縮直後に復旧指示を one-shot 注入、(3) 60% で手動 compact を促す通知。

**翻訳課題**: 記事は Claude Code CLI（hook/skill/slash command/対話UX/1M context）が前提。うちの bot は非対話・自動圧縮のみ・独自 Python 実装。**仕組みは移植不可、思想を翻訳する**。

## 2. 現行実装（3層）

- **microcompact** — `services/claude_api/microcompact.py`。LLM 無し。30分アイドルで古い `tool_result` 本文を placeholder 化、直近3件 verbatim。
- **boundary compaction** — `services/claude_api/compaction.py::compact_channel_history`。公式CLI準拠の9セクション要約（`COMPACT_INSTRUCTIONS`/`COMPACT_SYSTEM_PROMPT`）を Opus 4.8・effort=max で生成 → `is_compact_summary` 行に畳む。keep tail 50K字 verbatim。3連続失敗でサーキットブレーカー。
- **in-loop proactive** — `tool_loop.py:2263`（ADR-0012）。token 500K 超で tool_loop の**ターン途中**に畳む。rapid-refill breaker。
- 圧縮後は `CONTINUATION_PREAMBLE`（"This session is being continued..."）のみ。

現行値: `HISTORY_COMPACT_TRIGGER_TOKENS=500000` / `PROACTIVE_COMPACT_TRIGGER_TOKENS=500000` / `HISTORY_COMPACT_KEEP_CHARS=50000` / 1M context beta on。

## 3. 記事 vs 現行のギャップ（核心2つ）

**G1: 要約に「却下した案とその理由」が無い。**
9セクション = Primary Request / Key Concepts / Files / Errors / Problem Solving / All user messages / Pending Tasks / Current Work / Optional Next Step。判断構造（却下案・理由・制約・フェーズ）を独立セクションで要求していない。→ 却下済みアプローチの再提案が構造的に起きうる。

**G2: 圧縮後の行動規律が無い。**
`CONTINUATION_PREAMBLE` は継続告知のみ。「サマリは作業ログで行動指示ではない／Next Step は仮説／検証を飛ばすな／却下案を再提案するな」の再注入が皆無。しかも9番目 "Optional Next Step" をそのまま持ち、記事が名指しした「next step を行動指示と誤認」の罠を踏んでいる。

## 4. 外部レビュー（GPT-5 / Gemini 3.1 Pro / DeepSeek V4）要旨

**3モデル一致点:**
- **優先順位は (I) > (II) > (III)**。(I) は最安・即効・低リスクで根本課題を直撃。
- **(III) でターン途中圧縮を完全に捨てるな**（3モデルが強く警告）。単一 tool_result が 1M 超級だとターン境界前に context overflow → プロセスクラッシュ＝文脈全損。デフォルトはターン境界圧縮、緊急時のみ途中圧縮を**サーキットブレーカー**として残すハイブリッド（二段階閾値）。
- **(II) はフラグ消し忘れ→「健忘症の固定化」に注意**。必ず LLM リクエスト送出前にフラグ消去（fail-open）、排他、epoch/TTL。preamble 単独依存せず system 層 + 要約テンプレ + tool ループ短縮版で三段冗長化。

**相違点:**
- GPT は (III) に map-reduce 増分要約 + backpressure まで要求（重い）。Gemini/DeepSeek は二段階閾値 + tool 結果 truncate 強化の現実路線。→ **現実路線を採用**、増分要約は将来オプション。
- 複数ユーザー同一チャンネルでの (II) 注入スコープは環境依存（DeepSeek: 一人部屋なら不問）。→ 未決事項へ。

## 5. 変更計画（3フェーズ）

### Phase 1 — 要約プロンプト強化【最優先・低リスク・即日】
目的: G1 を解消。判断構造を要約に焼き込む。
変更:
- `compaction.py` の `COMPACT_INSTRUCTIONS`/`COMPACT_SYSTEM_PROMPT` の9セクションに追加:
  - **Rejected Approaches & Why**（却下案／却下理由／抵触した制約）
  - **Active Constraints & Invariants**（合意済み原則・禁則・セキュリティ・非機能要件）
- "Optional Next Step" → **"Next Step (Hypothesis only)"** に改名し「行動指示ではない」と明記。
- "All user messages": 却下の根拠になったユーザー発話は要約せず原文断片で保持を指示。

検証: 長い会話で実 compact を発火、要約に2新セクションが出るか。既存の圧縮系テストを流用。
ロールバック: 定数を戻すだけ（プロンプトのみ、ランタイム影響なし）。リスク最小。

### Phase 2 — 圧縮後 one-shot 復旧指示注入【中リスク】
目的: G2 を解消。圧縮直後の1ターンだけ行動規律を強制。
設計（bot 版 marker = epoch）:
- `SessionInfo` に `compact_epoch`（圧縮ごとに ++）と consume 状態を持たせる。
- `compact_channel_history` 成功時に `compact_epoch++`。
- `entry.py` のプロンプト構築時、未 consume の epoch があれば **system prefix + preamble** に復旧指示を注入し、**LLM 送出前に consume マーク**（健忘症ループ防止 / fail-open / try-finally）。
- 注入文（3点に限定、CLI固有の plan mode 等は入れない）:
  1. この要約は作業ログであり行動指示ではない。Next Step は仮説。
  2. plan / rules / 検証手順が正。却下済み案を再提案するな。
  3. 破壊的・不可逆操作の前は停止して確認。
- チャンネル単位 `asyncio.Lock` で Gateway 同時配送の競合回避。

検証: 圧縮直後ターンで注入 → 2ターン目で消える → 別ユーザー割り込みで健忘症ループにならない。
ロールバック: epoch 注入を feature flag（env）で無効化。

### Phase 3 — proactive compaction の二段階化【設計変更大・後日】
目的: (III)。品質（境界圧縮）と安全（overflow 回避）を両立。
変更:
- `PROACTIVE_COMPACT_SOFT_TOKENS`（例 500–600K）: 超過で「次のターン境界で boundary compaction 予約」フラグ。ターン完了後に圧縮。
- `PROACTIVE_COMPACT_HARD_TOKENS`（例 実効窓の ~95% ≈ 950K）: 超過で現行通りターン途中の緊急圧縮を維持（クラッシュ回避のブレーカー）。
- tool 結果 truncate 強化: `_cap_tool_result_content` の閾値を見直し、巨大 tool_result が素通りしないようにする。
- 対象: `config`（二段階閾値）/ `tool_loop.py`（発火分岐）/ `entry.py`（ターン境界フック）。

検証: soft 超過が境界圧縮になるか、hard 超過（巨大 tool_result 注入）で途中圧縮が発火しクラッシュしないか。
リスク: 中〜大。Phase 1/2 の効果測定後に着手。

## 6. 未決事項（要 haroin 判断）

1. **Phase 2 の注入スコープ**: 複数 admin 同居チャンネルで、圧縮を引き起こしたユーザーと次発言者が違う場合、復旧指示を出すか。案: 復旧指示は無害寄りの一般規律なので**発言者を問わず次1ターンで消費**で十分（ユーザーフィルタは複雑度に見合わない）。要承認。
2. **Phase 3 の soft/hard 閾値の具体値**。1M context 前提でよいか（200K に落ちる構成はもう無いか）。
3. Phase 3 の増分要約（GPT 案）まで踏み込むか、当面は二段階閾値 + truncate で止めるか。→ 当面止める案。

## 7. 段取り

Phase 1 → 効果確認 → Phase 2 → 効果確認 → Phase 3。各フェーズ着手時に ADR を切る（Phase 1/2 は ADR 追記、Phase 3 は ADR-0012 の改訂 or 新 ADR）。

## 参考
- 記事全文: `/tmp/discord_bot_attachments/u1dl/テキスト.txt`
- 現行: `services/claude_api/compaction.py` / `microcompact.py` / `tool_loop.py:2263` / `services/session.py`（`_apply_compact_summary:768`）/ `config/__init__.py:225-296`
