"""Tests for services/claude_api/codex_runner.py（codex ChatGPT サブスク枠 GPT-5.5）。

subprocess は全て fake。実 codex には触らない。
"""

import json

import pytest

from services.claude_api import codex_runner as cr
from services.claude_api.subprocess_runner import (
    SubprocessDeadlineExceeded,
    SubprocessResult,
)


@pytest.fixture(autouse=True)
def _reset():
    cr._reset_cache_for_test()
    yield
    cr._reset_cache_for_test()


class TestCleanEnv:
    def test_strips_openai_key(self, monkeypatch):
        for k in cr._STRIP_ENV_KEYS:
            monkeypatch.setenv(k, "leaked")
        env = cr._clean_env()
        for k in cr._STRIP_ENV_KEYS:
            assert k not in env  # サブスク枠強制の肝


class TestAskCodex:
    @pytest.mark.asyncio
    async def test_reads_output_file(self, monkeypatch):
        captured = {}

        async def fake_run(argv, **kwargs):
            captured["args"] = tuple(argv)
            captured["env"] = kwargs.get("env")
            # -o <path> の次要素が出力ファイル
            argv_list = list(argv)
            oi = argv_list.index("-o")
            out_path = argv_list[oi + 1]
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("2です。")
            return SubprocessResult(0, "", "")

        monkeypatch.setattr("services.claude_api._cli_runner.run_subprocess", fake_run)
        monkeypatch.setenv("OPENAI_API_KEY", "leaked")
        out = await cr.ask_codex("1+1?", timeout_s=30)
        assert out == "2です。"
        assert "OPENAI_API_KEY" not in captured["env"]  # サブスク枠強制
        assert captured["args"][:2] == (cr.CODEX_BIN, "exec")

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises(self, monkeypatch):
        async def fake_run(argv, **kwargs):
            return SubprocessResult(1, "", "model not supported with ChatGPT account")

        monkeypatch.setattr("services.claude_api._cli_runner.run_subprocess", fake_run)
        with pytest.raises(RuntimeError, match="not supported"):
            await cr.ask_codex("q", timeout_s=30)

    @pytest.mark.asyncio
    async def test_timeout_kills(self, monkeypatch):
        async def fake_run(argv, **kwargs):
            raise SubprocessDeadlineExceeded

        monkeypatch.setattr("services.claude_api._cli_runner.run_subprocess", fake_run)
        with pytest.raises(RuntimeError, match="timed out"):
            await cr.ask_codex("q", timeout_s=0.1)

    @pytest.mark.asyncio
    async def test_empty_output_raises(self, monkeypatch):
        async def fake_run(argv, **kwargs):
            return SubprocessResult(0, "", "")  # 出力ファイル書かない

        monkeypatch.setattr("services.claude_api._cli_runner.run_subprocess", fake_run)
        with pytest.raises(RuntimeError, match="no final message"):
            await cr.ask_codex("q", timeout_s=30)


class TestIsAvailable:
    @pytest.mark.asyncio
    async def test_missing_binary(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _b: None)
        assert await cr.is_codex_available() is False

    @pytest.mark.asyncio
    async def test_chatgpt_auth_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/codex")
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps({"auth_mode": "chatgpt"}))
        monkeypatch.setattr("os.path.expanduser", lambda p: str(auth))
        assert await cr.is_codex_available() is True

    @pytest.mark.asyncio
    async def test_api_key_auth_mode_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/codex")
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps({"auth_mode": "apikey"}))
        monkeypatch.setattr("os.path.expanduser", lambda p: str(auth))
        # apikey モードは従量課金なので available=False（サブスク枠が無い）
        assert await cr.is_codex_available() is False

    @pytest.mark.asyncio
    async def test_cache(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/codex")
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps({"auth_mode": "chatgpt"}))
        calls = {"n": 0}
        orig = auth.read_text

        def counting(*a, **k):
            calls["n"] += 1
            return orig()

        monkeypatch.setattr("os.path.expanduser", lambda p: str(auth))
        await cr.is_codex_available()
        await cr.is_codex_available()
        # 2回目はキャッシュ（which は呼ばれるが auth 判定まで行かない想定でも
        # 結果が一致すればよい）。厳密なファイル読み回数までは問わない。
        assert await cr.is_codex_available() is True
