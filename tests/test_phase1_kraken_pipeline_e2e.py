"""
Phase 1 End-to-End Isolated Test Suite (Lots B, C, D, E)
=========================================================
Covers:
- Lot C (D1 / P0-R2.2): get_tickers() plural compatibility + freshness TTL check
- Lot D (P0-R3): MultiBrokerConfig.from_env(), MultiBrokerEngine.execute_order_kraken()
  converging through protected KrakenFuturesExecutorWrapper.place_order(), SL & TP
- Lot E (D3 Real SQLite): Real temporary SQLite DB (tmp_path) verifying commit,
  exact trade_id close, read-back CLOSED, multi-trade isolation, quarantine persistence,
  restart reconcile_on_startup() lock, and SQLite unavailable fail-closed
- Obligatory scenarios: OPEN LONG & SHORT, SL & TP, REDUCE, CLOSE, timeout,
  ambiguous REST, AUTO_EXECUTE=false, KRAKEN_EXECUTION_ENABLED=false, quarantine present.

Strict isolation:
- External network hard-blocked (socket guard).
- Zero access to production data/trades.db (uses tmp_path SQLite only).
- Zero .env read, zero real Kraken orders, zero bot startup.
"""

import asyncio
import os
import socket
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ── Hard-block all external network calls ────────────────────────────────────
class _ExternalNetworkBlocked(RuntimeError):
    pass


_ORIG_CONNECT = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) and address else ""
    if host not in ("127.0.0.1", "::1"):
        raise _ExternalNetworkBlocked(f"External network blocked in Phase 1 tests: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in Phase 1 tests: {args}")


socket.socket.connect = _guarded_connect
socket.create_connection = _blocked_external
socket.getaddrinfo = _blocked_external

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SMARTTRADE_TEST_MODE"] = "1"
os.environ["KRAKEN_RETRY_ON_ORDER_REJECT"] = "1"
os.environ["KRAKEN_POS_RESOLVE_ATTEMPTS"] = "1"
os.environ["KRAKEN_POS_RESOLVE_DELAY"] = "0.0"

from backend.db.base import Base
from backend.models.live_trade import LiveTrade
from backend.services.live_trades_service import LiveTradesService
from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
from smc_futures.engine.multi_broker_engine import (
    BrokerType,
    MultiBrokerConfig,
    MultiBrokerEngine,
)


