import unittest
from unittest.mock import MagicMock, AsyncMock
import time
from smc_futures.engine.execution.order_manager import OrderManager

class TestOrderManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = {
            "AUTO_EXECUTE_ENABLED": True,
            "TRADING_MODE": "live",  # Updated to match strict guard
            "ORDER_TYPE": "limit",
            "LIMIT_OFFSET_BPS": 50,
            "MAX_TRADES_PER_DAY": 2,
            "MAX_DAILY_LOSS_PCT": 1.0,
            "MAX_OPEN_TRADES_SYMBOL": 1,
            "TRADE_COOLDOWN_SEC": 0.1,  # Fast for test
        }
        # Mock Phase 9 risk V2 gate so tests reach their actual check
        import smc_futures.engine.execution.order_manager as om_mod
        om_mod._evaluate_risk_v2 = lambda **kw: (True, 1.0, [])
        self.mgr = OrderManager(self.config)
        self.mgr.executor = MagicMock()
        
        # Mock actual execution logic
        self.executor = AsyncMock()
        self.executor.set_leverage.return_value = {"code": "00000"}
        self.executor.place_order.return_value = {
            "success": True,
            "order_id": "12345"
        }

    def test_pre_flight_checks_basic(self):
        ok, reason, details = self.mgr.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict={"direction": "long", "entry": 100000, "stop_loss": 99000, "take_profit": 102000},
            signal_id="sig1",
            active_trade=None,
            account_balance=1000,
            quantity=0.01,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")

    def test_auto_execute_disabled(self):
        self.mgr.auto_execute_enabled = False
        ok, reason, details = self.mgr.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict={},
            signal_id="sig1"
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "AUTO_EXECUTE_DISABLED")

    def test_paper_mode(self):
        self.mgr.trading_mode = "paper"
        ok, reason, details = self.mgr.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict={},
            signal_id="sig1"
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "PAPER_MODE")

    def test_kill_switch(self):
        self.mgr.engage_kill_switch("TEST_GUARD")
        ok, reason, details = self.mgr.pre_flight_checks(
            symbol="BTCUSDT",
            signal_dict={"direction": "long", "entry": 100},
            signal_id="sig1",
            quantity=0.01,
        )
        self.assertFalse(ok)
        self.assertIn("KILL_SWITCH", reason)

    async def test_execute_signal_limit(self):
        # 0.5% offset
        expected_bps = 50 
        entry_price = 10000.0
        
        signal = {
            "direction": "long",
            "entry": entry_price,
            "stop_loss": 9900,
            "take_profit": 10200
        }
        
        res = await self.mgr.execute_signal(
            self.executor, "BTCUSDT", signal, 0.1, 5
        )
        
        self.assertTrue(res["success"])
        
        # Verify calls
        args, kwargs = self.executor.place_order.call_args
        self.assertEqual(kwargs["order_type"], "limit")
        
        # Price check: 10000 * (1 - 0.005) = 9950
        expected_price = entry_price * (1 - (expected_bps/10000))
        actual_price = kwargs.get("entry_price") or kwargs.get("price")
        self.assertAlmostEqual(actual_price, expected_price, places=2)

    async def test_execute_signal_market(self):
        self.mgr.order_type = "market"
        signal = {"direction": "short", "entry": 2000.0}
        
        await self.mgr.execute_signal(
            self.executor, "ETHUSDT", signal, 1.0
        )
        
        args, kwargs = self.executor.place_order.call_args
        self.assertEqual(kwargs["order_type"], "market")
    async def test_get_stats(self):
        stats = self.mgr.get_stats()
        self.assertIsInstance(stats, dict)
        self.assertIn("mode", stats)
        self.assertIn("daily", stats)
        self.assertIn("open_orders", stats)
        self.assertIn("exec_log", stats)
        self.assertEqual(stats["daily"]["trades"], 0)

if __name__ == "__main__":
    unittest.main()
