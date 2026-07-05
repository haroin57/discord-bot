#!/usr/bin/env bash
# supergateway ローカルパッチの永続適用（2026-07-04, docs/oracle_mcp.md 参照）。
#
# supergateway 3.4.3 の dist/gateways/{stdioToStatelessStreamableHttp,stdioToSse}.js
# は transport.send() (async) を同期 try/catch でしか包んでおらず、クライアント
# 切断後に子プロセスが応答を返すと unhandled rejection
# (Error: No connection established for request ID: N) で node ごと落ちる。
# mcp-oracle-shared / mcp-papermcp unit が exit-code クラッシュ → systemd 再起動
# を繰り返す根本原因。本スクリプトはリポジトリ内の正本
# (infra/supergateway-patches/<version>/files/) をインストール先へ冪等に再適用する。
# oracle_mcp_launch.sh が MCP 起動のたびに呼ぶため、npm update -g で剥がれても
# 次回スポーンで自己修復する。
#
# volta は node バージョンごとに独立した node_modules を持つため、
# /home/ubuntu/.volta/tools/image/node/*/lib/node_modules/supergateway を全列挙し、
# package.json の version が正本ディレクトリと一致するものだけに適用する。
# バージョン不一致は「適用せず警告」して正常終了する
# （壊れた適用より無パッチの方がマシ）。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH_ROOT="${REPO_DIR}/infra/supergateway-patches"

shopt -s nullglob
INSTALLS=("${HOME}"/.volta/tools/image/node/*/lib/node_modules/supergateway)
shopt -u nullglob

if [[ "${#INSTALLS[@]}" -eq 0 ]]; then
    echo "[supergateway-patches] no supergateway install found under ${HOME}/.volta — skip" >&2
    exit 0
fi

total_applied=0
for PKG_DIR in "${INSTALLS[@]}"; do
    if [[ ! -f "${PKG_DIR}/package.json" ]]; then
        echo "[supergateway-patches] WARNING: no package.json in ${PKG_DIR} — skip" >&2
        continue
    fi

    VERSION="$(python3 -c "import json;print(json.load(open('${PKG_DIR}/package.json'))['version'])")"
    PATCH_DIR="${PATCH_ROOT}/${VERSION}/files"

    if [[ ! -d "${PATCH_DIR}" ]]; then
        echo "[supergateway-patches] WARNING: no patch set for installed version ${VERSION} (${PKG_DIR})" >&2
        echo "[supergateway-patches] available: $(ls "${PATCH_ROOT}" 2>/dev/null | tr '\n' ' ')" >&2
        echo "[supergateway-patches] port the patches (see docs/oracle_mcp.md) — supergateway runs UNPATCHED" >&2
        continue
    fi

    applied=0
    while IFS= read -r src; do
        rel="${src#"${PATCH_DIR}"/}"
        dst="${PKG_DIR}/${rel}"
        if [[ ! -f "${dst}" ]]; then
            echo "[supergateway-patches] WARNING: target missing: ${dst} — skip" >&2
            continue
        fi
        if ! cmp -s "${src}" "${dst}"; then
            cp "${src}" "${dst}"
            applied=$((applied + 1))
        fi
    done < <(find "${PATCH_DIR}" -type f)

    if [[ "${applied}" -gt 0 ]]; then
        echo "[supergateway-patches] re-applied ${applied} file(s) to ${PKG_DIR} (v${VERSION})" >&2
    fi
    total_applied=$((total_applied + applied))
done

echo "[supergateway-patches] done (re-applied ${total_applied} file(s) total)" >&2
