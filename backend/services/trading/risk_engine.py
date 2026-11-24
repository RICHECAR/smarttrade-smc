"""
Risk Engine — Kill Switch, Drawdown Governor, Consecutive Loss Pause
═══════════════════════════════════════════════════════════════════

CRITICAL safety layer. Evaluated BEFORE every trade placement.

Rules:
  1. Drawdown > RISK_DD_REDUCE_PCT → reduce size by 50%
  2. Drawdown > RISK_DD_BLOCK_PCT  → block new trades entirely
  3. Consecutive losses ≥ RISK_MAX_CONSEC_LOSSES → pause trading
  4. Daily loss > RISK_DAILY_MAX_LOSS_PCT → block for the day
  5. Equity crash > RISK_KILL_PCT in RISK_KILL_WINDOW_SEC → KILL SWITCH
     → emergency close ALL positions via existing execution layer

Config via .env:
  RISK_DD_REDUCE_PCT=3.0
  RISK_DD_BLOCK_PCT=5.0
  RISK_MAX_CONSEC_LOSSES=4
  RISK_DAILY_MAX_LOSS_PCT=3.0
  RISK_KILL_PCT=3.0
  RISK_KILL_WINDOW_SEC=300
  RISK_COOLDOWN_AFTER_LOSS_SEC=120
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from backend.services.trading.models import (
    RiskAction,
    RiskVerdict,
    TradeSignal,
)

logger = logging.getLogger("smarttrade.trading.risk")


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
#  EQUITY SNAPSHOT (for crash detection)
# ═══════════════════════════════════════════════════════════════

@dataclass
class EquitySnapshot:
    equity: float
    timestamp: float  # time.time()


class RiskEngine:
    """
    Central risk gate. Returns a RiskVerdict for every proposed trade.

    Also monitors equity in real-time for crash detection (kill switch).
    Kill switch state is persisted to disk to survive restarts.
    """

    _KILL_SWITCH_FILE = Path(os.getenv("DATA_DIR", "data")) / "kill_switch.json"

    def __init__(self) -> None:
        # ── Thresholds ──
        self._dd_reduce_pct = _envf("RISK_DD_REDUCE_PCT", 3.0)
        self._dd_block_pct = _envf("RISK_DD_BLOCK_PCT", 5.0)
        self._max_consec_losses = _envi("RISK_MAX_CONSEC_LOSSES", 4)
        self._daily_max_loss_pct = _envf("RISK_DAILY_MAX_LOSS_PCT", 3.0)
        self._kill_pct = _envf("RISK_KILL_PCT", 3.0)
        self._kill_window_sec = _envf("RISK_KILL_WINDOW_SEC", 300.0)
        self._cooldown_after_loss_sec = _envf("RISK_COOLDOWN_AFTER_LOSS_SEC", 120.0)

        # ── State ──
        self._peak_equity: float = 0.0
        self._daily_peak_equity: float = 0.0  # peak within the day (for accurate daily loss)
        self._daily_start_equity: float = 0.0
        self._daily_start_date: str = ""
        self._consecutive_losses: int = 0
        self._consecutive_wins: int = 0
        self._last_loss_time: float = 0.0
        self._kill_switch: bool = False
        self._kill_reason: str = ""
        self._lock = asyncio.Lock()

        # Rolling equity for crash detection
        self._equity_snapshots: Deque[EquitySnapshot] = deque(maxlen=1000)

        # ── Restore persisted kill switch ──
        self._restore_kill_switch()

    # ──────────────────────────────────────────────────
    #  PUBLIC API — Trade gate
    # ──────────────────────────────────────────────────

    def evaluate(
        self,
        signal: TradeSignal,
        current_equity: float,
    ) -> RiskVerdict:
        """
        Evaluate whether *signal* should be allowed given current risk state.
        Call BEFORE sizing and execution.
        """
        self._update_equity(current_equity)

        # ── Rule 0: Kill switch ──
        if self._kill_switch:
            return RiskVerdict(
                action=RiskAction.KILL,
                reason=f"KILL SWITCH: {self._kill_reason}",
                max_size_pct=0,
                current_drawdown_pct=self._current_drawdown_pct(current_equity),
                consecutive_losses=self._consecutive_losses,
                daily_loss_pct=self._daily_loss_pct(current_equity),
                kill_switch_active=True,
            )

        dd_pct = self._current_drawdown_pct(current_equity)
        daily_loss = self._daily_loss_pct(current_equity)

        # ── Rule 1: Daily loss limit ──
        if daily_loss >= self._daily_max_loss_pct:
            return RiskVerdict(
                action=RiskAction.BLOCK,
                reason=f"daily loss {daily_loss:.1f}% >= limit {self._daily_max_loss_pct}%",
                max_size_pct=0,
                current_drawdown_pct=dd_pct,
                consecutive_losses=self._consecutive_losses,
                daily_loss_pct=daily_loss,
            )

        # ── Rule 2: Consecutive losses ──
        if self._consecutive_losses >= self._max_consec_losses:
            return RiskVerdict(
                action=RiskAction.BLOCK,
                reason=f"{self._consecutive_losses} consecutive losses >= {self._max_consec_losses}",
                max_size_pct=0,
                current_drawdown_pct=dd_pct,
                consecutive_losses=self._consecutive_losses,
                daily_loss_pct=daily_loss,
            )

        # ── Rule 2b: Post-loss cooldown ──
        now = time.time()
        since_last_loss = now - self._last_loss_time
        if self._consecutive_losses > 0 and since_last_loss < self._cooldown_after_loss_sec:
            remaining = self._cooldown_after_loss_sec - since_last_loss
            return RiskVerdict(
                action=RiskAction.BLOCK,
                reason=f"loss cooldown ({remaining:.0f}s remaining)",
                max_size_pct=0,
                current_drawdown_pct=dd_pct,
                consecutive_losses=self._consecutive_losses,
                daily_loss_pct=daily_loss,
            )

        # ── Rule 3: Drawdown — block ──
        if dd_pct >= self._dd_block_pct:
            return RiskVerdict(
                action=RiskAction.BLOCK,
                reason=f"drawdown {dd_pct:.1f}% >= block threshold {self._dd_block_pct}%",
                max_size_pct=0,
                current_drawdown_pct=dd_pct,
                consecutive_losses=self._consecutive_losses,
                daily_loss_pct=daily_loss,
            )

        # ── Rule 4: Drawdown — reduce ──
        if dd_pct >= self._dd_reduce_pct:
            return RiskVerdict(
                action=RiskAction.REDUCE,
                reason=f"drawdown {dd_pct:.1f}% >= reduce threshold {self._dd_reduce_pct}%",
                max_size_pct=50.0,
                current_drawdown_pct=dd_pct,
                consecutive_losses=self._consecutive_losses,
                daily_loss_pct=daily_loss,
            )

        # ── All clear ──
        return RiskVerdict(
            action=RiskAction.ALLOW,
            reason="OK",
            max_size_pct=100.0,
            current_drawdown_pct=dd_pct,
            consecutive_losses=self._consecutive_losses,
            daily_loss_pct=daily_loss,
        )

    # ──────────────────────────────────────────────────
    #  PUBLIC API — Event recording
    # ──────────────────────────────────────────────────

    def record_win(self) -> None:
        """Record a winning trade."""
        self._consecutive_wins += 1
        self._consecutive_losses = 0

    def record_loss(self) -> None:
        """Record a losing trade."""
        self._consecutive_losses += 1
        self._consecutive_wins = 0
        self._last_loss_time = time.time()

    def reset_kill_switch(self) -> None:
        """Manually reset the kill switch (admin action)."""
        self._kill_switch = False
        self._kill_reason = ""
        self._persist_kill_switch()
        logger.warning("[RISK] Kill switch manually reset")

    def _persist_kill_switch(self) -> None:
        """Persist kill switch state to disk."""
        try:
            self._KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "active": self._kill_switch,
                "reason": self._kill_reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "peak_equity": self._peak_equity,
                "consecutive_losses": self._consecutive_losses,
            }
            self._KILL_SWITCH_FILE.write_text(json.dumps(payload), encoding="utf-8")
        except Exception as e:
            logger.error("[RISK] Failed to persist kill switch: %s", e)

    def _restore_kill_switch(self) -> None:
        """Restore kill switch from disk on startup.
        
        FAIL-CLOSED: If the file exists but is corrupted/unreadable,
        we activate the kill switch. Better to halt trading than to
        silently continue with unknown state.
        """
        try:
            if self._KILL_SWITCH_FILE.exists():
                raw = self._KILL_SWITCH_FILE.read_text(encoding="utf-8")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError(f"kill_switch.json is not a dict: {type(data)}")
                if data.get("active"):
                    self._kill_switch = True
                    self._kill_reason = data.get("reason", "Restored from disk after restart")
                    logger.critical(
                        "[RISK] KILL SWITCH RESTORED FROM DISK: %s", self._kill_reason
                    )
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
            # FAIL-CLOSED: corrupted file = assume kill switch was active
            self._kill_switch = True
            self._kill_reason = f"FAIL-CLOSED: kill_switch.json corrupted ({type(e).__name__}: {e})"
            logger.critical(
                "[RISK] KILL SWITCH FAIL-CLOSED — file corrupted: %s", e
            )
        except OSError as e:
            # FAIL-CLOSED: unreadable file = assume kill switch was active
            self._kill_switch = True
            self._kill_reason = f"FAIL-CLOSED: kill_switch.json unreadable ({e})"
            logger.critical(
                "[RISK] KILL SWITCH FAIL-CLOSED — file unreadable: %s", e
            )

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def consecutive_wins(self) -> int:
        return self._consecutive_wins

    # ──────────────────────────────────────────────────
    #  PUBLIC API — Equity feed
    # ──────────────────────────────────────────────────

    def update_equity(self, equity: float) -> None:
        """
        Feed equity on every cycle. Triggers crash detection.
        Call this FREQUENTLY (every price update or trading cycle).
        """
        self._update_equity(equity)

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    def _update_equity(self, equity: float) -> None:
        """Update internal equity tracking and check for crash."""
        if equity <= 0:
            return

        now = time.time()

        # Daily reset
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_start_date:
            self._daily_start_equity = equity
            self._daily_peak_equity = equity
            self._daily_start_date = today

        # Daily peak tracking (for accurate daily loss calc)
        if equity > self._daily_peak_equity:
            self._daily_peak_equity = equity

        # Peak tracking
        if equity > self._peak_equity:
            self._peak_equity = equity

        # Snapshot for crash detection
        self._equity_snapshots.append(EquitySnapshot(equity=equity, timestamp=now))

        # ── Crash detection: equity drop > kill_pct in kill_window ──
        if not self._kill_switch:
            cutoff = now - self._kill_window_sec
            window_max = equity
            for snap in self._equity_snapshots:
                if snap.timestamp >= cutoff and snap.equity > window_max:
                    window_max = snap.equity

            if window_max > 0:
                drop_pct = ((window_max - equity) / window_max) * 100
                if drop_pct >= self._kill_pct:
                    self._kill_switch = True
                    self._kill_reason = (
                        f"equity crashed {drop_pct:.1f}% in {self._kill_window_sec:.0f}s "
                        f"(from {window_max:.2f} to {equity:.2f})"
                    )
                    self._persist_kill_switch()
                    logger.critical(
                        "[RISK] KILL SWITCH ACTIVATED: %s", self._kill_reason
                    )

    def _current_drawdown_pct(self, equity: float) -> float:
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, ((self._peak_equity - equity) / self._peak_equity) * 100)

    def _daily_loss_pct(self, equity: float) -> float:
        if self._daily_start_equity <= 0:
            return 0.0
        # Use DAILY PEAK as reference (not start) to catch intra-day drawdowns
        ref = max(self._daily_start_equity, self._daily_peak_equity)
        loss = ref - equity
        if loss <= 0:
            return 0.0
        return (loss / ref) * 100
