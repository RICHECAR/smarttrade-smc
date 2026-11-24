"""Tests for MT5 Forex business-day hours and mandatory pre-close auto-flatten guard."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.execution.mt5_executor import (
    MT5Executor,
    is_forex_or_metal_symbol,
    is_mt5_forex_market_open,
)
from tests.test_mt5_integration import _make_fake_mt5


def test_forex_and_metal_symbol_classification():
    assert is_forex_or_metal_symbol("EURUSD.s") is True
    assert is_forex_or_metal_symbol("GBPUSD") is True
    assert is_forex_or_metal_symbol("USDJPY.s") is True
    assert is_forex_or_metal_symbol("XAUUSD.s") is True
    assert is_forex_or_metal_symbol("BTCUSD") is False
    assert is_forex_or_metal_symbol("ETHUSD") is False
    assert is_forex_or_metal_symbol("SOLUSD") is False


def test_business_day_market_hours_schedule(monkeypatch):
    monkeypatch.setenv("MT5_FOREX_FRIDAY_CLOSE_UTC_HOUR", "20")
    monkeypatch.setenv("MT5_FOREX_FRIDAY_CLOSE_UTC_MIN", "30")
    monkeypatch.setenv("MT5_FOREX_CLOSE_DAILY_ROLLOVER", "true")

    # Wednesday 14:00 UTC -> OPEN
    ok, _ = is_mt5_forex_market_open(datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc))
    assert ok is True

    # Friday 20:29 UTC -> OPEN
    ok_f1, _ = is_mt5_forex_market_open(datetime(2026, 11, 20, 20, 29, tzinfo=timezone.utc))
    assert ok_f1 is True

    # Friday 20:30 UTC -> CLOSED (30 min pre-close cutoff)
    ok_f2, r_f2 = is_mt5_forex_market_open(datetime(2026, 11, 20, 20, 30, tzinfo=timezone.utc))
    assert ok_f2 is False and "FRIDAY_PRE_CLOSE_CUTOFF" in r_f2

    # Saturday & Sunday -> CLOSED
    ok_sat, r_sat = is_mt5_forex_market_open(datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc))
    ok_sun, r_sun = is_mt5_forex_market_open(datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc))
    assert ok_sat is False and "WEEKEND_CLOSED" in r_sat
    assert ok_sun is False and "WEEKEND_CLOSED" in r_sun

    # Daily rollover Wednesday 21:05 UTC -> CLOSED
    ok_roll, r_roll = is_mt5_forex_market_open(datetime(2026, 11, 18, 21, 5, tzinfo=timezone.utc))
    assert ok_roll is False and "DAILY_ROLLOVER_CLOSURE" in r_roll


@pytest.mark.asyncio
async def test_forex_open_blocked_during_weekend_closure(monkeypatch):
    monkeypatch.setenv("MT5_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("MT5_ALLOW_LIVE", "false")
    fake = _make_fake_mt5(trade_mode=0, contract_size=100000.0)
    ex = MT5Executor(login=700222811, password="x", server="PUPrime-Demo")

    with patch("backend.execution.mt5_executor._import_mt5", return_value=fake), \
         patch("backend.execution.mt5_executor.is_mt5_forex_market_open", return_value=(False, "WEEKEND_CLOSED")):
        await ex.connect()
        res = await ex.place_order("EURUSD.s", "buy", 0.01, quantity_in_lots=True)

    assert res["success"] is False
    assert res["code"] == "MT5_FOREX_MARKET_CLOSED"
    fake.order_send.assert_not_called()


@pytest.mark.asyncio
async def test_enforce_market_close_flatten_closes_only_forex_on_friday_cutoff(monkeypatch):
    monkeypatch.setenv("MT5_FOREX_FRIDAY_CLOSE_UTC_HOUR", "20")
    monkeypatch.setenv("MT5_FOREX_FRIDAY_CLOSE_UTC_MIN", "30")
    monkeypatch.setenv("MT5_CLOSE_ALL_ON_CLOSURE", "false")

    ex = MT5Executor(login=700222811, password="x", server="PUPrime-Demo")
    ex.get_positions = AsyncMock(
        return_value=[
            {"ticket": 101, "symbol": "EURUSD.s", "volume": 0.05},
            {"ticket": 102, "symbol": "XAUUSD.s", "volume": 0.02},
            {"ticket": 103, "symbol": "BTCUSD", "volume": 0.01},
        ]
    )
    ex.close_position = AsyncMock(return_value={"success": True, "order_id": "CL_OK"})

    # Friday 20:35 UTC -> auto-flattens EURUSD.s and XAUUSD.s, leaves 24/7 BTCUSD untouched
    fri_close = datetime(2026, 11, 20, 20, 35, tzinfo=timezone.utc)
    report = await ex.enforce_market_close_flatten(now_utc=fri_close)
    assert report["action"] == "flatten"
    assert len(report["closed"]) == 2
    closed_syms = {c["symbol"] for c in report["closed"]}
    assert closed_syms == {"EURUSD.s", "XAUUSD.s"}
    assert ex.close_position.await_count == 2


def test_evaluate_forex_smc_signal_detects_bullish_sweep_on_usdjpy():
    from backend.execution.mt5_forex_smc_loop import evaluate_forex_smc_signal

    # HTF 4h bullish uptrend
    htf = [
        {"timestamp": 1700000000000 + i * 14400000, "open": 150.0 + i * 0.1, "high": 150.2 + i * 0.1, "low": 149.9 + i * 0.1, "close": 150.1 + i * 0.1}
        for i in range(60)
    ]
    # MTF 15m with prior low at 158.80, then sweep to 158.65 and close back above 158.95
    mtf = [
        {"timestamp": 1700000000000 + i * 900000, "open": 159.0, "high": 159.2, "low": 158.85, "close": 159.05}
        for i in range(35)
    ]
    mtf[-2] = {"timestamp": 1700000000000 + 35 * 900000, "open": 158.95, "high": 159.00, "low": 158.65, "close": 158.78}
    mtf[-1] = {"timestamp": 1700000000000 + 36 * 900000, "open": 158.78, "high": 159.10, "low": 158.75, "close": 159.02}

    # LTF 5m bullish confirmation closing above previous high
    ltf = [
        {"timestamp": 1700000000000 + i * 300000, "open": 158.90, "high": 159.00, "low": 158.85, "close": 158.95}
        for i in range(18)
    ]
    ltf[-2] = {"timestamp": 1700000000000 + 18 * 300000, "open": 158.92, "high": 158.98, "low": 158.80, "close": 158.94}
    ltf[-1] = {"timestamp": 1700000000000 + 19 * 300000, "open": 158.94, "high": 159.12, "low": 158.92, "close": 159.08}

    sig = evaluate_forex_smc_signal("USDJPY.s", htf, mtf, ltf, min_rr=1.5)
    assert sig is not None
    assert sig["symbol"] == "USDJPY.s"
    assert sig["direction"] == "long"
    assert sig["setup"] == "SMC_SWEEP_LONG"
    assert sig["stop_loss"] < sig["entry"] < sig["take_profit"]
    assert sig["rr_ratio"] >= 1.5
