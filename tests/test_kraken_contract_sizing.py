"""
Tests for Kraken Futures linear contract sizing — ensure_kraken_contracts & validate_kraken_order.

Covers Linear Multi-Collateral specifications (PF_*):
  - Quantity precision enforcement (BTC 4 dec, ETH 3 dec, SOL 2 dec, XRP 0 dec)
  - Passthrough of native crypto units (never multiplied by price)
  - Floor rounding to instrument step
  - Minimum quantity enforcement
  - Invalid size rejection
  - Notional coherence validation
"""
import pytest

from backend.risk.size_calculator import (
    coin_to_kraken_contracts,
    ensure_kraken_contracts,
    validate_kraken_order,
    KRAKEN_SPECS,
)


# ═══════════════════════════════════════════════════════════════
#  test_quantity_to_contracts_conversion
# ═══════════════════════════════════════════════════════════════

class TestQuantityToContractsConversion:
    """Verify coin quantities are correctly formatted in native crypto units."""

    def test_btc_fractional_to_contracts(self):
        """0.003042 BTC floored to 4 decimals -> 0.0030 BTC (not multiplied by price)."""
        contracts = ensure_kraken_contracts(0.003042, "PF_XBTUSD", entry_price=87000.0)
        assert contracts == 0.003
        assert isinstance(contracts, (int, float))

    def test_btc_small_at_high_price(self):
        """0.001 BTC at $90,000 -> 0.001 BTC ($90 notional)."""
        contracts = ensure_kraken_contracts(0.001, "PF_XBTUSD", entry_price=90000.0)
        assert contracts == 0.001

    def test_eth_fractional(self):
        """0.05 ETH at $3,500 -> 0.05 ETH (3 dec)."""
        contracts = ensure_kraken_contracts(0.05, "PF_ETHUSD", entry_price=3500.0)
        assert contracts == 0.05

    def test_sol_fractional(self):
        """0.5 SOL at $150 -> 0.5 SOL (2 dec)."""
        contracts = ensure_kraken_contracts(0.5, "PF_SOLUSD", entry_price=150.0)
        assert contracts == 0.5

    def test_xrp_integer_passthrough(self):
        """300 XRP (prec 0) remains 300 XRP."""
        contracts = ensure_kraken_contracts(300.0, "PF_XRPUSD", entry_price=1.15)
        assert contracts == 300

    def test_coin_to_kraken_original_function(self):
        """Verify coin_to_kraken_contracts returns native crypto quantity."""
        contracts = coin_to_kraken_contracts(0.003042, 87000.0)
        assert contracts == 0.003042

    def test_conversion_is_floor_not_round(self):
        """Conversion floors to 4 decimals without rounding up (0.00309 -> 0.0030)."""
        contracts = ensure_kraken_contracts(0.00309, "PF_XBTUSD", entry_price=87000.0)
        assert contracts == 0.0030


# ═══════════════════════════════════════════════════════════════
#  test_invalid_size_blocked
# ═══════════════════════════════════════════════════════════════

class TestInvalidSizeBlocked:
    """Verify that invalid sizes are properly rejected."""

    def test_contracts_zero_rejected(self):
        valid, reason = validate_kraken_order(0, "PF_XBTUSD")
        assert not valid
        assert "BELOW_MIN" in reason

    def test_contracts_negative_rejected(self):
        valid, reason = validate_kraken_order(-5, "PF_XBTUSD")
        assert not valid
        assert "BELOW_MIN" in reason

    def test_notional_too_high_rejected(self):
        """20 BTC at $87,000 = $1,740,000 > $100,000 cap."""
        valid, reason = validate_kraken_order(
            20.0, "PF_XBTUSD", entry_price=87000.0, max_notional_usd=100_000.0
        )
        assert not valid
        assert "NOTIONAL_TOO_HIGH" in reason

    def test_valid_order_passes(self):
        """0.5 BTC at $87,000 = $43,500 <= $100,000 cap."""
        valid, reason = validate_kraken_order(0.5, "PF_XBTUSD", entry_price=87000.0)
        assert valid
        assert reason is None

    def test_string_contracts_rejected(self):
        valid, reason = validate_kraken_order("invalid", "PF_XBTUSD")  # type: ignore
        assert not valid
        assert "CONTRACTS_NOT_NUMERIC" in reason


