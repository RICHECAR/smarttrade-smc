"""Tests for MetaTrader 5 (MT5) executor and MultiBrokerEngine integration."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.execution.broker_router import get_default_brokers, is_mt5_enabled
from backend.execution.mt5_executor import MT5Executor
from smc_futures.engine.multi_broker_engine import (
    BrokerType,
    MultiBrokerConfig,
    MultiBrokerEngine,
    OrderSide,
    Signal,
    SignalType,
)


def _make_fake_mt5(trade_mode: int = 0, contract_size: float = 1.0):
    fake = types.SimpleNamespace()
    fake.TIMEFRAME_M1 = 1
    fake.TIMEFRAME_M3 = 3
    fake.TIMEFRAME_M5 = 5
    fake.TIMEFRAME_M15 = 15
    fake.TIMEFRAME_M30 = 30
    fake.TIMEFRAME_H1 = 16385
    fake.TIMEFRAME_H2 = 16386
    fake.TIMEFRAME_H4 = 16388
    fake.TIMEFRAME_H6 = 16390
    fake.TIMEFRAME_H12 = 16396
    fake.TIMEFRAME_D1 = 16408
    fake.TIMEFRAME_W1 = 32769
    fake.ORDER_FILLING_FOK = 0
    fake.ORDER_FILLING_IOC = 1
    fake.ORDER_FILLING_RETURN = 2
    fake.ORDER_TYPE_BUY = 0
    fake.ORDER_TYPE_SELL = 1
    fake.ORDER_TYPE_BUY_LIMIT = 2
    fake.ORDER_TYPE_SELL_LIMIT = 3
    fake.ORDER_TIME_GTC = 0
    fake.TRADE_ACTION_DEAL = 1
    fake.TRADE_ACTION_PENDING = 5
    fake.POSITION_TYPE_BUY = 0
    fake.POSITION_TYPE_SELL = 1
    fake.TRADE_RETCODE_DONE = 10009
    fake.TRADE_RETCODE_PLACED = 10008
    fake.TRADE_RETCODE_DONE_PARTIAL = 10010

    fake.initialize = MagicMock(return_value=True)
    fake.shutdown = MagicMock(return_value=True)
    fake.last_error = MagicMock(return_value=(1, "Success"))
    fake.account_info = MagicMock(
        return_value=types.SimpleNamespace(
            login=700222811,
            server="PUPrime-Demo",
            trade_mode=trade_mode,
            currency="USD",
            balance=100000.0,
            equity=100000.0,
            margin=0.0,
            margin_free=100000.0,
            profit=0.0,
            leverage=500,
            trade_allowed=True,
        )
    )
    fake.terminal_info = MagicMock(
        return_value=types.SimpleNamespace(connected=True, trade_allowed=True)
    )
    fake.symbol_info = MagicMock(
        return_value=types.SimpleNamespace(
            visible=True,
            digits=2,
            point=0.01,
            trade_contract_size=contract_size,
            volume_min=0.01,
            volume_max=50.0,
            volume_step=0.01,
            filling_mode=2,
        )
    )
    fake.symbol_select = MagicMock(return_value=True)
    fake.symbol_info_tick = MagicMock(
        return_value=types.SimpleNamespace(bid=1718.0, ask=1720.0, last=1719.0, time_msc=1700000000000)
    )
    fake.positions_get = MagicMock(return_value=())
    fake.order_send = MagicMock(
        return_value=types.SimpleNamespace(
            retcode=10009,
            order=999001,
            deal=888001,
            price=1720.0,
            volume=0.05,
            comment="Request executed",
        )
    )
    return fake


def test_symbol_normalization():
    ex = MT5Executor(login=700222811, password="x", server="PUPrime-Demo")
    assert ex.normalize_symbol("BTCUSDT") == "BTCUSD"
    assert ex.normalize_symbol("PF_XBTUSD") == "BTCUSD"
    assert ex.normalize_symbol("PI_ETHUSD") == "ETHUSD"
    assert ex.normalize_symbol("SOLUSDT") == "SOLUSD"
    assert ex.normalize_symbol("XRPUSDT") == "XRPUSD"
    assert ex.normalize_symbol("XAUUSD.s") == "XAUUSD.s"


def test_round_volume():
    assert MT5Executor._round_volume(0.057, 0.01, 50.0, 0.01) == 0.05
    assert MT5Executor._round_volume(0.005, 0.01, 50.0, 0.01) == 0.0
    assert MT5Executor._round_volume(100.0, 0.01, 50.0, 0.01) == 50.0


@pytest.mark.asyncio
async def test_mt5_refuses_when_execution_disabled(monkeypatch):
    monkeypatch.setenv("MT5_EXECUTION_ENABLED", "false")
    fake = _make_fake_mt5(trade_mode=0)
    ex = MT5Executor(login=700222811, password="x", server="PUPrime-Demo")
    with patch("backend.execution.mt5_executor._import_mt5", return_value=fake):
        res = await ex.place_order("ETHUSDT", "buy", 0.05, stop_loss=1680.0, take_profit=1800.0)
    assert res["success"] is False
    assert res["code"] == "MT5_EXECUTION_DISABLED"
    fake.order_send.assert_not_called()


@pytest.mark.asyncio
async def test_mt5_blocks_real_account_unless_allow_live(monkeypatch):
    monkeypatch.setenv("MT5_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("MT5_ALLOW_LIVE", "false")
    fake = _make_fake_mt5(trade_mode=2)  # REAL account
    ex = MT5Executor(login=700222811, password="x", server="PUPrime-Live")
    with patch("backend.execution.mt5_executor._import_mt5", return_value=fake):
        await ex.connect()
        res = await ex.place_order("ETHUSDT", "buy", 0.05)
    assert res["success"] is False
    assert res["code"] == "MT5_LIVE_NOT_ALLOWED"
    fake.order_send.assert_not_called()


@pytest.mark.asyncio
async def test_mt5_place_order_demo_with_contract_size_conversion(monkeypatch):
    monkeypatch.setenv("MT5_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("MT5_ALLOW_LIVE", "false")
    # SOLUSD has contract_size=10.0 on PUPrime-Demo -> 0.5 SOL = 0.05 lots
    fake = _make_fake_mt5(trade_mode=0, contract_size=10.0)
    ex = MT5Executor(login=700222811, password="x", server="PUPrime-Demo")
    with patch("backend.execution.mt5_executor._import_mt5", return_value=fake):
        await ex.connect()
        res = await ex.place_order("SOLUSDT", "buy", 0.5, stop_loss=120.0, take_profit=160.0)
    assert res["success"] is True
    assert res["symbol"] == "SOLUSD"
    sent_req = fake.order_send.call_args[0][0]
    assert sent_req["volume"] == 0.05
    assert sent_req["sl"] == 120.0
    assert sent_req["tp"] == 160.0
    assert sent_req["type_filling"] == fake.ORDER_FILLING_IOC


@pytest.mark.asyncio
async def test_multi_broker_engine_mt5_integration():
    mock_mt5 = MagicMock()
    mock_mt5.is_connected = True
    mock_mt5.connect = AsyncMock(return_value=True)
    mock_mt5.disconnect = AsyncMock()
    mock_mt5.get_account_info = AsyncMock(
        return_value={"available": 100000.0, "total": 100000.0, "currency": "USD", "margin_used": 0.0, "unrealized_pnl": 0.0}
    )
    mock_mt5.get_candles = AsyncMock(
        return_value=[{"timestamp": 1700000000000, "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0, "volume": 50.0}]
    )
    mock_mt5.place_order = AsyncMock(
        return_value={"success": True, "symbol": "ETHUSD", "order_id": "999001", "price": 1720.0, "filled_qty": 0.05}
    )

    cfg = MultiBrokerConfig(bitget_enabled=False, kraken_enabled=False, mt5_enabled=True, default_broker="mt5")
    engine = MultiBrokerEngine(config=cfg, mt5_executor=mock_mt5)
    balances = await engine.refresh_all_balances()
    assert BrokerType.MT5 in balances
    assert balances[BrokerType.MT5].available == 100000.0

    candles = await engine.fetch_candles_all("ETHUSDT", "15m", 10)
    assert BrokerType.MT5 in candles and len(candles[BrokerType.MT5]) == 1

    res = await engine.execute_order_all_brokers("ETHUSDT", "buy", 0.05, stop_loss=1680.0, take_profit=1800.0)
    assert res[BrokerType.MT5].success is True
    assert res[BrokerType.MT5].order_id == "999001"

    await engine.shutdown()
    mock_mt5.disconnect.assert_awaited_once()


def test_broker_router_includes_mt5(monkeypatch):
    monkeypatch.setenv("BITGET_ENABLED", "false")
    monkeypatch.setenv("KRAKEN_ENABLED", "false")
    monkeypatch.setenv("MT5_ENABLED", "true")
    assert is_mt5_enabled() is True
    assert get_default_brokers() == ["mt5"]
