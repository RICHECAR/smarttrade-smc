import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock

import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.db.session import SessionLocal
from backend.models.live_trade import LiveTrade
from smc_futures.api_smc_engine import auto_archive_failed_loop

@pytest.mark.asyncio
async def test_auto_archive_failed_loop():
    """Test that the loop archives trades older than 5 minutes and leaves newer ones alone."""
    db = SessionLocal()
    # clean up previous test data if any
    db.query(LiveTrade).filter(LiveTrade.symbol == "TEST_ARCHIVE").delete()
    db.commit()

    old_failed = LiveTrade(
        id="test-old-failed-" + str(datetime.now().timestamp()),
        order_id="old-ord-" + str(datetime.now().timestamp()),
        broker="kraken",
        symbol="TEST_ARCHIVE",
        side="long",
        qty=1.0,
        entry=100.0,
        stop=90.0,
        tp=110.0,
        leverage=10,
        status="FAILED",
        ts_open=datetime.now(timezone.utc) - timedelta(minutes=10)
    )
    new_failed = LiveTrade(
        id="test-new-failed-" + str(datetime.now().timestamp()),
        order_id="new-ord-" + str(datetime.now().timestamp()),
        broker="kraken",
        symbol="TEST_ARCHIVE",
        side="short",
        qty=1.0,
        entry=100.0,
        stop=110.0,
        tp=90.0,
        leverage=10,
        status="FAILED",
        ts_open=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    db.add(old_failed)
    db.add(new_failed)
    db.commit()
    
    # mock asyncio.sleep to break the loop
    with patch("smc_futures.api_smc_engine.asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
        with patch("smc_futures.api_smc_engine.ws_manager.broadcast", new_callable=AsyncMock) as mock_ws:
            try:
                await auto_archive_failed_loop(interval_sec=0)
            except asyncio.CancelledError:
                pass
                
    db.refresh(old_failed)
    db.refresh(new_failed)
    
    assert old_failed.archived == 1, "Old FAILED trade should be archived"
    assert old_failed.archive_reason == "auto_failed_cleanup"
    assert new_failed.archived == 0, "New FAILED trade should remain unarchived"
    
    # We broadcast two messages per batch of archived trades
    assert mock_ws.call_count == 2
    
    db.query(LiveTrade).filter(LiveTrade.symbol == "TEST_ARCHIVE").delete()
    db.commit()
    db.close()
