"""Contract and subprocess tests for Phase 0B/Phase 4 operational gates."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUTO_DEPLOY = ROOT / "scripts" / "auto_deploy.sh"
RELEASE_GATE = ROOT / "scripts" / "release_gate.sh"


@pytest.fixture(autouse=True)
def _restore_tmp_cleanup_permissions(tmp_path: Path):
    yield
    for current, directories, files in os.walk(tmp_path, topdown=True):
        current_path = Path(current)
        if not current_path.is_symlink():
            current_path.chmod(stat.S_IMODE(current_path.stat().st_mode) | 0o700)
        for name in directories:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o700)
        for name in files:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o600)


def _test_publisher_env(prefix: str) -> dict[str, str]:
    return {
        f"{prefix}RELEASE_PUBLISHER": str(ROOT / "scripts/release_publisher.sh"),
        f"{prefix}PUBLISHER_USE_SUDO": "0",
        f"{prefix}GENERATION_OWNER_UID": str(os.getuid()),
        "RELEASE_TEST_PUBLISHER": "1",
    }


def _run(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        args,
        cwd=ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ops-test@example.invalid")
    _git(repo, "config", "user.name", "ops-test")
    (repo / ".gitignore").write_text(".state/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _deploy_env(repo: Path) -> dict[str, str]:
    state = repo.parent / "deploy-trust-state"
    release = repo.parent / "release-artifacts"
    fake_bin = repo.parent / "deploy-bin"
    fake_bin.mkdir(exist_ok=True)
    _install_fake_systemctl(fake_bin)
    return {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AUTO_DEPLOY_BOT_DIR": str(repo),
        "AUTO_DEPLOY_STATE_DIR": str(state),
        "AUTO_DEPLOY_LOCK_FILE": str(state / "deploy.lock"),
        "AUTO_DEPLOY_ROLLBACK_PIN_FILE": str(state / "rollback.pin"),
        "AUTO_DEPLOY_RELEASE_ROOT": str(release),
        "AUTO_DEPLOY_TEST_MODE": "1",
        **_test_publisher_env("AUTO_DEPLOY_"),
    }


def _release_gate_venv(tmp_path: Path, target: str) -> Path:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    _write_executable(venv / "bin" / "python", "#!/usr/bin/env bash\nexit 0\n")
    (venv / ".release-sha").write_text(target + "\n", encoding="utf-8")
    return venv


def _snapshot_inventory(tmp_path: Path, **roots: Path) -> Path:
    inventory = tmp_path / "state-inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "format": "discord-bot-state-inventory-v1",
                "services": [
                    {"name": name, "path": str(path)} for name, path in roots.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    inventory.chmod(0o600)
    return inventory


def _immutable_release_fixture(release_root: Path, sha: str) -> Path:
    generation = release_root / "generations" / sha
    (generation / "src/.state").mkdir(parents=True)
    (generation / "venv/bin").mkdir(parents=True)
    files = {
        generation / "src/.release-source-sha": f"{sha}\n".encode(),
        generation / "venv/.release-sha": f"{sha}\n".encode(),
        generation / "venv/bin/python": b"#!/usr/bin/env bash\nexit 0\n",
    }
    for path, content in files.items():
        path.write_bytes(content)
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    (generation / "venv/bin/python").chmod(0o555)
    manifest = generation / "release-content.jsonl"
    records = []
    for path in sorted(files, key=lambda item: item.as_posix()):
        records.append(
            json.dumps(
                {
                    "mode": stat.S_IMODE(path.stat().st_mode),
                    "path": path.relative_to(generation).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
    manifest.write_text("".join(records), encoding="utf-8")
    metadata = generation / "release.json"
    metadata.write_text(
        json.dumps(
            {
                "format": "discord-bot-release-v2",
                "release_sha": sha,
                "generation": sha,
                "content_manifest_sha256": hashlib.sha256(
                    manifest.read_bytes()
                ).hexdigest(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (generation / ".release-sha").write_text(f"{sha}\n", encoding="ascii")
    for path in generation.rglob("*"):
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    generation.chmod(0o555)
    return generation


def _protected_manifest(repo: Path, root: Path, target: str) -> tuple[Path, str]:
    payload = subprocess.run(
        ["git", "show", f"{target}:tracked.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    manifest = root / "protected.sha256"
    manifest.write_text(
        f"{hashlib.sha256(payload).hexdigest()}  tracked.txt\n", encoding="utf-8"
    )
    return manifest, hashlib.sha256(manifest.read_bytes()).hexdigest()


def test_auto_deploy_refuses_dirty_worktree_before_fetch(tmp_path: Path):
    repo, _ = _make_repo(tmp_path)
    (repo / "other-owner.txt").write_text("do not overwrite\n", encoding="utf-8")

    result = _run(str(AUTO_DEPLOY), "prod", env=_deploy_env(repo))

    assert result.returncode == 4
    assert "worktree is dirty" in result.stdout
    assert "other-owner.txt" in result.stdout
    assert "Fetching origin" not in result.stdout


def test_auto_deploy_skips_when_flock_is_owned(tmp_path: Path):
    repo, _ = _make_repo(tmp_path)
    env = _deploy_env(repo)
    lock_path = Path(env["AUTO_DEPLOY_LOCK_FILE"])
    lock_path.parent.mkdir(parents=True)

    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run(str(AUTO_DEPLOY), "prod", env=env)

    assert result.returncode == 0
    assert "Another deploy or rollback owns" in result.stdout
    assert "Fetching origin" not in result.stdout


def test_auto_deploy_skips_active_rollback_pin(tmp_path: Path):
    repo, _ = _make_repo(tmp_path)
    env = _deploy_env(repo)
    pin = Path(env["AUTO_DEPLOY_ROLLBACK_PIN_FILE"])
    pin.parent.mkdir(parents=True)
    pin.write_text("target_sha=" + "a" * 40 + "\n", encoding="utf-8")

    result = _run(str(AUTO_DEPLOY), "prod", env=env)

    assert result.returncode == 0
    assert "Rollback pin is active" in result.stdout
    assert "Fetching origin" not in result.stdout


def test_auto_deploy_rejects_release_artifacts_inside_checkout(tmp_path: Path):
    repo, _ = _make_repo(tmp_path)
    env = _deploy_env(repo)
    unsafe_release = repo / "release-artifacts"
    env["AUTO_DEPLOY_RELEASE_ROOT"] = str(unsafe_release)

    result = _run(str(AUTO_DEPLOY), "prod", env=env)

    assert result.returncode == 4
    assert "external RELEASE_ROOT" in result.stderr
    assert not unsafe_release.exists()


def test_dev_release_gate_runs_smoke_at_exact_target(tmp_path: Path):
    repo, target = _make_repo(tmp_path)
    marker = tmp_path / "smoke-ran"
    smoke = tmp_path / "smoke.sh"
    smoke.write_text(
        '#!/usr/bin/env bash\nset -eu\nprintf \'%s\' "$BOT_DIR" >"$SMOKE_MARKER"\n',
        encoding="utf-8",
    )
    smoke.chmod(smoke.stat().st_mode | stat.S_IXUSR)

    result = _run(
        str(RELEASE_GATE),
        "dev",
        target,
        env={
            "RELEASE_GATE_BOT_DIR": str(repo),
            "RELEASE_GATE_SMOKE_SCRIPT": str(smoke),
            "RELEASE_GATE_VENV": str(_release_gate_venv(tmp_path, target)),
            "SMOKE_MARKER": str(marker),
        },
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == str(repo)


def test_dev_release_gate_rejects_checkout_local_shared_venv(tmp_path: Path):
    repo, target = _make_repo(tmp_path)
    local_venv = repo / ".venv"
    (local_venv / "bin").mkdir(parents=True)
    _write_executable(local_venv / "bin/python", "#!/usr/bin/env bash\nexit 0\n")
    (local_venv / ".release-sha").write_text(target + "\n", encoding="utf-8")
    smoke = _write_executable(tmp_path / "smoke", "#!/usr/bin/env bash\nexit 0\n")

    result = _run(
        str(RELEASE_GATE),
        "dev",
        target,
        env={
            "RELEASE_GATE_BOT_DIR": str(repo),
            "RELEASE_GATE_SMOKE_SCRIPT": str(smoke),
            "RELEASE_GATE_VENV": str(local_venv),
        },
    )

    assert result.returncode == 3
    assert "must be outside the checkout" in result.stdout


def test_prod_release_gate_requires_staging_and_restore_rehearsal(tmp_path: Path):
    repo, _ = _make_repo(tmp_path)
    (repo / "tracked.txt").write_text("target\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "target")
    target_sha = _git(repo, "rev-parse", "HEAD")

    trusted_state = tmp_path / "trusted-state"
    receipt = trusted_state / "dev" / "successful.sha"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(target_sha + "\n", encoding="utf-8")
    manifest, manifest_digest = _protected_manifest(repo, tmp_path, target_sha)
    snapshot_dir = tmp_path / "snapshot"
    snapshot = tmp_path / "snapshot.sh"
    snapshot.write_text(
        """#!/usr/bin/env bash
