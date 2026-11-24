"""
Phase 1.2 — Real Adapter Contract Integration Tests
====================================================
Verifies the real production chain:
    MultiBrokerEngine -> KrakenFuturesExecutor -> KrakenFuturesExecutorWrapper -> FakeKrakenV2Client

Explicitly exercises a REAL `KrakenFuturesExecutor` instance (NOT injecting
`KrakenFuturesExecutorWrapper` directly into `MultiBrokerEngine`), covering:
  1. OPEN with valid `price` (and SL/TP) -> succeeds, SL & TP confirmed, zero TypeError.
  2. OPEN with `price=None` (MARKET) resolved via `get_tickers()` -> succeeds, SL & TP confirmed.
  3. OPEN with `price=0` (and `price=0.0`) -> refused fail-closed (`INVALID_REFERENCE_PRICE`),
     0 orders sent, never uses `0.0` as price.
  4. OPEN without available reference price (`price=None` and empty tickers) -> refused fail-closed
     (`MISSING_REFERENCE_PRICE`), 0 orders sent.
  5. Effective transmission of `stop_loss` and `take_profit` through the real adapter to the exchange.
  6. Absence of duplicate-kwarg `TypeError` (`side`, `intent`, `reduce_only`) on real `KrakenFuturesExecutor.place_order()`.
  7. OPEN refused when `KRAKEN_EXECUTION_ENABLED=false` (0 orders sent).
  8. `REDUCE` and `CLOSE` still allowed when `KRAKEN_EXECUTION_ENABLED=false`.
  9. Zero bypass: every order call passes through BOTH `KrakenFuturesExecutor` and `KrakenFuturesExecutorWrapper`.

Strict isolation:
- External network hard-blocked (socket guard).
- Uses temporary SQLite DB (`tmp_path`) only; never touches production `data/trades.db` or `.env`.
"""

import os
import socket
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class _ExternalNetworkBlocked(RuntimeError):
    pass


_ORIG_CONNECT = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) and address else ""
    if host not in ("127.0.0.1", "::1"):
        raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.2 tests: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in Phase 1.2 tests: {args}")


socket.socket.connect = _guarded_connect
socket.create_connection = _blocked_external
socket.getaddrinfo = _blocked_external

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SMARTTRADE_TEST_MODE"] = "1"
os.environ["KRAKEN_RETRY_ON_ORDER_REJECT"] = "1"
os.environ["KRAKEN_POS_RESOLVE_ATTEMPTS"] = "1"
os.environ["KRAKEN_POS_RESOLVE_DELAY"] = "0.0"

from backend.db.base import Base
from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper, KrakenTradeTracker
from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor
from smc_futures.engine.multi_broker_engine import (
    Balance,
    BrokerType,
    MultiBrokerConfig,
    MultiBrokerEngine,
)


@pytest.fixture
def isolated_sqlite(tmp_path, monkeypatch):
    """Temporary SQLite DB isolated from production data/trades.db."""
    db_file = tmp_path / "test_phase1_2_trades.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    @contextmanager
    def _temp_session():
        db = SessionLocal()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr("backend.services.live_trades_service.get_db_session", _temp_session)
    yield db_file, _temp_session
    engine.dispose()


