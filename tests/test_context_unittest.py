import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure smc_futures is in path
import sys
import os
sys.path.append(os.getcwd())

from smc_futures.app_context import AppContext
from smc_futures.orchestrator import SMCFuturesOrchestrator, run_symbol_cycle

class TestArchitecture(unittest.TestCase):
    def setUp(self):
        self.ctx = AppContext()
        self.ctx.metrics = MagicMock()
        self.ctx.order_manager = MagicMock()
        self.ctx.executors = {"futures": AsyncMock()}
        self.ctx.ws_manager = AsyncMock()

    def test_context_init(self):
        self.assertIsNotNone(self.ctx.config)
        self.assertIsNotNone(self.ctx.metrics)
        self.assertEqual(self.ctx.orchestrators, {})

    @patch("smc_futures.orchestrator.SMC_CONFIG", {"HTF_LIMIT": 100, "SETUP_LIMIT": 100, "ENTRY_LIMIT": 100, "MIN_HTF_CANDLES": 50, "MODE": "prod", "ALLOW_COUNTERTREND": False, "MIN_RR": 2.0, "SESSIONS": {}, "RISK_PER_TRADE_PCT": 1.0, "DEFAULT_LEVERAGE": 10, "HTF_REFRESH_INTERVAL": 10, "MIN_HTF_CANDLES": 20})
    def test_orchestrator_injection(self):
        orch = SMCFuturesOrchestrator(
            symbol="BTCUSDT",
            context=self.ctx
        )
        self.assertEqual(orch.ctx, self.ctx)
        self.assertEqual(orch.symbol, "BTCUSDT")

    @patch("smc_futures.orchestrator.SMC_CONFIG", {"HTF_LIMIT": 100, "SETUP_LIMIT": 100, "ENTRY_LIMIT": 100, "MIN_HTF_CANDLES": 50, "MODE": "prod", "ALLOW_COUNTERTREND": False, "MIN_RR": 2.0, "SESSIONS": {}, "RISK_PER_TRADE_PCT": 1.0, "DEFAULT_LEVERAGE": 10, "HTF_REFRESH_INTERVAL": 10, "MIN_HTF_CANDLES": 20, "HTF_TF": "4h", "SETUP_TF": "15m", "ENTRY_TF": "5m", "LOOP_SLEEP_SECONDS": 1})
    @patch("smc_futures.orchestrator.get_candles", new_callable=AsyncMock)
    def test_run_symbol_cycle(self, mock_get_candles):
        # Setup Async Loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        orch = SMCFuturesOrchestrator(
            symbol="BTCUSDT",
            context=self.ctx
        )
        # Mock methods
        orch.process_htf_context = MagicMock()
        orch.process_mtf_setup = MagicMock()
        orch.process_ltf_execution = AsyncMock(return_value=None)
        orch.get_dashboard_state = MagicMock(return_value={})
        
        # Mock Data Return
        mock_get_candles.return_value = [{"close": 100, "timestamp": 1000}]
        
        # Run Cycle (loop=0)
        loop.run_until_complete(run_symbol_cycle("BTCUSDT", orch, loop_count=0))
        
        # Validation
        self.assertTrue(orch.process_htf_context.called, "HTF should be processed on loop 0")
        self.assertTrue(orch.process_mtf_setup.called, "MTF should be processed")
        self.assertTrue(orch.process_ltf_execution.called, "LTF should be processed")
        self.assertTrue(self.ctx.ws_manager.broadcast.called, "Broadcast should occur")
        
        loop.close()

if __name__ == "__main__":
    unittest.main()
