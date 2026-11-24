import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone

from backend.services.trade_reconciler import PositionSyncService
from backend.models.live_trade import LiveTrade

@pytest.fixture
def mock_executors():
    bitget = AsyncMock()
    kraken = AsyncMock()
    
    # Mock positions empty initially
    bitget.get_positions.return_value = {"code": "00000", "data": []}
    kraken.get_positions.return_value = {"data": []}
    
    return bitget, kraken

@pytest.mark.asyncio
async def test_external_close_reconciles_trade(mock_executors):
    bitget, kraken = mock_executors
    sync_service = PositionSyncService(bitget_executor=bitget, kraken_executor=kraken)
    
    # Mock open trade
    mock_trade = LiveTrade(
        id="test-123",
        symbol="BTCUSDT",
        broker="kraken",
        side="long",
        qty=1.0,
        entry=50000.0,
        stop=49000.0,
        tp=52000.0,
        status="OPEN",
        order_id="order-555"
    )
    
    # Mock fills fetched from Kraken
    # _fetch_kraken_fills calls self.kraken_executor.client.get_fills()
    kraken.client = MagicMock()
    kraken.client.get_fills.return_value = {
        "fills": [
            {
                "order_id": "order-555",
                "size": "1.0",
                "price": "51000",
                "fee": "-2.5",
                "realizedPnl": "1000.0"
            }
        ]
    }
    
    with patch("backend.services.trade_reconciler.LiveTradesService.get_open_trades", return_value=[mock_trade]):
        with patch("backend.services.trade_reconciler.close_live_trade_async") as mock_close:
            # Return a mock trade so broadcast_trade_event can call .to_dict()
            closed_trade = MagicMock()
            closed_trade.to_dict.return_value = {"id": "test-123", "status": "CLOSED"}
            mock_close.return_value = closed_trade
            
            with patch("backend.services.trade_reconciler.add_fill_async") as mock_add_fill:
                with patch("backend.services.trade_reconciler.broadcast_trade_event") as mock_broadcast:
                    await sync_service.sync_positions()
                    
                    # Fills should be recorded as REDUCE (not CLOSE — close is done via close_live_trade)
                    mock_add_fill.assert_called_once()
                    fill_kwargs = mock_add_fill.call_args[1]
                    assert fill_kwargs["action"] == "REDUCE"
                    assert fill_kwargs["pnl"] == 1000.0
                    
                    # Trade should be finalized via close_live_trade_async
                    # pnl=None because fills already accumulated PnL on the parent trade
                    mock_close.assert_called_once()
                    close_kwargs = mock_close.call_args[1]
                    assert close_kwargs["close_price"] == 51000.0
                    assert close_kwargs["pnl"] is None
                    assert close_kwargs["close_reason"] == "external_close"
                    
                    # WS broadcast should fire
                    mock_broadcast.assert_called_once_with("trade_closed", closed_trade)

@pytest.mark.asyncio
async def test_external_close_without_fills_sets_unknown(mock_executors):
    bitget, kraken = mock_executors
    sync_service = PositionSyncService(bitget_executor=bitget, kraken_executor=kraken)
    
    mock_trade = LiveTrade(
        id="test-123",
        symbol="BTCUSDT",
        broker="kraken",
        status="OPEN",
        entry=50000.0,
        stop=49000.0,
        tp=52000.0,
        qty=1.0,
        order_id="order-555",
    )
    
    # Mock empty fill response from Kraken
    kraken.client = MagicMock()
    kraken.client.get_fills.return_value = {"fills": []}
    
    with patch("backend.services.trade_reconciler.LiveTradesService.get_open_trades", return_value=[mock_trade]):
        with patch("backend.services.trade_reconciler.close_live_trade_async") as mock_close:
            closed_trade = MagicMock()
            closed_trade.to_dict.return_value = {"id": "test-123", "status": "CLOSED"}
            mock_close.return_value = closed_trade
            
            with patch("backend.services.trade_reconciler.broadcast_trade_event") as mock_broadcast:
                await sync_service.sync_positions()
                
                # Trade should be closed with NONE (unknown) pnl
                mock_close.assert_called_once()
                close_kwargs = mock_close.call_args[1]
                assert close_kwargs["pnl"] is None
                assert close_kwargs["close_reason"] == "external_close_unknown_fill"
                
                # WS broadcast should fire
                mock_broadcast.assert_called_once_with("trade_closed", closed_trade)


@pytest.mark.asyncio
async def test_no_double_close_when_fills_found(mock_executors):
    """Regression: ensure add_fill is NOT called with action=CLOSE 
    (only close_live_trade_async should finalize the close)."""
    bitget, kraken = mock_executors
    sync_service = PositionSyncService(bitget_executor=bitget, kraken_executor=kraken)

    mock_trade = LiveTrade(
        id="test-456",
        symbol="ETHUSDT",
        broker="kraken",
        side="short",
        qty=10.0,
        entry=2000.0,
        stop=2100.0,
        tp=1800.0,
        status="OPEN",
        order_id="order-999",
    )

    # Mock Kraken fills
    kraken.client = MagicMock()
    kraken.client.get_fills.return_value = {
        "fills": [
            {"order_id": "order-999", "size": "5.0", "price": "1950", "fee": "-1.0", "realizedPnl": "250.0"},
            {"order_id": "order-999", "size": "5.0", "price": "1900", "fee": "-1.0", "realizedPnl": "500.0"},
        ]
    }

    with patch("backend.services.trade_reconciler.LiveTradesService.get_open_trades", return_value=[mock_trade]):
        with patch("backend.services.trade_reconciler.close_live_trade_async") as mock_close:
            closed_trade = MagicMock()
            mock_close.return_value = closed_trade

            with patch("backend.services.trade_reconciler.add_fill_async") as mock_add_fill:
                with patch("backend.services.trade_reconciler.broadcast_trade_event"):
                    await sync_service.sync_positions()

                    # ALL fills should be REDUCE (no CLOSE action via add_fill)
                    assert mock_add_fill.call_count == 2
                    for call in mock_add_fill.call_args_list:
                        assert call[1]["action"] == "REDUCE"

                    # close_live_trade_async should be called EXACTLY ONCE
                    # pnl=None because fills already accumulated PnL on the parent trade
                    mock_close.assert_called_once()
                    assert mock_close.call_args[1]["pnl"] is None
                    assert mock_close.call_args[1]["close_reason"] == "external_close"
