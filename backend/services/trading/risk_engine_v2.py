"""
Risk Engine V2 — Per-Symbol Risk Limits + Global Exposure Control.
═══════════════════════════════════════════════════════════════════

UPGRADE from V1 (risk_engine.py) which only has global gates.
V2 adds:
  1. PER-SYMBOL risk limits (max size, max loss, cooldown)
  2. GLOBAL EXPOSURE tracking (% of total capital in open positions)
  3. CORRELATION CHECK (block if too many correlated positions)
  4. AUTO DE-RISK (progressive reduction when approaching limits)
  5. EVENT BUS integration (all risk decisions emitted as events)

Coexists with V1: V1 handles kill switch + drawdown.
V2 is an additional gate evaluated AFTER V1 passes.

Config via .env:
  RISK_V2_MAX_POSITION_PCT=5.0          # Max single position = 5% of capital
  RISK_V2_MAX_GLOBAL_EXPOSURE_PCT=25.0  # Max total exposure = 25% of capital
  RISK_V2_MAX_CORRELATED_POSITIONS=3    # Max positions in same sector
  RISK_V2_MAX_DAILY_LOSS_PER_SYMBOL=2.0 # Max daily loss per symbol as % of capital
  RISK_V2_COOLDOWN_AFTER_LOSS_SEC=300   # Per-symbol cooldown after loss
  RISK_V2_AUTO_DERISK_THRESHOLD=0.8     # Start de-risking at 80% of limits
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from core.events.event_bus import get_event_bus
from core.events.event_types import EventType, RiskEvent, AlertEvent

logger = logging.getLogger("smarttrade.risk_v2")


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

@dataclass
class RiskV2Config:
    max_position_pct: float = field(default_factory=lambda: _envf("RISK_V2_MAX_POSITION_PCT", 5.0))
    max_global_exposure_pct: float = field(default_factory=lambda: _envf("RISK_V2_MAX_GLOBAL_EXPOSURE_PCT", 25.0))
    max_correlated_positions: int = field(default_factory=lambda: _envi("RISK_V2_MAX_CORRELATED_POSITIONS", 3))
    max_daily_loss_per_symbol_pct: float = field(default_factory=lambda: _envf("RISK_V2_MAX_DAILY_LOSS_PER_SYMBOL", 2.0))
    cooldown_after_loss_sec: float = field(default_factory=lambda: _envf("RISK_V2_COOLDOWN_AFTER_LOSS_SEC", 300.0))
    auto_derisk_threshold: float = field(default_factory=lambda: _envf("RISK_V2_AUTO_DERISK_THRESHOLD", 0.8))


# ═══════════════════════════════════════════════════════════════
#  PER-SYMBOL STATE
# ═══════════════════════════════════════════════════════════════

@dataclass
class SymbolRiskState:
    """Risk tracking for a single symbol."""
    symbol: str
    daily_pnl: float = 0.0
    daily_trades: int = 0
    daily_losses: int = 0
    last_loss_time: float = 0.0
    current_position_size: float = 0.0
    current_position_notional: float = 0.0
    consecutive_losses: int = 0
    daily_reset_date: str = ""

    def reset_daily(self, today: str) -> None:
        if self.daily_reset_date != today:
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.daily_losses = 0
            self.daily_reset_date = today


# ═══════════════════════════════════════════════════════════════
#  RISK VERDICT V2
# ═══════════════════════════════════════════════════════════════

@dataclass
class RiskV2Verdict:
    """Result of V2 evaluation."""
    allowed: bool
    action: str  # "ALLOW", "REDUCE", "BLOCK"
    size_multiplier: float = 1.0  # 0.0-1.0, applied to sizing
    reasons: List[str] = field(default_factory=list)
    checks: Dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
#  CORRELATION MAP
# ═══════════════════════════════════════════════════════════════

# Crypto correlation groups — highly correlated assets
CORRELATION_GROUPS: Dict[str, str] = {
    "BTCUSDT": "btc",
    "BTCUSD": "btc",
    "BTC/USD": "btc",
    "PF_XBTUSD": "btc",
    "ETHUSDT": "eth",
    "ETHUSD": "eth",
    "ETH/USD": "eth",
    "PF_ETHUSD": "eth",
    "SOLUSDT": "alt_l1",
    "SOLUSD": "alt_l1",
    "PF_SOLUSD": "alt_l1",
    "AVAXUSDT": "alt_l1",
    "DOGEUSDT": "meme",
    "DOGEUSD": "meme",
    "SHIBUSDT": "meme",
    "LINKUSDT": "oracle",
    "AAVEUSDT": "defi",
    "UNIUSDT": "defi",
}


class RiskEngineV2:
    """
    Per-symbol risk engine with global exposure control.

    Evaluated AFTER RiskEngine V1 passes (V1 handles kill switch + drawdown).
    V2 provides granular per-symbol control for institutional capital management.
    """

    def __init__(self, config: Optional[RiskV2Config] = None) -> None:
        self.config = config or RiskV2Config()
        self._symbol_states: Dict[str, SymbolRiskState] = defaultdict(
            lambda: SymbolRiskState(symbol="")
        )
        self._bus = get_event_bus()
        self._total_capital: float = 0.0  # Updated via update_capital()

        logger.info(
            "[RISK_V2] Initialized — max_pos=%.1f%% max_exposure=%.1f%% max_corr=%d",
            self.config.max_position_pct,
            self.config.max_global_exposure_pct,
            self.config.max_correlated_positions,
        )

    # ──────────────────────────────────────────────────
    #  CAPITAL UPDATE
    # ──────────────────────────────────────────────────

    def update_capital(self, total_capital: float) -> None:
        """Called every cycle with current account equity."""
        self._total_capital = total_capital

    # ──────────────────────────────────────────────────
    #  EVALUATION — the main gate
    # ──────────────────────────────────────────────────

    def evaluate(
        self,
        symbol: str,
        side: str,
        proposed_size_usd: float,
        current_price: float,
        open_positions: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> RiskV2Verdict:
        """
        Evaluate whether a proposed trade is allowed under V2 rules.

        Args:
            symbol: Trading pair (e.g. "BTCUSDT")
            side: "long" or "short"
            proposed_size_usd: Notional value of proposed trade in USD
            current_price: Current market price
            open_positions: Dict of symbol -> {side, notional, qty} for all open positions

        Returns:
            RiskV2Verdict with allowed/blocked/reduced + reasons
        """
        if self._total_capital <= 0:
            return RiskV2Verdict(
                allowed=True,
                action="ALLOW",
                reasons=["capital_not_set"],
                checks={"capital": 0},
            )

        today = time.strftime("%Y-%m-%d")
        state = self._get_symbol_state(symbol)
        state.reset_daily(today)

        open_positions = open_positions or {}
        verdict = RiskV2Verdict(allowed=True, action="ALLOW")
        multiplier = 1.0

        # ── CHECK 1: Per-symbol daily loss limit ──
        max_daily_loss = self._total_capital * self.config.max_daily_loss_per_symbol_pct / 100
        if state.daily_pnl < 0 and abs(state.daily_pnl) >= max_daily_loss:
            verdict.allowed = False
            verdict.action = "BLOCK"
            verdict.reasons.append(
                f"daily_loss_limit:{symbol} lost ${abs(state.daily_pnl):.2f} (max ${max_daily_loss:.2f})"
            )
            self._emit_risk_event(
                EventType.RISK_DAILY_LIMIT,
                symbol=symbol,
                reason=f"Daily loss limit reached: ${abs(state.daily_pnl):.2f} >= ${max_daily_loss:.2f}",
                current_value=abs(state.daily_pnl),
                threshold=max_daily_loss,
            )

        # ── CHECK 2: Per-symbol cooldown ──
        if state.last_loss_time > 0:
            elapsed = time.time() - state.last_loss_time
            if elapsed < self.config.cooldown_after_loss_sec:
                remaining = self.config.cooldown_after_loss_sec - elapsed
                verdict.allowed = False
                verdict.action = "BLOCK"
                verdict.reasons.append(
                    f"cooldown:{symbol} {remaining:.0f}s remaining after loss"
                )

        # ── CHECK 3: Single position size limit ──
        max_position_usd = self._total_capital * self.config.max_position_pct / 100
        if proposed_size_usd > max_position_usd:
            # Auto de-risk instead of block
            multiplier = min(multiplier, max_position_usd / proposed_size_usd)
            verdict.reasons.append(
                f"size_cap:{symbol} ${proposed_size_usd:.2f} > max ${max_position_usd:.2f}, reduced to {multiplier:.1%}"
            )

        # ── CHECK 4: Global exposure limit ──
        current_exposure = sum(
            abs(p.get("notional", 0)) for p in open_positions.values()
        )
        max_exposure = self._total_capital * self.config.max_global_exposure_pct / 100
        new_exposure = current_exposure + proposed_size_usd * multiplier

        if new_exposure > max_exposure:
            available = max(0, max_exposure - current_exposure)
            if available <= 0:
                verdict.allowed = False
                verdict.action = "BLOCK"
                verdict.reasons.append(
                    f"exposure_limit:${current_exposure:.0f} already at max ${max_exposure:.0f}"
                )
                self._emit_risk_event(
                    EventType.RISK_EXPOSURE_BREACH,
                    symbol=symbol,
                    reason=f"Global exposure limit: ${current_exposure:.0f} >= ${max_exposure:.0f}",
                    current_value=current_exposure,
                    threshold=max_exposure,
                )
            else:
                multiplier = min(multiplier, available / (proposed_size_usd * multiplier))
                verdict.reasons.append(
                    f"exposure_reduced:available=${available:.0f} of ${max_exposure:.0f}"
                )

        # ── CHECK 5: Correlation limit ──
        group = CORRELATION_GROUPS.get(symbol.upper(), symbol.upper())
        correlated_count = 0
        for pos_sym in open_positions:
            pos_group = CORRELATION_GROUPS.get(pos_sym.upper(), pos_sym.upper())
            if pos_group == group and pos_sym.upper() != symbol.upper():
                correlated_count += 1

        if correlated_count >= self.config.max_correlated_positions:
            verdict.allowed = False
            verdict.action = "BLOCK"
            verdict.reasons.append(
                f"correlation_limit:{correlated_count} correlated positions in group '{group}' (max {self.config.max_correlated_positions})"
            )

        # ── CHECK 6: Auto de-risk near limits ──
        if verdict.allowed and multiplier == 1.0:
            exposure_ratio = new_exposure / max_exposure if max_exposure > 0 else 0
            if exposure_ratio > self.config.auto_derisk_threshold:
                derisk_factor = 1.0 - (exposure_ratio - self.config.auto_derisk_threshold) / (1.0 - self.config.auto_derisk_threshold)
                multiplier = min(multiplier, max(0.25, derisk_factor))
                verdict.reasons.append(
                    f"auto_derisk:exposure at {exposure_ratio:.0%}, reduced to {multiplier:.1%}"
                )

        verdict.size_multiplier = multiplier
        if verdict.allowed and multiplier < 1.0:
            verdict.action = "REDUCE"

        verdict.checks = {
            "total_capital": round(self._total_capital, 2),
            "proposed_size_usd": round(proposed_size_usd, 2),
            "effective_size_usd": round(proposed_size_usd * multiplier, 2),
            "current_exposure": round(current_exposure, 2),
            "max_exposure": round(max_exposure, 2),
            "max_position": round(max_position_usd, 2),
            "daily_pnl": round(state.daily_pnl, 2),
            "correlation_group": group,
            "correlated_count": correlated_count,
            "multiplier": round(multiplier, 4),
        }

        # Emit verdict event
        self._bus.publish(RiskEvent(
            EventType.RISK_VERDICT,
            reason=verdict.action + ": " + "; ".join(verdict.reasons) if verdict.reasons else "ALLOW",
            symbol=symbol,
            action=verdict.action,
            current_value=proposed_size_usd * multiplier,
            threshold=max_position_usd,
            source="risk_v2",
        ))

        if not verdict.allowed:
            logger.warning(
                "[RISK_V2] BLOCKED %s %s $%.2f — %s",
                side, symbol, proposed_size_usd, "; ".join(verdict.reasons),
            )

        return verdict

    # ──────────────────────────────────────────────────
    #  TRADE RECORDING
    # ──────────────────────────────────────────────────

    def record_trade_result(
        self, symbol: str, pnl: float, is_win: bool
    ) -> None:
        """Called after a trade closes to update per-symbol stats."""
        state = self._get_symbol_state(symbol)
        today = time.strftime("%Y-%m-%d")
        state.reset_daily(today)

        state.daily_pnl += pnl
        state.daily_trades += 1
        if not is_win:
            state.daily_losses += 1
            state.last_loss_time = time.time()
            state.consecutive_losses += 1
        else:
            state.consecutive_losses = 0

    def update_position(
        self, symbol: str, size: float, notional: float
    ) -> None:
        """Update tracked position for a symbol."""
        state = self._get_symbol_state(symbol)
        state.current_position_size = size
        state.current_position_notional = notional

    # ──────────────────────────────────────────────────
    #  INTERNAL
    # ──────────────────────────────────────────────────

    def _get_symbol_state(self, symbol: str) -> SymbolRiskState:
        key = symbol.upper()
        if key not in self._symbol_states:
            self._symbol_states[key] = SymbolRiskState(symbol=key)
        return self._symbol_states[key]

    def _emit_risk_event(
        self, event_type: EventType, symbol: str, reason: str,
        current_value: float = 0, threshold: float = 0,
    ) -> None:
        self._bus.publish(RiskEvent(
            event_type,
            reason=reason,
            symbol=symbol,
            current_value=current_value,
            threshold=threshold,
            source="risk_v2",
        ))

    # ──────────────────────────────────────────────────
    #  DIAGNOSTICS
    # ──────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        return {
            "total_capital": self._total_capital,
            "config": {
                "max_position_pct": self.config.max_position_pct,
                "max_global_exposure_pct": self.config.max_global_exposure_pct,
                "max_correlated_positions": self.config.max_correlated_positions,
                "max_daily_loss_per_symbol_pct": self.config.max_daily_loss_per_symbol_pct,
                "cooldown_after_loss_sec": self.config.cooldown_after_loss_sec,
            },
            "symbols": {
                sym: {
                    "daily_pnl": round(s.daily_pnl, 2),
                    "daily_trades": s.daily_trades,
                    "daily_losses": s.daily_losses,
                    "consecutive_losses": s.consecutive_losses,
                    "position_notional": round(s.current_position_notional, 2),
                    "in_cooldown": (
                        time.time() - s.last_loss_time < self.config.cooldown_after_loss_sec
                        if s.last_loss_time > 0 else False
                    ),
                }
                for sym, s in self._symbol_states.items()
                if s.daily_trades > 0 or s.current_position_notional > 0
            },
        }


# ═══════════════════════════════════════════════════════════════
#  SINGLETON
# ═══════════════════════════════════════════════════════════════

_risk_v2_instance: Optional[RiskEngineV2] = None


def get_risk_engine_v2() -> RiskEngineV2:
    global _risk_v2_instance
    if _risk_v2_instance is None:
        _risk_v2_instance = RiskEngineV2()
    return _risk_v2_instance


def reset_risk_engine_v2() -> None:
    """Reset singleton — for testing only."""
    global _risk_v2_instance
    _risk_v2_instance = None