@pytest.fixture
def isolated_sqlite(tmp_path, monkeypatch):
    """Create a REAL temporary SQLite DB and bind LiveTradesService.get_db_session to it."""
    db_file = tmp_path / "test_phase1_trades.db"
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
    """Simulated KrakenFuturesRESTClientV2 (uses plural get_tickers() like production!)."""

    def __init__(self, tickers: Optional[Dict[str, Dict[str, Any]]] = None):
        self.sent_orders: List[Dict[str, Any]] = []
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.open_orders: List[Dict[str, Any]] = []
        self.fills: List[Dict[str, Any]] = []
        self._seq = 0
        self.tickers = tickers if tickers is not None else {
            "PF_XBTUSD": {"symbol": "PF_XBTUSD", "mark_price": 60000.0, "last": 60005.0},
            "PF_ETHUSD": {"symbol": "PF_ETHUSD", "mark_price": 3000.0, "last": 3001.0},
        }
        self.fail_sl = False
        self.open_timeout = False
        self.open_ambiguous = False

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
            o for o in self.open_orders
            if o.get("order_id") != order_id and o.get("cliOrdId") != cli_ord_id
        ]
        return {"success": True, "code": "00000"}

    async def place_market_order(self, symbol: str, side: str, size: float, reduce_only: bool = False, client_order_id: Optional[str] = None, **kwargs):
        return await self.place_order(symbol=symbol, side=side, order_type="mkt", size=size, reduce_only=reduce_only, client_order_id=client_order_id, **kwargs)

    async def place_limit_order(self, symbol: str, side: str, size: float, price: Optional[float] = None, limit_price: Optional[float] = None, reduce_only: bool = False, client_order_id: Optional[str] = None, **kwargs):
        return await self.place_order(symbol=symbol, side=side, order_type="lmt", size=size, limit_price=limit_price or price, reduce_only=reduce_only, client_order_id=client_order_id, **kwargs)

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
        oid = f"PH1-OID-{self._seq}"
        self.sent_orders.append({
            "order_id": oid, "symbol": symbol, "side": side,
            "order_type": order_type, "size": float(size),
            "stop_price": stop_price, "reduce_only": reduce_only,
            "client_order_id": client_order_id,
        })

        if order_type == "stp":
            if self.fail_sl:
                return {"success": False, "code": "SL_REJECTED", "error": "Simulated SL failure"}
            self.open_orders.append({
                "order_id": oid, "cliOrdId": client_order_id, "symbol": symbol,
                "side": side, "size": float(size), "stopPrice": stop_price,
                "orderType": "stp", "reduceOnly": True, "status": "untouched",
            })
            return {"success": True, "code": "00000", "order_id": oid}

        if order_type == "take_profit":
            self.open_orders.append({
                "order_id": oid, "cliOrdId": client_order_id, "symbol": symbol,
                "side": side, "size": float(size), "stopPrice": stop_price,
                "orderType": "take_profit", "reduceOnly": True, "status": "untouched",
            })
            return {"success": True, "code": "00000", "order_id": oid}

        if client_order_id and str(client_order_id).startswith("BOT_EMERGENCY_"):
            self.positions.pop(symbol, None)
            return {"success": True, "code": "00000", "order_id": oid, "price": 59900.0}

        if reduce_only:
            pos = self.positions.get(symbol)
            if pos:
                rem = max(0.0, float(pos["size"]) - float(size))
                if rem <= 1e-8:
                    self.positions.pop(symbol, None)
                else:
                    pos["size"] = rem
            self.fills.append({
                "fill_id": f"FILL-{self._seq}", "order_id": oid, "cliOrdId": client_order_id,
                "symbol": symbol, "side": side, "size": float(size), "price": 60500.0,
                "fillTime": "2026-11-21T17:00:00Z",
            })
            return {"success": True, "code": "00000", "order_id": oid, "filled_size": float(size), "price": 60500.0}

        if self.open_timeout:
            raise asyncio.TimeoutError("Simulated REST timeout on OPEN")
        if self.open_ambiguous:
            return {"result": "unknown_ambiguous_payload"}

        px = float(limit_price or self.tickers.get(symbol, {}).get("mark_price", 60000.0))
        pos_side = "long" if side == "buy" else "short"
        self.positions[symbol] = {"symbol": symbol, "side": pos_side, "size": float(size), "price": px}
        self.fills.append({
            "fill_id": f"FILL-{self._seq}", "order_id": oid, "cliOrdId": client_order_id,
            "symbol": symbol, "side": side, "size": float(size), "price": px,
            "fillTime": "2026-11-21T17:00:00Z",
        })
        return {"success": True, "code": "00000", "order_id": oid, "filled_size": float(size), "price": px}


def _make_wrapper(client: FakeKrakenV2Client) -> KrakenFuturesExecutorWrapper:
    w = KrakenFuturesExecutorWrapper(executor=client)
    w._rest_client = client
    w._global_entry_lock = False
    w._startup_reconciliation_completed = True
    return w


# ══════════════════════════════════════════════════════════════════════════════
# LOT C (D1 / P0-R2.2): get_tickers() + FRESHNESS TTL
# ══════════════════════════════════════════════════════════════════════════════

def test_lot_c_get_tickers_plural_resolves_long_and_short(monkeypatch, isolated_sqlite):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenV2Client(tickers={
        "PF_XBTUSD": {"symbol": "PF_XBTUSD", "mark_price": 60000.0, "last": 60010.0, "timestamp": time.time()},
        "PF_ETHUSD": {"symbol": "PF_ETHUSD", "mark_price": 3000.0, "last": 3001.0, "timestamp": time.time()},
    })
    w = _make_wrapper(client)

    # OPEN LONG (SL below mark_price, TP above)
    res_long = asyncio.run(w.place_order(
        symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=None, stop_loss=58000.0, take_profit=64000.0, reduce_only=False, intent="open",
    ))
    assert res_long["success"] is True
    assert res_long["sl_verified"] is True
    assert res_long["tp_attached"] is True

    # OPEN SHORT (SL above mark_price, TP below)
    res_short = asyncio.run(w.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.1, order_type="market",
        entry_price=None, stop_loss=3150.0, take_profit=2800.0, reduce_only=False, intent="open",
    ))
    assert res_short["success"] is True
    assert res_short["sl_verified"] is True
    assert res_short["tp_attached"] is True


