"""scripts/mem0_backfill_attributes.py の T6 硬化テスト（dry-run / shadow / apply）。

Chroma / Haiku には触らない（_get_mem0 / get_all_memories / 推論を全てモック）。
"""

import importlib.util
import json
import os
import sys

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "mem0_backfill_attributes.py",
)


@pytest.fixture()
def backfill_mod():
    spec = importlib.util.spec_from_file_location("mem0_backfill_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mem0_backfill_test"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("mem0_backfill_test", None)


class _FakeMem:
    def __init__(self):
        self.updates = []

    def update(self, memory_id=None, metadata=None, **kw):
        self.updates.append({"id": memory_id, "metadata": metadata})


_FACTS = [
    {"id": "f1", "memory": "体重は60kg", "metadata": {}},
    {"id": "f2", "memory": "沙耶の唄が好き", "metadata": {}},
    {"id": "f3", "memory": "既にattr付き", "metadata": {"attribute": "weight"}},
]

# f1 → high/weight, f2 → low/custom:xxx を返す推論スタブ
_INFER = {
    "体重は60kg": ("weight", "high"),
    "沙耶の唄が好き": ("custom:likes", "low"),
}


@pytest.fixture()
def wired(backfill_mod, monkeypatch):
    mem = _FakeMem()
    monkeypatch.setattr(
        "services.mem0.client._get_mem0", lambda: mem)

    async def fake_get_all(user_id, limit=None):
        return [dict(f) for f in _FACTS]
    monkeypatch.setattr(
        "services.mem0.client.get_all_memories", fake_get_all)

    async def fake_infer(text):
        return _INFER.get(text, ("", "low"))
    monkeypatch.setattr(backfill_mod, "infer_attribute_for_fact", fake_infer)
    return backfill_mod, mem


class TestBackfillModes:
    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self, wired):
        mod, mem = wired
        await mod.main("u1", apply=False, limit=None, batch_sleep=0)
        assert mem.updates == []

    @pytest.mark.asyncio
    async def test_shadow_writes_jsonl_only(self, wired, tmp_path):
        mod, mem = wired
        out = tmp_path / "shadow.jsonl"
        await mod.main("u1", apply=False, limit=None, batch_sleep=0,
                       shadow_path=str(out))
        assert mem.updates == []  # metadata は無変更
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        # attribute が推論できた 2 件（f3 は既 attr 付きで対象外）
        assert {r["id"] for r in rows} == {"f1", "f2"}
        by_id = {r["id"]: r for r in rows}
        assert by_id["f1"]["confidence"] == "high"
        assert by_id["f2"]["confidence"] == "low"

    @pytest.mark.asyncio
    async def test_apply_skips_low_confidence(self, wired):
        mod, mem = wired
        await mod.main("u1", apply=True, limit=None, batch_sleep=0)
        # high の f1 だけ書かれ、low の f2 は skip
        assert [u["id"] for u in mem.updates] == ["f1"]
        meta = mem.updates[0]["metadata"]
        assert meta["attribute"] == "weight"
        assert meta["schema_version"] == mod.BACKFILL_SCHEMA_VERSION
        assert meta["backfill_confidence"] == "high"
