"""
Phase 1.6 / Step 4 — P2-2 POST /order/test SQLite Isolation Suite (100% Offline)
===============================================================================
Covers all 10 mandatory offline tests for P2-2:
  1. dry_run=True -> zero SQLite writes (persisted_to_db is False).
  2. Real data/trades.db SHA-256 strictly unchanged before/after.
  3. Zero calls to on_trade_opened().
  4. Zero calls to on_position_opened().
  5. Zero fake OPEN rows in live_trades.
  6. Zero fake rows in persisted_positions.
  7. Zero real REST/WS/order execution calls (sockets hard-blocked).
  8. Exception during dry_run simulation creates zero persistent rows.
  9. Route security controls remain active (extra='forbid', ALLOW_TEST_EXECUTION guard).
 10. Non dry_run modes preserve guards (ALLOW_TEST_EXECUTION=0, ready_count=0) and public signatures.

Strict isolation:
  - External sockets hard-blocked.
  - Temporary SQLite DB via pytest `tmp_path` ONLY; real DB only read-hashed.
"""

import hashlib
import inspect
import os
import pathlib
import socket
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


class _ExternalNetworkBlocked(RuntimeError):
    pass


_ORIG_CONNECT = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) and address else ""
    if host not in ("127.0.0.1", "::1"):
        raise _ExternalNetworkBlocked(f"External network blocked in Step 4 P2-2: {address}")
    return _ORIG_CONNECT(self, address)


def _blocked_external(*args, **kwargs):
    raise _ExternalNetworkBlocked(f"External network blocked in Step 4 P2-2: {args}")


socket.socket.connect = _guarded_connect
socket.create_connection = _blocked_external
socket.getaddrinfo = _blocked_external

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["SMARTTRADE_TEST_MODE"] = "1"

from backend.db.base import Base
from backend.models.live_trade import LiveTrade  # noqa: F401 - register live_trades table in Base.metadata
from backend.routes.multi_broker import DryRunTestRequest, test_multi_broker_execution as _route_order_test


@pytest.fixture
def isolated_sqlite(tmp_path, monkeypatch):
    db_file = tmp_path / "test_step4_p2_2.db"
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
    yield db_file, engine
    engine.dispose()


class FakeMultiOrderExecutor:
    def __init__(self, kraken_enabled: bool = True, bitget_enabled: bool = False):
        self._kraken_enabled = kraken_enabled
        self._bitget_enabled = bitget_enabled
        self._kraken_executor = None  # Avoid any balance network call in dry-run tests
        self._bitget_executor = None
        self.execute_calls: List[Dict[str, Any]] = []

    def get_enabled_brokers(self) -> List[str]:
        out = []
        if self._bitget_enabled:
            out.append("bitget")
        if self._kraken_enabled:
            out.append("kraken")
        return out

    def is_broker_enabled(self, broker: str) -> bool:
        return self._kraken_enabled if broker == "kraken" else self._bitget_enabled

    def translate_symbol(self, symbol: str, broker: str) -> str:
        return "PF_XBTUSD" if broker == "kraken" else symbol

    async def execute_on_brokers(self, **kwargs):
        self.execute_calls.append(kwargs)
        return {}


@pytest.mark.asyncio
async def test_p2_2_1_dry_run_true_zero_sqlite_writes(isolated_sqlite, monkeypatch):
    """1. dry_run=True produces zero SQLite writes and returns persisted_to_db=False."""
    _, engine = isolated_sqlite
    fake_exec = FakeMultiOrderExecutor(kraken_enabled=True, bitget_enabled=False)
    monkeypatch.setattr("backend.services.multi_order_executor.get_multi_order_executor", lambda: fake_exec)

    req = DryRunTestRequest(symbol="BTCUSDT", quantity=0.001, side="buy", dry_run=True, brokers=["kraken"])
    res = await _route_order_test(req)

    assert res["dry_run_execution"]["status"] == "simulated"
    assert res["dry_run_execution"]["persisted_to_db"] is False
    assert res["dry_run_execution"]["dry_run_isolated"] is True
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM live_trades")).scalar() == 0


