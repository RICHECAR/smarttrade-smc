import pytest
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.session import Base
from backend.models.persisted_position import PersistedPosition
from backend.models.live_trade import LiveTrade
from backend.models.analytics import TradeAnalytics

from backend.services.trade_ledger_reconciler import TradeLedgerReconciler


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Use an in-memory SQLite DB for reconciliation tests."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # Patch SessionLocal used by the reconciler
    import backend.services.trade_ledger_reconciler as reconciler_module
    monkeypatch.setattr(reconciler_module, "SessionLocal", Session)

    return Session


def test_db_open_no_exchange_in_dry_run(in_memory_db, monkeypatch):
    session = in_memory_db()
    # Create an open persisted position + corresponding trade
    pos = PersistedPosition(
        broker="kraken",
        symbol="BTCUSDT",
        side="long",
        size=1.0,
        entry_price=100.0,
        entry_time=datetime.now(timezone.utc),
        status="OPEN",
    )
    session.add(pos)
    trade = LiveTrade(
        order_id="o1",
        symbol="BTCUSDT",
        side="long",
        qty=1.0,
        entry=100.0,
        stop=90.0,
        tp=110.0,
        leverage=10,
        status="OPEN",
        ts_open=datetime.now(timezone.utc),
    )
    session.add(trade)
    session.commit()

    # Force exchange to report no open positions
    monkeypatch.setattr(TradeLedgerReconciler, "_fetch_exchange_open_positions", lambda self: {})

    reconciler = TradeLedgerReconciler()
    report = reconciler.reconcile(dry_run=True)

    assert report["summary"]["issues_found"] > 0
    # No change in DB for dry_run
    trade_db = session.query(LiveTrade).filter_by(id=trade.id).one()
    assert trade_db.status == "OPEN"


def test_db_open_no_exchange_in_fix_mode(in_memory_db, monkeypatch):
    session = in_memory_db()
    pos = PersistedPosition(
        broker="kraken",
        symbol="BTCUSDT",
        side="long",
        size=1.0,
        entry_price=100.0,
        entry_time=datetime.now(timezone.utc),
        status="OPEN",
    )
    session.add(pos)
    trade = LiveTrade(
        order_id="o2",
        symbol="BTCUSDT",
        side="long",
        qty=1.0,
        entry=100.0,
        stop=90.0,
        tp=110.0,
        leverage=10,
        status="OPEN",
        ts_open=datetime.now(timezone.utc),
    )
    session.add(trade)
    session.commit()

    monkeypatch.setattr(TradeLedgerReconciler, "_fetch_exchange_open_positions", lambda self: {})

    reconciler = TradeLedgerReconciler()
    report = reconciler.reconcile(dry_run=False)

    assert report["summary"]["issues_fixed"] > 0
    trade_db = session.query(LiveTrade).filter_by(id=trade.id).one()
    assert trade_db.status == "CLOSED"
    assert trade_db.close_reason == "RECONCILER"


def test_closed_trade_missing_analytics_is_created(in_memory_db, monkeypatch):
    session = in_memory_db()
    # Closed trade without analytics
    trade = LiveTrade(
        order_id="o3",
        symbol="BTCUSDT",
        side="long",
        qty=1.0,
        entry=100.0,
        stop=90.0,
        tp=110.0,
        leverage=10,
        status="CLOSED",
        ts_open=datetime.now(timezone.utc),
        ts_close=datetime.now(timezone.utc),
        pnl=10.0,
        close_price=110.0,
    )
    session.add(trade)
    session.commit()

    # No exchange interaction needed for this case
    monkeypatch.setattr(TradeLedgerReconciler, "_fetch_exchange_open_positions", lambda self: {})

    reconciler = TradeLedgerReconciler()
    report = reconciler.reconcile(dry_run=False)

    assert report["summary"]["issues_fixed"] > 0
    analytics = session.query(TradeAnalytics).filter_by(trade_id=trade.id).first()
    assert analytics is not None


# ═══════════════════════════════════════════════════════════════════════════════
# EDGE CASE TESTS — L2 Audit additions
# ═══════════════════════════════════════════════════════════════════════════════


