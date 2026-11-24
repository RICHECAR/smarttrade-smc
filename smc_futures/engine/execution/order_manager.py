"""
OrderManager — Production-grade auto-execution with guard-rails
════════════════════════════════════════════════════════════════
Pipeline:  Signal → pre_flight_checks() → execute_signal() → record_fill() → persist_trade()

Guards:
  - AUTO_EXECUTE enabled (or SHADOW)
  - TRADING_MODE=live
  - No active_trade on symbol
  - Idempotency (signal hash dedup)
  - Daily trade count < MAX_TRADES_PER_DAY
  - Daily loss < MAX_DAILY_LOSS_PCT
  - Cooldown > TRADE_COOLDOWN_SEC
  - Kill-switch inactive
  - Balance > 0
  - Min Notional & Qty Checks
  
LIVE PERSISTENCE:
  - SQLite via LiveTradesService (on_trade_opened/on_trade_closed)
  - WebSocket broadcast (trade_opened/trade_closed)
  - Crash recovery (recover_open_trades on startup)
"""

import logging
import math
import time
import asyncio
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field

from smc_futures.engine.execution.contract_specs import get_contract_specs, ContractSpecs

# ── Analytics DB instrumentation ─────────────────────────────────────────────
try:
    from backend.services.analytics_service import AnalyticsRecorder as _AR
    _analytics_available = True
except ImportError:
    _AR = None  # type: ignore
    _analytics_available = False

# ── Phase 6: Performance Governor (in-memory real-time metrics) ──────────────
try:
    from smc_futures.observability.performance_governor import governor as _governor
except ImportError:
    _governor = None

# ── Phase 7: Wiring (event bus, risk V2 — sole authority) ──────────────────
try:
    from core.phase6_wiring import (
        evaluate_risk_v2 as _evaluate_risk_v2,
        publish_event as _p6_publish,
        record_risk_v2_trade_result as _record_risk_v2_result,
        update_risk_v2_capital as _update_risk_v2_capital,
    )
    _phase6_wiring_available = True
except ImportError:
    _phase6_wiring_available = False

# ── Phase 7: Adaptive Risk Engine ──────────────────────────────────────
try:
    from smc_futures.engine.risk.adaptive_risk_engine import (
        adaptive_risk_engine as _adaptive_risk,
        build_risk_inputs as _build_risk_inputs,
    )
    _adaptive_risk_available = True
except ImportError:
    _adaptive_risk = None
    _build_risk_inputs = None
    _adaptive_risk_available = False

# ── DrawdownGovernor (anti-ruin engine) ──────────────────────────
try:
    from smc_futures.risk.drawdown_governor import DrawdownGovernor as _DDGov
    _dd_gov_available = True
except ImportError:
    _DDGov = None
    _dd_gov_available = False

# Multi-broker configuration
def is_multi_broker_enabled() -> bool:
    """Check if multi-broker mirror trading is enabled."""
    from smc_futures.smc_config import SMC_CONFIG
    mode = SMC_CONFIG.get("MULTI_BROKER_MODE", "disabled")
    if mode != "disabled":
        return True
    # Legacy compat: check old env vars
    mirror = os.getenv("MULTI_BROKER_MIRROR", "0").lower() in ("1", "true", "yes")
    dual = os.getenv("DUAL_EXECUTION", "0").lower() in ("1", "true", "yes")
    multi = os.getenv("MULTI_BROKER_EXECUTION", "0").lower() in ("1", "true", "yes")
    kraken_on = os.getenv("KRAKEN_ENABLED", "0").lower() in ("1", "true", "yes")
    kraken_exec = os.getenv("KRAKEN_EXECUTION_ENABLED", "false").lower() in ("1", "true", "yes")
    return mirror or dual or multi or (kraken_on and kraken_exec)

def require_both_brokers() -> bool:
    """Check if strict mode is enabled (rollback if any broker fails)."""
    return os.getenv("REQUIRE_BOTH_BROKERS", "0").lower() in ("1", "true", "yes")


# Kraken symbol mapping (kept in sync with kraken_futures_executor.py)
KRAKEN_SYMBOL_MAP = {
    "BTCUSDT": "PF_XBTUSD",
    "ETHUSDT": "PF_ETHUSD",
    "SOLUSDT": "PF_SOLUSD",
    "XRPUSDT": "PF_XRPUSD",
    "DOGEUSDT": "PF_DOGEUSD",
}

def to_kraken_symbol(symbol: str) -> str:
    """Convert standard symbol to Kraken Futures format."""
    normalized = symbol.upper().replace("/", "").replace(":", "").replace("-", "")
    # Guard: already in Kraken format — pass through to prevent double prefix (PF_PF_)
    if normalized.startswith(("PF_", "PI_", "FF_", "FI_")):
        return normalized
    return KRAKEN_SYMBOL_MAP.get(normalized, normalized)


@dataclass
class BrokerResult:
    """Result from a single broker execution."""
    broker: str
    success: bool
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    quantity: Optional[float] = None
    error: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None


@dataclass
class MultiBrokerResult:
    """Aggregated result from multi-broker execution."""
    signal_id: str
    success: bool  # True if at least one broker succeeded
    full_success: bool  # True if ALL brokers succeeded
    broker_results: Dict[str, BrokerResult] = field(default_factory=dict)
    rollback_attempted: bool = False
    rollback_success: bool = False
    
    @property
    def success_brokers(self) -> List[str]:
        return [b for b, r in self.broker_results.items() if r.success]
    
    @property
    def failed_brokers(self) -> List[str]:
        return [b for b, r in self.broker_results.items() if not r.success and not r.skipped]

# Import persistence hooks
try:
    from backend.services.trade_persistence_hook import (
        on_trade_opened,
        on_trade_closed,
        recover_open_trades,
        sync_with_exchange,
        set_ws_manager,
    )
    from backend.services.position_persistence_hook import (
        on_position_opened,
    )
    PERSISTENCE_AVAILABLE = True
except ImportError:
    PERSISTENCE_AVAILABLE = False
    
    async def on_trade_opened(*args, **kwargs):
        return None
    def on_position_opened(*args, **kwargs):
        return None
    async def on_trade_closed(*args, **kwargs):
        return None
    async def recover_open_trades():
        return []
    async def sync_with_exchange(*args, **kwargs):
        pass
    def set_ws_manager(manager):
        pass

logger = logging.getLogger(__name__)

# Epsilon for floating-point RR comparison (avoids 1.9999999 < 2.0 false rejections)
RR_EPSILON = 1e-6


@dataclass
class PreFlightResult:
    """Result of pre-flight checks."""
    ok: bool
    reason: str  # "PASS", "KILL_SWITCH", "COOLDOWN", "MAX_TRADES", "PAPER_MODE", etc.
    details: Dict[str, Any]


