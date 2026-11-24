"""
Tests for contract_specs.py — Qty validation to prevent 45111 errors
═══════════════════════════════════════════════════════════════════════

Usage:
    pytest tests/test_contract_specs.py -v
"""

import pytest
import sys
import os
from unittest.mock import patch

# Ensure project root in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smc_futures.engine.execution.contract_specs import (
    ContractSpecs,
    get_contract_specs,
    CONTRACT_SPECS_REGISTRY,
    DEFAULT_CONTRACT_SPECS,
)


class TestContractSpecs:
    """Test ContractSpecs class methods."""

    def test_normalize_quantity_ok(self):
        """Quantity already valid — no clamp needed."""
        specs = ContractSpecs(
            symbol="BTCUSDT",
            min_qty=0.001,
            min_notional=5.0,
            qty_step=0.001,
            price_scale=1,
            qty_scale=3,
        )
        
        qty, status, details = specs.normalize_quantity(
            raw_qty=0.005,  # Above min_qty
            entry_price=60000.0  # Notional = 300 USD (well above 5)
        )
        
        assert qty == 0.005
        assert status == "OK"
        assert "final_qty" in details

    def test_normalize_quantity_clamp_to_min_qty(self):
        """Quantity below min_qty — must clamp up."""
        specs = ContractSpecs(
            symbol="SOLUSDT",
            min_qty=0.1,
            min_notional=5.0,
            qty_step=0.1,
            price_scale=2,
            qty_scale=1,
        )
        
        qty, status, details = specs.normalize_quantity(
            raw_qty=0.04,  # Below min_qty=0.1
            entry_price=150.0,  # Notional would be 6 USD with 0.04
            max_risk_deviation_pct=200.0  # Allow big deviation for test
        )
        
        assert qty == 0.1  # Clamped to min
        assert status == "CLAMPED"
        assert "deviation_pct" in details

    def test_normalize_quantity_reject_high_deviation(self):
        """Quantity too small, clamp would exceed risk tolerance — reject."""
        specs = ContractSpecs(
            symbol="XRPUSDT",
            min_qty=1.0,
            min_notional=5.0,
            qty_step=1.0,
            price_scale=4,
            qty_scale=0,
        )
        
        qty, status, details = specs.normalize_quantity(
            raw_qty=0.2,  # Very below min_qty=1.0
            entry_price=0.5,  # Low price
            max_risk_deviation_pct=50.0  # Clamp 0.2 -> 1.0 = 400% deviation
        )
        
        assert qty is None
        assert status == "REJECTED"
        assert "rejection_reason" in details

    def test_normalize_quantity_notional_check(self):
        """Quantity valid but notional below minimum."""
        specs = ContractSpecs(
            symbol="BTCUSDT",
            min_qty=0.001,
            min_notional=5.0,
            qty_step=0.001,
            price_scale=1,
            qty_scale=3,
        )
        
        qty, status, details = specs.normalize_quantity(
            raw_qty=0.001,
            entry_price=1000.0,  # Notional = 1.0 USD < 5.0 min
            max_risk_deviation_pct=500.0  # Allow big deviation
        )
        
        # Should clamp up for notional
        assert qty is not None
        assert qty * 1000.0 >= 5.0  # Notional >= min_notional
        assert status == "CLAMPED"

    def test_normalize_quantity_reject_notional_deviation(self):
        """Notional too small, needed clamp exceeds risk tolerance."""
        specs = ContractSpecs(
            symbol="BTCUSDT",
            min_qty=0.001,
            min_notional=5.0,
            qty_step=0.001,
            price_scale=1,
            qty_scale=3,
        )
        
        qty, status, details = specs.normalize_quantity(
            raw_qty=0.001,
            entry_price=500.0,  # Notional = 0.5 USD << 5.0
            max_risk_deviation_pct=50.0  # Very tight
        )
        
        # min_qty_for_notional = 5.0 / 500.0 = 0.01
        # deviation = (0.01 - 0.001) / 0.001 = 900%
        assert qty is None
        assert status == "REJECTED"

    def test_round_to_step(self):
        """Test step rounding logic."""
        specs = ContractSpecs(
            symbol="SOLUSDT",
            min_qty=0.1,
            min_notional=5.0,
            qty_step=0.1,
            price_scale=2,
            qty_scale=1,
        )
        
        # 0.15 should round down to 0.1
        assert specs._round_to_step(0.15) == 0.1
        
        # 0.19 should round down to 0.1
        assert specs._round_to_step(0.19) == 0.1
        
        # 0.21 should round down to 0.2
        assert specs._round_to_step(0.21) == 0.2

    def test_round_to_step_up(self):
        """Test step rounding up logic."""
        specs = ContractSpecs(
            symbol="SOLUSDT",
            min_qty=0.1,
            min_notional=5.0,
            qty_step=0.1,
            price_scale=2,
            qty_scale=1,
        )
        
        # 0.15 should round up to 0.2
        assert specs._round_to_step(0.15, round_up=True) == 0.2


