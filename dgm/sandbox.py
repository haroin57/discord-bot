"""Sandboxed code execution for DGM agent evaluation."""

import ast
import asyncio
import logging
import os
import sys
import tempfile
import textwrap

from config import DGM_SANDBOX_MEMORY_MB, DGM_SANDBOX_TIMEOUT_S
from dgm.models import SandboxResult

log = logging.getLogger(__name__)

ALLOWED_MODULES = frozenset({
    "math", "collections", "itertools", "re", "string",
    "functools", "operator", "json", "typing", "datetime",
    "decimal", "fractions", "heapq", "bisect", "copy",
    "textwrap", "unicodedata", "hashlib",
})

BANNED_MODULES = frozenset({
    "os", "sys", "subprocess", "shutil", "socket", "http",
    "urllib", "requests", "pathlib", "io", "builtins",
    "importlib", "ctypes", "signal", "threading", "multiprocessing",
    "pickle", "shelve", "tempfile", "glob", "code", "compile",
})


def check_imports(code: str) -> str | None:
    """Check code for forbidden imports. Returns error message or None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"SyntaxError: {e}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.split(".")[0]
                if module in BANNED_MODULES:
                    return f"Forbidden import: {module}"
                if module not in ALLOWED_MODULES:
                    return f"Unknown module: {module}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module = node.module.split(".")[0]
                if module in BANNED_MODULES:
                    return f"Forbidden import: {module}"
                if module not in ALLOWED_MODULES:
                    return f"Unknown module: {module}"
    return None


async def run_sandboxed(agent_code: str, test_code: str) -> SandboxResult:
    """Run agent code + test code in a sandboxed subprocess."""
    import_err = check_imports(agent_code)
    if import_err:
        return SandboxResult(error=import_err)

    script = textwrap.dedent(f'''\
import resource
import sys

# Set memory limit
memory_bytes = {DGM_SANDBOX_MEMORY_MB} * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))

# Set CPU time limit
resource.setrlimit(resource.RLIMIT_CPU, ({DGM_SANDBOX_TIMEOUT_S}, {DGM_SANDBOX_TIMEOUT_S}))

# Agent code
{agent_code}

# Test code
{test_code}
''')

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script)
        script_path = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "")},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=DGM_SANDBOX_TIMEOUT_S + 5
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SandboxResult(timed_out=True, error="Execution timed out")

        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")

        passed = 0
        total = 0
        for line in stdout_str.strip().splitlines():
            if line.startswith("PASSED "):
                parts = line.split()
                if len(parts) == 2 and "/" in parts[1]:
                    p, t = parts[1].split("/")
                    passed, total = int(p), int(t)

        if proc.returncode != 0 and total == 0:
            return SandboxResult(
                stdout=stdout_str[-500:], stderr=stderr_str[-500:],
                error=f"Exit code {proc.returncode}",
            )

        pass_rate = passed / total if total > 0 else 0.0
        return SandboxResult(
            passed=passed, total=total, pass_rate=pass_rate,
            stdout=stdout_str[-500:], stderr=stderr_str[-500:],
        )
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass
