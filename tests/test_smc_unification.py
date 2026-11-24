"""Tests for SMC V3 Single-Authority Unification & SMA20/50 Placeholder Lockout."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from backend.routes import multi_broker as mb_routes
from smc_futures.engine.multi_broker_engine import (
    BrokerType,
    Candle,
    MultiBrokerConfig,
    MultiBrokerEngine,
    Signal,
    SignalType,
)


def _make_trending_candles(n: int = 60):
    # Candles that would trigger SMA20/50 crossover if placeholder were active
    candles = []
    for i in range(n - 2):
        p = 100.0 + i * 0.5
        candles.append(Candle(timestamp=1700000000000 + i * 60000, open=p, high=p + 1, low=p - 1, close=p, volume=100))
    # Cross below then above SMA20 while SMA20 > SMA50
    candles.append(Candle(timestamp=1700000000000 + (n - 2) * 60000, open=120.0, high=121.0, low=115.0, close=116.0, volume=100))
    candles.append(Candle(timestamp=1700000000000 + (n - 1) * 60000, open=116.0, high=135.0, low=116.0, close=134.0, volume=100))
    return candles


def test_compute_signals_blocks_placeholder_sma_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_PLACEHOLDER_SMA_STRATEGY", raising=False)
    cfg = MultiBrokerConfig(bitget_enabled=False, kraken_enabled=False, mt5_enabled=True)
    engine = MultiBrokerEngine(config=cfg)  # strategy_callback=None
    candles = _make_trending_candles(60)
    assert engine.compute_signals("BTCUSDT", candles, BrokerType.MT5) is None


@pytest.mark.asyncio
async def test_run_strategy_loop_refuses_to_start_without_strategy_callback(monkeypatch):
    monkeypatch.delenv("ALLOW_PLACEHOLDER_SMA_STRATEGY", raising=False)
    cfg = MultiBrokerConfig(bitget_enabled=False, kraken_enabled=False, mt5_enabled=True)
    engine = MultiBrokerEngine(config=cfg)
    with pytest.raises(RuntimeError, match="MULTI_BROKER_STRATEGY_CALLBACK_REQUIRED"):
        await engine.run_strategy_loop()
    assert engine._running is False


@pytest.mark.asyncio
async def test_api_multi_start_enables_smc_v3_without_starting_sma_loop(monkeypatch):
    monkeypatch.delenv("ALLOW_PLACEHOLDER_SMA_STRATEGY", raising=False)
    cfg = MultiBrokerConfig(bitget_enabled=False, kraken_enabled=False, mt5_enabled=True)
    engine = MultiBrokerEngine(config=cfg)
    monkeypatch.setattr(mb_routes, "_engine", engine)
    res = await mb_routes.start_trading_loop()
    assert res["status"] == "started"
    assert res["engine"] == "SMC_V3"
    # Legacy SMA20/50 loop must NEVER be started
    assert engine._running is False


@pytest.mark.asyncio
async def test_dispatch_canonical_smc_signal_routes_to_mt5_and_deduplicates(monkeypatch):
    monkeypatch.setenv("TRADE_COOLDOWN_SEC", "0")
    mock_mt5 = MagicMock()
    mock_mt5.is_connected = True
    mock_mt5.get_account_info = AsyncMock(
        return_value={"available": 100000.0, "total": 100000.0, "currency": "USD", "margin_used": 0.0, "unrealized_pnl": 0.0}
    )
    mock_mt5.place_order = AsyncMock(
        return_value={"success": True, "symbol": "BTCUSD", "order_id": "MT5_SMC_1", "price": 61000.0, "filled_qty": 0.10}
    )

    cfg = MultiBrokerConfig(bitget_enabled=False, kraken_enabled=False, mt5_enabled=True)
    engine = MultiBrokerEngine(config=cfg, mt5_executor=mock_mt5)

    sig = Signal(
        symbol="BTCUSDT",
        signal_type=SignalType.LONG,
        entry_price=61000.0,
        stop_loss=60500.0,
        take_profit=62000.0,
        leverage=3,
        reason="SMC_V3:OB_SWEEP(TREND)",
        confidence=0.85,
    )

    # First dispatch executes on MT5
    out1 = await engine.dispatch_canonical_smc_signal(sig, signal_id="SMCV3_BTC_1", brokers=[BrokerType.MT5])
    assert out1 == {BrokerType.MT5: "dispatched"}
    assert mock_mt5.place_order.await_count == 1

    # Duplicate dispatch with SAME signal_id is strictly skipped (0 additional orders)
    out2 = await engine.dispatch_canonical_smc_signal(sig, signal_id="SMCV3_BTC_1", brokers=[BrokerType.MT5])
    assert out2 == {}
    assert mock_mt5.place_order.await_count == 1