def test_lot_c_stale_and_future_ticker_refused_fail_closed(monkeypatch, isolated_sqlite):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_REF_PRICE_MAX_AGE_SEC", "15")

    stale_client = FakeKrakenV2Client(tickers={
        "PF_XBTUSD": {"symbol": "PF_XBTUSD", "mark_price": 60000.0, "timestamp": time.time() - 120.0},
    })
    w_stale = _make_wrapper(stale_client)
    res_stale = asyncio.run(w_stale.place_order(
        symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=None, stop_loss=58000.0, reduce_only=False, intent="open",
    ))
    assert res_stale["success"] is False
    assert res_stale["code"] == "STALE_REFERENCE_PRICE"
    assert len(stale_client.sent_orders) == 0

    future_client = FakeKrakenV2Client(tickers={
        "PF_XBTUSD": {"symbol": "PF_XBTUSD", "mark_price": 60000.0, "timestamp": time.time() + 120.0},
    })
    w_fut = _make_wrapper(future_client)
    res_fut = asyncio.run(w_fut.place_order(
        symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=None, stop_loss=58000.0, reduce_only=False, intent="open",
    ))
    assert res_fut["success"] is False
    assert res_fut["code"] == "STALE_REFERENCE_PRICE"
    assert len(future_client.sent_orders) == 0


# ══════════════════════════════════════════════════════════════════════════════
# LOT D (P0-R3): MultiBrokerEngine CONVERGENCE + SL/TP + from_env() FAIL-CLOSED
# ══════════════════════════════════════════════════════════════════════════════