set -eu
if [[ "${1:-}" == "--verify" ]]; then
    [[ "$2" == "$SNAPSHOT_DIR" ]]
    printf 'verified' >"$VERIFY_MARKER"
    exit 0
fi
mkdir -p "$SNAPSHOT_DIR"
printf '[snapshot] done: %s\n' "$SNAPSHOT_DIR"
""",
        encoding="utf-8",
    )
    snapshot.chmod(snapshot.stat().st_mode | stat.S_IXUSR)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' \'{{"status":"ok","release_sha":"{target_sha}"}}\'\n',
        encoding="utf-8",
    )
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    verified = tmp_path / "verified"

    result = _run(
        str(RELEASE_GATE),
        "prod",
        target_sha,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RELEASE_GATE_BOT_DIR": str(repo),
            "RELEASE_GATE_TRUSTED_STATE_ROOT": str(trusted_state),
            "RELEASE_GATE_STAGING_RECEIPT": str(receipt),
            "RELEASE_GATE_PROTECTED_MANIFEST": str(manifest),
            "RELEASE_GATE_PROTECTED_MANIFEST_SHA256": manifest_digest,
            "RELEASE_GATE_SNAPSHOT_SCRIPT": str(snapshot),
            "SNAPSHOT_DIR": str(snapshot_dir),
            "VERIFY_MARKER": str(verified),
        },
    )

    assert result.returncode == 0, result.stderr
    assert verified.read_text(encoding="utf-8") == "verified"
    assert "backup/restore rehearsal passed" in result.stdout


def test_prod_release_gate_rejects_ancestor_receipt_for_new_target(tmp_path: Path):
    repo, ancestor_sha = _make_repo(tmp_path)
    (repo / "tracked.txt").write_text("target\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "target")
    target_sha = _git(repo, "rev-parse", "HEAD")
    trusted_state = tmp_path / "trusted-state"
    receipt = trusted_state / "dev" / "successful.sha"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(ancestor_sha + "\n", encoding="utf-8")

    result = _run(
        str(RELEASE_GATE),
        "prod",
        target_sha,
        env={
            "RELEASE_GATE_BOT_DIR": str(repo),
            "RELEASE_GATE_TRUSTED_STATE_ROOT": str(trusted_state),
            "RELEASE_GATE_STAGING_RECEIPT": str(receipt),
        },
    )

    assert result.returncode == 3
    assert "does not exactly match target" in result.stdout


def test_prod_release_gate_rejects_manifest_from_checkout(tmp_path: Path):
    repo, target_sha = _make_repo(tmp_path)
    trusted_state = tmp_path / "trusted-state"
    receipt = trusted_state / "dev" / "successful.sha"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(target_sha + "\n", encoding="utf-8")
    manifest = repo / "self-approved.sha256"
    manifest.write_text("0" * 64 + "  tracked.txt\n", encoding="utf-8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' \'{{"status":"ok","release_sha":"{target_sha}"}}\'\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)

    result = _run(
        str(RELEASE_GATE),
        "prod",
        target_sha,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RELEASE_GATE_BOT_DIR": str(repo),
            "RELEASE_GATE_TRUSTED_STATE_ROOT": str(trusted_state),
            "RELEASE_GATE_STAGING_RECEIPT": str(receipt),
            "RELEASE_GATE_PROTECTED_MANIFEST": str(manifest),
            "RELEASE_GATE_PROTECTED_MANIFEST_SHA256": digest,
        },
    )

    assert result.returncode == 3
    assert "must be rooted outside the checkout" in result.stdout


def test_prod_release_gate_rejects_non_json_staging_200(tmp_path: Path):
    repo, target_sha = _make_repo(tmp_path)
    trusted_state = tmp_path / "trusted-state"
    receipt = trusted_state / "dev" / "successful.sha"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(target_sha + "\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "curl",
        "#!/usr/bin/env bash\nprintf '<html>not health</html>\\n'\n",
    )

    result = _run(
        str(RELEASE_GATE),
        "prod",
        target_sha,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RELEASE_GATE_BOT_DIR": str(repo),
            "RELEASE_GATE_TRUSTED_STATE_ROOT": str(trusted_state),
            "RELEASE_GATE_STAGING_RECEIPT": str(receipt),
        },
    )

    assert result.returncode == 3
    assert "readiness payload is not status=ok" in result.stdout


def test_prod_release_gate_rejects_ready_process_at_wrong_sha(tmp_path: Path):
    repo, target_sha = _make_repo(tmp_path)
    trusted_state = tmp_path / "trusted-state"
    receipt = trusted_state / "dev" / "successful.sha"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(target_sha + "\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "curl",
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' \'{{"status":"ok","release_sha":"{"b" * 40}"}}\'\n',
    )

    result = _run(
        str(RELEASE_GATE),
        "prod",
        target_sha,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RELEASE_GATE_BOT_DIR": str(repo),
            "RELEASE_GATE_TRUSTED_STATE_ROOT": str(trusted_state),
            "RELEASE_GATE_STAGING_RECEIPT": str(receipt),
        },
    )

    assert result.returncode == 3
    assert "not status=ok at target" in result.stdout


def test_oracle_gateway_refuses_start_without_loopback_patch(tmp_path: Path):
    node = tmp_path / "node"
    node.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
    node.chmod(node.stat().st_mode | stat.S_IXUSR)
    gateway_root = tmp_path / "supergateway"
    implementation = gateway_root / "dist" / "gateways"
    implementation.mkdir(parents=True)
    (gateway_root / "dist" / "index.js").write_text("", encoding="utf-8")
    (implementation / "stdioToStatelessStreamableHttp.js").write_text(
        "app.listen(port, () => {});\n", encoding="utf-8"
    )

    result = _run(
        str(ROOT / "scripts" / "oracle_supergateway_launch.sh"),
        env={
            "HOME": str(tmp_path / "empty-home"),
            "ORACLE_NODE_BIN": str(node),
            "ORACLE_SUPERGATEWAY_ROOT": str(gateway_root),
        },
    )

    assert result.returncode == 4
    assert "loopback bind patch is absent" in result.stderr


def test_oracle_child_requires_dedicated_environment_secret(tmp_path: Path):
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        [str(ROOT / "scripts" / "oracle_mcp_launch.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "OPENAI_API_KEY must be injected" in result.stderr


def test_systemd_units_use_service_secrets_and_hardening():
    expected_env = {
        ROOT / "deploy/systemd/discord-bot.service": "discord-bot.env",
        ROOT / "deploy/systemd/discord-bot-dev.service": "discord-bot-dev.env",
        ROOT / "deploy/systemd/hermes-discord-codex.service": "hermes-discord.env",
        ROOT / "deploy/systemd/vi-signal-scheduler.service": "vi-signal.env",
        ROOT / "deploy/systemd/intervention-scheduler.service": "intervention.env",
        ROOT / "deploy/systemd/remote-opencode.service": "remote-opencode.env",
        ROOT / "infra/systemd/mcp-mem0-shared.service": "mcp-mem0.env",
        ROOT / "infra/systemd/mcp-oracle-shared.service": "mcp-oracle.env",
        ROOT
        / "infra/systemd/mcp-second-opinion-shared.service": "mcp-second-opinion.env",
        ROOT / "infra/systemd/mcp-x-scraper-shared.service": "mcp-x-scraper.env",
        ROOT / "infra/systemd/kanae-mcp-forward.service": "mcp-forward.env",
        ROOT / "infra/systemd/kanae-systemd-forward.service": "systemd-forward.env",
        ROOT / "infra/systemd/discord-bot-autodeploy.service": "deploy.env",
        ROOT / "infra/systemd/discord-bot-dev-autodeploy.service": "deploy.env",
    }

    for unit, env_name in expected_env.items():
        text = unit.read_text(encoding="utf-8")
        assert f"EnvironmentFile=/etc/discord-bot/credentials/{env_name}" in text
        assert not re.search(r"^User=ubuntu$", text, re.MULTILINE)
        assert "ProtectSystem=strict" in text
        assert "ProtectHome=read-only" in text


def test_runtime_units_have_distinct_uids_and_bot_cannot_write_checkout():
    units = [
        *sorted((ROOT / "deploy/systemd").glob("*.service")),
        *sorted((ROOT / "infra/systemd").glob("*.service")),
    ]
    runtime_units = [unit for unit in units if "autodeploy" not in unit.name]
    users = {}
    for unit in runtime_units:
        text = unit.read_text(encoding="utf-8")
        match = re.search(r"^User=(.+)$", text, re.MULTILINE)
        if match:
            assert match.group(1) != "ubuntu"
            users[unit.name] = match.group(1)
    assert users["discord-bot.service"] == "kanae-bot"
    assert users["discord-bot-dev.service"] == "kanae-bot-dev"
    assert users["mcp-mem0-shared.service"] == "kanae-mem0"
    assert len(set(users.values())) == len(users)
    bot_unit = (ROOT / "deploy/systemd/discord-bot.service").read_text(encoding="utf-8")
    read_write_line = next(
        line for line in bot_unit.splitlines() if line.startswith("ReadWritePaths=")
    )
    assert "/home/ubuntu/discord-bot " not in read_write_line
    assert "/home/ubuntu/discord-bot/.state" not in read_write_line
    assert "Environment=BOT_STATE_DIR=/var/lib/kanae-bot/state" in bot_unit
    for unit in runtime_units:
        text = unit.read_text(encoding="utf-8")
        if "/home/ubuntu" in text:
            assert "kanae-code" in text
        assert "kanae-state" not in text


def test_state_writers_use_private_service_directories():
    expected = {
        ROOT / "deploy/systemd/discord-bot.service": "/var/lib/kanae-bot/state",
        ROOT / "deploy/systemd/discord-bot-dev.service": "/var/lib/kanae-bot-dev/state",
        ROOT
        / "deploy/systemd/hermes-discord-codex.service": "/var/lib/kanae-hermes/state",
        ROOT
        / "deploy/systemd/vi-signal-scheduler.service": "/var/lib/kanae-vi-signal/state",
        ROOT
        / "deploy/systemd/intervention-scheduler.service": "/var/lib/kanae-intervention/state",
        ROOT / "infra/systemd/mcp-mem0-shared.service": "/var/lib/kanae-mem0/state",
        ROOT
        / "infra/systemd/mcp-second-opinion-shared.service": "/var/lib/kanae-second-opinion/state",
    }
    for unit, state_dir in expected.items():
        text = unit.read_text(encoding="utf-8")
        assert f"Environment=BOT_STATE_DIR={state_dir}" in text
        assert "/home/ubuntu/discord-bot/.state " not in text
        assert "SupplementaryGroups=kanae-state" not in text
        if "scheduler.service" in unit.name:
            assert (
                f"BindPaths={state_dir}:"
                "/var/lib/discord-bot-releases/prod/current/src/.state"
            ) in text

    chroma = (ROOT / "infra/systemd/chromadb-server.service").read_text(
        encoding="utf-8"
    )
    assert "--path /var/lib/kanae-chroma/mem0_db" in chroma
    assert "/home/ubuntu/discord-bot/.state/mem0_db" not in chroma


def test_security_procedure_covers_home_acl_oauth_and_state_migration():
    security = (ROOT / "deploy/OPERATIONS_SECURITY.md").read_text(encoding="utf-8")

    assert "setfacl -m g:kanae-code:--x /home/ubuntu" in security
    assert "/var/lib/discord-bot-releases/prod/current/venv" in security
    assert "/var/lib/kanae-bot/.claude/.credentials.json" in security
    assert "/var/lib/kanae-bot-dev /root/oauth-stage/dev" in security
    assert "/var/lib/kanae-hermes /root/oauth-stage/hermes" in security
    assert ".credentials-failover.json" in security
    assert 'payload.get("refreshToken")' in security
    assert "-m 0600 --" in security
    assert "BOT_STATE_DIR" in security
    assert "Do not blanket-copy the bot state into an MCP state root" in security
    assert "backup-state.inventory" in security
    assert "for name in .env .state .claude" in security
    assert security.index("for name in .env .state .claude") < security.index(
        "sudo chown -R kanae-deploy:kanae-deploy"
    )
    assert "/home/ubuntu/discord-bot-dev" in security

    staging = (ROOT / "deploy/systemd/discord-bot-dev.service").read_text(
        encoding="utf-8"
    )
    assert "User=kanae-bot-dev" in staging
    assert "WorkingDirectory=/var/lib/discord-bot-releases/dev/current/src" in staging
    assert "/var/lib/discord-bot-releases/dev/current/venv" in staging

    for unit in (
        ROOT / "infra/systemd/discord-bot-autodeploy.service",
        ROOT / "infra/systemd/discord-bot-dev-autodeploy.service",
    ):
        text = unit.read_text(encoding="utf-8")
        assert "UMask=0022" in text
        assert "/var/lib/discord-bot-deploy/checkouts/" in text
        assert "/home/ubuntu/discord-bot/scripts/auto_deploy.sh" not in text

    prod_deploy = (
        ROOT / "infra/systemd/discord-bot-autodeploy.service"
    ).read_text(encoding="utf-8")
    assert "Environment=AUTO_DEPLOY_PROFILE=core" in prod_deploy
    assert "Environment=AUTO_DEPLOY_CONSUMERS=discord-bot" in prod_deploy
    assert "Environment=AUTO_DEPLOY_READY_PORTS=3002" in prod_deploy


def test_oracle_unit_and_patch_are_loopback_fail_closed():
    unit = (ROOT / "infra/systemd/mcp-oracle-shared.service").read_text(
        encoding="utf-8"
    )
    patch_source = (
        ROOT
        / "infra/supergateway-patches/3.4.3/files/dist/gateways/stdioToStatelessStreamableHttp.js"
    ).read_text(encoding="utf-8")
    launcher = (ROOT / "scripts/oracle_mcp_launch.sh").read_text(encoding="utf-8")

    assert (
        "ExecStart=/var/lib/discord-bot-releases/prod/current/src/scripts/"
        "oracle_supergateway_launch.sh" in unit
    )
    assert "app.listen(port, '127.0.0.1'" in patch_source
    assert 'ENV_FILE="/home/ubuntu/discord-bot/.env"' not in launcher


def test_deploy_scripts_share_pin_ready_and_non_destructive_contracts():
    auto = AUTO_DEPLOY.read_text(encoding="utf-8")
    deploy = (ROOT / "scripts/bot-deploy.sh").read_text(encoding="utf-8")
    rollback = (ROOT / "scripts/bot-rollback.sh").read_text(encoding="utf-8")
    snapshot = (ROOT / "scripts/snapshot_state.sh").read_text(encoding="utf-8")
    release_gate = RELEASE_GATE.read_text(encoding="utf-8")
    release_lib = (ROOT / "scripts/release_artifact_lib.sh").read_text(encoding="utf-8")

    assert "git status --porcelain=v1 --untracked-files=all" in auto
    assert "flock -n 9" in auto
    assert "rollback.pin" in auto
    assert "ready_matches_release" in auto
    assert 'payload.get("release_sha") != expected' in release_lib
    assert "quarantine_release_artifacts" in auto
    assert 'curl -fsS "http://127.0.0.1:${RELEASE_READY_PORTS%% *}/live"' in auto
    assert "git switch --detach" in rollback
    assert "git reset" not in rollback
    assert "git checkout" not in rollback
    assert "rollback.pin" in rollback
    assert "DEPLOY_LOCK_HELD" not in rollback
    assert "EXPECTED_OLD_SHA" in rollback
    assert "/var/lib/discord-bot-deploy" in auto
    assert ".env.journal-forward" not in auto
    assert "DEPLOY_NOTIFY_WEBHOOK_URL" in auto
    assert 'git merge --ff-only "${REMOTE_SHA}"' in auto
    assert "/.venv/bin/pip" not in auto
    assert "release_artifact_init" in auto
    assert "SNAPSHOT_STATE_INVENTORY" in snapshot
    assert "SNAPSHOT_STATE_DIR" not in snapshot
    assert "SNAPSHOT_BOT_DIR" not in snapshot
    assert "SNAPSHOT_INVENTORY_OWNER_UID=0" in release_gate
    assert 'READY_ATTEMPTS="${AUTO_DEPLOY_READY_ATTEMPTS:-36}"' in auto
    assert 'READY_ATTEMPTS="${DEPLOY_READY_ATTEMPTS:-36}"' in deploy
    assert 'READY_ATTEMPTS="${DEPLOY_READY_ATTEMPTS:-36}"' in rollback
    for script in (auto, deploy, rollback):
        assert "log_readiness_diagnostics" in script
        assert "--fail-with-body" in script
        assert "--write-out $'\\n%{http_code}'" in script
        assert "body=${compact:0:4096}" in script


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _install_fake_systemctl(fake_bin: Path, *, provisioned: bool = True) -> Path:
    exit_code = 0 if provisioned else 1
    return _write_executable(
        fake_bin / "systemctl",
        f"#!/usr/bin/env bash\nexit {exit_code}\n",
    )


def test_auto_deploy_unprovisioned_is_failure_unless_explicitly_disabled(
    tmp_path: Path,
):
    repo, _ = _make_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_systemctl(fake_bin, provisioned=False)
    env = {
        **_deploy_env(repo),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DEPLOY_NOTIFY_WEBHOOK_URL": "",
    }

    missing = _run(str(AUTO_DEPLOY), "dev", env=env)
    assert missing.returncode == 4
    assert "is not provisioned" in missing.stdout

    disabled = _run(
        str(AUTO_DEPLOY),
        "dev",
        env={
            **env,
            "AUTO_DEPLOY_DISABLED": "1",
            "AUTO_DEPLOY_BOT_DIR": str(tmp_path / "absent-checkout"),
        },
    )
    assert disabled.returncode == 0
    assert "explicitly disabled" in disabled.stdout


def test_release_generation_publication_is_single_and_non_destructive(tmp_path: Path):
    repo, sha = _make_repo(tmp_path)
    release_root = tmp_path / "release-artifacts"
    command = """
