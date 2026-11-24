"""
Tests for LiquidationModel — Chantier 2
"""

import pytest
from smc_futures.risk.liquidation_model import (
    calculate_liquidation_price,
    get_maintenance_margin,
    liquidation_distance_pct,
)


class TestMaintenanceMargin:
    """Correct MM rates per broker per symbol."""

    def test_bitget_btc_mm(self):
        assert get_maintenance_margin("BTCUSDT", "bitget") == 0.004

    def test_bitget_eth_mm(self):
        assert get_maintenance_margin("ETHUSDT", "bitget") == 0.005

    def test_bitget_altcoin_default(self):
        assert get_maintenance_margin("SOLUSDT", "bitget") == 0.005

    def test_kraken_btc_mm(self):
        assert get_maintenance_margin("PF_XBTUSD", "kraken") == 0.005

    def test_kraken_default(self):
        assert get_maintenance_margin("PF_SOLUSD", "kraken") == 0.01


class TestLiquidationPrice:
    """Correct liq price vs old buggy calc."""

    def test_long_btc_10x_bitget(self):
        """Long BTC at 60k, 10x, mm=0.004 → liq below entry."""
        liq = calculate_liquidation_price(60000, 10, "long", "BTCUSDT", "bitget")
        # Liq = 60000 / (1 + 0.1 - 0.004) = 60000 / 1.096 ≈ 54744.5
        assert liq < 60000, f"Long liq {liq} should be below entry 60000"
        assert 54000 < liq < 55500  # sanity range
        assert liq > 0

    def test_short_btc_10x_bitget(self):
        """Short BTC at 60k, 10x leverage, mm=0.004"""
        liq = calculate_liquidation_price(60000, 10, "short", "BTCUSDT", "bitget")
        assert liq > 60000, f"Short liq {liq} should be above entry 60000"

    def test_old_mm_was_wrong(self):
        """The old code used mm=0.01. Using correct mm=0.004 should give a DIFFERENT liq."""
        liq_correct = calculate_liquidation_price(60000, 10, "long", "BTCUSDT", "bitget")
        liq_old = calculate_liquidation_price(60000, 10, "long", "BTCUSDT", "bitget", maintenance_margin=0.01)
        assert liq_correct != liq_old

    def test_distance_positive(self):
        dist = liquidation_distance_pct(60000, 10, "long", "BTCUSDT", "bitget")
        assert dist > 0

    def test_leverage_1x_wide_distance(self):
        """1x leverage should have very wide distance to liquidation."""
        dist = liquidation_distance_pct(60000, 1, "long", "BTCUSDT", "bitget")
        # At 1x: liq = entry / (1 + 1/1 - 0.004) = entry / 1.996 ≈ 30060
        # Distance ≈ 50%
        assert dist > 40
