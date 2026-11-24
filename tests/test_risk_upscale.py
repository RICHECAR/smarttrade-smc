"""
Tests for AUTO_RISK_UPSCALE feature in SMCTradeValidator
═══════════════════════════════════════════════════════════

Usage:
    pytest tests/test_risk_upscale.py -v
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smc_futures.engine.strategy.trade_validator import SMCTradeValidator


class TestAutoRiskUpscale:
    """Test AUTO_RISK_UPSCALE functionality."""

    def test_no_upscale_needed_qty_above_min(self):
        """When qty >= minQty, no upscale should occur."""
        validator = SMCTradeValidator(
            min_rr=1.5,
            risk_per_trade_pct=1.0,
            max_leverage=10,
            account_balance=10000.0,
            auto_risk_upscale=True,
            auto_risk_upscale_max_pct=3.0,
        )
        
        signal = {
            "entry": 100.0,
            "stop_loss": 95.0,  # risk = 5
            "take_profit": 110.0,  # reward = 10, RR = 2.0
            "direction": "long",
        }
        rules = {
            "min_qty": 0.1,  # Very low minQty
            "qty_step": 0.01,
            "min_notional": 5.0,
        }
        
        result = validator.validate("TESTUSDT", signal, 100.0, rules=rules)
        
        # 1% of 10000 = 100 / 5 risk = 20 qty >> 0.1 minQty
        assert result["valid"] is True
        assert result["quantity"] >= 0.1
        # Original risk_pct should be used (no upscale)

    def test_upscale_when_qty_below_min(self):
        """When qty < minQty and upscale possible, should auto-increase risk."""
        validator = SMCTradeValidator(
            min_rr=1.5,
            risk_per_trade_pct=0.5,  # Low risk
            max_leverage=10,
            account_balance=1000.0,  # Small account
            auto_risk_upscale=True,
            auto_risk_upscale_max_pct=3.0,
        )
        
        signal = {
            "entry": 100.0,
            "stop_loss": 99.0,  # risk = 1
            "take_profit": 103.0,  # reward = 3, RR = 3.0
            "direction": "long",
        }
        rules = {
            "min_qty": 10.0,  # High minQty
            "qty_step": 1.0,
            "min_notional": 5.0,
        }
        
        # Without upscale: 0.5% of 1000 = 5 / 1 risk = 5 qty < 10 minQty
        # Required: 10 qty * 1 risk * 100 / 1000 = 1.0%
        result = validator.validate("TESTUSDT", signal, 100.0, rules=rules)
        
        assert result["valid"] is True
        assert result["quantity"] == 10.0  # Clamped to minQty
        # Risk was upscaled from 0.5% to 1.0%

    def test_reject_when_upscale_exceeds_max(self):
        """When required risk > max_pct, should reject as SYMBOL_INELIGIBLE."""
        validator = SMCTradeValidator(
            min_rr=1.5,
            risk_per_trade_pct=0.1,  # Very low risk
            max_leverage=10,
            account_balance=100.0,  # Very small account
            auto_risk_upscale=True,
            auto_risk_upscale_max_pct=3.0,  # Max 3%
        )
        
        signal = {
            "entry": 150.0,
            "stop_loss": 140.0,  # risk = 10
            "take_profit": 170.0,  # reward = 20, RR = 2.0
            "direction": "long",
        }
        rules = {
            "min_qty": 1.0,  # minQty = 1
            "qty_step": 0.1,
            "min_notional": 5.0,
        }
        
        # Without upscale: 0.1% of 100 = 0.1 / 10 risk = 0.01 qty < 1 minQty
        # Required: 1.0 qty * 10 risk * 100 / 100 = 10% >> 3% max
        result = validator.validate("TESTUSDT", signal, 150.0, rules=rules)
        
        assert result["valid"] is False
        assert "SYMBOL_INELIGIBLE" in result["reason"]

    def test_upscale_disabled(self):
        """When auto_risk_upscale=False, should reject instead of upscaling."""
        validator = SMCTradeValidator(
            min_rr=1.5,
            risk_per_trade_pct=0.5,
            max_leverage=10,
            account_balance=1000.0,
            auto_risk_upscale=False,  # Disabled
            auto_risk_upscale_max_pct=3.0,
        )
        
        signal = {
            "entry": 100.0,
            "stop_loss": 99.0,  # risk = 1
            "take_profit": 103.0,  # reward = 3, RR = 3.0
            "direction": "long",
        }
        rules = {
            "min_qty": 10.0,
            "qty_step": 1.0,
            "min_notional": 5.0,
        }
        
        # qty = 5 < 10, but upscale disabled
        result = validator.validate("TESTUSDT", signal, 100.0, rules=rules)
        
        assert result["valid"] is False
        # Should be rejected with INSUFFICIENT_BALANCE (original behavior)

    def test_upscale_real_scenario_sol(self):
        """Real scenario: SOL with small account."""
        validator = SMCTradeValidator(
            min_rr=2.0,
            risk_per_trade_pct=1.0,
            max_leverage=5,
            account_balance=500.0,  # Small account
            auto_risk_upscale=True,
            auto_risk_upscale_max_pct=3.0,
        )
        
        signal = {
            "entry": 143.5,
            "stop_loss": 140.0,  # risk = 3.5
            "take_profit": 155.0,  # reward = 11.5, RR ≈ 3.3
            "direction": "long",
        }
        rules = {
            "min_qty": 0.1,  # SOL minQty
            "qty_step": 0.1,
            "min_notional": 5.0,
        }
        
        # Without upscale: 1% of 500 = 5 / 3.5 risk = 1.43 qty
        # 1.43 qty > 0.1 minQty → no upscale needed
        result = validator.validate("SOLUSDT", signal, 143.5, rules=rules)
        
        assert result["valid"] is True
        assert result["quantity"] >= 0.1

    def test_upscale_real_scenario_xrp_small_account(self):
        """Real scenario: XRP with very small account needing upscale."""
        validator = SMCTradeValidator(
            min_rr=2.0,
            risk_per_trade_pct=1.0,
            max_leverage=5,
            account_balance=50.0,  # Very small account
            auto_risk_upscale=True,
            auto_risk_upscale_max_pct=3.0,
        )
        
        signal = {
            "entry": 0.55,
            "stop_loss": 0.52,  # risk = 0.03
            "take_profit": 0.65,  # reward = 0.10, RR ≈ 3.3
            "direction": "long",
        }
        rules = {
            "min_qty": 1.0,  # XRP minQty
            "qty_step": 1.0,
            "min_notional": 5.0,
        }
        
        # Without upscale: 1% of 50 = 0.5 / 0.03 risk = 16.67 qty
        # 16.67 > 1 minQty → no upscale needed
        result = validator.validate("XRPUSDT", signal, 0.55, rules=rules)
        
        assert result["valid"] is True
        assert result["quantity"] >= 1.0

    def test_upscale_boundary_exactly_at_max(self):
        """Boundary test: required risk exactly at max should pass."""
        validator = SMCTradeValidator(
            min_rr=1.5,
            risk_per_trade_pct=1.0,
            max_leverage=10,
            account_balance=1000.0,
            auto_risk_upscale=True,
            auto_risk_upscale_max_pct=3.0,  # Exactly 3%
        )
        
        signal = {
            "entry": 100.0,
            "stop_loss": 97.0,  # risk = 3
            "take_profit": 106.0,  # reward = 6, RR = 2.0
            "direction": "long",
        }
        rules = {
            "min_qty": 10.0,
            "qty_step": 1.0,
            "min_notional": 5.0,
        }
        
        # Without upscale: 1% of 1000 = 10 / 3 risk = 3.33 qty < 10 minQty
        # Required: 10 * 3 * 100 / 1000 = 3.0% (exactly at max)
        result = validator.validate("TESTUSDT", signal, 100.0, rules=rules)
        
        assert result["valid"] is True
        assert result["quantity"] == 10.0


class TestUpscaleWithNeutralBias:
    """Test upscale interaction with neutral bias (halved risk)."""

    def test_upscale_with_neutral_override(self):
        """Upscale should work correctly with neutral_override halved risk."""
        validator = SMCTradeValidator(
            min_rr=1.5,
            risk_per_trade_pct=1.0,  # Will be halved to 0.5%
            max_leverage=10,
            account_balance=1000.0,
            auto_risk_upscale=True,
            auto_risk_upscale_max_pct=3.0,
        )
        
        signal = {
            "entry": 100.0,
            "stop_loss": 99.0,  # risk = 1
            "take_profit": 106.0,  # reward = 6, RR = 6.0 (passes 3.0 neutral min)
            "direction": "long",
        }
        rules = {
            "min_qty": 10.0,
            "qty_step": 1.0,
            "min_notional": 5.0,
        }
        
        # With neutral: risk = 0.5%, qty = 5 / 1 = 5 < 10 minQty
        # Should upscale
        result = validator.validate("TESTUSDT", signal, 100.0, rules=rules, neutral_override=True)
        
        # RR = 6.0 >= 3.0 (neutral requirement)
        # Upscale needed: 10 * 1 * 100 / 1000 = 1.0% <= 3.0% max
        assert result["valid"] is True
        assert result["quantity"] == 10.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
