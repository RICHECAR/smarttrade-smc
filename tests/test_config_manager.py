import unittest
import re
import sys
from pathlib import Path

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from scripts.reporting.config_manager import ConfigManager

class TestConfigManager(unittest.TestCase):
    def setUp(self):
        self.test_config = Path(__file__).resolve().parent / "test_smc_config.py"
        content = """
SMC_CONFIG = {
    "ASIA_MAX_RANGE_RATIO_BTC": 8.5, # Test comment
    "ASIA_MAX_RANGE_RATIO_ETH": 7.2,
    "ASIA_MAX_RANGE_RATIO_ALT": 6.7,
    "ASIA_RISK_FACTOR": 0.8,
}
"""
        with open(self.test_config, "w", encoding="utf-8") as f:
            f.write(content)
        self.mgr = ConfigManager(self.test_config)

    def tearDown(self):
        if self.test_config.exists():
            self.test_config.unlink()

    def test_update_constants(self):
        try:
            updates = {"ASIA_MAX_RANGE_RATIO_BTC": 6.0, "ASIA_RISK_FACTOR": 0.5}
            success = self.mgr.update_constants(updates)
            self.assertTrue(success)
            
            with open(self.test_config, "r", encoding="utf-8") as f:
                content = f.read()
                print(f"[DEBUG] Content:\n{content}")
            
            self.assertIn('"ASIA_MAX_RANGE_RATIO_BTC": 6.0,', content)
            self.assertIn('"ASIA_RISK_FACTOR": 0.5,', content)
        except Exception as e:
            print(f"[ERROR] test_update_constants failed: {e}")
            raise

    def test_rollback_asia_thresholds(self):
        try:
            # Bypass anti-thrash guard (returns 0 = no recent rollback)
            self.mgr.get_last_rollback_time = lambda: 0
            success = self.mgr.rollback_asia_thresholds()
            self.assertTrue(success)
            
            with open(self.test_config, "r", encoding="utf-8") as f:
                content = f.read()
            
            self.assertIn('"ASIA_MAX_RANGE_RATIO_BTC": 6.0,', content)
            self.assertIn('"ASIA_MAX_RANGE_RATIO_ETH": 5.0,', content)
        except Exception as e:
            print(f"[ERROR] test_rollback_asia_thresholds failed: {e}")
            raise
        self.assertIn('"ASIA_MAX_RANGE_RATIO_ALT": 4.0,', content)

if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
