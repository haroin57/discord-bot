"""services/mem0/fact_store.py (v5 T8a) のテスト。tmp SQLite で完結。"""

import pytest

from services.mem0 import fact_store as fs


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    fs._reset_for_tests(str(tmp_path / "facts.sqlite"))
    yield
    fs._reset_for_tests(None)


class TestRecordAndQuery:
    def test_record_and_get_active(self):
        assert fs.record_fact("c1", "u1", "weight", value_text="体重は60kg")
        rows = fs.get_active_facts("u1", "weight")
        assert len(rows) == 1
        assert rows[0]["fact_id"] == "c1"
        assert rows[0]["status"] == "active"
        assert rows[0]["valid_to"] is None

    def test_missing_args_rejected(self):
        assert fs.record_fact("", "u1", "weight") is False
        assert fs.record_fact("c1", "u1", "") is False
        assert fs.get_active_facts("u1", "") == []

    def test_idempotent_reinsert(self):
        fs.record_fact("c1", "u1", "weight", value_text="v1")
        fs.record_fact("c1", "u1", "weight", value_text="v2")  # 再 ingest
        rows = fs.get_active_facts("u1", "weight")
        assert len(rows) == 1
        assert rows[0]["value_text"] == "v2"

    def test_user_isolation(self):
        fs.record_fact("c1", "u1", "weight")
        fs.record_fact("c2", "u2", "weight")
        assert [r["fact_id"] for r in fs.get_active_facts("u1", "weight")] == ["c1"]


class TestSupersede:
    def test_supersede_removes_from_active(self):
        fs.record_fact("old", "u1", "weight", value_text="60kg")
        assert fs.supersede_fact("old", "new")
        assert fs.get_active_facts("u1", "weight") == []
        hist = fs.get_history("u1", "weight")
        assert hist[0]["status"] == "superseded"
        assert hist[0]["superseded_by"] == "new"
        assert hist[0]["valid_to"] is not None

    def test_record_with_supersedes_chains(self):
        fs.record_fact("old", "u1", "weight", value_text="60kg")
        fs.record_fact("new", "u1", "weight", value_text="58kg",
                       supersedes="old")
        active = fs.get_active_facts("u1", "weight")
        assert [r["fact_id"] for r in active] == ["new"]
        hist = {r["fact_id"]: r for r in fs.get_history("u1", "weight")}
        assert hist["old"]["superseded_by"] == "new"
        assert hist["new"]["supersedes"] == "old"

    def test_count_rows(self):
        assert fs.count_rows() == 0
        fs.record_fact("c1", "u1", "weight")
        fs.record_fact("c2", "u1", "residence")
        assert fs.count_rows() == 2


class TestDecisiveRouteIntegration:
    @pytest.mark.asyncio
    async def test_conflict_check_uses_fact_store(self, monkeypatch):
        """フラグ ON かつ store に行があれば、mem.get_all を呼ばず SQL で照会する。"""
        import config
        from services.mem0 import ingest
        monkeypatch.setattr(config, "MEM0_FACT_STORE_ENABLED", True)
        fs.record_fact("old1", "u1", "weight", value_text="体重は60kg")

        class _NoGetAllMem:
            def get_all(self, **kw):
                raise AssertionError("fact_store 有効時は get_all を呼ばない")

        async def fake_classify(existing, candidate):
            return {"conflict_type": "update"}
        monkeypatch.setattr(
            "services.mem0.conflict.classify_conflict", fake_classify)
        kept = [{"text": "体重は58kg", "attribute": "weight"}]
        out = await ingest._attribute_conflict_check(
            _NoGetAllMem(), "u1", kept, exclude_ids=set())
        assert out == [("old1", "update")]

    @pytest.mark.asyncio
    async def test_empty_store_falls_back_to_chroma(self, monkeypatch):
        """store が空なら従来の Chroma 経路にフォールバック（backfill 前の保険）。"""
        import config
        from services.mem0 import ingest
        monkeypatch.setattr(config, "MEM0_FACT_STORE_ENABLED", True)

        class _ChromaMem:
            def get_all(self, user_id=None, filters=None, limit=None):
                return {"results": [
                    {"id": "cold", "memory": "体重は60kg",
                     "metadata": {"attribute": "weight"}},
                ]}

        rows = await ingest._get_facts_by_attribute(_ChromaMem(), "u1", "weight")
        assert [r["id"] for r in rows] == ["cold"]
