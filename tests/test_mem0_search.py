"""Unit tests for M2 security fixes in services/mem0/search.py."""

import asyncio
import unittest.mock as mock


def test_rerank_candidates_wrapped_as_untrusted():
    """M2: _rerank_memories must wrap each candidate in <untrusted_candidate> tags."""
    from services.mem0.search import _rerank_memories

    memories = [
        {"memory": "先輩はTOEICの勉強をしている", "score": 0.9},
        {"memory": "先輩はRustが好き", "score": 0.8},
        {"memory": "先輩はアシタボの開発者", "score": 0.7},
        {"memory": "先輩は長野市在住", "score": 0.6},
        {"memory": "先輩はclannad好き", "score": 0.5},
        {"memory": "先輩はVPS運用中", "score": 0.4},
    ]

    captured_prompt: list[str] = []

    fake_response = mock.MagicMock()
    fake_response.content = [mock.MagicMock(text='{"top":[1,2,3]}')]

    async def fake_create(**kwargs):
        msg_content = kwargs["messages"][0]["content"]
        captured_prompt.append(msg_content)
        return fake_response

    async def run():
        with mock.patch("services.mem0.client.get_anthropic_client") as mock_get_client:
            fake_client = mock.AsyncMock()
            fake_client.messages.create = fake_create
            mock_get_client.return_value = fake_client
            return await _rerank_memories("TOEIC", memories, top_k=3)

    result = asyncio.run(run())

    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]
    assert "<untrusted_candidate>" in prompt
    assert "</untrusted_candidate>" in prompt


def test_rewrite_query_length_cap():
    """M2: _rewrite_query must cap query at 500 chars before format()."""
    from services.mem0.search import _rewrite_query

    long_query = "x" * 600

    fake_response = mock.MagicMock()
    fake_response.content = [mock.MagicMock(text='{"queries":["q1","q2"]}')]

    captured_prompt: list[str] = []

    async def fake_create(**kwargs):
        msg_content = kwargs["messages"][0]["content"]
        captured_prompt.append(msg_content)
        return fake_response

    async def run():
        with mock.patch("services.mem0.client.get_anthropic_client") as mock_get_client:
            fake_client = mock.AsyncMock()
            fake_client.messages.create = fake_create
            mock_get_client.return_value = fake_client
            return await _rewrite_query(long_query)

    result = asyncio.run(run())

    assert len(captured_prompt) == 1
    # The 600-char query should be capped to 500 in the prompt
    assert "x" * 501 not in captured_prompt[0]


def test_rerank_brace_injection_in_candidate():
    """M2: braces in candidate memory text must not cause KeyError in format()."""
    from services.mem0.search import _rerank_memories

    memories = [
        {"memory": "fact with {brace} injection", "score": 0.9},
        {"memory": "normal fact", "score": 0.8},
        {"memory": "another fact", "score": 0.7},
        {"memory": "yet another", "score": 0.6},
        {"memory": "fifth fact", "score": 0.5},
        {"memory": "sixth fact", "score": 0.4},
    ]

    fake_response = mock.MagicMock()
    fake_response.content = [mock.MagicMock(text='{"top":[1,2,3]}')]

    async def run():
        with mock.patch("services.mem0.client.get_anthropic_client") as mock_get_client:
            fake_client = mock.AsyncMock()
            fake_client.messages.create = mock.AsyncMock(return_value=fake_response)
            mock_get_client.return_value = fake_client
            # Must not raise KeyError
            return await _rerank_memories("{category}", memories, top_k=3)

    result = asyncio.run(run())
    assert isinstance(result, list)
