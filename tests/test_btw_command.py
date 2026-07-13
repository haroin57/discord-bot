"""Tests for the /btw side-question feature.

Two layers:
- ``services/claude_api/side_question.py`` (``answer_side_question``): a faithful
  port of claude-cli's ``/btw`` — a non-persisting, **tool-less, single-turn**
  bounded agent turn. The load-bearing invariants are that it NEVER persists
  (no append_jsonl / set_session), advertises **no** tools (``tools_for_api=[]``,
  deny-all gate), runs exactly one turn (``max_turns=1``), and wraps the question
  in the upstream ``<system-reminder>`` framing.
- ``bot_commands/btw.py`` (``/btw`` command): admin gate, ephemeral replies,
  "+N earlier /btw" counter.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.claude_api.side_question as sq
import services.session as session_mod
import bot_commands.btw as btw

CHANNEL = 10**15


# --------------------------------------------------------------------------- #
# Fakes shared by the service-layer tests
# --------------------------------------------------------------------------- #
class _FakeWatchdog:
    def __init__(self, channel_id, *a, **k):
        self.channel_id = channel_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def wired(monkeypatch):
    """Patch side_question's collaborators; return a dict capturing loop kwargs."""
    captured: dict = {}

    monkeypatch.setattr(
        sq, "load_api_replay_channel",
        lambda cid: (captured.get("prior", []), "", ""),
    )
    monkeypatch.setattr(sq, "StallWatchdog", _FakeWatchdog)

    async def _fake_loop(prompt, channel_id, **kw):
        captured["prompt"] = prompt
        captured["history"] = kw["history"]
        captured["tools"] = kw["tools_for_api"]
        captured["max_turns"] = kw["max_turns"]
        captured["model"] = kw["model"]
        captured["can_use_tool"] = kw["can_use_tool"]
        captured["active_mcp_keys"] = kw["active_mcp_keys"]
        return SimpleNamespace(
            text=captured.get("answer", "an answer"),
            usage_total={
                "input_tokens": 10,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 0,
                "output_tokens": 5,
            },
            paused_reason=captured.get("paused_reason"),
        )

    monkeypatch.setattr(sq, "run_agent_loop", AsyncMock(side_effect=_fake_loop))
    return captured


# --------------------------------------------------------------------------- #
# Service layer: answer_side_question
# --------------------------------------------------------------------------- #
class TestAnswerSideQuestion:
    async def test_does_not_persist(self, wired, monkeypatch):
        """Core invariant: no JSONL / session writes anywhere in the path."""
        spy_append = AsyncMock()
        spy_set = AsyncMock()
        monkeypatch.setattr(session_mod, "append_jsonl", spy_append)
        monkeypatch.setattr(session_mod, "set_session", spy_set)

        res = await sq.answer_side_question(CHANNEL, "何時ですか", object())

        assert res.answer == "an answer"
        assert res.output_tokens == 5
        assert res.input_tokens == 13  # 10 + 3 + 0
        spy_append.assert_not_called()
        spy_set.assert_not_called()

    async def test_forks_current_context(self, wired):
        prior = [
            {"role": "user", "content": "前の質問"},
            {"role": "assistant", "content": "前の答え"},
        ]
        wired["prior"] = prior
        await sq.answer_side_question(CHANNEL, "ついでの質問", object())
        # run_agent_loop appends the prompt itself, so the history it receives
        # is exactly the prior context (order preserved), and the prompt carries
        # the side question.
        assert wired["history"] == prior
        assert "ついでの質問" in wired["prompt"]

    async def test_no_tools_advertised(self, wired):
        """Upstream parity: /btw runs tool-less (empty tools_for_api)."""
        await sq.answer_side_question(CHANNEL, "q", object())
        assert wired["tools"] == []
        assert wired["active_mcp_keys"] == []

    async def test_gate_denies_every_tool(self, wired):
        """The can_use_tool gate refuses all tools (defense in depth)."""
        await sq.answer_side_question(CHANNEL, "q", object())
        gate = wired["can_use_tool"]
        for name in ("Read", "Write", "Bash", "mcp__mem0__add_memories"):
            allowed, reason = gate(name, {})
            assert allowed is False
            assert "cannot use tools" in reason

    async def test_system_reminder_wraps_question(self, wired):
        """The upstream side-question system-reminder frames the prompt."""
        await sq.answer_side_question(CHANNEL, "元号は", object())
        prompt = wired["prompt"]
        assert prompt.startswith("<system-reminder>")
        assert "NO tools available" in prompt
        assert "single response" in prompt
        assert "元号は" in prompt

    async def test_author_label_included(self, wired):
        await sq.answer_side_question(CHANNEL, "q", object(), author="先輩")
        assert "[発言者: 先輩]" in wired["prompt"]

    async def test_max_turns_one(self, wired):
        """Upstream parity: exactly one turn, no tool round-trips."""
        await sq.answer_side_question(CHANNEL, "q", object())
        assert wired["max_turns"] == 1

    async def test_no_history(self, wired):
        wired["prior"] = []
        await sq.answer_side_question(CHANNEL, "q", object())
        assert wired["history"] == []

    async def test_empty_answer(self, wired):
        wired["answer"] = ""
        res = await sq.answer_side_question(CHANNEL, "q", object())
        assert res.answer == ""