def test_close_trade_in_db_preserves_none_pnl(in_memory_db, monkeypatch):
    """
    L2 FIX: _close_trade_in_db must NOT set pnl=0.0 when pnl is None.
    This avoids polluting stats with false break-even trades.
    """
    session = in_memory_db()
    # Need a PersistedPosition so the reconciler detects an open DB position
    pos = PersistedPosition(
        broker="kraken",
        symbol="BTCUSDT",
        side="long",
        size=1.0,
        entry_price=100.0,
        entry_time=datetime.now(timezone.utc),
        status="OPEN",
    )
    session.add(pos)
    trade = LiveTrade(
        order_id="o-edge-1",
        symbol="BTCUSDT",
        side="long",
        qty=1.0,
        entry=100.0,
        stop=90.0,
        tp=110.0,
        leverage=10,
        status="OPEN",
        ts_open=datetime.now(timezone.utc),
        pnl=None,  # Unknown PnL
    )
    session.add(trade)
    session.commit()

    monkeypatch.setattr(TradeLedgerReconciler, "_fetch_exchange_open_positions", lambda self: {})

    reconciler = TradeLedgerReconciler()
    report = reconciler.reconcile(dry_run=False)

    trade_db = session.query(LiveTrade).filter_by(id=trade.id).one()
    assert trade_db.status == "CLOSED"
    # CRITICAL: pnl must remain None, NOT 0.0
    assert trade_db.pnl is None, f"pnl should be None but got {trade_db.pnl}"


def test_close_trade_in_db_preserves_existing_pnl(in_memory_db, monkeypatch):
    """
    When a trade already has a PnL value (e.g., from fills), _close_trade_in_db
    must NOT overwrite it.
    """
    session = in_memory_db()
    pos = PersistedPosition(
        broker="kraken",
        symbol="ETHUSDT",
        side="short",
        size=5.0,
        entry_price=2000.0,
        entry_time=datetime.now(timezone.utc),
        status="OPEN",
    )
    session.add(pos)
    trade = LiveTrade(
        order_id="o-edge-2",
        symbol="ETHUSDT",
        side="short",
        qty=5.0,
        entry=2000.0,
        stop=2100.0,
        tp=1800.0,
        leverage=10,
        status="OPEN",
        ts_open=datetime.now(timezone.utc),
        pnl=123.45,  # PnL from accumulated fills
    )
    session.add(trade)
    session.commit()

    monkeypatch.setattr(TradeLedgerReconciler, "_fetch_exchange_open_positions", lambda self: {})

    reconciler = TradeLedgerReconciler()
    report = reconciler.reconcile(dry_run=False)

    trade_db = session.query(LiveTrade).filter_by(id=trade.id).one()
    assert trade_db.status == "CLOSED"
    assert trade_db.pnl == 123.45, f"pnl should be preserved at 123.45 but got {trade_db.pnl}"


def test_trade_with_zero_pnl_is_excluded_from_pollution(in_memory_db, monkeypatch):
    """
    Verify that a trade closed with pnl=None doesn't contaminate stats.
    (Not a reconciler test per se, but validates the fix's downstream effect.)
    """
    session = in_memory_db()
    # Trade 1: real trade with known PnL
    trade1 = LiveTrade(
        order_id="o-stats-1",
        symbol="BTCUSDT",
        side="long",
        qty=1.0,
        entry=100.0,
        stop=90.0,
        tp=110.0,
        leverage=10,
        status="CLOSED",
        ts_open=datetime.now(timezone.utc),
        ts_close=datetime.now(timezone.utc),
        pnl=50.0,
        result="win",
    )
    # Trade 2: reconciler-closed with unknown PnL
    trade2 = LiveTrade(
        order_id="o-stats-2",
        symbol="ETHUSDT",
        side="short",
        qty=1.0,
        entry=2000.0,
        stop=2100.0,
        tp=1800.0,
        leverage=10,
        status="CLOSED",
        ts_open=datetime.now(timezone.utc),
        ts_close=datetime.now(timezone.utc),
        pnl=None,  # Unknown — must be excluded from stats
        close_reason="RECONCILER",
    )
    session.add_all([trade1, trade2])
    session.commit()

    # Query as stats.py does: pnl IS NOT NULL
    closed_with_pnl = (
        session.query(LiveTrade)
        .filter(LiveTrade.status == "CLOSED", LiveTrade.pnl != None)
        .all()
    )
    assert len(closed_with_pnl) == 1
    assert closed_with_pnl[0].pnl == 50.0
