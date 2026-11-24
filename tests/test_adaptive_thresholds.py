import unittest
from smc_futures.risk.adaptive_thresholds import AdaptiveThresholdManager, SessionType

class TestAdaptiveThresholds(unittest.TestCase):
    def setUp(self):
        self.manager = AdaptiveThresholdManager()

    def test_session_base_thresholds(self):
        # BTC Asia base should be around 6.4 (from config or default)
        threshold = self.manager.get_threshold("BTCUSDT", "asia", 100, 100)
        self.assertAlmostEqual(threshold, 6.4, places=1)
        
        # NY should be higher (default 7.5)
        threshold_ny = self.manager.get_threshold("BTCUSDT", "ny", 100, 100)
        self.assertGreater(threshold_ny, threshold)

    def test_volatility_scaling(self):
        # Baseline
        base = self.manager.get_threshold("BTCUSDT", "asia", 100, 100)
        
        # High volatility (2x average) -> should tighten (reduce threshold)
        tightened = self.manager.get_threshold("BTCUSDT", "asia", 200, 100)
        self.assertLess(tightened, base)
        
        # Low volatility (0.5x average) -> should relax (increase threshold)
        relaxed = self.manager.get_threshold("BTCUSDT", "asia", 50, 100)
        self.assertGreater(relaxed, base)

    def test_performance_coupling(self):
        # Baseline
        base = self.manager.get_threshold("BTCUSDT", "asia", 100, 100)
        
        # Poor performance (30% winrate) -> should tighten
        pessimistic = self.manager.get_threshold("BTCUSDT", "asia", 100, 100, recent_winrate=30)
        self.assertLess(pessimistic, base)
        
        # Good performance (60% winrate) -> should relax
        optimistic = self.manager.get_threshold("BTCUSDT", "asia", 100, 100, recent_winrate=60)
        self.assertGreater(optimistic, base)

    def test_hard_rails(self):
        # Very high volatility + extremely high base -> capped at max_ratio (8.0)
        # We simulate a very high base by picking NY session if it was extreme, 
        # but here we test the capped result.
        
        # Force a value way above max by using a relaxed session and good performance
        relaxed = self.manager.get_threshold("BTCUSDT", "ny", 50, 100, recent_winrate=90)
        self.assertLessEqual(relaxed, 8.0)
        
        # Force a value below min (4.0)
        tight = self.manager.get_threshold("SOLUSDT", "asia", 300, 100, recent_winrate=10)
        self.assertGreaterEqual(tight, 4.0)

if __name__ == "__main__":
    unittest.main()
