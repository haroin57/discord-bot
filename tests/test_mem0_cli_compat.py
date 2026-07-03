"""Unit tests for services/mem0/cli_compat.py — all external I/O is mocked."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import services.mem0.cli_compat as compat_mod


# ---------------------------------------------------------------------------
# Mock response helpers
# ---------------------------------------------------------------------------

class _TextBlock:
    def __init__(self, text: str):
        self.text = text


class _ThinkingBlock:
    """No .text attribute — simulates ThinkingBlock from the real API."""


class _FakeResponse:
    def __init__(self, content, stop_reason=None, model=None):
        self.content = content
        self.stop_reason = stop_reason
        self.model = model


# ---------------------------------------------------------------------------
# cli_system tests
# ---------------------------------------------------------------------------

class TestCliSystem:
    def test_cli_system_structure(self):
        """cli_system returns a list of exactly 4 dicts, each with type='text'."""
        system = compat_mod.cli_system("do the thing")
        assert isinstance(system, list)
        assert len(system) == 4
        for block in system:
            assert isinstance(block, dict)
            assert block.get("type") == "text"

    def test_cli_system_task_prompt_at_index_3(self):
        """system[3]['text'] is the task_prompt passed in."""
        prompt = "my specific task prompt"
        system = compat_mod.cli_system(prompt)
        assert system[3]["text"] == prompt

    def test_cli_system_cache_control(self):
        """system[0] and system[2] carry cache_control; system[1] and system[3] do not."""
        system = compat_mod.cli_system("some prompt")
        assert "cache_control" in system[0]
        assert "cache_control" in system[2]
        assert "cache_control" not in system[1]
        assert "cache_control" not in system[3]

    def test_cli_system_cache_control_ttl_is_1h(self):
        """The cached system blocks carry ttl:1h, matching the main bot path
        (services/claude_api/tool_loop.py:_build_cached_system) and the real CLI
        capture. The default 5-min TTL would expire between Mem0 helper calls and
        re-pay the cached CLI prefix on every fact-extraction / dedup / rerank."""
        system = compat_mod.cli_system("some prompt")
        assert system[0]["cache_control"] == {
            "type": "ephemeral", "ttl": "1h", "scope": "global"}
        assert system[2]["cache_control"] == {
            "type": "ephemeral", "ttl": "1h", "scope": "global"}


# ---------------------------------------------------------------------------
# cli_headers tests
# ---------------------------------------------------------------------------

class TestCliHeaders:
    def test_cli_headers_required_keys(self):
        """cli_headers includes the four mandatory identity/session keys."""
        headers = compat_mod.cli_headers()
        assert "x-app" in headers
        assert "User-Agent" in headers
        assert "X-Claude-Code-Session-Id" in headers
        assert "x-client-request-id" in headers

    def test_cli_headers_unique_request_id(self):
        """Two consecutive calls produce different x-client-request-id values."""
        h1 = compat_mod.cli_headers()
        h2 = compat_mod.cli_headers()
        assert h1["x-client-request-id"] != h2["x-client-request-id"]

    def test_cli_headers_x_app_is_cli(self):
        """x-app header value is 'cli'."""
        assert compat_mod.cli_headers()["x-app"] == "cli"


# ---------------------------------------------------------------------------
# cli_messages_create tests
# ---------------------------------------------------------------------------

class TestCliMessagesCreate:
    @pytest.mark.asyncio
    async def test_cli_messages_create_calls_client(self):
        """cli_messages_create calls client.messages.create with correct positional args."""
        fake_response = MagicMock()
        mock_create = AsyncMock(return_value=fake_response)

        mock_client = MagicMock()
        mock_client.messages.create = mock_create

        result = await compat_mod.cli_messages_create(
            mock_client,
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            user_message="is this a duplicate?",
            task_prompt="judge duplicate facts",
        )

        assert result is fake_response
        mock_create.assert_called_once()

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
        assert call_kwargs["max_tokens"] == 512

        # system must be the 4-block array
        assert isinstance(call_kwargs["system"], list)
        assert len(call_kwargs["system"]) == 4
        assert call_kwargs["system"][3]["text"] == "judge duplicate facts"

        # messages must contain the user turn
        messages = call_kwargs["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "is this a duplicate?"

    @pytest.mark.asyncio
    async def test_cli_messages_create_includes_extra_headers(self):
        """cli_messages_create passes extra_headers (including anthropic-beta)."""
        mock_create = AsyncMock(return_value=MagicMock())
        mock_client = MagicMock()
        mock_client.messages.create = mock_create

        await compat_mod.cli_messages_create(
            mock_client,
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            user_message="hi",
            task_prompt="task",
        )

        call_kwargs = mock_create.call_args.kwargs
        extra = call_kwargs.get("extra_headers", {})
        assert "anthropic-beta" in extra
        assert "x-app" in extra

    @pytest.mark.asyncio
    async def test_no_adaptive_thinking_for_haiku(self):
        """Haiku rejects adaptive thinking with HTTP 400 — the kwarg must be
        omitted for Haiku models (regression: 22 live 400s/day before fix)."""
        mock_create = AsyncMock(return_value=MagicMock())
        mock_client = MagicMock()
        mock_client.messages.create = mock_create
        await compat_mod.cli_messages_create(
            mock_client,
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            user_message="hi",
            task_prompt="task",
        )
        assert "thinking" not in mock_create.call_args.kwargs

    @pytest.mark.asyncio
    async def test_no_adaptive_thinking_even_for_opus(self):
        """Opus/Sonnet accept adaptive thinking but must not receive it here:
        Mem0 helpers cap max_tokens at 200-500 (sized for a short JSON reply),
        and a thinking-capable model can spend the whole budget on thinking
        alone, leaving zero TextBlocks (regression: rerank/rewrite silently
        failing after MEM0_RERANK_MODEL/MEM0_REWRITE_MODEL were switched to
        claude-sonnet-4-6)."""
        mock_create = AsyncMock(return_value=MagicMock())
        mock_client = MagicMock()
        mock_client.messages.create = mock_create
        await compat_mod.cli_messages_create(
            mock_client,
            model="claude-opus-4-8",
            max_tokens=256,
            user_message="hi",
            task_prompt="task",
        )
        assert "thinking" not in mock_create.call_args.kwargs

    @pytest.mark.asyncio
    async def test_no_adaptive_thinking_for_sonnet(self):
        """Same guarantee for Sonnet, the model actually involved in the
        production regression this test suite guards against."""
        mock_create = AsyncMock(return_value=MagicMock())
        mock_client = MagicMock()
        mock_client.messages.create = mock_create
        await compat_mod.cli_messages_create(
            mock_client,
            model="claude-sonnet-4-6",
            max_tokens=400,
            user_message="hi",
            task_prompt="task",
        )
        assert "thinking" not in mock_create.call_args.kwargs


# ---------------------------------------------------------------------------
# extract_text tests
# ---------------------------------------------------------------------------

class TestExtractText:
    def test_extract_text_simple(self):
        """Single TextBlock → returns its text."""
        resp = _FakeResponse(content=[_TextBlock("hello world")])
        assert compat_mod.extract_text(resp) == "hello world"

    def test_extract_text_with_thinking_blocks(self):
        """ThinkingBlock before TextBlock → skips ThinkingBlock, returns text."""
        resp = _FakeResponse(content=[
            _ThinkingBlock(),
            _ThinkingBlock(),
            _TextBlock("actual answer"),
        ])
        assert compat_mod.extract_text(resp) == "actual answer"

    def test_extract_text_empty(self):
        """No content blocks → returns empty string."""
        resp = _FakeResponse(content=[])
        assert compat_mod.extract_text(resp) == ""

    def test_extract_text_none_content(self):
        """content=None → returns empty string."""
        resp = _FakeResponse(content=None)
        assert compat_mod.extract_text(resp) == ""

    def test_extract_text_only_thinking_blocks(self):
        """Only ThinkingBlocks, no TextBlock → returns empty string."""
        resp = _FakeResponse(content=[_ThinkingBlock(), _ThinkingBlock()])
        assert compat_mod.extract_text(resp) == ""

    def test_extract_text_returns_first_text_block(self):
        """Multiple TextBlocks → returns the first one."""
        resp = _FakeResponse(content=[
            _TextBlock("first"),
            _TextBlock("second"),
        ])
        assert compat_mod.extract_text(resp) == "first"

    def test_extract_text_warns_on_max_tokens_exhaustion(self, caplog):
        """No TextBlock + stop_reason=max_tokens logs a diagnostic warning,
        so a token-budget exhaustion (thinking ate the whole response) is
        distinguishable in logs from any other empty-response cause."""
        resp = _FakeResponse(
            content=[_ThinkingBlock()],
            stop_reason="max_tokens",
            model="claude-sonnet-4-6",
        )
        with caplog.at_level("WARNING", logger="services.mem0.cli_compat"):
            assert compat_mod.extract_text(resp) == ""
        assert any("max_tokens" in r.message for r in caplog.records)

    def test_extract_text_no_warning_when_not_max_tokens(self, caplog):
        """No TextBlock but stop_reason=end_turn (e.g. empty content list)
        must not emit the max_tokens diagnostic warning."""
        resp = _FakeResponse(content=[], stop_reason="end_turn")
        with caplog.at_level("WARNING", logger="services.mem0.cli_compat"):
            assert compat_mod.extract_text(resp) == ""
        assert not any("max_tokens" in r.message for r in caplog.records)
