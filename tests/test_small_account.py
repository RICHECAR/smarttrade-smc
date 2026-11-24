"""
Tests for SmallAccountManager — Symbol eligibility & risk scaling
══════════════════════════════════════════════════════════════════

Usage:
    pytest tests/test_small_account.py -v
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smc_futures.engine.small_account_manager import (
    SmallAccountManager,
    estimate_equity_thresholds,
    get_recommended_symbols_for_equity,
)


class TestSymbolEligibility:
    """Test symbol eligibility calculations."""

    def test_xrp_eligible_with_small_equity(self):
        """XRP should be eligible with ~$23 equity."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "SMALL_ACC_RISK_START": 1.5,
            "SMALL_ACC_RISK_MAX": 3.0,
            "DEFAULT_LEVERAGE": 5,
        })
        
        # XRP: minQty=1, price~$0.55
        elig = sam.calculate_symbol_eligibility(
            symbol="XRPUSDT",
            equity=23.0,
            current_price=0.55,
            risk_pct=1.5,
        )
        
        assert elig.eligible is True
        assert "ELIGIBLE" in elig.reason
        print(f"XRP eligibility: {elig.to_dict()}")

    def test_btc_ineligible_with_small_equity(self):
        """BTC should be ineligible with ~$23 equity."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "SMALL_ACC_RISK_START": 1.5,
            "SMALL_ACC_RISK_MAX": 3.0,
            "DEFAULT_LEVERAGE": 5,
        })
        
        # BTC: minQty=0.001, price~$95000
        elig = sam.calculate_symbol_eligibility(
            symbol="BTCUSDT",
            equity=23.0,
            current_price=95000.0,
            risk_pct=1.5,
        )
        
        assert elig.eligible is False
        assert "MIN_QTY" in elig.reason or "INELIGIBLE" in elig.reason
        print(f"BTC ineligibility: {elig.to_dict()}")

    def test_eth_marginal_with_small_equity(self):
        """ETH might be eligible with upscale at ~$23 equity."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "SMALL_ACC_RISK_START": 1.5,
            "SMALL_ACC_RISK_MAX": 3.0,
        })
        
        # ETH: minQty=0.01, price~$3200
        elig = sam.calculate_symbol_eligibility(
            symbol="ETHUSDT",
            equity=23.0,
            current_price=3200.0,
            risk_pct=1.5,
        )
        
        # ETH may be eligible with upscale since risk_needed ~2% < 3% max
        print(f"ETH eligibility: {elig.to_dict()}")
        print(f"  risk_pct_needed: {elig.risk_pct_needed:.2f}%")
        # Key point: with $23, needs ~2-2.5% risk, which is within 3% max
        # So it's ELIGIBLE_WITH_UPSCALE, not ineligible

    def test_sol_marginal_with_small_equity(self):
        """SOL might be marginal with ~$23 equity."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "SMALL_ACC_RISK_START": 1.5,
            "SMALL_ACC_RISK_MAX": 3.0,
        })
        
        # SOL: minQty=0.1, price~$150
        elig = sam.calculate_symbol_eligibility(
            symbol="SOLUSDT",
            equity=23.0,
            current_price=150.0,
            risk_pct=1.5,
        )
        
        # May be eligible with upscale
        print(f"SOL marginal: {elig.to_dict()}")
        print(f"  min_equity_needed: ${elig.min_equity_needed:.2f}")
        print(f"  risk_pct_needed: {elig.risk_pct_needed:.2f}%")

    def test_btc_eligible_with_larger_equity(self):
        """BTC should become eligible with larger equity (~$500)."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "SMALL_ACC_RISK_START": 1.5,
            "SMALL_ACC_RISK_MAX": 3.0,
        })
        
        elig = sam.calculate_symbol_eligibility(
            symbol="BTCUSDT",
            equity=500.0,
            current_price=95000.0,
            risk_pct=1.5,
        )
        
        # May be eligible or need upscale
        print(f"BTC with $500: {elig.to_dict()}")
        # With $500, 1.5% risk = $7.5 risk amount
        # BTC minQty=0.001, at $95k, risk_distance=1.5%*95k=$1425
        # qty = 7.5 / 1425 = 0.0053 > 0.001 minQty
        # Should be eligible
        assert elig.eligible is True


