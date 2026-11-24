"""
MAE / MFE Calculator — Maximum Adverse & Favorable Excursion.
═════════════════════════════════════════════════════════════

Computes intra-trade excursions from OHLC candle data.

If candle data is unavailable or insufficient, returns explicit
``None`` markers — NEVER fabricates values.

Author: Chantier 5 — Trade Journal Institutionnel
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

logger = logging.getLogger("smarttrade.analytics.mae_mfe")


@dataclass(frozen=True)
class ExcursionResult:
    """Result of MAE/MFE computation."""

    mae: Optional[float] = None          # Worst unrealized loss (always >= 0 if computed)
    mfe: Optional[float] = None          # Best unrealized gain (always >= 0 if computed)
    max_dd_during_trade: Optional[float] = None  # Max intra-trade drawdown
    excursion_ratio: Optional[float] = None      # MFE / MAE (> 1 = good trade location)
    capture_ratio: Optional[float] = None        # realized_pnl / MFE
    data_quality: str = "unavailable"    # "full", "partial", "unavailable"
    candles_used: int = 0


def compute_excursions(
    side: str,
    entry_price: float,
    exit_price: float,
    candles: Optional[Sequence[dict]] = None,
    realized_pnl: Optional[float] = None,
) -> ExcursionResult:
    """
    Compute MAE/MFE from intra-trade candle data.

    Parameters
    ----------
    side : str
        "long" or "short" (case-insensitive).
    entry_price : float
        Actual fill price at entry.
    exit_price : float
        Actual fill price at exit.
    candles : list of dict, optional
        OHLC candles during the trade lifetime.
        Each candle must have ``high`` and ``low`` keys (float).
    realized_pnl : float, optional
        Actual PnL for capture ratio.

    Returns
    -------
    ExcursionResult
        With ``data_quality`` = ``"unavailable"`` if candles are missing.
    """
    if entry_price <= 0:
        return ExcursionResult(data_quality="unavailable")

    if not candles or len(candles) == 0:
        return ExcursionResult(data_quality="unavailable")

    is_long = side.strip().lower() == "long"

    highs: List[float] = []
    lows: List[float] = []
    valid = 0

    for c in candles:
        h = c.get("high") or c.get("High")
        lo = c.get("low") or c.get("Low")
        if h is not None and lo is not None:
            try:
                h = float(h)
                lo = float(lo)
            except (TypeError, ValueError):
                continue
            if h > 0 and lo > 0:
                highs.append(h)
                lows.append(lo)
                valid += 1

    if valid == 0:
        return ExcursionResult(data_quality="unavailable")

    if is_long:
        # Long: MAE = max loss = entry - lowest low
        #        MFE = max gain = highest high - entry
        worst_price = min(lows)
        best_price = max(highs)
        mae = max(entry_price - worst_price, 0.0)
        mfe = max(best_price - entry_price, 0.0)
    else:
        # Short: MAE = max loss = highest high - entry
        #         MFE = max gain = entry - lowest low
        worst_price = max(highs)
        best_price = min(lows)
        mae = max(worst_price - entry_price, 0.0)
        mfe = max(entry_price - best_price, 0.0)

    # Max intra-trade drawdown relative to entry
    max_dd = (mae / entry_price) * 100.0 if entry_price > 0 else None

    # Excursion ratio
    excursion_ratio: Optional[float] = None
    if mae > 0:
        excursion_ratio = round(mfe / mae, 4)
    elif mfe > 0:
        excursion_ratio = float("inf")

    # Capture ratio: how much of MFE was actually realized
    capture_ratio: Optional[float] = None
    if realized_pnl is not None and mfe > 0:
        capture_ratio = round(realized_pnl / mfe, 4)

    quality = "full" if valid >= 3 else "partial"

    return ExcursionResult(
        mae=round(mae, 8),
        mfe=round(mfe, 8),
        max_dd_during_trade=round(max_dd, 4) if max_dd is not None else None,
        excursion_ratio=excursion_ratio,
        capture_ratio=capture_ratio,
        data_quality=quality,
        candles_used=valid,
    )


def compute_slippage_bps(expected: float, filled: float) -> Optional[float]:
    """
    Slippage in basis points.  Positive = unfavorable.

    Returns None if expected is 0 or missing.
    """
    if not expected or expected <= 0:
        return None
    return round(abs(filled - expected) / expected * 10_000, 2)
