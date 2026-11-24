"""
Test Position Close/Reduce Functionality

Tests:
1. PositionManager.close_position_market()
2. PositionManager.reduce_position_market()
3. LiveTradesService.add_fill()
4. WebSocket broadcasts

Usage:
    # From project root
    python -m pytest tests/test_position_close_reduce.py -v
    
    # Or run directly
    python tests/test_position_close_reduce.py

Author: SMC Trading Bot Framework
"""

import asyncio
import logging
import sys
import pytest
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def test_imports():
    """Test all required imports work."""
    print("\n═══ TEST: Imports ═══")
    
    from smc_futures.engine.execution.position_manager import PositionManager
    from backend.services.live_trades_service import LiveTradesService, init_live_trades_db
    from backend.models.trade_fill import TradeFill
    from backend.services.trade_persistence_hook import on_trade_closed, on_trade_reduced
    from backend.ws.manager import ws_manager
    
    print("✓ All imports successful")
    return True


def test_database_init(memory_live_trades_db):
    """Test database tables are created in an isolated in-memory SQLite DB."""
    print("\n═══ TEST: Database Init ═══")
    from sqlalchemy import inspect

    eng, _ = memory_live_trades_db
    tables = inspect(eng).get_table_names()
    assert "live_trades" in tables and "trade_fills" in tables
    print("✓ Database tables initialized in-memory (live_trades + trade_fills)")
    return True


def test_live_trades_service_methods():
    """Test LiveTradesService has required methods."""
    print("\n═══ TEST: LiveTradesService Methods ═══")
    
    from backend.services.live_trades_service import LiveTradesService
    
    methods = [
        'create_live_trade',
        'close_live_trade',
        'add_fill',
        'get_open_trades',
        'get_open_trade_for_symbol',
        'get_fills_for_trade',
        'get_trade_by_order_id',
        'get_trade_by_id',
    ]
    
    for method in methods:
        assert hasattr(LiveTradesService, method), f"Missing method: {method}"
        print(f"  ✓ {method}()")
    
    print("✓ All LiveTradesService methods present")
    return True


def test_ws_manager_methods():
    """Test WebSocket manager has broadcast methods."""
    print("\n═══ TEST: WebSocket Manager Methods ═══")
    
    from backend.ws.manager import ws_manager
    
    methods = [
        'broadcast',
        'broadcast_trade_opened',
        'broadcast_trade_closed',
        'broadcast_trade_reduced',
    ]
    
    for method in methods:
        assert hasattr(ws_manager, method), f"Missing method: {method}"
        print(f"  ✓ {method}()")
    
    print("✓ All WebSocket broadcast methods present")
    return True


def test_trade_fill_model():
    """Test TradeFill model structure."""
    print("\n═══ TEST: TradeFill Model ═══")
    
    from backend.models.trade_fill import TradeFill
    
    # Check columns
    columns = ['id', 'trade_id', 'order_id', 'action', 'qty', 'price', 'fee', 'pnl', 'ts']
    
    for col in columns:
        assert hasattr(TradeFill, col), f"Missing column: {col}"
        print(f"  ✓ {col}")
    
    # Check to_dict method
    assert hasattr(TradeFill, 'to_dict')
    print("  ✓ to_dict()")
    
    print("✓ TradeFill model structure verified")
    return True


def test_position_manager_class():
    """Test PositionManager class structure."""
    print("\n═══ TEST: PositionManager Class ═══")
    
    from smc_futures.engine.execution.position_manager import PositionManager
    
    methods = [
        'close_position_market',
        'reduce_position_market',
        '_get_position',
        '_validate_reduce_qty',
        '_execute_close_order',
    ]
    
    for method in methods:
        assert hasattr(PositionManager, method), f"Missing method: {method}"
        print(f"  ✓ {method}()")
    
    print("✓ PositionManager class structure verified")
    return True


