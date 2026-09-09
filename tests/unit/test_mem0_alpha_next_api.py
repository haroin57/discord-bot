"""MCP integration contracts; only fake providers and private SQLite are used."""
import asyncio
import inspect
import json
import os
from pathlib import Path
import runpy
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MEM0_ALPHA_ENABLED") != "1", reason="alpha-only lane")
UID = "573489206240870410"


@pytest.fixture(scope="module", autouse=True)
def alpha_bootstrap():
    import services.mem0
    import llm_mem0.mcp_server as standalone
    import mcp_mem0_server as host
    return services.mem0, standalone, host


@pytest.mark.parametrize("result,success", [
    (None, False), ({}, False), (True, False), ({"other": "value"}, False),
    ({"status": "pending", "results": []}, False),
    ({"status": "partial", "failed": 1, "results": [{"id": "one"}]}, False),
    ({"status": "error", "results": []}, False),
    ({"error": "private-provider-body", "results": []}, False),
    ({"results": [], "failed": 1}, False),
    ({"results": []}, True), ({"results": [{"id": "one"}]}, True),
    ({"status": "ok", "results": []}, True),
    ({"status": "ok", "results": [{"id": "one"}]}, True),
    ({"results": "not-a-list"}, False), ({"results": ["not-a-row"]}, False),
    ({"status": "ok", "failed": True, "results": []}, False),
    ({"status": "ok", "results": ["not-a-row"]}, False),
])
@pytest.mark.asyncio
async def test_both_add_outcome_contracts(monkeypatch, result, success):
    import mcp_mem0_server as host
    from llm_mem0 import mcp_server as standalone
    for module, fn, args in ((host, host.add, ("fixture", "answer")),
                              (standalone, standalone.add_memory, ("fixture", UID))):
        monkeypatch.setattr(module, "add_memories", AsyncMock(return_value=result))
        output = await fn(*args)
        failed = output.startswith("Error:") or "add failed" in output
        assert failed is not success
        assert "private-provider-body" not in output
        if not success:
            assert not output.lower().startswith(("stored", "processed"))


@pytest.mark.parametrize("module_name", ["host", "standalone"])
@pytest.mark.asyncio
async def test_add_forwards_bounded_provenance_without_owner_change(monkeypatch, module_name):
    import mcp_mem0_server as host
    from llm_mem0 import mcp_server as standalone
    module = host if module_name == "host" else standalone
    provider = AsyncMock(return_value={"status": "ok", "results": []})
    monkeypatch.setattr(module, "add_memories", provider)
    kwargs = dict(user_id=UID, request_id="request:one", source_id="transcript:user",
                  source_message_ids=["message-2"], source_author_id="other-source-author",
                  source_timestamp="2026-01-01T00:00:00Z", channel_id="123",
                  valid_from="2026-01-01T00:00:00Z", valid_to=1798761600)
    if module_name == "host":
        await host.add("fixture", "answer", **kwargs)
    else:
        await standalone.add_memory("fixture", assistant_text="answer", **kwargs)
    actual = provider.call_args.kwargs
    assert actual["user_id"] == UID
    md = actual["metadata"]
    assert md["request_id"] == "request:one"
    assert md["source_author_id"] == "other-source-author"
    assert md["source_timestamp"] == "2026-01-01T00:00:00Z"
    assert json.loads(md["source_message_ids"]) == ["transcript:user", "message-2"]
    assert md["channel_id"] == "123" and md["valid_from"] == 1767225600
    assert md["valid_to"] == 1798761600 and "user_id" not in md


