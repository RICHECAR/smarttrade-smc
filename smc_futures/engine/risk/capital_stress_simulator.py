"""Capital Stress Simulator
=========================

Simulates short-term adverse market scenarios (shock + slippage + gap) to
assess whether adding a new trade would make the portfolio too fragile.

This module is intended to run **after** the Exposure Guardian (which blocks hard
exposure breaches) and **before** execution.

Usage:
    sim = CapitalStressSimulator()
    result = sim.check_signal(
        symbol="BTCUSDT",
        side="long",
        notional_usd=1000.0,
        account_balance=10000.0,
        open_positions=open_positions,
        pending_positions=pending_positions,
        leverage=10,
        group="majors",
    )

Result:
    {
        "allowed": bool,
        "reason": str,
        "projected_loss_usd": float,
        "projected_dd_pct": float,
        "projected_margin_pct": float,
        "worst_scenario": str,
        "details": {...}
    }
"""

import os
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from smc_futures.engine.risk.global_exposure_guardian import ExposurePosition, ExposureLimits

logger = logging.getLogger(__name__)


@dataclass
class StressScenario:
    name: str
    # Per-symbol move (negative = adverse for longs)
    symbol_moves: Dict[str, float]
    # Per-group move (applied when symbol not explicit)
    group_moves: Dict[str, float]
    # Multiplier applied to slippage stress
    slippage_mult: float = 1.0
    # Gap move (extra adverse move applied to the new trade)
    gap_pct: float = 0.0


