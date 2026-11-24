import unittest
import time
import numpy as np
from smc_futures.engine.analysis.latency_volatility_analyzer import LatencyVolatilityAnalyzer
from smc_futures.engine.risk.scale_readiness_gate import ScaleReadinessGate

class TestPhase45Analysis(unittest.TestCase):

    def test_volatility_calculation(self):
        """Verify that sigma is calculated correctly on synthetic price data."""
        # Fix constructor: window_size instead of windows
        analyzer = LatencyVolatilityAnalyzer("BTCUSDT", window_size=60)
        
        # Linear prices (zero volatility)
        for i in range(20):
            # Fix method: update instead of update_price
            analyzer.update(price=50000.0, latency_ms=10, is_stale=False)
            
        summary = analyzer.get_summary()
        self.assertEqual(summary["sigma"], 0.0)
        
        # Volatile prices
        prices = [50000.0, 51000.0, 49000.0, 52000.0, 48000.0] * 4
        for p in prices:
            analyzer.update(price=p, latency_ms=10, is_stale=False)
            
        summary = analyzer.get_summary()
        self.assertGreater(summary["sigma"], 0.0)

    def test_stale_rate_tracking(self):
        """Verify that stale rejection rate is computed correctly."""
        analyzer = LatencyVolatilityAnalyzer("BTCUSDT")
        
        # 10 intents, 2 stale
        for i in range(8):
            # Fix method: update instead of record_intent
            analyzer.update(price=50000.0, latency_ms=10, is_stale=False)
        for i in range(2):
            analyzer.update(price=50000.0, latency_ms=10, is_stale=True)
            
        summary = analyzer.get_summary()
        self.assertAlmostEqual(summary["stale_rate"], 0.2, places=2)

    def test_scale_gate_logic(self):
        """Verify that the scale gate correctly blocks scaling when metrics breach limits."""
        gate = ScaleReadinessGate(stale_threshold=0.05)
        
        # 1. Initial State
        res = gate.check_readiness("BTCUSDT")
        self.assertTrue(res["is_ready"])
        self.assertEqual(res["status"], "GREEN")
        
        # 2. Manual lock
        gate.lock_scaling("Infrastructure Maintenance")
        res = gate.check_readiness("BTCUSDT")
        self.assertFalse(res["is_ready"])
        self.assertEqual(res["status"], "AMBER")
        self.assertTrue(any("SCALING_MANUALLY_LOCKED" in f for f in res["failures"]))
        
        # 3. Unlock
        gate.unlock_scaling()
        res = gate.check_readiness("BTCUSDT")
        self.assertTrue(res["is_ready"])
        self.assertEqual(res["status"], "GREEN")

if __name__ == "__main__":
    unittest.main()
