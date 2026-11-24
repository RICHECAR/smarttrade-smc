"""
Realistic Market Model — Slippage, Spread, Fees, Latency, Liquidity
═══════════════════════════════════════════════════════════════════════
Models real execution conditions to kill false edges.

Usage:
    model = MarketModel.from_symbol("BTCUSDT")
    fill = model.simulate_fill(side="buy", price=60000, qty=0.1, atr=150, volume=1e6)
    # fill.fill_price  – actual execution price after slippage+spread
    # fill.fee         – taker/maker fee in USD
    # fill.latency_ms  – simulated order-to-fill delay
    # fill.rejected    – True if liquidity insufficient
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional, Dict


# ───────────────────────────────────────────
#  Symbol-Specific Market Profiles
# ───────────────────────────────────────────

@dataclass(frozen=True)
class SymbolProfile:
    """Market microstructure parameters per symbol."""
    symbol: str
    # Spread
    base_spread_bps: float        # Typical half-spread in basis points
    spread_vol_mult: float        # Spread widens by this × normalized ATR
    # Slippage
    base_slippage_bps: float      # Minimum slippage (market order realism)
    slippage_vol_mult: float      # Volatility component multiplier
    slippage_size_mult: float     # Size impact: slippage += qty/avg_volume × mult
    # Liquidity
    avg_5m_volume_usd: float      # Typical 5-min volume in USD
    min_fill_ratio: float         # Reject if order > this fraction of 5m volume
    # Fees (Bitget USDT-M perpetual defaults)
    maker_fee_rate: float
    taker_fee_rate: float
    # Funding
    avg_funding_rate_8h: float    # Average 8h funding rate (absolute)
    # Latency
    base_latency_ms: float        # Signal-to-fill base delay
    latency_jitter_ms: float      # Random jitter ±


# Pre-configured profiles based on real Bitget/Kraken data
_PROFILES: Dict[str, SymbolProfile] = {
    "BTCUSDT": SymbolProfile(
        symbol="BTCUSDT",
        base_spread_bps=1.0,
        spread_vol_mult=2.5,
        base_slippage_bps=1.5,
        slippage_vol_mult=3.0,
        slippage_size_mult=50.0,
        avg_5m_volume_usd=25_000_000,
        min_fill_ratio=0.05,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0006,
        avg_funding_rate_8h=0.0001,
        base_latency_ms=50,
        latency_jitter_ms=30,
    ),
    "ETHUSDT": SymbolProfile(
        symbol="ETHUSDT",
        base_spread_bps=1.5,
        spread_vol_mult=3.0,
        base_slippage_bps=2.0,
        slippage_vol_mult=3.5,
        slippage_size_mult=60.0,
        avg_5m_volume_usd=12_000_000,
        min_fill_ratio=0.05,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0006,
        avg_funding_rate_8h=0.00012,
        base_latency_ms=55,
        latency_jitter_ms=35,
    ),
    "SOLUSDT": SymbolProfile(
        symbol="SOLUSDT",
        base_spread_bps=3.0,
        spread_vol_mult=4.0,
        base_slippage_bps=3.5,
        slippage_vol_mult=5.0,
        slippage_size_mult=80.0,
        avg_5m_volume_usd=5_000_000,
        min_fill_ratio=0.03,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0006,
        avg_funding_rate_8h=0.00015,
        base_latency_ms=60,
        latency_jitter_ms=40,
    ),
}

# Fallback for unknown symbols — pessimistic
_DEFAULT_PROFILE = SymbolProfile(
    symbol="DEFAULT",
    base_spread_bps=5.0,
    spread_vol_mult=5.0,
    base_slippage_bps=5.0,
    slippage_vol_mult=6.0,
    slippage_size_mult=100.0,
    avg_5m_volume_usd=2_000_000,
    min_fill_ratio=0.02,
    maker_fee_rate=0.0002,
    taker_fee_rate=0.0006,
    avg_funding_rate_8h=0.0002,
    base_latency_ms=80,
    latency_jitter_ms=50,
)


def get_symbol_profile(symbol: str) -> SymbolProfile:
    """Get market profile for a symbol (case-insensitive)."""
    return _PROFILES.get(symbol.upper(), _DEFAULT_PROFILE)


# ───────────────────────────────────────────
#  Fill Result
# ───────────────────────────────────────────

@dataclass
class FillResult:
    """Result of a simulated order fill."""
    fill_price: float          # Actual execution price
    slippage_bps: float        # Total slippage in basis points
    spread_bps: float          # Spread component in bps
    fee_usd: float             # Fee in USD (notional × rate)
    fee_rate: float            # Applied fee rate
    latency_ms: float          # Order-to-fill delay
    rejected: bool = False     # True if order couldn't be filled
    reject_reason: str = ""
    partial_fill_ratio: float = 1.0  # 1.0 = full fill
    market_impact_bps: float = 0.0   # Size-dependent impact


# ───────────────────────────────────────────
#  Market Model
# ───────────────────────────────────────────

class MarketModel:
    """
    Realistic execution model that computes fill prices, fees, and latency.

    Key components:
    1. Dynamic spread (widens with volatility)
    2. Multi-factor slippage (spread + volatility + market impact)
    3. Liquidity gating (rejects oversized orders)
    4. Realistic fee structure (maker/taker)
    5. Latency simulation (signal-to-fill delay)
    6. Funding rate deduction (for position holding)
    """

    def __init__(self, profile: SymbolProfile, seed: Optional[int] = None):
        self.profile = profile
        self._rng = random.Random(seed)

    @classmethod
    def from_symbol(cls, symbol: str, seed: Optional[int] = None) -> "MarketModel":
        return cls(get_symbol_profile(symbol), seed=seed)

    # ── Core: simulate a market order fill ────────────────

    def simulate_fill(
        self,
        side: str,                      # "buy" or "sell"
        price: float,                   # Reference price (mid)
        qty: float,                     # Position size in base currency
        atr: float,                     # Current ATR (same unit as price)
        volume_5m: float = 0.0,         # Last 5m candle volume in USD (0 → use avg)
        is_maker: bool = False,         # Limit order (lower fee)
        volatility_regime: str = "normal",  # "low", "normal", "high", "extreme"
    ) -> FillResult:
        """
        Simulate full execution of a market order.

        Returns FillResult with adjusted fill_price, fees, latency.
        """
        p = self.profile
        notional_usd = price * qty

        # 1. Liquidity check
        effective_volume = volume_5m if volume_5m > 0 else p.avg_5m_volume_usd
        if notional_usd > effective_volume * p.min_fill_ratio:
            return FillResult(
                fill_price=price,
                slippage_bps=0,
                spread_bps=0,
                fee_usd=0,
                fee_rate=0,
                latency_ms=0,
                rejected=True,
                reject_reason=(
                    f"Order size ${notional_usd:.0f} exceeds "
                    f"{p.min_fill_ratio:.0%} of 5m volume ${effective_volume:.0f}"
                ),
            )

        # 2. Volatility multiplier based on regime
        vol_mult = {"low": 0.5, "normal": 1.0, "high": 2.0, "extreme": 4.0}.get(
            volatility_regime, 1.0
        )

        # 3. Dynamic spread
        atr_norm = atr / price if price > 0 else 0
        spread_bps = p.base_spread_bps + p.spread_vol_mult * atr_norm * 10000 * vol_mult
        # Add small randomness (±20%)
        spread_bps *= 1.0 + self._rng.uniform(-0.2, 0.2)
        spread_bps = max(spread_bps, p.base_spread_bps * 0.5)  # floor

        # 4. Volatility-based slippage
        vol_slippage_bps = p.slippage_vol_mult * atr_norm * 10000 * vol_mult

        # 5. Market impact (size-dependent)
        volume_ratio = notional_usd / effective_volume if effective_volume > 0 else 0
        impact_bps = p.slippage_size_mult * volume_ratio * 10000
        # Square-root impact model (more realistic)
        impact_bps = p.slippage_size_mult * math.sqrt(volume_ratio) * 100

        # 6. Total slippage = max(spread, vol_component, impact) + base
        slippage_bps = p.base_slippage_bps + max(
            spread_bps * 0.5,       # Half-spread crossing
            vol_slippage_bps,
            impact_bps,
        )
        # Add noise (±15%)
        noise = 1.0 + self._rng.uniform(-0.15, 0.15)
        slippage_bps = max(slippage_bps * noise, p.base_slippage_bps)

        # 7. Compute fill price
        slip_pct = slippage_bps / 10000
        if side.lower() == "buy":
            fill_price = price * (1 + slip_pct)
        else:
            fill_price = price * (1 - slip_pct)

        # 8. Fees
        fee_rate = p.maker_fee_rate if is_maker else p.taker_fee_rate
        fee_usd = notional_usd * fee_rate

        # 9. Latency
        latency_ms = p.base_latency_ms + self._rng.uniform(
            -p.latency_jitter_ms, p.latency_jitter_ms
        )
        latency_ms = max(latency_ms, 10)  # Minimum 10ms

        # 10. Partial fill simulation (rare for market orders, ~2% chance on large orders)
        partial_fill = 1.0
        if volume_ratio > 0.02:
            if self._rng.random() < min(volume_ratio * 2, 0.15):
                partial_fill = self._rng.uniform(0.7, 0.95)

        return FillResult(
            fill_price=round(fill_price, 8),
            slippage_bps=round(slippage_bps, 2),
            spread_bps=round(spread_bps, 2),
            fee_usd=round(fee_usd, 4),
            fee_rate=fee_rate,
            latency_ms=round(latency_ms, 1),
            partial_fill_ratio=round(partial_fill, 4),
            market_impact_bps=round(impact_bps, 2),
        )

    # ── SL/TP fill simulation ────────────────────────────

    def simulate_sl_fill(
        self,
        side: str,
        sl_price: float,
        qty: float,
        atr: float,
        volume_5m: float = 0.0,
        volatility_regime: str = "normal",
    ) -> FillResult:
        """
        Stop-loss fills are WORSE than market orders:
        - Triggered at the SL level but  filled after spread + slippage
        - In high vol, SL can gap through
        """
        fill = self.simulate_fill(
            side="sell" if side.lower() == "buy" else "buy",
            price=sl_price,
            qty=qty,
            atr=atr,
            volume_5m=volume_5m,
            volatility_regime=volatility_regime,
        )
        # SL slippage is typically 1.5-2x worse than entry
        extra_slip = fill.slippage_bps * self._rng.uniform(0.3, 0.8)
        total_slip = fill.slippage_bps + extra_slip
        slip_pct = total_slip / 10000

        # Recalculate fill price — SL executes worse
        if side.lower() == "buy":  # Closing a long → selling at SL
            fill.fill_price = sl_price * (1 - slip_pct)
        else:  # Closing a short → buying at SL
            fill.fill_price = sl_price * (1 + slip_pct)

        fill.slippage_bps = round(total_slip, 2)
        fill.fill_price = round(fill.fill_price, 8)
        return fill

    def simulate_tp_fill(
        self,
        side: str,
        tp_price: float,
        qty: float,
        atr: float,
        volume_5m: float = 0.0,
        volatility_regime: str = "normal",
    ) -> FillResult:
        """Take-profit fills: can use limit orders → lower slippage, maker fees."""
        return self.simulate_fill(
            side="sell" if side.lower() == "buy" else "buy",
            price=tp_price,
            qty=qty,
            atr=atr,
            volume_5m=volume_5m,
            is_maker=True,  # TP as limit → maker fee
            volatility_regime=volatility_regime,
        )

    # ── Funding rate deduction ────────────────────────────

    def compute_funding_cost(
        self,
        notional_usd: float,
        holding_hours: float,
        funding_rate_override: Optional[float] = None,
    ) -> float:
        """
        Compute funding fee cost for holding a perpetual futures position.
        Funding is charged every 8 hours.
        Returns cost in USD (positive = paid by holder).
        """
        rate = funding_rate_override or self.profile.avg_funding_rate_8h
        periods = holding_hours / 8.0
        return abs(notional_usd * rate * periods)

    # ── Volatility regime classifier ──────────────────────

    @staticmethod
    def classify_volatility(atr: float, price: float, atr_history: list[float] | None = None) -> str:
        """
        Classify current volatility regime.
        Uses ATR/price ratio and optional historical ATR for context.
        """
        if price <= 0:
            return "normal"
        atr_pct = atr / price * 100

        if atr_history and len(atr_history) >= 10:
            avg_atr = sum(atr_history[-20:]) / len(atr_history[-20:])
            ratio = atr / avg_atr if avg_atr > 0 else 1.0
            if ratio >= 3.0:
                return "extreme"
            if ratio >= 2.0:
                return "high"
            if ratio <= 0.5:
                return "low"
            return "normal"

        # Fallback: absolute thresholds
        if atr_pct >= 3.0:
            return "extreme"
        if atr_pct >= 1.5:
            return "high"
        if atr_pct <= 0.3:
            return "low"
        return "normal"
