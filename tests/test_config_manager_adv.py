import unittest
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from scripts.reporting.config_manager import ConfigManager

class TestConfigManagerAdvanced(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(__file__).resolve().parent / "test_data"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.test_config = self.test_dir / "test_smc_config.py"
        content = """
SMC_CONFIG = {
    "ASIA_MAX_RANGE_RATIO_BTC": 8.5,
    "ASIA_MAX_RANGE_RATIO_ETH": 7.2,
    "ASIA_MAX_RANGE_RATIO_ALT": 6.7,
    "ASIA_RISK_FACTOR": 0.8,
}
"""
        with open(self.test_config, "w", encoding="utf-8") as f:
            f.write(content)
        self.mgr = ConfigManager(self.test_config)
        
        # Override journal dir for testing
        self.mgr.journal_dir = self.test_dir / "rollbacks"
        self.mgr.journal_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

    def test_dry_run(self):
        updates = {"ASIA_MAX_RANGE_RATIO_BTC": 6.0}
        success = self.mgr.update_constants(updates, dry_run=True)
        self.assertTrue(success)
        
        with open(self.test_config, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn('8.5', content) # Should NOT be changed

    def test_journaling(self):
        # Create a fake old rollback to satisfy thrash guard (or just test guard later)
        success = self.mgr.rollback_asia_thresholds(reason="TEST_REASON")
        self.assertTrue(success)
        
        journals = list(self.mgr.journal_dir.glob("rollback_*.json"))
        self.assertEqual(len(journals), 1)
        
        with open(journals[0], "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["reason"], "TEST_REASON")
        self.assertEqual(data["updates"]["ASIA_MAX_RANGE_RATIO_BTC"], 6.0)

    def test_anti_thrash_guard(self):
        # First rollback should work
        self.assertTrue(self.mgr.rollback_asia_thresholds(reason="FIRST"))
        
        # Second rollback within seconds should fail
        self.assertFalse(self.mgr.rollback_asia_thresholds(reason="SECOND"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
