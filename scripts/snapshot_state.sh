#!/usr/bin/env bash
# Create or verify a fail-closed snapshot from an explicit live-state inventory.
set -euo pipefail
umask 077

SNAP_ROOT="${SNAPSHOT_ROOT:-/var/lib/discord-bot-backup/snapshots}"
MAX_ATTEMPTS="${SNAPSHOT_STABILITY_ATTEMPTS:-3}"
INVENTORY="${SNAPSHOT_STATE_INVENTORY:-}"
INVENTORY_OWNER_UID="${SNAPSHOT_INVENTORY_OWNER_UID:-0}"
REQUIRE_READ_ONLY="${SNAPSHOT_REQUIRE_READ_ONLY:-1}"

EXCLUDES=(
    --exclude='*.bak*' --exclude='*.broken.*' --exclude='*_flush_backup_*'
    --exclude='*.lock' --exclude='bot.pid' --exclude='eval'
    --exclude='agy_workdir' --exclude='codex_workdir'
    --exclude='*.db-shm' --exclude='*.db-wal' --exclude='*-shm' --exclude='*-wal'
    --exclude='*-journal'
)

if ! [[ "${MAX_ATTEMPTS}" =~ ^[1-9][0-9]*$ ]] \
    || ! [[ "${INVENTORY_OWNER_UID}" =~ ^[0-9]+$ ]] \
    || [[ "${REQUIRE_READ_ONLY}" != "0" && "${REQUIRE_READ_ONLY}" != "1" ]]; then
    echo "[snapshot] invalid snapshot validation configuration" >&2
    exit 2
fi

sqlite_files() {
    find "$1" -type f \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \) \
        | grep -vE '\.bak|\.broken\.|_flush_backup_' || true
}

sqlite_file_set() {
    python3 - "$1" "$2" <<'PY'
from pathlib import Path
import sys

root, output = map(Path, sys.argv[1:])
records: list[str] = []
for path in root.rglob("*"):
    relative = path.relative_to(root)
    name = path.name
    if (
        path.is_file()
        and path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
        and ".bak" not in name
        and ".broken." not in name
        and "_flush_backup_" not in name
    ):
        value = relative.as_posix()
        if "\n" in value or "\t" in value:
            raise SystemExit(f"unsupported SQLite path: {value!r}")
        records.append(value + "\n")
output.write_text("".join(sorted(records)), encoding="utf-8")
PY
}

source_fingerprint() {
    python3 - "$1" "$2" <<'PY'
import fnmatch
import hashlib
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
output = Path(sys.argv[2])
skip_dirs = {"eval", "agy_workdir", "codex_workdir"}

def skipped(relative: Path) -> bool:
    name = relative.name
    return (
        any(part in skip_dirs for part in relative.parts)
        or fnmatch.fnmatch(name, "*.bak*")
        or fnmatch.fnmatch(name, "*.broken.*")
        or "_flush_backup_" in name
        or fnmatch.fnmatch(name, "*.lock")
        or name == "bot.pid"
        # SQLite physical generations may checkpoint or append WAL while a
        # transactionally consistent .backup is running. Their consistency is
        # established from the backup result, not unstable physical bytes.
        or name.endswith((".db", ".sqlite", ".sqlite3", "-wal", "-shm", "-journal"))
    )

records: list[str] = []
for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
    relative = path.relative_to(root)
    if skipped(relative):
        continue
    before = path.lstat()
    if stat.S_ISDIR(before.st_mode):
        continue
    if not stat.S_ISREG(before.st_mode):
        raise SystemExit(f"unsupported live-state object: {relative!s}")
    if "\n" in relative.as_posix() or "\t" in relative.as_posix():
        raise SystemExit(f"unsupported live-state path: {relative!s}")
    digest_hash = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest_hash.update(chunk)
    after = path.lstat()
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode) != (
        after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode
    ):
        raise SystemExit(f"live-state object changed while hashing: {relative!s}")
    records.append(f"file\t{relative.as_posix()}\t{digest_hash.hexdigest()}\n")
output.write_text("".join(records), encoding="utf-8")
PY
}

