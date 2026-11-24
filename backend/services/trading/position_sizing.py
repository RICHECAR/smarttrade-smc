"""
Position Sizing Engine — Confidence-Based Dynamic Sizing
════════════════════════════════════════════════════════

Formula: size = base_size × confidence × streak_mult × volatility_mult

Inputs:
  - balance, risk_pct, entry_price, stop_loss (standard sizing)
  - confidence_score (from DecisionEngine, 0–1)
  - recent_pnl streak (win/loss adjustments)
  - volatility_regime (scale down in high vol)

Config via .env:
  SIZING_BASE_RISK_PCT=1.0
  SIZING_MAX_RISK_PCT=3.0
  SIZING_WIN_STREAK_BOOST=0.1
  SIZING_LOSS_STREAK_CUT=0.2
  SIZING_MAX_STREAK_EFFECT=0.5
  SIZING_VOL_HIGH_MULT=0.7
  SIZING_VOL_EXTREME_MULT=0.3
  SIZING_MIN_NOTIONAL=5.0
"""

import logging
import math
import os
from typing import Optional

from backend.services.trading.models import (
    SizingResult,
    TradeSignal,
    VolatilityRegime,
)

logger = logging.getLogger("smarttrade.trading.sizing")


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class PositionSizingEngine:
    """
    Computes position quantity from signal confidence and market conditions.

    base_risk = balance × risk_pct
    risk_per_unit = |entry - stop_loss|
    raw_qty = base_risk / risk_per_unit
    final_qty = raw_qty × confidence × streak × vol_mult
    """

    def __init__(self) -> None:
        self._base_risk_pct = _envf("SIZING_BASE_RISK_PCT", 1.0) / 100.0
        self._max_risk_pct = _envf("SIZING_MAX_RISK_PCT", 3.0) / 100.0
        self._win_boost = _envf("SIZING_WIN_STREAK_BOOST", 0.1)
        self._loss_cut = _envf("SIZING_LOSS_STREAK_CUT", 0.2)
        self._max_streak_effect = _envf("SIZING_MAX_STREAK_EFFECT", 0.5)
        self._vol_high_mult = _envf("SIZING_VOL_HIGH_MULT", 0.7)
        self._vol_extreme_mult = _envf("SIZING_VOL_EXTREME_MULT", 0.3)
        self._min_notional = _envf("SIZING_MIN_NOTIONAL", 5.0)

    def compute(
        self,
        signal: TradeSignal,
        balance: float,
        consecutive_wins: int = 0,
        consecutive_losses: int = 0,
        volatility_regime: VolatilityRegime = VolatilityRegime.NORMAL,
        qty_step: float = 0.001,
        min_qty: float = 0.001,
    ) -> SizingResult:
        """
        Compute position size for a given signal and account state.

        Returns SizingResult with the final quantity and all multipliers.
        """
        # ── Validation ──
        if balance <= 0:
            return SizingResult(rejected=True, reject_reason="balance <= 0")

        risk_per_unit = abs(signal.entry_price - signal.stop_loss)
        if risk_per_unit <= 0:
            return SizingResult(rejected=True, reject_reason="SL equals entry")

        # ── Base risk amount ──
        effective_risk_pct = self._base_risk_pct * signal.confidence
        effective_risk_pct = min(effective_risk_pct, self._max_risk_pct)
        base_risk = balance * effective_risk_pct

        # ── Raw quantity ──
        raw_qty = base_risk / risk_per_unit

        # ── Confidence multiplier ──
        # Linear scale: 0.4 conf → 0.4x, 1.0 conf → 1.0x
        conf_mult = max(0.2, min(1.0, signal.confidence))

        # ── Streak multiplier ──
        streak_mult = 1.0
        if consecutive_wins > 0:
            boost = min(consecutive_wins * self._win_boost, self._max_streak_effect)
            streak_mult = 1.0 + boost
        elif consecutive_losses > 0:
            cut = min(consecutive_losses * self._loss_cut, self._max_streak_effect)
            streak_mult = max(0.3, 1.0 - cut)

        # ── Volatility multiplier ──
        vol_mult = {
            VolatilityRegime.LOW: 1.0,
            VolatilityRegime.NORMAL: 1.0,
            VolatilityRegime.HIGH: self._vol_high_mult,
            VolatilityRegime.EXTREME: self._vol_extreme_mult,
        }.get(volatility_regime, 1.0)

        # ── Final quantity ──
        final_mult = conf_mult * streak_mult * vol_mult
        final_qty = raw_qty * final_mult

        # Floor to qty_step
        if qty_step > 0:
            final_qty = math.floor(final_qty / qty_step) * qty_step

        # Notional check
        notional = final_qty * signal.entry_price
        if notional < self._min_notional:
            # Try minimum quantity
            final_qty = min_qty
            notional = final_qty * signal.entry_price
            if notional < self._min_notional:
                return SizingResult(
                    rejected=True,
                    reject_reason=f"notional {notional:.2f} < min {self._min_notional}",
                )

        # Ensure above min_qty
        if final_qty < min_qty:
            final_qty = min_qty
            notional = final_qty * signal.entry_price

        risk_amount = final_qty * risk_per_unit

        logger.info(
            "[SIZING] %s | qty=%.6f notional=%.2f risk=%.2f | "
            "conf=%.2f streak=%.2f vol=%.2f final_mult=%.2f",
            signal.symbol, final_qty, notional, risk_amount,
            conf_mult, streak_mult, vol_mult, final_mult,
        )

        return SizingResult(
            quantity=final_qty,
            notional=notional,
            risk_amount=risk_amount,
            confidence_multiplier=conf_mult,
            streak_multiplier=streak_mult,
            volatility_multiplier=vol_mult,
            final_multiplier=final_mult,
        )
