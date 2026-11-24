"""
Phase 1.5 — Isolated Verification Suite for P1-1 / P1-2 / P1-3 Hardening
========================================================================
Tests strictly offline (external network hard-blocked):
  - P1-1: Legacy backend.exchanges.kraken_futures.KrakenFuturesExecutor.execute_order()
          raises RuntimeError without ever calling underlying REST client;
          read-only methods remain intact.
  - P1-2: smc_futures.engine.execution.kraken_executor.KrakenFuturesExecutor
          place_order(), place_order_market(), and place_order_limit() pop duplicate
          kwargs (side, intent, reduce_only) without raising TypeError.
  - P1-3: backend.routes.multi_broker POST /order/all raises HTTPException 403
          when targeting Kraken while KRAKEN_EXECUTION_ENABLED=false, without
          blocking Bitget-only requests.
"""

import os
import socket
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


class _ExternalNetworkBlocked(RuntimeError):
    pass


_ORIG_CONNECT = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) and address else ""
    if host not in ("127.0.0.1", "::1"):
        raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.5 tests: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.5 tests: {args}")


socket.socket.connect = _guarded_connect
socket.create_connection = _blocked_external
socket.getaddrinfo = _blocked_external

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SMARTTRADE_TEST_MODE"] = "1"


@pytest.mark.asyncio
async def test_p1_1_legacy_kraken_executor_execute_order_raises_runtime_error_and_preserves_readonly():
    """P1-1: Legacy execute_order() raises RuntimeError and never calls client; read-only helpers still work."""
    from backend.exchanges.kraken_futures import KrakenFuturesExecutor as LegacyKrakenExecutor

    mock_client = MagicMock()
    mock_client.place_market_order = AsyncMock()
    mock_client.place_limit_order = AsyncMock()
    mock_client.place_stop_order = AsyncMock()
    mock_client.place_take_profit_order = AsyncMock()
    mock_client.get_positions = AsyncMock(return_value=[])
    mock_client.is_connected = MagicMock(return_value=True)

    legacy = LegacyKrakenExecutor(client=mock_client)

    with pytest.raises(RuntimeError, match="FORBIDDEN_LEGACY_KRAKEN_EXECUTOR"):
        await legacy.execute_order(
            symbol="BTCUSDT",
            side="long",
            size=0.001,
            order_type="market",
            stop_loss=59000.0,
        )

    assert mock_client.place_market_order.await_count == 0
    assert mock_client.place_limit_order.await_count == 0
    assert mock_client.place_stop_order.await_count == 0
    assert mock_client.place_take_profit_order.await_count == 0

    # Read-only methods remain functional
    assert legacy.translate_symbol("BTCUSDT") == "PF_XBTUSD"
    assert await legacy.get_positions() == []
    assert legacy.is_connected() is True


@pytest.mark.asyncio
async def test_p1_2_kraken_executor_pops_duplicate_kwargs_without_typeerror():
    """P1-2: place_order / place_order_market / place_order_limit accept side/intent/reduce_only in kwargs cleanly."""
    from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor

    mock_wrapper = MagicMock()
    mock_wrapper.place_order = AsyncMock(return_value={"success": True, "order_id": "MOCK_P15_1", "price": 3000.0})

    executor = KrakenFuturesExecutor(
        api_key="test_k",
        api_secret="dGVzdF9z",
        auto_reconnect=False,
        wrapper=mock_wrapper,
    )

    # 1. place_order with explicit duplicate side, intent, reduce_only
    res1 = await executor.place_order(
        symbol="ETHUSDT",
        direction="long",
        entry_price=3000.0,
        stop_loss=2940.0,
        take_profit=3120.0,
        quantity="0.05",
        side="buy",
        intent="open",
        reduce_only=False,
    )
    assert res1["success"] is True

    # 2. place_order_market with explicit intent in kwargs
    res2 = await executor.place_order_market(
        symbol="ETHUSDT",
        side="sell",
        size="0.02",
        reduce_only=True,
        intent="close",
    )
    assert res2["code"] == "00000"

    # 3. place_order_limit with explicit intent in kwargs
    res3 = await executor.place_order_limit(
        symbol="ETHUSDT",
        side="buy",
        size="0.05",
        price="2990.0",
        reduce_only=False,
        intent="open",
        stop_loss=2940.0,
    )
    assert res3["code"] == "00000"
    assert mock_wrapper.place_order.await_count == 3


