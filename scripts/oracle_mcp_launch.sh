#!/usr/bin/env bash
# oracle-mcp (steipete/oracle) を MCP サーバーとして起動するランチャー。
# 目的: gpt-5.5-pro 等を OpenAI Responses API 直で叩く MCP を CLI から使う。
#
# なぜラッパーが要るか:
#  - CLI から spawn される MCP は最小 PATH になり得る → volta の node bin を PATH に足す
#  - API キーを .mcp.json に直書きすると git に乗る → .env から実行時に注入する
#
# 認証: OPENAI_API_KEY(必須, gpt系) / GOOGLE_AI_API_KEY(任意, gemini系) を .env から。
# ChatGPT browser 自動化モードは使わない前提（Responses API 直＝規約クリーン）。
set -euo pipefail

export PATH="$HOME/.volta/bin:$PATH"
ENV_FILE="/home/ubuntu/discord-bot/.env"

for k in OPENAI_API_KEY GOOGLE_AI_API_KEY; do
  v="$(grep -E "^${k}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  if [ -n "${v}" ]; then export "${k}=${v}"; fi
done

# oracle は Gemini に GEMINI_API_KEY を見る。GOOGLE_AI_API_KEY を写経。
if [ -n "${GOOGLE_AI_API_KEY:-}" ]; then export GEMINI_API_KEY="${GOOGLE_AI_API_KEY}"; fi

# TPM ガード（ローカルパッチ連動, docs/oracle_mcp.md 参照）:
# OpenAI の TPM 会計は「入力推定 + max_output_tokens(未指定ならモデル最大出力)」を
# 予約する。この org は gpt-5.5-pro の TPM が 50k しかなく、未指定だと predicted 55k超で
# 恒久 429 → background キューが予約を保持し続けて後続まで連鎖失敗する。
# ORACLE_TPM_LIMIT を設定すると、パッチ済み oracle が max_output_tokens を
# 「入力推定に応じて TPM 内に収まる値」へ自動クランプする。.env で上書き可。
export ORACLE_TPM_LIMIT="${ORACLE_TPM_LIMIT:-50000}"
export ORACLE_MAX_OUTPUT_TOKENS="${ORACLE_MAX_OUTPUT_TOKENS:-24000}"

# search デフォルト off（ローカルパッチ連動, 2026-07-04, docs/oracle_mcp.md）:
# oracle のデフォルト search:true は、gpt-5.5-pro + TPM 50k の org では致命的。
# background モードの web 検索多段ラウンドが同一 1分窓に admission 予約を
# 積み上げて 50k を突破 → 429 → background ジョブ恒久 fail（実プロンプト18連敗の
# 根本原因。search 無効の同一プロンプトは完走することを実測で確認済み）。
# 検索が必要な consult だけ明示的に search:true を渡す。
export ORACLE_DEFAULT_SEARCH="${ORACLE_DEFAULT_SEARCH:-off}"

# ローカルパッチの自己修復（npm update で剥がれても次のスポーンで再適用）。
# 正本: infra/oracle-patches/<version>/files/、手順: docs/oracle_mcp.md
"$(dirname "${BASH_SOURCE[0]}")/oracle_apply_patches.sh" || true

exec oracle-mcp "$@"
