"""
LiquidationModel — Broker-Aware Liquidation Price Calculation
═════════════════════════════════════════════════════════════
The futures_engine had maintenance_margin hardcoded at 0.01 (1%).
Bitget USDT-M Perps use 0.004–0.005 for BTC/ETH tier 1.
Wrong liq price → wrong liquidation_distance → wrong position sizing safety.

This module provides:
  - Broker-specific maintenance margin rates
  - Tiered maintenance margin (Bitget uses volume-based tiers)
  - Correct liq price formulas for cross/isolated USDT-M
"""

import logging
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class Broker(str, Enum):
    BITGET = "bitget"
    KRAKEN = "kraken"


# Bitget USDT-M Perpetual maintenance margin rates (Tier 1)
# Source: https://www.bitget.com/futures/introduction/margin
# BTC: 0.4%, ETH: 0.5%, altcoins: 0.5%-1.0%
_BITGET_MM_MAP: Dict[str, float] = {
    "BTCUSDT": 0.004,
    "ETHUSDT": 0.005,
    # Default for altcoins
    "_DEFAULT": 0.005,
}

# Kraken Futures maintenance margin rates
# Source: https://support.kraken.com/hc/en-us/articles/margin-requirements
_KRAKEN_MM_MAP: Dict[str, float] = {
    "PF_XBTUSD": 0.005,
    "PF_ETHUSD": 0.01,
    "_DEFAULT": 0.01,
}


def get_maintenance_margin(
    symbol: str,
    broker: str = "bitget",
) -> float:
    """
    Get maintenance margin rate for a symbol on a given broker.

    Returns:
        Float between 0 and 1 (e.g. 0.005 = 0.5%)
    """
    broker_lower = broker.lower()
    sym_upper = symbol.upper().replace("/", "")

    if broker_lower == "kraken":
        mm_map = _KRAKEN_MM_MAP
    else:
        mm_map = _BITGET_MM_MAP

    return mm_map.get(sym_upper, mm_map["_DEFAULT"])


def calculate_liquidation_price(
    entry_price: float,
    leverage: float,
    side: str,
    symbol: str = "BTCUSDT",
    broker: str = "bitget",
    maintenance_margin: Optional[float] = None,
) -> float:
    """
    Calculate liquidation price for a USDT-M perpetual position.

    For isolated margin:
      Long:  liq = entry * (1 - 1/leverage + mm)
             → rearranged: liq = entry / (1 + mm - 1/leverage)
             → but only valid when mm < 1/leverage
      Short: liq = entry / (1 - mm + 1/leverage)

    Args:
        entry_price: Entry price of the position
        leverage: Leverage used
        side: "long" or "short"
        symbol: Trading pair symbol
        broker: Broker name
        maintenance_margin: Override MM rate (if None, looked up from broker)

    Returns:
        Liquidation price (always > 0)
    """
    if maintenance_margin is None:
        maintenance_margin = get_maintenance_margin(symbol, broker)

    if leverage <= 0 or entry_price <= 0:
        return 0.0

    inv_lev = 1.0 / leverage

    if side.lower() == "long":
        # Long liquidation: price must DROP to liquidate
        # Liq = entry / (1 + 1/leverage - mm)
        denominator = 1.0 + inv_lev - maintenance_margin
        if denominator <= 0:
            return 0.0
        liq = entry_price / denominator
    else:
        # Short liquidation: price must RISE to liquidate
        # Liq = entry / (1 - 1/leverage + mm)
        denominator = 1.0 - inv_lev + maintenance_margin
        if denominator <= 0:
            return float("inf")
        liq = entry_price / denominator

    return max(liq, 0.0)


def liquidation_distance_pct(
    entry_price: float,
    leverage: float,
    side: str,
    symbol: str = "BTCUSDT",
    broker: str = "bitget",
) -> float:
    """
    Convenience: returns the % distance from entry to liquidation.
    """
    liq = calculate_liquidation_price(entry_price, leverage, side, symbol, broker)
    if entry_price <= 0:
        return 0.0
    return abs(entry_price - liq) / entry_price * 100
