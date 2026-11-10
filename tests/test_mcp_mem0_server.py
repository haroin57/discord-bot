"""Unit tests for mcp_mem0_server.py with fixture-scoped service mocks."""

from unittest.mock import AsyncMock

import pytest

import mcp_mem0_server as _m


_MEM0_SVC_ATTRS = (
    "add_memories",
    "delete_memory",
    "get_all_memories",
    "search_memories",
    "search_memories_smart",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_UID = "573489206240870410"  # 先輩's default Discord snowflake


def _svc(name: str):
    """Return the AsyncMock for a services.mem0 function."""
    return getattr(_m, name)


@pytest.fixture(autouse=True)
def reset_mocks(monkeypatch):
    """Install fresh service mocks on the module under test for one test."""
    for name in _MEM0_SVC_ATTRS:
        monkeypatch.setattr(_m, name, AsyncMock(return_value=None))
    yield


# ===========================================================================
# _validated_user_id
# ===========================================================================


class TestValidatedUserId:
    def test_valid_snowflake(self):
        assert _m._validated_user_id(VALID_UID) == VALID_UID

    def test_min_length_15_digits(self):
        uid = "1" * 15
        assert _m._validated_user_id(uid) == uid

    def test_max_length_22_digits(self):
        uid = "9" * 22
        assert _m._validated_user_id(uid) == uid

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="invalid user_id"):
            _m._validated_user_id("12345")

    def test_too_long_raises(self):
        with pytest.raises(ValueError, match="invalid user_id"):
            _m._validated_user_id("1" * 23)

    def test_non_digit_raises(self):
        with pytest.raises(ValueError, match="invalid user_id"):
            _m._validated_user_id("57348920624087041x")

    def test_non_string_raises(self):
        with pytest.raises(ValueError, match="invalid user_id"):
            _m._validated_user_id(573489206240870410)  # type: ignore[arg-type]

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="invalid user_id"):
            _m._validated_user_id("")


# ===========================================================================
# _clamped_limit
# ===========================================================================


class TestClampedLimit:
    def test_normal_value(self):
        assert _m._clamped_limit(10) == 10

    def test_zero_clamps_to_1(self):
        assert _m._clamped_limit(0) == 1

    def test_negative_clamps_to_1(self):
        assert _m._clamped_limit(-99) == 1

    def test_above_max_clamps(self):
        assert _m._clamped_limit(9999) == _m._MAX_LIMIT

    def test_exact_max(self):
        assert _m._clamped_limit(_m._MAX_LIMIT) == _m._MAX_LIMIT

    def test_non_int_string_falls_back_to_1(self):
        assert _m._clamped_limit("bad") == 1  # type: ignore[arg-type]

    def test_none_falls_back_to_1(self):
        assert _m._clamped_limit(None) == 1  # type: ignore[arg-type]

    def test_string_numeric(self):
        assert _m._clamped_limit("20") == 20  # type: ignore[arg-type]


# ===========================================================================
# _format_for_mcp
# ===========================================================================


class TestFormatForMcp:
    def test_basic(self):
        mems = [{"memory": "foo"}, {"memory": "bar"}]
        result = _m._format_for_mcp(mems)
        assert result == "- foo\n- bar"

    def test_uses_text_fallback(self):
        mems = [{"text": "hello"}]
        result = _m._format_for_mcp(mems)
        assert result == "- hello"

    def test_empty_list_returns_empty_string(self):
        assert _m._format_for_mcp([]) == ""

    def test_truncation_marker(self):
        mems = [{"memory": str(i)} for i in range(5)]
        result = _m._format_for_mcp(mems, limit=3)
        assert "(+2 more" in result
        assert result.count("\n") == 3  # 3 items + 1 truncation line

    def test_no_truncation_when_within_limit(self):
        mems = [{"memory": "a"}, {"memory": "b"}]
        result = _m._format_for_mcp(mems, limit=5)
        assert "(+" not in result

    def test_items_without_memory_or_text_skipped(self):
        mems = [{"other": "x"}, {"memory": "real"}]
        result = _m._format_for_mcp(mems)
        assert result == "- real"


# ===========================================================================
# search tool
# ===========================================================================


class TestSearchTool:
    @pytest.mark.asyncio
    async def test_plain_search_calls_search_memories(self):
        _svc("search_memories").return_value = [{"memory": "result1"}]
        out = await _m.search(
            query="test query", limit=3, user_id=VALID_UID, smart=False
        )
        _svc("search_memories").assert_awaited_once_with(
            query="test query", user_id=VALID_UID, limit=3
        )
        assert "result1" in out

    @pytest.mark.asyncio
    async def test_smart_search_calls_search_memories_smart(self):
        _svc("search_memories_smart").return_value = [{"memory": "smart_result"}]
        out = await _m.search(
            query="smart test", limit=2, user_id=VALID_UID, smart=True
        )
        _svc("search_memories_smart").assert_awaited_once_with(
            query="smart test", user_id=VALID_UID, limit=2
        )
        assert "smart_result" in out

    @pytest.mark.asyncio
    async def test_empty_result_returns_no_memories_message(self):
        _svc("search_memories").return_value = []
        out = await _m.search(query="nothing", user_id=VALID_UID)
        assert "no memories found" in out

    @pytest.mark.asyncio
    async def test_invalid_user_id_returns_rejected(self):
        out = await _m.search(query="q", user_id="bad_id")
        assert "rejected" in out
        _svc("search_memories").assert_not_awaited()

    @pytest.mark.asyncio
    async def test_limit_clamped(self):
        _svc("search_memories").return_value = [{"memory": "x"}]
        await _m.search(query="q", limit=9999, user_id=VALID_UID)
        call_limit = _svc("search_memories").call_args.kwargs["limit"]
        assert call_limit == _m._MAX_LIMIT

    @pytest.mark.asyncio
    async def test_default_user_id_used(self):
        _svc("search_memories").return_value = [{"memory": "y"}]
        await _m.search(query="q")
        assert _svc("search_memories").call_args.kwargs["user_id"] == _m.DEFAULT_USER_ID