class TestGetEligibleSymbols:
    """Test bulk symbol eligibility check."""

    def test_small_equity_filters_correctly(self):
        """With $23, XRP/SOL/ETH may be eligible, BTC ineligible."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "SMALL_ACC_RISK_START": 1.5,
            "SMALL_ACC_RISK_MAX": 3.0,
        })
        
        prices = {
            "BTCUSDT": 95000.0,
            "ETHUSDT": 3200.0,
            "SOLUSDT": 150.0,
            "XRPUSDT": 0.55,
        }
        
        eligible, ineligible, details = sam.get_eligible_symbols(
            equity=23.0,
            prices=prices,
            all_symbols=list(prices.keys()),
        )
        
        # XRP should definitely be eligible
        assert "XRPUSDT" in eligible
        # BTC should be ineligible (needs ~$95+)
        assert "BTCUSDT" in ineligible
        # ETH may be eligible with upscale (needs ~2% risk < 3% max)
        # SOL may be marginal
        
        print(f"Eligible: {eligible}")
        print(f"Ineligible: {ineligible}")
        for sym, elig in details.items():
            print(f"  {sym}: {elig.reason} (need ${elig.min_equity_needed:.2f})")

    def test_medium_equity_unlocks_more(self):
        """With $100, more symbols should unlock."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "SMALL_ACC_RISK_START": 1.5,
            "SMALL_ACC_RISK_MAX": 3.0,
        })
        
        prices = {
            "BTCUSDT": 95000.0,
            "ETHUSDT": 3200.0,
            "SOLUSDT": 150.0,
            "XRPUSDT": 0.55,
        }
        
        eligible, ineligible, _ = sam.get_eligible_symbols(
            equity=100.0,
            prices=prices,
            all_symbols=list(prices.keys()),
        )
        
        # XRP + SOL should be eligible
        assert "XRPUSDT" in eligible
        assert "SOLUSDT" in eligible
        
        print(f"$100 Eligible: {eligible}")
        print(f"$100 Ineligible: {ineligible}")