@pytest.mark.asyncio
async def test_p2_2_2_real_trades_db_sha256_strictly_unchanged(isolated_sqlite, monkeypatch):
    """2. Real data/trades.db SHA-256 is bit-for-bit identical before and after dry_run=True."""
    real_db = pathlib.Path("data/trades.db")
    sha_before = hashlib.sha256(real_db.read_bytes()).hexdigest()

    fake_exec = FakeMultiOrderExecutor(kraken_enabled=True, bitget_enabled=False)
    monkeypatch.setattr("backend.services.multi_order_executor.get_multi_order_executor", lambda: fake_exec)

    req = DryRunTestRequest(symbol="ETHUSDT", quantity=0.01, side="buy", dry_run=True, brokers=["kraken"])
    await _route_order_test(req)

    sha_after = hashlib.sha256(real_db.read_bytes()).hexdigest()
    assert sha_after == sha_before


@pytest.mark.asyncio
async def test_p2_2_3_zero_calls_to_on_trade_opened(isolated_sqlite, monkeypatch):
    """3. dry_run=True never calls on_trade_opened()."""
    trade_hook_calls: List[Any] = []

    async def _spy_on_trade_opened(*args, **kwargs):
        trade_hook_calls.append((args, kwargs))
        raise AssertionError("on_trade_opened MUST NOT be called during dry_run=True")

    monkeypatch.setattr("backend.services.trade_persistence_hook.on_trade_opened", _spy_on_trade_opened)
    fake_exec = FakeMultiOrderExecutor(kraken_enabled=True)
    monkeypatch.setattr("backend.services.multi_order_executor.get_multi_order_executor", lambda: fake_exec)

    req = DryRunTestRequest(symbol="BTCUSDT", quantity=0.001, side="buy", dry_run=True, brokers=["kraken"])
    res = await _route_order_test(req)

    assert len(trade_hook_calls) == 0
    assert res["dry_run_execution"]["persisted_to_db"] is False


@pytest.mark.asyncio
async def test_p2_2_4_zero_calls_to_on_position_opened(isolated_sqlite, monkeypatch):
    """4. dry_run=True never calls on_position_opened()."""
    pos_hook_calls: List[Any] = []

    def _spy_on_position_opened(*args, **kwargs):
        pos_hook_calls.append((args, kwargs))
        raise AssertionError("on_position_opened MUST NOT be called during dry_run=True")

    monkeypatch.setattr("backend.services.position_persistence_hook.on_position_opened", _spy_on_position_opened)
    fake_exec = FakeMultiOrderExecutor(kraken_enabled=True)
    monkeypatch.setattr("backend.services.multi_order_executor.get_multi_order_executor", lambda: fake_exec)

    req = DryRunTestRequest(symbol="BTCUSDT", quantity=0.001, side="sell", dry_run=True, brokers=["kraken"])
    res = await _route_order_test(req)

    assert len(pos_hook_calls) == 0
    assert res["dry_run_execution"]["persisted_to_db"] is False


@pytest.mark.asyncio
async def test_p2_2_5_zero_fake_open_trades_created(isolated_sqlite, monkeypatch):
    """5. dry_run=True creates zero fake OPEN trades in live_trades."""
    _, engine = isolated_sqlite
    fake_exec = FakeMultiOrderExecutor(kraken_enabled=True, bitget_enabled=True)
    monkeypatch.setattr("backend.services.multi_order_executor.get_multi_order_executor", lambda: fake_exec)

    req = DryRunTestRequest(symbol="SOLUSDT", quantity=0.5, side="buy", dry_run=True, brokers=["kraken", "bitget"])
    await _route_order_test(req)

    with engine.connect() as conn:
        open_cnt = conn.execute(text("SELECT COUNT(*) FROM live_trades WHERE UPPER(status)='OPEN'")).scalar()
        assert open_cnt == 0


@pytest.mark.asyncio
async def test_p2_2_6_zero_fake_persisted_positions_created(isolated_sqlite, monkeypatch):
    """6. dry_run=True creates zero fake rows in persisted_positions."""
    _, engine = isolated_sqlite
    fake_exec = FakeMultiOrderExecutor(kraken_enabled=True)
    monkeypatch.setattr("backend.services.multi_order_executor.get_multi_order_executor", lambda: fake_exec)

    req = DryRunTestRequest(symbol="BTCUSDT", quantity=0.001, side="buy", dry_run=True, brokers=["kraken"])
    await _route_order_test(req)

    with engine.connect() as conn:
        tables = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()]
        if "persisted_positions" in tables:
            assert conn.execute(text("SELECT COUNT(*) FROM persisted_positions")).scalar() == 0