class OrderManager:
    """
    Centralized order execution manager.
    Instantiated once at module level, shared across all orchestrators (via AppContext).
    """

    def __init__(self, config: Dict[str, Any]):
        # ── Config ────────────────────────────────────────
        self.config = config
        
        # Use new strict boolean keys
        self.auto_execute_enabled: bool = config.get("AUTO_EXECUTE_ENABLED", False)
        self.shadow_mode: bool = config.get("SHADOW_MODE", False)
        self.trading_mode: str = config.get("TRADING_MODE", "paper").lower()
        
        # FORCE_EXECUTE bypasses soft filters (cooldown, MIN_RR, session) but NOT hard filters
        self.force_execute: bool = config.get("FORCE_EXECUTE", False)
        
        self.order_type: str = config.get("ORDER_TYPE", "market").lower()
        self.limit_offset_bps: int = config.get("LIMIT_OFFSET_BPS", 5)
        
        self.max_trades_per_day: int = config.get("MAX_TRADES_PER_DAY", 5)
        self.max_daily_loss_pct: float = config.get("MAX_DAILY_LOSS_PCT", 3.0)
        self.max_open_symbol: int = config.get("MAX_OPEN_TRADES_PER_SYMBOL", 1)
        self.cooldown_sec: int = config.get("TRADE_COOLDOWN_SEC", 300)
        
        self.min_rr: float = config.get("MIN_RR", 1.5)
        self.risk_per_trade_pct: float = config.get("RISK_PER_TRADE_PCT", 0.01)
        
        # Phase 4.5 Latency Tracking (singleton, no circular dependency)
        from smc_futures.engine.analysis.latency_tracker import latency_tracker
        self.latency_tracker = latency_tracker
        self.default_leverage: int = config.get("DEFAULT_LEVERAGE", 3)

        # ── Kill Switch ───────────────────────────────────
        self.kill_switch_active: bool = False
        self.kill_switch_reason: str = ""

        # ── Daily Counters (global) ───────────────────────
        self._trades_today: int = 0
        self._daily_pnl: float = 0.0
        self._last_reset_date: str = self._utc_date_str()
        self._account_balance: float = 0.0  # refreshed from pre_flight settings

        # ── Per-Symbol State ──────────────────────────────
        self._per_symbol_trades: Dict[str, int] = defaultdict(int)
        self._last_trade_time: Dict[str, float] = defaultdict(float)
        self._executed_ids: Set[str] = set()

        # ── Execution Log ─────────────────────────────────
        self._exec_log: list = []  # last N fills
        self._reject_log: list = []  # last N rejects

        # ── Hard Cooldown & Auto-Upsize ───────────────────
        self.hard_reject_cooldown_sec = int(os.getenv("HARD_REJECT_COOLDOWN_SEC", "120"))
        upsize_env = os.getenv("AUTO_UPSIZE_TO_MIN_NOTIONAL", config.get("AUTO_UPSIZE_TO_MIN_NOTIONAL", "false"))
        if isinstance(upsize_env, str):
            self.auto_upsize_to_min_notional = upsize_env.lower() in ("true", "1", "yes")
        else:
            self.auto_upsize_to_min_notional = bool(upsize_env)

        self._hard_reject_cache: Dict[str, Tuple[float, str]] = {}
        self._cache_cleanup_interval = 300  # purge expired entries every 5 min
        self._last_cache_cleanup = 0.0

        # ── Correlation Filter ────────────────────────────
        from smc_futures.risk.correlation_filter import CorrelationFilter
        self.correlation_filter = CorrelationFilter(config)
        # Track which symbols have open positions (updated by record_fill/close)
        self._open_position_symbols: Set[str] = set()
        # Track internal position sizes for drift detection (symbol → qty)
        self._position_sizes: Dict[str, float] = {}
        self._position_size_units: Dict[str, str] = {}

        # Track pending (in-flight) orders for exposure projection
        self.pending_orders: List[Dict[str, Any]] = []

        # ── Extreme Volatility Filter ─────────────────────
        self._extreme_vol_enabled: bool = config.get("EXTREME_VOLATILITY_FILTER_ENABLED", True)
        self._extreme_vol_max_ratio: float = config.get("EXTREME_VOLATILITY_MAX_ATR_RATIO", 2.5)

        # ── Slippage Protection ────────────────────────────
        self._max_slippage_atr: float = config.get("MAX_ENTRY_SLIPPAGE_ATR", 0.3)
        self._max_slippage_bps: float = config.get("MAX_ENTRY_SLIPPAGE_BPS", 30.0)
        self._slippage_kill_bps: float = config.get("SLIPPAGE_KILL_THRESHOLD_BPS", 60.0)
        self._slippage_kill_cooldown_sec: int = config.get("SLIPPAGE_KILL_COOLDOWN_SEC", 300)
        self._slippage_kill_until: float = 0.0  # timestamp until which trading is paused

        # ── Emergency Close Retry Config ──────────────────
        self._emergency_max_retries: int = config.get("EMERGENCY_CLOSE_MAX_RETRIES", 3)
        self._emergency_backoff_base: float = config.get("EMERGENCY_CLOSE_BACKOFF_BASE", 1.5)
        # Track positions that failed emergency close (for reconciler)
        self._orphan_positions: Set[str] = set()

        # ── Execution Latency Guard ───────────────────────
        self._max_signal_age_sec: float = config.get("MAX_SIGNAL_AGE_SEC", 5.0)

        # ── Drawdown Governor (anti-ruin) ─────────────────
        self._dd_governor: Optional[Any] = None
        if _dd_gov_available and _DDGov is not None:
            try:
                from pathlib import Path
                log_dir = Path(config.get("LOG_DIR", "logs"))
                self._dd_governor = _DDGov.from_config(config, log_dir=log_dir)
                logger.info("[ORDER_MGR] DrawdownGovernor loaded")
            except Exception as e:
                logger.warning(f"[ORDER_MGR] DrawdownGovernor init failed: {e}")
                self._dd_governor = None

        # ── AccountStateService reference (set by AppContext) ─────
        self._account_state = None  # set via set_account_state()

        # ── StrategyAllocator reference (set by AppContext) ─────
        self._strategy_allocator = None  # set via set_strategy_allocator()

        mode_str = "SHADOW" if self.shadow_mode else ("LIVE" if self.auto_execute_enabled else "MONITOR")
        force_str = " | FORCE_EXECUTE=ON" if self.force_execute else ""
        logger.info(
            f"[ORDER_MGR] Initialized | MODE={mode_str}"
            f" | trading_mode={self.trading_mode}"
            f" | max_trades/day={self.max_trades_per_day}"
            f" | max_loss_pct={self.max_daily_loss_pct}%{force_str}"
        )

    # ──────────────────────────────────────────────────────
    #  Phase 6 — Performance Governor Helper
    # ──────────────────────────────────────────────────────

    # Filters classified as HARD (never bypassed) vs SOFT (bypassed by FORCE_EXECUTE)
    _HARD_FILTERS = frozenset({
        "DISABLED_SYMBOL", "AUTO_EXECUTE_DISABLED", "PAPER_MODE",
        "GLOBAL_RISK", "SLIPPAGE_KILL_COOLDOWN", "SIGNAL_TOO_OLD",
        "KILL_SWITCH", "DRAWDOWN_GOVERNOR", "ACTIVE_TRADE", "EXTREME_VOLATILITY",
        "CORRELATION_BLOCKED", "DUPLICATE_SIGNAL", "MAX_DAILY_LOSS",
        "ZERO_BALANCE", "INVALID_TPSL", "NOTIONAL_TOO_LOW",
        "ACCOUNT_STALE", "ACCOUNT_INCOHERENT",
        "STRATEGY_DISABLED_BY_LEARNING",
    })

    def _gov_reject(self, symbol: str, reason: str, signal_dict: dict = None) -> None:
        """Notify PerformanceGovernor of a rejection."""
        if _governor is None:
            return
        sd = signal_dict or {}
        base_reason = reason.split(":")[0]  # strip details after colon
        _governor.record_rejection(
            symbol=symbol,
            filter_name=base_reason,
            is_hard=base_reason in self._HARD_FILTERS,
            strategy=sd.get("strategy_id", ""),
            session=sd.get("session", ""),
        )

    # ──────────────────────────────────────────────────────
    #  Analytics DB Helper
    # ──────────────────────────────────────────────────────

    def _record_exec_attempt(
        self,
        symbol: str,
        status: str,
        block_reason: str = None,
        block_details: dict = None,
        signal_dict: dict = None,
        signal_id: str = None,
        is_multi_broker: bool = False,
        broker: str = None,
        order_id: str = None,
        fill_price: float = None,
        latency_ms: int = None,
    ):
        """Fire-and-forget DB write for an execution attempt + governor feed."""
        # Phase 6: Feed PerformanceGovernor on rejections
        if status == "PRE_FLIGHT_REJECT" and block_reason:
            self._gov_reject(symbol, block_reason, signal_dict)
            if _governor:
                _governor.record_signal_evaluated(
                    strategy=(signal_dict or {}).get("strategy_id", "")
                )

        if not _analytics_available or _AR is None:
            return
        try:
            sd = signal_dict or {}
            _AR.record_execution_attempt(
                symbol=symbol,
                status=status,
                block_reason=block_reason,
                block_details=block_details,
                signal_id=signal_id,
                side=sd.get("direction"),
                broker=broker,
                is_multi_broker=is_multi_broker,
                entry_price=float(sd.get("entry", 0)) or None,
                quantity=float(sd.get("quantity", 0)) or None,
                account_balance=self._account_balance or None,
                regime=sd.get("regime"),
                order_id=order_id,
                fill_price=fill_price,
                latency_ms=latency_ms,
            )
        except Exception:
            pass  # Never let analytics break trading

    # ──────────────────────────────────────────────────────
    #  Pre-Flight Checks
    # ──────────────────────────────────────────────────────

    def _cleanup_hard_reject_cache(self) -> None:
        """Purge expired entries from hard reject cache to prevent memory bloat."""
        now = time.time()
        if now - self._last_cache_cleanup < self._cache_cleanup_interval:
            return
        self._last_cache_cleanup = now
        expired = [k for k, (ts, _) in self._hard_reject_cache.items()
                   if now - ts >= self.hard_reject_cooldown_sec]
        for k in expired:
            del self._hard_reject_cache[k]
        if expired:
            logger.debug(f"[CACHE_CLEANUP] Purged {len(expired)} expired hard-reject entries")

    def _compute_fingerprint(self, symbol: str, signal_dict: Dict) -> str:
        """Compute a stable fingerprint for a signal to prevent spam loops on hard rejects."""
        direction = signal_dict.get("direction", "")
        entry = float(signal_dict.get("entry", 0))
        # Round entry price for stable hash across minor tick variations
        rounded_entry = round(entry, 4)
        strat = signal_dict.get("strategy_id", "")
        tf = signal_dict.get("trigger_tf", "")
        return f"{symbol}_{direction}_{rounded_entry}_{strat}_{tf}"

    def pre_flight_checks(
        self,
        symbol: str,
        signal_dict: Dict,
        signal_id: str,
        active_trade: Optional[Dict] = None,
        account_balance: float = 0.0,
        quantity: float = 0.0,
        open_symbols: Optional[Set[str]] = None,
        atr_info: Optional[Dict[str, float]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Run all guard-rails before sending an order.
        Returns (pass, reason_code, details).
        
        FORCE_EXECUTE=1 bypasses soft filters (cooldown, LOW_RR) but NOT:
        - Kill Switch, Max Daily Loss, Zero Balance (hard safety)
        
        Args:
            open_symbols: Set of symbols with currently open positions (for correlation check)
            atr_info: Dict with 'current_atr' and 'avg_atr' for volatility checks
        
        NOTE: Returns tuple to match orchestrator unpacking (ok, reason, details)
        """
        self._account_balance = account_balance
        details = {}

        # 0a. Disabled Symbols (HARD - never bypass)
        disabled_symbols = set(
            s.upper() for s in self.config.get("DISABLED_SYMBOLS", [])
        )
        if symbol.upper() in disabled_symbols:
            logger.warning(f"[ROUTER] {symbol} skipped (disabled by config)")
            self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "DISABLED_SYMBOL", signal_dict=signal_dict, signal_id=signal_id)
            return False, "DISABLED_SYMBOL", {}

        # 0. Global Mode Checks (HARD - never bypass)
        if not (self.auto_execute_enabled or self.shadow_mode):
            logger.warning(f"[ORDER_REJECTED] {symbol} | reason=AUTO_EXECUTE_DISABLED")
            self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "AUTO_EXECUTE_DISABLED", signal_dict=signal_dict, signal_id=signal_id)
            return False, "AUTO_EXECUTE_DISABLED", {}
        
        if self.trading_mode != "live" and not self.shadow_mode:
            logger.info(f"[ORDER_REJECTED] {symbol} | reason=PAPER_MODE | trading_mode={self.trading_mode}")
            self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "PAPER_MODE", signal_dict=signal_dict, signal_id=signal_id)
            return False, "PAPER_MODE", {}

        # 0b. Account State coherence + staleness (HARD - never bypass)
        if self._account_state is not None:
            block = self._account_state.block_reason
            if block:
                tag = "ACCOUNT_STALE" if "STALE" in block else "ACCOUNT_INCOHERENT"
                _snap = self._account_state.get_snapshot() if hasattr(self._account_state, 'get_snapshot') else {}
                _active = _snap.get("active_broker", "?")
                _mode = _snap.get("execution_broker_mode", "?")
                logger.error(
                    f"[ORDER_REJECTED] {symbol} | reason={tag} | {block} "
                    f"| active_broker={_active} | mode={_mode}"
                )
                self._record_exec_attempt(
                    symbol, "PRE_FLIGHT_REJECT", tag,
                    {"block_reason": block, "active_broker": _active, "mode": _mode},
                    signal_dict=signal_dict, signal_id=signal_id,
                )
                return False, f"{tag}:{block}", {}

        # 0c. Risk Engine V2 — SOLE AUTHORITY (unconditional blocking gate)
        signal_dict["risk_multiplier"] = 1.0
        signal_dict["regime"] = "NORMAL"
        if not _phase6_wiring_available:
            # Phase 10: FAIL-CLOSED — no risk wiring = no trading
            logger.error(f"[ORDER_REJECTED] {symbol} | reason=RISK_WIRING_UNAVAILABLE | phase6_wiring import failed")
            self._record_exec_attempt(
                symbol, "PRE_FLIGHT_REJECT", "RISK_WIRING_UNAVAILABLE",
                {"detail": "core.phase6_wiring import failed — fail-closed"},
                signal_dict=signal_dict, signal_id=signal_id,
            )
            return False, "RISK_WIRING_UNAVAILABLE", {"detail": "phase6_wiring not importable"}
        else:
            entry_price = float(signal_dict.get("entry", 0))
            proposed_notional = abs(quantity * entry_price) if entry_price > 0 else 0
            v2_allowed, v2_mult, v2_reasons = _evaluate_risk_v2(
                symbol=symbol,
                side=signal_dict.get("direction", "long"),
                proposed_size_usd=proposed_notional,
                current_price=entry_price,
                open_positions=None,  # V2 tracks internally
            )
            if not v2_allowed:
                reason_str = ",".join(v2_reasons) if v2_reasons else "BLOCKED"
                logger.warning(
                    f"[ORDER_REJECTED] {symbol} | reason=RISK_V2_BLOCKED | {reason_str}"
                )
                self._record_exec_attempt(
                    symbol, "PRE_FLIGHT_REJECT", "RISK_V2_BLOCKED",
                    {"reasons": v2_reasons},
                    signal_dict=signal_dict, signal_id=signal_id,
                )
                # Publish rejection event
                try:
                    from core.events.event_types import RiskEvent, EventType
                    _p6_publish(RiskEvent(
                        EventType.RISK_EXPOSURE_BREACH,
                        reason=",".join(v2_reasons) or "risk_v2_blocked",
                        symbol=symbol,
                        current_value=proposed_notional,
                        action="BLOCK",
                    ))
                except Exception:
                    pass
                return False, f"RISK_V2_BLOCKED:{reason_str}", {"v2_reasons": v2_reasons}
            if v2_mult < 1.0:
                signal_dict["risk_multiplier"] = v2_mult
                logger.info(
                    f"[RISK_V2] {symbol} | size_multiplier={v2_mult:.2f}"
                    f" | risk_multiplier → {signal_dict['risk_multiplier']:.2f}"
                    f" | reasons={v2_reasons}"
                )

        # 0d. Slippage Kill Cooldown (HARD - never bypass)
        if self._slippage_kill_until > 0:
            remaining = self._slippage_kill_until - time.time()
            if remaining > 0:
                logger.warning(
                    f"[ORDER_REJECTED] {symbol} | reason=SLIPPAGE_KILL_COOLDOWN"
                    f" | remaining={remaining:.0f}s"
                )
                self._record_exec_attempt(
                    symbol, "PRE_FLIGHT_REJECT", "SLIPPAGE_KILL_COOLDOWN",
                    {"remaining_sec": remaining},
                    signal_dict=signal_dict, signal_id=signal_id,
                )
                return False, "SLIPPAGE_KILL_COOLDOWN", {"remaining": remaining}
            else:
                self._slippage_kill_until = 0.0
                logger.info("[SLIPPAGE_KILL] Cooldown expired — trading resumed")

        # 0e. Execution Latency Guard (HARD - never bypass)
        t_signal = signal_dict.get("t_signal", 0)
        if t_signal > 0 and self._max_signal_age_sec > 0:
            signal_age = time.time() - t_signal
            if signal_age > self._max_signal_age_sec:
                logger.warning(
                    f"[ORDER_REJECTED] {symbol} | reason=SIGNAL_TOO_OLD"
                    f" | age={signal_age:.1f}s > max={self._max_signal_age_sec}s"
                )
                self._record_exec_attempt(
                    symbol, "PRE_FLIGHT_REJECT", "SIGNAL_TOO_OLD",
                    {"signal_age_sec": signal_age, "max_age_sec": self._max_signal_age_sec},
                    signal_dict=signal_dict, signal_id=signal_id,
                )
                return False, "SIGNAL_TOO_OLD", {"signal_age": signal_age}

        # 1. Kill switch (HARD - never bypass)
        if self.kill_switch_active:
            reason = f"KILL_SWITCH:{self.kill_switch_reason}"
            logger.error(f"[ORDER_REJECTED] {symbol} | reason={reason}")
            self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "KILL_SWITCH", {"kill_reason": self.kill_switch_reason}, signal_dict=signal_dict, signal_id=signal_id)
            return False, reason, {}

        # 1b. Drawdown Governor (HARD - never bypass)
        if self._dd_governor is not None:
            dd_allowed, dd_reason = self._dd_governor.is_trading_allowed()
            if not dd_allowed:
                logger.error(f"[ORDER_REJECTED] {symbol} | reason=DRAWDOWN_GOVERNOR | {dd_reason}")
                self._record_exec_attempt(
                    symbol, "PRE_FLIGHT_REJECT", "DRAWDOWN_GOVERNOR",
                    {"dd_reason": dd_reason, **self._dd_governor.get_state()},
                    signal_dict=signal_dict, signal_id=signal_id,
                )
                return False, f"DRAWDOWN_GOVERNOR:{dd_reason}", {}
            # Inject risk multiplier from DD governor into signal
            dd_mult = self._dd_governor.get_risk_multiplier()
            if dd_mult < 1.0:
                prev_mult = signal_dict.get("risk_multiplier", 1.0)
                signal_dict["risk_multiplier"] = prev_mult * dd_mult
                logger.info(
                    f"[DD_GOV] {symbol} | risk_multiplier {prev_mult:.2f} → "
                    f"{signal_dict['risk_multiplier']:.2f} | tier={self._dd_governor._tier.value}"
                )

        # 1c. Strategy Learning Gate (HARD - never bypass)
        if self._strategy_allocator is not None:
            strat_id = signal_dict.get("strategy_id", "")
            regime = signal_dict.get("regime", "*")
            if strat_id and not self._strategy_allocator.is_strategy_allowed(strat_id, regime):
                logger.warning(
                    f"[ORDER_REJECTED] {symbol} | reason=STRATEGY_DISABLED_BY_LEARNING"
                    f" | strategy={strat_id} | regime={regime}"
                )
                self._record_exec_attempt(
                    symbol, "PRE_FLIGHT_REJECT", "STRATEGY_DISABLED_BY_LEARNING",
                    {"strategy": strat_id, "regime": regime},
                    signal_dict=signal_dict, signal_id=signal_id,
                )
                return False, f"STRATEGY_DISABLED_BY_LEARNING:{strat_id}", {}
            # Inject learning risk multiplier
            if strat_id:
                learn_mult = self._strategy_allocator.get_risk_multiplier(strat_id, regime)
                if learn_mult < 1.0:
                    prev_mult = signal_dict.get("risk_multiplier", 1.0)
                    signal_dict["risk_multiplier"] = prev_mult * learn_mult
                    logger.info(
                        f"[LEARNING] {symbol} | strategy={strat_id} | risk_multiplier "
                        f"{prev_mult:.2f} → {signal_dict['risk_multiplier']:.2f}"
                    )

        # 2. Active position (HARD - never bypass)
        if active_trade is not None:
            logger.info(f"[ORDER_REJECTED] {symbol} | reason=ACTIVE_TRADE")
            self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "ACTIVE_TRADE", signal_dict=signal_dict, signal_id=signal_id)
            return False, "ACTIVE_TRADE", {}

        # 2b. Extreme Volatility (HARD - never bypass)
        if self._extreme_vol_enabled and atr_info:
            current_atr = atr_info.get("current_atr", 0)
            avg_atr = atr_info.get("avg_atr", 0)
            if avg_atr > 0 and current_atr / avg_atr > self._extreme_vol_max_ratio:
                ratio = current_atr / avg_atr
                logger.warning(
                    f"[ORDER_REJECTED] {symbol} | reason=EXTREME_VOLATILITY"
                    f" | atr_ratio={ratio:.2f} > max={self._extreme_vol_max_ratio}"
                )
                self._record_exec_attempt(
                    symbol, "PRE_FLIGHT_REJECT", "EXTREME_VOLATILITY",
                    {"atr_ratio": ratio, "max_ratio": self._extreme_vol_max_ratio},
                    signal_dict=signal_dict, signal_id=signal_id,
                )
                return False, "EXTREME_VOLATILITY", {"atr_ratio": ratio}

        # 2c. Correlation Filter (SOFT - adjusts risk_multiplier or blocks)
        if self.correlation_filter and open_symbols is not None:
            corr_mult, corr_reason = self.correlation_filter.check(symbol, open_symbols)
            if corr_mult == 0.0:
                logger.warning(
                    f"[ORDER_REJECTED] {symbol} | reason=CORRELATION_BLOCKED | {corr_reason}"
                )
                self._record_exec_attempt(
                    symbol, "PRE_FLIGHT_REJECT", "CORRELATION_BLOCKED",
                    {"reason": corr_reason},
                    signal_dict=signal_dict, signal_id=signal_id,
                )
                return False, "CORRELATION_BLOCKED", {"reason": corr_reason}
            if corr_mult < 1.0:
                prev_mult = signal_dict.get("risk_multiplier", 1.0)
                signal_dict["risk_multiplier"] = prev_mult * corr_mult
                logger.info(
                    f"[CORRELATION] {symbol} | risk_multiplier {prev_mult:.2f} → "
                    f"{signal_dict['risk_multiplier']:.2f} | {corr_reason}"
                )

        # 3. Idempotency (HARD - never bypass)
        if signal_id and signal_id in self._executed_ids:
            logger.info(f"[ORDER_REJECTED] {symbol} | reason=DUPLICATE_SIGNAL | id={signal_id}")
            self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "DUPLICATE_SIGNAL", signal_dict=signal_dict, signal_id=signal_id)
            return False, "DUPLICATE_SIGNAL", {}

        # 4. Daily trade limit (SOFT - bypassed by FORCE_EXECUTE)
        self._maybe_reset_daily()
        if self._trades_today >= self.max_trades_per_day:
            if self.force_execute:
                logger.warning(f"[FORCE_EXECUTE] {symbol} | BYPASSING MAX_TRADES_PER_DAY ({self._trades_today}/{self.max_trades_per_day})")
            else:
                msg = f"MAX_TRADES_PER_DAY ({self.max_trades_per_day}) reached"
                logger.warning(f"[ORDER_REJECTED] {symbol} | reason={msg}")
                self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "MAX_TRADES_PER_DAY", {"limit": self.max_trades_per_day, "current": self._trades_today}, signal_dict=signal_dict, signal_id=signal_id)
                return False, "MAX_TRADES_PER_DAY", {"limit": self.max_trades_per_day, "current": self._trades_today}

        # 5. Daily loss limit (HARD - never bypass)
        if account_balance > 0 and self.max_daily_loss_pct > 0:
            loss_pct = abs(min(0, self._daily_pnl)) / account_balance * 100
            if loss_pct >= self.max_daily_loss_pct:
                if not self.kill_switch_active:
                    self.engage_kill_switch(f"MAX_DAILY_LOSS_{loss_pct:.1f}%")
                logger.error(f"[ORDER_REJECTED] {symbol} | reason=MAX_DAILY_LOSS | loss={loss_pct:.1f}%")
                self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "MAX_DAILY_LOSS", {"loss_pct": loss_pct, "limit": self.max_daily_loss_pct}, signal_dict=signal_dict, signal_id=signal_id)
                return False, "MAX_DAILY_LOSS", {"loss_pct": loss_pct, "limit": self.max_daily_loss_pct}

        # 6. Cooldown (SOFT - bypassed by FORCE_EXECUTE)
        now = time.time()
        last = self._last_trade_time.get(symbol, 0)
        elapsed = now - last
        if elapsed < self.cooldown_sec:
            if self.force_execute:
                logger.warning(f"[FORCE_EXECUTE] {symbol} | BYPASSING COOLDOWN ({elapsed:.0f}s < {self.cooldown_sec}s)")
            else:
                remaining = self.cooldown_sec - elapsed
                logger.info(f"[ORDER_REJECTED] {symbol} | reason=COOLDOWN_ACTIVE | remaining={remaining:.0f}s")
                self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "COOLDOWN_ACTIVE", {"remaining": remaining}, signal_dict=signal_dict, signal_id=signal_id)
                return False, "COOLDOWN_ACTIVE", {"remaining": remaining}

        # 7. Balance check (HARD - never bypass unless shadow)
        if not self.shadow_mode and account_balance <= 0:
            logger.error(f"[ORDER_REJECTED] {symbol} | reason=ZERO_BALANCE")
            self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "ZERO_BALANCE", signal_dict=signal_dict, signal_id=signal_id)
            return False, "ZERO_BALANCE", {}

        # 8. Check signal validity (prices)
        entry = float(signal_dict.get("entry", 0))
        sl = float(signal_dict.get("stop_loss", 0))
        tp = float(signal_dict.get("take_profit", 0) or signal_dict.get("tp1", 0))
        direction = signal_dict.get("direction", "").lower()

        # 9. TP/SL Logic Check (HARD - never bypass)
        if not self._validate_tpsl_logic(direction, entry, sl, tp):
            reason = f"INVALID_TPSL: {direction} E={entry} SL={sl} TP={tp}"
            logger.warning(f"[ORDER_REJECTED] {symbol} | reason={reason}")
            self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "INVALID_TPSL", {"entry": entry, "sl": sl, "tp": tp}, signal_dict=signal_dict, signal_id=signal_id)
            return False, "INVALID_TPSL", {"entry": entry, "sl": sl, "tp": tp}

        # 10. Risk/Reward Check (SOFT - bypassed by FORCE_EXECUTE)
        if not self._validate_risk_reward(entry, sl, tp):
            rr = self._calculate_rr(entry, sl, tp)
            if self.force_execute:
                logger.warning(f"[FORCE_EXECUTE] {symbol} | BYPASSING LOW_RR ({rr:.2f} < {self.min_rr})")
            else:
                logger.warning(f"[ORDER_REJECTED] {symbol} | reason=LOW_RR | rr={rr:.2f} < min={self.min_rr}")
                self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "LOW_RR", {"rr": rr, "min_rr": self.min_rr}, signal_dict=signal_dict, signal_id=signal_id)
                return False, "LOW_RR", {"rr": rr, "min_rr": self.min_rr}

        # 11. Hard Reject Cooldown Check
        self._cleanup_hard_reject_cache()
        fp = self._compute_fingerprint(symbol, signal_dict)
        last_ts, last_reason = self._hard_reject_cache.get(fp, (0, ""))
        if now - last_ts < self.hard_reject_cooldown_sec:
            logger.warning(f"[ORDER_REJECTED] {symbol} | reason=COOLDOWN_HARD_REJECT | cache_reason={last_reason}")
            self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "COOLDOWN_HARD_REJECT", {"last_reason": last_reason}, signal_dict=signal_dict, signal_id=signal_id)
            return False, "COOLDOWN_HARD_REJECT", {"last_reason": last_reason}

        # 12. Notional Pre-flight block (if quantity is known here)
        if quantity > 0:
            specs = get_contract_specs(symbol)
            notional = abs(quantity * entry)
            min_notional = specs.min_notional
            if notional < min_notional and not self.auto_upsize_to_min_notional:
                # Do NOT cache NOTIONAL_TOO_LOW — it's a sizing issue, not signal spam
                reason_msg = f"NOTIONAL_TOO_LOW {notional:.2f}<{min_notional:.2f}"
                logger.warning(f"[ORDER_REJECTED] {symbol} | reason={reason_msg}")
                self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "NOTIONAL_TOO_LOW", {"notional": notional, "min_notional": min_notional}, signal_dict=signal_dict, signal_id=signal_id)
                return False, "NOTIONAL_TOO_LOW", {"notional": notional, "min_notional": min_notional}

        # All checks passed
        if _governor:
            _governor.record_signal_evaluated(
                strategy=signal_dict.get("strategy_id", "")
            )
        logger.info(
            f"[ORDER] PRE-FLIGHT PASS | {symbol}"
            f" | {direction.upper()} | entry={entry}"
            f" | SHADOW={self.shadow_mode}"
        )
        self._record_exec_attempt(symbol, "PRE_FLIGHT_PASS", signal_dict=signal_dict, signal_id=signal_id)
        return True, "OK", {}

    # ──────────────────────────────────────────────────────
    #  Execute Signal
    # ──────────────────────────────────────────────────────

    async def execute_signal(
        self,
        executor,
        symbol: str,
        signal_dict: Dict,
        quantity: float,
        leverage: int = 10,
        max_qty_deviation_pct: float = None,
        signal_id: Optional[str] = None,
    ) -> Dict:
        # Phase 4.5 Latency Tracking: t_signal
        from smc_futures.engine.analysis.latency_tracker import latency_tracker
        latency_tracker.record_timestamp(signal_id or "manual", "t_signal", metadata={
            "symbol": symbol,
            "side": signal_dict.get("direction", "unknown"),
            "qty": quantity
        })

        # ═══════════════════════════════════════════════════════════════
        # [PHASE 1] STRICT MIRROR OVERRIDE — DISABLED
        # Multi-broker execution is now handled exclusively by
        # execute_multi_broker() in the orchestrator. The old
        # build_app_context() interception created a NEW empty context
        # (no singleton), so kraken_exec was always None = dead code.
        # Keeping the comment for audit trail.
        # ═══════════════════════════════════════════════════════════════

        # Use env-based default if not explicitly provided
        if max_qty_deviation_pct is None:
            max_qty_deviation_pct = float(os.getenv("MAX_RISK_DEVIATION_PCT", "500"))
        direction = signal_dict.get("direction", "long")
        entry = float(signal_dict.get("entry", 0))
        sl = float(signal_dict.get("stop_loss", 0))
        tp = float(signal_dict.get("take_profit", 0))

        # Used for analytics and emergency close persistence.
        # Prefer actual executed order_id, then fallback to signal_id.
        trade_id_for_analytics = None
        
        # ═══════════════════════════════════════════════════════════════
        # MANDATORY SL/TP GATE — Never open a position without SL
        # ═══════════════════════════════════════════════════════════════
        risk_sl_pct = float(os.getenv("RISK_SL_PCT", "1.0"))
        risk_tp_pct = float(os.getenv("RISK_TP_PCT", "2.0"))
        
        if sl <= 0 and entry > 0:
            if direction == "long":
                sl = round(entry * (1 - risk_sl_pct / 100), 8)
            else:
                sl = round(entry * (1 + risk_sl_pct / 100), 8)
            signal_dict["stop_loss"] = sl
            logger.warning(
                "[SL_GUARD] No SL provided — auto-set to %.4f (%.1f%% from entry %.4f)",
                sl, risk_sl_pct, entry,
            )
        
        if tp <= 0 and entry > 0:
            if direction == "long":
                tp = round(entry * (1 + risk_tp_pct / 100), 8)
            else:
                tp = round(entry * (1 - risk_tp_pct / 100), 8)
            signal_dict["take_profit"] = tp
            logger.warning(
                "[TP_GUARD] No TP provided — auto-set to %.4f (%.1f%% from entry %.4f)",
                tp, risk_tp_pct, entry,
            )
        
        if sl <= 0:
            logger.error("[ORDER_REJECTED] %s | reason=NO_STOP_LOSS", symbol)
            return {
                "success": False,
                "error": "ORDER_REJECTED: No stop loss — set RISK_SL_PCT or provide sl in signal",
                "status": "REJECTED_NO_SL",
            }
        
        # ═══════════════════════════════════════════════════════════════
        # QUANTITY VALIDATION (Pre-flight check to avoid 45111 errors)
        # ═══════════════════════════════════════════════════════════════
        specs = get_contract_specs(symbol)
        
        # ── Margin/balance pre-check ──────────────────────────────────
        # Prevent "order amount exceeds the balance" by verifying required
        # margin < available balance BEFORE sending to exchange.
        notional = abs(quantity * entry)
        if self._account_balance > 0 and leverage > 0 and notional > 0:
            required_margin = notional / leverage
            # Use 95% of balance to leave room for fees
            available_margin = self._account_balance * 0.95
            if required_margin > available_margin:
                # Clamp quantity to what the balance can actually support
                max_notional = available_margin * leverage
                max_qty = max_notional / entry if entry > 0 else 0
                clamped_qty = specs.normalize_quantity(max_qty, entry)[0] if max_qty > 0 else None
                if clamped_qty and clamped_qty > 0:
                    logger.warning(
                        f"[MARGIN_GUARD] {symbol} | required_margin={required_margin:.2f}"
                        f" > available={available_margin:.2f}"
                        f" | Clamping qty {quantity:.6f} -> {clamped_qty:.6f}"
                    )
                    quantity = clamped_qty
                    notional = abs(quantity * entry)
                else:
                    logger.error(
                        f"[ORDER_REJECTED] {symbol} | INSUFFICIENT_MARGIN"
                        f" | required={required_margin:.2f} > available={available_margin:.2f}"
                        f" | Cannot clamp to valid qty"
                    )
                    self._record_exec_attempt(
                        symbol, "PRE_FLIGHT_REJECT", "INSUFFICIENT_MARGIN",
                        {"required": required_margin, "available": available_margin},
                        signal_dict=signal_dict, signal_id=signal_id,
                    )
                    return {
                        "success": False,
                        "error": f"ORDER_REJECTED: Insufficient margin ({required_margin:.2f} > {available_margin:.2f})",
                        "status": "REJECTED_INSUFFICIENT_MARGIN",
                    }
        
        # Enforce NOTIONAL blocks explicitly inside execute_signal as well
        if notional > 0 and notional < specs.min_notional and not self.auto_upsize_to_min_notional:
            # Do NOT cache NOTIONAL_TOO_LOW — sizing issue, not signal spam
            
            error_msg = f"NOTIONAL_TOO_LOW {notional:.2f}<{specs.min_notional:.2f}"
            logger.error(f"[ORDER_REJECTED_PRE_FLIGHT] {symbol} | {error_msg}")
            self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "NOTIONAL_TOO_LOW", {"notional": notional, "min": specs.min_notional}, signal_dict=signal_dict, signal_id=signal_id)
            return {
                "success": False,
                "error": f"ORDER_REJECTED_PRE_FLIGHT: {error_msg}",
                "status": "SKIPPED_NOTIONAL_TOO_LOW",
                "qty_details": {"notional": notional, "min": specs.min_notional}
            }

        normalized_qty, qty_status, qty_details = specs.normalize_quantity(
            raw_qty=quantity,
            entry_price=entry,
            max_risk_deviation_pct=max_qty_deviation_pct
        )
        
        if normalized_qty is None:
            # Quantity validation failed - reject order before sending
            rejection_reason = qty_details.get("rejection_reason", "QTY_VALIDATION_FAILED")
            status = qty_details.get("status", "REJECTED")
            suggested_balance = qty_details.get("suggested_min_balance", "N/A")
            
            # Do NOT cache notional/sizing rejections — they resolve when balance changes
            # Only cache true structural rejections (e.g. exceeds max position cap)
            if "exceeds" in rejection_reason.lower():
                fp = self._compute_fingerprint(symbol, signal_dict)
                self._hard_reject_cache[fp] = (time.time(), rejection_reason)
            
            # Single consolidated error log with actionable info
            logger.error(
                f"[ORDER_REJECTED_PRE_FLIGHT] {symbol} | "
                f"raw_qty={quantity:.6f} | min_qty={specs.min_qty} | min_notional=${specs.min_notional} | "
                f"reason={rejection_reason} | suggested_min_balance=${suggested_balance}"
            )
            self._record_exec_attempt(symbol, "PRE_FLIGHT_REJECT", "QTY_VALIDATION_FAILED", {"reason": rejection_reason, "qty_details": qty_details}, signal_dict=signal_dict, signal_id=signal_id)
            return {
                "success": False,
                "error": f"ORDER_REJECTED_PRE_FLIGHT: {rejection_reason}",
                "status": status,
                "qty_details": qty_details,
                "suggested_action": f"Increase balance to ${suggested_balance} or reduce position risk",
            }
        
        # Use normalized quantity
        if qty_status == "CLAMPED":
            logger.info(
                f"[QTY_ADJUSTED] {symbol} | {quantity:.6f} -> {normalized_qty:.6f}"
                f" | status={qty_status}"
            )
        quantity = normalized_qty
        
        # ═══════════════════════════════════════════════════════════════
        
        # 1. Determine Order Parameters
        final_order_type = self.order_type
        price = None
        
        if final_order_type == "limit":
            # entry ± offset
            bps = self.limit_offset_bps / 10000.0
            if direction == "long":
                price = entry * (1.0 - bps)
            else:
                price = entry * (1.0 + bps)
        
        # 2. Strict Log [ORDER_SENT]
        price_log = f"price={price:.4f}" if price else "MARKET"
        mode_log = "[SHADOW]" if self.shadow_mode else "[LIVE]"
        
        logger.info(
            f"[ORDER_SENT] {mode_log} | {symbol} {direction.upper()} {final_order_type.upper()}"
            f" | qty={quantity} | {price_log}"
            f" | sl={sl} | tp={tp} | lev={leverage}x"
        )

        try:
            # 3. Call Executor (Native)
            # Ensure executor knows about Shadow Mode
            # If shadow mode, we can simulate here OR rely on executor's shadow logic if it has it.
            # Best to handle Shadow logic HERE in Manager to be executor-agnostic
            
            if self.shadow_mode:
                 # Simulate execution
                 await asyncio.sleep(0.1) # mimic latency
                 import uuid
                 dummy_id = f"DRYRUN-{uuid.uuid4().hex[:8]}"
                 logger.info(f"[DRY_RUN] WOULD_SEND (Shadow) | {symbol} {direction} qty={quantity} sl={sl} tp={tp}")
                 
                 return {
                    "success": True,
                    "entry_order_id": dummy_id,
                    "quantity": quantity,
                    "price_executed": price or entry,
                    "timestamp": datetime.now().isoformat(),
                    "mode": "shadow"
                }

            # LIVE EXECUTION
            if not executor:
                return {"success": False, "error": "No executor provided for LIVE trade"}

            if getattr(executor, "broker_name", None) != "kraken":
                return {
                    "success": False,
                    "error": "LIVE_ENTRY_REJECTED: only Kraken execution is permitted",
                }

            # Market data provenance: live Kraken WS v1 L2 book feed verification is enforced
            # strictly inside KrakenFuturesExecutorWrapper.place_order() (Phase 1.9.7/1.9.8).
            execution_proof = None
            try:
                from backend.market_data_provenance import issue_execution_proof
                execution_proof = issue_execution_proof(
                    signal_dict.get("market_data_proofs", []),
                    "kraken",
                    symbol,
                    direction,
                    quantity,
                    price or entry,
                    sl,
                    tp,
                    market_reference_price=entry,
                )
                if not execution_proof:
                    return {
                        "success": False,
                        "error": "LIVE_ENTRY_REJECTED: verified Kraken market data is required",
                    }
            except ModuleNotFoundError:
                execution_proof = None

            # Step A: Set Leverage
            # Note: Executor must have set_leverage
            if hasattr(executor, "set_leverage"):
                lev_res = await executor.set_leverage(symbol, leverage)
                
                # Check if leverage set failed
                if lev_res.get("code") != "00000":
                     logger.error(f"[EXEC] LEVERAGE FAIL | {symbol} | {lev_res.get('msg')}")
                     return {"success": False, "error": f"Failed to set leverage: {lev_res.get('msg')}"}
                
                # Use ACTUAL applied leverage (in case of clamp)
                applied_leverage = int(lev_res.get("leverage", leverage))
                if applied_leverage != leverage:
                     logger.warning(f"[EXEC] LEVERAGE CLAMPED | Req={leverage} -> Applied={applied_leverage}")
                     leverage = applied_leverage
            
            # Step B: Place Order
            # Use high-level place_order interface of BitgetV2Executor
            # Signature: place_order(symbol, direction, entry_price, stop_loss, take_profit, order_type, leverage)

            pending_entry = {
                "symbol": symbol,
                "side": direction,
                "notional_usd": abs(quantity * (price or entry)),
                "broker": getattr(self, "broker", None),
            }
            self.pending_orders.append(pending_entry)

            try:
                if hasattr(executor, "place_order") and not hasattr(executor, "attach_tpsl"):
                    # Legacy executor signature
                    result = await executor.place_order(
                        symbol=symbol,
                        direction=direction,
                        entry_price=entry,
                        stop_loss=sl,
                        take_profit=tp,
                        order_type=final_order_type,
                        leverage=leverage,
                        signal_id=signal_id,
                        execution_proof=execution_proof,
                        market_reference_price=entry,
                    )
                else:
                    # Standardized V2 executor
                    result = await executor.place_order(
                        symbol=symbol,
                        direction=direction,
                        entry_price=price or entry,
                        stop_loss=sl,
                        take_profit=tp,
                        order_type=final_order_type,
                        leverage=leverage,
                        quantity=str(quantity), # Pass strict string
                        signal_id=signal_id,
                        execution_proof=execution_proof,
                        market_reference_price=entry,
                    )
            finally:
                try:
                    self.pending_orders.remove(pending_entry)
                except ValueError:
                    pass

            
            # 4. Handle Result
            if result.get("success"):
                # Record order_id/signal_id for analytics and emergency close.
                trade_id_for_analytics = result.get("order_id") or signal_id

                # Ensure SL is attached (important for safety). If executor
                # indicates SL was not attached, treat as failure.
                if result.get("sl_attached") is False:
                    logger.critical(
                        f"[EXEC] FAILURE | {symbol} | SL NOT ATTACHED | closing position"
                    )

                    emergency_close_result = None
                    try:
                        if executor and hasattr(executor, "close_position"):
                            emergency_close_result = await executor.close_position(symbol)
                        elif executor and hasattr(executor, "close_position_market"):
                            close_dir = "sell" if direction == "long" else "buy"
                            emergency_close_result = await executor.close_position_market(
                                symbol,
                                close_dir,
                                str(quantity),
                            )
                    except Exception as e:
                        logger.critical(f"[EXEC] Emergency close failed: {e}")

                    self._orphan_positions.add(symbol)
                    logger.critical(
                        "[EXEC] Emergency close outcome is unconfirmed; preserving "
                        "position as orphaned | %s | open_order_id=%s",
                        symbol,
                        result.get("order_id"),
                    )

                    # Record the failure for analytics / auditing
                    self._record_exec_attempt(
                        symbol,
                        "EXECUTED",
                        signal_dict=signal_dict,
                        signal_id=signal_id,
                        order_id=str(result.get("order_id")) if result.get("order_id") else None,
                        fill_price=result.get("fill_price"),
                        block_reason="NO_SL_ATTACHED",
                    )

                    return {
                        "success": False,
                        "error": "NO_SL_ATTACHED",
                        "order_id": result.get("order_id"),
                        "sl_attached": False,
                        "position_state": "unknown",
                        "emergency_close": emergency_close_result,
                    }

                order_id = result.get("order_id")
                fill_price = result.get("price_executed") or result.get("fill_price")
                is_kraken = getattr(executor, "broker_name", None) == "kraken"
                if is_kraken:
                    filled_qty = result.get("filled_qty")
                    try:
                        filled_qty = float(filled_qty)
                    except (TypeError, ValueError, OverflowError):
                        filled_qty = 0.0
                    if (
                        result.get("fill_quantity_verified") is not True
                        or result.get("qty_unit") != "contracts"
                        or not math.isfinite(filled_qty)
                        or filled_qty <= 0
                    ):
                        self._orphan_positions.add(symbol)
                        return {
                            "success": False,
                            "error": "KRAKEN_FILL_QUANTITY_UNVERIFIED",
                            "order_id": order_id,
                        }
                    quantity = filled_qty
                    try:
                        fill_price = float(fill_price)
                    except (TypeError, ValueError, OverflowError):
                        fill_price = 0.0
                    if (
                        result.get("fill_price_verified") is not True
                        or not math.isfinite(fill_price)
                        or fill_price <= 0
                    ):
                        self._orphan_positions.add(symbol)
                        return {
                            "success": False,
                            "error": "KRAKEN_FILL_PRICE_UNVERIFIED",
                            "order_id": order_id,
                            "filled_qty": filled_qty,
                            "qty_unit": "contracts",
                        }
                else:
                    quantity = float(result.get("quantity", quantity) or quantity)
                
                # 4b. Slippage Detection (post-fill — informational + tracking)
                if fill_price and entry > 0:
                    slippage_bps = abs(fill_price - entry) / entry * 10000
                    atr_val = float(signal_dict.get("atr", 0))
                    slippage_atr = abs(fill_price - entry) / atr_val if atr_val > 0 else 0
                    exceeded = (
                        slippage_bps > self._max_slippage_bps
                        or (atr_val > 0 and slippage_atr > self._max_slippage_atr)
                    )
                    if exceeded:
                        logger.warning(
                            f"[SLIPPAGE] {symbol} | fill={fill_price:.6f} vs entry={entry:.6f}"
                            f" | slip_bps={slippage_bps:.1f} (max={self._max_slippage_bps})"
                            f" | slip_atr={slippage_atr:.2f} (max={self._max_slippage_atr})"
                        )
                    result["slippage_bps"] = round(slippage_bps, 2)
                    result["slippage_exceeded"] = exceeded

                    # 4c. Slippage Kill Switch — emergency close if > threshold
                    if self._slippage_kill_bps > 0 and slippage_bps > self._slippage_kill_bps:
                        logger.critical(
                            f"[SLIPPAGE_KILL] {symbol} | slip={slippage_bps:.1f}bps > kill_threshold={self._slippage_kill_bps:.0f}bps"
                            f" — EMERGENCY CLOSE"
                        )
                        result["slippage_kill"] = True
                        # Engage cooldown to prevent re-trade loop
                        self._slippage_kill_until = time.time() + self._slippage_kill_cooldown_sec
                        logger.critical(
                            f"[SLIPPAGE_KILL] Trading paused for {self._slippage_kill_cooldown_sec}s"
                        )
                        # Attempt emergency close with retry
                        close_dir = "sell" if direction == "long" else "buy"
                        close_ok = await self._emergency_close_with_retry(
                            executor=executor,
                            symbol=symbol,
                            direction=close_dir,
                            quantity=quantity,
                            signal_id=signal_id,
                        )
                        if not close_ok:
                            self._orphan_positions.add(symbol)
                        # Phase 6: Record emergency close in governor
                        if _governor:
                            _governor.record_emergency_close(
                                symbol=symbol,
                                attempts=self._emergency_max_retries,
                                success=close_ok,
                            )
                        return {
                            "success": False,
                            "error": f"SLIPPAGE_KILL: {slippage_bps:.1f}bps exceeded {self._slippage_kill_bps:.0f}bps threshold",
                            "slippage_bps": round(slippage_bps, 2),
                            "slippage_kill": True,
                            "emergency_close_ok": close_ok,
                        }
                
                logger.info(
                    f"[EXEC] FILLED {mode_log} | {symbol} {direction.upper()}"
                    f" | orderId={order_id}"
                    f" | price={fill_price}"
                )

                # Phase 6: Record fill in governor
                if _governor:
                    t_sig = signal_dict.get("t_signal", 0)
                    fill_delay = (time.time() - t_sig) * 1000 if t_sig > 0 else 0.0
                    _governor.record_fill(
                        symbol=symbol,
                        strategy=signal_dict.get("strategy_id", ""),
                        direction=direction,
                        entry=entry,
                        fill_price=fill_price,
                        slippage_bps=result.get("slippage_bps", 0.0),
                        fill_delay_ms=fill_delay,
                        regime=signal_dict.get("regime", ""),
                    )

                return {
                    "success": True,
                    "entry_order_id": order_id,
                    "quantity": quantity,
                    "filled_qty": result.get("filled_qty", quantity),
                    "qty_unit": result.get("qty_unit"),
                    "fill_quantity_verified": result.get("fill_quantity_verified") is True,
                    "fill_price_verified": result.get("fill_price_verified") is True,
                    "price_executed": fill_price,
                    "broker": "kraken",
                    "timestamp": datetime.now().isoformat(),
                    "mode": "live",
                    "sl_attached": result.get("sl_attached", False),
                    "tp_attached": result.get("tp_attached", False),
                }
            else:
                # API Error
                error_msg = result.get("error", "Unknown API Error")
                logger.error(f"[EXEC] ERROR | {symbol} | {error_msg}")
                self._record_exec_attempt(
                    symbol,
                    "EXEC_FAILED",
                    block_reason=str(error_msg)[:120],
                    block_details={
                        "error": str(error_msg),
                        "broker": getattr(executor, "broker_name", None),
                        "quantity": quantity,
                    },
                    signal_dict=signal_dict,
                    signal_id=signal_id,
                )
                return {"success": False, "error": error_msg}

        except Exception as e:
            # Exception
            logger.error(f"[EXEC] EXCEPTION | {symbol} | {str(e)}", exc_info=True)
            self._record_exec_attempt(
                symbol,
                "EXEC_FAILED",
                block_reason=str(e)[:120],
                block_details={
                    "error": str(e),
                    "broker": getattr(executor, "broker_name", None),
                    "quantity": quantity,
                },
                signal_dict=signal_dict,
                signal_id=signal_id,
            )
            return {"success": False, "error": str(e)}

    # ──────────────────────────────────────────────────────
    #  Strict Mirror OMS (Phase 1)
    # ──────────────────────────────────────────────────────

    async def _execute_strict_mirror(
        self,
        executor_bg,
        executor_kr,
        symbol: str,
        signal_dict: Dict,
        quantity: float,
        leverage: int = 10,
        signal_id: Optional[str] = None,
    ) -> Dict:
        """
        Executes an order on both Bitget and Kraken concurrently.
        Enforces STRICT_MIRROR logic: If one fails, Rollback the other.
        """
        if not self.shadow_mode:
            return {
                "success": False,
                "error": "LIVE_ENTRY_REJECTED: strict multi-broker entries are disabled",
            }

        direction = signal_dict.get("direction", "long")
        entry = float(signal_dict.get("entry", 0))
        sl = float(signal_dict.get("stop_loss", 0))
        tp = float(signal_dict.get("take_profit", 0))
        final_order_type = self.order_type

        # Use kraken specific symbol
        kr_symbol = to_kraken_symbol(symbol)

        logger.info(f"[STRICT_MIRROR] Dispatching to BG({symbol}) and KR({kr_symbol}) | qty={quantity}")

        async def _exec_bg():
            if self.shadow_mode:
                await asyncio.sleep(0.1)
                return {"success": True, "order_id": f"SHADOW-BG-{int(time.time())}"}
            
            if hasattr(executor_bg, "set_leverage"):
                await executor_bg.set_leverage(symbol, leverage)
            return await executor_bg.place_order(symbol, direction, entry, sl, tp, final_order_type, leverage, str(quantity), signal_id)

        async def _exec_kr():
            if self.shadow_mode:
                await asyncio.sleep(0.12)
                return {"success": True, "order_id": f"SHADOW-KR-{int(time.time())}"}
            
            if hasattr(executor_kr, "set_leverage"):
                await executor_kr.set_leverage(kr_symbol, leverage)
            return await executor_kr.place_order(kr_symbol, direction, entry, sl, tp, final_order_type, leverage, str(quantity), signal_id)

        # 1. Concurrent Dispatch with 5s timeout
        try:
            bg_res, kr_res = await asyncio.wait_for(
                asyncio.gather(_exec_bg(), _exec_kr(), return_exceptions=True),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            logger.error("[STRICT_MIRROR] GLOBAL 5s TIMEOUT EXCEEDED")
            bg_res = Exception("Global Timeout")
            kr_res = Exception("Global Timeout")

        bg_success = isinstance(bg_res, dict) and bg_res.get("success", False)
        kr_success = isinstance(kr_res, dict) and kr_res.get("success", False)

        # CASE A: Both Succeeded
        if bg_success and kr_success:
            logger.info(f"[STRICT_MIRROR] FULL SUCCESS | BG_ID={bg_res.get('entry_order_id', bg_res.get('order_id'))} KR_ID={kr_res.get('entry_order_id', kr_res.get('order_id'))}")
            # We return Bitget's dict to make standard persist_trade hook happy
            bg_res["kr_order_id"] = kr_res.get("entry_order_id", kr_res.get("order_id"))
            
            # Hotfix: Ensure `entry_order_id` is defined to satisfy `OrderManager.execute` hook.
            if "entry_order_id" not in bg_res:
                bg_res["entry_order_id"] = bg_res.get("order_id", f"MOCK-{int(time.time())}")
            if "price_executed" not in bg_res:
                bg_res["price_executed"] = entry

            return bg_res

        # CASE B: Partial Success -> ROLLBACK REQUIRED
        logger.critical(f"CRITICAL: STRICT MIRROR ROLLBACK INITIATED")
        
        # Disable auto-execute immediately
        import smc_futures.smc_config as live_config
        live_config.AUTO_EXECUTE_ENABLED = False
        self.auto_execute_enabled = False
        self.engage_kill_switch("STRICT MIRROR PARTIAL FAIL")
        
        rollback_side = "sell" if direction.lower() == "long" else "buy"
        
        # Determine who to rollback
        if bg_success and not kr_success:
            logger.error(f"[STRICT_MIRROR] Kraken Failed ({kr_res}). Rolling back Bitget...")
            if not self.shadow_mode:
                try:
                    await executor_bg.place_order(symbol, rollback_side, entry, 0, 0, "market", leverage, str(quantity))
                except Exception as e:
                    logger.critical(f"BITGET ROLLBACK FAILED: {e}")
        elif kr_success and not bg_success:
            logger.error(f"[STRICT_MIRROR] Bitget Failed ({bg_res}). Rolling back Kraken...")
            if not self.shadow_mode:
                try:
                    await executor_kr.place_order(kr_symbol, rollback_side, entry, 0, 0, "market", leverage, str(quantity))
                except Exception as e:
                    logger.critical(f"KRAKEN ROLLBACK FAILED: {e}")
        else:
            logger.error(f"[STRICT_MIRROR] BOTH FAILED. BG={bg_res}, KR={kr_res}")

        return {"success": False, "error": "STRICT_MIRROR_PARTIAL_FAIL"}

    # ──────────────────────────────────────────────────────
    #  Post-Trade Bookkeeping
    # ──────────────────────────────────────────────────────

    def record_fill(
        self,
        symbol: str,
        signal_dict: Dict,
        exec_result: Dict,
        signal_id: str = "",
    ) -> None:
        """Update counters after a successful fill."""
        now = time.time()
        self._trades_today += 1
        self._per_symbol_trades[symbol] += 1
        self._last_trade_time[symbol] = now
        self._open_position_symbols.add(symbol)
        qty = float(exec_result.get("quantity", 0) or 0)
        if qty > 0:
            self._position_sizes[symbol] = qty
            qty_unit = exec_result.get("qty_unit")
            if qty_unit:
                self._position_size_units[symbol] = str(qty_unit)

        if signal_id:
            self._executed_ids.add(signal_id)

        fill_record = {
            "symbol": symbol,
            "direction": signal_dict.get("direction"),
            "entry": signal_dict.get("entry"),
            "qty": exec_result.get("quantity"),
            "order_id": exec_result.get("entry_order_id"),
            "ts": exec_result.get("timestamp"),
            "mode": exec_result.get("mode", "live"),
            "day_count": self._trades_today,
        }
        self._exec_log.append(fill_record)

        # Cap log size
        if len(self._exec_log) > 200:
            self._exec_log = self._exec_log[-100:]

        logger.info(
            f"[ORDER] BOOKED | {symbol}"
            f" | trades_today={self._trades_today}/{self.max_trades_per_day}"
        )

        # Publish TRADE_OPENED event (unconditional)
        if _phase6_wiring_available:
            try:
                from core.events.event_types import TradeEvent, EventType
                _p6_publish(TradeEvent(
                    EventType.TRADE_OPENED,
                    symbol=symbol,
                    broker=exec_result.get("broker", "bitget"),
                    side=signal_dict.get("direction", ""),
                    price=float(exec_result.get("price_executed", 0) or 0),
                    qty=float(exec_result.get("quantity", 0) or 0),
                    order_id=exec_result.get("entry_order_id", ""),
                ))
            except Exception:
                pass

    def record_rejection(self, symbol: str, reason: str, details: Dict = None) -> None:
        """Log a rejected execution attempt."""
        self._reject_log.append({
            "symbol": symbol,
            "reason": reason,
            "details": details or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if len(self._reject_log) > 200:
            self._reject_log = self._reject_log[-100:]

    # ──────────────────────────────────────────────────────
    #  Public Execute Hook (ATOMIC: pre_flight + execute)
    # ──────────────────────────────────────────────────────

    async def execute(
        self,
        symbol: str,
        signal_dict: Dict,
        signal_id: str,
        executor,
        active_trade: Optional[Dict],
        account_balance: float,
        quantity: float,
        leverage: int = 10,
    ) -> Dict[str, Any]:
        """
        One-shot execution hook called by orchestrator.
        
        Returns:
            {
                "success": bool,
                "reason": str,           # "OK" | "KILL_SWITCH" | "COOLDOWN" | etc.
                "details": dict,         # Additional context
                "entry_order_id": str,   # If success
                "price_executed": float, # If success
            }
        """
        direction = signal_dict.get("direction", "").upper()
        entry = signal_dict.get("entry", 0)
        
        logger.info(
            f"[AUTO_EXEC] HOOK TRIGGERED | {symbol} {direction}"
            f" | entry={entry} | qty={quantity} | signal_id={signal_id}"
        )

        # 1. Pre-Flight Guards
        ok, reason, details = self.pre_flight_checks(
            symbol=symbol,
            signal_dict=signal_dict,
            signal_id=signal_id,
            active_trade=active_trade,
            account_balance=account_balance,
            quantity=quantity,
            open_symbols=self._open_position_symbols,
            atr_info=signal_dict.get("atr_info"),
        )

        if not ok:
            logger.warning(
                f"[AUTO_EXEC] PRE-FLIGHT REJECT | {symbol} | reason={reason}"
            )
            self.record_rejection(symbol, reason, details)
            return {
                "success": False,
                "reason": reason,
                "details": details,
            }

        # 2. Phase 7: Adaptive Risk Engine
        adaptive_leverage = leverage
        adaptive_size_mult = 1.0
        if _adaptive_risk_available and _adaptive_risk and _adaptive_risk.cfg.enabled:
            try:
                risk_inputs = _build_risk_inputs(
                    signal_dict=signal_dict,
                    base_risk_pct=self.risk_per_trade_pct,
                    base_leverage=leverage,
                    governor=_governor,
                )
                are_decision = _adaptive_risk.evaluate(risk_inputs)

                if are_decision.mode == "blocked":
                    block_reason = f"ADAPTIVE_RISK_BLOCKED:{','.join(are_decision.reasons)}"
                    self._record_exec_attempt(
                        symbol, "PRE_FLIGHT_REJECT", block_reason,
                        signal_dict=signal_dict, signal_id=signal_id,
                    )
                    if _governor:
                        _governor.record_adaptive_risk_decision(
                            mode="blocked",
                            risk_multiplier=0.0,
                            effective_leverage=0,
                            effective_risk_pct=0.0,
                            reasons=are_decision.reasons,
                            symbol=symbol,
                        )
                    return {
                        "success": False,
                        "reason": block_reason,
                        "details": are_decision.to_dict(),
                    }

                adaptive_size_mult = are_decision.size_multiplier
                adaptive_leverage = are_decision.effective_leverage
                signal_dict["adaptive_risk_metadata"] = are_decision.to_dict()
                signal_dict["effective_risk_pct"] = are_decision.effective_risk_pct
                signal_dict["effective_leverage"] = are_decision.effective_leverage

                if _governor:
                    _governor.record_adaptive_risk_decision(
                        mode=are_decision.mode,
                        risk_multiplier=are_decision.risk_multiplier,
                        effective_leverage=are_decision.effective_leverage,
                        effective_risk_pct=are_decision.effective_risk_pct,
                        reasons=are_decision.reasons,
                        symbol=symbol,
                    )
            except Exception as e:
                logger.warning(f"[ADAPTIVE_RISK] Evaluation error (fallback normal): {e}")

        # 3. Apply Risk Multiplier (Phase 5 + Phase 7)
        multiplier = signal_dict.get("risk_multiplier", 1.0) * adaptive_size_mult
        adjusted_qty = quantity * multiplier
        
        # Phase 4.5 Latency Tracking
        intent_id = signal_id or uuid.uuid4().hex[:8]
        t_signal = signal_dict.get("t_signal", time.time())
        self.latency_tracker.record_timestamp(intent_id, "t_signal", t_signal)
        
        if multiplier != 1.0:
            logger.info(f"[CAPITAL] Applying Risk Multiplier: {multiplier:.2f} | {quantity:.6f} -> {adjusted_qty:.6f}")

        # 4. Execute (Shadow or Live)
        self.latency_tracker.record_timestamp(intent_id, "t_order_send")
        exec_result = await self.execute_signal(
            executor=executor,
            symbol=symbol,
            signal_dict=signal_dict,
            quantity=adjusted_qty,
            leverage=adaptive_leverage,
            signal_id=signal_id,
        )
        self.latency_tracker.record_timestamp(intent_id, "t_exchange_ack")
        
        # For simple execution, we record WS/Fill immediately after ack if success 
        # (In reality, they would come from WS events, but this is a simplified fallback for non-OMS path)
        if exec_result.get("success"):
            self.latency_tracker.record_timestamp(intent_id, "t_ws_order_update_first")
            self.latency_tracker.record_timestamp(intent_id, "t_fill")

        if exec_result.get("success"):
            # Sanity check: ensure broker-side SL attachment succeeded.
            # Conservative default: if executor doesn't report `sl_attached`, treat as missing.
            if exec_result.get("sl_attached") is False:
                logger.critical(
                    f"[AUTO_EXEC] FAILURE | {symbol} | SL NOT ATTACHED | Emergency close attempted"
                )
                emergency_close_result = None
                try:
                    if executor and hasattr(executor, "close_position"):
                        emergency_close_result = await executor.close_position(symbol)
                    elif executor and hasattr(executor, "close_position_market"):
                        close_dir = "sell" if signal_dict.get("direction") == "long" else "buy"
                        emergency_close_result = await executor.close_position_market(
                            symbol,
                            close_dir,
                            str(adjusted_qty),
                        )
                except Exception as e:
                    logger.critical(f"[AUTO_EXEC] Emergency close failed: {e}")

                self._orphan_positions.add(symbol)
                logger.critical(
                    "[AUTO_EXEC] Emergency close outcome is unconfirmed; skipping "
                    "fill bookkeeping and preserving position as orphaned | %s | open_order_id=%s",
                    symbol,
                    exec_result.get("entry_order_id"),
                )
                return {
                    "success": False,
                    "error": "NO_SL_ATTACHED",
                    "entry_order_id": exec_result.get("entry_order_id"),
                    "position_state": "unknown",
                    "emergency_close": emergency_close_result,
                }

            # 3. Bookkeeping
            self.record_fill(symbol, signal_dict, exec_result, signal_id)
            
            mode = "SHADOW" if self.shadow_mode else "LIVE"
            order_id = exec_result.get("entry_order_id")
            
            # Record successful execution in analytics DB
            self._record_exec_attempt(
                symbol, "EXECUTED",
                signal_dict=signal_dict, signal_id=signal_id,
                order_id=str(order_id) if order_id else None,
                fill_price=exec_result.get('price_executed'),
            )

            # Use trade_id for analytics when available
            trade_id_for_analytics = signal_id or (str(order_id) if order_id else None)
            # 4. PERSIST TO SQLITE + BROADCAST (LIVE ONLY)
            # ════════════════════════════════════════════════════════════
            if not self.shadow_mode and PERSISTENCE_AVAILABLE:
                try:
                    entry_price = float(exec_result.get("price_executed") or 0)
                    if entry_price <= 0:
                        logger.critical(
                            "[PERSISTENCE] Fill price missing; refusing estimated entry price | symbol=%s",
                            symbol,
                        )
                        self._orphan_positions.add(symbol)
                        raise ValueError("KRAKEN_FILL_PRICE_UNVERIFIED")
                    stop_loss = float(signal_dict.get("stop_loss", 0))
                    take_profit = float(signal_dict.get("take_profit", 0))
                    
                    persisted_trade = await on_trade_opened(
                        order_id=str(order_id),
                        symbol=symbol,
                        side=direction.lower(),
                        qty=float(exec_result.get("quantity", 0)),
                        entry=entry_price,
                        stop=stop_loss,
                        tp=take_profit,
                        leverage=leverage,
                        session_name=signal_dict.get("session"),
                        preset=signal_dict.get("preset"),
                        signal_id=signal_id,
                        raw_exchange=exec_result,
                        broker="kraken" if getattr(executor, "broker_name", None) == "kraken" else "bitget",
                    )
                    trade_id_for_analytics = None
                    if persisted_trade:
                        trade_id_for_analytics = persisted_trade.id
                        logger.info(
                            f"[PERSISTENCE] Trade persisted | id={persisted_trade.id}"
                            f" | order_id={order_id}"
                        )
                except Exception as e:
                    logger.error(f"[PERSISTENCE] FAILED to persist trade: {e}", exc_info=True)
                    # Continue anyway - trade was executed on exchange

            # Write analytics entry AFTER persistence (so we can use DB trade id)
            if _analytics_available and _AR:
                try:
                    _AR.record_trade_analytics(
                        trade_id=trade_id_for_analytics or "",
                        symbol=symbol,
                        side=direction,
                        entry_price=float(signal_dict.get("entry", 0)),
                        size=exec_result.get("quantity"),
                        sl_price=float(signal_dict.get("stop_loss", 0)) or None,
                        tp_price=float(signal_dict.get("take_profit", 0)) or None,
                        execution_mode=mode.lower(),
                        broker=exec_result.get("broker", "bitget"),
                        regime_at_entry=signal_dict.get("regime"),
                        strategy=signal_dict.get("setup", "smc"),
                        edge_score_at_entry=signal_dict.get("confidence"),
                    )
                except Exception:
                    pass  # Never let analytics break trading

            return {
                "success": True,
                "reason": "OK",
                "details": {},
                "entry_order_id": exec_result.get("entry_order_id"),
                "price_executed": exec_result.get("price_executed"),
                "quantity": exec_result.get("quantity"),
                "mode": exec_result.get("mode", mode.lower()),
                "trade_id": trade_id_for_analytics,
            }
        else:
            # Execution failed
            error_msg = exec_result.get("error", "Unknown Error")
            logger.error(f"[AUTO_EXEC] FAILED | {symbol} | {error_msg}")
            return {
                "success": False,
                "reason": "EXECUTION_ERROR",
                "details": {"error": error_msg},
            }

    # ──────────────────────────────────────────────────────
    #  Multi-Broker Execution (Mirror Trading)
    # ──────────────────────────────────────────────────────

    async def execute_multi_broker(
        self,
        symbol: str,
        signal_dict: Dict,
        signal_id: str,
        executors: Dict[str, Any],
        active_trade: Optional[Dict],
        account_balance: float,
        quantity: float,
        leverage: int = 10,
        mode: str = "mirror",
    ) -> MultiBrokerResult:
        """
        Execute on multiple brokers with configurable mode.
        
        Modes:
            - 'mirror':   Execute on ALL brokers concurrently (same qty)
            - 'fallback': Execute on primary first, secondary only if primary fails
            - 'split':    Execute on ALL brokers concurrently, qty divided equally
        
        Args:
            executors: Dict mapping broker name to executor instance.
            mode: Execution mode ('mirror', 'fallback', 'split')
        
        Returns:
            MultiBrokerResult with per-broker results and overall status.
        """
        direction = signal_dict.get("direction", "").upper()
        entry = signal_dict.get("entry", 0)
        
        # Generate unique signal_id if not provided
        if not signal_id:
            signal_id = f"multi_{uuid.uuid4().hex[:12]}"

        # Used for analytics and emergency close persistence
        trade_id_for_analytics = signal_id
        
        logger.info(
            f"[MULTI_EXEC] ═══════════════════════════════════════════════════════"
        )
        logger.info(
            f"[MULTI_EXEC] DISPATCH STARTED | {symbol} {direction}"
            f" | mode={mode.upper()} | entry={entry} | qty={quantity}"
            f" | signal_id={signal_id} | brokers={list(executors.keys())}"
        )
        logger.warning(
            f"[MULTI_EXEC] DISPATCHING | mode={mode.upper()} | brokers={list(executors.keys())} "
            f"| balance={account_balance} | qty={quantity} | active_trade={'YES' if active_trade else 'NO'}"
        )
        self._record_exec_attempt(symbol, "DISPATCH_STARTED", signal_dict=signal_dict, signal_id=signal_id, is_multi_broker=True)

        # 1. Pre-Flight Guards (shared for all brokers)
        ok, reason, details = self.pre_flight_checks(
            symbol=symbol,
            signal_dict=signal_dict,
            signal_id=signal_id,
            active_trade=active_trade,
            account_balance=account_balance,
            quantity=quantity,
            open_symbols=self._open_position_symbols,
            atr_info=signal_dict.get("atr_info"),
        )

        if not ok:
            logger.warning(
                f"[MULTI_EXEC] PRE-FLIGHT REJECT | {symbol} | reason={reason}"
            )
            self.record_rejection(symbol, reason, details)
            return MultiBrokerResult(
                signal_id=signal_id,
                success=False,
                full_success=False,
                broker_results={
                    broker: BrokerResult(broker=broker, success=False, error=reason)
                    for broker in executors.keys()
                },
            )

        logger.info(f"[MULTI_EXEC] PRE-FLIGHT PASS | {symbol}")

        # 1c. Apply Risk Multiplier (Phase 5)
        multiplier = signal_dict.get("risk_multiplier", 1.0)
        adjusted_qty = quantity * multiplier
        if multiplier != 1.0:
            logger.info(f"[CAPITAL] Multi-Broker Risk Adjustment: {multiplier:.2f} | {quantity:.6f} -> {adjusted_qty:.6f}")

        # 1d. Divide qty across active brokers (both split and mirror)
        active_executor_count = sum(1 for e in executors.values() if e is not None)
        if active_executor_count > 1:
            per_broker_qty = adjusted_qty / active_executor_count
            logger.info(
                f"[MULTI_EXEC] {mode.upper()} MODE | total_qty={adjusted_qty:.6f}"
                f" / {active_executor_count} brokers = {per_broker_qty:.6f} each"
            )
            adjusted_qty_per_broker = per_broker_qty
        else:
            adjusted_qty_per_broker = adjusted_qty  # single broker: full qty

        # 2. Execute on all brokers concurrently
        broker_results: Dict[str, BrokerResult] = {}
        
        async def execute_on_single_broker(
            broker_name: str,
            executor: Any,
        ) -> BrokerResult:
            """Execute on a single broker and return the result."""
            broker_symbol = symbol
            broker_qty = adjusted_qty_per_broker
            
            # Kraken needs symbol conversion (and coin->USD contract conversion ONLY for inverse PI_ symbols;
            # linear PF_ perpetuals like PF_XBTUSD / PF_ETHUSD / PF_SOLUSD are denominated 1 contract = 1 base coin)
            if broker_name == "kraken":
                broker_symbol = to_kraken_symbol(symbol)
                entry = float(signal_dict.get("entry", 0))
                if broker_symbol.upper().startswith("PI_") and entry > 0 and adjusted_qty_per_broker < 50:
                    # Convert coin qty to Kraken inverse contracts (1 contract = $1)
                    broker_qty = float(max(1, round(adjusted_qty_per_broker * entry)))
                    logger.info(
                        f"[MULTI_EXEC:{broker_name}] inverse coin->contracts: {adjusted_qty_per_broker:.6f} coins "
                        f"* ${entry:.2f} = {broker_qty:.0f} contracts | symbol: {symbol} -> {broker_symbol}"
                    )
                else:
                    logger.info(
                        f"[MULTI_EXEC:{broker_name}] linear symbol mapping: {symbol} -> {broker_symbol} | qty={broker_qty:.6f}"
                    )
            
            logger.info(
                f"[MULTI_EXEC:{broker_name}] EXECUTING | {broker_symbol} {direction}"
                f" | qty={broker_qty} | lev={leverage}x"
            )
            
            try:
                # Call the broker's execute_signal
                # Use broker_symbol for Kraken, original symbol for Bitget
                exec_symbol = broker_symbol if broker_name == "kraken" else symbol
                broker_signal = {**signal_dict, "_from_multi_broker": True}
                exec_result = await self.execute_signal(
                    executor=executor,
                    symbol=exec_symbol,
                    signal_dict=broker_signal,
                    quantity=broker_qty,
                    leverage=leverage,
                    signal_id=signal_id,
                )
                
                if exec_result.get("success"):
                    order_id = exec_result.get("entry_order_id")
                    fill_price = exec_result.get("price_executed")
                    qty = exec_result.get("quantity")
                    
                    logger.info(
                        f"[MULTI_EXEC:{broker_name}] ✓ SUCCESS | order_id={order_id}"
                        f" | price={fill_price} | qty={qty}"
                    )
                    
                    return BrokerResult(
                        broker=broker_name,
                        success=True,
                        order_id=str(order_id),
                        fill_price=fill_price,
                        quantity=qty,
                    )
                else:
                    status = exec_result.get("status", "")
                    error = exec_result.get("error", "Unknown")
                    
                    if status and status.startswith("SKIPPED"):
                        logger.warning(f"[MULTI_EXEC:{broker_name}] ⏸ SKIPPED | reason={error}")
                        return BrokerResult(
                            broker=broker_name,
                            success=False,
                            skipped=True,
                            skip_reason=error,
                            error=error,
                        )

                    logger.error(f"[MULTI_EXEC:{broker_name}] ✗ FAILED | error={error}")
                    
                    return BrokerResult(
                        broker=broker_name,
                        success=False,
                        error=error,
                    )
                    
            except Exception as e:
                logger.error(f"[MULTI_EXEC:{broker_name}] ✗ EXCEPTION | {e}", exc_info=True)
                return BrokerResult(
                    broker=broker_name,
                    success=False,
                    error=str(e),
                )
        
        # Create tasks for concurrent execution
        # FALLBACK mode: execute primary first, secondary only if primary fails
        # MIRROR/SPLIT mode: execute all concurrently
        
        # Skip brokers without executors (all modes)
        for broker_name, executor in executors.items():
            if executor is None:
                broker_results[broker_name] = BrokerResult(
                    broker=broker_name,
                    success=False,
                    skipped=True,
                    skip_reason="No executor available",
                )
        
        if mode == "fallback":
            # ── FALLBACK MODE: Sequential, primary first ──
            primary_broker = "bitget"  # Default primary
            secondary_brokers = [b for b in executors if b != primary_broker and executors[b] is not None]
            
            logger.info(f"[MULTI_EXEC] FALLBACK MODE | primary={primary_broker} | secondaries={secondary_brokers}")
            
            # Step 1: Execute on primary
            if primary_broker in executors and executors[primary_broker] is not None:
                primary_result = await execute_on_single_broker(primary_broker, executors[primary_broker])
                broker_results[primary_broker] = primary_result
                
                if primary_result.success:
                    # Primary succeeded → skip secondaries
                    logger.info(f"[MULTI_EXEC] FALLBACK | {primary_broker} SUCCESS → skipping {secondary_brokers}")
                    for sec_broker in secondary_brokers:
                        broker_results[sec_broker] = BrokerResult(
                            broker=sec_broker,
                            success=False,
                            skipped=True,
                            skip_reason=f"FALLBACK: {primary_broker} succeeded",
                        )
                else:
                    # Primary failed → try secondaries sequentially
                    logger.warning(
                        f"[MULTI_EXEC] FALLBACK | {primary_broker} FAILED ({primary_result.error})"
                        f" → trying {secondary_brokers}"
                    )
                    for sec_broker in secondary_brokers:
                        sec_result = await execute_on_single_broker(sec_broker, executors[sec_broker])
                        broker_results[sec_broker] = sec_result
                        if sec_result.success:
                            logger.info(f"[MULTI_EXEC] FALLBACK | {sec_broker} SUCCESS (fallback)")
                            break  # Stop on first success
                        else:
                            logger.warning(f"[MULTI_EXEC] FALLBACK | {sec_broker} also FAILED ({sec_result.error})")
        else:
            # ── MIRROR / SPLIT MODE: Concurrent execution ──
            tasks = [
                execute_on_single_broker(broker_name, executor)
                for broker_name, executor in executors.items()
                if executor is not None
            ]
        
            # Execute all concurrently
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for result in results:
                    if isinstance(result, Exception):
                        # This shouldn't happen as we catch exceptions inside, but just in case
                        logger.error(f"[MULTI_EXEC] Unexpected exception: {result}")
                    elif isinstance(result, BrokerResult):
                        broker_results[result.broker] = result
        
        # 3. Calculate overall success
        success_count = sum(1 for r in broker_results.values() if r.success)
        failed_count = sum(1 for r in broker_results.values() if not r.success and not r.skipped)
        
        full_success = success_count == len(executors) and failed_count == 0
        partial_success = success_count > 0
        
        # ── 3b. Kraken Fallback: if Bitget failed due to balance, accept Kraken ──
        #    When Bitget has insufficient balance but Kraken succeeded,
        #    treat this as an acceptable single-broker execution (no rollback).
        bitget_balance_fail = False
        kraken_succeeded = False
        for b_name, b_result in broker_results.items():
            if b_name == "bitget" and not b_result.success:
                err = (b_result.error or "").upper()
                if "BALANCE" in err or "INSUFFICIENT" in err or "NOTIONAL" in err:
                    bitget_balance_fail = True
            if b_name == "kraken" and b_result.success:
                kraken_succeeded = True

        kraken_fallback_active = bitget_balance_fail and kraken_succeeded
        if kraken_fallback_active:
            logger.warning(
                f"[MULTI_EXEC] KRAKEN_FALLBACK | Bitget balance insufficient "
                f"→ accepting Kraken-only execution for {symbol}"
            )
        
        # 4. Handle Strict Mirror Mode (rollback + kill-switch on partial failure)
        rollback_attempted = False
        rollback_success = False
        
        # Strict mirror only applies in 'mirror' mode (not fallback or split)
        # In fallback mode, partial is expected. In split mode, partial is degraded but not critical.
        require_strict = mode == "mirror"

        if require_strict and not full_success and success_count > 0 and not kraken_fallback_active:
            logger.critical(
                f"[MULTI_EXEC] ⚠️ STRICT MIRROR FAULT ⚠️"
                f" | success={success_count} | failed={failed_count}"
                f" | Attempting emergency rollback and locking Bot..."
            )
            rollback_attempted = True
            
            # Attempt to close positions on successful brokers immediately
            for broker_name, result in broker_results.items():
                if result.success and result.order_id:
                    executor = executors.get(broker_name)
                    if executor and hasattr(executor, 'close_position'):
                        try:
                            # Usually taking the specific symbol to close
                            close_result = await executor.close_position(symbol)
                            logger.info(f"[MULTI_EXEC] ROLLBACK {broker_name}: {close_result}")
                        except Exception as e:
                            logger.error(f"[MULTI_EXEC] ROLLBACK FAILED {broker_name}: {e}")
            
            # Lock the bot via Global Kill Switch to prevent further mismatches
            self.set_kill_switch(True, f"STRICT_MIRROR_MISMATCH_ON_{symbol}")
            
            # In strict mode, treat as definitive failure if rollback was attempted
            partial_success = False
            rollback_success = True
            
        # 5. Bookkeeping for successful brokers (Only if NOT rolled back)
        if full_success or kraken_fallback_active or (partial_success and not rollback_attempted):
            self._trades_today += 1
            self._per_symbol_trades[symbol] += 1
            self._last_trade_time[symbol] = time.time()
            self._executed_ids.add(signal_id)
        
        # 6. Persist to SQLite for each attempted broker
        if not self.shadow_mode and PERSISTENCE_AVAILABLE:
            for broker_name, result in broker_results.items():
                if result.skipped or not result.success:
                    continue  # Don't persist skipped or failed brokers
                    
                try:
                    entry_price = float(signal_dict.get("entry", 0))
                    stop_loss = float(signal_dict.get("stop_loss", 0))
                    take_profit = float(signal_dict.get("take_profit", 0))
                    
                    # If rolled back, consider the trade failed globally
                    if rollback_attempted:
                        continue  # Don't persist rolled-back trades as open
                        
                    status = "open"
                    order_id_str = str(result.order_id)
                    
                    persisted_trade = await on_trade_opened(
                        order_id=order_id_str,
                        symbol=symbol,
                        side=direction.lower(),
                        qty=(
                            result.quantity
                            if broker_name != "kraken"
                            or (
                                result.fill_quantity_verified
                                and result.qty_unit == "contracts"
                            )
                            else None
                        ),
                        entry=(
                            result.fill_price
                            if broker_name == "kraken"
                            else result.fill_price or entry_price
                        ),
                        stop=stop_loss,
                        tp=take_profit,
                        leverage=leverage,
                        session_name=signal_dict.get("session"),
                        preset=signal_dict.get("preset"),
                        signal_id=signal_id,  # Same signal_id links all brokers
                        broker=broker_name,
                        status=status,
                        raw_exchange={
                            **result.raw_response,
                            "filled_qty": result.quantity,
                            "qty_unit": result.qty_unit,
                            "fill_quantity_verified": result.fill_quantity_verified,
                            "price_executed": result.fill_price,
                        },
                    )
                    
                    if persisted_trade:
                        logger.info(
                            f"[MULTI_EXEC] PERSISTED LIVE TRADE | broker={broker_name} | status={status}"
                            f" | trade_id={persisted_trade.id}"
                            f" | order_id={order_id_str}"
                        )
                    
                    # Also persist to UI positions table
                    try:
                        pos_id = on_position_opened(
                            broker=broker_name,
                            symbol=symbol,
                            side=direction.lower(),
                            size=float(result.quantity or quantity),
                            entry_price=result.fill_price or entry_price,
                            order_id=order_id_str,
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                            leverage=leverage,
                            session_name=signal_dict.get("session"),
                            preset=signal_dict.get("preset"),
                            signal_id=signal_id,
                            status=status.upper(),
                        )
                        if pos_id:
                            logger.info(f"[MULTI_EXEC] PERSISTED UI POSITION | pos_id={pos_id}")
                    except Exception as pos_e:
                        logger.error(f"[MULTI_EXEC] UI POSITION PERSIST FAILED | {pos_e}")
                    
                    # Record trade analytics (entry) for aggregation pipeline
                    if _analytics_available and _AR:
                        try:
                            _AR.record_trade_analytics(
                                trade_id=signal_id or order_id_str,
                                symbol=symbol,
                                side=direction,
                                entry_price=result.fill_price or entry_price,
                                size=float(result.quantity or quantity),
                                sl_price=stop_loss or None,
                                tp_price=take_profit or None,
                                execution_mode="live",
                                broker=broker_name,
                                regime_at_entry=signal_dict.get("regime"),
                                strategy=signal_dict.get("setup", "smc"),
                                edge_score_at_entry=signal_dict.get("confidence"),
                            )
                        except Exception:
                            pass  # Never let analytics break trading
                        
                except Exception as e:
                    logger.error(
                        f"[MULTI_EXEC] PERSIST FAILED | broker={broker_name} | {e}",
                        exc_info=True
                    )
        
        # 7. Build and return result
        effective_success = partial_success or kraken_fallback_active
        multi_result = MultiBrokerResult(
            signal_id=signal_id,
            success=effective_success,
            full_success=full_success,
            broker_results=broker_results,
            rollback_attempted=rollback_attempted,
            rollback_success=rollback_success,
        )
        
        # Final summary log
        if kraken_fallback_active:
            status = "KRAKEN_FALLBACK"
        elif full_success:
            status = "FULL_SUCCESS"
        elif partial_success:
            status = "PARTIAL"
        else:
            status = "FAILED"
        logger.info(
            f"[MULTI_EXEC] {status} | mode={mode.upper()} | signal_id={signal_id}"
            f" | bitget={'OK' if broker_results.get('bitget', BrokerResult('', False)).success else 'FAIL'}"
            f" | kraken={'OK' if broker_results.get('kraken', BrokerResult('', False)).success else 'FAIL'}"
        )
        logger.info(
            f"[MULTI_EXEC] ═══════════════════════════════════════════════════════"
        )
        
        return multi_result

    # ──────────────────────────────────────────────────────
    #  Daily Reset / Kill Switch
    # ──────────────────────────────────────────────────────

    def _maybe_reset_daily(self) -> None:
        today = self._utc_date_str()
        if today != self._last_reset_date:
            logger.info(f"[ORDER_MGR] Daily reset | prev_pnl={self._daily_pnl:.2f}")
            self._trades_today = 0
            self._daily_pnl = 0.0
            self._per_symbol_trades.clear()
            self._last_reset_date = today

    def maybe_reset_daily(self) -> None:
        self._maybe_reset_daily()

    def update_daily_pnl(self, pnl_delta: float) -> None:
        """Called by portfolio tracker or trade closer to update PnL."""
        self._daily_pnl += pnl_delta
        if self._account_balance > 0 and self.max_daily_loss_pct > 0:
            loss_pct = abs(min(0, self._daily_pnl)) / self._account_balance * 100
            if loss_pct >= self.max_daily_loss_pct:
                self.engage_kill_switch(f"MAX_DAILY_LOSS_{loss_pct:.1f}%")
        # Feed DrawdownGovernor with trade result
        if self._dd_governor is not None:
            self._dd_governor.record_trade_result(pnl_delta)

    def update_equity(self, current_equity: float) -> Optional[Dict]:
        """
        Call on every cycle with fresh account equity.
        Updates DrawdownGovernor and returns risk assessment.
        """
        self._account_balance = current_equity
        if self._dd_governor is not None:
            if self._dd_governor._peak_equity <= 0:
                self._dd_governor.initialize(current_equity)
            return self._dd_governor.update_equity(current_equity)
        return None

    def set_account_state(self, account_state) -> None:
        """Wire AccountStateService from AppContext."""
        self._account_state = account_state

    def set_strategy_allocator(self, allocator) -> None:
        """Wire StrategyAllocator from AppContext."""
        self._strategy_allocator = allocator

    def get_drawdown_state(self) -> Optional[Dict]:
        """Return DrawdownGovernor state for dashboards."""
        if self._dd_governor is not None:
            return self._dd_governor.get_state()
        return None

    def set_kill_switch(self, enabled: bool, reason: str = "") -> None:
        """Direct control."""
        self.kill_switch_active = enabled
        self.kill_switch_reason = reason
        level = logging.ERROR if enabled else logging.INFO
        logger.log(level, f"[KILL] {'ENGAGED' if enabled else 'DISENGAGED'} | reason={reason}")

    def engage_kill_switch(self, reason: str = "manual") -> None:
        self.set_kill_switch(True, reason)

    def disengage_kill_switch(self) -> None:
        self.set_kill_switch(False, "")

    def get_status(self) -> Dict:
        """Unified status for API."""
        status = {
            "mode": "shadow" if self.shadow_mode else ("live" if self.auto_execute_enabled else "monitor"),
            "kill_switch": {"active": self.kill_switch_active, "reason": self.kill_switch_reason},
            "daily": {
                "trades": self._trades_today,
                "max": self.max_trades_per_day,
                "pnl": round(self._daily_pnl, 2),
                "balance": self._account_balance
            },
            "exec_log": self._exec_log[-10:],
            "reject_log": self._reject_log[-10:]
        }
        dd_state = self.get_drawdown_state()
        if dd_state is not None:
            status["drawdown_governor"] = dd_state
        return status
    
    # Alias for API compatibility
    def get_daily_stats(self) -> Dict:
        return {
            "trades_count": self._trades_today,
            "trades_by_symbol": self._per_symbol_trades.copy(),
            "pnl_usd": self._daily_pnl
        }

    # ──────────────────────────────────────────────────────
    #  Positions
    # ──────────────────────────────────────────────────────
    
    async def reconcile_positions(self, executor, orchestrators: Dict) -> None:
        """Compare exchange snapshots without inferring a close from absence."""
        if not executor or not orchestrators or self.shadow_mode:
            return

        for symbol, orch in list(orchestrators.items()):
            if not orch.active_trade:
                continue

            try:
                positions_res = await executor.get_positions(symbol)
                if isinstance(positions_res, list):
                    pos_list = positions_res
                elif isinstance(positions_res, dict):
                    code = positions_res.get("code")
                    if code is not None and code != "00000":
                        logger.error(
                            "[GUARD] Position snapshot failed for %s | code=%s | msg=%s",
                            symbol,
                            code,
                            positions_res.get("msg"),
                        )
                        continue
                    candidate = positions_res.get("data", positions_res.get("positions"))
                    if not isinstance(candidate, list):
                        logger.error(
                            "[GUARD] Invalid position snapshot for %s; preserving active trade",
                            symbol,
                        )
                        continue
                    pos_list = candidate
                else:
                    logger.error(
                        "[GUARD] Invalid position snapshot type for %s: %s",
                        symbol,
                        type(positions_res).__name__,
                    )
                    continue

                matching_positions = []
                invalid_snapshot = False
                for position in pos_list:
                    if not isinstance(position, dict):
                        invalid_snapshot = True
                        break
                    position_symbol = str(position.get("symbol", symbol)).upper()
                    if position_symbol != symbol.upper():
                        continue
                    raw_size = position.get("total", position.get("size", position.get("available")))
                    try:
                        size = float(raw_size)
                    except (TypeError, ValueError, OverflowError):
                        invalid_snapshot = True
                        break
                    if not math.isfinite(size) or size < 0:
                        invalid_snapshot = True
                        break
                    if size > 0:
                        matching_positions.append(position)

                if invalid_snapshot:
                    logger.error(
                        "[GUARD] Malformed position snapshot for %s; preserving active trade",
                        symbol,
                    )
                    continue

                if not matching_positions:
                    logger.error(
                        "[POSITION_DIVERGENCE] %s | exchange snapshot contains no open "
                        "position, but no confirmed close fill is available; preserving "
                        "memory and database state",
                        symbol,
                    )
                    continue

                logger.debug(
                    "[GUARD] %s | exchange position present; active trade retained pending fill reconciliation",
                    symbol,
                )

            except Exception as e:
                logger.error(
                    "[GUARD] Position check failed for %s; preserving active trade: %s",
                    symbol,
                    e,
                    exc_info=True,
                )

    def get_stats(self) -> Dict[str, Any]:
        """
        Return comprehensive stats for monitoring/API.
        Required for WS_DASHBOARD broadcasts.
        """
        try:
            now_str = self._utc_date_str()
            self._maybe_reset_daily()
            
            return {
                "mode": "shadow" if self.shadow_mode else ("live" if self.auto_execute_enabled else "monitor"),
                "kill_switch": {
                    "active": self.kill_switch_active,
                    "reason": self.kill_switch_reason,
                },
                "daily": {
                    "trades": self._trades_today,
                    "max": self.max_trades_per_day,
                    "pnl": round(self._daily_pnl, 2),
                    "balance": round(self._account_balance, 2),
                    "date": now_str
                },
                "open_orders": len(self.open_orders) if hasattr(self, "open_orders") else 0,
                "pending_orders": len(self.pending_orders) if hasattr(self, "pending_orders") else 0,
                "filled_orders": self.filled_count if hasattr(self, "filled_count") else 0,
                "canceled_orders": self.canceled_count if hasattr(self, "canceled_count") else 0,
                "exec_log": self._exec_log[-10:] if self._exec_log else [],
                "reject_log": self._reject_log[-10:] if self._reject_log else [],
                "last_update_ts": getattr(self, "last_update_ts", now_str),
                "uptime_last_reset": self._last_reset_date
            }
        except Exception as e:
            logger.exception("[ORDER_MANAGER] get_stats failed: %s", e)
            return {"error": str(e)}

    # ──────────────────────────────────────────────────────
    #  Emergency Close with Retry (Phase 5c)
    # ──────────────────────────────────────────────────────

    async def _emergency_close_with_retry(
        self,
        executor,
        symbol: str,
        direction: str,
        quantity: float,
        signal_id: Optional[str] = None,
    ) -> bool:
        """Submit one close; never retry an order with an ambiguous outcome."""
        try:
            if (
                not executor
                or getattr(executor, "broker_name", None) != "kraken"
                or not hasattr(executor, "close_position")
            ):
                raise RuntimeError("No broker-specific reduce-only close is available")
            close_result = await executor.close_position(
                symbol,
                quantity=quantity,
                reduce_only=True,
                reason=f"emergency:{signal_id or 'slippage'}",
            )
        except Exception as e:
            logger.critical(
                "[EMERGENCY_CLOSE] %s outcome unknown; preserving position state: %s",
                symbol,
                e,
            )
            self._orphan_positions.add(symbol)
            return False

        if not isinstance(close_result, dict) or close_result.get("success") is not True:
            logger.critical(
                "[EMERGENCY_CLOSE] %s was not confirmed; preserving position state | response=%s",
                symbol,
                close_result,
            )
            self._orphan_positions.add(symbol)
            return False

        try:
            filled_qty = float(close_result.get("filled_qty"))
            fill_price = float(close_result.get("fill_price") or close_result.get("avg_price"))
        except (TypeError, ValueError, OverflowError):
            filled_qty = 0.0
            fill_price = 0.0

        tolerance = max(1e-12, abs(quantity) * 1e-8)
        fill_confirmed = (
            close_result.get("fill_quantity_verified") is True
            and close_result.get("fill_price_verified") is True
            and close_result.get("qty_unit") == "contracts"
            and math.isfinite(filled_qty)
            and filled_qty > 0
            and filled_qty <= quantity + tolerance
            and math.isfinite(fill_price)
            and fill_price > 0
        )
        if not fill_confirmed:
            logger.critical(
                "[EMERGENCY_CLOSE] %s order accepted without a verified fill; "
                "preserving position state | order_id=%s",
                symbol,
                close_result.get("order_id"),
            )
            self._orphan_positions.add(symbol)
            return False

        if filled_qty + tolerance < quantity:
            remaining_qty = max(0.0, quantity - filled_qty)
            tracked_qty = self._position_sizes.get(symbol)
            tracked_unit = self._position_size_units.get(symbol)
            if (
                tracked_unit == "contracts"
                and tracked_qty is not None
                and math.isfinite(float(tracked_qty))
                and abs(float(tracked_qty) - quantity) <= tolerance
            ):
                self._position_sizes[symbol] = max(0.0, float(tracked_qty) - filled_qty)
            elif tracked_qty is None:
                self._position_sizes[symbol] = remaining_qty
                self._position_size_units[symbol] = "contracts"
            else:
                logger.error(
                    "[EMERGENCY_CLOSE] %s tracked quantity/unit diverges from close request; "
                    "preserving tracked value | tracked=%s unit=%s requested=%s",
                    symbol,
                    tracked_qty,
                    tracked_unit,
                    quantity,
                )
            self._open_position_symbols.add(symbol)
            self._orphan_positions.add(symbol)
            logger.critical(
                "[EMERGENCY_CLOSE] %s partial fill | filled=%s remaining=%s; position remains open",
                symbol,
                filled_qty,
                remaining_qty,
            )
            return False

        tracked_qty = self._position_sizes.get(symbol)
        tracked_unit = self._position_size_units.get(symbol)
        if tracked_qty is not None and (
            tracked_unit != "contracts"
            or not math.isfinite(float(tracked_qty))
            or abs(float(tracked_qty) - quantity) > tolerance
        ):
            logger.error(
                "[EMERGENCY_CLOSE] %s confirmed fill conflicts with tracked quantity/unit; "
                "preserving state | tracked=%s unit=%s fill=%s",
                symbol,
                tracked_qty,
                tracked_unit,
                quantity,
            )
            self._orphan_positions.add(symbol)
            return False

        self._open_position_symbols.discard(symbol)
        self._position_sizes.pop(symbol, None)
        self._position_size_units.pop(symbol, None)
        self._orphan_positions.discard(symbol)
        logger.critical(
            "[EMERGENCY_CLOSE] %s fill confirmed | qty=%s | order_id=%s",
            symbol,
            filled_qty,
            close_result.get("order_id"),
        )
        # Record risk event
        if _analytics_available and _AR:
            try:
                _AR.record_risk_event(
                    event_type="EMERGENCY_CLOSE_OK",
                    severity="CRITICAL",
                    detail={"symbol": symbol, "filled_qty": filled_qty},
                    symbol=symbol,
                )
            except Exception:
                pass
        return True

    # ──────────────────────────────────────────────────────
    #  Internal Helpers
    # ──────────────────────────────────────────────────────

    @staticmethod
    def _utc_date_str() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _calculate_rr(self, entry: float, sl: float, tp: float) -> float:
        if entry == 0 or sl == 0 or tp == 0:
            return 0.0
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk == 0:
            return 0.0
        return reward / risk

    def _validate_risk_reward(self, entry: float, sl: float, tp: float) -> bool:
        """Validate RR with epsilon tolerance for floating-point comparison."""
        rr = self._calculate_rr(entry, sl, tp)
        # Use epsilon to avoid false rejections like 1.9999999 < 2.0
        return rr >= (self.min_rr - RR_EPSILON)

    def _validate_tpsl_logic(self, direction: str, entry: float, sl: float, tp: float) -> bool:
        if entry <= 0 or sl <= 0 or tp <= 0:
            return False
            
        if direction == "long":
            # SL < Entry < TP
            return sl < entry < tp
        elif direction == "short":
            # TP < Entry < SL
            return tp < entry < sl
        return False
