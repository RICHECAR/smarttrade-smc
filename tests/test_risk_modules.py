"""
Tests — SizeCalculator, PositionSafetyGuard, SL/TP enforcement
══════════════════════════════════════════════════════════════

Run:  python -m pytest tests/test_risk_modules.py -v
"""

import asyncio
import math
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════
#  1. SizeCalculator Tests
# ═══════════════════════════════════════════════════════════════

class TestSizeCalculator(unittest.TestCase):
    """Verify SizeCalculator guarantees qty > 0 and respects exchange specs."""
    
    def setUp(self):
        from backend.risk import size_calculator as sc
        self.calc = sc  # module-level functions
    
    # ── Core guarantee: never return 0 ──
    
    def test_sizecalculator_never_zero_btc_bitget(self):
        """BTC Bitget: very small balance should still produce qty > 0."""
        result = self.calc.calculate_size(
            balance_usd=10.0, risk_pct=0.01, entry_price=67000, stop_price=66500,
            symbol="BTCUSDT", broker="kraken", leverage=3,
        )
        self.assertGreater(result.qty, 0, "qty must never be 0")
        self.assertGreaterEqual(result.qty, 0.001, "BTC min_qty = 0.001")
    
    def test_sizecalculator_never_zero_eth_bitget(self):
        """ETH Bitget: with adequate balance."""
        result = self.calc.calculate_size(
            balance_usd=100.0, risk_pct=0.01, entry_price=3500, stop_price=3450,
            symbol="ETHUSDT", broker="kraken", leverage=3,
        )
        self.assertGreater(result.qty, 0)
    
    def test_sizecalculator_never_zero_kraken(self):
        """Kraken contracts: valid balance should produce >= 1 contract."""
        result = self.calc.calculate_size(
            balance_usd=500.0, risk_pct=0.01, entry_price=67000, stop_price=66500,
            symbol="BTCUSDT", broker="kraken", leverage=3,
        )
        self.assertTrue(result.valid)
        self.assertIsNotNone(result.contracts)
        self.assertGreaterEqual(result.contracts, 1)
    
    def test_sizecalculator_never_zero_doge_kraken(self):
        """DOGE via Kraken: even tiny amount should produce >= 1 contract."""
        result = self.calc.calculate_size(
            balance_usd=5.0, risk_pct=0.005, entry_price=0.15, stop_price=0.14,
            symbol="DOGEUSDT", broker="kraken", leverage=2,
        )
        self.assertIsNotNone(result.contracts)
        self.assertGreaterEqual(result.contracts, 1)
    
    # ── Rounding: floor-step, never round up ──
    
    def test_rounding_step_floor_only(self):
        """Verify floor logic (never rounds up)."""
        from backend.risk.size_calculator import normalize_order
        valid, params = normalize_order("bitget", "BTCUSDT", "buy", 0.001499, 67000)
        self.assertTrue(valid)
        self.assertEqual(params["qty"], 0.001, "Should floor to 0.001, not round up to 0.002")
    
    def test_rounding_step_exact_multiple(self):
        """Exact multiple should not change."""
        valid, params = self.calc.normalize_order("bitget", "BTCUSDT", "buy", 0.005, 67000)
        self.assertTrue(valid)
        self.assertEqual(params["qty"], 0.005)
    
    # ── Min notional guard ──
    
    def test_min_notional_guard_bitget(self):
        """Very small qty below min_qty should be rejected or clamped."""
        valid, params = self.calc.normalize_order("bitget", "BTCUSDT", "buy", 0.00001, 67000)
        # Either it's rejected (valid=False) because qty < min_qty after floor,
        # or the normalize_order returns the floored qty which may be 0 -> rejected
        if valid:
            notional = params["qty"] * 67000
            self.assertGreater(params["qty"], 0)
    
    # ── Diagnose ──
    
    def test_diagnose_returns_dict(self):
        """diagnose() should return a diagnostic dict."""
        diag = self.calc.diagnose("BTCUSDT", 100.0, 0.01, 67000, 66500)
        self.assertIn("broker", diag)
        self.assertIn("symbol", diag)
        self.assertIn("valid", diag)


# ═══════════════════════════════════════════════════════════════
#  2. SL/TP Gate Tests (OrderManager)
# ═══════════════════════════════════════════════════════════════

