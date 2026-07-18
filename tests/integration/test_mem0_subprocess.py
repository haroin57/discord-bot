from __future__ import annotations

import os
import subprocess
import sys


def test_alpha_and_canonical_modes_fail_closed_together():
    environment = {
        **dict(os.environ),
        "MEM0_ALPHA_ENABLED": "1",
        "MEM0_INTEGRITY_MODE": "canonical",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    result = subprocess.run(
        [sys.executable, "-c", "import services.mem0"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "cannot be combined" in result.stderr
