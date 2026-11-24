"""
Live Config — Capital limits, position sizing, exposure caps.
═════════════════════════════════════════════════════════════
Immutable safety parameters for controlled live deployment.
All limits are HARD: exceed → block order.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LiveSafeConfig:
    """Safety limits for live trading — frozen after init."""

    # ── Capital limits ────────────────────────────────────────────
    capital_limit_pct: float = 0.02        # Max 2% of account per trade
    max_position_size_usd: float = 5_000.0 # Hard cap per position ($)
    global_exposure_cap_usd: float = 50_000.0  # Total invested cap ($)
    global_exposure_cap_pct: float = 0.50  # Max 50% of total account invested

    # ── Order controls ────────────────────────────────────────────
    max_slippage_bps: float = 50.0         # 0.50% max slippage tolerance
    max_order_retry: int = 2               # Retry once on transient failure
    order_timeout_sec: float = 30.0        # Cancel if not filled in 30s

    # ── Risk guards ───────────────────────────────────────────────
    max_dd_pct: float = 0.10               # 10% drawdown → halt
    max_tracking_error_pct: float = 0.05   # 5% paper/live divergence → alert
    max_weight_divergence_pct: float = 0.10  # 10% weight diff → alert
    max_single_loss_pct: float = 0.03      # 3% loss on single position → alert

    # ── Frequency ─────────────────────────────────────────────────
    rebal_freq: str = "M"                  # Monthly only (inherited from paper)
    cost_bps: float = 5.0                  # Expected cost for paper shadow

    # ── Mode ──────────────────────────────────────────────────────
    dry_run: bool = True                   # Default: simulate, don't send to broker


# ── Reason codes for live blocks ──────────────────────────────────

LIVE_POSITION_SIZE_EXCEEDED = "LIVE_POSITION_SIZE_EXCEEDED"
LIVE_EXPOSURE_CAP_EXCEEDED = "LIVE_EXPOSURE_CAP_EXCEEDED"
LIVE_CAPITAL_LIMIT_EXCEEDED = "LIVE_CAPITAL_LIMIT_EXCEEDED"
LIVE_SLIPPAGE_EXCEEDED = "LIVE_SLIPPAGE_EXCEEDED"
LIVE_DD_HALT = "LIVE_DD_HALT"
LIVE_TRACKING_ERROR = "LIVE_TRACKING_ERROR"
LIVE_DATA_INCOHERENT = "LIVE_DATA_INCOHERENT"
LIVE_BROKER_ERROR = "LIVE_BROKER_ERROR"
LIVE_ORDER_TIMEOUT = "LIVE_ORDER_TIMEOUT"
LIVE_WEIGHT_DIVERGENCE = "LIVE_WEIGHT_DIVERGENCE"

ALL_LIVE_REASON_CODES = (
    LIVE_POSITION_SIZE_EXCEEDED,
    LIVE_EXPOSURE_CAP_EXCEEDED,
    LIVE_CAPITAL_LIMIT_EXCEEDED,
    LIVE_SLIPPAGE_EXCEEDED,
    LIVE_DD_HALT,
    LIVE_TRACKING_ERROR,
    LIVE_DATA_INCOHERENT,
    LIVE_BROKER_ERROR,
    LIVE_ORDER_TIMEOUT,
    LIVE_WEIGHT_DIVERGENCE,
)
