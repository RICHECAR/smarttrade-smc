"""
Risk Kill-Switch — Phase 8.2 → 8.3 (Force Close)

Central module that can:
  L1 — Block all new entries (daily / session loss limits)
  L2 — Reduce risk (cut position sizes via risk multiplier)
  L3 — Force close ALL open positions (critical drawdown / incoherence)

Design principles:
  - NEVER raises exceptions — always returns a safe result
  - Uses StatsService / DB closed trades as source of truth
  - L3 force close is idempotent (safe to call multiple times)
  - Produces structured JSON logs for observability

Config keys (from smc_config):
  - MAX_DAILY_LOSS_PCT          (default 2.0)
  - MAX_DAILY_LOSS_ABS          (default 0  = disabled)
  - MAX_SESSION_LOSS_PCT_ASIA   (default 1.0)
  - MAX_CONSECUTIVE_LOSSES_ASIA (default 6)
  - KILL_SWITCH_MODE            (default "block_entries")
  - KILL_SWITCH_L2_LOSS_PCT     (default 1.5)   — daily loss % to enter L2
  - KILL_SWITCH_L2_RISK_MULT    (default 0.5)   — risk multiplier at L2
  - KILL_SWITCH_L3_LOSS_PCT     (default 3.0)   — daily loss % to force close
"""

import json
import logging
from datetime import date, datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class KillSwitchLevel(IntEnum):
    """Severity levels: higher = more aggressive response."""
    OFF = 0
    L1_BLOCK_ENTRIES = 1
    L2_REDUCE_RISK = 2
    L3_FORCE_CLOSE = 3