class FakeKrakenV2Client:
    """Offline simulated KrakenFuturesRESTClientV2 (plural get_tickers())."""

    def __init__(self, tickers: Optional[Dict[str, Dict[str, Any]]] = None):
        self.sent_orders: List[Dict[str, Any]] = []
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.open_orders: List[Dict[str, Any]] = []
        self.fills: List[Dict[str, Any]] = []
        self._seq = 0
        self.tickers = (
            tickers
            if tickers is not None
            else {
                "PF_ETHUSD": {"symbol": "PF_ETHUSD", "mark_price": 3000.0, "last": 3001.0},
                "PF_XBTUSD": {"symbol": "PF_XBTUSD", "mark_price": 60000.0, "last": 60005.0},
            }
        )

    async def get_tickers(self) -> Dict[str, Dict[str, Any]]:
        return dict(self.tickers)

    async def get_positions(self, **kwargs) -> List[Dict[str, Any]]:
        return list(self.positions.values())

    async def get_open_orders(self, symbol: Optional[str] = None, **kwargs) -> List[Dict[str, Any]]:
        if symbol:
            return [o for o in self.open_orders if o.get("symbol", "").upper() == symbol.upper()]
        return list(self.open_orders)

    async def get_fills(self, last_fill_time: Optional[str] = None, **kwargs) -> List[Dict[str, Any]]:
        return list(self.fills)

    async def cancel_order(self, order_id: Optional[str] = None, cli_ord_id: Optional[str] = None, **kwargs):
        self.open_orders = [
            o
            for o in self.open_orders
            if not (
                (order_id and o.get("order_id") == order_id)
                or (cli_ord_id and o.get("cliOrdId") == cli_ord_id)
            )
        ]
        return {"result": "success", "cancelStatus": {"status": "cancelled"}}

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        size: float,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        return await self.place_order(
            symbol=symbol,
            side=side,
            order_type="mkt",
            size=size,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            **kwargs,
        )

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        size: float,
        price: Optional[float] = None,
        limit_price: Optional[float] = None,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        return await self.place_order(
            symbol=symbol,
            side=side,
            order_type="lmt",
            size=size,
            limit_price=limit_price or price,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            **kwargs,
        )

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: float,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        reduce_only: bool = False,
        trigger_signal: Optional[str] = None,
        client_order_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        self._seq += 1
        oid = f"FAKE_P12_OID_{self._seq}"
        rec = {
            "order_id": oid,
            "symbol": symbol,
            "side": side,
            "size": float(size),
            "orderType": order_type,
            "limitPrice": limit_price,
            "stopPrice": stop_price,
            "reduceOnly": bool(reduce_only),
            "cliOrdId": client_order_id,
        }
        self.sent_orders.append(rec)

        if order_type in ("stp", "take_profit"):
            self.open_orders.append(
                {
                    "order_id": oid,
                    "cliOrdId": client_order_id,
                    "symbol": symbol,
                    "side": side,
                    "orderType": order_type,
                    "size": float(size),
                    "quantity": float(size),
                    "filled": 0.0,
                    "reduceOnly": bool(reduce_only),
                    "stopPrice": float(stop_price) if stop_price is not None else None,
                    "limitPrice": float(limit_price) if limit_price is not None else None,
                    "status": "untouched",
                }
            )
            return {"success": True, "code": "00000", "order_id": oid}

        # Entry / reduce / close fill
        tk = self.tickers.get(symbol, {})
        fill_px = float(limit_price or tk.get("mark_price") or 3000.0)
        if not reduce_only:
            pos_side = "long" if side == "buy" else "short"
            self.positions[symbol] = {
                "symbol": symbol,
                "side": pos_side,
                "size": float(size),
                "price": fill_px,
            }
        else:
            existing = self.positions.get(symbol)
            if existing:
                remaining = max(0.0, float(existing["size"]) - float(size))
                if remaining <= 1e-8:
                    self.positions.pop(symbol, None)
                else:
                    existing["size"] = remaining

        self.fills.append(
            {
                "fill_id": f"FILL_{oid}",
                "order_id": oid,
                "cliOrdId": client_order_id,
                "symbol": symbol,
                "side": side,
                "size": float(size),
                "price": fill_px,
                "fillTime": "2026-11-21T17:00:00Z",
            }
        )
        return {
            "success": True,
            "code": "00000",
            "order_id": oid,
            "filled_size": float(size),
            "price": fill_px,
        }