candidate_non_db_matches_source() {
    python3 - "$1" "$2" <<'PY'
import fnmatch
import hashlib
from pathlib import Path
import stat
import sys

source_root, candidate_root = map(Path, sys.argv[1:])
skip_dirs = {"eval", "agy_workdir", "codex_workdir"}

def skipped(relative: Path) -> bool:
    name = relative.name
    return (
        any(part in skip_dirs for part in relative.parts)
        or fnmatch.fnmatch(name, "*.bak*")
        or fnmatch.fnmatch(name, "*.broken.*")
        or "_flush_backup_" in name
        or fnmatch.fnmatch(name, "*.lock")
        or name == "bot.pid"
        or name.endswith(
            (".db", ".sqlite", ".sqlite3", "-wal", "-shm", "-journal")
        )
    )

def inventory(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if skipped(relative) or path.is_dir():
            continue
        if not stat.S_ISREG(path.lstat().st_mode):
            raise SystemExit(f"unsupported state object: {relative!s}")
        result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result

if inventory(source_root) != inventory(candidate_root):
    raise SystemExit("candidate non-database files do not match live state")
PY
}

write_sqlite_logical_fingerprint() {
    python3 - "$1" "$2" <<'PY'
import hashlib
from pathlib import Path
import sqlite3
import sys

root = Path(sys.argv[1])
output = Path(sys.argv[2])
records: list[str] = []
for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
    if not path.is_file() or path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        continue
    relative = path.relative_to(root).as_posix()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise SystemExit(f"SQLite backup integrity failed: {relative}: {integrity!r}")
        logical = "\n".join(connection.iterdump()).encode()
    finally:
        connection.close()
    records.append(f"sqlite\t{relative}\t{hashlib.sha256(logical).hexdigest()}\n")
output.write_text("".join(records), encoding="utf-8")
PY
}

write_snapshot_manifest() {
    python3 - "$1" <<'PY'
import hashlib
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
manifest = root / "SNAPSHOT_FILES.sha256"
lines: list[str] = []
for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
    relative = path.relative_to(root)
    if path == manifest or path.is_dir():
        continue
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise SystemExit(f"snapshot contains unsupported object: {relative!s}")
    if "\n" in relative.as_posix():
        raise SystemExit(f"unsupported newline in snapshot path: {relative!s}")
    lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative.as_posix()}\n")
if not lines:
    raise SystemExit("snapshot contains no files")
manifest.write_text("".join(lines), encoding="utf-8")
PY
}

