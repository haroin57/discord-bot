#!/usr/bin/env bash
# Install root-owned as /usr/local/libexec/discord-bot-release-publisher.
set -euo pipefail
umask 022

if [[ "${RELEASE_PUBLISHER_TEST_MODE:-0}" != "1" && "${EUID}" -ne 0 ]]; then
    echo "[publisher] root execution is required" >&2
    exit 4
fi
fsync_dir() {
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

validate_candidate() {
    python3 - "$1" "$2" "$3" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

generation, expected_sha, expected_owner_text = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
expected_owner = int(expected_owner_text)
if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
    raise SystemExit("invalid release SHA")
if {path.name for path in generation.iterdir()} != {
    "src", "venv", ".release-sha", "release.json", "release-content.jsonl"
}:
    raise SystemExit("unexpected release root object")
for directory in (generation, generation / "src", generation / "venv"):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o222:
        raise SystemExit("release directory is not immutable")
marker = generation / ".release-sha"
if marker.is_symlink() or marker.read_text(encoding="ascii").strip() != expected_sha:
    raise SystemExit("invalid release marker")
metadata_path = generation / "release.json"
manifest_path = generation / "release-content.jsonl"
if metadata_path.is_symlink() or manifest_path.is_symlink():
    raise SystemExit("release metadata cannot be a symlink")
metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
if (
    metadata.get("format") != "discord-bot-release-v2"
    or metadata.get("release_sha") != expected_sha
    or metadata.get("generation") != expected_sha
    or metadata.get("content_manifest_sha256")
    != hashlib.sha256(manifest_path.read_bytes()).hexdigest()
):
    raise SystemExit("invalid release metadata")

expected: dict[str, tuple[int, str]] = {}
for line in manifest_path.read_text(encoding="utf-8").splitlines():
    record = json.loads(line)
    if not isinstance(record, dict) or set(record) != {"path", "mode", "sha256"}:
        raise SystemExit("invalid content manifest record")
    relative = record["path"]
    relative_path = Path(relative) if isinstance(relative, str) else Path("/")
    if (
        not isinstance(relative, str)
        or relative_path.is_absolute()
        or relative_path.parts[:1] not in {("src",), ("venv",)}
        or ".." in relative_path.parts
        or relative in expected
        or not isinstance(record["mode"], int)
        or not isinstance(record["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None
    ):
        raise SystemExit("invalid content manifest path")
    expected[relative] = (record["mode"], record["sha256"])

actual: dict[str, tuple[int, str]] = {}
for current, directories, files in os.walk(generation, followlinks=False):
    current_path = Path(current)
    for name in [*directories, *files]:
        path = current_path / name
        info = path.lstat()
        relative = path.relative_to(generation)
        if expected_owner >= 0 and info.st_uid != expected_owner:
            raise SystemExit("release object owner mismatch")
        if not stat.S_ISLNK(info.st_mode) and stat.S_IMODE(info.st_mode) & 0o222:
            raise SystemExit("release object is writable")
        if stat.S_ISLNK(info.st_mode):
            if relative.parts[:1] != ("venv",):
                raise SystemExit("source symlink is not allowed")
            resolved = path.resolve(strict=True)
            internal = resolved == generation / "venv" or generation / "venv" in resolved.parents
            system_python = (
                relative.parts[:2] == ("venv", "bin")
                and path.name.startswith("python")
                and resolved.parent == Path("/usr/bin")
                and resolved.name.startswith("python")
            )
            internal_lib = relative == Path("venv/lib64") and resolved == generation / "venv/lib"
            if not (internal or system_python or internal_lib):
                raise SystemExit("unsafe virtualenv symlink")
        elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise SystemExit("unsupported release object")
    for name in files:
        path = current_path / name
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and path.parts[: len(generation.parts)] == generation.parts:
            relative = path.relative_to(generation).as_posix()
            if relative.startswith(("src/", "venv/")):
                actual[relative] = (
                    stat.S_IMODE(info.st_mode),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
if not actual or actual != expected:
    raise SystemExit("release content does not match manifest")
PY
}

case "${1:-}" in
    publish)
        [[ "$#" -eq 6 ]] || exit 2
        staging="$2"
        generation="$3"
        generations_root="$4"
        build_root="$5"
        owner_uid="$6"
        [[ "${owner_uid}" =~ ^[0-9]+$ ]] || exit 2
        staging_real="$(realpath -e "${staging}")"
        generations_real="$(realpath -e "${generations_root}")"
        build_real="$(realpath -e "${build_root}")"
        generation_parent="$(realpath -e "$(dirname "${generation}")")"
        sha="$(basename "${generation}")"
        if [[ "${RELEASE_PUBLISHER_TEST_MODE:-0}" != "1" ]]; then
            case "${generations_real}:${build_real}:${owner_uid}" in
                /var/lib/discord-bot-releases/prod/generations:/var/lib/discord-bot-deploy/prod/release-build:0 | \
                    /var/lib/discord-bot-releases/dev/generations:/var/lib/discord-bot-deploy/dev/release-build:0) ;;
                *)
                    echo "[publisher] publication target is outside the installed allowlist" >&2
                    exit 4
                    ;;
            esac
        fi
        [[ "${generation_parent}" == "${generations_real}" \
            && "${staging_real}" == "${build_real}"/."${sha}".building.* \
            && "${sha}" =~ ^[0-9a-f]{40}$ \
            && ! -e "${generation}" && ! -L "${generation}" \
            && "$(tr -d '[:space:]' <"${staging_real}/.release-sha")" == "${sha}" ]] || {
            echo "[publisher] invalid publication paths" >&2
            exit 4
        }
        [[ "$(stat -c %d "${staging_real}")" == "$(stat -c %d "${generations_real}")" ]] \
            || {
                echo "[publisher] build and generation roots must share a filesystem" >&2
                exit 4
            }
        validate_candidate "${staging_real}" "${sha}" -1
        chown -hR "${owner_uid}:${owner_uid}" "${staging_real}"
        validate_candidate "${staging_real}" "${sha}" "${owner_uid}"
        # rename(2) of a directory requires owner write permission on the source
        # directory under the hardened deployment mount. Its contents are
        # already read-only and root-owned before publication.
        chmod u+w "${staging_real}"
        mv -T -- "${staging_real}" "${generation}"
        chmod a-w "${generation}"
        fsync_dir "${generations_real}"
        ;;
    activate)
        [[ "$#" -eq 4 ]] || exit 2
        sha="$2"
        release_root="$(realpath -e "$3")"
        owner_uid="$4"
        if [[ "${RELEASE_PUBLISHER_TEST_MODE:-0}" != "1" ]]; then
            [[ "${owner_uid}" == "0" ]] || exit 4
            case "${release_root}" in
                /var/lib/discord-bot-releases/prod | /var/lib/discord-bot-releases/dev) ;;
                *)
                    echo "[publisher] activation target is outside the installed allowlist" >&2
                    exit 4
                    ;;
            esac
        fi
        [[ "${sha}" =~ ^[0-9a-f]{40}$ && "${owner_uid}" =~ ^[0-9]+$ \
            && -d "${release_root}/generations/${sha}" \
            && "$(stat -c %u "${release_root}/generations/${sha}")" == "${owner_uid}" ]] \
            || exit 4
        validate_candidate "${release_root}/generations/${sha}" "${sha}" "${owner_uid}"
        tmp_link="${release_root}/.current.${sha}.$$"
        if [[ "${RELEASE_PUBLISHER_TEST_MODE:-0}" == "1" ]]; then
            chmod u+w "${release_root}"
        fi
        ln -s -- "generations/${sha}" "${tmp_link}"
        mv -Tf -- "${tmp_link}" "${release_root}/current"
        fsync_dir "${release_root}"
        ;;
    *)
        echo "usage: release_publisher.sh publish|activate ..." >&2
        exit 2
        ;;
esac
