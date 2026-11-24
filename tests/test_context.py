import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from smc_futures.app_context import AppContext
from smc_futures.orchestrator import SMCFuturesOrchestrator, run_symbol_cycle
from smc_futures.engine.utils.reason_codes import DecisionCategory

@pytest.fixture
def mock_ctx():
    """fixture for AppContext with mocks"""
    ctx = AppContext()
    ctx.metrics = MagicMock()
    ctx.order_manager = MagicMock()
    ctx.executors = {"futures": AsyncMock()}
    ctx.ws_manager = AsyncMock()
    return ctx

@pytest.fixture
def mock_orch(mock_ctx):
    """fixture for Orchestrator with injected context"""
    with patch("smc_futures.orchestrator.SMC_CONFIG", {"HTF_LIMIT": 100, "SETUP_LIMIT": 100, "ENTRY_LIMIT": 100, "MIN_HTF_CANDLES": 50, "MODE": "prod", "ALLOW_COUNTERTREND": False, "MIN_RR": 2.0, "SESSIONS": {}, "RISK_PER_TRADE_PCT": 1.0, "DEFAULT_LEVERAGE": 10, "HTF_REFRESH_INTERVAL": 10, "MIN_HTF_CANDLES": 20}):
        orch = SMCFuturesOrchestrator(
            symbol="BTCUSDT",
            context=mock_ctx
        )
        return orch

@pytest.mark.asyncio
async def test_app_context_initialization():
    ctx = AppContext()
    assert ctx.config is not None
    assert ctx.metrics is not None
    assert ctx.order_manager is not None
    assert ctx.orchestrators == {}

@pytest.mark.asyncio
async def test_orchestrator_injection(mock_ctx, mock_orch):
    """Verify orchestrator uses the injected context"""
    assert mock_orch.ctx == mock_ctx
    assert mock_orch.symbol == "BTCUSDT"

@pytest.mark.asyncio
async def test_run_symbol_cycle_uniformity(mock_ctx, mock_orch):
    """Verify run_symbol_cycle calls specific stream methods on orchestrator"""
    
    # Mock methods
    mock_orch.process_htf_context = MagicMock()
    mock_orch.process_mtf_setup = MagicMock()
    mock_orch.process_ltf_execution = AsyncMock(return_value=None)
    mock_orch.get_dashboard_state = MagicMock(return_value={})
    
    # Mock helper `get_candles` to return data so streams run
    with patch("smc_futures.orchestrator.get_candles", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = [{"close": 100, "timestamp": 1000}]
        
        # Run cycle (loop_count=0 -> triggers HTF refresh)
        await run_symbol_cycle("BTCUSDT", mock_orch, loop_count=0)
        
        # Check Stream A (HTF)
        assert mock_orch.process_htf_context.called
        
        # Check Stream B (MTF)
        assert mock_orch.process_mtf_setup.called
        
        # Check Stream C (LTF)
        assert mock_orch.process_ltf_execution.called
        
        # Check Broadcast
        assert mock_ctx.ws_manager.broadcast.called

@pytest.mark.asyncio
async def test_run_symbol_cycle_skip_htf(mock_ctx, mock_orch):
    """Verify HTF is skipped if not refresh interval"""
    
    mock_orch.process_htf_context = MagicMock()
    mock_orch.process_mtf_setup = MagicMock()
    mock_orch.process_ltf_execution = AsyncMock(return_value=None)
    
    with patch("smc_futures.orchestrator.get_candles", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = [{"close": 100, "timestamp": 1000}]
        with patch("smc_futures.orchestrator.SMC_CONFIG", {"HTF_REFRESH_INTERVAL": 10, "MIN_HTF_CANDLES": 20, "HTF_TF": "4h", "HTF_LIMIT": 100, "SETUP_TF": "15m", "SETUP_LIMIT": 100, "ENTRY_TF": "5m", "ENTRY_LIMIT": 100, "LOOP_SLEEP_SECONDS": 60}):
             # loop_count=1 (not 0, not % 10)
            await run_symbol_cycle("BTCUSDT", mock_orch, loop_count=1)
            
            # Check Stream A (HTF) -> Should NOT be called
            assert not mock_orch.process_htf_context.called
            
            # Others should be called
            assert mock_orch.process_mtf_setup.called
            assert mock_orch.process_ltf_execution.called

