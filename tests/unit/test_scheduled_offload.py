"""One-shot execution preserves business-day and independent tracking behavior."""

from unittest.mock import AsyncMock

import pytest

from services import vi_signal_scheduler as vi


async def test_once_skips_non_business_days(monkeypatch):
    monkeypatch.setattr(vi, "is_business_day", lambda _: False)
    check, forward = AsyncMock(), AsyncMock()
    monkeypatch.setattr(vi, "run_vi_check", check)
    monkeypatch.setattr(vi, "run_fwd_record", forward)
    assert await vi.run_once() == 0
    check.assert_not_called()
    forward.assert_not_called()


@pytest.mark.parametrize("error", [False, True])
async def test_once_records_forward_prices_even_when_signal_check_fails(monkeypatch, error):
    monkeypatch.setattr(vi, "is_business_day", lambda _: True)
    check = AsyncMock(side_effect=RuntimeError("failed")) if error else AsyncMock(return_value=7)
    forward = AsyncMock(return_value=0)
    monkeypatch.setattr(vi, "run_vi_check", check)
    monkeypatch.setattr(vi, "run_fwd_record", forward)
    assert await vi.run_once() != 0
    forward.assert_awaited_once()