@pytest.mark.asyncio
async def test_p2_2_7_zero_rest_ws_or_order_execution_calls(isolated_sqlite, monkeypatch):
    """7. dry_run=True makes zero REST/WS/order execution calls."""
    fake_exec = FakeMultiOrderExecutor(kraken_enabled=True)
    monkeypatch.setattr("backend.services.multi_order_executor.get_multi_order_executor", lambda: fake_exec)

    req = DryRunTestRequest(symbol="BTCUSDT", quantity=0.001, side="buy", dry_run=True, brokers=["kraken"])
    res = await _route_order_test(req)

    assert len(fake_exec.execute_calls) == 0
    assert res["test_type"] == "dry_run"


@pytest.mark.asyncio
async def test_p2_2_8_exception_during_dry_run_simulation_leaves_zero_persistent_data(isolated_sqlite, monkeypatch):
    """8. Exception inside dry_run simulation returns persisted_to_db=False and leaves 0 SQLite rows."""
    _, engine = isolated_sqlite
    fake_exec = FakeMultiOrderExecutor(kraken_enabled=True)
    monkeypatch.setattr("backend.services.multi_order_executor.get_multi_order_executor", lambda: fake_exec)
    monkeypatch.setattr("uuid.uuid4", lambda: (_ for _ in ()).throw(RuntimeError("uuid boom")) if False else __import__("uuid").UUID(int=1))

    # Force exception inside dry_run loop via non-iterable brokers override after request construction
    req = DryRunTestRequest(symbol="BTCUSDT", quantity=0.001, side="buy", dry_run=True, brokers=["kraken"])
    object.__setattr__(req, "side", None)  # Causes req.side.lower() -> AttributeError inside try block
    res = await _route_order_test(req)

    assert "error" in res["dry_run_execution"]
    assert res["dry_run_execution"]["persisted_to_db"] is False
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM live_trades")).scalar() == 0


@pytest.mark.asyncio
async def test_p2_2_9_route_security_controls_remain_active(isolated_sqlite, monkeypatch):
    """9. DryRunTestRequest rejects extra fields (extra='forbid') and blocks live test without ALLOW_TEST_EXECUTION."""
    with pytest.raises(ValidationError):
        DryRunTestRequest(symbol="BTCUSDT", quantity=0.001, side="buy", dry_run=True, injected_flag=True)

    monkeypatch.setenv("ALLOW_TEST_EXECUTION", "0")
    fake_exec = FakeMultiOrderExecutor(kraken_enabled=True)
    monkeypatch.setattr("backend.services.multi_order_executor.get_multi_order_executor", lambda: fake_exec)

    req = DryRunTestRequest(symbol="BTCUSDT", quantity=0.001, side="buy", dry_run=False, brokers=["kraken"])
    res = await _route_order_test(req)

    assert res["execution"]["status"] == "blocked"
    assert "ALLOW_TEST_EXECUTION" in res["execution"]["reason"]
    assert len(fake_exec.execute_calls) == 0


@pytest.mark.asyncio
async def test_p2_2_10_non_dry_run_preserves_ready_count_guard_and_public_signature(isolated_sqlite, monkeypatch):
    """10. Non-dry-run preserves ready_count==0 guard and public signature of test_multi_broker_execution."""
    sig = inspect.signature(_route_order_test)
    assert list(sig.parameters.keys()) == ["req"]

    monkeypatch.setenv("ALLOW_TEST_EXECUTION", "1")
    fake_exec = FakeMultiOrderExecutor(kraken_enabled=False, bitget_enabled=False)
    monkeypatch.setattr("backend.services.multi_order_executor.get_multi_order_executor", lambda: fake_exec)

    req = DryRunTestRequest(symbol="BTCUSDT", quantity=0.001, side="buy", dry_run=False)
    res = await _route_order_test(req)

    assert res["ready_count"] == 0
    assert res["execution"]["status"] == "blocked"
    assert "No brokers ready" in res["execution"]["reason"]
    assert len(fake_exec.execute_calls) == 0
