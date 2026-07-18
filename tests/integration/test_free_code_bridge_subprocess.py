from __future__ import annotations

from services.free_code_bridge import BotCommandRuntime


async def test_runtime_runs_shell_in_working_dir(tmp_path):
    (tmp_path / "marker.txt").write_text("hello", encoding="utf-8")
    runtime = BotCommandRuntime(working_dir=str(tmp_path))

    result = await runtime.run_shell("ls")

    assert "marker.txt" in result.stdout
