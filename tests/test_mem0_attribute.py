"""Phase A (Mem0 v4): attribute 正規化 + 決定的 conflict ルートのテスト。

cli_compat / claude_api を import しないので、この単体ファイルは
claude_api.__init__ 経由の import 循環を踏まない（extract/ingest/conflict は
モジュールトップで cli_compat を import しないため）。
"""

import time

import pytest

from services.mem0.extract import (
    _normalize_attribute, _normalize_condition, _normalize_scope,
)
from services.mem0.attribute_registry import (
    get_cardinality, get_conflict_group, is_update_cardinality,
    condition_compatibility, conditions_are_conflict_candidate,
)


class TestNormalizeAttribute:
    @pytest.mark.parametrize("raw,expected", [
        ("weight", "weight"),
        ("WEIGHT", "weight"),
        ("  Weight  ", "weight"),
        ("residence", "residence"),
        ("habit.coffee", "habit.coffee"),
        ("preference.music", "preference.music"),
        ("possession.headphones", "possession.headphones"),
        ("unknown_attr", "custom:unknown_attr"),
        ("体重", "custom:体重"),          # 統制語彙外は custom: に逃がす
        ("custom:foo", "custom:foo"),
        ("", ""),
        ("none", ""),
        ("null", ""),
        ("n/a", ""),
        ("-", ""),
        (None, ""),
        (123, ""),
        ([], ""),
    ])
    def test_normalize(self, raw, expected):
        assert _normalize_attribute(raw) == expected

    def test_long_value_is_capped(self):
        out = _normalize_attribute("x" * 200)
        assert out.startswith("custom:")
        assert len(out) <= 48


class _FakeMem:
    """mem.get_all を模す。accept_filters=False で SDK 非対応(TypeError)を再現。"""

    def __init__(self, rows, *, accept_filters=True):
        self._rows = rows
        self._accept_filters = accept_filters
        self.calls = []

    def get_all(self, user_id=None, filters=None, limit=None):
        self.calls.append({"user_id": user_id, "filters": filters, "limit": limit})
        if filters is not None and not self._accept_filters:
            raise TypeError("filters not supported by this SDK")
        rows = self._rows
        if filters and self._accept_filters:
            attr = filters.get("attribute")
            rows = [r for r in rows if (r.get("metadata") or {}).get("attribute") == attr]
        return {"results": rows}


class TestGetFactsByAttribute:
    @pytest.mark.asyncio
    async def test_filters_path(self):
        from services.mem0.ingest import _get_facts_by_attribute
        rows = [
            {"id": "1", "memory": "体重は60kg", "metadata": {"attribute": "weight"}},
            {"id": "2", "memory": "身長は170cm", "metadata": {"attribute": "height"}},
        ]
        mem = _FakeMem(rows)
        out = await _get_facts_by_attribute(mem, "u1", "weight")
        assert [r["id"] for r in out] == ["1"]

    @pytest.mark.asyncio
    async def test_fallback_on_typeerror(self):
        from services.mem0.ingest import _get_facts_by_attribute
        rows = [
            {"id": "1", "memory": "体重は60kg", "metadata": {"attribute": "weight"}},
            {"id": "2", "memory": "身長は170cm", "metadata": {"attribute": "height"}},
        ]
        mem = _FakeMem(rows, accept_filters=False)  # SDK が filters 非対応
        out = await _get_facts_by_attribute(mem, "u1", "weight")
        # 全件取得 → Python フィルタで weight だけ残る
        assert [r["id"] for r in out] == ["1"]

    @pytest.mark.asyncio
    async def test_excludes_archived(self):
        from services.mem0.ingest import _get_facts_by_attribute
        rows = [
            {"id": "1", "memory": "旧", "metadata": {
                "attribute": "weight", "valid_to": int(time.time()) - 100}},
            {"id": "2", "memory": "新", "metadata": {"attribute": "weight"}},
        ]
        mem = _FakeMem(rows)
        out = await _get_facts_by_attribute(mem, "u1", "weight")
        assert [r["id"] for r in out] == ["2"]  # valid_to 済みは除外

    @pytest.mark.asyncio
    async def test_empty_attribute_returns_empty(self):
        from services.mem0.ingest import _get_facts_by_attribute
        mem = _FakeMem([{"id": "1", "memory": "x", "metadata": {"attribute": "weight"}}])
        assert await _get_facts_by_attribute(mem, "u1", "") == []


