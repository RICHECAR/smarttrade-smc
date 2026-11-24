"""
Test Pre-flight Sizing Rejection Reasons

Tests the order sizing validation and rejection logic:
- QTY below min_qty with risk deviation too high
- Notional below min_notional
- compute_min_viable_risk_usd helper
- log_skip_small_size consolidated logging

Usage:
    pytest tests/test_preflight_sizing.py -v
"""

import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from smc_futures.engine.execution.contract_specs import ContractSpecs, get_contract_specs
from smc_futures.engine.execution.trade_feasibility import (
    is_trade_feasible,
    compute_min_viable_risk_usd,
    compute_min_risk_for_symbol,
    compute_min_balance_for_symbol,
    log_skip_small_size,
    FeasibilityResult,
    RejectionReason,
)

# Configure logging for tests
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: compute_min_viable_risk_usd
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeMinViableRiskUsd:
    """Tests for the min viable risk calculator."""
    
    def test_btc_min_viable_risk(self):
        """Test min viable risk for BTC with typical stop."""
        result = compute_min_viable_risk_usd(
            symbol="BTCUSDT",
            entry_price=95000.0,
            stop_price=94050.0,  # ~1% stop
        )
        
        assert "min_risk_usd" in result
        assert "min_qty" in result
        assert "min_balance_1pct" in result
        assert result["min_risk_usd"] > 0
        assert result["limiting_factor"] in ("min_qty", "min_notional")
        
        logger.info(f"BTC min_risk: ${result['min_risk_usd']:.2f}, limiting: {result['limiting_factor']}")
    
    def test_xrp_min_viable_risk(self):
        """Test min viable risk for XRP (often problematic for small accounts)."""
        result = compute_min_viable_risk_usd(
            symbol="XRPUSDT",
            entry_price=2.50,
            stop_price=2.475,  # 1% stop
        )
        
        assert result["min_risk_usd"] > 0
        assert result["min_balance_1pct"] > 0
        
        logger.info(f"XRP min_risk: ${result['min_risk_usd']:.4f}, min_balance_1pct: ${result['min_balance_1pct']:.0f}")
    
    def test_eth_min_viable_risk(self):
        """Test min viable risk for ETH."""
        result = compute_min_viable_risk_usd(
            symbol="ETHUSDT",
            entry_price=3500.0,
            stop_price=3465.0,  # ~1% stop
        )
        
        assert result["min_risk_usd"] > 0
        # ETH with 0.01 min_qty and $35 stop = $0.35 min risk
        logger.info(f"ETH min_risk: ${result['min_risk_usd']:.2f}")


# ══════════════════════════════════════════════════════════════════════════════
# TEST: is_trade_feasible
# ══════════════════════════════════════════════════════════════════════════════

class TestIsTradeFeasible:
    """Tests for the trade feasibility checker."""
    
    def test_feasible_large_account(self):
        """Test feasible trade with large account."""
        result = is_trade_feasible(
            symbol="BTCUSDT",
            balance=10000.0,
            risk_pct=0.01,  # 1% = $100 risk
            entry_price=95000.0,
            stop_price=94050.0,  # ~1% stop
        )
        
        assert result.feasible
        assert result.reason == RejectionReason.FEASIBLE
    
    def test_infeasible_small_account_high_deviation(self):
        """Test infeasible trade - quantity too small, clamp exceeds deviation."""
        result = is_trade_feasible(
            symbol="ETHUSDT",
            balance=50.0,  # Very small account
            risk_pct=0.01,  # 1% = $0.50 risk
            entry_price=3500.0,
            stop_price=3465.0,  # $35 stop distance
        )
        
        # raw_qty = $0.50 / $35 = 0.0143 ETH
        # min_qty = 0.01 ETH
        # But notional = 0.0143 * 3500 = $50, need min_notional = $5
        # Should be feasible actually if notional is OK
        
        # Let me check with tighter stop
        result = is_trade_feasible(
            symbol="ETHUSDT",
            balance=10.0,  # Very small 
            risk_pct=0.01,  # 1% = $0.10 risk
            entry_price=3500.0,
            stop_price=3499.0,  # $1 stop distance
        )
        
        # raw_qty = $0.10 / $1 = 0.1 ETH
        # But that's above min_qty=0.01, so should be OK?
        # The issue is notional: 0.1 * 3500 = $350 > $5 min_notional
        # So this should pass
        
        # Let me test the actual failure case
        result = is_trade_feasible(
            symbol="ETHUSDT",
            balance=5.0,  # $5 account
            risk_pct=0.01,  # 1% = $0.05 risk
            entry_price=3500.0,
            stop_price=3465.0,  # $35 stop
        )
        
        # raw_qty = $0.05 / $35 = 0.00143 ETH
        # min_qty = 0.01 ETH
        # deviation = (0.01 - 0.00143) / 0.00143 * 100 = 599%
        # Should be rejected
        
        assert not result.feasible
        assert result.reason == RejectionReason.CLAMP_EXCEEDS_DEVIATION
        assert "suggested_action" in result.to_dict()
    
    def test_infeasible_hard_risk_cap(self):
        """Test rejection when risk exceeds hard cap (5%)."""
        result = is_trade_feasible(
            symbol="BTCUSDT",
            balance=10000.0,
            risk_pct=0.10,  # 10% risk - exceeds 5% cap
            entry_price=95000.0,
            stop_price=94050.0,
        )
        
        assert not result.feasible
        assert result.reason == RejectionReason.HARD_RISK_CAP_EXCEEDED
    
    def test_feasibility_result_to_dict(self):
        """Test FeasibilityResult serialization."""
        result = is_trade_feasible(
            symbol="BTCUSDT",
            balance=10000.0,
            risk_pct=0.01,
            entry_price=95000.0,
            stop_price=94050.0,
        )
        
        d = result.to_dict()
        assert "feasible" in d
        assert "reason" in d
        assert "message" in d
        assert "details" in d