@pytest.mark.parametrize("bad", [
    {"request_id": "x" * 257}, {"source_id": "bad\nsource"},
    {"source_message_ids": ["x"] * 101}, {"source_message_ids": "x"},
    {"source_message_ids": [1]}, {"source_author_id": 3},
    {"source_timestamp": True}, {"source_timestamp": float("nan")},
    {"source_timestamp": "2026-01-01T00:00:00"}, {"valid_from": float("inf")},
    {"valid_from": 200, "valid_to": 100}, {"valid_to": -1},
    {"valid_from": "yesterday"}, {"channel_id": "x" * 257},
    {"source_timestamp": 10**1000},
])
@pytest.mark.asyncio
async def test_both_add_reject_bad_provenance_before_provider(monkeypatch, bad):
    import mcp_mem0_server as host
    from llm_mem0 import mcp_server as standalone
    for module, fn, args in ((host, host.add, ("fixture", "answer")),
                              (standalone, standalone.add_memory, ("fixture", UID))):
        provider = AsyncMock()
        monkeypatch.setattr(module, "add_memories", provider)
        output = await fn(*args, **bad)
        assert "rejected" in output or output.startswith("Error:")
        provider.assert_not_awaited()


def test_add_signatures_expose_same_optional_metadata():
    import mcp_mem0_server as host
    from llm_mem0 import mcp_server as standalone
    fields = {"request_id", "source_id", "source_message_ids", "source_author_id",
              "source_timestamp", "channel_id", "valid_from", "valid_to"}
    for fn in (host.add, standalone.add_memory):
        signature = inspect.signature(fn)
        assert fields <= set(signature.parameters)
        assert all(signature.parameters[field].default is None for field in fields)


def test_real_mcp_schema_does_not_coerce_bool_into_time_or_limit():
    from pydantic import ValidationError
    import mcp_mem0_server as host
    from llm_mem0 import mcp_server as standalone
    for module, name, arguments in ((host, "add", {"user_message": "fixture", "assistant_message": "answer"}),
                                    (standalone, "add_memory", {"user_text": "fixture", "user_id": UID})):
        tool = module.mcp._tool_manager.get_tool(name)
        assert "request_id" in tool.parameters["properties"]
        with pytest.raises(ValidationError):
            tool.fn_metadata.arg_model.model_validate({**arguments, "source_timestamp": True})
        with pytest.raises(ValidationError):
            tool.fn_metadata.arg_model.model_validate({**arguments, "valid_from": True})
    for module, name in ((host, "list_all"), (standalone, "list_memories")):
        with pytest.raises(ValidationError):
            module.mcp._tool_manager.get_tool(name).fn_metadata.arg_model.model_validate({"limit": True})


@pytest.mark.asyncio
async def test_add_same_retry_id_and_safe_continuation(monkeypatch):
    import mcp_mem0_server as host
    from llm_mem0 import mcp_server as standalone
    result = {"status": "partial", "results": [], "continuation_id": "mem0-input:abc123"}
    for module, fn, args in ((host, host.add, ("fixture", "answer")),
                              (standalone, standalone.add_memory, ("fixture", UID))):
        provider = AsyncMock(return_value=result)
        monkeypatch.setattr(module, "add_memories", provider)
        output = await fn(*args, request_id="retry-1")
        assert "request_id=mem0-input:abc123" in output
        await fn(*args, request_id="retry-1")
        assert provider.call_args_list[0].kwargs["metadata"] == provider.call_args_list[1].kwargs["metadata"]
    from llm_mem0.api_contracts import add_failure, classify_add_outcome
    assert "<secret>" not in add_failure(classify_add_outcome(result), {**result, "continuation_id": "<secret>"})


@pytest.mark.asyncio
async def test_both_add_omitted_metadata_and_cancellation_are_preserved(monkeypatch):
    import mcp_mem0_server as host
    from llm_mem0 import mcp_server as standalone
    for module, fn, args in ((host, host.add, ("fixture", "answer")),
                              (standalone, standalone.add_memory, ("fixture", UID))):
        provider = AsyncMock(return_value={"results": []})
        monkeypatch.setattr(module, "add_memories", provider)
        await fn(*args)
        assert "metadata" not in provider.call_args.kwargs
        provider.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await fn(*args)