set -euo pipefail
source "$RELEASE_LIB"
cd "$BOT_DIR"
release_artifact_init
if [[ "${FAIL_FSYNC:-0}" == "1" ]]; then
  _fsync_tree() { return 99; }
fi
build_release_venv "$TARGET_SHA"
activate_release "$TARGET_SHA"
"""
    env = {
        "RELEASE_LIB": str(ROOT / "scripts/release_artifact_lib.sh"),
        "BOT_DIR": str(repo),
        "RELEASE_ROOT": str(release_root),
        "RELEASE_BUILD_ROOT": str(tmp_path / "release-build"),
        "DEPLOY_STATE_DIR": str(tmp_path / "deploy-state"),
        "RELEASE_PUBLISHER_USE_SUDO": "0",
        "RELEASE_GENERATION_OWNER_UID": str(os.getuid()),
        "RELEASE_TEST_PUBLISHER": "1",
        "RELEASE_PUBLISHER": str(ROOT / "scripts/release_publisher.sh"),
        "TARGET_SHA": sha,
    }
    built = _run("bash", "-c", command, env=env)
    assert built.returncode == 0, built.stderr

    current = release_root / "current"
    generation = release_root / "generations" / sha
    assert current.is_symlink()
    assert os.readlink(current) == f"generations/{sha}"
    assert (generation / "src/tracked.txt").read_text(encoding="utf-8") == "base\n"
    pip_launcher = (generation / "venv/bin/pip").read_text(encoding="utf-8")
    assert f"{generation}/venv/bin/python" in pip_launcher
    assert ".building." not in pip_launcher
    metadata = json.loads((generation / "release.json").read_text(encoding="utf-8"))
    assert metadata["release_sha"] == metadata["generation"] == sha

    (repo / "tracked.txt").write_text("next\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "next")
    next_sha = _git(repo, "rev-parse", "HEAD")
    interrupted = _run(
        "bash",
        "-c",
        command,
        env={
            **env,
            "FAIL_FSYNC": "1",
            "TARGET_SHA": next_sha,
        },
    )
    assert interrupted.returncode != 0
    assert not (release_root / "generations" / next_sha).exists()
    assert os.readlink(current) == f"generations/{sha}"

    marker = generation / ".release-sha"
    marker.chmod(0o644)
    marker.write_text("corrupt\n", encoding="utf-8")
    rebuilt = _run("bash", "-c", command, env=env)
    assert rebuilt.returncode != 0
    assert marker.read_text(encoding="utf-8") == "corrupt\n"
    assert os.readlink(current) == f"generations/{sha}"


def test_release_generation_requires_external_protected_publisher(tmp_path: Path):
    repo, _ = _make_repo(tmp_path)
    result = _run(
        "bash",
        "-c",
        'source "$RELEASE_LIB"; release_artifact_init',
        env={
            "RELEASE_LIB": str(ROOT / "scripts/release_artifact_lib.sh"),
            "BOT_DIR": str(repo),
            "RELEASE_ROOT": str(tmp_path / "release"),
            "RELEASE_BUILD_ROOT": str(tmp_path / "build"),
            "DEPLOY_STATE_DIR": str(tmp_path / "state"),
            "RELEASE_PUBLISHER": str(tmp_path / "missing-publisher"),
            "RELEASE_TEST_PUBLISHER": "1",
            "RELEASE_PUBLISHER_USE_SUDO": "0",
            "RELEASE_GENERATION_OWNER_UID": str(os.getuid()),
        },
    )
    assert result.returncode != 0
    assert "publisher is not provisioned" in result.stderr


def test_release_publisher_rejects_manifested_content_tampering(tmp_path: Path):
    _, sha = _make_repo(tmp_path)
    fixture_root = tmp_path / "fixture"
    candidate = _immutable_release_fixture(fixture_root, sha)
    build_root = tmp_path / "build"
    build_root.mkdir()
    staging = build_root / f".{sha}.building.fixture"
    candidate.chmod(0o755)
    candidate.rename(staging)
    staging.chmod(0o555)
    tampered = staging / "venv/bin/python"
    tampered.chmod(0o755)
    tampered.write_bytes(tampered.read_bytes() + b"# modified\n")
    tampered.chmod(0o555)
    generations = tmp_path / "published/generations"
    generations.mkdir(parents=True)
    destination = generations / sha

    result = _run(
        str(ROOT / "scripts/release_publisher.sh"),
        "publish",
        str(staging),
        str(destination),
        str(generations),
        str(build_root),
        str(os.getuid()),
        env={"RELEASE_PUBLISHER_TEST_MODE": "1"},
    )
    assert result.returncode != 0
    assert "release content does not match manifest" in result.stderr
    assert not destination.exists()


def test_activation_journal_recovers_after_consumer_kill_point(tmp_path: Path):
    repo, old_sha = _make_repo(tmp_path)
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "new")
    new_sha = _git(repo, "rev-parse", "HEAD")
    release_root = tmp_path / "release"
    _immutable_release_fixture(release_root, old_sha)
    _immutable_release_fixture(release_root, new_sha)
    (release_root / "current").symlink_to(Path("generations") / old_sha)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    restart_log = tmp_path / "restart.log"
    count = tmp_path / "restart.count"
    _write_executable(
        fake_bin / "sudo",
        """#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RESTART_LOG"