class TestAttributeConflictCheck:
    @pytest.mark.asyncio
    async def test_detects_update(self, monkeypatch):
        from services.mem0 import ingest
        rows = [{"id": "old1", "memory": "体重は60kg",
                 "metadata": {"attribute": "weight"}}]
        mem = _FakeMem(rows)

        async def fake_classify(existing, candidate):
            return {"conflict_type": "update", "reason": "数値変化"}
        monkeypatch.setattr(
            "services.mem0.conflict.classify_conflict", fake_classify)
        kept = [{"text": "体重は58kg", "attribute": "weight"}]
        out = await ingest._attribute_conflict_check(
            mem, "u1", kept, exclude_ids=set())
        assert out == [("old1", "update")]

    @pytest.mark.asyncio
    async def test_skips_addition(self, monkeypatch):
        from services.mem0 import ingest
        rows = [{"id": "old1", "memory": "コーヒー好き",
                 "metadata": {"attribute": "preference.drink"}}]
        mem = _FakeMem(rows)

        async def fake_classify(existing, candidate):
            return {"conflict_type": "addition"}
        monkeypatch.setattr(
            "services.mem0.conflict.classify_conflict", fake_classify)
        kept = [{"text": "紅茶も好き", "attribute": "preference.drink"}]
        out = await ingest._attribute_conflict_check(
            mem, "u1", kept, exclude_ids=set())
        assert out == []  # addition は archive しない

    @pytest.mark.asyncio
    async def test_respects_exclude_ids(self, monkeypatch):
        from services.mem0 import ingest
        rows = [{"id": "old1", "memory": "体重は60kg",
                 "metadata": {"attribute": "weight"}}]
        mem = _FakeMem(rows)

        async def fake_classify(existing, candidate):
            return {"conflict_type": "update"}
        monkeypatch.setattr(
            "services.mem0.conflict.classify_conflict", fake_classify)
        kept = [{"text": "体重は58kg", "attribute": "weight"}]
        # dedup が既に old1 を処理済み → 二重 archive しない
        out = await ingest._attribute_conflict_check(
            mem, "u1", kept, exclude_ids={"old1"})
        assert out == []

    @pytest.mark.asyncio
    async def test_empty_attribute_skipped(self):
        from services.mem0 import ingest
        mem = _FakeMem([])
        kept = [{"text": "単発の出来事", "attribute": ""}]
        out = await ingest._attribute_conflict_check(
            mem, "u1", kept, exclude_ids=set())
        assert out == []  # attribute 空は決定ルート対象外

    @pytest.mark.asyncio
    async def test_identical_text_not_conflict(self, monkeypatch):
        from services.mem0 import ingest
        rows = [{"id": "old1", "memory": "体重は60kg",
                 "metadata": {"attribute": "weight"}}]
        mem = _FakeMem(rows)
        called = {"n": 0}

        async def fake_classify(existing, candidate):
            called["n"] += 1
            return {"conflict_type": "update"}
        monkeypatch.setattr(
            "services.mem0.conflict.classify_conflict", fake_classify)
        kept = [{"text": "体重は60kg", "attribute": "weight"}]  # 完全同一
        out = await ingest._attribute_conflict_check(
            mem, "u1", kept, exclude_ids=set())
        assert out == []
        assert called["n"] == 0  # 同一テキストは classify を呼ばない


class TestNormalizeCondition:
    @pytest.mark.parametrize("raw,expected", [
        ("time:morning", "time:morning"),
        ("TIME:Morning", "time:morning"),
        ("context:work", "context:work"),
        ("weird", "custom:weird"),
        ("custom:foo", "custom:foo"),
        ("", ""),
        ("none", ""),
        (None, ""),
        (123, ""),
    ])
    def test_normalize(self, raw, expected):
        assert _normalize_condition(raw) == expected


class TestNormalizeScope:
    @pytest.mark.parametrize("raw,expected", [
        ("global", "global"),
        ("conversation", "conversation"),
        ("meta", "meta"),
        ("GLOBAL", "global"),
        ("unknown", "global"),   # 未知は安全側 global
        ("", "global"),
        (None, "global"),
        (123, "global"),
    ])
    def test_normalize(self, raw, expected):
        assert _normalize_scope(raw) == expected