# ═══════════════════════════════════════════════════════════════
#  test_min_contract_enforced
# ═══════════════════════════════════════════════════════════════

class TestMinContractEnforced:
    """Verify minimum contract quantity rules."""

    def test_tiny_btc_floors_below_min(self):
        """0.000001 BTC < 0.0001 min -> floors to 0.0 and rejected."""
        contracts = ensure_kraken_contracts(0.000001, "PF_XBTUSD", entry_price=87000.0)
        valid, reason = validate_kraken_order(contracts, "PF_XBTUSD")
        assert not valid
        assert "BELOW_MIN" in reason

    def test_zero_qty_rejected(self):
        contracts = ensure_kraken_contracts(0.0, "PF_XBTUSD", entry_price=87000.0)
        valid, reason = validate_kraken_order(contracts, "PF_XBTUSD")
        assert not valid
        assert "BELOW_MIN" in reason

    def test_min_contract_validates(self):
        """0.0001 BTC is min size and validates."""
        valid, reason = validate_kraken_order(0.0001, "PF_XBTUSD")
        assert valid


# ═══════════════════════════════════════════════════════════════
#  test_all_symbols_in_specs
# ═══════════════════════════════════════════════════════════════

class TestKrakenSpecs:
    """Verify all expected symbols are in KRAKEN_SPECS with linear specs."""

    @pytest.mark.parametrize("symbol,expected_min", [
        ("PF_XBTUSD", 0.0001),
        ("PF_ETHUSD", 0.001),
        ("PF_SOLUSD", 0.01),
        ("PF_XRPUSD", 1.0),
    ])
    def test_symbol_has_specs(self, symbol, expected_min):
        assert symbol in KRAKEN_SPECS
        assert KRAKEN_SPECS[symbol]["min_qty"] == expected_min
        assert KRAKEN_SPECS[symbol]["qty_step"] == expected_min


# ═══════════════════════════════════════════════════════════════
#  Integration-style tests
# ═══════════════════════════════════════════════════════════════

class TestEndToEndFlow:
    """Simulate linear order sizing values."""

    def test_realistic_btc_trade(self):
        """$500 risk with 2% SL on BTC at $87,000."""
        # risk = 500, sl_dist = 1740 -> raw_qty = 500 / 1740 = 0.28735 BTC
        coin_qty = 0.28735
        price = 87000.0

        contracts = ensure_kraken_contracts(coin_qty, "PF_XBTUSD", entry_price=price)
        assert contracts == 0.2873  # 4 decimals floored

        valid, reason = validate_kraken_order(contracts, "PF_XBTUSD", entry_price=price, max_notional_usd=100000.0)
        assert valid

    def test_realistic_eth_trade(self):
        """0.1 ETH at $3,500 = $350 notional."""
        contracts = ensure_kraken_contracts(0.1, "PF_ETHUSD", entry_price=3500.0)
        assert contracts == 0.1
        valid, _ = validate_kraken_order(contracts, "PF_ETHUSD", entry_price=3500.0)
        assert valid

    def test_the_exact_bug_scenario_prevented(self):
        """Bug prevention: 0.003042 BTC must NOT be multiplied by $87,000 into 264 BTC.
        It must remain 0.0030 BTC."""
        raw_btc = 0.003042
        btc_price = 87000.0

        contracts = ensure_kraken_contracts(raw_btc, "PF_XBTUSD", entry_price=btc_price)
        assert contracts == 0.0030  # 0.0030 BTC ($261 notional), NEVER 264 BTC!
        valid, _ = validate_kraken_order(contracts, "PF_XBTUSD", entry_price=btc_price)
        assert valid