def _build_real_adapter_stack(
    tickers: Optional[Dict[str, Dict[str, Any]]] = None,
    unlock_startup: bool = True,
):
    """
    Build the REAL MultiBrokerEngine -> KrakenFuturesExecutor -> KrakenFuturesExecutorWrapper
    stack with spy counters proving zero bypass of either the adapter or the central wrapper.
    """
    fake_client = FakeKrakenV2Client(tickers=tickers)
    wrapper = KrakenFuturesExecutorWrapper(
        api_key="test_key_p12",
        api_secret="dGVzdF9zZWNyZXRfcDEy",
        timeout=2.0,
        single_entry_mode=False,
    )
    wrapper._rest_client = fake_client
    wrapper._get_rest_client = lambda: fake_client
    if unlock_startup:
        wrapper._global_entry_lock = False
        wrapper._startup_reconciliation_completed = True

    # Real KrakenFuturesExecutor adapter (NOT injecting wrapper directly into MultiBrokerEngine)
    executor = KrakenFuturesExecutor(
        api_key="test_key_p12",
        api_secret="dGVzdF9zZWNyZXRfcDEy",
        timeout=2.0,
        auto_reconnect=False,
        single_entry_mode=False,
        wrapper=wrapper,
    )
    executor._connected = True
    executor.check_balance_sufficient = AsyncMock(return_value=(True, 1000.0))

    # Spy counters to prove zero bypass
    spy = {"adapter_place_order_calls": 0, "wrapper_place_order_calls": 0, "wrapper_kwargs": []}
    orig_adapter_place = executor.place_order
    orig_wrapper_place = wrapper.place_order

    async def spied_adapter_place(*args, **kwargs):
        spy["adapter_place_order_calls"] += 1
        return await orig_adapter_place(*args, **kwargs)

    async def spied_wrapper_place(*args, **kwargs):
        spy["wrapper_place_order_calls"] += 1
        spy["wrapper_kwargs"].append(dict(kwargs))
        return await orig_wrapper_place(*args, **kwargs)

    executor.place_order = spied_adapter_place
    wrapper.place_order = spied_wrapper_place

    cfg = MultiBrokerConfig(
        default_broker="kraken",
        bitget_enabled=False,
        kraken_enabled=True,
        kraken_api_key="test_key_p12",
        kraken_api_secret="dGVzdF9zZWNyZXRfcDEy",
        kraken_symbols=["PF_ETHUSD", "PF_XBTUSD"],
        default_leverage=10,
        min_balance_usd=5.0,
    )
    engine = MultiBrokerEngine(config=cfg, kraken_executor=executor)
    engine._kraken_market = MagicMock()
    engine._balances[BrokerType.KRAKEN] = Balance(
        broker=BrokerType.KRAKEN, available=1000.0, total=1000.0, currency="USD"
    )
    if unlock_startup:
        engine._kraken_reconciliation_done = True
        engine._kraken_reconciliation_report = {
            "status": "SUCCESS",
            "startup_secure": True,
            "global_entry_lock": False,
        }

    return engine, executor, wrapper, fake_client, spy


@pytest.mark.asyncio
async def test_1_open_with_valid_price_and_sl_tp_via_real_adapter(isolated_sqlite, monkeypatch):
    """
    OPEN with explicit valid price + SL + TP through REAL KrakenFuturesExecutor:
    - No TypeError from duplicate side/intent/reduce_only kwargs
    - Both adapter and central wrapper invoked (zero bypass)
    - Entry, SL (stp, reduceOnly=True), and TP (take_profit, reduceOnly=True) confirmed on exchange
    """
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    engine, executor, wrapper, fake_client, spy = _build_real_adapter_stack()
    assert isinstance(engine._kraken_executor, KrakenFuturesExecutor)

    res = await engine.execute_order_kraken(
        symbol="ETHUSDT",
        side="buy",
        quantity=0.05,
        order_type="market",
        price=3000.0,
        stop_loss=2940.0,
        take_profit=3120.0,
    )

    assert res.success is True, f"Expected success, got error={res.error}"
    assert res.order_id
    assert spy["adapter_place_order_calls"] == 1
    assert spy["wrapper_place_order_calls"] == 1
    # Verify wrapper received exact parameters without corruption
    w_kw = spy["wrapper_kwargs"][0]
    assert w_kw["side"] == "buy"
    assert w_kw["intent"] == "open"
    assert w_kw["reduce_only"] is False
    assert w_kw["entry_price"] == 3000.0
    assert w_kw["stop_loss"] == 2940.0
    assert w_kw["take_profit"] == 3120.0

    # Verify SL and TP orders exist on fake exchange
    stp_orders = [o for o in fake_client.open_orders if o["orderType"] == "stp"]
    tp_orders = [o for o in fake_client.open_orders if o["orderType"] == "take_profit"]
    assert len(stp_orders) == 1
    assert stp_orders[0]["reduceOnly"] is True
    assert stp_orders[0]["side"] == "sell"
    assert pytest.approx(stp_orders[0]["stopPrice"], rel=1e-4) == 2940.0
    assert len(tp_orders) == 1
    assert tp_orders[0]["reduceOnly"] is True
    assert tp_orders[0]["side"] == "sell"
    assert pytest.approx(tp_orders[0]["stopPrice"], rel=1e-4) == 3120.0


