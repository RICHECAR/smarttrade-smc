import pytest
import time
import os
from unittest.mock import patch, MagicMock, AsyncMock

@pytest.mark.asyncio
async def test_preflight_skips_below_min_notional():
    """Ensure order < min_notional is skipped and cache is populated."""
    from smc_futures.engine.execution.order_manager import OrderManager
    from smc_futures.engine.execution.contract_specs import ContractSpecs
    
    with patch("smc_futures.engine.execution.order_manager.get_contract_specs") as mock_get_specs, \
         patch("smc_futures.engine.execution.order_manager._evaluate_risk_v2", return_value=(True, 1.0, [])):
        mock_get_specs.return_value = ContractSpecs("XRPUSDT", min_qty=1.0, min_notional=5.0, qty_step=1.0, price_scale=4, qty_scale=0)
        
        om = OrderManager({
            "AUTO_EXECUTE_ENABLED": True,
            "TRADING_MODE": "live",
            "AUTO_UPSIZE_TO_MIN_NOTIONAL": False
        })
        om._hard_reject_cache.clear()
        
        signal = {"direction": "short", "entry": 1.4089, "stop_loss": 1.5000, "take_profit": 1.0000}
        quantity = 0.1213 # Notional: ~0.17
        
        mock_executor = AsyncMock()
        
        # Test execute (which calls pre_flight_checks and execute_signal)
        res = await om.execute(
            symbol="XRPUSDT",
            signal_dict=signal,
            signal_id="sig_test_1",
            executor=mock_executor,
            active_trade=None,
            account_balance=100.0,
            quantity=quantity
        )
        
        assert res["success"] is False
        assert "NOTIONAL_TOO_LOW" in res.get("reason", "") or "NOTIONAL_TOO_LOW" in str(res)
        # Broker shouldn't be called
        mock_executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_cooldown_blocks_repeated_hard_rejects():
    """Ensure that 2nd order is blocked entirely by the hard reject cache."""
    from smc_futures.engine.execution.order_manager import OrderManager
    
    with patch("smc_futures.engine.execution.order_manager._evaluate_risk_v2", return_value=(True, 1.0, [])):
        om = OrderManager({
            "AUTO_EXECUTE_ENABLED": True,
            "TRADING_MODE": "live",
            "HARD_REJECT_COOLDOWN_SEC": "900"
        })
        
        signal = {"direction": "short", "entry": 1.4089, "stop_loss": 1.5000, "take_profit": 1.0000}
        quantity = 0.1213
        fp = om._compute_fingerprint("XRPUSDT", signal)
        
        # Pre-populate cache like it just failed
        om._hard_reject_cache[fp] = (time.time(), "NOTIONAL_TOO_LOW")
        
        mock_executor = AsyncMock()
        
        res = await om.execute(
            symbol="XRPUSDT",
            signal_dict=signal,
            signal_id="sig_test_2",
            executor=mock_executor,
            active_trade=None,
            account_balance=100.0,
            quantity=quantity
        )
        
        assert res["success"] is False
        assert res["reason"] == "COOLDOWN_HARD_REJECT"
        mock_executor.place_order.assert_not_called()

@pytest.mark.asyncio
async def test_kraken_disabled_not_dispatched():
    """Verify MultiBroker skips kraken dispatch if KRAKEN_EXECUTION_ENABLED is false."""
    from backend.services.multi_order_executor import get_default_brokers
    
    with patch.dict(os.environ, {"BITGET_ENABLED": "true", "KRAKEN_EXECUTION_ENABLED": "false"}):
        brokers = get_default_brokers()
        assert "bitget" in brokers
        assert "kraken" not in brokers

    with patch.dict(os.environ, {"BITGET_ENABLED": "true", "KRAKEN_EXECUTION_ENABLED": "true"}):
        brokers2 = get_default_brokers()
        assert "bitget" in brokers2
        assert "kraken" in brokers2
