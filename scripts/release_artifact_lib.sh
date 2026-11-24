#!/usr/bin/env bash
# Shared immutable release-generation primitives. The caller must hold the deploy flock.

release_artifact_init() {
    RELEASE_ROOT="${RELEASE_ROOT:?RELEASE_ROOT is required}"
    RELEASE_GENERATIONS_ROOT="${RELEASE_GENERATIONS_ROOT:-${RELEASE_ROOT}/generations}"
    RELEASE_CURRENT_LINK="${RELEASE_CURRENT_LINK:-${RELEASE_ROOT}/current}"
    RELEASE_PUBLISHER="${RELEASE_PUBLISHER:-/usr/local/libexec/discord-bot-release-publisher}"
    RELEASE_PUBLISHER_USE_SUDO="${RELEASE_PUBLISHER_USE_SUDO:-1}"
    RELEASE_GENERATION_OWNER_UID="${RELEASE_GENERATION_OWNER_UID:-0}"
    RELEASE_BUILD_ROOT="${RELEASE_BUILD_ROOT:?RELEASE_BUILD_ROOT is required}"
    RELEASE_ACTIVATION_JOURNAL="${RELEASE_ACTIVATION_JOURNAL:-${DEPLOY_STATE_DIR:?DEPLOY_STATE_DIR is required}/activation.json}"

    local bot_real release_candidate release_real generations_candidate current_candidate build_candidate
    bot_real="$(realpath -e "${BOT_DIR}")" || return 1
    release_candidate="$(realpath -m "${RELEASE_ROOT}")" || return 1
    generations_candidate="$(realpath -m "${RELEASE_GENERATIONS_ROOT}")" || return 1
    current_candidate="$(realpath -m "${RELEASE_CURRENT_LINK}")" || return 1
    build_candidate="$(realpath -m "${RELEASE_BUILD_ROOT}")" || return 1
    if [[ "${release_candidate}" == "${bot_real}" \
        || "${release_candidate}" == "${bot_real}"/* \
        || "${generations_candidate}" != "${release_candidate}"/* \
        || "${current_candidate}" != "${release_candidate}"/* \
        || "${build_candidate}" == "${release_candidate}" \
        || "${build_candidate}" == "${release_candidate}"/* \
        || "${build_candidate}" == "${bot_real}" \
        || "${build_candidate}" == "${bot_real}"/* ]]; then
        echo "[release] generation paths must share an external RELEASE_ROOT" >&2
        return 1
    fi

    if [[ "${RELEASE_TEST_PUBLISHER:-0}" == "1" ]]; then
        mkdir -p "${RELEASE_GENERATIONS_ROOT}" || return 1
        chmod 0755 "${RELEASE_ROOT}" "${RELEASE_GENERATIONS_ROOT}" || return 1
    fi
    [[ -d "${RELEASE_ROOT}" && ! -L "${RELEASE_ROOT}" \
        && -d "${RELEASE_GENERATIONS_ROOT}" && ! -L "${RELEASE_GENERATIONS_ROOT}" ]] || {
        echo "[release] root-owned release directories are not provisioned" >&2
        return 1
    }
    if [[ "$(stat -c %u "${RELEASE_ROOT}")" != "${RELEASE_GENERATION_OWNER_UID}" \
        || "$(stat -c %u "${RELEASE_GENERATIONS_ROOT}")" != "${RELEASE_GENERATION_OWNER_UID}" \
        || -n "$(find "${RELEASE_ROOT}" "${RELEASE_GENERATIONS_ROOT}" -maxdepth 0 \
            -perm /022 -print -quit)" ]]; then
        echo "[release] release roots must be publisher-owned and protected" >&2
        return 1
    fi
    mkdir -p "${RELEASE_BUILD_ROOT}" || return 1
    chmod 0700 "${RELEASE_BUILD_ROOT}" || return 1
    release_real="$(realpath -e "${RELEASE_ROOT}")" || return 1
    if [[ "${release_real}" == "${bot_real}" || "${release_real}" == "${bot_real}"/* ]]; then
        echo "[release] release generations must be outside the checkout" >&2
        return 1
    fi
    if [[ "$(realpath -m "${RELEASE_GENERATIONS_ROOT}")" != "${release_real}"/* ]] \
        || [[ "$(realpath -m "${RELEASE_CURRENT_LINK}")" != "${release_real}"/* ]]; then
        echo "[release] generations and current must be inside RELEASE_ROOT" >&2
        return 1
    fi
    if [[ -e "${RELEASE_CURRENT_LINK}" && ! -L "${RELEASE_CURRENT_LINK}" ]]; then
        echo "[release] current must be a symbolic link" >&2
        return 1
    fi
    validate_release_publisher
}

_write_release_transaction() {
    /usr/bin/python3 - "$1" "$2" "$3" "$4" "$5" <<'PY'
import json
import os
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
operation, old_sha, new_sha, phase = sys.argv[2:]
if operation not in {"deploy", "rollback"}:
    raise SystemExit(2)
if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (old_sha, new_sha)):
    raise SystemExit(2)
if phase not in {"prepared", "activated", "restarting", "ready"}:
    raise SystemExit(2)
payload = {
    "format": "discord-bot-activation-v1",
    "operation": operation,
    "old_sha": old_sha,
    "new_sha": new_sha,
    "phase": phase,
    "restarted_consumers": [],
}
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(temporary, flags, 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
except BaseException:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    raise
PY
}

_read_release_transaction() {
    /usr/bin/python3 - "${RELEASE_ACTIVATION_JOURNAL}" <<'PY'
import json
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
required = {"format", "operation", "old_sha", "new_sha", "phase", "restarted_consumers"}
if not isinstance(payload, dict) or set(payload) != required:
    raise SystemExit(1)
if payload["format"] != "discord-bot-activation-v1":
    raise SystemExit(1)
if payload["operation"] not in {"deploy", "rollback"}:
    raise SystemExit(1)
if payload["phase"] not in {"prepared", "activated", "restarting", "ready"}:
    raise SystemExit(1)
if not all(re.fullmatch(r"[0-9a-f]{40}", payload[key]) for key in ("old_sha", "new_sha")):
    raise SystemExit(1)
consumers = payload["restarted_consumers"]
if not isinstance(consumers, list) or any(
    not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9@_.-]+", item)
    for item in consumers
) or len(consumers) != len(set(consumers)):
    raise SystemExit(1)
print("\t".join((
    payload["operation"], payload["old_sha"], payload["new_sha"],
    payload["phase"], " ".join(consumers),
)))
PY
}

_update_release_transaction() {
    local phase="$1" consumer="${2:-}"
    /usr/bin/python3 - "${RELEASE_ACTIVATION_JOURNAL}" "${phase}" "${consumer}" <<'PY'
import json
import os
from pathlib import Path
import re
import sys

path, phase, consumer = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
if phase not in {"activated", "restarting", "ready"}:
    raise SystemExit(2)
if consumer and not re.fullmatch(r"[A-Za-z0-9@_.-]+", consumer):
    raise SystemExit(2)
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("format") != "discord-bot-activation-v1":
    raise SystemExit(1)
if consumer and consumer not in payload["restarted_consumers"]:
    payload["restarted_consumers"].append(consumer)
payload["phase"] = phase
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
except BaseException:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    raise
PY
}

begin_release_transaction() {
    local operation="$1" old_sha="$2" new_sha="$3" replace="${4:-0}"
    if [[ -e "${RELEASE_ACTIVATION_JOURNAL}" || -L "${RELEASE_ACTIVATION_JOURNAL}" ]]; then
        [[ "${replace}" == "1" ]] || {
            echo "[release] incomplete activation transaction must be resumed first" >&2
            return 1
        }
    fi
    _write_release_transaction "${RELEASE_ACTIVATION_JOURNAL}" \
        "${operation}" "${old_sha}" "${new_sha}" prepared
}

activate_release_transaction() {
    local sha="$1"
    activate_release "${sha}" || return 1
    _update_release_transaction activated
}

restart_release_consumers_transactional() {
    local operation old_sha new_sha phase restarted service
    IFS=$'\t' read -r operation old_sha new_sha phase restarted \
        < <(_read_release_transaction) || return 1
    for service in ${RELEASE_CONSUMERS}; do
        if [[ " ${restarted} " == *" ${service} "* ]]; then
            continue
        fi
        echo "[release] restarting ${service}.service"
        sudo systemctl restart "${service}" || return 1
        _update_release_transaction restarting "${service}" || return 1
        restarted="${restarted} ${service}"
    done
}

complete_release_transaction() {
    _update_release_transaction ready || return 1
    rm -f -- "${RELEASE_ACTIVATION_JOURNAL}" || return 1
    _fsync_directory "$(dirname "${RELEASE_ACTIVATION_JOURNAL}")"
}

resume_release_transaction() {
    local operation old_sha new_sha phase restarted head_sha i
    RELEASE_RECOVERY_RESUMED=0
    [[ -e "${RELEASE_ACTIVATION_JOURNAL}" || -L "${RELEASE_ACTIVATION_JOURNAL}" ]] \
        || return 0
    [[ -f "${RELEASE_ACTIVATION_JOURNAL}" && ! -L "${RELEASE_ACTIVATION_JOURNAL}" ]] \
        || return 1
    IFS=$'\t' read -r operation old_sha new_sha phase restarted \
        < <(_read_release_transaction) || return 1
    RELEASE_RECOVERY_RESUMED=1
    RELEASE_RECOVERY_OPERATION="${operation}"
    RELEASE_RECOVERY_OLD_SHA="${old_sha}"
    RELEASE_RECOVERY_NEW_SHA="${new_sha}"
    echo "[release] resuming ${operation} activation ${old_sha} -> ${new_sha} (${phase})"
    [[ -z "$(git status --porcelain=v1 --untracked-files=all)" ]] || return 1
    head_sha="$(git rev-parse HEAD)" || return 1
    [[ "${head_sha}" == "${old_sha}" || "${head_sha}" == "${new_sha}" ]] || return 1
    if [[ "${head_sha}" != "${new_sha}" ]]; then
        git switch --detach "${new_sha}" || return 1
        git clean -ffdx --quiet || return 1
    fi
    validate_release_generation "${new_sha}" || return 1
    activate_release_transaction "${new_sha}" || return 1
    restart_release_consumers_transactional || return 1
    for ((i = 1; i <= RELEASE_RECOVERY_ATTEMPTS; i++)); do
        if release_ready_matches_sha "${new_sha}"; then
            complete_release_transaction || return 1
            return 0
        fi
        sleep "${RELEASE_RECOVERY_INTERVAL_SECONDS}"
    done
    return 1
}

validate_release_publisher() {
    local publisher_real release_real bot_real publisher_uid
    [[ "${RELEASE_PUBLISHER}" == /* && -f "${RELEASE_PUBLISHER}" \
        && ! -L "${RELEASE_PUBLISHER}" && -x "${RELEASE_PUBLISHER}" ]] || {
        echo "[release] root-owned release publisher is not provisioned" >&2
        return 1
    }
    [[ "${RELEASE_PUBLISHER_USE_SUDO}" == "0" \
        || "${RELEASE_PUBLISHER_USE_SUDO}" == "1" ]] || return 1
    [[ "${RELEASE_GENERATION_OWNER_UID}" =~ ^[0-9]+$ ]] || return 1
    publisher_real="$(realpath -e "${RELEASE_PUBLISHER}")" || return 1
    release_real="$(realpath -e "${RELEASE_ROOT}")" || return 1
    bot_real="$(realpath -e "${BOT_DIR}")" || return 1
    publisher_uid="$(stat -c %u "${publisher_real}")" || return 1
    if [[ "${publisher_real}" == "${bot_real}"/* ]]; then
        echo "[release] release publisher must be outside the checkout" >&2
        return 1
    fi
    if [[ "${publisher_uid}" != "0" ]] \
        || [[ -n "$(find "${publisher_real}" -maxdepth 0 -perm /022 -print -quit)" ]]; then
        if [[ "${RELEASE_TEST_PUBLISHER:-0}" != "1" \
            || "${release_real}" != /tmp/* \
            || "${RELEASE_PUBLISHER_USE_SUDO}" != "0" \
            || "${RELEASE_GENERATION_OWNER_UID}" != "$(id -u)" \
            || "${publisher_uid}" != "$(id -u)" ]]; then
            echo "[release] release publisher must be root-owned and protected" >&2
            return 1
        fi
    fi
}

release_generation_path() {
    printf '%s/%s' "${RELEASE_GENERATIONS_ROOT}" "$1"
}

release_venv_path() {
    printf '%s/venv' "$(release_generation_path "$1")"
}

release_source_path() {
    printf '%s/src' "$(release_generation_path "$1")"
}

validate_release_generation() {
    local sha="$1" generation venv marker metadata source_marker content_manifest
    [[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || return 1
    generation="$(release_generation_path "${sha}")"
    venv="${generation}/venv"
    marker="${generation}/.release-sha"
    source_marker="${generation}/src/.release-source-sha"
    metadata="${generation}/release.json"
    content_manifest="${generation}/release-content.jsonl"

    [[ -d "${generation}" && ! -L "${generation}" ]] || return 1
    [[ -d "${generation}/src" && ! -L "${generation}/src" ]] || return 1
    [[ -d "${venv}" && ! -L "${venv}" ]] || return 1
    [[ -x "${venv}/bin/python" ]] || return 1
    [[ -f "${marker}" && ! -L "${marker}" \
        && "$(tr -d '[:space:]' <"${marker}")" == "${sha}" ]] || return 1
    [[ -f "${source_marker}" && ! -L "${source_marker}" \
        && "$(tr -d '[:space:]' <"${source_marker}")" == "${sha}" ]] || return 1
    [[ -f "${metadata}" && ! -L "${metadata}" ]] || return 1
    [[ -f "${content_manifest}" && ! -L "${content_manifest}" ]] || return 1
    [[ "$(stat -c %u "${generation}")" == "${RELEASE_GENERATION_OWNER_UID:-0}" ]] \
        || return 1
    [[ -z "$(find "${generation}" -xdev ! -uid "${RELEASE_GENERATION_OWNER_UID:-0}" \
        -print -quit)" ]] || return 1
    /usr/bin/python3 - "${generation}" "${metadata}" "${content_manifest}" \
        "${sha}" "${RELEASE_GENERATION_OWNER_UID:-0}" <<'PY' || return 1
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

generation, path, manifest = map(Path, sys.argv[1:4])
expected, owner_uid_text = sys.argv[4:]
owner_uid = int(owner_uid_text)
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("format") != "discord-bot-release-v2":
    raise SystemExit(1)
if payload.get("release_sha") != expected:
    raise SystemExit(1)
if payload.get("generation") != expected:
    raise SystemExit(1)
if payload.get("content_manifest_sha256") != hashlib.sha256(manifest.read_bytes()).hexdigest():
    raise SystemExit(1)

expected_files: dict[str, tuple[int, str]] = {}
for line in manifest.read_text(encoding="utf-8").splitlines():
    record = json.loads(line)
    if not isinstance(record, dict) or set(record) != {"path", "mode", "sha256"}:
        raise SystemExit(1)
    relative = record["path"]
    relative_path = Path(relative) if isinstance(relative, str) else Path("/")
    if (
        not isinstance(relative, str)
        or relative_path.is_absolute()
        or relative_path.parts[:1] not in {("src",), ("venv",)}
        or ".." in relative_path.parts
        or relative in expected_files
        or not isinstance(record["mode"], int)
        or not isinstance(record["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
    ):
        raise SystemExit(1)
    expected_files[relative] = (record["mode"], record["sha256"])

actual_files: dict[str, tuple[int, str]] = {}
for subtree in (generation / "src", generation / "venv"):
    for current, directories, files in os.walk(subtree, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            object_stat = (current_path / name).lstat()
            if object_stat.st_uid != owner_uid:
                raise SystemExit(1)
            if not stat.S_ISLNK(object_stat.st_mode) and stat.S_IMODE(object_stat.st_mode) & 0o222:
                raise SystemExit(1)
        for name in files:
            object_path = current_path / name
            object_stat = object_path.lstat()
            if not stat.S_ISREG(object_stat.st_mode):
                continue
            actual_files[object_path.relative_to(generation).as_posix()] = (
                stat.S_IMODE(object_stat.st_mode),
                hashlib.sha256(object_path.read_bytes()).hexdigest(),
            )
if actual_files != expected_files or not actual_files:
    raise SystemExit(1)
PY
    [[ -z "$(find "${generation}" -xdev ! -type l -perm /222 -print -quit)" ]] \
        || return 1
    "${venv}/bin/python" -m pip check >/dev/null || return 1
    "${venv}/bin/python" -c \
        'import sys; raise SystemExit(sys.prefix == sys.base_prefix)' || return 1
}

# Compatibility name retained for release_gate and existing deploy callers.
validate_release_venv() {
    validate_release_generation "$1"
}

_write_generation_metadata() {
    local path="$1" sha="$2" content_manifest_digest="$3"
    /usr/bin/python3 - "${path}" "${sha}" "${content_manifest_digest}" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, sha, content_manifest_digest = sys.argv[1:]
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(path, flags, 0o644)
try:
    payload = {
        "format": "discord-bot-release-v2",
        "release_sha": sha,
        "generation": sha,
        "content_manifest_sha256": content_manifest_digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
except BaseException:
    try:
        os.close(fd)
    except OSError:
        pass
    raise
PY
}

_write_release_content_manifest() {
    /usr/bin/python3 - "$1" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

generation = Path(sys.argv[1])
manifest = generation / "release-content.jsonl"
records: list[str] = []
for subtree_name in ("src", "venv"):
    subtree = generation / subtree_name
    for current, _directories, files in os.walk(subtree, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            path_stat = path.lstat()
            if not stat.S_ISREG(path_stat.st_mode):
                continue
            record = {
                "mode": stat.S_IMODE(path_stat.st_mode),
                "path": path.relative_to(generation).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            records.append(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
if not records:
    raise SystemExit("release content manifest is empty")
manifest.write_text("".join(sorted(records)), encoding="utf-8")
with manifest.open("rb") as stream:
    os.fsync(stream.fileno())
print(hashlib.sha256(manifest.read_bytes()).hexdigest())
PY
}

_publish_release_generation() {
    local temporary="$1" generation="$2"
    validate_release_publisher || return 1
    if [[ "${RELEASE_PUBLISHER_USE_SUDO}" == "1" ]]; then
        sudo -- "${RELEASE_PUBLISHER}" publish "${temporary}" "${generation}" \
            "${RELEASE_GENERATIONS_ROOT}" "${RELEASE_BUILD_ROOT}" \
            "${RELEASE_GENERATION_OWNER_UID}"
    else
        RELEASE_PUBLISHER_TEST_MODE="${RELEASE_TEST_PUBLISHER:-0}" \
            "${RELEASE_PUBLISHER}" publish "${temporary}" "${generation}" \
            "${RELEASE_GENERATIONS_ROOT}" "${RELEASE_BUILD_ROOT}" \
            "${RELEASE_GENERATION_OWNER_UID}"
    fi
}

_relocate_venv_scripts() {
    local venv="$1" future_venv="$2"
    /usr/bin/python3 - "${venv}" "${future_venv}" <<'PY'
from pathlib import Path
import sys

venv = Path(sys.argv[1])
old = str(venv).encode()
new = sys.argv[2].encode()
for path in (venv / "bin").iterdir():
    if not path.is_file() or path.is_symlink():
        continue
    data = path.read_bytes()
    if (
        not data.startswith(b"#!")
        and not path.name.startswith("activate")
        and path.name != "Activate.ps1"
    ):
        continue
    if old not in data:
        continue
    path.write_bytes(data.replace(old, new))
PY
}

_fsync_directory() {
    /usr/bin/python3 - "$1" <<'PY'
import os
import sys

fd = os.open(sys.argv[1], os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(fd)
finally:
    os.close(fd)
PY
}

_fsync_tree() {
    /usr/bin/python3 - "$1" <<'PY'
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
for current, directories, files in os.walk(root, topdown=False, followlinks=False):
    current_path = Path(current)
    for name in files:
        path = current_path / name
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            continue
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    for name in directories:
        path = current_path / name
        if path.is_symlink():
            continue
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

build_release_venv() {
    local sha="$1" generation temporary archive venv final_venv content_manifest_digest
    if ! [[ "${sha}" =~ ^[0-9a-f]{40}$ ]] \
        || ! git cat-file -e "${sha}^{commit}" 2>/dev/null; then
        echo "[release] invalid release commit: ${sha}" >&2
        return 1
    fi
    generation="$(release_generation_path "${sha}")"
    if [[ -e "${generation}" || -L "${generation}" ]]; then
        if validate_release_generation "${sha}"; then
            return 0
        fi
        echo "[release] existing immutable generation is invalid; refusing deletion: ${sha}" >&2
        return 1
    fi

    temporary="$(mktemp -d "${RELEASE_BUILD_ROOT}/.${sha}.building.XXXXXX")" \
        || return 1
    archive="${temporary}/source.tar"
    if ! mkdir -p "${temporary}/src"; then
        rm -rf -- "${temporary}"
        return 1
    fi
    if ! git archive --format=tar --output="${archive}" "${sha}" \
        || ! tar -xf "${archive}" -C "${temporary}/src"; then
        rm -rf -- "${temporary}"
        return 1
    fi
    if ! rm -f -- "${archive}" \
        || ! mkdir -p "${temporary}/src/.state" \
        || ! printf '%s\n' "${sha}" >"${temporary}/src/.release-source-sha"; then
        rm -rf -- "${temporary}"
        return 1
    fi

    venv="${temporary}/venv"
    if ! /usr/bin/python3 -m venv "${venv}"; then
        rm -rf -- "${temporary}"
        return 1
    fi
    if [[ -f "${temporary}/src/requirements.lock" ]] \
        && ! (cd "${temporary}/src" && "${venv}/bin/python" -m pip install \
            --requirement requirements.lock); then
        rm -rf -- "${temporary}"
        return 1
    fi
    if ! "${venv}/bin/python" -m pip check >/dev/null \
        || ! "${venv}/bin/python" -c \
            'import sys; raise SystemExit(sys.prefix == sys.base_prefix)'; then
        rm -rf -- "${temporary}"
        return 1
    fi

    final_venv="${generation}/venv"
    if ! _relocate_venv_scripts "${venv}" "${final_venv}"; then
        rm -rf -- "${temporary}"
        return 1
    fi
    if ! printf '%s\n' "${sha}" >"${temporary}/.release-sha" \
        || ! printf '%s\n' "${sha}" >"${temporary}/venv/.release-sha"; then
        rm -rf -- "${temporary}"
        return 1
    fi
    if ! chmod -R a+rX,go-w "${temporary}" \
        || ! chmod -R a-w "${temporary}/src" "${temporary}/venv"; then
        rm -rf -- "${temporary}"
        return 1
    fi
    content_manifest_digest="$(_write_release_content_manifest "${temporary}")" || {
        rm -rf -- "${temporary}"
        return 1
    }
    if ! _write_generation_metadata "${temporary}/release.json" "${sha}" \
        "${content_manifest_digest}" \
        || ! chmod a+r,a-w "${temporary}/.release-sha" \
            "${temporary}/release.json" "${temporary}/release-content.jsonl" \
        || ! chmod a-w "${temporary}" \
        || ! _fsync_tree "${temporary}"; then
        rm -rf -- "${temporary}"
        return 1
    fi

    # The directory rename is the sole publication of a fully built generation.
    # Existing generations, including current and rollback candidates, are never removed.
    if ! _publish_release_generation "${temporary}" "${generation}"; then
        rm -rf -- "${temporary}"
        return 1
    fi
    _fsync_directory "${RELEASE_GENERATIONS_ROOT}" || return 1
    if ! validate_release_generation "${sha}"; then
        echo "[release] published generation failed validation; refusing deletion: ${sha}" >&2
        return 1
    fi
}

activate_release() {
    local sha="$1" target
    target="generations/${sha}"
    validate_release_generation "${sha}" || {
        echo "[release] release generation is incomplete: ${sha}" >&2
        return 1
    }
    if [[ "${RELEASE_PUBLISHER_USE_SUDO}" == "1" ]]; then
        sudo -- "${RELEASE_PUBLISHER}" activate "${sha}" "${RELEASE_ROOT}" \
            "${RELEASE_GENERATION_OWNER_UID}" || return 1
    else
        RELEASE_PUBLISHER_TEST_MODE="${RELEASE_TEST_PUBLISHER:-0}" \
            "${RELEASE_PUBLISHER}" activate "${sha}" "${RELEASE_ROOT}" \
            "${RELEASE_GENERATION_OWNER_UID}" || return 1
    fi
    [[ "$(readlink "${RELEASE_CURRENT_LINK}")" == "${target}" ]] \
        && validate_release_generation "${sha}"
}

release_current_matches_sha() {
    local sha="$1"
    [[ -L "${RELEASE_CURRENT_LINK}" \
        && "$(readlink "${RELEASE_CURRENT_LINK}")" == "generations/${sha}" ]] \
        && validate_release_generation "${sha}"
}

validate_release_coordination() {
    local service port url
    for service in ${RELEASE_CONSUMERS:?RELEASE_CONSUMERS is required}; do
        [[ "${service}" =~ ^[A-Za-z0-9@_.-]+$ ]] || return 1
    done
    for port in ${RELEASE_READY_PORTS:?RELEASE_READY_PORTS is required}; do
        [[ "${port}" =~ ^[0-9]+$ ]] \
            && ((10#${port} >= 1 && 10#${port} <= 65535)) || return 1
    done
    for url in ${RELEASE_LIVENESS_URLS:-}; do
        [[ "${url}" =~ ^http://127\.0\.0\.1:[0-9]+/[A-Za-z0-9_./-]+$ ]] || return 1
    done
}

release_consumers_provisioned() {
    local service
    for service in ${RELEASE_CONSUMERS}; do
        if ! systemctl cat "${service}.service" >/dev/null 2>&1; then
            echo "[release] required consumer ${service}.service is not provisioned" >&2
            return 1
        fi
    done
}

restart_release_consumers() {
    local service
    for service in ${RELEASE_CONSUMERS}; do
        echo "[release] restarting ${service}.service"
        sudo systemctl restart "${service}" || return 1
    done
}

release_consumers_active() {
    local service
    for service in ${RELEASE_CONSUMERS}; do
        systemctl is-active --quiet "${service}.service" || return 1
    done
}

release_liveness_urls_ready() {
    local url
    for url in ${RELEASE_LIVENESS_URLS:-}; do
        curl -fsS --max-time 5 -- "${url}" >/dev/null || return 1
    done
}

release_ready_matches_sha() {
    local expected_sha="$1" port payload
    for port in ${RELEASE_READY_PORTS}; do
        payload="$(curl -fsS --max-time 5 -- "http://127.0.0.1:${port}/ready")" \
            || return 1
        /usr/bin/python3 -c '
import json, sys
try:
    payload = json.loads(sys.stdin.read())
except (json.JSONDecodeError, UnicodeError):
    raise SystemExit(1)
expected = sys.argv[1]
if not isinstance(payload, dict):
    raise SystemExit(1)
if payload.get("status") != "ok" or payload.get("release_sha") != expected:
    raise SystemExit(1)
' "${expected_sha}" <<<"${payload}" || return 1
    done
    release_consumers_active && release_liveness_urls_ready
}