@pytest.mark.asyncio
async def test_add_fill_flow(memory_live_trades_db):
    """Test the add_fill flow with isolated in-memory DB."""
    print("\n═══ TEST: Add Fill Flow ═══")
    
    from backend.services.live_trades_service import LiveTradesService
    from backend.models.live_trade import LiveTrade
    
    # Create a test trade first
    test_order_id = f"TEST_FILL_{datetime.now().timestamp()}"
    
    trade = LiveTradesService.create_live_trade(
        order_id=test_order_id,
        symbol="BTCUSDT",
        side="long",
        qty=0.001,
        entry=50000.0,
        stop=49000.0,
        tp=52000.0,
        leverage=10,
    )
    
    if not trade:
        print("✗ Failed to create test trade")
        return False
    
    print(f"  ✓ Created test trade: {trade.id[:8]}...")
    
    # Add a REDUCE fill
    fill = LiveTradesService.add_fill(
        trade_id=trade.id,
        action="REDUCE",
        qty=0.0005,
        price=51000.0,
        fee=0.03,
        pnl=0.50,
        order_id=f"FILL_{test_order_id}_1",
    )
    
    if not fill:
        print("✗ Failed to add reduce fill")
        return False
    
    print(f"  ✓ Added REDUCE fill: {fill.id[:8]}...")
    
    # Verify fill
    fills = LiveTradesService.get_fills_for_trade(trade.id)
    assert len(fills) >= 1, "No fills found"
    print(f"  ✓ Retrieved {len(fills)} fill(s) for trade")
    
    # Get updated trade
    updated_trade = LiveTradesService.get_trade_by_id(trade.id)
    print(f"  ✓ Trade PnL after fill: {updated_trade.pnl}")
    print(f"  ✓ Trade fee after fill: {updated_trade.fee}")
    
    # Add a CLOSE fill
    close_fill = LiveTradesService.add_fill(
        trade_id=trade.id,
        action="CLOSE",
        qty=0.0005,
        price=52000.0,
        fee=0.03,
        pnl=1.00,
        order_id=f"FILL_{test_order_id}_2",
    )
    
    if not close_fill:
        print("✗ Failed to add close fill")
        return False
    
    print(f"  ✓ Added CLOSE fill: {close_fill.id[:8]}...")
    
    # Verify trade is now CLOSED
    final_trade = LiveTradesService.get_trade_by_id(trade.id)
    assert final_trade.status == "CLOSED", f"Trade status is {final_trade.status}, expected CLOSED"
    print(f"  ✓ Trade status: {final_trade.status}")
    print(f"  ✓ Trade total PnL: {final_trade.pnl}")
    print(f"  ✓ Trade result: {final_trade.result}")
    
    print("✓ Add Fill flow verified")
    return True


def run_all_tests():
    """Run all tests in standalone script mode using an isolated in-memory SQLite DB."""
    import os
    from backend.db.session import (
        configure_database,
        get_engine,
        get_session_factory,
        reset_database_configuration,
    )
    from backend.services.live_trades_service import (
        init_live_trades_db,
        set_live_trades_session_factory,
    )

    print("\n" + "═" * 50)
    print(" POSITION CLOSE/REDUCE TESTS")
    print("═" * 50)

    os.environ["SMARTTRADE_TEST_MODE"] = "1"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    configure_database("sqlite:///:memory:")
    eng = get_engine()
    session_factory = get_session_factory()
    set_live_trades_session_factory(session_factory)
    init_live_trades_db(target_engine=eng)
    db_ctx = (eng, session_factory)

    sync_tests = [
        ("test_imports", lambda: test_imports()),
        ("test_database_init", lambda: test_database_init(db_ctx)),
        ("test_live_trades_service_methods", lambda: test_live_trades_service_methods()),
        ("test_ws_manager_methods", lambda: test_ws_manager_methods()),
        ("test_trade_fill_model", lambda: test_trade_fill_model()),
        ("test_position_manager_class", lambda: test_position_manager_class()),
    ]

    passed = 0
    failed = 0

    try:
        for test_name, test_fn in sync_tests:
            try:
                if test_fn():
                    passed += 1
            except Exception as e:
                print(f"✗ {test_name} FAILED: {e}")
                failed += 1

        # Async test
        try:
            if asyncio.run(test_add_fill_flow(db_ctx)):
                passed += 1
        except Exception as e:
            print(f"✗ test_add_fill_flow FAILED: {e}")
            failed += 1
    finally:
        set_live_trades_session_factory(None)
        reset_database_configuration()

    # Summary
    print("\n" + "═" * 50)
    print(f" RESULTS: {passed} passed, {failed} failed")
    print("═" * 50)

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