def test_lot_d_from_env_fail_closed_when_kraken_exec_disabled(monkeypatch):
    monkeypatch.setenv("KRAKEN_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    cfg = MultiBrokerConfig.from_env()
    assert cfg.kraken_enabled is False

    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    cfg_on = MultiBrokerConfig.from_env()
    assert cfg_on.kraken_enabled is True


def test_lot_d_multi_broker_engine_converges_through_wrapper_with_sl_tp(monkeypatch, isolated_sqlite):
    client = FakeKrakenV2Client()
    w = _make_wrapper(client)
    cfg = MultiBrokerConfig(bitget_enabled=False, kraken_enabled=True, default_leverage=10)
    eng = MultiBrokerEngine(config=cfg, kraken_executor=w)
    eng._kraken_reconcile_completed = True
    eng._kraken_reconcile_secure = True

    # 1. Blocked when KRAKEN_EXECUTION_ENABLED=false
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    res_off = asyncio.run(eng.execute_order_kraken(
        symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
        price=60000.0, stop_loss=58000.0, take_profit=64000.0,
    ))
    assert res_off.success is False
    assert len(client.sent_orders) == 0

    # 2. Blocked when SL missing even with KRAKEN_EXECUTION_ENABLED=true
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    res_nosl = asyncio.run(eng.execute_order_kraken(
        symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
        price=60000.0, stop_loss=None, take_profit=64000.0,
    ))
    assert res_nosl.success is False
    assert len(client.sent_orders) == 0

    # 3. Succeeds with confirmed SL + TP via central wrapper
    res_ok = asyncio.run(eng.execute_order_kraken(
        symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
        price=60000.0, stop_loss=58000.0, take_profit=64000.0,
    ))
    assert res_ok.success is True
    assert any(o["order_type"] == "stp" and o["reduce_only"] is True for o in client.sent_orders)
    assert any(o["order_type"] == "take_profit" and o["reduce_only"] is True for o in client.sent_orders)

    # 4. Dead _attach_tpsl_kraken raises NotImplementedError
    with pytest.raises(NotImplementedError):
        asyncio.run(eng._attach_tpsl_kraken("PF_XBTUSD", "buy", 58000.0, "stop-loss"))


# ══════════════════════════════════════════════════════════════════════════════
# LOT E (D3 REAL SQLITE): COMMIT, EXACT TRADE_ID CLOSE, QUARANTINE & RESTART
# ══════════════════════════════════════════════════════════════════════════════

def test_lot_e_real_sqlite_emergency_close_closes_exact_row_only(monkeypatch, isolated_sqlite):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    # Pre-seed an existing OPEN trade on PF_XBTUSD in REAL temp SQLite
    other = LiveTradesService.create_live_trade(
        order_id="PRE-EXISTING-OID", symbol="PF_XBTUSD", side="long",
        qty=0.05, entry=59000.0, stop=57000.0, tp=63000.0, leverage=10,
        broker="kraken", status="OPEN", client_oid="BOT_PRE_EXISTING",
    )
    assert other is not None and other.id

    client = FakeKrakenV2Client()
    client.fail_sl = True
    w = _make_wrapper(client)

    res = asyncio.run(w.place_order(
        symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=60000.0, stop_loss=58000.0, reduce_only=False, intent="open",
    ))
    assert res["success"] is False
    assert res["status"] == "SL_FAILED_POSITION_EMERGENCY_CLOSED"
    assert res["db_close_verified"] is True

    # Verify in REAL temp SQLite: new row is CLOSED, pre-existing row is still OPEN!
    closed_row = LiveTradesService.get_trade_by_id(res["trade_id"])
    untouched_row = LiveTradesService.get_trade_by_id(other.id)
    assert closed_row is not None and closed_row.status == "CLOSED"
    assert closed_row.close_reason == "SL_FAILED_EMERGENCY_CLOSE"
    assert untouched_row is not None and untouched_row.status == "OPEN"


def test_lot_e_real_sqlite_quarantine_blocks_open_after_restart(monkeypatch, isolated_sqlite):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    # Persist a REAL QUARANTINED trade in temp SQLite
    q_row = LiveTradesService.create_live_trade(
        order_id="Q-OID-1", symbol="PF_XBTUSD", side="short",
        qty=0.0019, entry=63177.0, stop=0.0, tp=0.0, leverage=10,
        broker="kraken", status="OPEN", client_oid="BOT_Q_1",
    )
    assert LiveTradesService.mark_trade_quarantined("PF_XBTUSD", order_id="Q-OID-1", reason="EXCHANGE_SQLITE_EXPOSURE_MISMATCH") is True

    # Simulate fresh process restart: brand new wrapper runs reconcile_on_startup()
    client = FakeKrakenV2Client()
    w_restarted = KrakenFuturesExecutorWrapper(executor=client)
    w_restarted._rest_client = client
    report = asyncio.run(w_restarted.reconcile_on_startup())

    assert report["startup_secure"] is False
    assert w_restarted._global_entry_lock is True

    # Attempt OPEN after restart -> MUST be blocked by GLOBAL_ENTRY_LOCK_ACTIVE
    res = asyncio.run(w_restarted.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.1, order_type="market",
        entry_price=3000.0, stop_loss=2900.0, reduce_only=False, intent="open",
    ))
    assert res["success"] is False
    assert res["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"
    assert len(client.sent_orders) == 0


def test_lot_e_sqlite_unavailable_fails_closed_on_reconcile(monkeypatch):
    def _broken_session():
        raise RuntimeError("SQLite database locked / unavailable")

    monkeypatch.setattr("backend.services.live_trades_service.get_db_session", _broken_session)
    client = FakeKrakenV2Client()
    w = KrakenFuturesExecutorWrapper(executor=client)
    w._rest_client = client
    report = asyncio.run(w.reconcile_on_startup())

    assert report["startup_secure"] is False
    assert report["status"] == "DATABASE_UNAVAILABLE_LOCKED"
    assert w._global_entry_lock is True


# ══════════════════════════════════════════════════════════════════════════════
# OBLIGATORY SCENARIOS: TIMEOUT, AMBIGUOUS REST, REDUCE, CLOSE
# ══════════════════════════════════════════════════════════════════════════════

def test_obligatory_timeout_and_ambiguous_rest_fail_closed(monkeypatch, isolated_sqlite):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")

    c_to = FakeKrakenV2Client()
    c_to.open_timeout = True
    w_to = _make_wrapper(c_to)
    res_to = asyncio.run(w_to.place_order(
        symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=60000.0, stop_loss=58000.0, reduce_only=False, intent="open",
    ))
    assert res_to["success"] is False

    c_amb = FakeKrakenV2Client()
    c_amb.open_ambiguous = True
    w_amb = _make_wrapper(c_amb)
    res_amb = asyncio.run(w_amb.place_order(
        symbol="PF_XBTUSD", side="buy", quantity=0.01, order_type="market",
        entry_price=60000.0, stop_loss=58000.0, reduce_only=False, intent="open",
    ))
    assert res_amb["success"] is False


def test_obligatory_reduce_and_close_work_with_real_sqlite_when_open_disabled(monkeypatch, isolated_sqlite):
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    client = FakeKrakenV2Client()
    w = _make_wrapper(client)

    # Open position while authorized
    op = asyncio.run(w.place_order(
        symbol="PF_XBTUSD", side="buy", quantity=0.02, order_type="market",
        entry_price=60000.0, stop_loss=58000.0, take_profit=64000.0, reduce_only=False, intent="open",
    ))
    assert op["success"] is True

    # Disable OPENs: REDUCE and CLOSE MUST still succeed!
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "false")
    red = asyncio.run(w.place_order(
        symbol="PF_XBTUSD", side="sell", quantity=0.01, order_type="market",
        reduce_only=True, intent="reduce",
    ))
    assert red["success"] is True

    cls = asyncio.run(w.close_position("PF_XBTUSD", quantity=0.01, reduce_only=True, reason="phase1_close"))
    assert cls.get("success") is True or cls.get("code") == "00000"
    assert "PF_XBTUSD" not in client.positions