@pytest.mark.asyncio
async def test_p1_3_order_all_route_returns_403_when_kraken_disabled(monkeypatch):
    """P1-3: POST /order/all returns HTTP 403 when Kraken is active on engine and KRAKEN_EXECUTION_ENABLED=false."""
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    import backend.routes.multi_broker as mb_routes

    fake_engine = MagicMock()
    fake_engine._kraken_executor = MagicMock()
    fake_engine.config = MagicMock(kraken_enabled=True)
    fake_engine.execute_order_all_brokers = AsyncMock()
    monkeypatch.setattr(mb_routes, "_engine", fake_engine)

    req = mb_routes.MultiOrderRequest(
        symbol="ETHUSDT",
        side="buy",
        quantity_per_broker=0.05,
        order_type="market",
        stop_loss=2940.0,
    )

    with pytest.raises(HTTPException) as exc_info:
        await mb_routes.execute_order_all_brokers(req)

    assert exc_info.value.status_code == 403
    assert "KRAKEN_EXECUTION_ENABLED" in exc_info.value.detail
    assert fake_engine.execute_order_all_brokers.await_count == 0


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1.5.1 — COMPLEMENTARY COVERAGE (P1-1 FACTORY, P1-2 STRICT WRAPPER, P1-3)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_p1_1_factory_returns_neutralized_legacy_executor(monkeypatch):
    """
    P1-1 (factory): get_kraken_futures_executor() returns a LegacyKrakenExecutor
    whose execute_order() raises RuntimeError(FORBIDDEN_LEGACY_KRAKEN_EXECUTOR)
    with zero execution calls dispatched to the isolated REST client.
    """
    import backend.exchanges.kraken_futures as kf_mod

    isolated_client = MagicMock()
    isolated_client.place_market_order = AsyncMock()
    isolated_client.place_limit_order = AsyncMock()
    isolated_client.place_stop_order = AsyncMock()
    isolated_client.place_take_profit_order = AsyncMock()

    monkeypatch.setattr(kf_mod, "_client_instance", isolated_client)
    monkeypatch.setattr(kf_mod, "get_kraken_futures_client", lambda: isolated_client)

    inst = kf_mod.get_kraken_futures_executor()
    assert isinstance(inst, kf_mod.KrakenFuturesExecutor)
    assert inst.client is isolated_client

    with pytest.raises(RuntimeError, match="FORBIDDEN_LEGACY_KRAKEN_EXECUTOR"):
        await inst.execute_order(
            symbol="BTCUSDT",
            side="long",
            size=0.001,
            order_type="market",
            price=60000.0,
            stop_loss=59000.0,
            take_profit=62000.0,
            leverage=10,
        )

    assert isolated_client.place_market_order.await_count == 0
    assert isolated_client.place_limit_order.await_count == 0
    assert isolated_client.place_stop_order.await_count == 0
    assert isolated_client.place_take_profit_order.await_count == 0


class _StrictSignatureKrakenWrapper:
    """
    Strict test double mirroring the exact Python signature of
    KrakenFuturesExecutorWrapper.place_order() so duplicate keyword arguments
    raise a real Python TypeError instead of being masked by a MagicMock.
    """

    def __init__(self):
        self.calls = []

    async def place_order(
        self,
        symbol: str,
        side=None,
        quantity=None,
        order_type: str = "market",
        price=None,
        stop_loss=None,
        take_profit=None,
        leverage: int = 10,
        reduce_only: bool = False,
        entry_price=None,
        intent=None,
        **kwargs,
    ):
        self.calls.append(
            {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "order_type": order_type,
                "price": price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "leverage": leverage,
                "reduce_only": reduce_only,
                "entry_price": entry_price,
                "intent": intent,
                "extra_kwargs": dict(kwargs),
            }
        )
        return {"success": False, "code": "TEST_CAPTURE_ONLY", "error": "Strict capture wrapper (no execution)"}


