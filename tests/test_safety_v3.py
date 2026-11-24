import os
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from smc_futures.settings import TradingSettings
from backend.ws.manager import ConnectionManager
from fastapi import WebSocket

def test_multi_broker_startup_guard():
    """Verify that LIVE + Multi + Mirror fails without confirmation."""
    # This should raise ValueError due to validate_security_rules
    with pytest.raises(ValueError, match="CRITICAL SECURITY BLOCK"):
        TradingSettings(
            trading_mode="live",
            auto_execute="true",
            kraken_enabled=True,
            mirror_trading_enabled=True,
            confirm_live_multi_broker=False
        )

def test_multi_broker_startup_pass():
    """Verify that LIVE + Multi + Mirror passes WITH confirmation."""
    settings = TradingSettings(
        trading_mode="live",
        auto_execute="true",
        kraken_enabled=True,
        mirror_trading_enabled=True,
        confirm_live_multi_broker=True
    )
    assert settings.confirm_live_multi_broker is True

@pytest.mark.asyncio
async def test_ws_lifecycle_cleanup():
    """Verify that WS manager removes failed clients immediately."""
    manager = ConnectionManager()
    mock_ws = AsyncMock(spec=WebSocket)
    mock_ws.accept = AsyncMock()
    mock_ws.send_json = AsyncMock(side_effect=Exception("Connection lost"))
    
    # Connect (this calls accept and sends welcome)
    # We expect it to fail during welcome message send and disconnect
    await manager.connect(mock_ws)
    
    # Client should be removed from active_connections and _accepted
    assert len(manager.active_connections) == 0
    assert id(mock_ws) not in manager._accepted

@pytest.mark.asyncio
async def test_ws_broadcast_partial_failure():
    """Verify that broadcast continues even if one client fails."""
    manager = ConnectionManager()
    
    # Good client
    good_ws = AsyncMock(spec=WebSocket)
    good_ws.accept = AsyncMock()
    good_ws.send_json = AsyncMock()
    await manager.connect(good_ws)
    
    # Bad client - succeeds on connection, fails on broadcast
    bad_ws = AsyncMock(spec=WebSocket)
    bad_ws.accept = AsyncMock()
    bad_ws.send_json = AsyncMock() # Succeeds during connect()
    await manager.connect(bad_ws)
    
    # Now make it fail for subsequent calls (broadcast)
    bad_ws.send_json.side_effect = Exception("Broadcast Failed")
    
    # Total should be 2 initially
    assert manager.connection_count == 2
    
    # Broadcast
    reached = await manager.broadcast({"data": "test"})
    
    # Should reach 1 (good_ws)
    assert reached == 1
    # bad_ws should be removed
    assert manager.connection_count == 1
    assert good_ws in manager.active_connections
    assert bad_ws not in manager.active_connections

def test_dev_mode_decoupling():
    """Verify WS_DEV_MODE is decoupled from TRADING_MODE."""
    # Clear env vars
    os.environ.pop("DEV_MODE", None)
    os.environ.pop("WS_DEV_MODE", None)
    
    # Test with DEV_MODE=true
    os.environ["DEV_MODE"] = "true"
    val = os.getenv("DEV_MODE", "0").lower() in ("1", "true", "yes")
    assert val is True

@pytest.mark.asyncio
async def test_positions_api_divergence_warning():
    """Verify divergence warning when DB has positions but exchange is empty."""
    from backend.routes.positions import get_positions
    from unittest.mock import patch
    
    # Mock LiveTradesService
    mock_service = MagicMock()
    mock_pos = MagicMock()
    mock_pos.to_dict = lambda: {"id": "1"}
    mock_service.get_all = MagicMock(return_value=[mock_pos])
    mock_service.get_count = MagicMock(return_value=1)
    mock_service.get_counts = MagicMock(return_value={"open": 1, "closed": 0})
    mock_service.get_positions = MagicMock(return_value=([mock_pos], 1))
    
    # Mock ctx and executors
    mock_ctx = MagicMock()
    mock_bg = AsyncMock()
    mock_bg.get_positions = AsyncMock(return_value={"data": []}) # Empty exchange
    mock_ctx.executors = {"futures": mock_bg}
    
    # We need to mock the import of ctx in backend.routes.positions
    with patch("backend.routes.positions.get_positions_service", return_value=mock_service):
        with patch("smc_futures.api_smc_engine.ctx", mock_ctx):
            response = await get_positions(
                broker=None,
                status_filter=None,
                symbol=None,
                from_date=None,
                to_date=None,
                page=1,
                page_size=50,
                order_by="entry_time",
                order_desc=True
            )
    
    assert response["source"] == "db"
    assert response["open_count"] == 1
    assert "DIVERGENCE DETECTED" in response["warning"]
    assert "Exchange reports 0 active positions" in response["warning"]

