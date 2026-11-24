"""
Enhanced Microstructure Model — Chantier 11
═════════════════════════════════════════════
Wraps MarketModel with entry-type-specific slippage adjustments:

  - BREAKOUT entries: higher slippage (market orders chasing momentum,
    thin liquidity at breakout levels, aggressive takers)
  - RETEST entries: lower slippage (patient limit orders at known
    support/resistance, better fills)
  - FALSE BREAKOUT: severe penalty (trapped in bad fill, wide spread,
    slippage compounds as panic exit needed)

This does NOT change MinMarketModel internals — it applies multipliers
to the fill results based on entry context.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, List

from smc_futures.backtest.market_model import (
    MarketModel,
    FillResult,
    SymbolProfile,
    get_symbol_profile,
)
from smc_futures.backtest.engine import BTCandle


# ─── Entry Context ────────────────────────

@dataclass(frozen=True)
class EntryContext:
    """Describes the market context at entry time."""
    entry_type: str  # "breakout", "retest", "false_breakout", "default"
    bars_since_breakout: int = 0
    volume_surge_ratio: float = 1.0  # Current vol / avg vol
    spread_from_level_bps: float = 0.0  # Distance from key level in bps
    is_momentum_chase: bool = False  # Entering far from breakout level


# ─── Slippage Multipliers ─────────────────

@dataclass(frozen=True)
class MicrostructureConfig:
    """Configuration for entry-type-based slippage adjustments."""
    # Breakout: chasing price, thin ask/bid at extremes
    breakout_slippage_mult: float = 1.8      # 80% more slippage
    breakout_spread_mult: float = 1.5         # spread widens at breakout
    breakout_volume_penalty: float = 0.3      # Extra slippage per vol_surge ratio unit

    # Retest: patient, known level, limit orders possible
    retest_slippage_mult: float = 0.6         # 40% less slippage
    retest_spread_mult: float = 0.8           # tighter spread at known levels
    retest_maker_possible: bool = True        # Can use maker fees

    # False breakout: worst case — trapped, panic close
    false_breakout_slippage_mult: float = 2.5  # 150% more slippage
    false_breakout_spread_mult: float = 2.0    # spread blows out
    false_breakout_exit_penalty_bps: float = 3.0  # Extra exit cost in bps

    # Momentum chase penalty (entering far from breakout level)
    chase_penalty_per_bps: float = 0.02       # +2% slippage per bps from level


# ─── Enhanced Market Model ────────────────

class EnhancedMarketModel:
    """
    Wraps MarketModel with microstructure-aware fill simulation.
    
    For Alpha B (Momentum Breakout), entries are on RETEST of breakout level,
    which should get better fills. But false breakouts get penalized.
    """

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        seed: Optional[int] = None,
        config: Optional[MicrostructureConfig] = None,
    ):
        self.base_model = MarketModel.from_symbol(symbol, seed=seed)
        self.config = config or MicrostructureConfig()
        self.symbol = symbol

    def simulate_entry_fill(
        self,
        side: str,
        price: float,
        qty: float,
        atr: float,
        context: EntryContext,
        volume_5m: float = 0.0,
        volatility_regime: str = "normal",
    ) -> FillResult:
        """Simulate an entry fill with microstructure adjustments."""
        cfg = self.config

        # Get base fill from underlying model
        is_maker = (
            context.entry_type == "retest" and cfg.retest_maker_possible
        )
        base_fill = self.base_model.simulate_fill(
            side=side,
            price=price,
            qty=qty,
            atr=atr,
            volume_5m=volume_5m,
            is_maker=is_maker,
            volatility_regime=volatility_regime,
        )

        if base_fill.rejected:
            return base_fill

        # Apply entry-type multiplier
        if context.entry_type == "breakout":
            slip_mult = cfg.breakout_slippage_mult
            spread_mult = cfg.breakout_spread_mult

            # Extra penalty for volume surge (more aggressive participants)
            if context.volume_surge_ratio > 1.5:
                slip_mult += cfg.breakout_volume_penalty * (
                    context.volume_surge_ratio - 1.0
                )

        elif context.entry_type == "retest":
            slip_mult = cfg.retest_slippage_mult
            spread_mult = cfg.retest_spread_mult

        elif context.entry_type == "false_breakout":
            slip_mult = cfg.false_breakout_slippage_mult
            spread_mult = cfg.false_breakout_spread_mult

        else:
            slip_mult = 1.0
            spread_mult = 1.0

        # Momentum chase penalty
        if context.is_momentum_chase and context.spread_from_level_bps > 10:
            chase_extra = (
                context.spread_from_level_bps * cfg.chase_penalty_per_bps
            )
            slip_mult += chase_extra / 100.0

        # Apply multipliers to fill
        adjusted_slippage_bps = base_fill.slippage_bps * slip_mult
        adjusted_spread_bps = base_fill.spread_bps * spread_mult

        # Recalculate fill price
        total_slip_pct = adjusted_slippage_bps / 10000
        if side.lower() == "buy":
            adjusted_fill_price = price * (1 + total_slip_pct)
        else:
            adjusted_fill_price = price * (1 - total_slip_pct)

        return FillResult(
            fill_price=round(adjusted_fill_price, 8),
            slippage_bps=round(adjusted_slippage_bps, 2),
            spread_bps=round(adjusted_spread_bps, 2),
            fee_usd=base_fill.fee_usd,
            fee_rate=base_fill.fee_rate,
            latency_ms=base_fill.latency_ms,
            partial_fill_ratio=base_fill.partial_fill_ratio,
            market_impact_bps=base_fill.market_impact_bps,
        )

    def simulate_sl_fill(self, *args, **kwargs) -> FillResult:
        """Delegate SL fills to base model (already pessimistic)."""
        return self.base_model.simulate_sl_fill(*args, **kwargs)

    def simulate_tp_fill(self, *args, **kwargs) -> FillResult:
        """Delegate TP fills to base model (uses maker fees)."""
        return self.base_model.simulate_tp_fill(*args, **kwargs)

    def compute_funding_cost(self, *args, **kwargs) -> float:
        return self.base_model.compute_funding_cost(*args, **kwargs)

    @staticmethod
    def classify_volatility(atr: float, price: float, atr_history=None) -> str:
        return MarketModel.classify_volatility(atr, price, atr_history)


# ─── Helper: classify entry context from Alpha B data ─

def classify_alpha_b_entry(
    setup_type: str,
    candles: List[BTCandle],
    entry_price: float,
    breakout_level: float,
) -> EntryContext:
    """
    Classify entry context for Alpha B momentum breakout.
    
    Alpha B entries are always on RETEST (by design), but we
    check how far from the breakout level to gauge chase risk.
    """
    if not candles or entry_price <= 0 or breakout_level <= 0:
        return EntryContext(entry_type="default")

    # Distance from breakout level in bps
    distance_bps = abs(entry_price - breakout_level) / breakout_level * 10000

    # Volume surge from recent candles
    if len(candles) >= 20:
        recent_vol = candles[-1].volume
        avg_vol = sum(c.volume for c in candles[-20:]) / 20
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
    else:
        vol_ratio = 1.0

    # Alpha B is RETEST by design
    is_chase = distance_bps > 50  # > 50 bps from level = chasing

    return EntryContext(
        entry_type="retest",  # Alpha B enters on retest
        bars_since_breakout=0,  # Not tracked per-trade here
        volume_surge_ratio=vol_ratio,
        spread_from_level_bps=distance_bps,
        is_momentum_chase=is_chase,
    )