class CapitalStressSimulator:
    """Lightweight stress simulator for short-term market shocks."""

    def __init__(
        self,
        limits: Optional[ExposureLimits] = None,
        scenarios: Optional[List[StressScenario]] = None,
    ):
        self.limits = limits or ExposureLimits()
        self.groups_map = self.limits.parse_groups()

        # Default stress scenarios
        self.scenarios = scenarios or [
            StressScenario(
                name="majors_-2_alts_-4",
                symbol_moves={},
                group_moves={"majors": -0.02, "alts": -0.04},
                slippage_mult=1.0,
                gap_pct=0.0,
            ),
            StressScenario(
                name="majors_-2_alts_-4_slippage",
                symbol_moves={},
                group_moves={"majors": -0.02, "alts": -0.04},
                slippage_mult=3.0,
                gap_pct=0.0,
            ),
            StressScenario(
                name="new_trade_gap_-3",
                symbol_moves={},
                group_moves={},
                slippage_mult=1.0,
                gap_pct=-0.03,
            ),
        ]

        # Stress thresholds
        self.max_stress_loss_usd = float(os.getenv("MAX_STRESS_LOSS_USD", "1000"))
        self.max_stress_dd_pct = float(os.getenv("MAX_STRESS_DD_PCT", "5.0"))
        self.max_stress_margin_pct = float(os.getenv("MAX_STRESS_MARGIN_PCT", "60"))
        self.base_slippage_pct = float(os.getenv("STRESS_SLIPPAGE_BASE_PCT", "0.002"))

        # Soft throttle levels (fraction of limit), e.g. 0.7,0.85
        self.throttle_levels = [
            float(x)
            for x in os.getenv("STRESS_THROTTLE_LEVELS", "0.7,0.85").split(",")
            if x.strip()
        ]

        # Liquidity-based slippage scaling (symbol:factor or group:factor)
        self.liquidity_slippage_map = self._parse_float_map(os.getenv("STRESS_SLIPPAGE_FACTORS", ""))

        # Per-symbol/group correlation weights (1.0 = fully correlated, <1 = less correlated)
        self.correlation_weights = self._parse_float_map(os.getenv("STRESS_CORRELATION_WEIGHTS", ""))

    def _parse_float_map(self, raw: str) -> Dict[str, float]:
        """Parse a comma / newline separated map like 'BTCUSDT:1.0,ETHUSDT:0.9'."""
        mapping: Dict[str, float] = {}
        if not raw:
            return mapping
        for part in raw.replace("\n", ",").split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            key, val = [p.strip() for p in part.split(":", 1)]
            try:
                mapping[key.upper()] = float(val)
            except ValueError:
                continue
        return mapping

    def _get_group(self, symbol: str) -> Optional[str]:
        return self.groups_map.get(symbol.upper())

    def _get_liquidity_slippage_factor(self, symbol: str, group: Optional[str]) -> float:
        """Return a liquidity-based slippage multiplier for a symbol/group."""
        sym = symbol.upper()
        if sym in self.liquidity_slippage_map:
            return self.liquidity_slippage_map[sym]
        grp = (group or self._get_group(sym) or "").upper()
        if grp and grp in self.liquidity_slippage_map:
            return self.liquidity_slippage_map[grp]
        return 1.0

    def _get_correlation_weight(self, symbol: str, group: Optional[str]) -> float:
        """Return a correlation weight for a symbol/group."""
        sym = symbol.upper()
        if sym in self.correlation_weights:
            return self.correlation_weights[sym]
        grp = (group or self._get_group(sym) or "").upper()
        if grp and grp in self.correlation_weights:
            return self.correlation_weights[grp]
        return 1.0

    def _compute_stress_loss(
        self,
        positions: List[ExposurePosition],
        scenario: StressScenario,
        new_trade: Optional[ExposurePosition],
    ) -> float:
        """Compute total stress loss (USD) for a given scenario.

        Uses correlation weights to adjust exposure (e.g., alts being less
        correlated with majors) and applies liquidity-based slippage scaling
        to the new trade.
        """
        loss = 0.0

        def move_for_pos(pos: ExposurePosition) -> float:
            sym = pos.symbol.upper()
            if sym in scenario.symbol_moves:
                return scenario.symbol_moves[sym]
            grp = pos.group or self._get_group(sym)
            if grp and grp in scenario.group_moves:
                return scenario.group_moves[grp]
            return 0.0

        def weighted_notional(pos: ExposurePosition) -> float:
            base = abs(pos.notional_usd)
            weight = self._get_correlation_weight(pos.symbol, pos.group)
            return base * weight

        for pos in positions:
            move = move_for_pos(pos)
            if not move:
                continue
            notional = weighted_notional(pos)
            if pos.side.lower().startswith("l"):
                loss += notional * max(-move, 0.0)
            else:
                loss += notional * max(move, 0.0)

        if new_trade:
            move = move_for_pos(new_trade)
            move += scenario.gap_pct
            notional = weighted_notional(new_trade)
            if new_trade.side.lower().startswith("l"):
                stress_loss = notional * max(-move, 0.0)
            else:
                stress_loss = notional * max(move, 0.0)

            # Add slippage stress (liquidity-based multiplier)
            liquidity = self._get_liquidity_slippage_factor(new_trade.symbol, new_trade.group)
            slippage_loss = abs(new_trade.notional_usd) * self.base_slippage_pct * liquidity * max(0.0, scenario.slippage_mult - 1.0)
            loss += stress_loss + slippage_loss

        return loss

    def check_signal(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        account_balance: float,
        open_positions: List[ExposurePosition],
        pending_positions: Optional[List[ExposurePosition]] = None,
        leverage: Optional[float] = None,
        group: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return whether a proposed trade is safe under stress scenarios."""
        symbol = symbol.upper()
        side = side.lower()

        current_equity = account_balance
        if current_equity <= 0:
            logger.warning("[STRESS_SIM] account balance non-positive, allowing by default")
            return {"allowed": True, "reason": "OK", "details": {}}

        # Build combined book (open + pending)
        combined_positions = list(open_positions)
        if pending_positions:
            combined_positions.extend(pending_positions)

        # New trade projection
        new_trade = ExposurePosition(symbol=symbol, side=side, notional_usd=notional_usd, group=group)

        worst_loss = 0.0
        worst_scenario = None
        for scenario in self.scenarios:
            loss = self._compute_stress_loss(combined_positions, scenario, new_trade)
            if loss > worst_loss:
                worst_loss = loss
                worst_scenario = scenario.name

        projected_dd_pct = (worst_loss / current_equity) * 100.0
        projected_margin_pct = (abs(notional_usd) / current_equity) * 100.0

        # Throttle helpers
        def _throttle_reason(value: float, max_value: float, label: str) -> Optional[str]:
            if max_value <= 0:
                return None
            ratio = value / max_value
            for lvl in sorted(self.throttle_levels, reverse=True):
                if ratio >= lvl:
                    return f"THROTTLE_{label}_{int(lvl * 100)}"
            return None

        throttle_reasons = []
        if self.max_stress_loss_usd > 0:
            thr = _throttle_reason(worst_loss, self.max_stress_loss_usd, "LOSS")
            if thr:
                throttle_reasons.append((thr, worst_loss / self.max_stress_loss_usd))
        if self.max_stress_dd_pct > 0:
            thr = _throttle_reason(projected_dd_pct, self.max_stress_dd_pct, "DD")
            if thr:
                throttle_reasons.append((thr, projected_dd_pct / self.max_stress_dd_pct))
        if self.max_stress_margin_pct > 0:
            thr = _throttle_reason(projected_margin_pct, self.max_stress_margin_pct, "MARGIN")
            if thr:
                throttle_reasons.append((thr, projected_margin_pct / self.max_stress_margin_pct))

        # Default allow/block logic
        reason = "OK"
        allowed = True

        if self.max_stress_loss_usd > 0 and worst_loss >= self.max_stress_loss_usd:
            allowed = False
            reason = "STRESS_LOSS_EXCEEDED"
        elif self.max_stress_dd_pct > 0 and projected_dd_pct >= self.max_stress_dd_pct:
            allowed = False
            reason = "STRESS_DD_EXCEEDED"
        elif self.max_stress_margin_pct > 0 and projected_margin_pct >= self.max_stress_margin_pct:
            allowed = False
            reason = "STRESS_MARGIN_EXCEEDED"
        elif throttle_reasons:
            # Pick the most severe throttle reason (highest ratio)
            throttle_reasons.sort(key=lambda x: x[1], reverse=True)
            reason = throttle_reasons[0][0]
            allowed = True

        # Exposure metrics
        corr_exposure = sum(
            abs(p.notional_usd) * self._get_correlation_weight(p.symbol, p.group)
            for p in combined_positions
        )

        return {
            "allowed": allowed,
            "reason": reason,
            "projected_loss_usd": round(worst_loss, 2),
            "projected_dd_pct": round(projected_dd_pct, 2),
            "projected_margin_pct": round(projected_margin_pct, 2),
            "worst_scenario": worst_scenario,
            "details": {
                "current_open_exposure": sum(abs(p.notional_usd) for p in combined_positions),
                "correlation_weighted_exposure": round(corr_exposure, 2),
                "new_trade_exposure": notional_usd,
                "post_trade_exposure": sum(abs(p.notional_usd) for p in combined_positions) + abs(notional_usd),
                "throttle_levels": self.throttle_levels,
            },
        }