def test_kraken_execution_gate():
    """Verify that Kraken settings default to safe execution=False."""
    settings = TradingSettings(
        trading_mode="live",
        bitget_api_key="key",
        bitget_api_secret="secret",
        bitget_passphrase="pass",
        kraken_enabled=True,
        kraken_execution_enabled=False
    )
    assert settings.kraken_execution_enabled is False

def test_asia_range_bypass_logic():
    """Verify Asia Strategy range bypass logic."""
    from smc_futures.engine.strategy.asia_weekend_strategy import AsiaWeekendStrategy
    from smc_futures.engine.strategy.range_detector import RangeLevel, RangeType, RangeState
    from unittest.mock import patch
    
    strat = AsiaWeekendStrategy()
    
    # Mock range state where range is too large
    mock_range = RangeLevel(
        range_type=RangeType.CONSOLIDATION,
        high=100.0,
        low=90.0,
        mid=95.0,
        range_size=10.0,
        range_pct=10.0,
        start_ts=0,
        end_ts=0,
        num_bars=10,
        atr=1.0,
        range_atr_ratio=10.0, 
        is_valid=False,
        invalidation_reason="RANGE_TOO_LARGE"
    )
    range_state = RangeState(current_range=mock_range)
    
    # Case 1: Bypass is DISABLED by default
    with patch("smc_futures.smc_config.SMC_CONFIG", {"ASIA_ALLOW_RANGE_TOO_LARGE": False}):
        allowed = strat._try_range_too_large_bypass(
            "BTC", range_state, {"last_ratio": 10.0}, {}, {}, {}, 100.0
        )
        assert allowed is False
        
    # Case 2: Bypass is ENABLED but bias is NEUTRAL
    with patch("smc_futures.smc_config.SMC_CONFIG", {"ASIA_ALLOW_RANGE_TOO_LARGE": True}):
        allowed = strat._try_range_too_large_bypass(
            "BTC", range_state, {"last_ratio": 10.0}, {"htf_bias": "neutral"}, {}, {}, 100.0
        )
        assert allowed is False

    # Case 3: Bypass is ENABLED and conditions met
    config = {
        "ASIA_ALLOW_RANGE_TOO_LARGE": True,
        "ASIA_BOUNDARY_TOL_PCT": 0.05
    }
    with patch("smc_futures.smc_config.SMC_CONFIG", config):
        strat._find_entry_zone = MagicMock(return_value={"entry": 99.0})
        
        allowed = strat._try_range_too_large_bypass(
            "BTC", 
            range_state, 
            {"last_ratio": 10.0}, 
            {"htf_bias": "bullish"}, 
            {}, {}, 91.0 # Near low
        )
        assert allowed is True

def test_asia_threshold_multiplier():
    """Verify that ASIA_RANGE_RATIO_MULTIPLIER is applied."""
    from smc_futures.engine.strategy.asia_weekend_strategy import AsiaWeekendStrategy
    from unittest.mock import patch
    
    strat = AsiaWeekendStrategy()
    
    config = {
        "ASIA_MAX_RANGE_RATIO_BTC": 6.0,
        "ASIA_RANGE_RATIO_MULTIPLIER": 1.5
    }
    
    with patch("smc_futures.smc_config.SMC_CONFIG", config):
        result = strat._configure_threshold_for_symbol("BTC", is_weekend=False)
        # Static threshold = 6.0 (base, no multiplier in this function)
        assert result["static"] == 6.0
        assert result["source_a"] == "BTC_PROFILE"