class TestConditionInConflictCheck:
    @pytest.mark.asyncio
    async def test_different_condition_not_conflict(self, monkeypatch):
        from services.mem0 import ingest
        rows = [{"id": "old1", "memory": "朝はコーヒー", "metadata": {
            "attribute": "habit.drink", "condition": "time:morning"}}]
        mem = _FakeMem(rows)
        called = {"n": 0}

        async def fake_classify(existing, candidate):
            called["n"] += 1
            return {"conflict_type": "update"}
        monkeypatch.setattr(
            "services.mem0.conflict.classify_conflict", fake_classify)
        kept = [{"text": "夜は紅茶", "attribute": "habit.drink",
                 "condition": "time:night"}]
        out = await ingest._attribute_conflict_check(
            mem, "u1", kept, exclude_ids=set())
        assert out == []              # 別条件下の事実 → 矛盾ではない
        assert called["n"] == 0       # condition が違えば classify を呼ばない

    @pytest.mark.asyncio
    async def test_same_condition_setlike_not_archived(self, monkeypatch):
        # habit.* は set_like(追加型)。同 condition で update 判定に至っても
        # archive しない(既存の好みを消さない。gpt-5.5-pro 指摘3)。
        from services.mem0 import ingest
        rows = [{"id": "old1", "memory": "朝はコーヒー", "metadata": {
            "attribute": "habit.drink", "condition": "time:morning"}}]
        mem = _FakeMem(rows)
        called = {"n": 0}

        async def fake_classify(existing, candidate):
            called["n"] += 1
            return {"conflict_type": "update"}
        monkeypatch.setattr(
            "services.mem0.conflict.classify_conflict", fake_classify)
        kept = [{"text": "朝は緑茶", "attribute": "habit.drink",
                 "condition": "time:morning"}]
        out = await ingest._attribute_conflict_check(
            mem, "u1", kept, exclude_ids=set())
        assert out == []          # set_like は archive しない
        assert called["n"] == 1   # 同 condition なので classify までは進む

    @pytest.mark.asyncio
    async def test_update_type_same_condition_archived(self, monkeypatch):
        # temporal(weight) は更新型。conflict 判定 update なら archive する。
        from services.mem0 import ingest
        rows = [{"id": "old1", "memory": "体重は60kg",
                 "metadata": {"attribute": "weight"}}]
        mem = _FakeMem(rows)

        async def fake_classify(existing, candidate):
            return {"conflict_type": "update"}
        monkeypatch.setattr(
            "services.mem0.conflict.classify_conflict", fake_classify)
        kept = [{"text": "体重は58kg", "attribute": "weight"}]
        out = await ingest._attribute_conflict_check(
            mem, "u1", kept, exclude_ids=set())
        assert out == [("old1", "update")]  # temporal → archive