value=0
[[ ! -f "$RESTART_COUNT" ]] || value="$(cat "$RESTART_COUNT")"
value=$((value + 1))
printf '%s\n' "$value" >"$RESTART_COUNT"
if [[ "${KILL_ON_SECOND:-0}" == "1" && "$value" -eq 2 ]]; then
  kill -KILL "$PPID"
fi
exit 0
""",
    )
    _write_executable(fake_bin / "systemctl", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "curl",
        "#!/usr/bin/env bash\n"
        'printf \'{"status":"ok","release_sha":"%s"}\\n\' "$READY_SHA"\n',
    )
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RELEASE_LIB": str(ROOT / "scripts/release_artifact_lib.sh"),
        "BOT_DIR": str(repo),
        "DEPLOY_STATE_DIR": str(tmp_path / "state"),
        "RELEASE_ROOT": str(release_root),
        "RELEASE_BUILD_ROOT": str(tmp_path / "state/build"),
        "RELEASE_ACTIVATION_JOURNAL": str(tmp_path / "state/activation.json"),
        "RELEASE_PUBLISHER": str(ROOT / "scripts/release_publisher.sh"),
        "RELEASE_PUBLISHER_USE_SUDO": "0",
        "RELEASE_GENERATION_OWNER_UID": str(os.getuid()),
        "RELEASE_TEST_PUBLISHER": "1",
        "RELEASE_CONSUMERS": "first second third",
        "RELEASE_READY_PORTS": "3004",
        "RELEASE_LIVENESS_URLS": "",
        "RESTART_LOG": str(restart_log),
        "RESTART_COUNT": str(count),
        "READY_SHA": new_sha,
    }
    start = _run(
        "bash",
        "-c",
        'source "$RELEASE_LIB"; cd "$BOT_DIR"; release_artifact_init; '
        'begin_release_transaction deploy "$OLD_SHA" "$NEW_SHA"; '
        'activate_release_transaction "$NEW_SHA"; '
        "restart_release_consumers_transactional",
        env={**env, "OLD_SHA": old_sha, "NEW_SHA": new_sha, "KILL_ON_SECOND": "1"},
    )
    assert start.returncode != 0
    journal = json.loads((tmp_path / "state/activation.json").read_text())
    assert journal["restarted_consumers"] == ["first"]
    assert os.readlink(release_root / "current") == f"generations/{new_sha}"

    resumed = _run(
        "bash",
        "-c",
        'source "$RELEASE_LIB"; cd "$BOT_DIR"; release_artifact_init; '
        "RELEASE_RECOVERY_ATTEMPTS=1; RELEASE_RECOVERY_INTERVAL_SECONDS=0; "
        "resume_release_transaction",
        env={**env, "KILL_ON_SECOND": "0"},
    )
    assert resumed.returncode == 0, resumed.stderr
    assert not (tmp_path / "state/activation.json").exists()
    assert restart_log.read_text(encoding="utf-8").splitlines() == [
        "systemctl restart first",
        "systemctl restart second",
        "systemctl restart second",
        "systemctl restart third",
    ]


def test_auto_deploy_rejects_environment_safety_override(tmp_path: Path):
    repo, _ = _make_repo(tmp_path)
    env = _deploy_env(repo)
    env.update(
        {
            "AUTO_DEPLOY_TEST_MODE": "0",
            "AUTO_DEPLOY_RELEASE_GATE": "/bin/true",
            "AUTO_DEPLOY_CONSUMERS": "discord-bot",
        }
    )
    result = _run(str(AUTO_DEPLOY), "prod", env=env)
    assert result.returncode == 4
    assert "safety allowlist is invalid" in result.stdout


def test_consumer_restart_stops_at_first_failure(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "restart.log"
    _write_executable(
        fake_bin / "sudo",
        """#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RESTART_LOG"
