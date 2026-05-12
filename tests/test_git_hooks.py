import os
import subprocess
from pathlib import Path

def test_pre_commit_hook_exists_and_executable():
    p = Path(__file__).parent.parent / ".git-hooks" / "pre-commit"
    assert p.exists()
    assert os.access(p, os.X_OK)

def test_pre_commit_syntax_valid():
    p = Path(__file__).parent.parent / ".git-hooks" / "pre-commit"
    result = subprocess.run(["bash", "-n", str(p)], capture_output=True)
    assert result.returncode == 0

def test_install_hooks_script_runnable():
    p = Path(__file__).parent.parent / "scripts" / "install-hooks.sh"
    result = subprocess.run(["bash", "-n", str(p)], capture_output=True)
    assert result.returncode == 0