@pytest.mark.parametrize("missing_name,should_fallback", [
    ("llm_mem0", True), ("llm_mem0.api_contracts", True),
    ("fixture_internal_dependency", False),
])
def test_legacy_contract_shim_only_falls_back_for_target_module(monkeypatch, missing_name, should_fallback):
    import importlib
    import mcp_mem0_server as host
    original_import = importlib.import_module
    def missing_target(name, *args, **kwargs):
        if name == "llm_mem0.api_contracts":
            raise ModuleNotFoundError("fixture missing module", name=missing_name)
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(importlib, "import_module", missing_target)
    path = Path(host.__file__).parent / "services/mem0/api_contracts.py"
    if should_fallback:
        loaded = runpy.run_path(str(path))
        assert loaded["contracts"].classify_add_outcome({}).status == "unknown"
    else:
        with pytest.raises(ModuleNotFoundError) as caught:
            runpy.run_path(str(path))
        assert caught.value.name == missing_name


@pytest.mark.asyncio
async def test_host_strict_outage_is_not_empty_or_permission(monkeypatch):
    import mcp_mem0_server as host
    from llm_mem0 import client
    monkeypatch.setattr(client, "_get_mem0", lambda: None)
    monkeypatch.setattr(host, "get_all_memories", client.get_all_memories)
    delete = AsyncMock()
    monkeypatch.setattr(host, "delete_memory", delete)
    assert "list failed" in await host.list_all()
    output = await host.delete("fixture-id")
    assert "ownership could not be verified" in output and output != "permission denied"
    delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_host_list_ids_details_and_offset(monkeypatch):
    import mcp_mem0_server as host
    provider = AsyncMock(return_value=[{"id": str(i), "memory": f"fixture-{i}",
                                       "metadata": {"source": f"source-{i}"}} for i in range(4)])
    monkeypatch.setattr(host, "get_all_memories", provider)
    output = await host.list_all(limit=2, offset=2, details=True)
    assert "fixture-0" not in output and "fixture-2" in output and "id=2" in output and "source-2" in output
    assert provider.call_args.kwargs == {"user_id": UID, "limit": 4, "strict": True}
    assert "id=0" in await host.list_all(limit=1)
    assert "rejected" in await host.list_all(offset=True)


@pytest.mark.asyncio
async def test_legacy_listing_signature_remains_usable(monkeypatch):
    import mcp_mem0_server as host
    async def legacy(user_id, limit=None):
        return [{"id": "legacy", "memory": "fixture"}]
    monkeypatch.setattr(host, "get_all_memories", legacy)
    assert "id=legacy" in await host.list_all()


@pytest.mark.parametrize("which", ["host", "standalone"])
@pytest.mark.asyncio
async def test_explicit_hybrid_dispatch_has_no_llm_calls(monkeypatch, which):
    import mcp_mem0_server as host
    from llm_mem0 import mcp_server as standalone, search
    from llm_mem0 import client
    hybrid = AsyncMock(return_value=[{"id": "one", "memory": "fixture"}])
    monkeypatch.setattr(search, "search_memories_hybrid", hybrid)
    monkeypatch.setattr(client, "_get_mem0", lambda: object())
    module = host if which == "host" else standalone
    plain, smart = AsyncMock(side_effect=AssertionError), AsyncMock(side_effect=AssertionError)
    monkeypatch.setattr(module, "search_memories", plain)
    monkeypatch.setattr(module, "search_memories_smart", smart)
    fn = host.search if which == "host" else standalone.search_memory
    output = await fn("fixture", user_id=UID, mode="hybrid", smart=True, channel_id="123")
    assert "fixture" in output
    hybrid.assert_awaited_once()
    assert hybrid.call_args.kwargs["current_channel_id"] == "123"
    plain.assert_not_awaited()
    smart.assert_not_awaited()
    assert "rejected" in await host.search("fixture", mode="unknown")


