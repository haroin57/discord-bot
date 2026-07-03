#!/usr/bin/env bash
# oracle ローカルパッチの永続適用（2026-07-04, docs/oracle_mcp.md 参照）。
#
# @steipete/oracle は npm update -g で dist が素に戻り、TPM クランプ /
# maxOutput 公開 / search デフォルト off のパッチが消える。本スクリプトは
# リポジトリ内の正本 (infra/oracle-patches/<version>/files/) をインストール先へ
# 冪等に再適用する。oracle_mcp_launch.sh が MCP 起動のたびに呼ぶため、
# 更新で剥がれても次回スポーンで自己修復する。
#
# バージョンが正本と一致しない場合は「適用せず警告」して正常終了する
# （壊れた適用より無パッチの方がマシ。その場合 gpt-5.5-pro consult は
# search:false を明示しないと 429 死する点に注意）。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH_ROOT="${REPO_DIR}/infra/oracle-patches"

# インストール先の解決。volta は PATH 上に shim を置くため command -v では
# 実体に届かない → volta which を優先し、旧来の readlink 追跡と既知パスを
# フォールバックに持つ。
resolve_real_bin() {
    if command -v volta >/dev/null 2>&1; then
        volta which oracle-mcp 2>/dev/null && return 0
    fi
    local bin
    bin="$(command -v oracle-mcp || true)"
    [[ -n "${bin}" ]] && readlink -f "${bin}" && return 0
    return 1
}

REAL="$(resolve_real_bin || true)"
PKG_DIR=""
if [[ -n "${REAL}" ]]; then
    # .../node_modules/@steipete/oracle/{bin,dist}/... → パッケージルートへ遡る
    PKG_DIR="${REAL}"
    while [[ "${PKG_DIR}" != "/" ]]; do
        PKG_DIR="$(dirname "${PKG_DIR}")"
        if [[ -f "${PKG_DIR}/package.json" ]] && \
           grep -q '"name": "@steipete/oracle"' "${PKG_DIR}/package.json"; then
            break
        fi
    done
    [[ "${PKG_DIR}" == "/" ]] && PKG_DIR=""
fi
# volta の bin/ は package.json (name=@steipete/oracle) がルートに無い配置も
# あるため、既知の node_modules 配下を最終フォールバックにする
if [[ -z "${PKG_DIR}" ]]; then
    PKG_DIR="$(ls -d "${HOME}"/.volta/tools/image/packages/@steipete/oracle/lib/node_modules/@steipete/oracle 2>/dev/null || true)"
fi
if [[ -z "${PKG_DIR}" || ! -f "${PKG_DIR}/package.json" ]]; then
    echo "[oracle-patches] @steipete/oracle install not found — skip" >&2
    exit 0
fi

VERSION="$(python3 -c "import json;print(json.load(open('${PKG_DIR}/package.json'))['version'])")"
PATCH_DIR="${PATCH_ROOT}/${VERSION}/files"

if [[ ! -d "${PATCH_DIR}" ]]; then
    echo "[oracle-patches] WARNING: no patch set for installed version ${VERSION}" >&2
    echo "[oracle-patches] available: $(ls "${PATCH_ROOT}" 2>/dev/null | tr '\n' ' ')" >&2
    echo "[oracle-patches] port the patches (see docs/oracle_mcp.md) — oracle runs UNPATCHED" >&2
    exit 0
fi

applied=0
while IFS= read -r src; do
    rel="${src#"${PATCH_DIR}"/}"
    dst="${PKG_DIR}/${rel}"
    if [[ ! -f "${dst}" ]]; then
        echo "[oracle-patches] WARNING: target missing: ${dst} — skip" >&2
        continue
    fi
    if ! cmp -s "${src}" "${dst}"; then
        cp "${src}" "${dst}"
        applied=$((applied + 1))
    fi
done < <(find "${PATCH_DIR}" -type f)

if [[ "${applied}" -gt 0 ]]; then
    echo "[oracle-patches] re-applied ${applied} file(s) to ${PKG_DIR} (v${VERSION})" >&2
fi
