import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from smc_futures.engine.execution.order_manager import OrderManager
import smc_futures.smc_config

# NOTE: The strict mirror code path in OrderManager.execute_signal() was disabled
# (see PHASE 1 comment block). Multi-broker execution now uses execute_multi_broker()
# in the orchestrator. These tests target the old dead code and use
# build_app_context which is no longer imported in order_manager.
# They are marked skip until the tests are rewritten for the new architecture.

@pytest.fixture
def base_config():
    return {
        "AUTO_EXECUTE_ENABLED": True,
        "TRADING_MODE": "live",
        "KRAKEN_EXECUTION_ENABLED": True,
        "STRICT_MIRROR": True,
        "ORDER_TYPE": "market",
        "MAX_TRADES_PER_DAY": 100,
        "MAX_DAILY_LOSS_PCT": 100.0, # Disable loss limit for tests
        "MIN_RR": 0.0,               # Ignore RR checks
    }

@pytest.fixture
def mock_executors():
    bg = AsyncMock()
    bg.set_leverage = AsyncMock(return_value={"code": "00000"})
    
    kr = AsyncMock()
    kr.set_leverage = AsyncMock(return_value={"code": "00000"})
    
    return bg, kr

@pytest.fixture
def mock_ctx(mock_executors):
    bg, kr = mock_executors
    ctx = MagicMock()
    ctx.executors = {
        "futures": bg,
        "kraken_futures": kr
    }
    return ctx

@pytest.mark.anyio
@pytest.mark.skip(reason="Strict mirror in execute_signal is dead code — build_app_context no longer imported")
async def test_strict_mirror_both_success(base_config, mock_executors):
    bg, kr = mock_executors
    bg.place_order = AsyncMock(return_value={"success": True, "entry_order_id": "BG-123", "price_executed": 50000})
    kr.place_order = AsyncMock(return_value={"success": True, "entry_order_id": "KR-123", "price_executed": 50000})
    
    om = OrderManager(base_config)
    
    with patch("smc_futures.engine.execution.order_manager.build_app_context") as mock_build:
        ctx = MagicMock()
        ctx.executors = {"futures": bg, "kraken_futures": kr}
        mock_build.return_value = ctx
        
        signal = {"direction": "long", "entry": 50000, "stop_loss": 49000, "take_profit": 52000}
        
        # Bypass Pre-flight directly to execute_signal
        res = await om.execute_signal(bg, "BTCUSDT", signal, quantity=0.1, signal_id="sig-1")
        
        assert res["success"] is True
        assert res["entry_order_id"] == "BG-123"
        assert res["kr_order_id"] == "KR-123"
        
        bg.place_order.assert_called_once()
        kr.place_order.assert_called_once()


@pytest.mark.anyio
@pytest.mark.skip(reason="Strict mirror in execute_signal is dead code — build_app_context no longer imported")
async def test_strict_mirror_kraken_reject_rollback_bitget(base_config, mock_executors):
    bg, kr = mock_executors
    
    # Bitget succeeds
    bg.place_order = AsyncMock(return_value={"success": True, "entry_order_id": "BG-123"})
    # Kraken rejects
    kr.place_order = AsyncMock(return_value={"success": False, "error": "Insufficient Margin"})
    
    om = OrderManager(base_config)
    
    with patch("smc_futures.engine.execution.order_manager.build_app_context") as mock_build:
        ctx = MagicMock()
        ctx.executors = {"futures": bg, "kraken_futures": kr}
        mock_build.return_value = ctx
        
        signal = {"direction": "long", "entry": 50000, "stop_loss": 49000, "take_profit": 52000}
        res = await om.execute_signal(bg, "BTCUSDT", signal, quantity=0.1, signal_id="sig-2")
        
        assert res["success"] is False
        assert res["error"] == "STRICT_MIRROR_PARTIAL_FAIL"
        
        # Bitget Rollback must be triggered 
        # (It should send a secondary order on the opposite side "sell")
        assert bg.place_order.call_count == 2
        calls = bg.place_order.call_args_list
        # First call is LONG
        assert calls[0][0][1] == "long"
        # Second call is SELL (Rollback)
        assert calls[1][0][1] == "sell"
        assert calls[1][0][5] == "market" # Market inverse
        
        # Verify Auto Execute is disabled
        assert smc_futures.smc_config.AUTO_EXECUTE_ENABLED is False
        assert om.auto_execute_enabled is False
        assert om.kill_switch_active is True

@pytest.mark.anyio
@pytest.mark.skip(reason="Strict mirror in execute_signal is dead code — build_app_context no longer imported")
async def test_strict_mirror_kraken_timeout(base_config, mock_executors):
    bg, kr = mock_executors
    
    # Bitget succeeds
    bg.place_order = AsyncMock(return_value={"success": True, "entry_order_id": "BG-123"})
    
    # Kraken hangs out longer than 5s!
    async def hanging_kraken(*args, **kwargs):
        await asyncio.sleep(6.0)
        return {"success": True}
        
    kr.place_order = AsyncMock(side_effect=hanging_kraken)
    
    om = OrderManager(base_config)
    
    with patch("smc_futures.engine.execution.order_manager.build_app_context") as mock_build:
        ctx = MagicMock()
        ctx.executors = {"futures": bg, "kraken_futures": kr}
        mock_build.return_value = ctx
        
        signal = {"direction": "short", "entry": 50000, "stop_loss": 51000, "take_profit": 48000}
        
        # Due to 5s timeout, we should wrap this in a broader wait_for just in case of test hang, 
        # but the inner logic uses wait_for(5.0)
        task = asyncio.create_task(om.execute_signal(bg, "BTCUSDT", signal, quantity=0.1, signal_id="sig-3"))
        res = await asyncio.wait_for(task, timeout=6.0)
        
        assert res["success"] is False
        assert res["error"] == "STRICT_MIRROR_PARTIAL_FAIL"
        
        # Bitget Rollback must be triggered (direction="short", rollback="buy")
        assert bg.place_order.call_count == 2
        calls = bg.place_order.call_args_list
        assert calls[0][0][1] == "short"
        assert calls[1][0][1] == "buy"
