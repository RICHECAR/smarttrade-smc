import unittest
import json
import os
import shutil
import sys
from pathlib import Path

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from smc_futures.engine.strategy.asia_weekend_strategy import AsiaWeekendStrategy
from smc_futures.engine.strategy.range_detector import RangeDetector

class TestShadowTrading(unittest.TestCase):
    def setUp(self):
        self.project_root = Path(__file__).resolve().parent.parent
        self.test_ab_dir = self.project_root / "reports" / "ab_tests_test"
        if self.test_ab_dir.exists():
            shutil.rmtree(self.test_ab_dir)
        self.test_ab_dir.mkdir(parents=True)
        
        # Initialize strategy with test ab dir
        self.strategy = AsiaWeekendStrategy()
        from backend.services.ab_testing_service import ABTestingService
        self.strategy.ab_test_service = ABTestingService(self.test_ab_dir)
        
        # Generic mock candle
        self.candle = {"high": 100, "low": 90, "close": 95, "open": 92, "volume": 1000, "timestamp": 123456789}

    def tearDown(self):
        if self.test_ab_dir.exists():
            shutil.rmtree(self.test_ab_dir)

    def test_shadow_decision_recording(self):
        try:
            # We need enough data for ATR
            candles = [self.candle] * 20
            
            # Manually trigger build_signal to see if it records
            # Use a dummy ATR > 0 to trigger adaptive
            self.strategy.build_signal(
                symbol="BTCUSDT",
                candles_htf=candles,
                candles_mtf=candles,
                candles_ltf=candles,
                structure_state={},
                liquidity_state={},
                ob_state={},
                fvg_state={},
                current_price=95.0,
                timestamp=123456789,
                atr=2.0
            )
            
            # Check if record was created
            journals = list(self.test_ab_dir.glob("decisions_*.jsonl"))
            print(f"[DEBUG] Found journals: {journals}")
            self.assertGreaterEqual(len(journals), 1)
            
            with open(journals[0], "r") as f:
                line = f.readline()
                data = json.loads(line)
                print(f"[DEBUG] JSON Record: {data}")
                self.assertEqual(data["symbol"], "BTCUSDT")
        except Exception as e:
            print(f"[ERROR] Test failed: {e}")
            raise

if __name__ == "__main__":
    unittest.main(verbosity=2)