@pytest.mark.asyncio
async def test_fact_history_disabled_error_empty_and_rows(monkeypatch, tmp_path):
    import mcp_mem0_server as host
    from llm_mem0 import mcp_server as standalone, fact_store
    monkeypatch.setenv("MEM0_FACT_STORE_ENABLED", "0")
    assert "disabled" in await host.fact_history("job")
    assert "disabled" in await standalone.fact_history("job", UID)
    monkeypatch.setenv("MEM0_FACT_STORE_ENABLED", "1")
    def failed(*args, strict=False):
        assert strict
        raise RuntimeError("private database body")
    monkeypatch.setattr(fact_store, "get_history", failed)
    for fn, args in ((host.fact_history, ("job",)), (standalone.fact_history, ("job", UID))):
        output = await fn(*args)
        assert "unavailable" in output and "private database body" not in output
    def rows(*args, strict=False):
        assert strict
        return [{"fact_id": "one", "user_id": UID, "text": "fixture"}]
    monkeypatch.setattr(fact_store, "get_history", rows)
    assert json.loads(await host.fact_history("job"))["untrusted_fact_history"][0]["fact_id"] == "one"


def test_outputs_are_bounded_and_details_remain_valid_json():
    import mcp_mem0_server as host
    from llm_mem0.api_contracts import MAX_OUTPUT_CHARS, details_json
    rows = [{"id": str(i), "memory": "x" * 100_000, "metadata": {"nested": ["y" * 10000] * 500}} for i in range(20)]
    assert len(host._format_for_mcp(rows, details=True)) <= MAX_OUTPUT_CHARS
    rendered = details_json("untrusted_memory_data", rows)
    assert len(rendered) <= MAX_OUTPUT_CHARS and json.loads(rendered)["truncated"] is True


@pytest.mark.asyncio
async def test_empty_host_search_output_is_also_bounded(monkeypatch):
    import mcp_mem0_server as host
    from llm_mem0.api_contracts import MAX_OUTPUT_CHARS
    monkeypatch.setattr(host, "search_memories", AsyncMock(return_value=[]))
    output = await host.search("x" * 100_000)
    assert "no memories found" in output and "output truncated" in output
    assert len(output) <= MAX_OUTPUT_CHARS


@pytest.mark.asyncio
async def test_helper_timeout_and_caller_cancellation(monkeypatch):
    from llm_mem0 import llm, auth
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    class Backend:
        async def complete(self, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    backend = Backend()
    with pytest.raises(asyncio.TimeoutError):
        await llm.complete_text(system="fixture", user_message="fixture", model="same-model", max_tokens=9,
                                timeout_s=0.01, backend=backend)
    assert cancelled.is_set()
    monkeypatch.setattr(auth, "get_auth_backend", lambda: backend)
    assert await llm.complete_json(system="fixture", user_message="fixture", model="same-model", max_tokens=9, timeout_s=0.01) is None
    task = asyncio.create_task(llm.complete_text(system="fixture", user_message="fixture", model="same-model", max_tokens=9, timeout_s=1, backend=backend))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("budget", [True, 0, -1, 601, 10**1000, float("inf"), float("nan"), "1"])
@pytest.mark.asyncio
async def test_helper_rejects_invalid_explicit_budgets(budget):
    from llm_mem0 import llm
    backend = AsyncMock()
    with pytest.raises(ValueError):
        await llm.complete_text(system="s", user_message="u", model="m", max_tokens=1, timeout_s=budget, backend=backend)
    backend.complete.assert_not_called()


@pytest.mark.asyncio
async def test_helper_settings_deadline_override_and_model_unchanged(monkeypatch):
    from llm_mem0 import llm
    backend = AsyncMock()
    backend.complete.return_value = "```json\n{\"ok\":true}\n```"
    monkeypatch.setenv("MEM0_HELPER_TIMEOUT_S", "0.02")
    text = await llm.complete_text(system="s", user_message="u", model="existing-model", max_tokens=321, backend=backend)
    assert text.startswith("```json")
    backend.complete.assert_awaited_once_with(system="s", user_message="u", model="existing-model", max_tokens=321)
    monkeypatch.setenv("MEM0_HELPER_TIMEOUT_S", "nan")
    with pytest.raises(ValueError):
        await llm.complete_text(system="s", user_message="u", model="m", max_tokens=1, backend=backend)
    assert await llm.complete_text(system="s", user_message="u", model="m", max_tokens=1, timeout_s=0.1, backend=backend)