class TestScopeFilter:
    def test_disabled_by_default(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MEM0_SCOPE_FILTER_ENABLED", False)
        from services.mem0.search import _is_fact_valid
        # フィルタ無効なら conversation scope でも valid
        assert _is_fact_valid({"metadata": {"scope": "conversation:123"}}) is True

    def test_conversation_excluded_without_channel(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MEM0_SCOPE_FILTER_ENABLED", True)
        from services.mem0.search import _is_fact_valid
        assert _is_fact_valid(
            {"metadata": {"scope": "conversation:123"}}) is False

    def test_conversation_matches_channel(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MEM0_SCOPE_FILTER_ENABLED", True)
        from services.mem0.search import _is_fact_valid
        assert _is_fact_valid(
            {"metadata": {"scope": "conversation:123"}},
            current_channel_id="123") is True
        assert _is_fact_valid(
            {"metadata": {"scope": "conversation:123"}},
            current_channel_id="999") is False

    def test_global_always_valid(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "MEM0_SCOPE_FILTER_ENABLED", True)
        from services.mem0.search import _is_fact_valid
        assert _is_fact_valid({"metadata": {"scope": "global"}}) is True
        assert _is_fact_valid({"metadata": {}}) is True  # scope 無し=global


class TestAttributeRegistry:
    @pytest.mark.parametrize("attr,expected", [
        ("weight", "temporal"),
        ("height", "single"),
        ("residence", "single"),
        ("habit.coffee", "set_like"),
        ("preference.music", "set_like"),
        ("possession.headphones", "set_like"),
        ("custom:foo", "set_like"),    # custom は保守的に set_like
        ("unknown_single", "single"),  # 未知は single
        ("", "single"),
    ])
    def test_cardinality(self, attr, expected):
        assert get_cardinality(attr) == expected

    @pytest.mark.parametrize("attr,expected", [
        ("weight", True),         # temporal → 更新型
        ("residence", True),      # single → 更新型
        ("habit.coffee", False),  # set_like → 追加型
        ("preference.music", False),
        ("custom:foo", False),    # custom=set_like → 追加型
    ])
    def test_is_update_cardinality(self, attr, expected):
        assert is_update_cardinality(attr) is expected

    @pytest.mark.parametrize("a,b,expected", [
        ("time:morning", "time:morning", "same"),
        ("time:morning", "time:night", "disjoint"),
        ("time:morning", "time:morning.weekday", "superset"),  # a ⊃ b
        ("time:morning.weekday", "time:morning", "subset"),    # a ⊂ b
        ("time:morning", "context:work", "unknown"),           # 別軸
        ("", "time:morning", "unknown"),
        ("time:morning", "", "unknown"),
        ("", "", "unknown"),
    ])
    def test_condition_compatibility(self, a, b, expected):
        assert condition_compatibility(a, b) == expected

    @pytest.mark.parametrize("a,b,expected", [
        ("time:morning", "time:night", False),           # disjoint → addition
        ("time:morning", "time:morning", True),          # same → conflict判定へ
        ("time:morning", "time:morning.weekday", True),  # superset
        ("time:morning", "context:work", True),          # unknown → 保守的にconflict
        ("", "", True),                                  # unknown
    ])
    def test_conflict_candidate(self, a, b, expected):
        assert conditions_are_conflict_candidate(a, b) is expected


class TestConflictGroup:
    def test_default_singleton(self):
        assert get_conflict_group("weight") == {"weight"}
        assert get_conflict_group("") == set()

    def test_group_membership(self, monkeypatch):
        from services.mem0 import attribute_registry as reg
        monkeypatch.setitem(
            reg.CONFLICT_GROUPS, "caffeine",
            {"habit.coffee", "custom:avoids_caffeine"},
        )
        assert get_conflict_group("habit.coffee") == {
            "habit.coffee", "custom:avoids_caffeine"}
        assert get_conflict_group("custom:avoids_caffeine") == {
            "habit.coffee", "custom:avoids_caffeine"}

    @pytest.mark.asyncio
    async def test_cross_group_conflict_archives_update_type(self, monkeypatch):
        # group 越し: 既存が residence(single=更新型) なら candidate が別 attribute
        # でも update 判定で archive される。
        from services.mem0 import ingest
        from services.mem0 import attribute_registry as reg
        monkeypatch.setitem(
            reg.CONFLICT_GROUPS, "home",
            {"residence", "custom:current_city"},
        )
        rows = [{"id": "old1", "memory": "東京に住んでいる",
                 "metadata": {"attribute": "residence"}}]
        mem = _FakeMem(rows)

        async def fake_classify(existing, candidate):
            return {"conflict_type": "update", "reason": "引っ越し"}
        monkeypatch.setattr(
            "services.mem0.conflict.classify_conflict", fake_classify)
        kept = [{"text": "大阪に引っ越した", "attribute": "custom:current_city"}]
        out = await ingest._attribute_conflict_check(
            mem, "u1", kept, exclude_ids=set())
        assert out == [("old1", "update")]

    @pytest.mark.asyncio
    async def test_cross_group_setlike_existing_not_archived(self, monkeypatch):
        # 消される側(既存)が set_like なら group 越し update でも archive しない。
        from services.mem0 import ingest
        from services.mem0 import attribute_registry as reg
        monkeypatch.setitem(
            reg.CONFLICT_GROUPS, "caffeine",
            {"habit.coffee", "residence"},  # 人工的な組み合わせ(テスト用)
        )
        rows = [{"id": "old1", "memory": "毎朝コーヒーを飲む",
                 "metadata": {"attribute": "habit.coffee"}}]
        mem = _FakeMem(rows)

        async def fake_classify(existing, candidate):
            return {"conflict_type": "update"}
        monkeypatch.setattr(
            "services.mem0.conflict.classify_conflict", fake_classify)
        kept = [{"text": "大阪に引っ越した", "attribute": "residence"}]
        out = await ingest._attribute_conflict_check(
            mem, "u1", kept, exclude_ids=set())
        assert out == []  # 既存 habit.coffee は set_like → 消さない


class TestFormatConditionScope:
    def test_condition_in_suffix(self):
        from services.mem0.format import _fact_meta_suffix
        out = _fact_meta_suffix({"metadata": {"condition": "time:night"}})
        assert "[条件: time:night]" in out

    def test_scope_conversation_marker(self):
        from services.mem0.format import _fact_meta_suffix
        out = _fact_meta_suffix({"metadata": {"scope": "conversation:123"}})
        assert "[この会話限定]" in out

    def test_scope_meta_marker(self):
        from services.mem0.format import _fact_meta_suffix
        out = _fact_meta_suffix({"metadata": {"scope": "meta"}})
        assert "[メタ情報]" in out

    def test_no_metadata_unchanged(self):
        from services.mem0.format import _fact_meta_suffix
        assert _fact_meta_suffix({"metadata": {}}) == ""
        assert _fact_meta_suffix({}) == ""

    def test_full_line_keeps_condition(self):
        from services.mem0.format import format_memories_for_prompt
        out = format_memories_for_prompt([{
            "memory": "夜は紅茶を好む",
            "metadata": {"condition": "time:night", "importance": 3, "tier": 1},
        }])
        assert "夜は紅茶を好む" in out
        assert "[条件: time:night]" in out


class TestNormalizeValue:
    def test_valid_quantity(self):
        from services.mem0.extract import _normalize_value
        out = _normalize_value({"type": "quantity", "value": 58, "unit": "kg"})
        assert out == {"type": "quantity", "value": 58.0, "unit": "kg"}

    def test_unit_lowercased_and_capped(self):
        from services.mem0.extract import _normalize_value
        out = _normalize_value(
            {"type": "quantity", "value": 1.5, "unit": " KG "})
        assert out["unit"] == "kg"

    @pytest.mark.parametrize("raw", [
        None, "58kg", 58, [],
        {"type": "quantity", "value": "58", "unit": "kg"},   # 数値でない
        {"type": "quantity", "value": True, "unit": "kg"},   # bool は拒否
        {"type": "quantity", "value": 58},                   # unit なし
        {"type": "date", "value": 20260703},                 # 未対応 type
        {"type": "quantity", "value": 58, "unit": ""},       # 空 unit
    ])
    def test_rejects_invalid(self, raw):
        from services.mem0.extract import _normalize_value
        assert _normalize_value(raw) is None


class TestTypedValueShadow:
    def _ex(self, value_json):
        return {"id": "e1", "memory": "体重は60kg",
                "metadata": {"attribute": "weight", "value_json": value_json}}

    def test_changed_logged(self, caplog):
        import json
        from services.mem0.ingest import _compare_typed_values_shadow
        ex = self._ex(json.dumps({"type": "quantity", "value": 60, "unit": "kg"}))
        cand = {"text": "体重は58kg",
                "value": {"type": "quantity", "value": 58.0, "unit": "kg"}}
        with caplog.at_level("INFO"):
            _compare_typed_values_shadow(ex, cand, "weight")
        assert "verdict=changed" in caplog.text

    def test_same_logged(self, caplog):
        import json
        from services.mem0.ingest import _compare_typed_values_shadow
        ex = self._ex(json.dumps({"type": "quantity", "value": 58, "unit": "kg"}))
        cand = {"value": {"type": "quantity", "value": 58.0, "unit": "kg"}}
        with caplog.at_level("INFO"):
            _compare_typed_values_shadow(ex, cand, "weight")
        assert "verdict=same" in caplog.text

    def test_unit_mismatch(self, caplog):
        import json
        from services.mem0.ingest import _compare_typed_values_shadow
        ex = self._ex(json.dumps({"type": "quantity", "value": 132, "unit": "lb"}))
        cand = {"value": {"type": "quantity", "value": 60.0, "unit": "kg"}}
        with caplog.at_level("INFO"):
            _compare_typed_values_shadow(ex, cand, "weight")
        assert "verdict=unit_mismatch" in caplog.text

    def test_missing_value_silent(self, caplog):
        from services.mem0.ingest import _compare_typed_values_shadow
        with caplog.at_level("INFO"):
            _compare_typed_values_shadow(
                {"metadata": {}}, {"value": None}, "weight")
        assert "ATTR_VALUE_COMPARE" not in caplog.text

    def test_corrupt_json_never_raises(self, caplog):
        from services.mem0.ingest import _compare_typed_values_shadow
        ex = self._ex("{{{not json")
        cand = {"value": {"type": "quantity", "value": 1.0, "unit": "kg"}}
        _compare_typed_values_shadow(ex, cand, "weight")  # raise しないこと


class TestExtractPoisoningGuard:
    def test_prompt_has_instruction_guard(self):
        from services.mem0.extract import _EXTRACT_PROMPT
        assert "運用指示" in _EXTRACT_PROMPT
        assert "管理者として扱え" in _EXTRACT_PROMPT
        # 事実の訂正は fact として残す旨も明記されていること
        assert "事実の更新・訂正は fact" in _EXTRACT_PROMPT


class _FakeSearchMem:
    def __init__(self, rows):
        self._rows = rows

    def search(self, query=None, user_id=None, limit=None):
        return {"results": [dict(r) for r in self._rows]}


class TestChannelWiring:
    @pytest.mark.asyncio
    async def test_search_memories_passes_channel(self, monkeypatch):
        import config
        from services.mem0 import search as s
        monkeypatch.setattr(config, "MEM0_SCOPE_FILTER_ENABLED", True)
        rows = [
            {"id": "1", "memory": "conv fact", "score": 0.1,
             "metadata": {"scope": "conversation:123"}},
            {"id": "2", "memory": "global fact", "score": 0.1, "metadata": {}},
        ]
        monkeypatch.setattr(s, "_get_mem0", lambda: _FakeSearchMem(rows))

        async def _noop():
            return None
        monkeypatch.setattr(s, "ensure_valid_token", _noop)
        monkeypatch.setattr(s, "_refresh_mem0_client_token", lambda: None)

        out = await s.search_memories("q", "u1", current_channel_id="123")
        assert {r["id"] for r in out} == {"1", "2"}   # 本来の会話では見える
        out = await s.search_memories("q", "u1", current_channel_id="999")
        assert {r["id"] for r in out} == {"2"}        # 他会話では落ちる
        out = await s.search_memories("q", "u1")      # channel 不明
        assert {r["id"] for r in out} == {"2"}

    @pytest.mark.asyncio
    async def test_core_cache_not_poisoned_across_channels(self, monkeypatch):
        import config
        from services.mem0 import search as s
        monkeypatch.setattr(config, "MEM0_SCOPE_FILTER_ENABLED", True)
        monkeypatch.setattr(config, "MEM0_CORE_MEMORY_ENABLED", True)
        s._CORE_CACHE.clear()
        facts = [
            {"id": "c1", "memory": "conv core",
             "metadata": {"importance": 5, "tier": 1,
                          "scope": "conversation:123"}},
            {"id": "g1", "memory": "global core",
             "metadata": {"importance": 5, "tier": 0}},
        ]
        calls = {"n": 0}

        async def fake_get_all(user_id, limit=None):
            calls["n"] += 1
            return [dict(f) for f in facts]
        monkeypatch.setattr(
            "services.mem0.client.get_all_memories", fake_get_all)

        # channel 123: conversation core も見える
        out = await s.get_core_memories("u1", current_channel_id="123")
        assert {m["id"] for m in out} == {"c1", "g1"}
        # channel 999 (キャッシュヒット): conversation core は落ちる
        out = await s.get_core_memories("u1", current_channel_id="999")
        assert {m["id"] for m in out} == {"g1"}
        # また 123 (キャッシュヒット): 復活する = キャッシュが汚染されていない
        out = await s.get_core_memories("u1", current_channel_id="123")
        assert {m["id"] for m in out} == {"c1", "g1"}
        assert calls["n"] == 1  # get_all は初回のみ(キャッシュ有効)
        s._CORE_CACHE.clear()