# ===========================================================================
# add tool
# ===========================================================================


class TestAddTool:
    @pytest.mark.asyncio
    async def test_basic_add_without_context(self):
        _svc("add_memories").return_value = {"results": [{"id": "1"}, {"id": "2"}]}
        out = await _m.add(
            user_message="hello",
            assistant_message="world",
            user_id=VALID_UID,
        )
        assert "stored 2 new memories" in out
        called_msgs = _svc("add_memories").call_args.args[0]
        assert called_msgs[0]["role"] == "user"
        assert called_msgs[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_add_with_context_prepends_system_message(self):
        _svc("add_memories").return_value = {"results": [{"id": "a"}]}
        await _m.add(
            user_message="hi",
            assistant_message="hey",
            user_id=VALID_UID,
            context="some context",
        )
        msgs = _svc("add_memories").call_args.args[0]
        assert msgs[0]["role"] == "system"
        assert "some context" in msgs[0]["content"]
        assert msgs[1]["role"] == "user"
        assert msgs[2]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_add_returns_failed_when_result_falsy(self):
        _svc("add_memories").return_value = None
        out = await _m.add(user_message="u", assistant_message="a", user_id=VALID_UID)
        assert "failed" in out

    @pytest.mark.asyncio
    async def test_add_invalid_user_id_rejected(self):
        out = await _m.add(user_message="u", assistant_message="a", user_id="short")
        assert "rejected" in out
        _svc("add_memories").assert_not_awaited()

    @pytest.mark.asyncio
    async def test_add_result_without_results_key_is_not_success(self):
        _svc("add_memories").return_value = {"other": "data"}
        out = await _m.add(user_message="u", assistant_message="a", user_id=VALID_UID)
        assert "add failed" in out and "incomplete" in out
        assert not out.startswith("stored")

    @pytest.mark.asyncio
    async def test_add_non_dict_result_is_not_success(self):
        _svc("add_memories").return_value = True
        out = await _m.add(user_message="u", assistant_message="a", user_id=VALID_UID)
        assert "add failed" in out and "incomplete" in out
        assert not out.startswith("stored")


# ===========================================================================
# list_all tool
# ===========================================================================


class TestListAllTool:
    @pytest.mark.asyncio
    async def test_list_returns_formatted_memories(self):
        _svc("get_all_memories").return_value = [
            {"memory": "mem_a"},
            {"memory": "mem_b"},
        ]
        out = await _m.list_all(user_id=VALID_UID, limit=10)
        assert "mem_a" in out
        assert "mem_b" in out

    @pytest.mark.asyncio
    async def test_list_empty_returns_no_memories_message(self):
        _svc("get_all_memories").return_value = []
        out = await _m.list_all(user_id=VALID_UID)
        assert "no memories stored" in out

    @pytest.mark.asyncio
    async def test_list_invalid_user_id(self):
        out = await _m.list_all(user_id="BAD")
        assert "rejected" in out
        _svc("get_all_memories").assert_not_awaited()

    @pytest.mark.asyncio
    async def test_list_limit_passed_to_service(self):
        _svc("get_all_memories").return_value = [{"memory": "x"}]
        await _m.list_all(user_id=VALID_UID, limit=7)
        assert _svc("get_all_memories").call_args.kwargs["limit"] == 7

    @pytest.mark.asyncio
    async def test_list_limit_clamped(self):
        _svc("get_all_memories").return_value = [{"memory": "x"}]
        await _m.list_all(user_id=VALID_UID, limit=9999)
        assert _svc("get_all_memories").call_args.kwargs["limit"] == _m._MAX_LIMIT

    @pytest.mark.asyncio
    async def test_list_truncation_when_many_memories(self):
        mems = [{"memory": f"item{i}"} for i in range(10)]
        _svc("get_all_memories").return_value = mems
        out = await _m.list_all(user_id=VALID_UID, limit=3)
        assert "(+7 more" in out


# ===========================================================================
# delete tool
# ===========================================================================


class TestDeleteTool:
    @pytest.mark.asyncio
    async def test_delete_success(self):
        _svc("get_all_memories").return_value = [{"id": "abc-123", "memory": "x"}]
        _svc("delete_memory").return_value = True
        out = await _m.delete(memory_id="abc-123")
        assert "deleted abc-123" in out
        _svc("delete_memory").assert_awaited_once_with("abc-123")

    @pytest.mark.asyncio
    async def test_delete_failure(self):
        _svc("get_all_memories").return_value = [{"id": "xyz", "memory": "x"}]
        _svc("delete_memory").return_value = False
        out = await _m.delete(memory_id="xyz")
        assert "delete failed" in out
        assert "xyz" in out

    @pytest.mark.asyncio
    async def test_delete_none_return(self):
        _svc("get_all_memories").return_value = [{"id": "nope", "memory": "x"}]
        _svc("delete_memory").return_value = None
        out = await _m.delete(memory_id="nope")
        assert "delete failed" in out
