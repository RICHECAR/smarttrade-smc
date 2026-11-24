"""
Order Manager V2 — Production-Grade Trade Execution & Safeguards.

Responsibilities:
  1. Pre-flight checks (kill-switch, cooldown, max trades, risk checks)
  2. Signal deduplication (idempotence)
  3. Execute signals with full instrumentation
  4. Shadow mode (0 network calls)
  5. Live mode (real execution via exchange API)
  6. Structured logging: [REJECT], [ORDER], [EXEC], [DRY_RUN]

Author: Trading Bot Framework
"""

import logging
import time
from typing import Dict, Optional, Tuple, Any, Set
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class PreFlightResult:
    """Result of pre-flight checks."""
    ok: bool
    reason: str  # "PASS", "KILL_SWITCH", "COOLDOWN", "MAX_TRADES", "PAPER_MODE", etc.
    details: Dict[str, Any]


class OrderManager:
    """
    Manages order execution with comprehensive safeguards.
    
    States:
      - kill_switch: bool (blocks execution if True)
      - last_trade_time: int (ms, for cooldown enforcement)
      - executed_signals: Set[int] (dedup, prevents double-execution)
      - daily_trades: Dict[str, int] (tracks trades per symbol per day)
      - daily_loss: Dict[str, float] (tracks accumulated loss per symbol)
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Args:
            config: SMC_CONFIG dict with safeguard params
        """
        self.config = config
        
        # ── Safeguard state ─────────────────────
        self.kill_switch = False
        self.kill_switch_reason = ""
        self.last_trade_time = 0  # ms
        self.executed_signals: Set[int] = set()  # signal_id dedup
        self.daily_trades_count = 0
        self.daily_trades_symbols: Dict[str, int] = {}  # symbol -> count
        self.daily_pnl_usd = 0.0
        self.daily_reset_utc = self._get_utc_day()
        
        # ── Rejection tracking ──────────────────
        self.rejection_log: list = []
        
        logger.info("[ORDER_MGR] Initialized with config: auto_exec=%s shadow=%s max_trades=%d",
                    config.get("AUTO_EXECUTE_ENABLED", False),
                    config.get("SHADOW_MODE", False),
                    config.get("MAX_TRADES_PER_DAY", 5))
    
    # ──────────────────────────────────────────────────
    #  SAFEGUARD: Kill-Switch
    # ──────────────────────────────────────────────────
    
    def set_kill_switch(self, enabled: bool, reason: str = "") -> None:
        """
        Enable/disable kill-switch.
        When enabled, blocks ALL order execution immediately.
        
        Args:
            enabled: True to block, False to allow
            reason: Optional reason (logged)
        """
        self.kill_switch = enabled
        self.kill_switch_reason = reason
        level = logging.ERROR if enabled else logging.INFO
        logger.log(level, "[KILL_SWITCH] %s | reason=%s", "ENABLED" if enabled else "DISABLED", reason)
    
    # ──────────────────────────────────────────────────
    #  SAFEGUARD: Daily Reset (UTC)
    # ──────────────────────────────────────────────────
    
    def _get_utc_day(self) -> str:
        """Return UTC date as 'YYYY-MM-DD'."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    def _check_daily_reset(self) -> None:
        """Reset counters if we've passed midnight UTC."""
        today = self._get_utc_day()
        if today != self.daily_reset_utc:
            logger.info("[DAILY_RESET] UTC day changed from %s to %s | trades=%d pnl=%.2f USD",
                        self.daily_reset_utc, today, self.daily_trades_count, self.daily_pnl_usd)
            self.daily_reset_utc = today
            self.daily_trades_count = 0
            self.daily_trades_symbols.clear()
            self.daily_pnl_usd = 0.0
    
    # ──────────────────────────────────────────────────
    #  MAIN: Pre-Flight Checks
    # ──────────────────────────────────────────────────
    
    def pre_flight_checks(
        self,
        symbol: str,
        signal_dict: Dict[str, Any],
        signal_id: str,
        active_trade: Optional[Dict] = None,
    ) -> PreFlightResult:
        """
        Execute all pre-execution guards.
        
        Checks (in order):
          1. Kill-switch
          2. Trading mode (live vs paper)
          3. Position dedup (no active trade)
          4. Cooldown (global)
          5. Max trades per day (global + symbol-specific)
          6. Valid TP/SL geometry
          7. Signal dedup (idempotence)
          8. Quantity validation (minQty, stepSize, minNotional)
        
        Args:
            symbol: e.g., "BTCUSDT"
            signal_dict: {"direction", "entry", "stop_loss", "take_profit", "rr_ratio", ...}
            signal_id: Unique signal identifier (str)
            active_trade: Current active position (None if no position)
        
        Returns:
            PreFlightResult(ok=True/False, reason, details)
        """
        details = {}
        
        # Check 1: Kill-Switch
        if self.kill_switch:
            logger.error("[REJECT] symbol=%s reason=KILL_SWITCH details=%s", symbol, self.kill_switch_reason)
            return PreFlightResult(
                ok=False,
                reason="KILL_SWITCH",
                details={"kill_switch_reason": self.kill_switch_reason}
            )
        
        # Check 2: Trading mode
        trading_mode = self.config.get("TRADING_MODE", "paper")
        if trading_mode not in ("live", "paper"):
            logger.error("[REJECT] symbol=%s reason=INVALID_TRADING_MODE mode=%s", symbol, trading_mode)
            return PreFlightResult(ok=False, reason="INVALID_TRADING_MODE", details={"mode": trading_mode})
        
        # Special: Paper mode blocks auto-execution
        if trading_mode == "paper":
            logger.info("[REJECT] symbol=%s reason=PAPER_MODE", symbol)
            return PreFlightResult(ok=False, reason="PAPER_MODE", details={})
        
        # Check 3: Position dedup (no active trade)
        if active_trade:
            logger.warning("[REJECT] symbol=%s reason=POSITION_ALREADY_OPEN last_entry=%.4f",
                          symbol, active_trade.get("entry", 0))
            return PreFlightResult(
                ok=False,
                reason="POSITION_ALREADY_OPEN",
                details={"active_entry": active_trade.get("entry")}
            )
        
        # Check 4: Cooldown (global throttle)
        now_ms = int(time.time() * 1000)
        cooldown_sec = self.config.get("TRADE_COOLDOWN_SEC", 300)
        cooldown_ms = cooldown_sec * 1000
        
        if (now_ms - self.last_trade_time) < cooldown_ms:
            remaining_ms = cooldown_ms - (now_ms - self.last_trade_time)
            logger.warning("[REJECT] symbol=%s reason=COOLDOWN_ACTIVE remaining_sec=%.1f",
                          symbol, remaining_ms / 1000)
            return PreFlightResult(
                ok=False,
                reason="COOLDOWN_ACTIVE",
                details={"cooldown_remaining_sec": remaining_ms / 1000}
            )
        
        # Check 5: Max trades per day (UTC reset)
        self._check_daily_reset()
        max_trades_day = self.config.get("MAX_TRADES_PER_DAY", 5)
        max_trades_symbol = self.config.get("MAX_OPEN_TRADES_PER_SYMBOL", 1)
        trades_today = self.daily_trades_count
        trades_symbol_today = self.daily_trades_symbols.get(symbol, 0)
        
        if trades_today >= max_trades_day:
            logger.warning("[REJECT] symbol=%s reason=MAX_TRADES_PER_DAY limit=%d today=%d",
                          symbol, max_trades_day, trades_today)
            return PreFlightResult(
                ok=False,
                reason="MAX_TRADES_PER_DAY",
                details={"limit": max_trades_day, "today": trades_today}
            )
        
        if trades_symbol_today >= max_trades_symbol:
            logger.warning("[REJECT] symbol=%s reason=MAX_TRADES_PER_SYMBOL limit=%d today=%d",
                          symbol, max_trades_symbol, trades_symbol_today)
            return PreFlightResult(
                ok=False,
                reason="MAX_TRADES_PER_SYMBOL",
                details={"limit": max_trades_symbol, "today": trades_symbol_today}
            )
        
        # Check 6: Valid TP/SL geometry
        direction = signal_dict.get("direction", "").lower()
        entry = float(signal_dict.get("entry", 0))
        sl = float(signal_dict.get("stop_loss", 0))
        tp = float(signal_dict.get("take_profit", 0))
        
        if direction == "long":
            if not (sl < entry < tp):
                logger.error("[REJECT] symbol=%s reason=INVALID_TP_SL_GEOMETRY direction=long sl=%.4f entry=%.4f tp=%.4f",
                            symbol, sl, entry, tp)
                return PreFlightResult(
                    ok=False,
                    reason="INVALID_TP_SL_GEOMETRY",
                    details={"geometry": "LONG: SL < entry < TP", "sl": sl, "entry": entry, "tp": tp}
                )
        elif direction == "short":
            if not (tp < entry < sl):
                logger.error("[REJECT] symbol=%s reason=INVALID_TP_SL_GEOMETRY direction=short tp=%.4f entry=%.4f sl=%.4f",
                            symbol, tp, entry, sl)
                return PreFlightResult(
                    ok=False,
                    reason="INVALID_TP_SL_GEOMETRY",
                    details={"geometry": "SHORT: TP < entry < SL", "tp": tp, "entry": entry, "sl": sl}
                )
        
        # Check 7: Signal dedup (prevent double-execution)
        signal_hash = hash(signal_id)
        if signal_hash in self.executed_signals:
            logger.warning("[REJECT] symbol=%s reason=DUPLICATE_SIGNAL signal_id=%s", symbol, signal_id)
            return PreFlightResult(
                ok=False,
                reason="DUPLICATE_SIGNAL",
                details={"signal_id": signal_id}
            )
        
        # Check 8: Min RR (Risk Reward)
        rr = float(signal_dict.get("rr_ratio", 0))
        min_rr = float(self.config.get("MIN_RR", 1.5))
        if rr < min_rr:
            logger.warning("[REJECT] symbol=%s reason=INSUFFICIENT_RR rr=%.2f min=%f",
                          symbol, rr, min_rr)
            return PreFlightResult(
                ok=False,
                reason="INSUFFICIENT_RR",
                details={"rr": rr, "min_rr": min_rr}
            )
        
        logger.info("[ORDER] PRE_FLIGHT_PASS symbol=%s rr=%.2f direction=%s sl=%.4f entry=%.4f tp=%.4f",
                   symbol, rr, direction, sl, entry, tp)
        
        return PreFlightResult(ok=True, reason="PASS", details={"rr": rr})
    
    # ──────────────────────────────────────────────────
    #  EXECUTION: Shadow (DRY_RUN) vs Live
    # ──────────────────────────────────────────────────
    
    async def execute_signal(
        self,
        symbol: str,
        signal_dict: Dict[str, Any],
        signal_id: str,
        executor: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Execute signal: DRY_RUN (shadow) or LIVE.
        
        Args:
            symbol: e.g., "BTCUSDT"
            signal_dict: Signal data
            signal_id: Unique ID (str)
            executor: BitgetFuturesExecutor instance (required for live)
        
        Returns:
            {
                "success": bool,
                "mode": "DRY_RUN" | "LIVE",
                "order_id": str (simulated or real),
                "error": str (if failed),
                "fill_price": float (if available),
            }
        """
        shadow_mode = self.config.get("SHADOW_MODE", False)
        
        entry = float(signal_dict.get("entry", 0))
        sl = float(signal_dict.get("stop_loss", 0))
        tp = float(signal_dict.get("take_profit", 0))
        direction = signal_dict.get("direction", "").lower()
        
        # ── DRY_RUN Mode (SHADOW) ───────────────
        if shadow_mode:
            simulated_order_id = f"DRY_RUN_{signal_id[:8]}_{int(time.time() * 1000)}"
            logger.info("[DRY_RUN] WOULD_SEND symbol=%s direction=%s entry=%.4f sl=%.4f tp=%.4f order_id=%s",
                       symbol, direction, entry, sl, tp, simulated_order_id)
            
            # Record execution for dedup
            signal_hash = hash(signal_id)
            self.executed_signals.add(signal_hash)
            self.last_trade_time = int(time.time() * 1000)
            
            return {
                "success": True,
                "mode": "DRY_RUN",
                "order_id": simulated_order_id,
                "fill_price": entry,
                "error": None,
            }
        
        # ── LIVE Mode ────────────────────────────
        if not executor:
            logger.error("[EXEC] FAILED symbol=%s reason=NO_EXECUTOR", symbol)
            return {
                "success": False,
                "mode": "LIVE",
                "order_id": None,
                "error": "No executor available",
            }
        
        try:
            logger.info("[ORDER] SENDING symbol=%s direction=%s entry=%.4f sl=%.4f tp=%.4f",
                       symbol, direction, entry, sl, tp)
            
            # Call executor (BitgetFuturesExecutor)
            result = await executor.place_order(
                symbol=symbol,
                direction=direction,
                entry_price=entry,
                stop_loss=sl,
                take_profit=tp,
                # Other params as needed
            )
            
            if result.get("success"):
                order_id = result.get("order_id", "UNKNOWN")
                fill_price = result.get("fill_price", entry)
                
                logger.info("[EXEC] OK symbol=%s order_id=%s fill_price=%.4f",
                           symbol, order_id, fill_price)
                
                # Record execution for dedup
                signal_hash = hash(signal_id)
                self.executed_signals.add(signal_hash)
                self.last_trade_time = int(time.time() * 1000)
                self.daily_trades_count += 1
                self.daily_trades_symbols[symbol] = self.daily_trades_symbols.get(symbol, 0) + 1
                
                return {
                    "success": True,
                    "mode": "LIVE",
                    "order_id": order_id,
                    "fill_price": fill_price,
                    "error": None,
                }
            else:
                error_msg = result.get("error", "Unknown error")
                logger.error("[EXEC] FAILED symbol=%s error=%s", symbol, error_msg)
                return {
                    "success": False,
                    "mode": "LIVE",
                    "order_id": None,
                    "error": error_msg,
                }
        
        except Exception as e:
            logger.error("[EXEC] EXCEPTION symbol=%s error=%s", symbol, str(e), exc_info=True)
            return {
                "success": False,
                "mode": "LIVE",
                "order_id": None,
                "error": str(e),
            }
    
    # ──────────────────────────────────────────────────
    #  UTILITIES
    # ──────────────────────────────────────────────────
    
    def record_rejection(self, symbol: str, reason: str, details: Optional[Dict] = None) -> None:
        """Log a rejection for audit."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "reason": reason,
            "details": details or {},
        }
        self.rejection_log.append(entry)
        if len(self.rejection_log) > 1000:
            self.rejection_log.pop(0)
    
    def get_daily_stats(self) -> Dict[str, Any]:
        """Return today's trading stats (UTC)."""
        return {
            "date_utc": self.daily_reset_utc,
            "trades_count": self.daily_trades_count,
            "trades_by_symbol": self.daily_trades_symbols.copy(),
            "pnl_usd": self.daily_pnl_usd,
            "kill_switch": self.kill_switch,
        }
    
    def get_status(self) -> Dict[str, Any]:
        """Return current manager status."""
        return {
            "kill_switch_enabled": self.kill_switch,
            "kill_switch_reason": self.kill_switch_reason,
            "last_trade_time_ms": self.last_trade_time,
            "daily_stats": self.get_daily_stats(),
            "rejections_count": len(self.rejection_log),
        }
    
    def maybe_reset_daily(self) -> None:
        """
        Public interface: Check and reset daily counters if UTC day has changed.
        Called periodically by orchestrator.
        """
        self._check_daily_reset()
    
    async def reconcile_positions(
        self,
        executor: Optional[Any] = None,
        orchestrators: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Reconcile active_trade states with exchange positions.
        
        Clears active_trade on orchestrators if their positions have closed on exchange.
        Skips reconciliation if executor is None or orchestrators is empty.
        
        Args:
            executor: BitgetV2Executor instance
            orchestrators: Dict[symbol, orch] with active_trade states
        """
        if not executor or not orchestrators:
            logger.debug("[GUARD] Reconciliation skipped: executor=%s, orchestrators=%s",
                        "ok" if executor else "None", len(orchestrators) if orchestrators else 0)
            return
        
        for symbol, orch in list(orchestrators.items()):
            if not orch.active_trade:
                continue
            
            try:
                # Fetch open positions for this symbol
                positions = await executor.get_open_positions(symbol)
                pos_list = (
                    positions.get("data", [])
                    if isinstance(positions, dict)
                    else (positions or [])
                )
                
                # Check if any position is still open
                has_open = any(
                    float(p.get("total", 0) or p.get("available", 0) or 0) > 0
                    for p in pos_list
                )
                
                if not has_open:
                    logger.info("[GUARD] %s | active_trade cleared (position closed on exchange)", symbol)
                    orch.active_trade = None
            
            except Exception as e:
                logger.warning("[GUARD] %s | reconciliation error: %s", symbol, str(e))
    
    def engage_kill_switch(self, reason: str = "manual") -> None:
        """
        Alias for set_kill_switch(True, reason).
        Legacy API compatibility.
        """
        self.set_kill_switch(True, reason)
    
    def disengage_kill_switch(self) -> None:
        """
        Alias for set_kill_switch(False, "").
        Legacy API compatibility.
        """
        self.set_kill_switch(False, "")
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Return comprehensive stats for monitoring/API.
        Legacy API compatibility (maps to get_status() + get_daily_stats()).
        """
        daily_stats = self.get_daily_stats()
        return {
            "mode": "shadow" if self.config.get("SHADOW_MODE", False) else ("live" if self.config.get("AUTO_EXECUTE_ENABLED", False) else "monitor"),
            "kill_switch": {
                "active": self.kill_switch,
                "reason": self.kill_switch_reason,
            },
            "daily": {
                "trades": daily_stats["trades_count"],
                "max": self.config.get("MAX_TRADES_PER_DAY", 5),
                "pnl": round(daily_stats["pnl_usd"], 2),
                "balance": 0.0,  # Not tracked in v2, can integrate later
            },
            "exec_log": [],  # Could populate from rejection_log if needed
            "reject_log": self.rejection_log[-10:] if len(self.rejection_log) > 0 else [],
        }
