"""Auxiliary Codex helpers walk a model chain when a model is retired."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.codex_app_server import auxiliary
from services.codex_app_server.session import CodexModelUnavailableError, CodexTurnError


class _FakeSession:
    calls: list[str] = []
    unavailable: set[str] = set()
    other_error: set[str] = set()

    def __init__(self, **_kwargs):
        pass

    async def run_turn(self, prompt, *, model, **_kwargs):
        type(self).calls.append(model)
        if model in self.unavailable:
            raise CodexModelUnavailableError(f"Codex model unavailable: {model}")
        if model in self.other_error:
            raise CodexTurnError("upstream exploded")
        return SimpleNamespace(final_text="ok", model=model)

    async def close(self):
        return None


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    import config
    _FakeSession.calls = []
    _FakeSession.unavailable = set()
    _FakeSession.other_error = set()
    monkeypatch.setattr(auxiliary, "CodexSession", _FakeSession)
    monkeypatch.setattr(auxiliary, "isolated_args", lambda: [])
    monkeypatch.setattr(auxiliary, "_sticky_model", None)
    monkeypatch.setattr(config, "CODEX_APP_SERVER_MODEL", "gpt-6-astra")
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.delenv("CODEX_AUX_MODEL", raising=False)
    monkeypatch.setenv("CODEX_AUX_FALLBACK_MODELS", "gpt-5.6-terra,gpt-5.6-luna")


@pytest.mark.asyncio
async def test_retired_model_falls_back_and_sticks():
    _FakeSession.unavailable = {"gpt-6-astra"}

    first = await auxiliary.run_task(prompt="p", instructions="i")
    second = await auxiliary.run_task(prompt="p", instructions="i")

    assert first.model == second.model == "gpt-5.6-terra"
    # The retired model is not retried once a working one is pinned.
    assert _FakeSession.calls == ["gpt-6-astra", "gpt-5.6-terra", "gpt-5.6-terra"]


@pytest.mark.asyncio
async def test_non_model_errors_do_not_fall_back():
    _FakeSession.other_error = {"gpt-6-astra"}

    with pytest.raises(CodexTurnError, match="exploded"):
        await auxiliary.run_task(prompt="p", instructions="i")

    assert _FakeSession.calls == ["gpt-6-astra"]


@pytest.mark.asyncio
async def test_all_models_unavailable_raises_last():
    _FakeSession.unavailable = {"gpt-6-astra", "gpt-5.6-terra", "gpt-5.6-luna"}

    with pytest.raises(CodexModelUnavailableError, match="gpt-5.6-luna"):
        await auxiliary.run_task(prompt="p", instructions="i")


def test_candidates_dedupe_and_honor_override(monkeypatch):
    monkeypatch.setenv("CODEX_AUX_MODEL", "gpt-5.6-terra")
    assert auxiliary.model_candidates() == ["gpt-5.6-terra", "gpt-5.6-luna"]
