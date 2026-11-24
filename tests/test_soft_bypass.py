import logging
import unittest
from unittest.mock import MagicMock
from smc_futures.engine.strategy.signal_builder import SignalBuilder, SignalResult

# Configure logging
logging.basicConfig(level=logging.INFO)

class TestSoftBypass(unittest.TestCase):
    def setUp(self):
        self.builder = SignalBuilder(
            allow_no_sweep_with_ob=True,
            no_sweep_min_rr=1.5,
            no_sweep_require_fresh_ob=True,
            no_sweep_block_strong_htf=True,
            sweep_fallback_max_ob_age=15 # Standard OB age limit
        )
        self.symbol = "BTCUSDT"
        self.current_price = 100.0
        self.timestamp = 1600000000
        
        # Base Mock States
        self.structure_state = {"recent_bos": 1, "recent_choch": 0} # BOS confirmed
        self.liquidity_state = {"sweep_history": [], "atr": 1.0}    # No sweep
        self.ob_state = {"blocks": []}
        self.fvg_state = {"fvgs": []}
        self.mtf_state = {"bias": "bullish", "bias_strength": 0.5} # Moderate Bullish (pass weak check)
        
        # Trigger mock (aligned)
        self.ltf_trigger = {"confirmed": True, "direction": "long", "trigger_type": "bos"}


    def test_soft_bypass_success(self):
        """Case A: No Sweep + BOS + Fresh OB + Weak HTF -> PASS with [SOFT_BYPASS]"""
        # Add fresh OB
        self.ob_state["blocks"] = [{
            "type": "bullish", "high": 99.0, "low": 98.0, "age_bars": 5 # Fresh (<15)
        }]
        
        result = self.builder.build_signal(
            self.symbol, self.structure_state, self.liquidity_state,
            self.ob_state, self.fvg_state, self.mtf_state,
            self.current_price, self.timestamp, self.ltf_trigger
        )
        
        if result.status != "CREATED":
             self.fail(f"Expected CREATED but got {result.status}. Reasons: {result.reasons}")
        found_reason = any("SWEEP_SOFT_BYPASS" in r for r in result.reasons)
        self.assertTrue(found_reason, f"Expected SWEEP_SOFT_BYPASS in reasons: {result.reasons}")

    def test_bypass_rejected_no_ob(self):
        """Case D: No Sweep + BOS + No OB -> REJECT"""
        self.ob_state["blocks"] = [] # Empty OBs
        
        result = self.builder.build_signal(
            self.symbol, self.structure_state, self.liquidity_state,
            self.ob_state, self.fvg_state, self.mtf_state,
            self.current_price, self.timestamp, self.ltf_trigger
        )
        
        self.assertEqual(result.status, "NO_SIGNAL")
        self.assertTrue(any("NO_LIQUIDITY_SWEEP" in r for r in result.reasons))

    def test_bypass_rejected_stale_ob(self):
        """No Sweep + BOS + Stale OB -> REJECT (if fresh enforced)"""
        self.ob_state["blocks"] = [{
            "type": "bullish", "high": 99.0, "low": 98.0, "age_bars": 20 # Stale (>15)
        }]
        
        result = self.builder.build_signal(
            self.symbol, self.structure_state, self.liquidity_state,
            self.ob_state, self.fvg_state, self.mtf_state,
            self.current_price, self.timestamp, self.ltf_trigger
        )
        
        self.assertEqual(result.status, "NO_SIGNAL") # Stale OB filtered out

    def test_bypass_blocked_by_strong_htf(self):
        """Case C: Strong HTF + Trigger Counter-Trend -> REJECT"""
        # Scenario: HTF is Strong Bearish, but we are trying to Long via bypass (reversal attempt?)
        # Wait, if bias is Bearish, SignalBuilder logic expects Short trigger.
        # But let's say we have a Neutral bias override or similar.
        # Actually my logic checks: if trigger_dir != expected_dir?
        # Standard logic: expected_dir follows bias.
        # If bias=Bullish, expected=Long.
        # If trigger=Short (CounterTrend), we are blocked unless allowance.
        # But NO_SWEEP_BLOCK_STRONG_HTF logic says: 
        # If TRIGGER is Counter-Trend to HTF Bias, AND HTF Bias is Strong -> BLOCK even if AllowCT is on?
        
        # Let's test standard flow: Bias=Bearish (Strong), Trigger=Long (Counter)
        self.mtf_state = {"bias": "bearish", "bias_strength": 0.8} # Strong Bearish
        self.ltf_trigger = {"confirmed": True, "direction": "long"} # Trying to Long
        
        # Enable CT generally to reach bypass check
        self.builder.allow_countertrend = True 
        
        # Add Bullish OB for Long
        self.ob_state["blocks"] = [{
            "type": "bullish", "low": 90.0, "high": 91.0, "age_bars": 5
        }]
        
        # Attempt signal
        # Logic: 
        # 1. Trigger vs Bias -> CounterTrend Allowed check -> PASS (allow_ct=True)
        # 2. No Sweep -> Bypass Check ->
        #    Check 3: Strong HTF Guard?
        #    Trigger(Long) != Expected(Short) -> YES
        #    BiasStrength(0.8) >= 0.7 -> YES
        #    -> is_htf_blocked = True
        
        result = self.builder.build_signal(
            self.symbol, self.structure_state, self.liquidity_state,
            self.ob_state, self.fvg_state, self.mtf_state,
            self.current_price, self.timestamp, self.ltf_trigger
        )
        
        # Should be rejected because Bypass is blocked by Strong HTF
        # And since No Sweep + Blocked Bypass -> NO_SIGNAL / REJECT via NO_LIQUIDITY_SWEEP
        
        self.assertNotEqual(result.status, "CREATED")
        # Check logs/reasons? Hard to check internal vars but result won't be CREATED
        
    def test_bypass_success_aligned(self):
        """Case B: Aligned with Strong HTF -> PASS"""
        # Strong Bullish Bias, Long Trigger -> Aligned
        self.mtf_state = {"bias": "bullish", "bias_strength": 0.9}
        self.ltf_trigger = {"confirmed": True, "direction": "long"}
        
        self.ob_state["blocks"] = [{
            "type": "bullish", "high": 99.0, "low": 98.0, "age_bars": 5
        }]
        
        result = self.builder.build_signal(
            self.symbol, self.structure_state, self.liquidity_state,
            self.ob_state, self.fvg_state, self.mtf_state,
            self.current_price, self.timestamp, self.ltf_trigger
        )
        
        self.assertEqual(result.status, "CREATED", "Should allow bypass if aligned even if HTF is strong")

if __name__ == '__main__':
    unittest.main()