class TestProgressiveRiskTuning:
    """Test auto risk tuning on rejections."""

    def test_risk_increases_on_rejections(self):
        """Risk should increase after consecutive rejections."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "SMALL_ACC_RISK_START": 1.5,
            "SMALL_ACC_RISK_MAX": 3.0,
            "SMALL_ACC_RISK_STEP": 0.25,
        })
        
        assert sam.get_current_risk_pct() == 1.5
        
        # First rejection - no change yet
        sam.handle_rejection("SOLUSDT", "BELOW_MIN_QTY")
        assert sam.get_current_risk_pct() == 1.5
        
        # Second consecutive rejection - should increase
        new_risk = sam.handle_rejection("SOLUSDT", "BELOW_MIN_QTY")
        assert new_risk == 1.75
        assert sam.get_current_risk_pct() == 1.75

    def test_risk_capped_at_max(self):
        """Risk should not exceed max."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "SMALL_ACC_RISK_START": 2.5,
            "SMALL_ACC_RISK_MAX": 3.0,
            "SMALL_ACC_RISK_STEP": 0.25,
        })
        
        # Simulate multiple rejections
        sam.handle_rejection("SOLUSDT", "BELOW_MIN_QTY")
        sam.handle_rejection("SOLUSDT", "BELOW_MIN_QTY")  # 2.75
        sam.handle_rejection("SOLUSDT", "BELOW_MIN_QTY")
        sam.handle_rejection("SOLUSDT", "BELOW_MIN_QTY")  # 3.0
        sam.handle_rejection("SOLUSDT", "BELOW_MIN_QTY")
        sam.handle_rejection("SOLUSDT", "BELOW_MIN_QTY")  # Should stay at 3.0
        
        assert sam.get_current_risk_pct() == 3.0

    def test_success_resets_counter(self):
        """Successful trade should reset rejection counter."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "SMALL_ACC_RISK_START": 1.5,
            "SMALL_ACC_RISK_MAX": 3.0,
            "SMALL_ACC_RISK_STEP": 0.25,
        })
        
        # One rejection
        sam.handle_rejection("SOLUSDT", "BELOW_MIN_QTY")
        
        # Success
        sam.handle_success("SOLUSDT")
        
        # Another single rejection should not increase risk
        sam.handle_rejection("SOLUSDT", "BELOW_MIN_QTY")
        assert sam.get_current_risk_pct() == 1.5


class TestDynamicCooldown:
    """Test dynamic cooldown calculation."""

    def test_small_equity_longer_cooldown(self):
        """Smaller equity should have longer cooldown."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "TRADE_COOLDOWN_SEC": 300,
        })
        
        cooldown_small = sam.get_dynamic_cooldown(equity=30.0)
        cooldown_medium = sam.get_dynamic_cooldown(equity=100.0)
        cooldown_large = sam.get_dynamic_cooldown(equity=500.0)
        
        assert cooldown_small > cooldown_medium
        assert cooldown_medium >= cooldown_large
        
        print(f"Cooldowns: $30={cooldown_small}s, $100={cooldown_medium}s, $500={cooldown_large}s")

    def test_loss_streak_increases_cooldown(self):
        """Loss streak should increase cooldown."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "TRADE_COOLDOWN_SEC": 300,
        })
        
        normal = sam.get_dynamic_cooldown(equity=100.0, loss_streak=0)
        after_losses = sam.get_dynamic_cooldown(equity=100.0, loss_streak=3)
        
        assert after_losses > normal
        print(f"Cooldown: normal={normal}s, after 3 losses={after_losses}s")


class TestEquityThresholds:
    """Test equity threshold estimation."""

    def test_estimate_thresholds(self):
        """Should estimate reasonable thresholds."""
        thresholds = estimate_equity_thresholds(risk_pct=1.5)
        
        # XRP should have lowest threshold
        assert "XRPUSDT" in thresholds
        assert thresholds["XRPUSDT"] < thresholds.get("BTCUSDT", float('inf'))
        
        print("Equity thresholds (sorted by min equity):")
        for symbol, min_eq in thresholds.items():
            print(f"  {symbol}: ${min_eq:.2f}")

    def test_recommended_symbols_for_equity(self):
        """Should recommend appropriate symbols for equity."""
        symbols_20 = get_recommended_symbols_for_equity(20.0)
        symbols_100 = get_recommended_symbols_for_equity(100.0)
        symbols_1000 = get_recommended_symbols_for_equity(1000.0)
        
        # More equity = more symbols
        assert len(symbols_100) >= len(symbols_20)
        assert len(symbols_1000) >= len(symbols_100)
        
        print(f"$20: {symbols_20}")
        print(f"$100: {symbols_100}")
        print(f"$1000: {symbols_1000}")


class TestSmallAccountStatus:
    """Test status generation for API."""

    def test_status_generation(self):
        """Should generate complete status dict."""
        sam = SmallAccountManager({
            "SMALL_ACCOUNT_MODE": True,
            "SMALL_ACC_RISK_START": 1.5,
            "SMALL_ACC_RISK_MAX": 3.0,
        })
        
        prices = {
            "BTCUSDT": 95000.0,
            "ETHUSDT": 3200.0,
            "SOLUSDT": 150.0,
            "XRPUSDT": 0.55,
        }
        
        status = sam.get_status(
            equity=23.0,
            prices=prices,
            all_symbols=list(prices.keys()),
        )
        
        status_dict = status.to_dict()
        
        assert "small_account_mode" in status_dict
        assert "eligible_symbols" in status_dict
        assert "ineligible_symbols" in status_dict
        assert "next_equity_thresholds" in status_dict
        
        print("Status:", status_dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