[[ "$*" != *"restart second"* ]]
""",
    )
    result = _run(
        "bash",
        "-c",
        'source "$RELEASE_LIB"; RELEASE_CONSUMERS="first second third"; '
        "restart_release_consumers",
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RELEASE_LIB": str(ROOT / "scripts/release_artifact_lib.sh"),
            "RESTART_LOG": str(log),
        },
    )
    assert result.returncode != 0
    assert log.read_text(encoding="utf-8").splitlines() == [
        "systemctl restart first",
        "systemctl restart second",
    ]


def test_release_consumer_units_share_generation_and_health_ports_are_unique():
    prod_units = [
        ROOT / "infra/systemd/chromadb-server.service",
        ROOT / "infra/systemd/mcp-mem0-shared.service",
        ROOT / "infra/systemd/mcp-second-opinion-shared.service",
        ROOT / "infra/systemd/mcp-oracle-shared.service",
        ROOT / "deploy/systemd/vi-signal-scheduler.service",
        ROOT / "deploy/systemd/intervention-scheduler.service",
        ROOT / "deploy/systemd/hermes-discord-codex.service",
        ROOT / "deploy/systemd/discord-bot.service",
        ROOT / "infra/systemd/kanae-mcp-forward.service",
        ROOT / "infra/systemd/kanae-systemd-forward.service",
    ]
    for unit in prod_units:
        text = unit.read_text(encoding="utf-8")
        assert "/var/lib/discord-bot-releases/prod/current/" in text
        assert "current-venv" not in text

    health_units = {
        ROOT / "deploy/systemd/discord-bot.service": "3002",
        ROOT / "deploy/systemd/discord-bot-dev.service": "3003",
        ROOT / "deploy/systemd/hermes-discord-codex.service": "3004",
    }
    ports: list[str] = []
    for unit, expected in health_units.items():
        text = unit.read_text(encoding="utf-8")
        assert f"Environment=WEB_API_PORT={expected}" in text
        assert "TimeoutStopSec=1980s" in text
        ports.append(expected)
        assert "OAUTH_PRIMARY_CREDENTIALS_PATH=" in text
        assert "OAUTH_FAILOVER_CREDENTIALS_PATH=" in text
        assert "Environment=OAUTH_FAILOVER_ENABLED=true" in text
        assert text.index("EnvironmentFile=") < text.index(
            f"Environment=WEB_API_PORT={expected}"
        )
    assert len(ports) == len(set(ports))


def _install_post_merge_mismatch_git(fake_bin: Path) -> dict[str, str]:
    marker = fake_bin.parent / "post-merge-mismatch"
    _write_executable(
        fake_bin / "git",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "merge" && "${2:-}" == "--ff-only" ]]; then
  "$REAL_GIT" "$@"
  touch "$GIT_MISMATCH_MARKER"
  exit 0
fi
if [[ "${1:-}" == "rev-parse" && "${2:-}" == "HEAD" \
    && -e "$GIT_MISMATCH_MARKER" ]]; then
  rm -f "$GIT_MISMATCH_MARKER"
  printf '%040d\n' 0
  exit 0
fi
exec "$REAL_GIT" "$@"
""",
    )
    return {
        "REAL_GIT": shutil.which("git") or "/usr/bin/git",
        "GIT_MISMATCH_MARKER": str(marker),
    }