# ══════════════════════════════════════════════════════════════════════════════
# TEST: normalize_quantity rejection
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizeQuantity:
    """Tests for ContractSpecs.normalize_quantity rejection logic."""
    
    def test_qty_rejected_high_deviation(self):
        """Test quantity rejection when deviation exceeds max."""
        specs = get_contract_specs("ETHUSDT")
        
        # Disable AUTO-ADJUST risk USD cap so deviation % rejection triggers
        with patch.dict(os.environ, {"MAX_RISK_USD_CAP": "0"}):
            # Very small qty that would require huge clamp
            normalized_qty, status, details = specs.normalize_quantity(
                raw_qty=0.0001,  # Way below min_qty=0.01
                entry_price=3500.0,
                max_risk_deviation_pct=50.0,
            )
        
        assert normalized_qty is None
        assert status == "REJECTED"
        assert "deviation_pct" in details
        assert details["deviation_pct"] > 50.0  # Should be ~9900%
        assert "SKIPPED_SMALL_SIZE" in details.get("status", "")
    
    def test_qty_clamped_acceptable_deviation(self):
        """Test quantity clamping when deviation is acceptable."""
        specs = get_contract_specs("ETHUSDT")
        
        # Qty slightly below min_qty
        normalized_qty, status, details = specs.normalize_quantity(
            raw_qty=0.008,  # Below min_qty=0.01, but 25% deviation
            entry_price=3500.0,
            max_risk_deviation_pct=50.0,
        )
        
        assert normalized_qty == 0.01  # Clamped to min_qty
        assert status == "CLAMPED"
    
    def test_qty_ok_no_clamp(self):
        """Test quantity passes without clamping."""
        specs = get_contract_specs("BTCUSDT")
        
        normalized_qty, status, details = specs.normalize_quantity(
            raw_qty=0.005,  # Above min_qty=0.001
            entry_price=95000.0,
            max_risk_deviation_pct=50.0,
        )
        
        assert normalized_qty is not None
        assert status in ("OK", "CLAMPED")


# ══════════════════════════════════════════════════════════════════════════════
# TEST: log_skip_small_size
# ══════════════════════════════════════════════════════════════════════════════

class TestLogSkipSmallSize:
    """Tests for the consolidated logging function."""
    
    def test_log_format(self, caplog):
        """Test that log_skip_small_size produces expected format."""
        with caplog.at_level(logging.WARNING):
            log_skip_small_size(
                symbol="ETHUSDT",
                raw_qty=0.0001,
                min_qty=0.01,
                risk_amount=0.50,
                deviation_pct=9900.0,
                balance=50.0,
                min_balance_needed=500.0,
            )
        
        assert len(caplog.records) == 1
        record = caplog.records[0]
        
        assert "SKIPPED_SMALL_SIZE" in record.message
        assert "ETHUSDT" in record.message
        assert "min_balance_needed" in record.message


# ══════════════════════════════════════════════════════════════════════════════
# TEST: compute_min_balance_for_symbol
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeMinBalance:
    """Tests for minimum balance calculator."""
    
    def test_min_balance_calculation(self):
        """Test minimum balance calculation."""
        min_balance, details = compute_min_balance_for_symbol(
            symbol="ETHUSDT",
            entry_price=3500.0,
            stop_price=3465.0,  # $35 stop
            risk_pct=0.01,  # 1% risk
        )
        
        # min_risk = 0.01 * $35 = $0.35
        # min_balance = $0.35 / 0.01 = $35
        assert min_balance > 0
        assert "min_balance_usdt" in details
        
        logger.info(f"Min balance for ETH at 1% risk: ${min_balance:.0f}")
    
    def test_min_balance_different_risk_pcts(self):
        """Test minimum balance at different risk percentages."""
        for risk_pct in [0.01, 0.02, 0.05]:
            min_balance, _ = compute_min_balance_for_symbol(
                symbol="BTCUSDT",
                entry_price=95000.0,
                stop_price=94050.0,
                risk_pct=risk_pct,
            )
            
            logger.info(f"BTC min_balance at {risk_pct*100:.0f}% risk: ${min_balance:.0f}")
            assert min_balance > 0


# ══════════════════════════════════════════════════════════════════════════════
# RUN TESTS
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