@pytest.mark.asyncio
async def test_2_open_market_price_none_resolved_via_get_tickers(isolated_sqlite, monkeypatch):
    """
    OPEN MARKET with price=None through REAL KrakenFuturesExecutor:
    - MultiBrokerEngine MUST pass entry_price=None (NOT 0.0!)
    - Central wrapper resolves reference price via get_tickers() (3000.0)
    - Pre-flight SL directional check passes and SL/TP are placed and confirmed
    """
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    engine, executor, wrapper, fake_client, spy = _build_real_adapter_stack()
    assert isinstance(engine._kraken_executor, KrakenFuturesExecutor)

    res = await engine.execute_order_kraken(
        symbol="ETHUSDT",
        side="sell",
        quantity=0.05,
        order_type="market",
        price=None,
        stop_loss=3060.0,
        take_profit=2880.0,
    )

    assert res.success is True, f"Expected success with price=None via get_tickers(), got error={res.error}"
    assert spy["adapter_place_order_calls"] == 1
    assert spy["wrapper_place_order_calls"] == 1
    # Confirm price=None was NOT converted to 0.0
    assert spy["wrapper_kwargs"][0]["entry_price"] is None
    assert "price" not in spy["wrapper_kwargs"][0] or spy["wrapper_kwargs"][0]["price"] is None

    stp_orders = [o for o in fake_client.open_orders if o["orderType"] == "stp"]
    tp_orders = [o for o in fake_client.open_orders if o["orderType"] == "take_profit"]
    assert len(stp_orders) == 1
    assert stp_orders[0]["side"] == "buy"
    assert pytest.approx(stp_orders[0]["stopPrice"], rel=1e-4) == 3060.0
    assert len(tp_orders) == 1
    assert tp_orders[0]["side"] == "buy"
    assert pytest.approx(tp_orders[0]["stopPrice"], rel=1e-4) == 2880.0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_price", [0, 0.0])
async def test_3_open_with_price_zero_refused_fail_closed(isolated_sqlite, monkeypatch, bad_price):
    """
    OPEN with price=0 or price=0.0 through REAL KrakenFuturesExecutor:
    - Must NEVER use 0.0 as a valid reference price
    - Must fail closed with INVALID_REFERENCE_PRICE
    - Zero orders dispatched to exchange
    """
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    engine, executor, wrapper, fake_client, spy = _build_real_adapter_stack()

    res = await engine.execute_order_kraken(
        symbol="ETHUSDT",
        side="buy",
        quantity=0.05,
        order_type="market",
        price=bad_price,
        stop_loss=2940.0,
        take_profit=3120.0,
    )

    assert res.success is False
    assert "invalid" in (res.error or "").lower()
    assert spy["adapter_place_order_calls"] == 1
    assert spy["wrapper_place_order_calls"] == 1
    assert len(fake_client.sent_orders) == 0


@pytest.mark.asyncio
async def test_4_open_without_reference_price_refused_fail_closed(isolated_sqlite, monkeypatch):
    """
    OPEN MARKET with price=None AND empty get_tickers():
    - Must fail closed (MISSING_REFERENCE_PRICE)
    - Zero orders dispatched to exchange
    """
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    engine, executor, wrapper, fake_client, spy = _build_real_adapter_stack(tickers={})

    res = await engine.execute_order_kraken(
        symbol="ETHUSDT",
        side="buy",
        quantity=0.05,
        order_type="market",
        price=None,
        stop_loss=2940.0,
        take_profit=3120.0,
    )

    assert res.success is False
    assert "reference price" in (res.error or "").lower()
    assert spy["adapter_place_order_calls"] == 1
    assert spy["wrapper_place_order_calls"] == 1
    assert len(fake_client.sent_orders) == 0