def test_rollback_requires_expected_tag_sha_and_writes_external_pin(tmp_path: Path):
    repo, old_sha = _make_repo(tmp_path)
    _git(repo, "tag", "pre-deploy-test", old_sha)
    (repo / "tracked.txt").write_text("blocked\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "blocked")
    blocked_sha = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "rollback-state"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_systemctl(fake_bin)
    _write_executable(fake_bin / "sudo", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "curl",
        "#!/usr/bin/env bash\n"
        'printf \'{"status":"ok","release_sha":"%s"}\\n\' "$READY_SHA"\n',
    )
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DEPLOY_BOT_DIR": str(repo),
        "DEPLOY_STATE_DIR": str(state),
        "DEPLOY_RELEASE_ROOT": str(tmp_path / "release-artifacts"),
        "DEPLOY_READY_ATTEMPTS": "1",
        "DEPLOY_READY_INTERVAL_SECONDS": "0",
        "READY_SHA": old_sha,
        **_test_publisher_env("DEPLOY_"),
    }

    mismatch = _run(
        str(ROOT / "scripts/bot-rollback.sh"),
        "pre-deploy-test",
        blocked_sha,
        blocked_sha,
        env=env,
    )
    assert mismatch.returncode == 2
    assert "expected" in mismatch.stderr
    assert _git(repo, "rev-parse", "HEAD") == blocked_sha

    result = _run(
        str(ROOT / "scripts/bot-rollback.sh"),
        "pre-deploy-test",
        old_sha,
        blocked_sha,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert _git(repo, "rev-parse", "HEAD") == old_sha
    pin = (state / "rollback.pin").read_text(encoding="utf-8")
    assert f"target_sha={old_sha}" in pin
    assert f"blocked_sha={blocked_sha}" in pin
    repeated = _run(
        str(ROOT / "scripts/bot-rollback.sh"),
        "pre-deploy-test",
        old_sha,
        blocked_sha,
        env=env,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert _git(repo, "rev-parse", "HEAD") == old_sha


def test_rollback_logs_degraded_ready_response_body(tmp_path: Path):
    repo, old_sha = _make_repo(tmp_path)
    _git(repo, "tag", "pre-deploy-test", old_sha)
    (repo / "tracked.txt").write_text("blocked\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "blocked")
    blocked_sha = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "rollback-state"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_systemctl(fake_bin)
    _write_executable(fake_bin / "sudo", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
body='{"status":"degraded","release_sha":null,"subsystems":{"release":{"ok":false,"reason":"release_verification_pending"}}}'
printf '%s\n503' "$body"
exit 22
""",
    )

    result = _run(
        str(ROOT / "scripts/bot-rollback.sh"),
        "pre-deploy-test",
        old_sha,
        blocked_sha,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DEPLOY_BOT_DIR": str(repo),
            "DEPLOY_STATE_DIR": str(state),
            "DEPLOY_RELEASE_ROOT": str(tmp_path / "release-artifacts"),
            "DEPLOY_READY_ATTEMPTS": "1",
            "DEPLOY_READY_INTERVAL_SECONDS": "0",
            **_test_publisher_env("DEPLOY_"),
        },
    )

    assert result.returncode == 3, result.stderr
    assert "readiness diagnostic" in result.stderr
    assert "curl_rc=22" in result.stderr
    assert "http=503" in result.stderr
    assert "release_verification_pending" in result.stderr


def test_rollback_lock_cannot_be_bypassed_by_environment(tmp_path: Path):
    repo, old_sha = _make_repo(tmp_path)
    _git(repo, "tag", "pre-deploy-test", old_sha)
    (repo / "tracked.txt").write_text("blocked\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "blocked")
    blocked_sha = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "rollback-state"
    state.mkdir()
    lock = state / "deploy.lock"

    with lock.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run(
            str(ROOT / "scripts/bot-rollback.sh"),
            "pre-deploy-test",
            old_sha,
            blocked_sha,
            env={
                "DEPLOY_BOT_DIR": str(repo),
                "DEPLOY_STATE_DIR": str(state),
                "DEPLOY_RELEASE_ROOT": str(tmp_path / "release-artifacts"),
                "DEPLOY_LOCK_HELD": "1",
            },
        )

    assert result.returncode == 4
    assert "another deploy or rollback is running" in result.stderr
    assert _git(repo, "rev-parse", "HEAD") == blocked_sha


def test_manual_deploy_readiness_failure_automatically_rolls_back(tmp_path: Path):
    seed_root = tmp_path / "seed-root"
    seed_root.mkdir()
    seed, old_sha = _make_repo(seed_root)
    _git(seed, "branch", "-M", "master")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "master")
    deploy_repo = tmp_path / "deploy-repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(deploy_repo)], check=True)
    (seed / "tracked.txt").write_text("target\n", encoding="utf-8")
    _git(seed, "commit", "-qam", "target")
    new_sha = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", "origin", "master")

    fake_gate = _write_executable(
        tmp_path / "release-gate", "#!/usr/bin/env bash\nexit 0\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_systemctl(fake_bin)
    restart_log = tmp_path / "restart.log"
    _write_executable(
        fake_bin / "sudo",
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"$RESTART_LOG"\n',
    )
    curl_count = tmp_path / "curl.count"
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
if [[ ! -e "$CURL_COUNT" ]]; then
  printf '1' >"$CURL_COUNT"
  exit 22
fi
printf '{"status":"ok","release_sha":"%s"}\n' "$READY_SHA"
""",
    )
    runner = ROOT / "scripts/bot-rollback.sh"
    runner_digest = hashlib.sha256(runner.read_bytes()).hexdigest()
    state = tmp_path / "deploy-state"

    result = _run(
        str(ROOT / "scripts/bot-deploy.sh"),
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DEPLOY_BOT_DIR": str(deploy_repo),
            "DEPLOY_STATE_DIR": str(state),
            "DEPLOY_RELEASE_ROOT": str(tmp_path / "release-artifacts"),
            "DEPLOY_RELEASE_GATE": str(fake_gate),
            "DEPLOY_TRUSTED_ROLLBACK_RUNNER": str(runner),
            "DEPLOY_TRUSTED_ROLLBACK_RUNNER_SHA256": runner_digest,
            "DEPLOY_READY_ATTEMPTS": "1",
            "DEPLOY_READY_INTERVAL_SECONDS": "0",
            "RESTART_LOG": str(restart_log),
            "CURL_COUNT": str(curl_count),
            "READY_SHA": old_sha,
            **_test_publisher_env("DEPLOY_"),
        },
    )

    assert result.returncode == 3, result.stderr
    assert _git(deploy_repo, "rev-parse", "HEAD") == old_sha
    assert restart_log.read_text(encoding="utf-8").count("restart") == 20
    pin = (state / "rollback.pin").read_text(encoding="utf-8")
    assert f"target_sha={old_sha}" in pin
    assert f"blocked_sha={new_sha}" in pin


def test_manual_deploy_quarantines_post_merge_sha_mismatch_before_restart(
    tmp_path: Path,
):
    seed_root = tmp_path / "seed-root"
    seed_root.mkdir()
    seed, old_sha = _make_repo(seed_root)
    _git(seed, "branch", "-M", "master")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "master")
    deploy_repo = tmp_path / "deploy-repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(deploy_repo)], check=True)
    (seed / "tracked.txt").write_text("target\n", encoding="utf-8")
    (seed / "requirements.lock").write_text("# changed\n", encoding="utf-8")
    _git(seed, "add", "requirements.lock")
    _git(seed, "commit", "-qam", "target with dependencies")
    _git(seed, "push", "-q", "origin", "master")

    fake_gate = _write_executable(
        tmp_path / "release-gate", "#!/usr/bin/env bash\nexit 0\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_systemctl(fake_bin)
    mismatch_env = _install_post_merge_mismatch_git(fake_bin)
    restart_marker = tmp_path / "restart-ran"
    _write_executable(
        fake_bin / "sudo",
        '#!/usr/bin/env bash\nprintf restarted >"$RESTART_MARKER"\n',
    )
    _write_executable(
        fake_bin / "curl",
        "#!/usr/bin/env bash\n"
        'printf \'{"status":"ok","release_sha":"%s"}\\n\' "$READY_SHA"\n',
    )
    runner = ROOT / "scripts/bot-rollback.sh"
    state = tmp_path / "deploy-state"

    result = _run(
        str(ROOT / "scripts/bot-deploy.sh"),
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DEPLOY_BOT_DIR": str(deploy_repo),
            "DEPLOY_STATE_DIR": str(state),
            "DEPLOY_RELEASE_ROOT": str(tmp_path / "release-artifacts"),
            "DEPLOY_RELEASE_GATE": str(fake_gate),
            "DEPLOY_TRUSTED_ROLLBACK_RUNNER": str(runner),
            "DEPLOY_TRUSTED_ROLLBACK_RUNNER_SHA256": hashlib.sha256(
                runner.read_bytes()
            ).hexdigest(),
            "RESTART_MARKER": str(restart_marker),
            "READY_SHA": old_sha,
            "DEPLOY_READY_ATTEMPTS": "1",
            "DEPLOY_READY_INTERVAL_SECONDS": "0",
            **_test_publisher_env("DEPLOY_"),
            **mismatch_env,
        },
    )

    assert result.returncode == 5, result.stderr
    assert "merged SHA does not match gated fetched target" in result.stderr
    assert not restart_marker.exists()
    assert _git(deploy_repo, "status", "--porcelain") == ""
    assert _git(deploy_repo, "rev-parse", "HEAD") == old_sha
    assert (state / "rollback.pin").is_file()
    quarantine = list((state / "quarantine").glob("*/observed-sha.txt"))
    assert quarantine
    assert quarantine[0].read_text(encoding="utf-8").strip() == "0" * 40
    release_root = tmp_path / "release-artifacts"
    current = release_root / "current"
    metadata_path = current / "release.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["release_sha"] == old_sha
    assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o444
    assert current.is_symlink()
    assert os.readlink(current) == f"generations/{old_sha}"
    assert (current / "src/tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert not (deploy_repo / ".venv").exists()


def test_auto_deploy_quarantines_post_merge_sha_mismatch_before_restart(
    tmp_path: Path,
):
    seed_root = tmp_path / "seed-root"
    seed_root.mkdir()
    seed, old_sha = _make_repo(seed_root)
    _git(seed, "branch", "-M", "master")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "master")
    deploy_repo = tmp_path / "deploy-repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(deploy_repo)], check=True)

    (seed / "tracked.txt").write_text("target\n", encoding="utf-8")
    (seed / "requirements.lock").write_text("# changed\n", encoding="utf-8")
    _git(seed, "add", "requirements.lock")
    _git(seed, "commit", "-qam", "target with dependencies")
    new_sha = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", "origin", "master")

    fake_gate = _write_executable(
        tmp_path / "release-gate", "#!/usr/bin/env bash\nexit 0\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_systemctl(fake_bin)
    mismatch_env = _install_post_merge_mismatch_git(fake_bin)
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        'printf \'[{"headSha":"%s","conclusion":"success",'
        '"status":"completed"}]\\n\' "$TARGET_SHA"\n',
    )
    restart_marker = tmp_path / "restart-ran"
    _write_executable(
        fake_bin / "sudo",
        '#!/usr/bin/env bash\nprintf restarted >"$RESTART_MARKER"\n',
    )
    _write_executable(
        fake_bin / "curl",
        "#!/usr/bin/env bash\n"
        'printf \'{"status":"ok","release_sha":"%s"}\\n\' "$READY_SHA"\n',
    )
    runner = ROOT / "scripts/bot-rollback.sh"
    state = tmp_path / "deploy-state"

    result = _run(
        str(AUTO_DEPLOY),
        "prod",
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AUTO_DEPLOY_BOT_DIR": str(deploy_repo),
            "AUTO_DEPLOY_STATE_DIR": str(state),
            "AUTO_DEPLOY_RELEASE_ROOT": str(tmp_path / "release-artifacts"),
            "AUTO_DEPLOY_RELEASE_GATE": str(fake_gate),
            "AUTO_DEPLOY_TRUSTED_ROLLBACK_RUNNER": str(runner),
            "AUTO_DEPLOY_TRUSTED_ROLLBACK_RUNNER_SHA256": hashlib.sha256(
                runner.read_bytes()
            ).hexdigest(),
            "AUTO_DEPLOY_READY_ATTEMPTS": "1",
            "AUTO_DEPLOY_READY_INTERVAL_SECONDS": "0",
            "DEPLOY_NOTIFY_WEBHOOK_URL": "",
            "RESTART_MARKER": str(restart_marker),
            "READY_SHA": old_sha,
            "TARGET_SHA": new_sha,
            "AUTO_DEPLOY_TEST_MODE": "1",
            **_test_publisher_env("AUTO_DEPLOY_"),
            **mismatch_env,
        },
    )

    assert result.returncode == 5, result.stderr
    assert _git(deploy_repo, "rev-parse", "HEAD") == old_sha
    assert _git(deploy_repo, "status", "--porcelain") == ""
    assert not restart_marker.exists()
    pin = (state / "rollback.pin").read_text(encoding="utf-8")
    assert f"target_sha={old_sha}" in pin
    assert f"blocked_sha={new_sha}" in pin
    observed = list((state / "quarantine").glob("*/observed-sha.txt"))
    assert observed
    assert observed[0].read_text(encoding="utf-8").strip() == "0" * 40


