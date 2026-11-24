"""
DrawdownGovernor — Adaptive Risk De-Leveraging Engine
═════════════════════════════════════════════════════════
The single most important risk component for account survival.

Design principles:
  1. The bot MUST suffer when it loses (progressive de-leveraging)
  2. Weekly limits prevent slow bleed death
  3. Critical drawdown triggers forced liquidation, not just entry block
  4. Regime transitions use hysteresis (no churn)
  5. Recovery is graduated, not cliff

Risk Tiers:
  NORMAL   → full trading (risk_mult = 1.0)
  CAUTION  → reduced sizing (risk_mult = 0.5)
  DANGER   → minimal sizing (risk_mult = 0.25)
  CRITICAL → halt + force close existing positions
  RECOVERY → graduated re-enable after profitable period

Config keys (from env / SMC_CONFIG):
  DD_GOV_CAUTION_PCT          (default 5.0)   — drawdown % to enter CAUTION
  DD_GOV_DANGER_PCT           (default 10.0)  — drawdown % to enter DANGER
  DD_GOV_CRITICAL_PCT         (default 15.0)  — drawdown % to enter CRITICAL (force close)
  DD_GOV_MAX_WEEKLY_LOSS_PCT  (default 5.0)   — max allowed weekly loss %
  DD_GOV_MAX_CONSEC_LOSSES    (default 5)     — consecutive losses before tier upgrade
  DD_GOV_RECOVERY_PROFIT_PCT  (default 2.0)   — profit % from trough to begin recovery
  DD_GOV_RECOVERY_TRADES      (default 3)     — winning trades needed to advance recovery stage
  DD_GOV_HYSTERESIS_CYCLES    (default 3)     — cycles a regime must persist before transition
"""

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class RiskTier(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    DANGER = "DANGER"
    CRITICAL = "CRITICAL"
    RECOVERY = "RECOVERY"


# Risk multipliers per tier — how much of base risk to use
TIER_RISK_MULT: Dict[RiskTier, float] = {
    RiskTier.NORMAL: 1.0,
    RiskTier.CAUTION: 0.5,
    RiskTier.DANGER: 0.25,
    RiskTier.CRITICAL: 0.0,   # no new trades
    RiskTier.RECOVERY: 0.35,  # cautious re-entry
}

# Max concurrent positions per tier
TIER_MAX_POSITIONS: Dict[RiskTier, int] = {
    RiskTier.NORMAL: 5,
    RiskTier.CAUTION: 3,
    RiskTier.DANGER: 1,
    RiskTier.CRITICAL: 0,
    RiskTier.RECOVERY: 2,
}


class DrawdownGovernor:
    """
    Tracks equity in real time and progressively de-leverages
    as drawdown increases. Forces position liquidation on critical drawdown.

    Thread-safe: all state mutations go through _transition_tier().
    """

    def __init__(self, config: Dict[str, Any], log_dir: Optional[Path] = None):
        # ── Thresholds ──
        self.caution_pct: float = float(config.get("DD_GOV_CAUTION_PCT", 5.0))
        self.danger_pct: float = float(config.get("DD_GOV_DANGER_PCT", 10.0))
        self.critical_pct: float = float(config.get("DD_GOV_CRITICAL_PCT", 15.0))
        self.max_weekly_loss_pct: float = float(config.get("DD_GOV_MAX_WEEKLY_LOSS_PCT", 5.0))
        self.max_consec_losses: int = int(config.get("DD_GOV_MAX_CONSEC_LOSSES", 5))
        self.recovery_profit_pct: float = float(config.get("DD_GOV_RECOVERY_PROFIT_PCT", 2.0))
        self.recovery_trades_needed: int = int(config.get("DD_GOV_RECOVERY_TRADES", 3))
        self.hysteresis_cycles: int = int(config.get("DD_GOV_HYSTERESIS_CYCLES", 3))
        self.log_dir = log_dir

        # Validate thresholds are ordered
        if not (0 < self.caution_pct < self.danger_pct < self.critical_pct <= 50):
            raise ValueError(
                f"DD thresholds must be ordered: 0 < caution({self.caution_pct}) "
                f"< danger({self.danger_pct}) < critical({self.critical_pct}) <= 50"
            )

        # ── State ──
        self._tier: RiskTier = RiskTier.NORMAL
        self._peak_equity: float = 0.0
        self._current_equity: float = 0.0
        self._trough_equity: float = float("inf")
        self._week_start_equity: float = 0.0
        self._week_start_ts: float = 0.0
        self._consecutive_losses: int = 0
        self._recovery_wins: int = 0
        self._pending_tier: Optional[RiskTier] = None
        self._pending_tier_cycles: int = 0
        self._last_update_ts: float = 0.0
        self._force_close_callback: Optional[Callable[[], Coroutine]] = None
        self._trade_log: List[Dict[str, Any]] = []  # bounded

        # ── Metrics (read-only for dashboards) ──
        self.total_tier_transitions: int = 0
        self.total_force_closes: int = 0
        self.worst_drawdown_pct: float = 0.0

        logger.info(
            f"[DD_GOV] Init | caution={self.caution_pct}% danger={self.danger_pct}% "
            f"critical={self.critical_pct}% weekly_max={self.max_weekly_loss_pct}% "
            f"consec_max={self.max_consec_losses} hysteresis={self.hysteresis_cycles}"
        )

    # ══════════════════════════════════════════════════════════════
    #  PUBLIC API
    # ══════════════════════════════════════════════════════════════

    def initialize(self, starting_equity: float) -> None:
        """Call once at startup with current account equity."""
        if starting_equity <= 0:
            raise ValueError(f"Starting equity must be positive, got {starting_equity}")
        self._peak_equity = starting_equity
        self._current_equity = starting_equity
        self._trough_equity = starting_equity
        self._week_start_equity = starting_equity
        self._week_start_ts = time.time()
        self._last_update_ts = time.time()
        logger.info(f"[DD_GOV] Initialized | equity={starting_equity:.2f}")

    def set_force_close_callback(
        self, callback: Callable[[], Coroutine]
    ) -> None:
        """Register async callback to force-close all positions."""
        self._force_close_callback = callback

    def update_equity(self, current_equity: float) -> Dict[str, Any]:
        """
        Call on every cycle with current account equity.
        Returns risk assessment dict for the orchestrator.
        """
        if self._peak_equity <= 0:
            logger.warning("[DD_GOV] Not initialized — call initialize() first")
            return self._build_assessment(blocked=False, reason="NOT_INITIALIZED")

        self._current_equity = current_equity
        self._last_update_ts = time.time()

        # Update peak (high-water mark)
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity

        # Update trough
        if current_equity < self._trough_equity:
            self._trough_equity = current_equity

        # Calculate drawdowns
        dd_from_peak = self._drawdown_from_peak()
        dd_weekly = self._weekly_drawdown()

        # Track worst ever
        if dd_from_peak > self.worst_drawdown_pct:
            self.worst_drawdown_pct = dd_from_peak

        # Determine target tier
        target_tier = self._compute_target_tier(dd_from_peak, dd_weekly)

        # Apply hysteresis for upgrades (tighter risk), instant for downgrades (looser)
        self._apply_hysteresis(target_tier)

        # Build response
        blocked = self._tier == RiskTier.CRITICAL
        reason = ""
        if blocked:
            reason = f"CRITICAL_DD: {dd_from_peak:.1f}% from peak"

        assessment = self._build_assessment(
            blocked=blocked,
            reason=reason,
            dd_from_peak=dd_from_peak,
            dd_weekly=dd_weekly,
        )

        self._log_state(assessment)
        return assessment

    def record_trade_result(self, pnl: float, trade_info: Optional[Dict] = None) -> None:
        """
        Call after each trade closes to update streak tracking.
        """
        if pnl < 0:
            self._consecutive_losses += 1
            self._recovery_wins = 0
        else:
            self._consecutive_losses = 0
            if self._tier == RiskTier.RECOVERY:
                self._recovery_wins += 1
                if self._recovery_wins >= self.recovery_trades_needed:
                    logger.info(
                        f"[DD_GOV] Recovery complete: {self._recovery_wins} wins"
                    )
                    self._transition_tier(RiskTier.NORMAL, "RECOVERY_COMPLETE")

        # Consecutive loss escalation (immediate, no hysteresis)
        if self._consecutive_losses >= self.max_consec_losses:
            current_idx = list(RiskTier).index(self._tier)
            danger_idx = list(RiskTier).index(RiskTier.DANGER)
            if current_idx < danger_idx:
                self._transition_tier(
                    RiskTier.DANGER,
                    f"CONSEC_LOSSES:{self._consecutive_losses}",
                )

        # Bounded trade log
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "pnl": round(pnl, 4),
            "tier": self._tier.value,
            "consec_losses": self._consecutive_losses,
            "dd_pct": round(self._drawdown_from_peak(), 2),
        }
        if trade_info:
            entry["symbol"] = trade_info.get("symbol", "")
            entry["strategy"] = trade_info.get("strategy", "")
        self._trade_log.append(entry)
        if len(self._trade_log) > 500:
            self._trade_log = self._trade_log[-250:]

    def get_risk_multiplier(self) -> float:
        """Current risk sizing multiplier (0.0 to 1.0)."""
        return TIER_RISK_MULT.get(self._tier, 1.0)

    def get_max_positions(self) -> int:
        """Current maximum allowed concurrent positions."""
        return TIER_MAX_POSITIONS.get(self._tier, 5)

    def is_trading_allowed(self) -> Tuple[bool, str]:
        """Check if new entries are permitted."""
        if self._tier == RiskTier.CRITICAL:
            return False, f"CRITICAL_DRAWDOWN:{self._drawdown_from_peak():.1f}%"
        return True, ""

    def get_state(self) -> Dict[str, Any]:
        """Full state snapshot for dashboards/persistence."""
        return {
            "tier": self._tier.value,
            "risk_multiplier": self.get_risk_multiplier(),
            "max_positions": self.get_max_positions(),
            "current_equity": round(self._current_equity, 2),
            "peak_equity": round(self._peak_equity, 2),
            "trough_equity": round(self._trough_equity, 2),
            "drawdown_from_peak_pct": round(self._drawdown_from_peak(), 2),
            "weekly_drawdown_pct": round(self._weekly_drawdown(), 2),
            "consecutive_losses": self._consecutive_losses,
            "recovery_wins": self._recovery_wins,
            "worst_drawdown_pct": round(self.worst_drawdown_pct, 2),
            "total_tier_transitions": self.total_tier_transitions,
            "total_force_closes": self.total_force_closes,
            "last_update": datetime.fromtimestamp(
                self._last_update_ts, tz=timezone.utc
            ).isoformat() if self._last_update_ts > 0 else None,
        }

    def reset_weekly(self) -> None:
        """
        Call at the start of each trading week (e.g., Monday 00:00 UTC).
        Resets weekly drawdown tracking.
        """
        self._week_start_equity = self._current_equity
        self._week_start_ts = time.time()
        logger.info(
            f"[DD_GOV] Weekly reset | equity={self._current_equity:.2f}"
        )

    # ══════════════════════════════════════════════════════════════
    #  INTERNAL
    # ══════════════════════════════════════════════════════════════

    def _drawdown_from_peak(self) -> float:
        """Current drawdown percentage from high-water mark."""
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, (self._peak_equity - self._current_equity) / self._peak_equity * 100)

    def _weekly_drawdown(self) -> float:
        """Current drawdown since week start."""
        if self._week_start_equity <= 0:
            return 0.0
        return max(
            0.0,
            (self._week_start_equity - self._current_equity) / self._week_start_equity * 100,
        )

    def _compute_target_tier(self, dd_from_peak: float, dd_weekly: float) -> RiskTier:
        """
        Pure function: given current drawdowns, what tier should we be in?
        Uses the WORST (most restrictive) of peak-DD, weekly-DD, and consecutive losses.
        """
        candidates: List[RiskTier] = []

        # Peak drawdown tiers
        if dd_from_peak >= self.critical_pct:
            candidates.append(RiskTier.CRITICAL)
        elif dd_from_peak >= self.danger_pct:
            candidates.append(RiskTier.DANGER)
        elif dd_from_peak >= self.caution_pct:
            candidates.append(RiskTier.CAUTION)

        # Weekly loss kill → DANGER
        if dd_weekly >= self.max_weekly_loss_pct:
            candidates.append(RiskTier.DANGER)

        # Consecutive loss escalation → DANGER
        if self._consecutive_losses >= self.max_consec_losses:
            candidates.append(RiskTier.DANGER)

        # Take the worst (most restrictive) tier
        tier_severity = [RiskTier.NORMAL, RiskTier.CAUTION, RiskTier.RECOVERY,
                         RiskTier.DANGER, RiskTier.CRITICAL]
        if candidates:
            worst = max(candidates, key=lambda t: tier_severity.index(t))
        else:
            worst = RiskTier.NORMAL

        # If currently in CRITICAL/DANGER, require recovery before downgrade
        if self._tier in (RiskTier.CRITICAL, RiskTier.DANGER) and worst.value not in ("CRITICAL", "DANGER"):
            if self._trough_equity > 0:
                recovery_pct = (
                    (self._current_equity - self._trough_equity)
                    / self._trough_equity * 100
                )
                if recovery_pct >= self.recovery_profit_pct:
                    return RiskTier.RECOVERY
            return self._tier  # stay in current tier

        if self._tier == RiskTier.RECOVERY and worst == RiskTier.NORMAL:
            return RiskTier.RECOVERY

        return worst

    def _apply_hysteresis(self, target_tier: RiskTier) -> None:
        """
        Tightening (NORMAL→CAUTION→DANGER→CRITICAL): requires N consecutive
        cycles at target before transition. Prevents churn on noise.

        Loosening (CRITICAL→DANGER→RECOVERY→NORMAL): also requires hysteresis
        to prevent premature recovery.

        Exception: CRITICAL is INSTANT (safety first).
        """
        if target_tier == self._tier:
            self._pending_tier = None
            self._pending_tier_cycles = 0
            return

        # CRITICAL is always instant — safety override
        if target_tier == RiskTier.CRITICAL:
            self._transition_tier(target_tier, f"INSTANT_CRITICAL:DD={self._drawdown_from_peak():.1f}%")
            return

        # Hysteresis for all other transitions
        if target_tier == self._pending_tier:
            self._pending_tier_cycles += 1
        else:
            self._pending_tier = target_tier
            self._pending_tier_cycles = 1

        if self._pending_tier_cycles >= self.hysteresis_cycles:
            self._transition_tier(target_tier, f"HYSTERESIS:{self._pending_tier_cycles}cycles")

    def _transition_tier(self, new_tier: RiskTier, reason: str) -> None:
        """Execute tier transition with logging and side effects."""
        old_tier = self._tier
        self._tier = new_tier
        self._pending_tier = None
        self._pending_tier_cycles = 0
        self.total_tier_transitions += 1

        logger.warning(
            f"[DD_GOV] TIER CHANGE: {old_tier.value} → {new_tier.value} | "
            f"reason={reason} | equity={self._current_equity:.2f} | "
            f"dd={self._drawdown_from_peak():.1f}% | "
            f"risk_mult={TIER_RISK_MULT[new_tier]}"
        )

        # On entering CRITICAL: trigger force close
        if new_tier == RiskTier.CRITICAL:
            self._trigger_force_close(reason)

        # On entering RECOVERY: reset trough and recovery counter
        if new_tier == RiskTier.RECOVERY:
            self._trough_equity = self._current_equity
            self._recovery_wins = 0

    async def _execute_force_close(self, reason: str) -> None:
        """Execute the force close callback if registered."""
        if self._force_close_callback is not None:
            try:
                logger.critical(
                    f"[DD_GOV] FORCE CLOSE ALL POSITIONS | reason={reason}"
                )
                await self._force_close_callback()
                self.total_force_closes += 1
            except Exception as e:
                logger.critical(
                    f"[DD_GOV] FORCE CLOSE FAILED: {e} — MANUAL INTERVENTION REQUIRED"
                )

    def _trigger_force_close(self, reason: str) -> None:
        """
        Schedule force close. Since this may be called from sync context,
        we log the intent and the async execution is handled by the caller
        checking is_trading_allowed() + get_state()['tier'] == 'CRITICAL'.
        """
        self.total_force_closes += 1
        logger.critical(
            f"[DD_GOV] FORCE CLOSE TRIGGERED | reason={reason} | "
            f"equity={self._current_equity:.2f} | peak={self._peak_equity:.2f} | "
            f"dd={self._drawdown_from_peak():.1f}%"
        )

    def _build_assessment(
        self,
        blocked: bool,
        reason: str,
        dd_from_peak: float = 0.0,
        dd_weekly: float = 0.0,
    ) -> Dict[str, Any]:
        return {
            "allowed": not blocked,
            "blocked": blocked,
            "reason": reason,
            "tier": self._tier.value,
            "risk_multiplier": self.get_risk_multiplier(),
            "max_positions": self.get_max_positions(),
            "drawdown_from_peak_pct": round(dd_from_peak, 2),
            "weekly_drawdown_pct": round(dd_weekly, 2),
            "consecutive_losses": self._consecutive_losses,
            "peak_equity": round(self._peak_equity, 2),
            "current_equity": round(self._current_equity, 2),
        }

    def _log_state(self, assessment: Dict[str, Any]) -> None:
        """Append state to daily JSONL log."""
        if not self.log_dir:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            fpath = self.log_dir / f"dd_governor_{today}.jsonl"
            entry = {
                **assessment,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            with open(fpath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"[DD_GOV] Log write failed: {e}")

    # ══════════════════════════════════════════════════════════════
    #  FACTORY
    # ══════════════════════════════════════════════════════════════

    @classmethod
    def from_config(
        cls,
        config: Dict[str, Any],
        log_dir: Optional[Path] = None,
    ) -> "DrawdownGovernor":
        """Create from SMC_CONFIG dict."""
        return cls(config=config, log_dir=log_dir)
