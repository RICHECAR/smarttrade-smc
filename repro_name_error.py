import sys
import os
import asyncio
import traceback
from unittest.mock import MagicMock, AsyncMock

# Add project root to path
sys.path.append(os.getcwd())

from smc_futures.orchestrator import SMCFuturesOrchestrator
from smc_futures.app_context import AppContext

async def main():
    ctx = AppContext()
    ctx.metrics = MagicMock()
    # Mock update_htf_progress
    ctx.metrics.update_htf_progress = MagicMock()

    orch = SMCFuturesOrchestrator("BTCUSDT", context=ctx)
    
    # Mock engines to avoid DB calls
    orch.htf_structure_engine = MagicMock()
    orch.htf_structure_engine.candle_history = []
    orch.mtf_engine = MagicMock()
    
    # Simulate HTF Context Update
    print("Testing process_htf_context...")
    try:
        orch.process_htf_context("BTCUSDT", [{"timestamp": 1000, "open": 1, "high": 2, "low": 0.5, "close": 1, "volume": 100}])
        print("✅ process_htf_context passed")
    except NameError as e:
        print(f"❌ NameError in process_htf_context: {e}")
        traceback.print_exc()
    except Exception as e:
        print(f"❌ Other Error in process_htf_context: {e}")
        traceback.print_exc()

    # Simulate LTF Execution
    print("\nTesting process_ltf_execution...")
    try:
        orch.session_filter = MagicMock()
        orch.session_filter.check_and_log.return_value = True
        orch.smc_validator = MagicMock()
        orch.smc_validator.validate.return_value = {"valid": True, "reason": "OK", "rr": 2.0, "quantity": 0.1}
        
        # Mock engines to return state
        orch.ltf_structure_engine.get_current_state.return_value = {}
        orch.ltf_liquidity_engine.get_current_state.return_value = {}
        orch.mtf_ob_engine.get_current_state.return_value = []
        orch.mtf_fvg_engine.get_current_state.return_value = []
        orch.mtf_engine.check_ltf_trigger.return_value = {"confirmed": True}
        
        # Mock signal builder to return a valid signal so we reach execution block
        signal_mock = MagicMock()
        signal_mock.direction.value = "long"
        signal_mock.entry_price = 100
        signal_mock.stop_loss = 90
        signal_mock.take_profit = 120
        signal_mock.setup_type = "TEST"
        signal_mock.trigger_type = "TEST"
        signal_mock.confidence_score = 1.0
        signal_mock.risk_reward_ratio = 2.0
        signal_mock.invalidation_level = 90
        signal_mock.mtf_bias = "long"
        signal_mock.timestamp = 1000
        
        orch.signal_builder.build_signal.return_value = MagicMock(status="CREATED", signal=signal_mock, reasons=[])
        
        # Mock Order Manager
        orch.ctx.order_manager = MagicMock()
        orch.ctx.order_manager.pre_flight_checks.return_value = (True, "OK")
        orch.ctx.order_manager.execute_signal = AsyncMock(return_value={"success": True, "entry_order_id": "123"})
        orch.ctx.order_manager.record_fill = MagicMock()
        
        await orch.process_ltf_execution("BTCUSDT", [{"timestamp": 1000, "open": 1, "high": 2, "low": 0.5, "close": 1, "volume": 100}])
        print("✅ process_ltf_execution passed")
    except NameError as e:
        print(f"❌ NameError in process_ltf_execution: {e}")
        traceback.print_exc()
    except Exception as e:
        print(f"❌ Other Error in process_ltf_execution: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