def test_snapshot_requires_stable_generation_and_detects_tampering(tmp_path: Path):
    if shutil.which("rsync") is None or shutil.which("sqlite3") is None:
        pytest.skip("snapshot utilities are not installed")
    state = tmp_path / "live-bot-state"
    state.mkdir()
    (state / "runtime_state.json").write_text('{"generation":1}\n', encoding="utf-8")
    (state / "plain.txt").write_text("stable\n", encoding="utf-8")
    connection = sqlite3.connect(state / "state.db")
    connection.execute("CREATE TABLE state (value TEXT)")
    connection.execute("INSERT INTO state VALUES ('stable')")
    connection.commit()
    connection.close()
    snapshots = tmp_path / "snapshots"
    other_state = tmp_path / "live-scheduler-state"
    other_state.mkdir()
    (other_state / "scheduler.json").write_text('{"ok":true}\n', encoding="utf-8")
    inventory = _snapshot_inventory(tmp_path, bot=state, scheduler=other_state)

    created = _run(
        str(ROOT / "scripts/snapshot_state.sh"),
        env={
            "SNAPSHOT_STATE_INVENTORY": str(inventory),
            "SNAPSHOT_INVENTORY_OWNER_UID": str(os.getuid()),
            "SNAPSHOT_REQUIRE_READ_ONLY": "0",
            "SNAPSHOT_ROOT": str(snapshots),
            "SNAPSHOT_R2_SYNC": "0",
        },
    )
    assert created.returncode == 0, created.stderr
    snapshot_line = next(
        line
        for line in created.stdout.splitlines()
        if line.startswith("[snapshot] done: ")
    )
    snapshot = Path(snapshot_line.removeprefix("[snapshot] done: "))
    assert (snapshot / "SNAPSHOT_GENERATION").is_file()
    assert (snapshot / "SNAPSHOT_FILES.sha256").is_file()

    verified = _run(str(ROOT / "scripts/snapshot_state.sh"), "--verify", str(snapshot))
    assert verified.returncode == 0, verified.stderr
    assert (snapshot / "services/scheduler/scheduler.json").is_file()
    (snapshot / "services/bot/plain.txt").write_text("tampered\n", encoding="utf-8")
    tampered = _run(str(ROOT / "scripts/snapshot_state.sh"), "--verify", str(snapshot))
    assert tampered.returncode == 1
    assert "manifest mismatch" in tampered.stderr