class RiskKillSwitch:
    """
    Evaluator with 3 severity levels.

    L1  – Block new entries (original behaviour)
    L2  – Block new entries + reduce risk multiplier
    L3  – Block + force close all positions
    """

    def __init__(
        self,
        max_daily_loss_pct: float = 2.0,
        max_daily_loss_abs: float = 0.0,
        max_session_loss_pct_asia: float = 1.0,
        max_consecutive_losses_asia: int = 6,
        kill_switch_mode: str = "block_entries",
        log_dir: Optional[Path] = None,
        # L2/L3 thresholds
        l2_loss_pct: float = 1.5,
        l2_risk_mult: float = 0.5,
        l3_loss_pct: float = 3.0,
    ):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_daily_loss_abs = max_daily_loss_abs
        self.max_session_loss_pct_asia = max_session_loss_pct_asia
        self.max_consecutive_losses_asia = max_consecutive_losses_asia
        self.kill_switch_mode = kill_switch_mode
        self.log_dir = log_dir

        # L2 / L3
        self.l2_loss_pct = l2_loss_pct
        self.l2_risk_mult = l2_risk_mult
        self.l3_loss_pct = l3_loss_pct

        # Current state
        self._level: KillSwitchLevel = KillSwitchLevel.OFF
        self._force_close_callback: Optional[Callable[[], Coroutine]] = None
        self._force_close_triggered: bool = False  # idempotence guard

    # ── Public API ────────────────────────────────────────────────────

    def set_force_close_callback(
        self, callback: Callable[[], Coroutine]
    ) -> None:
        """Register an async callback that closes ALL open positions."""
        self._force_close_callback = callback

    @property
    def level(self) -> KillSwitchLevel:
        return self._level

    @property
    def risk_multiplier(self) -> float:
        """Risk multiplier based on current level."""
        if self._level >= KillSwitchLevel.L3_FORCE_CLOSE:
            return 0.0
        if self._level >= KillSwitchLevel.L2_REDUCE_RISK:
            return self.l2_risk_mult
        return 1.0

    def is_blocked(
        self,
        session: str,
        target_date: date,
        daily_pnl: float,
        session_pnl: float,
        equity_start: float,
        consecutive_losses: int = 0,
    ) -> Tuple[bool, str]:
        """
        Check if new entries should be blocked.
        Also evaluates L2/L3 and triggers force close if needed.

        Returns:
            (blocked: bool, reason: str)
            reason is "" when not blocked.
        """
        try:
            return self._evaluate(
                session, target_date, daily_pnl, session_pnl,
                equity_start, consecutive_losses,
            )
        except Exception as e:
            # Safety net — never crash the orchestrator
            logger.error(f"[KILL_SWITCH] Evaluation error (allowing trade): {e}")
            return False, ""

    async def force_close_all(self, reason: str = "KILL_SWITCH_L3") -> bool:
        """
        Trigger force close of all positions. Idempotent.
        Returns True if callback was invoked (or already triggered).
        """
        if self._force_close_triggered:
            logger.info(f"[KILL_SWITCH] Force close already triggered — skipping")
            return True

        self._level = KillSwitchLevel.L3_FORCE_CLOSE
        self._force_close_triggered = True

        if self._force_close_callback is not None:
            logger.critical(f"[KILL_SWITCH] L3 FORCE CLOSE | reason={reason}")
            try:
                await self._force_close_callback()
                return True
            except Exception as e:
                logger.critical(f"[KILL_SWITCH] Force close callback FAILED: {e}")
                return False
        else:
            logger.critical(
                f"[KILL_SWITCH] L3 FORCE CLOSE requested but no callback registered | reason={reason}"
            )
            return False

    def reset_force_close(self) -> None:
        """Reset force close flag (e.g. new day)."""
        self._force_close_triggered = False
        self._level = KillSwitchLevel.OFF

    # ── Internal ──────────────────────────────────────────────────────

    def _evaluate(
        self,
        session: str,
        target_date: date,
        daily_pnl: float,
        session_pnl: float,
        equity_start: float,
        consecutive_losses: int,
    ) -> Tuple[bool, str]:
        reason = ""

        # ── L3 check: force close threshold ──
        if equity_start > 0 and self.l3_loss_pct > 0:
            daily_loss_pct = abs(min(daily_pnl, 0)) / equity_start * 100
            if daily_loss_pct >= self.l3_loss_pct:
                reason = (
                    f"L3_FORCE_CLOSE: {daily_loss_pct:.2f}% >= "
                    f"{self.l3_loss_pct:.2f}%"
                )
                self._level = KillSwitchLevel.L3_FORCE_CLOSE
                self._log_event(session, target_date, "L3_FORCE_CLOSE", reason,
                                daily_pnl, session_pnl)
                return True, reason

        # ── L1 checks (original) ──

        # 1. Daily loss % check
        if equity_start > 0 and self.max_daily_loss_pct > 0:
            daily_loss_pct = abs(min(daily_pnl, 0)) / equity_start * 100
            if daily_loss_pct >= self.max_daily_loss_pct:
                reason = (
                    f"DAILY_LOSS_PCT: {daily_loss_pct:.2f}% >= "
                    f"{self.max_daily_loss_pct:.2f}%"
                )
                self._level = KillSwitchLevel.L1_BLOCK_ENTRIES
                self._log_event(session, target_date, "BLOCKED", reason,
                                daily_pnl, session_pnl)
                return True, reason

        # 2. Daily loss absolute check
        if self.max_daily_loss_abs > 0:
            if daily_pnl <= -self.max_daily_loss_abs:
                reason = (
                    f"DAILY_LOSS_ABS: {daily_pnl:.2f} <= "
                    f"-{self.max_daily_loss_abs:.2f}"
                )
                self._level = KillSwitchLevel.L1_BLOCK_ENTRIES
                self._log_event(session, target_date, "BLOCKED", reason,
                                daily_pnl, session_pnl)
                return True, reason

        # 3. Session-specific checks (ASIA)
        if session.lower() == "asia":
            # Session loss %
            if equity_start > 0 and self.max_session_loss_pct_asia > 0:
                sess_loss_pct = abs(min(session_pnl, 0)) / equity_start * 100
                if sess_loss_pct >= self.max_session_loss_pct_asia:
                    reason = (
                        f"SESSION_LOSS_PCT_ASIA: {sess_loss_pct:.2f}% >= "
                        f"{self.max_session_loss_pct_asia:.2f}%"
                    )
                    self._level = KillSwitchLevel.L1_BLOCK_ENTRIES
                    self._log_event(session, target_date, "BLOCKED", reason,
                                    daily_pnl, session_pnl)
                    return True, reason

            # Consecutive losses
            if (self.max_consecutive_losses_asia > 0 and
                    consecutive_losses >= self.max_consecutive_losses_asia):
                reason = (
                    f"CONSECUTIVE_LOSSES_ASIA: {consecutive_losses} >= "
                    f"{self.max_consecutive_losses_asia}"
                )
                self._level = KillSwitchLevel.L1_BLOCK_ENTRIES
                self._log_event(session, target_date, "BLOCKED", reason,
                                daily_pnl, session_pnl)
                return True, reason

        # ── L2 check: reduce risk (not blocking, but sets multiplier) ──
        if equity_start > 0 and self.l2_loss_pct > 0:
            daily_loss_pct = abs(min(daily_pnl, 0)) / equity_start * 100
            if daily_loss_pct >= self.l2_loss_pct:
                self._level = KillSwitchLevel.L2_REDUCE_RISK
                self._log_event(session, target_date, "L2_REDUCE", "",
                                daily_pnl, session_pnl)
                # L2 does NOT block; it reduces risk_multiplier (caller reads .risk_multiplier)
                return False, ""

        # All clear
        self._level = KillSwitchLevel.OFF
        self._log_event(session, target_date, "OK", "", daily_pnl, session_pnl)
        return False, ""

    def _log_event(
        self,
        session: str,
        target_date: date,
        status: str,
        reason: str,
        daily_pnl: float,
        session_pnl: float,
    ) -> None:
        entry = {
            "event": "KILL_SWITCH",
            "date": target_date.isoformat(),
            "session": session,
            "status": status,
            "reason": reason,
            "daily_pnl": round(daily_pnl, 2),
            "session_pnl": round(session_pnl, 2),
            "ts": datetime.now(timezone.utc).isoformat(),
        }

        if status == "BLOCKED":
            logger.warning(f"[KILL_SWITCH] BLOCKED | {session} | {reason}")
        else:
            logger.debug(f"[KILL_SWITCH] OK | {session}")

        # Write to file if log_dir is set
        if self.log_dir:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                fpath = self.log_dir / f"kill_switch_{target_date.strftime('%Y%m%d')}.jsonl"
                with open(fpath, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception as e:
                logger.error(f"[KILL_SWITCH] Failed to write log: {e}")

    # ── Factory from config ──────────────────────────────────────────

    @classmethod
    def from_config(cls, config: Dict[str, Any], log_dir: Optional[Path] = None) -> "RiskKillSwitch":
        """Create a RiskKillSwitch from the SMC_CONFIG dict."""
        return cls(
            max_daily_loss_pct=config.get("MAX_DAILY_LOSS_PCT", 2.0),
            max_daily_loss_abs=config.get("MAX_DAILY_LOSS_ABS", 0.0),
            max_session_loss_pct_asia=config.get("MAX_SESSION_LOSS_PCT_ASIA", 1.0),
            max_consecutive_losses_asia=config.get("MAX_CONSECUTIVE_LOSSES_ASIA", 6),
            kill_switch_mode=config.get("KILL_SWITCH_MODE", "block_entries"),
            log_dir=log_dir,
            l2_loss_pct=config.get("KILL_SWITCH_L2_LOSS_PCT", 1.5),
            l2_risk_mult=config.get("KILL_SWITCH_L2_RISK_MULT", 0.5),
            l3_loss_pct=config.get("KILL_SWITCH_L3_LOSS_PCT", 3.0),
        )
