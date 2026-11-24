import unittest
from smc_futures.smc_config import SMC_CONFIG

class TestRangeThresholds(unittest.TestCase):
    def test_threshold_values(self):
        # Verify the new relaxed thresholds are loaded
        self.assertEqual(SMC_CONFIG["ASIA_MAX_RANGE_RATIO_BTC"], 6.4)
        self.assertEqual(SMC_CONFIG["ASIA_MAX_RANGE_RATIO_ETH"], 5.2)
        self.assertEqual(SMC_CONFIG["ASIA_MAX_RANGE_RATIO_ALT"], 5.7)
    
    def test_btc_threshold_pass(self):
        # Logic check: would ratio 6.23 pass?
        ratio = 6.23
        threshold = SMC_CONFIG["ASIA_MAX_RANGE_RATIO_BTC"]
        self.assertLess(ratio, threshold, f"Ratio {ratio} should be less than threshold {threshold}")

if __name__ == "__main__":
    unittest.main()
