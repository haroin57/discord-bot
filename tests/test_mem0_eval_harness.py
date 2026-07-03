"""T8b 評価ハーネス (scripts/eval_mem0_memory.py) を pytest から回す回帰ゲート。

シナリオが1件でも落ちたら CI で気づく（段階 ON 判断の前提となる評価セット）。
"""

import asyncio
import importlib.util
import os
import sys

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "eval_mem0_memory.py",
)


@pytest.fixture()
def eval_mod():
    spec = importlib.util.spec_from_file_location("mem0_eval_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mem0_eval_test"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("mem0_eval_test", None)


def test_all_scenarios_pass_offline(eval_mod, capsys):
    rc = asyncio.run(eval_mod.main(live=False))
    out = capsys.readouterr().out
    assert rc == 0, f"eval scenarios failed:\n{out}"
    assert "TOTAL" in out
