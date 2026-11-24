import pytest
import os
import importlib
from unittest.mock import patch, MagicMock, AsyncMock

# 1. Test Boot-up guard for CONFIRM_LIVE_MULTI_BROKER
def test_boot_guard_multi_broker():
    """Test that importing smc_config raises ValueError if CONFIRM_LIVE_MULTI_BROKER is missing in LIVE + dual broker."""
    with patch.dict(os.environ, {
        "TRADING_MODE": "live",
        "AUTO_EXECUTE": "true",
        "BITGET_ENABLED": "true",
        "KRAKEN_ENABLED": "true",
        "CONFIRM_LIVE_MULTI_BROKER": "false"
    }):
        with pytest.raises(ValueError, match="CONFIRM_LIVE_MULTI_BROKER=true"):
            import smc_futures.smc_config
            importlib.reload(smc_futures.smc_config)
            
    # Valid startup should not raise
    with patch.dict(os.environ, {
        "TRADING_MODE": "live",
        "AUTO_EXECUTE": "true",
        "BITGET_ENABLED": "true",
        "KRAKEN_ENABLED": "true",
        "CONFIRM_LIVE_MULTI_BROKER": "true"
    }):
        import smc_futures.smc_config
        importlib.reload(smc_futures.smc_config)

# 2. Test Kraken execution mode explicitly
def test_kraken_execution_enabled():
    """Ensure KRAKEN_EXECUTION_ENABLED strictly controls execution readiness."""
    from backend.services.multi_order_executor import is_kraken_executor_ready
    
    with patch.dict(os.environ, {"KRAKEN_EXECUTION_ENABLED": "false", "KRAKEN_ENABLED": "true"}):
        assert is_kraken_executor_ready() is False

    with patch.dict(os.environ, {"KRAKEN_EXECUTION_ENABLED": "true", "KRAKEN_ENABLED": "true"}):
        assert is_kraken_executor_ready() is True

# 3. Test External Close PNL Persistence
@pytest.mark.asyncio
async def test_external_close_pnl_persistence():
    """Test that trade.pnl is preserved when position is closed externally."""
    from backend.services.trade_persistence_hook import sync_with_exchange
    from backend.models.live_trade import LiveTrade
    
    trade = MagicMock(spec=LiveTrade)
    trade.id = "testing123"
    trade.symbol = "BTCUSDT"
    trade.broker = "bitget"
    trade.order_id = "test_order"
    trade.qty = 1.0
    trade.entry = 50000.0
    trade.pnl = 150.5  # Last known unrealized Pnl

    bitget_exec = AsyncMock()
    # Mock get_positions returning empty list (meaning position is closed externally)
    bitget_exec.get_positions.return_value = {"code": "00000", "data": []}
    # Mock get_historical_pnl returning the PnL we expect to be preserved
    bitget_exec.get_historical_pnl.return_value = {"pnl": 150.5, "fee": 0.5}
    
    kraken_exec = None
    
    with patch("backend.services.trade_persistence_hook.on_trade_closed", new_callable=AsyncMock) as mock_on_close:
        await sync_with_exchange(bitget_exec, kraken_exec, [trade])
        
        mock_on_close.assert_called_once()
        kwargs = mock_on_close.call_args.kwargs
        assert kwargs["trade_id"] == "testing123"
        assert kwargs["pnl"] == 150.5  # Preserved
        assert kwargs["qty"] == 1.0
        assert "ext_close_" in kwargs["order_id"]

# 4. Test Bitget Position Symbol Filtering
@pytest.mark.asyncio
async def test_bitget_position_filtering():
    """Test get_positions('ALL') filters out non-tracked symbols like ETHUSDC."""
    from backend.engine.execution.bitget_v2_executor import BitgetV2Executor
    
    executor = BitgetV2Executor("key", "sec", "pass")
    
    # Mock the HTTP GET response
    raw_response = {
        "code": "00000",
        "data": [
            {"symbol": "BTCUSDT", "total": "1.0", "holdSide": "long", "available": "1.0"},
            {"symbol": "ETHUSDC", "total": "10.0", "holdSide": "long", "available": "10.0"}  # Untracked symbol
        ]
    }
    executor._http_get = AsyncMock(return_value=raw_response)

    with patch("smc_futures.smc_config.SMC_CONFIG", {"SYMBOLS": ["BTCUSDT"]}):
        res = await executor.get_positions("ALL")
        assert res["code"] == "00000"
        positions = res["data"]
        # Only BTCUSDT should remain
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTCUSDT"