# --------------------------------------------------------------------------- #
# Command layer: /btw
# --------------------------------------------------------------------------- #
class _FakeTree:
    def __init__(self):
        self.cmds: dict = {}

    def command(self, *, name, description=None, **kw):
        def deco(fn):
            self.cmds[name] = fn
            return fn
        return deco


def _make_interaction(*, channel_id=CHANNEL, uid=573489206240870410):
    resp = SimpleNamespace(
        send_message=AsyncMock(),
        defer=AsyncMock(),
    )
    followup = SimpleNamespace(send=AsyncMock())
    user = SimpleNamespace(id=uid, display_name="先輩", name="haroin")
    return SimpleNamespace(
        response=resp, followup=followup, user=user, channel_id=channel_id,
    )


def _register(is_admin=True):
    tree = _FakeTree()
    deps = SimpleNamespace(
        is_admin=lambda u: is_admin,
        model_overrides={},
        effort_overrides={},
    )
    btw.register(tree, deps)
    return tree.cmds["btw"]


@pytest.fixture(autouse=True)
def _reset_counts():
    btw._btw_counts.clear()
    yield
    btw._btw_counts.clear()


class TestBtwCommand:
    async def test_non_admin_blocked(self, monkeypatch):
        spy = AsyncMock()
        monkeypatch.setattr(
            "services.claude_api.side_question.answer_side_question", spy,
        )
        cmd = _register(is_admin=False)
        interaction = _make_interaction()
        await cmd(interaction, "質問")
        interaction.response.send_message.assert_awaited_once_with(
            "admin only", ephemeral=True,
        )
        spy.assert_not_called()

    async def test_outside_channel(self):
        cmd = _register()
        interaction = _make_interaction(channel_id=None)
        await cmd(interaction, "質問")
        args, kwargs = interaction.response.send_message.call_args
        assert "チャンネル外" in args[0]
        assert kwargs["ephemeral"] is True

    async def test_empty_question(self):
        cmd = _register()
        interaction = _make_interaction()
        await cmd(interaction, "   ")
        args, kwargs = interaction.response.send_message.call_args
        assert "空" in args[0]

    async def test_replies_ephemeral(self, monkeypatch):
        monkeypatch.setattr(
            "services.claude_api.client_factory.make_api_client",
            AsyncMock(return_value=object()),
        )
        monkeypatch.setattr(
            "services.claude_api.side_question.answer_side_question",
            AsyncMock(return_value=sq.SideQuestionResult(
                answer="答えです", input_tokens=13, output_tokens=5,
                duration_ms=1200, prior_turns=2, paused_reason=None,
            )),
        )
        cmd = _register()
        interaction = _make_interaction()
        await cmd(interaction, "質問")
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        assert interaction.followup.send.await_count >= 1
        for call in interaction.followup.send.await_args_list:
            assert call.kwargs["ephemeral"] is True

    async def test_use_count_footer(self, monkeypatch):
        monkeypatch.setattr(
            "services.claude_api.client_factory.make_api_client",
            AsyncMock(return_value=object()),
        )
        monkeypatch.setattr(
            "services.claude_api.side_question.answer_side_question",
            AsyncMock(return_value=sq.SideQuestionResult(
                answer="答え", input_tokens=1, output_tokens=1,
                duration_ms=100, prior_turns=0, paused_reason=None,
            )),
        )
        cmd = _register()
        # first call → "+0 earlier", second → "+1 earlier"
        await cmd(_make_interaction(), "q1")
        interaction2 = _make_interaction()
        await cmd(interaction2, "q2")
        last_text = interaction2.followup.send.await_args_list[-1].args[0]
        assert "+1 earlier /btw" in last_text

    async def test_empty_answer_reports(self, monkeypatch):
        monkeypatch.setattr(
            "services.claude_api.client_factory.make_api_client",
            AsyncMock(return_value=object()),
        )
        monkeypatch.setattr(
            "services.claude_api.side_question.answer_side_question",
            AsyncMock(return_value=sq.SideQuestionResult(
                answer="   ", input_tokens=1, output_tokens=0,
                duration_ms=100, prior_turns=0, paused_reason=None,
            )),
        )
        cmd = _register()
        interaction = _make_interaction()
        await cmd(interaction, "q")
        last_text = interaction.followup.send.await_args_list[-1].args[0]
        assert "応答が空" in last_text


# --------------------------------------------------------------------------- #
# Chunking helper
# --------------------------------------------------------------------------- #
class TestChunk:
    def test_short_text_single_chunk(self):
        assert btw._chunk("hello") == ["hello"]

    def test_splits_on_paragraphs(self):
        a, b = "x" * 1500, "y" * 1500
        chunks = btw._chunk(f"{a}\n\n{b}")
        assert len(chunks) == 2
        assert chunks[0].startswith("x") and chunks[1].startswith("y")

    def test_hard_split_long_line(self):
        chunks = btw._chunk("z" * 5000)
        assert all(len(c) <= btw._CHUNK_LIMIT for c in chunks)
        assert "".join(chunks) == "z" * 5000
