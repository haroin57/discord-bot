"""Tests for services/claude_api/agy_runner.py（agy OAuth サブスク枠経由 Gemini）。

subprocess は全て fake。実 agy には触らない。
"""

import json
import os

import pytest

from services.claude_api import agy_runner as ar
from services.claude_api.subprocess_runner import (
    SubprocessDeadlineExceeded,
    SubprocessResult,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    ar._reset_cache_for_test()
    yield
    ar._reset_cache_for_test()


class TestCleanEnv:
    def test_strips_api_keys(self, monkeypatch):
        for k in ar._STRIP_ENV_KEYS:
            monkeypatch.setenv(k, "leaked")
        env = ar._clean_env()
        for k in ar._STRIP_ENV_KEYS:
            assert k not in env  # OAuth 強制の肝


class TestAskAgy:
    @pytest.mark.asyncio
    async def test_stdout_answer(self, monkeypatch):
        captured = {}

        async def fake_run(argv, **kwargs):
            captured["args"] = tuple(argv)
            captured["env"] = kwargs.get("env")
            return SubprocessResult(0, "4です", "")

        monkeypatch.setattr("services.claude_api._cli_runner.run_subprocess", fake_run)
        monkeypatch.setenv("GEMINI_API_KEY", "leaked")
        out = await ar.ask_agy("1+1?", timeout_s=30)
        assert out == "4です"
        # env から API キーが除去されている（OAuth 強制）
        assert "GEMINI_API_KEY" not in captured["env"]
        assert captured["args"][:2] == (ar.AGY_BIN, "-p")

    @pytest.mark.asyncio
    async def test_stdout_empty_falls_back_to_transcript(self, monkeypatch, tmp_path):
        async def fake_run(argv, **kwargs):
            return SubprocessResult(0, "", "")

        monkeypatch.setattr("services.claude_api._cli_runner.run_subprocess", fake_run)

        # transcript ツリーを組む
        conv = tmp_path / "conv-1" / ".system_generated" / "logs"
        conv.mkdir(parents=True)
        jsonl = conv / "transcript.jsonl"
        jsonl.write_text(
            "\n".join(
                [
                    json.dumps({"source": "USER", "type": "PROMPT", "text": "1+1?"}),
                    json.dumps(
                        {
                            "source": "MODEL",
                            "status": "DONE",
                            "type": "PLANNER_RESPONSE",
                            "text": "答えは4です",
                        }
                    ),
                ]
            )
        )
        monkeypatch.setattr(ar, "AGY_TRANSCRIPT_ROOT", str(tmp_path))
        out = await ar.ask_agy("1+1?", timeout_s=30)
        assert out == "答えは4です"

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises(self, monkeypatch):
        async def fake_run(argv, **kwargs):
            return SubprocessResult(1, "", "auth expired")

        monkeypatch.setattr("services.claude_api._cli_runner.run_subprocess", fake_run)
        with pytest.raises(RuntimeError, match="auth expired"):
            await ar.ask_agy("q", timeout_s=30)

    @pytest.mark.asyncio
    async def test_timeout_kills_proc(self, monkeypatch):
        async def fake_run(argv, **kwargs):
            raise SubprocessDeadlineExceeded

        monkeypatch.setattr("services.claude_api._cli_runner.run_subprocess", fake_run)
        with pytest.raises(RuntimeError, match="timed out"):
            await ar.ask_agy("q", timeout_s=0.1)

    @pytest.mark.asyncio
    async def test_no_stdout_no_transcript_raises(self, monkeypatch, tmp_path):
        async def fake_run(argv, **kwargs):
            return SubprocessResult(0, "", "")

        monkeypatch.setattr("services.claude_api._cli_runner.run_subprocess", fake_run)
        monkeypatch.setattr(ar, "AGY_TRANSCRIPT_ROOT", str(tmp_path / "nope"))
        with pytest.raises(RuntimeError, match="no stdout"):
            await ar.ask_agy("q", timeout_s=30)


class TestIsAvailable:
    @pytest.mark.asyncio
    async def test_missing_binary(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _b: None)
        assert await ar.is_agy_available() is False

    @pytest.mark.asyncio
    async def test_present_and_version_ok(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/agy")

        async def fake_run(argv, **kwargs):
            return SubprocessResult(0, "agy 1.0.8", "")

        monkeypatch.setattr("services.claude_api._cli_runner.run_subprocess", fake_run)
        assert await ar.is_agy_available() is True

    @pytest.mark.asyncio
    async def test_cache_avoids_repeat(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/agy")
        calls = {"n": 0}

        async def fake_run(argv, **kwargs):
            calls["n"] += 1
            return SubprocessResult(0, "", "")

        monkeypatch.setattr("services.claude_api._cli_runner.run_subprocess", fake_run)
        await ar.is_agy_available()
        await ar.is_agy_available()
        assert calls["n"] == 1  # 2回目はキャッシュ


class TestParseTranscript:
    def test_picks_last_done_response(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "source": "MODEL",
                            "status": "DONE",
                            "type": "PLANNER_RESPONSE",
                            "text": "古い",
                        }
                    ),
                    json.dumps(
                        {
                            "source": "MODEL",
                            "status": "WORKING",
                            "type": "PLANNER_RESPONSE",
                            "text": "途中",
                        }
                    ),
                    json.dumps(
                        {
                            "source": "MODEL",
                            "status": "DONE",
                            "type": "PLANNER_RESPONSE",
                            "text": "最終",
                        }
                    ),
                ]
            )
        )
        assert ar._parse_transcript_file(str(p)) == "最終"

    def test_missing_file_returns_none(self, tmp_path):
        assert ar._parse_transcript_file(str(tmp_path / "nope.jsonl")) is None