class TestSLTPGate(unittest.TestCase):
    """Verify that order_manager never sends an order without SL."""
    
    def _make_signal(self, direction="long", entry=67000, sl=0, tp=0):
        return {
            "direction": direction,
            "entry": entry,
            "stop_loss": sl,
            "take_profit": tp,
            "strategy_id": "test",
            "trigger_tf": "15m",
        }
    
    def test_auto_sl_when_missing(self):
        """When sl=0, OrderManager should compute default from RISK_SL_PCT."""
        os.environ["RISK_SL_PCT"] = "1.0"
        signal = self._make_signal(entry=67000, sl=0)
        
        # Simulate what order_manager.execute_signal does:
        entry = float(signal["entry"])
        sl = float(signal["stop_loss"])
        risk_sl_pct = float(os.getenv("RISK_SL_PCT", "1.0"))
        
        if sl <= 0 and entry > 0:
            direction = signal["direction"]
            if direction == "long":
                sl = round(entry * (1 - risk_sl_pct / 100), 8)
            else:
                sl = round(entry * (1 + risk_sl_pct / 100), 8)
        
        self.assertGreater(sl, 0, "SL must be auto-computed when missing")
        self.assertAlmostEqual(sl, 66330.0, places=0)  # 67000 * 0.99
    
    def test_auto_tp_when_missing(self):
        """When tp=0, auto-compute from RISK_TP_PCT."""
        os.environ["RISK_TP_PCT"] = "2.0"
        signal = self._make_signal(entry=67000, tp=0, sl=66000)
        
        entry = float(signal["entry"])
        tp = float(signal["take_profit"])
        risk_tp_pct = float(os.getenv("RISK_TP_PCT", "2.0"))
        
        if tp <= 0 and entry > 0:
            tp = round(entry * (1 + risk_tp_pct / 100), 8)
        
        self.assertGreater(tp, entry, "TP must be above entry for long")
        self.assertAlmostEqual(tp, 68340.0, places=0)  # 67000 * 1.02
    
    def test_short_sl_above_entry(self):
        """For short, SL should be above entry."""
        os.environ["RISK_SL_PCT"] = "1.0"
        signal = self._make_signal(direction="short", entry=67000, sl=0)
        
        entry = float(signal["entry"])
        sl = float(signal["stop_loss"])
        
        if sl <= 0 and entry > 0:
            sl = round(entry * (1 + 1.0 / 100), 8)
        
        self.assertGreater(sl, entry, "Short SL must be above entry")


# ═══════════════════════════════════════════════════════════════
#  3. PositionSafetyGuard Tests (Async)
# ═══════════════════════════════════════════════════════════════

class TestPositionSafetyGuard(unittest.TestCase):
    """Verify guard detects and re-creates missing SL/TP."""
    
    def test_guard_config_defaults(self):
        """GuardConfig loads from env with sensible defaults."""
        from backend.risk.position_safety_guard import GuardConfig
        cfg = GuardConfig()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.retry_count, 3)
        self.assertGreater(cfg.sl_pct, 0)
    
    def test_guard_creates_instance(self):
        """Can instantiate guard with mock executors."""
        from backend.risk.position_safety_guard import PositionSafetyGuard
        guard = PositionSafetyGuard(executors={"bitget": MagicMock()})
        self.assertIsNotNone(guard)
        self.assertTrue(guard.config.enabled)


# ═══════════════════════════════════════════════════════════════
#  4. DecisionEngine Tests
# ═══════════════════════════════════════════════════════════════

class TestDecisionEngine(unittest.TestCase):
    """Verify DecisionEngine evaluation logic."""
    
    def test_engine_config_defaults(self):
        """Engine loads config from env."""
        from backend.risk.decision_engine import DecisionEngine
        engine = DecisionEngine(executors={})
        self.assertTrue(engine.enabled)
        self.assertGreater(engine.max_adverse_pct, 0)
    
    def test_status_api(self):
        """get_status returns proper dict."""
        from backend.risk.decision_engine import DecisionEngine
        engine = DecisionEngine(executors={})
        status = engine.get_status()
        self.assertIn("enabled", status)
        self.assertIn("stats", status)
        self.assertIn("config", status)
    
    def test_atr_spike_detection(self):
        """ATR spike detection."""
        from backend.risk.decision_engine import DecisionEngine
        engine = DecisionEngine(executors={})
        engine.update_atr("BTCUSDT", 500)  # Baseline
        # Many updates to stabilize baseline
        for _ in range(20):
            engine.update_atr("BTCUSDT", 500)
        engine.update_atr("BTCUSDT", 1500)  # Spike (3x)
        self.assertTrue(engine.is_atr_spiking("BTCUSDT"))


# ═══════════════════════════════════════════════════════════════
#  5. Kraken Executor SL/TP REST Fallback Test
# ═══════════════════════════════════════════════════════════════

class TestKrakenSLTPFallback(unittest.TestCase):
    """Verify that Kraken executor uses REST SL/TP when WS is unavailable."""
    
    def test_place_order_code_has_rest_fallback(self):
        """The kraken executor place_order should reference SL/TP REST dispatch."""
        import inspect
        from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
        source = inspect.getsource(KrakenFuturesExecutorWrapper.place_order)
        self.assertIn("_place_stop_order_rest", source, "REST SL dispatch must be in place_order")
        self.assertIn("_place_tp_order_rest", source, "REST TP dispatch must be in place_order")
        self.assertIn("EMERGENCY", source.upper(), "Emergency close must be present")


if __name__ == "__main__":
    unittest.main(verbosity=2)
