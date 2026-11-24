"""
Integration test for Multi-Broker execution and SQLite trade_orders persistence.
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, patch
from sqlalchemy import create_engine, text

from backend.services.multi_order_executor import MultiOrderExecutor, BrokerOrderResult
import uuid

@pytest.fixture
def mock_executors():
    bitget_mock = AsyncMock()
    bitget_mock.place_order.return_value = {
        "success": True,
        "entry_order_id": f"bitget_mock_{uuid.uuid4().hex[:8]}",
        "price_executed": 50000.0,
        "quantity": 0.002
    }
    
    kraken_mock = AsyncMock()
    kraken_mock.place_order.return_value = {
        "success": True,
        "entry_order_id": f"kraken_mock_{uuid.uuid4().hex[:8]}",
        "price_executed": 50000.0,
        "quantity": 0.002
    }
    
    return bitget_mock, kraken_mock

@pytest.mark.asyncio
async def test_dual_best_effort_persistence(mock_executors, monkeypatch):
    bitget_mock, kraken_mock = mock_executors
    
    # Mock OS environ
    monkeypatch.setenv("BITGET_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("MULTI_EXEC_POLICY", "DUAL_BEST_EFFORT")

    from backend.db.session import engine
    from backend.db.migrations import ensure_schema_up_to_date
    import os
    # Ensure migrations are applied on the default test DB
    from backend.db.session import DATABASE_URL
    ensure_schema_up_to_date(DATABASE_URL)
    
    executor = MultiOrderExecutor()
    executor.set_bitget_executor(bitget_mock)
    executor.set_kraken_executor(kraken_mock)
    
    signal_id = f"test_signal_{uuid.uuid4().hex[:8]}"
    
    results = await executor.execute_on_brokers(
        symbol="BTCUSDT",
        direction="long",
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit=52000.0,
        quantity=0.002,
        leverage=10,
        order_type="market",
        signal_id=signal_id
    )
    
    assert results["bitget"].success is True
    assert results["kraken"].success is True
    
    # Verify the database
    from backend.db.session import engine
    with engine.connect() as conn:
        result = conn.execute(text("SELECT * FROM trade_orders WHERE signal_id = :sid"), {"sid": signal_id}).fetchall()
        
        # Clean up immediately after reading
        conn.execute(text("DELETE FROM trade_orders WHERE signal_id = :sid"), {"sid": signal_id})
        conn.commit()
        
        assert len(result) == 2, f"Expected 2 trade_orders, got {len(result)}"
        
        brokers_saved = {row._mapping["broker"] for row in result}
        assert brokers_saved == {"bitget", "kraken"}
        
        for row in result:
            mapping = row._mapping
            assert mapping["status"] == "OPEN"
            assert mapping["qty"] == 0.002
            assert mapping["symbol"] != ""  # basic check