fsync_directory() {
    python3 - "$1" <<'PY'
import os
import sys

fd = os.open(sys.argv[1], os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(fd)
finally:
    os.close(fd)
PY
}

fsync_tree() {
    python3 - "$1" <<'PY'
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
for current, directories, files in os.walk(root, topdown=False, followlinks=False):
    current_path = Path(current)
    for name in files:
        path = current_path / name
        if not stat.S_ISREG(path.lstat().st_mode):
            raise SystemExit(f"unsupported snapshot object: {path}")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    for name in directories:
        path = current_path / name
        if path.is_symlink():
            raise SystemExit(f"unsupported snapshot symlink: {path}")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    fd = os.open(current_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
PY
}

WORK=""
verify_snapshot() {
    local snap="$1" fail=0 checked=0 result db
    [[ -d "${snap}" && -s "${snap}/SNAPSHOT_GENERATION" \
        && -s "${snap}/SNAPSHOT_FILES.sha256" ]] || {
        echo "[verify] snapshot or required metadata is missing: ${snap}" >&2
        exit 2
    }
    WORK="$(mktemp -d /tmp/state_restore_rehearsal.XXXXXX)"
    trap 'rm -rf "${WORK}"' EXIT
    cp -a "${snap}/." "${WORK}/"
    if find "${WORK}" -type l -print -quit | grep -q .; then
        echo "[verify] FAIL snapshot contains a symbolic link" >&2
        exit 1
    fi
    if ! python3 - "${WORK}" <<'PY'
import hashlib
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
manifest_path = root / "SNAPSHOT_FILES.sha256"
expected: dict[str, str] = {}
for line in manifest_path.read_text(encoding="utf-8").splitlines():
    digest, separator, relative = line.partition("  ")
    path = Path(relative)
    if (separator != "  " or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or path.is_absolute() or ".." in path.parts or relative in expected):
        raise SystemExit(f"invalid snapshot manifest entry: {line}")
    expected[relative] = digest
actual = {path.relative_to(root).as_posix() for path in root.rglob("*")
          if path.is_file() and path != manifest_path}
if actual != set(expected):
    raise SystemExit("snapshot file set mismatch")
for relative, digest in expected.items():
    if hashlib.sha256((root / relative).read_bytes()).hexdigest() != digest:
        raise SystemExit(f"snapshot digest mismatch: {relative}")
PY
    then
        echo "[verify] FAIL snapshot content manifest mismatch" >&2
        exit 1
    fi
    while IFS= read -r db; do
        [[ -n "${db}" ]] || continue
        checked=$((checked + 1))
        result="$(sqlite3 "${db}" 'PRAGMA integrity_check;' 2>&1 | head -1)"
        if [[ "${result}" != "ok" ]]; then
            echo "[verify] FAIL ${db}: ${result}" >&2
            fail=1
        fi
    done < <(sqlite_files "${WORK}")
    while IFS= read -r json_file; do
        [[ -n "${json_file}" ]] || continue
        if ! python3 -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' \
            "${json_file}"; then
            echo "[verify] FAIL ${json_file}: invalid JSON" >&2
            fail=1
        fi
    done < <(find "${WORK}" -type f -name '*.json')
    [[ "${fail}" -eq 0 ]] || exit 1
    echo "[verify] integrity-checked ${checked} databases"
    echo "[verify] rehearsal OK — generation and contents are restorable"
}

if [[ "${1:-}" == "--verify" ]]; then
    verify_snapshot "${2:?usage: --verify <snapshot-dir>}"
    exit 0
fi

[[ -n "${INVENTORY}" && "${INVENTORY}" == /* ]] || {
    echo "[snapshot] SNAPSHOT_STATE_INVENTORY must be an absolute path" >&2
    exit 2
}
[[ -f "${INVENTORY}" && ! -L "${INVENTORY}" ]] || {
    echo "[snapshot] inventory must be a regular non-symlink file" >&2
    exit 2
}
if [[ "$(stat -c %u "${INVENTORY}")" != "${INVENTORY_OWNER_UID}" ]] \
    || [[ -n "$(find "${INVENTORY}" -maxdepth 0 -perm /022 -print -quit)" ]]; then
    echo "[snapshot] inventory owner or mode is not protected" >&2
    exit 2
fi

mapfile -t STATE_ROOTS < <(python3 - "${INVENTORY}" <<'PY'
import json
from pathlib import Path
import re
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, dict) or payload.get("format") != "discord-bot-state-inventory-v1":
    raise SystemExit("invalid state inventory format")
services = payload.get("services")
if not isinstance(services, list) or not services:
    raise SystemExit("state inventory has no services")
seen_names: set[str] = set()
seen_paths: set[str] = set()
for item in services:
    if not isinstance(item, dict) or set(item) != {"name", "path"}:
        raise SystemExit("invalid state inventory entry")
    name, raw_path = item["name"], item["path"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
        raise SystemExit("invalid state inventory service name")
    if not isinstance(raw_path, str) or "\n" in raw_path or "\t" in raw_path:
        raise SystemExit("invalid state inventory path")
    path = Path(raw_path)
    if not path.is_absolute() or name in seen_names or raw_path in seen_paths:
        raise SystemExit("duplicate or relative state inventory entry")
    seen_names.add(name)
    seen_paths.add(raw_path)
    print(f"{name}\t{raw_path}")
PY
)
[[ "${#STATE_ROOTS[@]}" -gt 0 ]] || {
    echo "[snapshot] inventory parsing produced no live roots" >&2
    exit 2
}

mkdir -p "${SNAP_ROOT}"
snap_root_real="$(realpath -e "${SNAP_ROOT}")"
for entry in "${STATE_ROOTS[@]}"; do
    IFS=$'\t' read -r name state_dir <<<"${entry}"
    [[ -d "${state_dir}" && ! -L "${state_dir}" && -r "${state_dir}" && -x "${state_dir}" ]] || {
        echo "[snapshot] unreadable live root for ${name}: ${state_dir}" >&2
        exit 2
    }
    state_real="$(realpath -e "${state_dir}")"
    if [[ "${snap_root_real}" == "${state_real}" \
        || "${snap_root_real}" == "${state_real}"/* \
        || "${state_real}" == "${snap_root_real}"/* ]]; then
        echo "[snapshot] snapshot and live roots overlap: ${name}" >&2
        exit 2
    fi
    if [[ "${REQUIRE_READ_ONLY}" == "1" && -w "${state_dir}" ]]; then
        echo "[snapshot] backup identity has write access to live root: ${name}" >&2
        exit 2
    fi
done

STAMP="$(date +%Y%m%dT%H%M%S)-$$"
DEST="${SNAP_ROOT}/${STAMP}"
ATTEMPT_ROOT="${SNAP_ROOT}/.${STAMP}.incomplete"
trap 'rm -rf "${ATTEMPT_ROOT}"' EXIT

snapshot_ok=0
for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
    rm -rf "${ATTEMPT_ROOT}"
    mkdir -p "${ATTEMPT_ROOT}/data/services" "${ATTEMPT_ROOT}/fingerprints"
    attempt_ok=1
    for entry in "${STATE_ROOTS[@]}"; do
        IFS=$'\t' read -r name state_dir <<<"${entry}"
        source_fingerprint "${state_dir}" "${ATTEMPT_ROOT}/fingerprints/${name}.before" \
            || attempt_ok=0
        sqlite_file_set "${state_dir}" \
            "${ATTEMPT_ROOT}/fingerprints/${name}.dbset.before" || attempt_ok=0
    done
    [[ "${attempt_ok}" -eq 1 ]] || continue

    echo "[snapshot] copying candidate generation (attempt ${attempt}/${MAX_ATTEMPTS})"
    for entry in "${STATE_ROOTS[@]}"; do
        IFS=$'\t' read -r name state_dir <<<"${entry}"
        candidate="${ATTEMPT_ROOT}/data/services/${name}"
        mkdir -p "${candidate}"
        rsync -a "${EXCLUDES[@]}" --exclude='*.db' --exclude='*.sqlite' \
            --exclude='*.sqlite3' "${state_dir}/" "${candidate}/" || attempt_ok=0
        while IFS= read -r db; do
            [[ -n "${db}" ]] || continue
            rel="${db#"${state_dir}"/}"
            destination="${candidate}/${rel}"
            mkdir -p "$(dirname "${destination}")"
            sqlite_destination="${destination//\'/\'\'}"
            # The release controller runs with ProtectSystem=strict.  Open the
            # live database explicitly read-only so SQLite does not require a
            # writable source directory for journal/WAL setup.
            if ! sqlite3 -readonly "${db}" ".backup '${sqlite_destination}'"; then
                echo "[snapshot] SQLite backup failed: ${db}" >&2
                attempt_ok=0
            fi
        done < <(sqlite_files "${state_dir}")
        write_sqlite_logical_fingerprint \
            "${candidate}" "${ATTEMPT_ROOT}/fingerprints/${name}.sqlite" \
            || attempt_ok=0
        sqlite_file_set "${candidate}" \
            "${ATTEMPT_ROOT}/fingerprints/${name}.dbset.candidate" || attempt_ok=0
        source_fingerprint "${state_dir}" "${ATTEMPT_ROOT}/fingerprints/${name}.after" \
            || attempt_ok=0
        sqlite_file_set "${state_dir}" \
            "${ATTEMPT_ROOT}/fingerprints/${name}.dbset.after" || attempt_ok=0
        cmp -s "${ATTEMPT_ROOT}/fingerprints/${name}.before" \
            "${ATTEMPT_ROOT}/fingerprints/${name}.after" || attempt_ok=0
        cmp -s "${ATTEMPT_ROOT}/fingerprints/${name}.dbset.before" \
            "${ATTEMPT_ROOT}/fingerprints/${name}.dbset.after" || attempt_ok=0
        cmp -s "${ATTEMPT_ROOT}/fingerprints/${name}.dbset.before" \
            "${ATTEMPT_ROOT}/fingerprints/${name}.dbset.candidate" || attempt_ok=0
        candidate_non_db_matches_source "${state_dir}" "${candidate}" || attempt_ok=0
        source_fingerprint "${state_dir}" "${ATTEMPT_ROOT}/fingerprints/${name}.final" \
            || attempt_ok=0
        sqlite_file_set "${state_dir}" \
            "${ATTEMPT_ROOT}/fingerprints/${name}.dbset.final" || attempt_ok=0
        cmp -s "${ATTEMPT_ROOT}/fingerprints/${name}.after" \
            "${ATTEMPT_ROOT}/fingerprints/${name}.final" || attempt_ok=0
        cmp -s "${ATTEMPT_ROOT}/fingerprints/${name}.dbset.after" \
            "${ATTEMPT_ROOT}/fingerprints/${name}.dbset.final" || attempt_ok=0
    done
    if [[ "${attempt_ok}" -ne 1 ]]; then
        echo "[snapshot] live generation changed; discarding attempt ${attempt}/${MAX_ATTEMPTS}" >&2
        continue
    fi

    cp -- "${INVENTORY}" "${ATTEMPT_ROOT}/data/STATE_INVENTORY.json"
    if [[ -n "${SNAPSHOT_RELEASE_METADATA_PATH:-}" ]]; then
        cp -- "${SNAPSHOT_RELEASE_METADATA_PATH}" "${ATTEMPT_ROOT}/data/RELEASE_METADATA.json"
    fi
    combined_digest="$(cat "${ATTEMPT_ROOT}"/fingerprints/*.final \
        "${ATTEMPT_ROOT}"/fingerprints/*.sqlite | sha256sum | awk '{print $1}')"
    {
        printf 'format=discord-bot-state-v3\n'
        printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'inventory_sha256=%s\n' "$(sha256sum "${INVENTORY}" | awk '{print $1}')"
        printf 'source_fingerprint_sha256=%s\n' "${combined_digest}"
    } >"${ATTEMPT_ROOT}/data/SNAPSHOT_GENERATION"
    write_snapshot_manifest "${ATTEMPT_ROOT}/data"
    fsync_tree "${ATTEMPT_ROOT}/data"
    fsync_directory "${ATTEMPT_ROOT}"
    fsync_directory "${SNAP_ROOT}"
    mv "${ATTEMPT_ROOT}/data" "${DEST}"
    fsync_directory "${ATTEMPT_ROOT}"
    fsync_directory "${SNAP_ROOT}"
    snapshot_ok=1
    break
done

if [[ "${snapshot_ok}" -ne 1 ]]; then
    echo "[snapshot] FAIL live state did not remain stable for a complete generation" >&2
    exit 1
fi

du -sh "${DEST}" | awk '{print "[snapshot] size: "$1}'
echo "[snapshot] done: ${DEST}"
echo "[snapshot] verify with: scripts/snapshot_state.sh --verify ${DEST}"

if [[ "${SNAPSHOT_R2_SYNC:-1}" == "1" ]] && command -v wrangler >/dev/null 2>&1; then
    export PATH="${HOME}/.volta/bin:${PATH}"
    R2_ARCHIVE="$(mktemp /tmp/state-snapshot.XXXXXX.tar.gz)"
    trap 'rm -rf "${ATTEMPT_ROOT}"; rm -f "${R2_ARCHIVE:-}"' EXIT
    tar czf "${R2_ARCHIVE}" -C "${DEST}" .
    if wrangler r2 object put "kanae-state-backup/snapshots/${STAMP}.tar.gz" \
        --file "${R2_ARCHIVE}" --remote >/dev/null 2>&1; then
        echo "[snapshot] R2 mirror OK: snapshots/${STAMP}.tar.gz"
    else
        echo "[snapshot] R2 mirror FAILED — local generation remains valid" >&2
    fi
    rm -f "${R2_ARCHIVE}"
fi
