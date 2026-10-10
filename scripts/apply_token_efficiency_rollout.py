"""Apply or roll back the one validated Kanae query-sharing generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

UNIT = "discord-bot-kanae-claude.service"
GENERATION = Path("/var/lib/kanae-claude-performance/20261010-token-efficiency1")
DROPIN = Path("/etc/systemd/system/discord-bot-kanae-claude.service.d/90-token-efficiency.conf")
TARGET = "/var/lib/kanae-claude-quality/20260909-botquality1/alpha/llm_mem0/search.py"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def systemctl(*args):
    return subprocess.check_output(["systemctl", *args], text=True).strip()


def atomic_config(content):
    with tempfile.NamedTemporaryFile(dir=DROPIN.parent, prefix=".token-efficiency-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o644)
    os.replace(temporary, DROPIN)


def recent_activity():
    journal = subprocess.check_output([
        "journalctl", "-u", UNIT, "--since", "5 minutes ago", "-o", "json", "--no-pager",
    ], text=True)
    for line in journal.splitlines():
        record = json.loads(line)
        message = record.get("MESSAGE", "")
        try:
            inner = json.loads(message)
            message = inner.get("msg", "") if isinstance(inner, dict) else ""
        except (ValueError, TypeError):
            pass
        if any(marker in message for marker in (
            "claude_api_usage ", "api_payload:", "llm_helper_usage ", "agent_loop", "agent-loop",
        )):
            return True
    return False


def health():
    try:
        response = urllib.request.urlopen("http://127.0.0.1:3004/health", timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("activate", "rollback"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    manifest = json.loads((GENERATION / "manifest.json").read_text())
    if manifest.get("generation") != str(GENERATION) or manifest.get("unit") != UNIT:
        raise ValueError("Unexpected generation or service")
    if not manifest.get("deploy_ready"):
        raise ValueError("Live evidence has not passed the adoption gate")
    files = {
        "alpha/llm_mem0/search.py": "payload_sha256",
        "services/query_expansion.py": "dependency_sha256",
        "90-token-efficiency.conf": "rollout_sha256",
        "rollback.conf": "rollback_sha256",
    }
    for relative, field in files.items():
        path = GENERATION / relative
        mode = path.stat()
        if mode.st_uid != 0 or mode.st_mode & 0o022 or digest(path) != manifest[field]:
            raise ValueError(f"Untrusted or changed generation file: {relative}")
    mounts = systemctl("show", UNIT, "--property=BindReadOnlyPaths", "--value").split()
    if sorted(mounts) not in (sorted(manifest["original_mounts"]), sorted(manifest["new_mounts"])):
        raise ValueError("Another service overlay changed; refusing to overwrite it")
    if DROPIN.exists() and digest(DROPIN) not in (manifest["rollout_sha256"], manifest["rollback_sha256"]):
        raise ValueError("Another change owns the destination drop-in")
    if args.dry_run:
        print(json.dumps({"action": args.action, "unit": UNIT, "dropin": str(DROPIN),
                          "generation_verified": True, "unrelated_mounts_preserved": True}))
        return
    if os.geteuid() != 0:
        raise PermissionError("Run the reviewed deployment script with sudo")
    if recent_activity():
        raise RuntimeError("Recent model activity exists; wait for an idle window")
    before = health()
    if not all(before.get("subsystems", {}).get(name, {}).get("ok") for name in ("discord", "oauth", "mem0", "system")):
        raise RuntimeError("A required production subsystem is unhealthy")
    source = "90-token-efficiency.conf" if args.action == "activate" else "rollback.conf"
    content = (GENERATION / source).read_bytes()
    previous = DROPIN.read_bytes() if DROPIN.exists() else (GENERATION / "rollback.conf").read_bytes()
    atomic_config(content)
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "restart", UNIT], check=True)
        expected = manifest["payload_sha256"] if args.action == "activate" else manifest["baseline_sha256"]
        for _ in range(45):
            pid = systemctl("show", UNIT, "--property=MainPID", "--value")
            if pid != "0":
                try:
                    after = health()
                    if all(after.get("subsystems", {}).get(name, {}).get("ok") for name in ("discord", "oauth", "mem0", "system")):
                        break
                except (OSError, ValueError):
                    pass
            time.sleep(1)
        else:
            raise RuntimeError("Required production subsystems did not recover")
        if digest(Path(f"/proc/{pid}/root{TARGET}")) != expected:
            raise RuntimeError("The running mount namespace uses an unexpected search source")
        if args.action == "activate" and digest(Path(f"/proc/{pid}/root/home/ubuntu/discord-bot/services/query_expansion.py")) != manifest["dependency_sha256"]:
            raise RuntimeError("The running dependency did not match the verified payload")
        selected = dict(item.split(b"=", 1) for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if b"=" in item)
        expected_flag = b"1" if args.action == "activate" else b"0"
        if selected.get(b"MEM0_QUERY_SINGLEFLIGHT_ENABLED") != expected_flag:
            raise RuntimeError("The running feature flag did not match the action")
    except BaseException:
        atomic_config(previous)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "restart", UNIT], check=True)
        raise
    record = {"action": args.action, "unit": UNIT, "pid": int(pid), "search_sha256": expected,
              "singleflight_enabled": args.action == "activate", "required_subsystems_ok": True,
              "release_reason": after.get("subsystems", {}).get("release", {}).get("reason"),
              "completed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (GENERATION / f"{args.action}-verification.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