class TestGetContractSpecs:
    """Test contract specs registry lookup."""

    def test_get_known_symbol(self):
        """Known symbol returns correct specs."""
        specs = get_contract_specs("BTCUSDT")
        
        assert specs.symbol == "BTCUSDT"
        assert specs.min_qty == 0.001
        assert specs.min_notional == 5.0

    def test_get_known_symbol_with_slash(self):
        """Handles BTC/USDT format."""
        specs = get_contract_specs("BTC/USDT")
        
        assert specs.symbol == "BTCUSDT"
        assert specs.min_qty == 0.001

    def test_get_unknown_symbol_uses_default(self):
        """Unknown symbol falls back to defaults."""
        specs = get_contract_specs("UNKNOWNUSDT")
        
        assert specs.symbol == "UNKNOWNUSDT"
        assert specs.min_qty == DEFAULT_CONTRACT_SPECS.min_qty
        assert specs.min_notional == DEFAULT_CONTRACT_SPECS.min_notional


class TestRealWorldScenarios:
    """Test scenarios from real 45111 errors."""

    def test_sol_small_qty_rejection(self):
        """
        Real case: SOL qty=0.0042 rejected with 45111.
        SOL min_qty=0.1, so 0.0042 << 0.1.
        """
        specs = get_contract_specs("SOLUSDT")
        
        # User's calculated qty was 0.0042
        with patch.dict(os.environ, {"MAX_RISK_USD_CAP": "0"}):
            qty, status, details = specs.normalize_quantity(
                raw_qty=0.0042,
                entry_price=143.5,
                max_risk_deviation_pct=50.0  # Reasonable tolerance
            )
        
        # Clamp to 0.1 would be 2280% increase — rejected!
        assert qty is None
        assert status == "REJECTED"
        print(f"SOL rejection details: {details}")

    def test_xrp_small_qty_rejection(self):
        """
        Real case: XRP qty=0.23 rejected with 45111.
        XRP min_qty=1.0, so 0.23 < 1.0.
        """
        specs = get_contract_specs("XRPUSDT")
        
        with patch.dict(os.environ, {"MAX_RISK_USD_CAP": "0"}):
            qty, status, details = specs.normalize_quantity(
                raw_qty=0.23,
                entry_price=0.55,
                max_risk_deviation_pct=50.0
            )
        
        # Clamp to 1.0 would be 335% increase — rejected!
        assert qty is None
        assert status == "REJECTED"
        print(f"XRP rejection details: {details}")

    def test_btc_valid_order(self):
        """Valid BTC order should pass."""
        specs = get_contract_specs("BTCUSDT")
        
        qty, status, details = specs.normalize_quantity(
            raw_qty=0.01,  # 0.01 BTC
            entry_price=60000.0,  # Notional = 600 USD
            max_risk_deviation_pct=50.0
        )
        
        assert qty == 0.01
        assert status == "OK"

    def test_eth_clamp_acceptable(self):
        """ETH order that needs minor clamp — should be accepted."""
        specs = get_contract_specs("ETHUSDT")
        
        qty, status, details = specs.normalize_quantity(
            raw_qty=0.008,  # Below min_qty=0.01
            entry_price=3500.0,  # Notional = 28 USD (ok)
            max_risk_deviation_pct=50.0  # 0.008 -> 0.01 = 25% increase (acceptable)
        )
        
        assert qty == 0.01
        assert status == "CLAMPED"


class TestPreFlightValidation:
    """Test pre-flight validation method."""

    def test_validate_qty_pre_flight_pass(self):
        """Valid qty passes pre-flight."""
        specs = get_contract_specs("BTCUSDT")
        
        is_valid, reason, details = specs.validate_qty_pre_flight(
            qty=0.01,
            entry_price=60000.0
        )
        
        assert is_valid is True
        assert reason == "OK"

    def test_validate_qty_pre_flight_fail_min_qty(self):
        """Below min_qty fails pre-flight."""
        specs = get_contract_specs("BTCUSDT")
        
        is_valid, reason, details = specs.validate_qty_pre_flight(
            qty=0.0001,  # << 0.001
            entry_price=60000.0
        )
        
        assert is_valid is False
        assert reason == "BELOW_MIN_QTY"

    def test_validate_qty_pre_flight_fail_notional(self):
        """Below min_notional fails pre-flight."""
        specs = get_contract_specs("BTCUSDT")
        
        is_valid, reason, details = specs.validate_qty_pre_flight(
            qty=0.001,
            entry_price=1000.0  # Notional = 1 USD < 5 USD
        )
        
        assert is_valid is False
        assert reason == "BELOW_MIN_NOTIONAL"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
