# tool loop eval — baseline 記録（smoke, k=1, 2026-07-02）

熟考パイプライン導入前の現行アーキテクチャ計測。設計は
docs/2026-07-02-tool-loop-eval-design.md、ハーネスは scripts/eval_tool_loop.py。

- 実行モデル: claude-fable-5（effort=max, .env 既定）
- judge: claude-opus-4-8（rubric 二値 + fabrication 列挙）
- 実行時フラグ: Tool Search 有効（TOOL_SEARCH_ENABLED=true, 本番同等）、DELIB_* 全 OFF
- run: `.state/eval/20260702_181803_smoke_baseline/`（E1/F3/G1 は transient
  失敗のため `20260702_182504_smoke_baseline/` で再計測。worker/oauth の
  プロセス間 race と思われる — runner に1回リトライを実装済み）

## 結果（8/8 ケース成立）

| case | wall | auto | rubric | 備考 |
|---|---|---|---|---|
| A1 数値逐語 | 39.6s | **PASS**（13数値全一致） | 1/1 | |
| B1 2-hop統合 | 49.7s | PASS（2hop実行・両記事統合） | 2/2 | |
| B4 コード読解 | 98.1s | - | 4/5 | 1基準のみjudge FAIL（拡張種別の言及粒度） |
| E1 正直さ | 46.8s | - | PASS | 未来決算を「存在しない」と正答 |
| F3 JSON指示 | 32.2s | PASS | - | |
| G1 tool発見 | 175.7s | PASS | PASS | **キーワード無しで tool search → playwright 発見・実行**（層1の実地動作確認） |
| H1 記憶想起 | 26.5s | PASS（seed値一致 + mem0 search使用） | - | |
| J1 雑談ガード | 14.4s | PASS（tool不使用・500字以内） | 1/1 | |

## Gates

- **fabrication count: 0 → PASS**
- 数値逐語（A1）: 100% → PASS
- auto-check failures: なし（再計測後）
- rubric pass rate: 約 90%（B4 の1基準のみ落ち）

## Phase 1 candidate 計測（同日）

evidence index + result distillation を ON にした candidate（蒸留は
`DELIB_DISTILL_MIN_CHARS=200` で強制発火させて canary を成立させた。本番既定は 4000）:

| case | 結果 | 備考 |
|---|---|---|
| A1 | auto **PASS**（13数値すべて逐語一致） | **蒸留3発火の下で数値破損ゼロ** — hard gate 成立。蒸留ノートは数値を逐語コピーし、切断箇所を「本文截断」と明示 |
| B1 | auto PASS, rubric 2/2 | 2-hop 統合が蒸留経路でも成立 |
| F3 | auto PASS | JSON 指示遵守維持 |
| J1 | auto PASS | tool 不使用パスへの干渉なし |
| gates | fabrication 0 / auto失敗 0 / rubric 100% | |

runs: `.state/eval/20260702_183302_smoke_candidate/`（通常閾値）,
`20260702_183823_smoke_candidate/`（蒸留強制・canary 確認）

## Phase 2-5 実装後の deliberate パイプライン実弾確認（同日）

B1 を全フラグ ON（router/planner/verifier/scratchpad + 蒸留強制）で実行:

| run | 結果 |
|---|---|
| 1回目（初期 verifier プロンプト） | auto PASS / rubric 2/2 だが **eval judge が fabrication 1 件検出**（「推論の事実化」— hot/cold キュー = stream/batch という根拠なき対応付け。verifier はスルー） |
| 2回目（verifier に「推論の事実化」観点を追加後） | **fabrication 0 / auto PASS / rubric 2/2**。transcript で verifier→revise の実往復を確認（指摘→修正→合格） |

- eval ハーネスがパイプラインの盲点（verifier プロンプトの穴）を1本目で検出し、
  修正の効果を2本目で確認できた — 評価基盤が設計意図どおりに機能
- planner は単体検証で正常（Fable, 20s, 5 steps）。plan ブロックが transcript に
  見えないのは「最初の user ターンを api_messages に保存しない」既存の設計仕様
  （per-request scaffolding を replay に焼き込まない）で正しい挙動
- advisor probe（追調査で訂正）: 計4回 + multi-turn echo 検証の結果
  **subscriber 経路で動作可能**。初回の実行エラーは transient（以降 3/3 成功）。
  advisor=Fable のみ可、結果は `advisor_redacted_result`+encrypted_content
  （opaque）で verbatim echo の安全性も確認。ADVISOR_ENABLED=true で有効化可能

## 留意

- k=1 の点推定。**k=3 の本計測は P2 投入時の再 baseline と同時に実施**
  （本日 subscriber 枠を大量消費済みのため延期。ばらつき計測はそこで）
- transient 失敗（OAuth race / ExceptionGroup）は worker で leaf 展開 +
  runner 1回リトライを実装して吸収
- G1 の 175s は playwright 初回 spawn（chromium）込み
