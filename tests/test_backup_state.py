import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "backup_state.sh"
CRON_FILE = REPO_ROOT / "deploy" / "cron" / "backup-state.cron"


def test_backup_script_syntactically_valid():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_backup_script_is_executable():
    assert SCRIPT.exists(), f"Script not found: {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"Script is not executable: {SCRIPT}"


def test_cron_entry_format():
    assert CRON_FILE.exists(), f"Cron file not found: {CRON_FILE}"
    content = CRON_FILE.read_text()
    # Find a non-comment, non-empty line that looks like a cron entry
    cron_line_pattern = re.compile(
        r"^\s*(\S+\s+){4}\S+\s+\S+"  # 5 fields + at least one path token
    )
    cron_lines = [
        line for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert cron_lines, "No cron entries found in file"
    for line in cron_lines:
        assert cron_line_pattern.match(line), (
            f"Line does not look like a valid cron entry (need 5 time fields + command):\n  {line!r}"
        )