@pytest.mark.asyncio
async def test_p1_2_strict_wrapper_receives_exact_forwarded_arguments():
    """
    P1-2 (strict signature): Verify exact forwarded arguments on real KrakenFuturesExecutor:
    1. Explicit intent='close', reduce_only=True, entry_price=None, positive SL/TP, coherent side/direction.
    2. Legacy/default call without intent or reduce_only preserves intent='open', reduce_only=False.
    Zero duplicate kwargs, zero silent coercion of None.
    """
    from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor

    strict_wrapper = _StrictSignatureKrakenWrapper()
    executor = KrakenFuturesExecutor(
        api_key="test_key_p151",
        api_secret="dGVzdF9zZWNyZXRfcDE1MQ==",
        auto_reconnect=False,
        wrapper=strict_wrapper,
    )

    # Scenario 1: Explicit close + reduce_only=True + entry_price=None + positive SL/TP + coherent side/direction
    res_close = await executor.place_order(
        symbol="ETHUSDT",
        direction="short",
        entry_price=None,
        stop_loss=3060.0,
        take_profit=2880.0,
        order_type="market",
        leverage=10,
        quantity="0.05",
        side="sell",
        intent="close",
        reduce_only=True,
    )
    assert res_close["code"] == "TEST_CAPTURE_ONLY"
    assert len(strict_wrapper.calls) == 1

    c1 = strict_wrapper.calls[0]
    assert c1["symbol"] == "ETHUSDT"
    assert c1["side"] == "sell"
    assert c1["intent"] == "close"
    assert c1["reduce_only"] is True
    assert c1["entry_price"] is None
    assert c1["stop_loss"] == 3060.0
    assert c1["take_profit"] == 2880.0
    assert c1["quantity"] == "0.05"
    assert c1["leverage"] == 10
    assert c1["extra_kwargs"].get("direction") == "short"
    assert "side" not in c1["extra_kwargs"]
    assert "intent" not in c1["extra_kwargs"]
    assert "reduce_only" not in c1["extra_kwargs"]

    # Scenario 2: Historical call without intent / reduce_only preserves defaults ('open', False)
    res_default = await executor.place_order(
        symbol="ETHUSDT",
        direction="long",
        entry_price=3000.0,
        stop_loss=2940.0,
        take_profit=3120.0,
        order_type="market",
        leverage=10,
        quantity="0.05",
    )
    assert res_default["code"] == "TEST_CAPTURE_ONLY"
    assert len(strict_wrapper.calls) == 2

    c2 = strict_wrapper.calls[1]
    assert c2["symbol"] == "ETHUSDT"
    assert c2["side"] == "buy"
    assert c2["intent"] == "open"
    assert c2["reduce_only"] is False
    assert c2["entry_price"] == 3000.0
    assert c2["stop_loss"] == 2940.0
    assert c2["take_profit"] == 3120.0
    assert "side" not in c2["extra_kwargs"]
    assert "intent" not in c2["extra_kwargs"]
    assert "reduce_only" not in c2["extra_kwargs"]


@pytest.mark.asyncio
async def test_p1_3_order_all_route_allows_when_kraken_enabled_and_for_bitget_only(monkeypatch):
    """
    P1-3 (positive & Bitget-only paths):
    1. When KRAKEN_EXECUTION_ENABLED=true and Kraken is active on engine -> POST /order/all delegates without 403.
    2. When KRAKEN_EXECUTION_ENABLED=false and engine is Bitget-only -> POST /order/all delegates without 403.
    """
    import backend.routes.multi_broker as mb_routes
    from smc_futures.engine.multi_broker_engine import BrokerType, OrderResult

    req = mb_routes.MultiOrderRequest(
        symbol="ETHUSDT",
        side="buy",
        quantity_per_broker=0.05,
        order_type="market",
        stop_loss=2940.0,
        take_profit=3120.0,
    )

    # Branch 1: KRAKEN_EXECUTION_ENABLED=true -> allowed
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    fake_engine_kr = MagicMock()
    fake_engine_kr._kraken_executor = MagicMock()
    fake_engine_kr.config = MagicMock(kraken_enabled=True)
    fake_engine_kr.execute_order_all_brokers = AsyncMock(
        return_value={
            BrokerType.KRAKEN: OrderResult(
                success=False,
                broker=BrokerType.KRAKEN,
                symbol="ETHUSDT",
                error="SIMULATED_DOWNSTREAM_RESPONSE",
            )
        }
    )
    monkeypatch.setattr(mb_routes, "_engine", fake_engine_kr)

    resp_kr = await mb_routes.execute_order_all_brokers(req)
    assert fake_engine_kr.execute_order_all_brokers.await_count == 1
    assert "kraken" in resp_kr["results"]

    # Branch 2: KRAKEN_EXECUTION_ENABLED=false + Bitget-only engine -> not blocked by Kraken 403
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    fake_engine_bg = MagicMock()
    fake_engine_bg._kraken_executor = None
    fake_engine_bg.config = MagicMock(kraken_enabled=False)
    fake_engine_bg.execute_order_all_brokers = AsyncMock(
        return_value={
            BrokerType.BITGET: OrderResult(
                success=False,
                broker=BrokerType.BITGET,
                symbol="ETHUSDT",
                error="SIMULATED_BITGET_ONLY",
            )
        }
    )
    monkeypatch.setattr(mb_routes, "_engine", fake_engine_bg)

    resp_bg = await mb_routes.execute_order_all_brokers(req)
    assert fake_engine_bg.execute_order_all_brokers.await_count == 1
    assert "bitget" in resp_bg["results"]