@pytest.mark.asyncio
async def test_5_open_refused_when_kraken_execution_disabled(isolated_sqlite, monkeypatch):
    """
    When KRAKEN_EXECUTION_ENABLED=false:
    - MultiBrokerEngine.execute_order_kraken() blocks OPEN fail-closed
    - Zero orders dispatched to exchange
    """
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    engine, executor, wrapper, fake_client, spy = _build_real_adapter_stack()

    res = await engine.execute_order_kraken(
        symbol="ETHUSDT",
        side="buy",
        quantity=0.05,
        order_type="market",
        price=3000.0,
        stop_loss=2940.0,
        take_profit=3120.0,
    )

    assert res.success is False
    assert "KRAKEN_EXECUTION_ENABLED" in (res.error or "")
    assert len(fake_client.sent_orders) == 0


@pytest.mark.asyncio
async def test_6_reduce_and_close_allowed_when_kraken_execution_disabled(isolated_sqlite, monkeypatch):
    """
    When KRAKEN_EXECUTION_ENABLED=false:
    - OPEN via real adapter is blocked (KRAKEN_EXECUTION_DISABLED)
    - REDUCE via real KrakenFuturesExecutor.place_order_market(reduce_only=True) succeeds
    - CLOSE via central KrakenFuturesExecutorWrapper.place_order(intent='close', reduce_only=True) succeeds
    """
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    engine, executor, wrapper, fake_client, spy = _build_real_adapter_stack()

    # Seed an existing open position + active bot tracker on the fake exchange
    fake_client.positions["PF_ETHUSD"] = {
        "symbol": "PF_ETHUSD",
        "side": "long",
        "size": 0.05,
        "price": 3000.0,
    }
    wrapper._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
        order_id="EXISTING_P12_OID",
        client_oid="BOT_PF_ETHUSD_P12",
        symbol="PF_ETHUSD",
        side="long",
        filled_qty=0.05,
        expected_exposure=0.05,
        status="OPEN",
    )

    # 1. Direct OPEN through real adapter is refused
    open_res = await executor.place_order(
        symbol="ETHUSDT",
        direction="long",
        entry_price=3000.0,
        stop_loss=2940.0,
        take_profit=3120.0,
        order_type="market",
        leverage=10,
        quantity="0.05",
    )
    assert open_res["success"] is False
    assert open_res["code"] == "KRAKEN_EXECUTION_DISABLED"
    assert len(fake_client.sent_orders) == 0

    # 2. REDUCE through real KrakenFuturesExecutor.place_order_market(reduce_only=True) succeeds
    reduce_res = await executor.place_order_market(
        symbol="ETHUSDT",
        side="sell",
        size="0.02",
        reduce_only=True,
    )
    assert reduce_res.get("code") == "00000" or reduce_res.get("success") is True
    assert len(fake_client.sent_orders) == 1
    assert fake_client.sent_orders[0]["reduceOnly"] is True
    assert pytest.approx(fake_client.positions["PF_ETHUSD"]["size"], rel=1e-6) == 0.03

    # 3. CLOSE through central wrapper succeeds and flattens position
    close_res = await wrapper.place_order(
        symbol="ETHUSDT",
        side="sell",
        quantity=0.03,
        order_type="market",
        entry_price=3000.0,
        reduce_only=True,
        intent="close",
    )
    assert close_res.get("success") is True
    assert len(fake_client.sent_orders) == 2
    assert fake_client.sent_orders[1]["reduceOnly"] is True
    assert "PF_ETHUSD" not in fake_client.positions
    assert spy["wrapper_place_order_calls"] == 3  # 1 OPEN blocked + 1 REDUCE + 1 CLOSE