def test_snapshot_discards_generation_changed_during_copy(tmp_path: Path):
    real_rsync = shutil.which("rsync")
    if real_rsync is None or shutil.which("sqlite3") is None:
        pytest.skip("snapshot utilities are not installed")
    state = tmp_path / "live-state"
    state.mkdir()
    mutable = state / "mutable.txt"
    mutable.write_text("before\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "rsync",
        f'#!/usr/bin/env bash\n"{real_rsync}" "$@"\nprintf changed >>"$SNAPSHOT_MUTATE_FILE"\n',
    )
    inventory = _snapshot_inventory(tmp_path, bot=state)

    result = _run(
        str(ROOT / "scripts/snapshot_state.sh"),
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SNAPSHOT_STATE_INVENTORY": str(inventory),
            "SNAPSHOT_INVENTORY_OWNER_UID": str(os.getuid()),
            "SNAPSHOT_REQUIRE_READ_ONLY": "0",
            "SNAPSHOT_ROOT": str(tmp_path / "snapshots"),
            "SNAPSHOT_R2_SYNC": "0",
            "SNAPSHOT_STABILITY_ATTEMPTS": "1",
            "SNAPSHOT_MUTATE_FILE": str(mutable),
        },
    )

    assert result.returncode == 1
    assert "live generation changed" in result.stderr
    assert "did not remain stable" in result.stderr


def test_snapshot_accepts_wal_growth_and_validates_logical_backup(tmp_path: Path):
    real_rsync = shutil.which("rsync")
    if real_rsync is None or shutil.which("sqlite3") is None:
        pytest.skip("snapshot utilities are not installed")
    state = tmp_path / "live-state"
    state.mkdir()
    database = state / "state.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE state (value TEXT)")
    connection.execute("INSERT INTO state VALUES ('before')")
    connection.commit()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "rsync",
        f"""#!/usr/bin/env bash
set -e
"{real_rsync}" "$@"
python3 - "$SNAPSHOT_SQLITE" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("INSERT INTO state VALUES ('during-copy')")
PY
""",
    )
    inventory = _snapshot_inventory(tmp_path, bot=state)
    result = _run(
        str(ROOT / "scripts/snapshot_state.sh"),
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SNAPSHOT_STATE_INVENTORY": str(inventory),
            "SNAPSHOT_INVENTORY_OWNER_UID": str(os.getuid()),
            "SNAPSHOT_REQUIRE_READ_ONLY": "0",
            "SNAPSHOT_ROOT": str(tmp_path / "snapshots"),
            "SNAPSHOT_R2_SYNC": "0",
            "SNAPSHOT_STABILITY_ATTEMPTS": "1",
            "SNAPSHOT_SQLITE": str(database),
        },
    )
    connection.close()

    assert result.returncode == 0, result.stderr
    snapshot_line = next(
        line
        for line in result.stdout.splitlines()
        if line.startswith("[snapshot] done: ")
    )
    snapshot = Path(snapshot_line.removeprefix("[snapshot] done: "))
    backup = sqlite3.connect(snapshot / "services/bot/state.db")
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("SELECT count(*) FROM state").fetchone() == (2,)
    finally:
        backup.close()


def test_snapshot_requires_explicit_inventory_and_read_only_identity(tmp_path: Path):
    missing = _run(
        str(ROOT / "scripts/snapshot_state.sh"),
        env={"SNAPSHOT_ROOT": str(tmp_path / "snapshots")},
    )
    assert missing.returncode == 2
    assert "SNAPSHOT_STATE_INVENTORY" in missing.stderr

    state = tmp_path / "writable-state"
    state.mkdir()
    (state / "state.json").write_text("{}\n", encoding="utf-8")
    inventory = _snapshot_inventory(tmp_path, bot=state)
    writable = _run(
        str(ROOT / "scripts/snapshot_state.sh"),
        env={
            "SNAPSHOT_STATE_INVENTORY": str(inventory),
            "SNAPSHOT_INVENTORY_OWNER_UID": str(os.getuid()),
            "SNAPSHOT_ROOT": str(tmp_path / "snapshots"),
            "SNAPSHOT_R2_SYNC": "0",
        },
    )
    assert writable.returncode == 2
    assert "write access to live root" in writable.stderr


def test_snapshot_rejects_sqlite_file_set_change_during_backup(tmp_path: Path):
    sqlite_binary = shutil.which("sqlite3")
    if shutil.which("rsync") is None or sqlite_binary is None:
        pytest.skip("snapshot utilities are not installed")
    state = tmp_path / "live-state"
    state.mkdir()
    with sqlite3.connect(state / "state.db") as connection:
        connection.execute("CREATE TABLE state (value TEXT)")
        connection.execute("INSERT INTO state VALUES ('stable')")
    inventory = _snapshot_inventory(tmp_path, bot=state)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "sqlite3",
        """#!/usr/bin/env bash
"$REAL_SQLITE" "$@"
rc=$?
if [[ "$*" == *".backup"* && ! -e "$INJECTION_MARKER" ]]; then
  : >"$INJECTION_MARKER"
  "$REAL_SQLITE" "$LIVE_STATE/late.db" 'CREATE TABLE late(value TEXT);'
fi
exit "$rc"
""",
    )
    result = _run(
        str(ROOT / "scripts/snapshot_state.sh"),
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REAL_SQLITE": sqlite_binary,
            "LIVE_STATE": str(state),
            "INJECTION_MARKER": str(tmp_path / "injected"),
            "SNAPSHOT_STATE_INVENTORY": str(inventory),
            "SNAPSHOT_INVENTORY_OWNER_UID": str(os.getuid()),
            "SNAPSHOT_REQUIRE_READ_ONLY": "0",
            "SNAPSHOT_ROOT": str(tmp_path / "snapshots"),
            "SNAPSHOT_R2_SYNC": "0",
            "SNAPSHOT_STABILITY_ATTEMPTS": "1",
        },
    )
    assert result.returncode == 1
    assert "live generation changed" in result.stderr
    assert not any(
        path.is_dir() and not path.name.startswith(".")
        for path in (tmp_path / "snapshots").iterdir()
    )


@pytest.mark.parametrize(
    "script",
    [
        "scripts/auto_deploy.sh",
        "scripts/bot-deploy.sh",
        "scripts/bot-rollback.sh",
        "scripts/release_gate.sh",
        "scripts/oracle_mcp_launch.sh",
        "scripts/oracle_supergateway_launch.sh",
        "scripts/smoke_bot.sh",
        "scripts/snapshot_state.sh",
        "scripts/release_artifact_lib.sh",
        "scripts/release_publisher.sh",
    ],
)
def test_operational_shell_syntax(script: str):
    result = _run("bash", "-n", str(ROOT / script))
    assert result.returncode == 0, result.stderr


def test_runtime_smoke_uses_only_release_dependencies_and_preserves_failures():
    smoke = (ROOT / "scripts/smoke_bot.sh").read_text(encoding="utf-8")
    assert '${BOT_VENV}/bin/ruff' not in smoke
    assert '${BOT_VENV}/bin/mypy' not in smoke
    assert "-p no:cacheprovider" in smoke
    assert "| tail" not in smoke
