import pytest
import os
from unittest.mock import patch, MagicMock

# 1. Test Live Multi-Broker Guard
def test_live_guard_multibroker_requires_confirm():
    """Ensure parse_config raises ValueError when CONFIRM_LIVE_MULTI_BROKER is missing."""
    import sys
    from importlib import reload
    import smc_futures.smc_config

    # Save original env
    orig_env = dict(os.environ)

    try:
        os.environ["TRADING_MODE"] = "live"
        os.environ["AUTO_EXECUTE"] = "true"
        os.environ["MIRROR_TRADING_ENABLED"] = "true"
        os.environ["CONFIRM_LIVE_MULTI_BROKER"] = "false"

        with pytest.raises(ValueError, match=r"\[LIVE_MULTI_BROKER\] REFUSED TO START!"):
            # Reloading the module triggers the block validation
            reload(smc_futures.smc_config)
            
        # Add confirmation, should pass
        os.environ["CONFIRM_LIVE_MULTI_BROKER"] = "true"
        reload(smc_futures.smc_config)

    finally:
        os.environ.clear()
        os.environ.update(orig_env)
        reload(smc_futures.smc_config)

# 2. Test Kraken Execution Isolation
@patch("smc_futures.orchestrator.BitgetFuturesExecutor")
@patch("smc_futures.engine.execution.kraken_executor.KrakenFuturesExecutor")
def test_kraken_execution_disabled_not_registered(mock_kraken_exec_cls, mock_bitget_exec_cls):
    orig_env = dict(os.environ)
    try:
        os.environ["KRAKEN_ENABLED"] = "true"
        os.environ["KRAKEN_EXECUTION_ENABLED"] = "false"
        os.environ["KRAKEN_API_KEY"] = "test"
        os.environ["KRAKEN_API_SECRET"] = "test"
        os.environ["BITGET_API_KEY"] = "test"
        os.environ["BITGET_API_SECRET"] = "test"
        os.environ["BITGET_PASSPHRASE"] = "test"
        
        # Mock executors
        mock_kraken_instance = MagicMock()
        mock_kraken_exec_cls.return_value = mock_kraken_instance
        
        from smc_futures.orchestrator import SMCFuturesOrchestrator
        orchestrator = SMCFuturesOrchestrator(
            symbol="BTCUSDT",
            live_trading=True
        )

        # Bitget should be registered
        assert "bitget" in orchestrator.executors
        # Kraken should NOT be registered for execution
        assert "kraken" not in orchestrator.executors
        
        # Now test with KRAKEN_EXECUTION_ENABLED = true
        os.environ["KRAKEN_EXECUTION_ENABLED"] = "true"
        orchestrator_exec = SMCFuturesOrchestrator(
            symbol="BTCUSDT",
            live_trading=True
        )
        assert "kraken" in orchestrator_exec.executors
        assert mock_kraken_instance == orchestrator_exec.multi_executor._kraken_executor

    finally:
        os.environ.clear()
        os.environ.update(orig_env)

# 3. Test Recovery Order ID Preservation and External Close
@pytest.mark.asyncio
@patch("backend.services.trade_persistence_hook.LiveTradesService")
@patch("backend.services.trade_persistence_hook.add_fill_async")
async def test_recovery_external_close_keeps_order_id(mock_add_fill, mock_lts):
    from backend.services.trade_persistence_hook import on_trade_closed
    from backend.models.live_trade import LiveTrade
    
    mock_add_fill.return_value = MagicMock()

    # Mock an existing trade in DB
    existing_trade = LiveTrade(id="trade-123", order_id="ord-abc", qty=1.5, entry=50000)
    mock_lts.get_trade_by_id.return_value = existing_trade

    # Test closing explicitly with NO qty (which is actually a bug case: qty=0)
    # The safety feature now falls back to the original trade qty instead of rejecting
    result_prevented = await on_trade_closed(
        trade_id="trade-123", close_price=51000, pnl=100, qty=0, raw_exchange={"note": "prevent qty 0"}
    )
    # Code now does fallback to original qty=1.5, so trade IS recorded
    assert result_prevented is not None
    assert mock_add_fill.call_count == 1

    # Reset mock for next scenario
    mock_add_fill.reset_mock()

    # Test valid external close (missing order_id, provides qty)
    result_success = await on_trade_closed(
        trade_id="trade-123", close_price=51000, pnl=100, qty=1.5, raw_exchange={"close_reason": "external_close_unknown_fill"}
    )
    
    # Should use the retrieved order_id
    mock_add_fill.assert_called_once()
    called_kwargs = mock_add_fill.call_args.kwargs
    assert called_kwargs["trade_id"] == "trade-123"
    assert called_kwargs["order_id"] == "ord-abc"  # Assert preservation happened
    assert called_kwargs["raw_response"]["close_reason"] == "external_close_unknown_fill"

# 4. Test Positions Symbol Filter
@pytest.mark.asyncio
async def test_positions_filter_by_symbol():
    from backend.engine.execution.bitget_v2_executor import BitgetV2Executor
    
    executor = BitgetV2Executor("key", "secret", "pass")
    
    mock_raw_positions = [
        {"symbol": "BTCUSDT", "total": "1.5", "holdSide": "long", "holdMode": "one_way_mode", "available": "1.5"},
        {"symbol": "ETHUSDT", "total": "10.0", "holdSide": "short", "holdMode": "one_way_mode", "available": "10.0"},
    ]
    
    from unittest.mock import AsyncMock
    executor._http_get = AsyncMock(return_value={"code": "00000", "data": mock_raw_positions})
    
    # 1. Fetch only BTCUSDT. Should isolate at python level.
    btc_result = await executor.get_positions("BTCUSDT")
    assert btc_result.get("data") is not None
    assert len(btc_result["data"]) == 1
    assert btc_result["data"][0]["symbol"] == "BTCUSDT"
    
    # 2. Fetch "ALL" symbols. Should return both.
    all_result = await executor.get_positions("ALL")
    assert len(all_result["data"]) == 2
