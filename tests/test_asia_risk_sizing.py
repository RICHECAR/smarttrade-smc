import unittest
from smc_futures.smc_config import SMC_CONFIG

class TestAsiaRiskSizing(unittest.TestCase):
    def test_asia_risk_factor_value(self):
        # Verify ASIA_RISK_FACTOR is a valid float between 0 and 1
        factor = SMC_CONFIG.get("ASIA_RISK_FACTOR")
        self.assertIsInstance(factor, float)
        self.assertGreater(factor, 0.0, "ASIA_RISK_FACTOR must be > 0")
        self.assertLessEqual(factor, 1.0, "ASIA_RISK_FACTOR must be <= 1")

    def test_risk_reduction_calculation(self):
        base_risk = 0.01  # 1%
        factor = SMC_CONFIG.get("ASIA_RISK_FACTOR", 0.5)
        effective = base_risk * factor
        self.assertLess(effective, base_risk, "Asia factor should reduce risk")

if __name__ == "__main__":
    unittest.main()
