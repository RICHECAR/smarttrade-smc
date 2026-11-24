"""
Multi-Broker Trading Engine — Simultaneous LIVE Trading on Bitget & Kraken
═══════════════════════════════════════════════════════════════════════════

Coordinates real-time trading across multiple exchanges in a single loop.

Features:
  - LIVE trading on Bitget Futures AND Kraken Futures simultaneously
  - Unified balance checking across brokers
  - Parallel market data fetching
  - Signal execution on both exchanges
  - WebSocket reconnection handling
  - Comprehensive logging with broker identification

Usage:
    from smc_futures.engine.multi_broker_engine import MultiBrokerEngine
    
    engine = MultiBrokerEngine()
    await engine.initialize()
    
    # Run trading loop
    await engine.run_strategy_loop()

Author: SmartTrade AI Framework
"""

import asyncio
import logging
import os
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from core.env_loader import safe_load_dotenv

# Re-load environment gracefully (no-op in test mode)
safe_load_dotenv()

# We only optionally import load_secret if available, otherwise fallback securely
try:
    from smc_futures.security.secrets import load_secret
except ImportError:
    # Fallback if accessed from weird path
    def load_secret(var_name: str, required: bool = False, default: str = "") -> str:
        return os.getenv(var_name, default)

logger = logging.getLogger("smarttrade.multi_broker")

# Per-wrapper startup reconciliation registry (prevents double reconciliation
# when multiple components start or share the same KrakenFuturesExecutorWrapper).
_KRAKEN_RECONCILE_LOCKS: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = weakref.WeakKeyDictionary()
_KRAKEN_RECONCILE_REPORTS: "weakref.WeakKeyDictionary[Any, Dict[str, Any]]" = weakref.WeakKeyDictionary()
_KRAKEN_RECONCILE_IN_PROGRESS: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _resolve_kraken_wrapper(kraken_executor: Any) -> Optional[Any]:
    """
    Resolve the exact KrakenFuturesExecutorWrapper backing an executor.
    Supports KrakenFuturesExecutor (via _get_wrapper()) and direct wrapper instances.
    """
    if kraken_executor is None:
        return None
    if hasattr(kraken_executor, "reconcile_on_startup") and callable(kraken_executor.reconcile_on_startup):
        return kraken_executor
    get_wrapper = getattr(kraken_executor, "_get_wrapper", None)
    if callable(get_wrapper):
        return get_wrapper()
    wrapper = getattr(kraken_executor, "_wrapper", None)
    if wrapper is not None and hasattr(wrapper, "reconcile_on_startup"):
        return wrapper
    return None


# Helper function for mirror mode
def is_multi_broker_mirror_enabled() -> bool:
    """
    Check if mirror mode is enabled — duplicate all orders to both brokers.
    
    Env vars (any of these enables mirror mode):
      - MULTI_BROKER_MIRROR=true
      - DUAL_EXECUTION=true
    """
    mirror = os.getenv("MULTI_BROKER_MIRROR", "0").lower() in ("1", "true", "yes")
    dual = os.getenv("DUAL_EXECUTION", "0").lower() in ("1", "true", "yes")
    return mirror or dual


# ══════════════════════════════════════════════════════════════════════════════
# TYPES & ENUMS
# ══════════════════════════════════════════════════════════════════════════════

class BrokerType(str, Enum):
    """Supported broker types."""
    BITGET = "bitget"
    KRAKEN = "kraken"
    MT5 = "mt5"


class OrderSide(str, Enum):
    """Order side."""
    BUY = "buy"
    SELL = "sell"
    LONG = "long"   # Alias
    SHORT = "short"  # Alias


class SignalType(str, Enum):
    """Trading signal types."""
    LONG = "long"
    SHORT = "short"
    EXIT_LONG = "exit_long"
    EXIT_SHORT = "exit_short"
    NO_SIGNAL = "none"


@dataclass
class Balance:
    """Account balance information."""
    broker: BrokerType
    available: float
    total: float
    currency: str = "USD"
    margin_used: float = 0.0
    unrealized_pnl: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def usable(self) -> float:
        """Alias for available balance."""
        return self.available


@dataclass
class Candle:
    """OHLCV candle data."""
    timestamp: int  # Milliseconds
    open: float
    high: float
    low: float
    close: float
    volume: float
    
    @property
    def ts_datetime(self) -> datetime:
        """Convert timestamp to datetime."""
        return datetime.fromtimestamp(self.timestamp / 1000, tz=timezone.utc)


@dataclass
class Signal:
    """Trading signal."""
    symbol: str
    signal_type: SignalType
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    quantity: Optional[float] = None
    leverage: int = 1
    reason: str = ""
    confidence: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def is_entry(self) -> bool:
        """Check if signal is entry (long or short)."""
        return self.signal_type in (SignalType.LONG, SignalType.SHORT)
    
    @property
    def side(self) -> Optional[str]:
        """Get order side for entry signals."""
        if self.signal_type == SignalType.LONG:
            return "buy"
        elif self.signal_type == SignalType.SHORT:
            return "sell"
        return None


@dataclass
class OrderResult:
    """Order execution result."""
    success: bool
    broker: BrokerType
    symbol: str
    order_id: Optional[str] = None
    filled_price: Optional[float] = None
    filled_qty: Optional[float] = None
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BrokerStatus:
    """Broker connection status."""
    broker: BrokerType
    connected: bool
    last_heartbeat: Optional[datetime] = None
    error: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MultiBrokerConfig:
    """Multi-broker trading configuration."""
    
    # Bitget credentials
    bitget_api_key: str = ""
    bitget_api_secret: str = ""
    bitget_passphrase: str = ""
    bitget_enabled: bool = False
    
    # Kraken credentials
    kraken_api_key: str = ""
    kraken_api_secret: str = ""
    kraken_enabled: bool = True

    # MetaTrader 5 (MT5) credentials
    mt5_login: Optional[int] = None
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_terminal_path: str = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    mt5_enabled: bool = False
    
    # Multi-broker routing
    default_broker: str = "kraken"  # "bitget", "kraken", or "mt5"
    
    # Trading symbols per broker
    symbols: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"])
    bitget_symbols: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"])
    kraken_symbols: List[str] = field(default_factory=lambda: ["PI_XBTUSD", "PI_ETHUSD", "PI_SOLUSD", "PI_XRPUSD"])
    mt5_symbols: List[str] = field(default_factory=lambda: ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"])
    
    # Position sizing
    default_leverage: int = 3
    risk_per_trade_pct: float = 0.01  # 1% risk per trade
    max_position_size_pct: float = 0.1  # 10% max position
    min_balance_usd: float = 10.0  # Minimum balance to trade
    
    # Timing
    loop_interval_sec: int = 60  # Strategy loop interval
    balance_check_interval_sec: int = 300  # Balance refresh interval
    reconnect_delay_sec: int = 5  # Delay before reconnect attempt
    max_reconnect_attempts: int = 5
    
    @classmethod
    def from_env(cls) -> "MultiBrokerConfig":
        """Load configuration from environment variables."""
        symbols_str = os.getenv("BOT_SYMBOLS", "BTCUSDT,ETHUSDT")
        symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
        
        from smc_futures.smc_config import is_kraken_execution_enabled
        kraken_on = os.getenv("KRAKEN_ENABLED", "false").strip().lower() in ("1", "true", "yes")
        mt5_on = os.getenv("MT5_ENABLED", "false").strip().lower() in ("1", "true", "yes")
        mt5_exec_on = os.getenv("MT5_EXECUTION_ENABLED", "false").strip().lower() in ("1", "true", "yes")
        raw_mt5_login = os.getenv("MT5_LOGIN", "").strip()
        mt5_syms_str = os.getenv("MT5_SYMBOLS", "BTCUSD,ETHUSD,SOLUSD,XRPUSD")
        mt5_syms = [s.strip() for s in mt5_syms_str.split(",") if s.strip()]
        return cls(
            bitget_api_key=load_secret("EXEC_BITGET_API_KEY", required=False, default=os.getenv("BITGET_API_KEY", "")),
            bitget_api_secret=load_secret("EXEC_BITGET_SECRET_KEY", required=False, default=os.getenv("BITGET_API_SECRET", "")),
            bitget_passphrase=load_secret("EXEC_BITGET_PASSPHRASE", required=False, default=os.getenv("BITGET_PASSPHRASE", "")),
            bitget_enabled=os.getenv("BITGET_ENABLED", "false").lower() == "true",
            kraken_api_key=load_secret("EXEC_KRAKEN_API_KEY", required=False, default=os.getenv("KRAKEN_API_KEY", "")),
            kraken_api_secret=load_secret("EXEC_KRAKEN_API_SECRET", required=False, default=os.getenv("KRAKEN_API_SECRET", "")),
            kraken_enabled=kraken_on and is_kraken_execution_enabled(),
            mt5_login=int(raw_mt5_login) if raw_mt5_login.isdigit() else None,
            mt5_password=os.getenv("MT5_PASSWORD", ""),
            mt5_server=os.getenv("MT5_SERVER", ""),
            mt5_terminal_path=os.getenv("MT5_TERMINAL_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe"),
            mt5_enabled=mt5_on and mt5_exec_on,
            mt5_symbols=mt5_syms,
            symbols=symbols,
            default_broker=os.getenv("DEFAULT_BROKER", "kraken"),
            default_leverage=int(os.getenv("BOT_LEVERAGE", "3")),
            risk_per_trade_pct=float(os.getenv("RISK_PCT", "0.01")),
            min_balance_usd=float(os.getenv("MIN_BALANCE_USD", "10.0")),
            loop_interval_sec=int(os.getenv("LOOP_INTERVAL_SEC", "60")),
        )


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-BROKER ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class MultiBrokerEngine:
    """
    Multi-broker trading engine for simultaneous LIVE trading.
    
    Coordinates trading operations across Bitget and Kraken Futures,
    running them in parallel within the same strategy loop.
    """
    
    def __init__(
        self,
        config: Optional[MultiBrokerConfig] = None,
        strategy_callback: Optional[Callable[[str, List[Candle], BrokerType], Optional[Signal]]] = None,
        bitget_executor: Optional[Any] = None,
        kraken_executor: Optional[Any] = None,
        mt5_executor: Optional[Any] = None,
    ):
        """
        Initialize multi-broker engine.
        
        Args:
            config: Trading configuration (loads from env if None)
            strategy_callback: Custom strategy function for signal generation
                Signature: (symbol, candles, broker) -> Optional[Signal]
            bitget_executor: Pre-initialized Bitget executor (optional)
            kraken_executor: Pre-initialized Kraken executor (optional)
            mt5_executor: Pre-initialized MetaTrader 5 executor (optional)
        """
        self.config = config or MultiBrokerConfig.from_env()
        self.strategy_callback = strategy_callback
        
        # Executors (use provided or lazy initialization)
        self._bitget_executor = bitget_executor
        self._kraken_executor = kraken_executor
        self._mt5_executor = mt5_executor
        
        # Market data clients (lazy initialization)
        self._bitget_market = None
        self._kraken_market = None
        
        # State tracking
        self._running = False
        self._kraken_reconciliation_in_progress = False
        self._kraken_reconciliation_done = False
        self._kraken_reconciliation_report: Optional[Dict[str, Any]] = None
        # Phase 1.9.8: Single-owner Kraken Futures WS v1 book feed lifecycle
        self._kraken_book_feed: Optional[Any] = None
        self._book_feed_init_lock: asyncio.Lock = asyncio.Lock()
        self._reconnect_lock: asyncio.Lock = asyncio.Lock()
        self._balances: Dict[BrokerType, Balance] = {}
        self._last_balance_check: Dict[BrokerType, float] = {}
        self._broker_status: Dict[BrokerType, BrokerStatus] = {}
        self._active_positions: Dict[str, Dict[BrokerType, Any]] = {}
        
        # Set initial broker status based on provided executors
        if bitget_executor:
            self._broker_status[BrokerType.BITGET] = BrokerStatus(
                broker=BrokerType.BITGET,
                connected=True,
                last_heartbeat=datetime.now(timezone.utc),
            )
        
        if kraken_executor:
            self._broker_status[BrokerType.KRAKEN] = BrokerStatus(
                broker=BrokerType.KRAKEN,
                connected=getattr(kraken_executor, 'is_connected', lambda: True)() if callable(getattr(kraken_executor, 'is_connected', None)) else True,
                last_heartbeat=datetime.now(timezone.utc),
            )

        if mt5_executor:
            self._broker_status[BrokerType.MT5] = BrokerStatus(
                broker=BrokerType.MT5,
                connected=bool(getattr(mt5_executor, 'is_connected', False)),
                last_heartbeat=datetime.now(timezone.utc),
            )
        
        # Statistics
        self._stats = {
            "signals_generated": 0,
            "orders_sent": 0,
            "orders_filled": 0,
            "orders_failed": 0,
            "balance_checks": 0,
            "candle_fetches": 0,
        }
        
        logger.info("[MULTI_BROKER] Engine created | symbols=%s | bitget=%s | kraken=%s | mt5=%s",
                   self.config.symbols,
                   "enabled" if self.config.bitget_enabled else "disabled",
                   "enabled" if self.config.kraken_enabled else "disabled",
                   "enabled" if self.config.mt5_enabled else "disabled")
    
    # ──────────────────────────────────────────────────────────────────────────
    #  INITIALIZATION
    # ──────────────────────────────────────────────────────────────────────────
    
    async def initialize(self) -> None:
        """
        Initialize all broker connections and market data clients.
        
        Connects to enabled brokers and fetches initial balances.
        If executors are already set (from constructor), only initializes
        the market data clients.
        
        Raises exception if no brokers can be connected.
        """
        logger.info("[MULTI_BROKER] Initializing connections...")
        
        init_tasks = []
        
        # Init Bitget if: (enabled + api_key) OR (executor already set but no market client)
        bitget_needs_init = (
            (self.config.bitget_enabled and self.config.bitget_api_key) or
            (self._bitget_executor and not self._bitget_market)
        )
        if bitget_needs_init:
            init_tasks.append(self._init_bitget())
        
        # Init Kraken if: (enabled + api_key) OR (executor already set but no market client)
        kraken_needs_init = (
            (self.config.kraken_enabled and self.config.kraken_api_key) or
            (self._kraken_executor and not self._kraken_market)
        )
        if kraken_needs_init:
            init_tasks.append(self._init_kraken())

        # Init MT5 if: enabled + login/server OR executor already provided
        mt5_needs_init = (
            (self.config.mt5_enabled and self.config.mt5_login and self.config.mt5_server) or
            (self._mt5_executor is not None and not getattr(self._mt5_executor, "is_connected", False))
        )
        if mt5_needs_init:
            init_tasks.append(self._init_mt5())
        
        if not init_tasks:
            # Log but don't error - maybe executors were passed without market data needs
            logger.warning("[MULTI_BROKER] No brokers to initialize. Check configuration.")
            if self._kraken_executor:
                await self.reconcile_kraken_on_startup()
            return
        
        # Initialize in parallel
        results = await asyncio.gather(*init_tasks, return_exceptions=True)
        
        # Check results
        successful = sum(1 for r in results if r is True)
        if successful == 0:
            errors = [str(r) for r in results if isinstance(r, Exception)]
            raise RuntimeError(f"[MULTI_BROKER] All broker initializations failed: {errors}")
        
        logger.info("[MULTI_BROKER] Initialized %d/%d brokers successfully",
                   successful, len(init_tasks))
        
        # Mandatory fail-closed Kraken startup reconciliation before trading
        if self._kraken_executor:
            await self.reconcile_kraken_on_startup()
        
        # Fetch initial balances
        await self.refresh_all_balances()

    def _is_kraken_reconciliation_ready(self) -> Tuple[bool, str]:
        """
        Strict fail-closed check ensuring Kraken startup reconciliation has
        genuinely succeeded and released the global entry lock before any
        signal or entry order is processed.

        READY requires ALL of the following simultaneously:
        - Wrapper resolved
        - No reconciliation currently in progress
        - wrapper._startup_reconciliation_completed is True
        - wrapper._global_entry_lock is False
        - If a reconciliation report is present:
            report["startup_secure"] is True and report["global_entry_lock"] is False
        """
        wrapper = _resolve_kraken_wrapper(self._kraken_executor)
        if wrapper is None:
            return False, "KRAKEN_WRAPPER_NOT_INITIALIZED"
        if self._kraken_reconciliation_in_progress or wrapper in _KRAKEN_RECONCILE_IN_PROGRESS:
            return False, "STARTUP_RECONCILIATION_IN_PROGRESS"
        if getattr(wrapper, "_startup_reconciliation_completed", False) is not True:
            return False, "STARTUP_RECONCILIATION_NOT_COMPLETED"
        if getattr(wrapper, "_global_entry_lock", True) is not False:
            reason = getattr(wrapper, "_global_entry_lock_reason", None) or "GLOBAL_ENTRY_LOCK_ACTIVE"
            return False, str(reason)

        report = self._kraken_reconciliation_report or _KRAKEN_RECONCILE_REPORTS.get(wrapper)
        if report is not None:
            if not isinstance(report, dict):
                return False, "INVALID_RECONCILIATION_REPORT"
            if report.get("startup_secure") is not True:
                return False, str(report.get("status") or "STARTUP_RECONCILIATION_NOT_SECURE")
            if report.get("global_entry_lock") is not False:
                return False, str(report.get("error") or report.get("status") or "GLOBAL_ENTRY_LOCK_ACTIVE")

        return True, "READY"

    async def reconcile_kraken_on_startup(self) -> Dict[str, Any]:
        """
        Run KrakenFuturesExecutorWrapper.reconcile_on_startup() once per wrapper
        instance on success (idempotent and serialized across concurrent callers).

        Preserves strict fail-closed semantics:
        - Requires Kraken broker to be connected before reconciling.
        - Never overrides _global_entry_lock to False.
        - Maintains global entry lock on anomalies, Kraken API errors, exceptions,
          or asyncio.CancelledError.
        - Caches ONLY genuinely successful reconciliations (startup_secure is True),
          allowing controlled retries after transient failures or cancellations.
        """
        wrapper = _resolve_kraken_wrapper(self._kraken_executor)
        if wrapper is None:
            report: Dict[str, Any] = {
                "status": "NO_KRAKEN_WRAPPER",
                "startup_secure": False,
                "global_entry_lock": True,
                "error": "KrakenFuturesExecutorWrapper unavailable",
            }
            self._kraken_reconciliation_done = False
            self._kraken_reconciliation_report = report
            logger.critical("[KRAKEN_RECONCILE] STARTUP_RECONCILIATION_FAILED | error=%s", report["error"])
            return report

        kraken_status = self._broker_status.get(BrokerType.KRAKEN)
        if kraken_status is not None and not kraken_status.connected:
            wrapper._global_entry_lock = True
            wrapper._startup_reconciliation_completed = False
            self._kraken_reconciliation_done = False
            _KRAKEN_RECONCILE_REPORTS.pop(wrapper, None)
            report = {
                "status": "KRAKEN_NOT_CONNECTED",
                "startup_secure": False,
                "global_entry_lock": True,
                "error": kraken_status.error or "Kraken broker is not connected",
            }
            self._kraken_reconciliation_report = report
            logger.critical(
                "[KRAKEN_RECONCILE] STARTUP_RECONCILIATION_FAILED | status=KRAKEN_NOT_CONNECTED | error=%s | global_entry_lock=True",
                report["error"],
            )
            return report

        lock = _KRAKEN_RECONCILE_LOCKS.get(wrapper)
        if lock is None:
            lock = asyncio.Lock()
            _KRAKEN_RECONCILE_LOCKS[wrapper] = lock

        async with lock:
            cached = _KRAKEN_RECONCILE_REPORTS.get(wrapper)
            if (
                isinstance(cached, dict)
                and cached.get("startup_secure") is True
                and getattr(wrapper, "_startup_reconciliation_completed", False) is True
            ):
                self._kraken_reconciliation_report = cached
                self._kraken_reconciliation_done = True
                logger.info(
                    "[KRAKEN_RECONCILE] Skipping duplicate startup reconciliation (already succeeded with status=%s)",
                    cached.get("status"),
                )
                return cached

            if getattr(wrapper, "_startup_reconciliation_completed", False) is True:
                existing = {
                    "status": "SUCCESS",
                    "startup_secure": True,
                    "global_entry_lock": bool(getattr(wrapper, "_global_entry_lock", True)),
                    "already_reconciled": True,
                }
                _KRAKEN_RECONCILE_REPORTS[wrapper] = existing
                self._kraken_reconciliation_report = existing
                self._kraken_reconciliation_done = True
                logger.info("[KRAKEN_RECONCILE] Skipping duplicate startup reconciliation (wrapper already reconciled)")
                return existing

            self._kraken_reconciliation_in_progress = True
            _KRAKEN_RECONCILE_IN_PROGRESS.add(wrapper)
            logger.info("[KRAKEN_RECONCILE] STARTUP_RECONCILIATION_STARTED")

            report = {
                "status": "STARTUP_RECONCILIATION_FAILED",
                "startup_secure": False,
                "global_entry_lock": True,
            }
            try:
                raw_report = await wrapper.reconcile_on_startup()
                if isinstance(raw_report, dict):
                    report = raw_report
                else:
                    report = {
                        "status": "STARTUP_RECONCILIATION_FAILED",
                        "startup_secure": False,
                        "global_entry_lock": True,
                        "error": f"Invalid reconciliation report type: {type(raw_report).__name__}",
                    }
                    wrapper._global_entry_lock = True
                    wrapper._startup_reconciliation_completed = False

                status = str(report.get("status", "UNKNOWN"))
                startup_secure = report.get("startup_secure") is True
                global_lock = bool(
                    report.get("global_entry_lock", getattr(wrapper, "_global_entry_lock", True))
                )

                if startup_secure and not global_lock:
                    logger.info(
                        "[KRAKEN_RECONCILE] STARTUP_RECONCILIATION_COMPLETED | status=%s | startup_secure=True",
                        status,
                    )
                    logger.info("[KRAKEN_RECONCILE] GLOBAL_ENTRY_LOCK_RELEASED | status=%s", status)
                elif status == "UNRESOLVED_ANOMALIES_LOCKED":
                    logger.critical(
                        "[KRAKEN_RECONCILE] UNRESOLVED_ANOMALIES_LOCKED | status=%s | error=%s | global_entry_lock=True",
                        status,
                        report.get("error"),
                    )
                    logger.critical(
                        "[KRAKEN_RECONCILE] STARTUP_RECONCILIATION_FAILED | status=%s | error=%s",
                        status,
                        report.get("error"),
                    )
                elif startup_secure and global_lock:
                    logger.warning(
                        "[KRAKEN_RECONCILE] STARTUP_RECONCILIATION_COMPLETED | status=%s | startup_secure=True | global_entry_lock=True | reason=%s",
                        status,
                        getattr(wrapper, "_global_entry_lock_reason", None),
                    )
                else:
                    logger.critical(
                        "[KRAKEN_RECONCILE] STARTUP_RECONCILIATION_FAILED | status=%s | error=%s | global_entry_lock=True",
                        status,
                        report.get("error"),
                    )

                self._kraken_reconciliation_report = report
                if startup_secure and getattr(wrapper, "_startup_reconciliation_completed", False) is True:
                    _KRAKEN_RECONCILE_REPORTS[wrapper] = report
                    self._kraken_reconciliation_done = True
                else:
                    _KRAKEN_RECONCILE_REPORTS.pop(wrapper, None)
                    self._kraken_reconciliation_done = False

            except asyncio.CancelledError:
                wrapper._global_entry_lock = True
                wrapper._startup_reconciliation_completed = False
                self._kraken_reconciliation_done = False
                _KRAKEN_RECONCILE_REPORTS.pop(wrapper, None)
                logger.critical(
                    "[KRAKEN_RECONCILE] STARTUP_RECONCILIATION_FAILED | status=CANCELLED | global_entry_lock=True"
                )
                raise
            except Exception as exc:
                wrapper._global_entry_lock = True
                wrapper._startup_reconciliation_completed = False
                if not getattr(wrapper, "_global_entry_lock_reason", None):
                    wrapper._global_entry_lock_reason = f"STARTUP_RECONCILIATION_EXCEPTION: {exc}"
                report = {
                    "status": "STARTUP_RECONCILIATION_FAILED",
                    "startup_secure": False,
                    "global_entry_lock": True,
                    "error": str(exc),
                }
                self._kraken_reconciliation_done = False
                self._kraken_reconciliation_report = report
                _KRAKEN_RECONCILE_REPORTS.pop(wrapper, None)
                logger.critical(
                    "[KRAKEN_RECONCILE] STARTUP_RECONCILIATION_FAILED | error=%s",
                    exc,
                    exc_info=True,
                )
            finally:
                _KRAKEN_RECONCILE_IN_PROGRESS.discard(wrapper)
                self._kraken_reconciliation_in_progress = False

            return report
    
    async def _init_bitget(self) -> bool:
        """Initialize Bitget connection."""
        try:
            from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
            from backend.market.bitget_market import BitgetMarket
            
            # Only create executor if not already provided
            if not self._bitget_executor:
                self._bitget_executor = BitgetFuturesExecutor(
                    self.config.bitget_api_key,
                    self.config.bitget_api_secret,
                    self.config.bitget_passphrase,
                )
            
            # Always create market data client
            self._bitget_market = BitgetMarket()
            
            self._broker_status[BrokerType.BITGET] = BrokerStatus(
                broker=BrokerType.BITGET,
                connected=True,
                last_heartbeat=datetime.now(timezone.utc),
            )
            
            logger.info("[MULTI_BROKER] Bitget initialized successfully")
            return True
            
        except Exception as e:
            logger.error("[MULTI_BROKER] Bitget init failed: %s", e)
            self._broker_status[BrokerType.BITGET] = BrokerStatus(
                broker=BrokerType.BITGET,
                connected=False,
                error=str(e),
            )
            return False
    
    def _resolve_authorized_kraken_book_symbols(self) -> List[str]:
        """Resolve deduplicated, authorized Kraken Futures symbols (KRAKEN_CONTRACT_SPECS only)."""
        from backend.kraken_contracts import KRAKEN_CONTRACT_SPECS, to_kraken_symbol

        raw_symbols = list(getattr(self.config, "kraken_symbols", None) or [])
        if not raw_symbols:
            raw_symbols = list(getattr(self.config, "symbols", None) or [])

        authorized: List[str] = []
        for sym in raw_symbols:
            if not isinstance(sym, str) or not sym.strip():
                continue
            k_sym = to_kraken_symbol(sym.strip())
            if k_sym in KRAKEN_CONTRACT_SPECS and k_sym not in authorized:
                authorized.append(k_sym)
        return authorized

    async def start_kraken_book_feed(self) -> bool:
        """
        Phase 1.9.8 / R1: Single-owner runtime initializer for KrakenFuturesBookFeed.

        - Creates at most one active KrakenFuturesBookFeed instance for authorized symbols.
        - Guards disconnect callbacks strictly by instance identity (P2-1).
        - On spontaneous disconnect of the active feed, atomically detaches wrapper._book_feed
          and locks new entries (P2-2).
        - On asyncio.CancelledError during connect(), cleans up partial instance, conditionally
          detaches wrapper, clears obsolete reference, and re-raises CancelledError (P3).
        - Calls wrapper.set_book_feed(feed) ONLY AFTER connect() returns True.
        """
        wrapper = _resolve_kraken_wrapper(self._kraken_executor)
        if wrapper is None or not hasattr(wrapper, "set_book_feed"):
            logger.warning("[MULTI_BROKER] Cannot start Kraken book feed: wrapper unavailable")
            return False

        async with self._book_feed_init_lock:
            if self._kraken_book_feed is not None and self._kraken_book_feed.is_connected:
                wrapper.set_book_feed(self._kraken_book_feed)
                return True

            symbols = self._resolve_authorized_kraken_book_symbols()
            if not symbols:
                logger.error("[MULTI_BROKER] No authorized Kraken Futures symbols for WS book feed — OPEN MARKET blocked")
                wrapper.set_book_feed(None)
                return False

            from backend.market_data.kraken_futures_book_feed import KrakenFuturesBookFeed

            feed_holder: List[Any] = []

            def _on_book_disconnect(reason: str) -> None:
                # P2-1: Strict instance-identity guard — obsolete callbacks have ZERO side effects
                if not feed_holder:
                    return
                src_feed = feed_holder[0]
                if src_feed is not self._kraken_book_feed:
                    logger.debug(
                        "[MULTI_BROKER] Ignoring obsolete book feed disconnect callback (reason=%s)",
                        reason,
                    )
                    return
                # P2-2: Atomic conditional detach of active feed + immediate entry lock
                if getattr(wrapper, "_book_feed", None) is src_feed:
                    wrapper.set_book_feed(None)
                self._kraken_book_feed = None
                if hasattr(wrapper, "on_ws_disconnect"):
                    wrapper.on_ws_disconnect(f"BOOK_FEED_{reason}")

                # Schedule non-blocking background reconnect with backoff (only during live runtime)
                if getattr(self, "_auto_reconnect_book_feed", True):
                    try:
                        loop = asyncio.get_running_loop()
                        existing_task = getattr(self, "_book_reconnect_task", None)
                        if existing_task is None or existing_task.done():
                            self._book_reconnect_task = loop.create_task(
                                self._reconnect_kraken_book_feed_loop(wrapper)
                            )
                    except RuntimeError:
                        pass

            # Reuse a freshly pre-injected unstarted feed if provided (e.g. test ack timeout),
            # otherwise always instantiate a fresh feed and disarm any obsolete reference.
            pre_injected = self._kraken_book_feed
            if (
                pre_injected is not None
                and not pre_injected.is_connected
                and getattr(pre_injected, "_ws", None) is None
                and getattr(pre_injected, "on_disconnect_callback", None) is None
            ):
                feed = pre_injected
                feed.on_disconnect_callback = _on_book_disconnect
            else:
                if pre_injected is not None:
                    pre_injected.on_disconnect_callback = None
                    if getattr(wrapper, "_book_feed", None) is pre_injected:
                        wrapper.set_book_feed(None)
                    self._kraken_book_feed = None
                feed = KrakenFuturesBookFeed(
                    symbols=symbols,
                    on_disconnect_callback=_on_book_disconnect,
                )

            feed_holder.append(feed)
            self._kraken_book_feed = feed

            try:
                connected = await feed.connect()
            except asyncio.CancelledError:
                # P3: Clean up partial instance, conditionally detach, clear obsolete ref, and re-raise
                feed.on_disconnect_callback = None
                if getattr(wrapper, "_book_feed", None) is feed:
                    wrapper.set_book_feed(None)
                if self._kraken_book_feed is feed:
                    self._kraken_book_feed = None
                raise
            except Exception as exc:
                logger.error("[MULTI_BROKER] Kraken WS book feed connect exception: %s", exc)
                connected = False

            if connected is True:
                if self._kraken_book_feed is feed and feed.is_connected:
                    wrapper.set_book_feed(feed)
                    logger.info("[MULTI_BROKER] Kraken WS book feed connected & attached for %s", symbols)
                    return True
                feed.on_disconnect_callback = None
                if getattr(wrapper, "_book_feed", None) is feed:
                    wrapper.set_book_feed(None)
                if self._kraken_book_feed is feed:
                    self._kraken_book_feed = None
                return False

            feed.on_disconnect_callback = None
            if getattr(wrapper, "_book_feed", None) is feed:
                wrapper.set_book_feed(None)
            if self._kraken_book_feed is feed:
                self._kraken_book_feed = None
            logger.critical(
                "[MULTI_BROKER] Kraken WS book feed failed to confirm subscriptions for %s — OPEN MARKET remains blocked",
                symbols,
            )
            return False

    async def _reconnect_kraken_book_feed_loop(self, wrapper: Any) -> None:
        """Reconnect KrakenFuturesBookFeed with exponential backoff after transient WS disconnect."""
        delays = (5.0, 15.0, 30.0, 60.0)
        for attempt, delay in enumerate(delays, 1):
            await asyncio.sleep(delay)
            if self._kraken_book_feed is not None and self._kraken_book_feed.is_connected:
                return
            logger.info("[MULTI_BROKER] Attempting Kraken WS book feed reconnect (attempt %d/%d)...", attempt, len(delays))
            try:
                ok = await self.start_kraken_book_feed()
                if ok:
                    logger.info("[MULTI_BROKER] Kraken WS book feed reconnected & re-attached!")
                    if hasattr(wrapper, "on_ws_reconnect"):
                        res = wrapper.on_ws_reconnect()
                        if asyncio.iscoroutine(res):
                            await res
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[MULTI_BROKER] Kraken WS book feed reconnect attempt %d failed: %s", attempt, exc)
        logger.critical("[MULTI_BROKER] Kraken WS book feed reconnect exhausted — OPEN MARKET remains blocked fail-closed")

    async def stop_kraken_book_feed(self) -> None:
        """Phase 1.9.8 / R1: Disarm callback, conditionally detach, and cleanly disconnect KrakenFuturesBookFeed."""
        recon_task = getattr(self, "_book_reconnect_task", None)
        if recon_task is not None and not recon_task.done():
            recon_task.cancel()
        self._book_reconnect_task = None
        wrapper = _resolve_kraken_wrapper(self._kraken_executor)
        async with self._book_feed_init_lock:
            feed = self._kraken_book_feed
            self._kraken_book_feed = None
            if feed is not None:
                feed.on_disconnect_callback = None
                if wrapper is not None and hasattr(wrapper, "set_book_feed"):
                    if getattr(wrapper, "_book_feed", None) is feed:
                        wrapper.set_book_feed(None)
                try:
                    await feed.disconnect()
                except Exception as exc:
                    logger.warning("[MULTI_BROKER] Kraken WS book feed disconnect error: %s", exc)
            elif wrapper is not None and hasattr(wrapper, "set_book_feed"):
                wrapper.set_book_feed(None)

    async def _init_kraken(self) -> bool:
        """Initialize Kraken connection."""
        try:
            from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor
            from backend.market.kraken_market import KrakenMarket
            
            # Only create executor if not already provided
            if not self._kraken_executor:
                self._kraken_executor = KrakenFuturesExecutor(
                    api_key=self.config.kraken_api_key,
                    api_secret=self.config.kraken_api_secret,
                    timeout=10.0,
                    auto_reconnect=True,
                )
                
                # Connect WebSocket for orders (only for newly created executor)
                await self._kraken_executor.connect()
            
            # Always create market data client
            self._kraken_market = KrakenMarket()
            
            self._broker_status[BrokerType.KRAKEN] = BrokerStatus(
                broker=BrokerType.KRAKEN,
                connected=True,
                last_heartbeat=datetime.now(timezone.utc),
            )

            # Phase 1.6 Step 4 (P2-1): Bind WebSocket disconnect/reconnect callbacks to wrapper fail-closed guard
            wrapper = _resolve_kraken_wrapper(self._kraken_executor)
            ws_client = getattr(self._kraken_executor, "_client", None)
            if wrapper is not None and ws_client is not None and hasattr(wrapper, "bind_ws_client"):
                wrapper.bind_ws_client(ws_client)

            # Phase 1.9.8: Connect and attach synchronized WS v1 book feed before any OPEN MARKET
            book_ok = await self.start_kraken_book_feed()
            if not book_ok:
                logger.warning("[MULTI_BROKER] Kraken initialized WITHOUT active WS book feed (OPEN MARKET blocked fail-closed)")

            logger.info("[MULTI_BROKER] Kraken initialized successfully")
            return True
            
        except Exception as e:
            logger.error("[MULTI_BROKER] Kraken init failed: %s", e)
            self._broker_status[BrokerType.KRAKEN] = BrokerStatus(
                broker=BrokerType.KRAKEN,
                connected=False,
                error=str(e),
            )
            return False
    
    async def _init_mt5(self) -> bool:
        """Initialize MetaTrader 5 executor connection."""
        try:
            if not self._mt5_executor:
                from backend.execution.mt5_executor import MT5Executor
                self._mt5_executor = MT5Executor(
                    login=self.config.mt5_login,
                    password=self.config.mt5_password,
                    server=self.config.mt5_server,
                    terminal_path=self.config.mt5_terminal_path,
                )

            ok = await self._mt5_executor.connect()
            self._broker_status[BrokerType.MT5] = BrokerStatus(
                broker=BrokerType.MT5,
                connected=bool(ok),
                last_heartbeat=datetime.now(timezone.utc) if ok else None,
                error=None if ok else getattr(self._mt5_executor, "last_error", "MT5 connect failed"),
            )
            if ok:
                logger.info("[MULTI_BROKER] MetaTrader 5 initialized successfully")
            else:
                logger.error("[MULTI_BROKER] MetaTrader 5 initialization failed: %s", self._broker_status[BrokerType.MT5].error)
            return bool(ok)
        except Exception as e:
            self._broker_status[BrokerType.MT5] = BrokerStatus(
                broker=BrokerType.MT5,
                connected=False,
                error=str(e),
            )
            logger.error("[MULTI_BROKER] MT5 init exception: %s", e)
            return False

    async def shutdown(self) -> None:
        """Gracefully shutdown all connections."""
        logger.info("[MULTI_BROKER] Shutting down...")
        self._running = False

        await self.stop_kraken_book_feed()

        if self._kraken_executor:
            try:
                await self._kraken_executor.disconnect()
            except Exception as e:
                logger.warning("[MULTI_BROKER] Kraken disconnect error: %s", e)

        if self._mt5_executor:
            try:
                await self._mt5_executor.disconnect()
            except Exception as e:
                logger.warning("[MULTI_BROKER] MT5 disconnect error: %s", e)
        
        logger.info("[MULTI_BROKER] Shutdown complete")
    
    # ──────────────────────────────────────────────────────────────────────────
    #  BALANCE METHODS
    # ──────────────────────────────────────────────────────────────────────────
    
    async def fetch_balance_bitget(self) -> Optional[Balance]:
        """
        Fetch available balance from Bitget.
        
        Returns:
            Balance object with USDT balance, or None on error.
        """
        if not self._bitget_executor:
            logger.warning("[BALANCE] Bitget executor not initialized")
            return None
        
        try:
            result = await self._bitget_executor.get_account_balance(marginCoin="USDT")
            
            balance = Balance(
                broker=BrokerType.BITGET,
                available=float(result.get("available", 0)),
                total=float(result.get("equity", result.get("available", 0))),
                currency="USDT",
                margin_used=float(result.get("frozen", 0)),
                unrealized_pnl=float(result.get("unrealizedPL", 0)),
            )
            
            self._balances[BrokerType.BITGET] = balance
            self._last_balance_check[BrokerType.BITGET] = time.time()
            self._stats["balance_checks"] += 1
            
            logger.info("[BALANCE_BITGET] Available: $%.2f | Total: $%.2f",
                       balance.available, balance.total)
            
            return balance
            
        except Exception as e:
            logger.error("[BALANCE_BITGET] Fetch failed: %s", e)
            return None
    
    async def fetch_balance_kraken(self) -> Optional[Balance]:
        """
        Fetch available balance from Kraken.
        
        Returns:
            Balance object with USD balance, or None on error.
        """
        if not self._kraken_executor:
            logger.warning("[BALANCE] Kraken executor not initialized")
            return None
        
        try:
            result = await self._kraken_executor.get_account_info()
            
            balance = Balance(
                broker=BrokerType.KRAKEN,
                available=float(result.get("available", 0)),
                total=float(result.get("total", result.get("available", 0))),
                currency="USD",
                margin_used=float(result.get("margin_used", 0)),
                unrealized_pnl=float(result.get("unrealized_pnl", 0)),
            )
            
            self._balances[BrokerType.KRAKEN] = balance
            self._last_balance_check[BrokerType.KRAKEN] = time.time()
            self._stats["balance_checks"] += 1
            
            logger.info("[BALANCE_KRAKEN] Available: $%.2f | Total: $%.2f",
                       balance.available, balance.total)
            
            return balance
            
        except Exception as e:
            logger.error("[BALANCE_KRAKEN] Fetch failed: %s", e)
            return None
    
    async def fetch_balance_mt5(self) -> Optional[Balance]:
        """Fetch available balance from MetaTrader 5."""
        if not self._mt5_executor:
            logger.warning("[BALANCE] MT5 executor not initialized")
            return None

        try:
            result = await self._mt5_executor.get_account_info()
            balance = Balance(
                broker=BrokerType.MT5,
                available=float(result.get("available", 0)),
                total=float(result.get("total", result.get("equity", 0))),
                currency=str(result.get("currency", "USD")),
                margin_used=float(result.get("margin_used", 0)),
                unrealized_pnl=float(result.get("unrealized_pnl", 0)),
            )
            self._balances[BrokerType.MT5] = balance
            self._last_balance_check[BrokerType.MT5] = time.time()
            self._stats["balance_checks"] += 1
            logger.info("[BALANCE_MT5] Available: $%.2f | Total: $%.2f", balance.available, balance.total)
            return balance
        except Exception as e:
            logger.error("[BALANCE_MT5] Fetch failed: %s", e)
            return None

    async def refresh_all_balances(self) -> Dict[BrokerType, Optional[Balance]]:
        """
        Refresh balances for all active brokers in parallel.
        
        Returns:
            Dict mapping broker type to balance (or None on error).
        """
        tasks = []
        brokers = []
        
        if self._bitget_executor:
            tasks.append(self.fetch_balance_bitget())
            brokers.append(BrokerType.BITGET)
        
        if self._kraken_executor:
            tasks.append(self.fetch_balance_kraken())
            brokers.append(BrokerType.KRAKEN)

        if self._mt5_executor:
            tasks.append(self.fetch_balance_mt5())
            brokers.append(BrokerType.MT5)
        
        if not tasks:
            return {}
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        return {
            broker: (result if not isinstance(result, Exception) else None)
            for broker, result in zip(brokers, results)
        }
    
    def get_balance(self, broker: BrokerType) -> Optional[Balance]:
        """Get cached balance for broker."""
        return self._balances.get(broker)
    
    def check_sufficient_balance(
        self,
        broker: BrokerType,
        required_usd: float,
    ) -> bool:
        """
        Check if broker has sufficient balance.
        
        Args:
            broker: Broker to check
            required_usd: Required USD amount
            
        Returns:
            True if balance is sufficient, False otherwise.
        """
        balance = self._balances.get(broker)
        if not balance:
            return False
        
        return balance.available >= required_usd
    
    # ──────────────────────────────────────────────────────────────────────────
    #  MARKET DATA METHODS
    # ──────────────────────────────────────────────────────────────────────────
    
    async def fetch_candles_bitget(
        self,
        symbol: str,
        timeframe: str = "15m",
        limit: int = 200,
    ) -> List[Candle]:
        """
        Fetch OHLCV candles from Bitget.
        
        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
            timeframe: Candle timeframe (e.g., "1m", "5m", "15m", "1h", "4h")
            limit: Maximum number of candles
            
        Returns:
            List of Candle objects, sorted ascending by timestamp.
        """
        if not self._bitget_market:
            logger.warning("[CANDLES] Bitget market client not initialized")
            return []
        
        try:
            candles = await self._bitget_market.get_candles(
                symbol=symbol,
                tf=timeframe,
                limit=limit,
            )
            
            self._stats["candle_fetches"] += 1
            logger.debug("[CANDLES_BITGET] %s %s | fetched %d candles",
                        symbol, timeframe, len(candles))
            
            return candles
            
        except Exception as e:
            logger.error("[CANDLES_BITGET] Fetch failed for %s: %s", symbol, e)
            return []
    
    async def fetch_candles_kraken(
        self,
        symbol: str,
        timeframe: str = "15m",
        limit: int = 200,
    ) -> List[Candle]:
        """
        Fetch OHLCV candles from Kraken.
        
        Args:
            symbol: Trading symbol (Bitget format accepted, e.g., "BTCUSDT")
            timeframe: Candle timeframe (e.g., "1m", "5m", "15m", "1h", "4h")
            limit: Maximum number of candles
            
        Returns:
            List of Candle objects, sorted ascending by timestamp.
        """
        if not self._kraken_market:
            logger.warning("[CANDLES] Kraken market client not initialized")
            return []
        
        try:
            candles = await self._kraken_market.get_candles(
                symbol=symbol,
                tf=timeframe,
                limit=limit,
            )
            
            self._stats["candle_fetches"] += 1
            logger.debug("[CANDLES_KRAKEN] %s %s | fetched %d candles",
                        symbol, timeframe, len(candles))
            
            return candles
            
        except Exception as e:
            logger.error("[CANDLES_KRAKEN] Fetch failed for %s: %s", symbol, e)
            return []
    
    async def fetch_candles_mt5(
        self,
        symbol: str,
        timeframe: str = "15m",
        limit: int = 200,
    ) -> List[Candle]:
        """Fetch OHLCV candles from MetaTrader 5."""
        if not self._mt5_executor:
            return []
        try:
            raw = await self._mt5_executor.get_candles(symbol=symbol, timeframe=timeframe, limit=limit)
            self._stats["candle_fetches"] += 1
            return [
                Candle(
                    timestamp=int(c["timestamp"]),
                    open=float(c["open"]),
                    high=float(c["high"]),
                    low=float(c["low"]),
                    close=float(c["close"]),
                    volume=float(c.get("volume", 0.0)),
                )
                for c in (raw or [])
            ]
        except Exception as e:
            logger.error("[CANDLES_MT5] %s fetch failed: %s", symbol, e)
            return []

    async def fetch_candles_all(
        self,
        symbol: str,
        timeframe: str = "15m",
        limit: int = 200,
    ) -> Dict[BrokerType, List[Candle]]:
        """
        Fetch candles from all active brokers in parallel.
        
        Args:
            symbol: Trading symbol
            timeframe: Candle timeframe
            limit: Maximum candles per broker
            
        Returns:
            Dict mapping broker to list of candles.
        """
        tasks = []
        brokers = []
        
        if self._bitget_market:
            tasks.append(self.fetch_candles_bitget(symbol, timeframe, limit))
            brokers.append(BrokerType.BITGET)
        
        if self._kraken_market:
            tasks.append(self.fetch_candles_kraken(symbol, timeframe, limit))
            brokers.append(BrokerType.KRAKEN)

        if self._mt5_executor and self.config.mt5_enabled:
            tasks.append(self.fetch_candles_mt5(symbol, timeframe, limit))
            brokers.append(BrokerType.MT5)
        
        if not tasks:
            return {}
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        return {
            broker: (result if not isinstance(result, Exception) else [])
            for broker, result in zip(brokers, results)
        }
    
    # ──────────────────────────────────────────────────────────────────────────
    #  SIGNAL COMPUTATION
    # ──────────────────────────────────────────────────────────────────────────
    
    def compute_signals(
        self,
        symbol: str,
        candles: List[Candle],
        broker: BrokerType,
    ) -> Optional[Signal]:
        """
        Compute trading signal from candle data.
        
        Uses either the custom strategy callback or a default strategy.
        
        Args:
            symbol: Trading symbol
            candles: Historical candles
            broker: Source broker for the candles
            
        Returns:
            Signal object if entry/exit condition met, None otherwise.
        """
        if not candles or len(candles) < 20:
            return None
        
        # Use custom strategy if provided
        if self.strategy_callback:
            try:
                signal = self.strategy_callback(symbol, candles, broker)
                if signal:
                    self._stats["signals_generated"] += 1
                return signal
            except Exception as e:
                logger.error("[SIGNAL] Strategy callback error: %s", e)
                return None

        # Unification guard: NEVER silently fall back to placeholder SMA20/50 in production
        if os.getenv("ALLOW_PLACEHOLDER_SMA_STRATEGY", "false").strip().lower() not in ("1", "true", "yes"):
            logger.warning(
                "[SIGNAL] Refusing placeholder SMA20/50 strategy on %s (%s): "
                "strategy_callback is None (SMC V3 orchestrator is the single strategic authority).",
                symbol, broker.value,
            )
            return None

        # Explicit opt-in only (legacy/testing)
        return self._default_strategy(symbol, candles, broker)
    
    def _default_strategy(
        self,
        symbol: str,
        candles: List[Candle],
        broker: BrokerType,
    ) -> Optional[Signal]:
        """
        Default strategy: Simple MA crossover with trend confirmation.
        
        This is a placeholder - replace with your actual strategy.
        """
        if len(candles) < 50:
            return None
        
        # Calculate SMAs
        closes = [c.close for c in candles]
        sma_20 = sum(closes[-20:]) / 20
        sma_50 = sum(closes[-50:]) / 50
        
        current_price = candles[-1].close
        prev_close = candles[-2].close
        
        # Simple crossover detection
        # Long: Price crosses above SMA20, SMA20 > SMA50
        # Short: Price crosses below SMA20, SMA20 < SMA50
        
        signal_type = SignalType.NO_SIGNAL
        
        if prev_close < sma_20 and current_price > sma_20 and sma_20 > sma_50:
            signal_type = SignalType.LONG
        elif prev_close > sma_20 and current_price < sma_20 and sma_20 < sma_50:
            signal_type = SignalType.SHORT
        
        if signal_type == SignalType.NO_SIGNAL:
            return None
        
        # Calculate stop/take profit
        atr = self._calculate_atr(candles, 14)
        stop_distance = atr * 1.5
        tp_distance = atr * 2.5
        
        if signal_type == SignalType.LONG:
            stop_loss = current_price - stop_distance
            take_profit = current_price + tp_distance
        else:
            stop_loss = current_price + stop_distance
            take_profit = current_price - tp_distance
        
        signal = Signal(
            symbol=symbol,
            signal_type=signal_type,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=self.config.default_leverage,
            reason=f"MA crossover ({broker.value})",
            confidence=0.7,
        )
        
        self._stats["signals_generated"] += 1
        logger.info("[SIGNAL] %s | %s @ %.2f | SL=%.2f TP=%.2f | broker=%s",
                   symbol, signal_type.value, current_price, stop_loss, take_profit, broker.value)
        
        return signal
    
    def _calculate_atr(self, candles: List[Candle], period: int = 14) -> float:
        """Calculate Average True Range."""
        if len(candles) < period + 1:
            return candles[-1].high - candles[-1].low
        
        tr_list = []
        for i in range(-period, 0):
            high = candles[i].high
            low = candles[i].low
            prev_close = candles[i - 1].close
            
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_list.append(tr)
        
        return sum(tr_list) / len(tr_list)
    
    # ──────────────────────────────────────────────────────────────────────────
    #  ORDER EXECUTION
    # ──────────────────────────────────────────────────────────────────────────
    
    async def execute_order_bitget(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        """
        Execute order on Bitget.
        
        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
            side: Order side ("buy" or "sell")
            quantity: Order quantity
            order_type: "market" or "limit"
            price: Limit price (required for limit orders)
            stop_loss: Stop-loss price
            take_profit: Take-profit price
            
        Returns:
            OrderResult with execution details.
        """
        if not self._bitget_executor:
            return OrderResult(
                success=False,
                broker=BrokerType.BITGET,
                symbol=symbol,
                error="Bitget executor not initialized",
            )
        
        logger.info("[ORDER_BITGET] %s %s | qty=%.6f | type=%s",
                   side.upper(), symbol, quantity, order_type)
        
        try:
            self._stats["orders_sent"] += 1
            
            if order_type == "market":
                result = await self._bitget_executor.place_order_market(
                    symbol=symbol,
                    side=side,
                    size=str(quantity),
                )
            else:
                result = await self._bitget_executor.place_order_limit(
                    symbol=symbol,
                    side=side,
                    size=str(quantity),
                    price=str(price),
                )
            
            success = result.get("code") == "00000"
            
            if success:
                self._stats["orders_filled"] += 1
                order_id = result.get("data", {}).get("orderId", "")
                logger.info("[ORDER_BITGET] SUCCESS | order_id=%s", order_id)
                
                # Attach TP/SL if provided
                if stop_loss:
                    await self._attach_tpsl_bitget(symbol, side, stop_loss, "STOP_LOSS")
                if take_profit:
                    await self._attach_tpsl_bitget(symbol, side, take_profit, "TAKE_PROFIT")
                
                return OrderResult(
                    success=True,
                    broker=BrokerType.BITGET,
                    symbol=symbol,
                    order_id=order_id,
                )
            else:
                self._stats["orders_failed"] += 1
                error = result.get("msg", "Unknown error")
                logger.error("[ORDER_BITGET] FAILED | error=%s", error)
                return OrderResult(
                    success=False,
                    broker=BrokerType.BITGET,
                    symbol=symbol,
                    error=error,
                )
                
        except Exception as e:
            self._stats["orders_failed"] += 1
            logger.error("[ORDER_BITGET] Exception: %s", e)
            return OrderResult(
                success=False,
                broker=BrokerType.BITGET,
                symbol=symbol,
                error=str(e),
            )
    
    async def _attach_tpsl_bitget(
        self,
        symbol: str,
        side: str,
        price: float,
        plan_type: str,
    ) -> None:
        """Attach TP/SL to Bitget position."""
        try:
            hold_side = "long" if side == "buy" else "short"
            await self._bitget_executor.attach_tpsl(
                symbol=symbol,
                plan_type=plan_type.lower(),
                trigger_price=str(price),
                hold_side=hold_side,
            )
            logger.info("[TPSL_BITGET] %s attached at %.2f", plan_type, price)
        except Exception as e:
            logger.warning("[TPSL_BITGET] Failed to attach %s: %s", plan_type, e)
    
    async def execute_order_kraken(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        required_margin: Optional[float] = None,
    ) -> OrderResult:
        """
        Execute order on Kraken with balance verification.
        
        Args:
            symbol: Trading symbol (Bitget format accepted, e.g., "BTCUSDT")
            side: Order side ("buy" or "sell")
            quantity: Order quantity
            order_type: "market" or "limit"
            price: Limit price (required for limit orders)
            stop_loss: Stop-loss price
            take_profit: Take-profit price
            required_margin: Required margin in USD (will estimate if not provided)
            
        Returns:
            OrderResult with execution details.
        """
        if not self._kraken_executor:
            return OrderResult(
                success=False,
                broker=BrokerType.KRAKEN,
                symbol=symbol,
                error="Kraken executor not initialized",
            )

        reconcile_ready, reconcile_reason = self._is_kraken_reconciliation_ready()
        if not reconcile_ready:
            logger.warning(
                "[ORDER_KRAKEN] BLOCKED %s %s: %s",
                side.upper(), symbol, reconcile_reason,
            )
            return OrderResult(
                success=False,
                broker=BrokerType.KRAKEN,
                symbol=symbol,
                error=f"Entry blocked: {reconcile_reason}",
            )
        
        logger.info("[ORDER_KRAKEN] %s %s | qty=%.6f | type=%s",
                   side.upper(), symbol, quantity, order_type)
        
        # ────────────────────────────────────────────────
        # BALANCE CHECK: Verify Kraken balance is sufficient
        # ────────────────────────────────────────────────
        try:
            # Estimate required margin if not provided
            margin_to_check = required_margin
            if margin_to_check is None:
                # Rough estimate: quantity * price / leverage
                order_price = price if price else 0.0
                if order_price == 0.0:
                    # For market orders, we'd need current price
                    # Use a conservative estimate or skip check
                    margin_to_check = 0.0
                else:
                    margin_to_check = (quantity * order_price) / self._config.default_leverage
            
            if margin_to_check > 0:
                is_sufficient, available = await self._kraken_executor.check_balance_sufficient(margin_to_check)
                
                if not is_sufficient:
                    error_msg = f"INSUFFICIENT_BALANCE: required=${margin_to_check:.2f}, available=${available:.2f}"
                    logger.error("[ORDER_KRAKEN] %s", error_msg)
                    self._stats["orders_failed"] += 1
                    return OrderResult(
                        success=False,
                        broker=BrokerType.KRAKEN,
                        symbol=symbol,
                        error=error_msg,
                    )
                
                logger.info("[ORDER_KRAKEN_BALANCE] OK | required=$%.2f | available=$%.2f",
                           margin_to_check, available)
        except Exception as bal_err:
            logger.warning("[ORDER_KRAKEN_BALANCE] Check failed (proceeding): %s", bal_err)
        
        # ────────────────────────────────────────────────
        # EXECUTE ORDER (Lot D / P0-R3: Converge through central protected place_order)
        # ────────────────────────────────────────────────
        from smc_futures.smc_config import is_kraken_execution_enabled
        if not is_kraken_execution_enabled():
            logger.critical("[ORDER_KRAKEN] BLOCKED %s %s: KRAKEN_EXECUTION_ENABLED is not enabled", side.upper(), symbol)
            self._stats["orders_failed"] += 1
            return OrderResult(
                success=False,
                broker=BrokerType.KRAKEN,
                symbol=symbol,
                error="Kraken OPEN blocked: KRAKEN_EXECUTION_ENABLED is not explicitly enabled",
            )

        try:
            self._stats["orders_sent"] += 1
            direction = "long" if str(side).lower() in ("buy", "long") else "short"

            if hasattr(self._kraken_executor, "place_order"):
                if hasattr(self._kraken_executor, "_get_wrapper"):
                    # Phase 1.2: Real KrakenFuturesExecutor adapter derives side/intent/reduce_only
                    # internally and forwards **kwargs to KrakenFuturesExecutorWrapper.place_order().
                    # Do NOT pass duplicate side/intent/reduce_only keys and NEVER coerce price=None to 0.0.
                    adapter_kwargs: Dict[str, Any] = {
                        "symbol": symbol,
                        "direction": direction,
                        "entry_price": price,
                        "stop_loss": stop_loss,
                        "take_profit": take_profit,
                        "order_type": order_type,
                        "leverage": self.config.default_leverage,
                        "quantity": str(quantity),
                    }
                    if price is not None:
                        adapter_kwargs["price"] = price
                    result = await self._kraken_executor.place_order(**adapter_kwargs)
                else:
                    # Direct KrakenFuturesExecutorWrapper compatibility (e.g. unit tests)
                    result = await self._kraken_executor.place_order(
                        symbol=symbol,
                        direction=direction,
                        side=side,
                        quantity=str(quantity),
                        order_type=order_type,
                        price=price,
                        entry_price=price,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        leverage=self.config.default_leverage,
                        reduce_only=False,
                        intent="open",
                    )
            elif order_type == "market":
                result = await self._kraken_executor.place_order_market(
                    symbol=symbol,
                    side=side,
                    size=str(quantity),
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    entry_price=price,
                    reduce_only=False,
                    intent="open",
                )
            else:
                result = await self._kraken_executor.place_order_limit(
                    symbol=symbol,
                    side=side,
                    size=str(quantity),
                    price=str(price),
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reduce_only=False,
                    intent="open",
                )

            success = bool(result.get("success") is True)
            sl_confirmed = bool(result.get("sl_verified") is True or result.get("sl_attached") is True)
            if success and not sl_confirmed:
                success = False
                result["error"] = result.get("error") or "SL protection not confirmed on exchange"

            if success:
                self._stats["orders_filled"] += 1
                order_id = result.get("order_id") or result.get("data", {}).get("orderId", "")
                logger.info("[ORDER_KRAKEN] SUCCESS (SL confirmed) | order_id=%s", order_id)
                return OrderResult(
                    success=True,
                    broker=BrokerType.KRAKEN,
                    symbol=symbol,
                    order_id=order_id,
                )
            else:
                self._stats["orders_failed"] += 1
                error = result.get("error") or result.get("msg") or "Unknown error"
                logger.error("[ORDER_KRAKEN] FAILED | error=%s", error)
                return OrderResult(
                    success=False,
                    broker=BrokerType.KRAKEN,
                    symbol=symbol,
                    error=error,
                )
                
        except Exception as e:
            self._stats["orders_failed"] += 1
            logger.error("[ORDER_KRAKEN] Exception: %s", e)
            return OrderResult(
                success=False,
                broker=BrokerType.KRAKEN,
                symbol=symbol,
                error=str(e),
            )
    
    async def _attach_tpsl_kraken(
        self,
        symbol: str,
        side: str,
        price: float,
        order_type: str,
    ) -> None:
        """
        DEPRECATED (Lot D / P0-R3): Previously called nonexistent place_conditional_order().
        SL/TP attachment and exchange-side confirmation are now enforced atomically
        inside KrakenFuturesExecutorWrapper.place_order().
        """
        raise NotImplementedError(
            "Direct _attach_tpsl_kraken() is disabled: pass stop_loss/take_profit through place_order()."
        )
    
    async def execute_order_mt5(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        """Execute an order on MetaTrader 5."""
        if not self._mt5_executor:
            return OrderResult(
                success=False,
                broker=BrokerType.MT5,
                symbol=symbol,
                error="MT5 executor not initialized",
            )
        try:
            self._stats["orders_sent"] += 1
            res = await self._mt5_executor.place_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reduce_only=reduce_only,
            )
            if res.get("success"):
                self._stats["orders_filled"] += 1
                return OrderResult(
                    success=True,
                    broker=BrokerType.MT5,
                    symbol=str(res.get("symbol") or symbol),
                    order_id=str(res.get("order_id") or ""),
                    filled_price=res.get("price"),
                    filled_qty=res.get("filled_qty"),
                )
            self._stats["orders_failed"] += 1
            return OrderResult(
                success=False,
                broker=BrokerType.MT5,
                symbol=symbol,
                error=str(res.get("error") or res.get("code") or "MT5 order failed"),
            )
        except Exception as e:
            self._stats["orders_failed"] += 1
            logger.error("[ORDER_MT5] Exception: %s", e)
            return OrderResult(
                success=False,
                broker=BrokerType.MT5,
                symbol=symbol,
                error=str(e),
            )

    async def execute_order_all_brokers(
        self,
        symbol: str,
        side: str,
        quantity_per_broker: float,
        order_type: str = "market",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict[BrokerType, OrderResult]:
        """
        Execute order on all active brokers simultaneously.
        
        Args:
            symbol: Trading symbol
            side: Order side ("buy" or "sell")
            quantity_per_broker: Quantity to trade on each broker
            order_type: "market" or "limit"
            price: Limit price
            stop_loss: Stop-loss price
            take_profit: Take-profit price
            
        Returns:
            Dict mapping broker to OrderResult.
        """
        tasks = []
        brokers = []
        
        if self._bitget_executor:
            tasks.append(self.execute_order_bitget(
                symbol, side, quantity_per_broker, order_type, price, stop_loss, take_profit
            ))
            brokers.append(BrokerType.BITGET)
        
        if self._kraken_executor:
            tasks.append(self.execute_order_kraken(
                symbol, side, quantity_per_broker, order_type, price, stop_loss, take_profit
            ))
            brokers.append(BrokerType.KRAKEN)

        if self._mt5_executor and self.config.mt5_enabled:
            tasks.append(self.execute_order_mt5(
                symbol, side, quantity_per_broker, order_type, price, stop_loss, take_profit
            ))
            brokers.append(BrokerType.MT5)
        
        if not tasks:
            return {}
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        return {
            broker: (
                result if not isinstance(result, Exception)
                else OrderResult(success=False, broker=broker, symbol=symbol, error=str(result))
            )
            for broker, result in zip(brokers, results)
        }
    
    # ──────────────────────────────────────────────────────────────────────────
    #  MAIN STRATEGY LOOP
    # ──────────────────────────────────────────────────────────────────────────
    
    async def dispatch_canonical_smc_signal(
        self,
        signal: Signal,
        *,
        signal_id: str,
        brokers: Optional[List[BrokerType]] = None,
    ) -> Dict[BrokerType, str]:
        """
        Single-Authority Execution Router: dispatch a canonical SMC V3 signal
        produced by `orchestrator.py` to one or more authorized brokers with
        strict `signal_id` deduplication (prevents double execution).
        """
        if not hasattr(self, "_dispatched_signal_ids"):
            self._dispatched_signal_ids: set = set()
            self._dispatched_signal_order: List[str] = []

        sig_key = str(signal_id or "").strip()
        if not sig_key:
            logger.error("[SMC_ROUTER] Refused signal without canonical signal_id on %s", signal.symbol)
            return {}

        if sig_key in self._dispatched_signal_ids:
            logger.warning("[SMC_ROUTER] Duplicate signal_id=%s on %s — skipped", sig_key, signal.symbol)
            return {}

        self._dispatched_signal_ids.add(sig_key)
        self._dispatched_signal_order.append(sig_key)
        if len(self._dispatched_signal_order) > 5000:
            old = self._dispatched_signal_order.pop(0)
            self._dispatched_signal_ids.discard(old)

        self._stats["signals_generated"] += 1

        target_brokers: List[BrokerType] = []
        if brokers is not None:
            target_brokers = list(brokers)
        else:
            if self._kraken_executor and self.config.kraken_enabled:
                target_brokers.append(BrokerType.KRAKEN)
            if self._mt5_executor and self.config.mt5_enabled:
                target_brokers.append(BrokerType.MT5)
            if self._bitget_executor and self.config.bitget_enabled:
                target_brokers.append(BrokerType.BITGET)

        outcomes: Dict[BrokerType, str] = {}
        for b in target_brokers:
            if b == BrokerType.MT5 and not (self._mt5_executor and self.config.mt5_enabled):
                outcomes[b] = "skipped_disabled"
                continue
            if b == BrokerType.KRAKEN and not (self._kraken_executor and self.config.kraken_enabled):
                outcomes[b] = "skipped_disabled"
                continue
            if b == BrokerType.BITGET and not (self._bitget_executor and self.config.bitget_enabled):
                outcomes[b] = "skipped_disabled"
                continue
            # Ensure balance is available before per-broker risk sizing
            if b not in self._balances:
                if b == BrokerType.MT5:
                    await self.fetch_balance_mt5()
                elif b == BrokerType.KRAKEN:
                    await self.fetch_balance_kraken()
                elif b == BrokerType.BITGET:
                    await self.fetch_balance_bitget()
            await self._execute_signal_single(b, signal)
            outcomes[b] = "dispatched"

        logger.info(
            "[SMC_ROUTER] Dispatched canonical signal_id=%s | %s %s | outcomes=%s",
            sig_key, signal.symbol, signal.signal_type.value,
            {k.value: v for k, v in outcomes.items()},
        )
        return outcomes

    async def run_strategy_loop(self) -> None:
        """
        Main strategy loop — runs trading logic on all brokers simultaneously.
        Fail-closed: refuses to start without an explicit strategy_callback so
        the placeholder SMA20/50 strategy can never execute in production.
        """
        if self.strategy_callback is None and os.getenv("ALLOW_PLACEHOLDER_SMA_STRATEGY", "false").strip().lower() not in ("1", "true", "yes"):
            self._running = False
            msg = (
                "MULTI_BROKER_STRATEGY_CALLBACK_REQUIRED: Refusing to start MultiBrokerEngine "
                "standalone loop without strategy_callback (placeholder SMA20/50 disabled; "
                "SMC V3 orchestrator.py is the single active strategic authority)."
            )
            logger.critical("[MULTI_BROKER] %s", msg)
            raise RuntimeError(msg)
        logger.info("=" * 60)
        logger.info("[MULTI_BROKER] Starting LIVE trading loop")
        logger.info("[MULTI_BROKER] Symbols: %s", self.config.symbols)
        logger.info("[MULTI_BROKER] Loop interval: %ds", self.config.loop_interval_sec)
        logger.info("=" * 60)
        
        if self._kraken_executor:
            await self.reconcile_kraken_on_startup()
        
        self._running = True
        loop_count = 0
        
        while self._running:
            loop_count += 1
            loop_start = time.time()
            
            try:
                logger.info("[LOOP_%d] Starting iteration", loop_count)
                
                # 1. Refresh balances periodically
                await self._maybe_refresh_balances()
                
                # 2. Process each symbol
                for symbol in self.config.symbols:
                    await self._process_symbol(symbol, loop_count)
                
                # 3. Log loop statistics
                loop_duration = time.time() - loop_start
                logger.info("[LOOP_%d] Completed in %.2fs | signals=%d orders=%d/%d",
                           loop_count, loop_duration,
                           self._stats["signals_generated"],
                           self._stats["orders_filled"],
                           self._stats["orders_sent"])
                
            except Exception as e:
                logger.error("[LOOP_%d] Error: %s", loop_count, e, exc_info=True)
                await self._handle_reconnection()
            
            # Sleep until next iteration
            elapsed = time.time() - loop_start
            sleep_time = max(0, self.config.loop_interval_sec - elapsed)
            
            if sleep_time > 0:
                logger.debug("[LOOP] Sleeping %.1fs until next iteration", sleep_time)
                await asyncio.sleep(sleep_time)
        
        logger.info("[MULTI_BROKER] Strategy loop terminated")
    
    async def _maybe_refresh_balances(self) -> None:
        """Refresh balances if interval has passed."""
        now = time.time()
        
        for broker in [BrokerType.BITGET, BrokerType.KRAKEN, BrokerType.MT5]:
            last_check = self._last_balance_check.get(broker, 0)
            if now - last_check >= self.config.balance_check_interval_sec:
                if broker == BrokerType.BITGET and self._bitget_executor:
                    await self.fetch_balance_bitget()
                elif broker == BrokerType.KRAKEN and self._kraken_executor:
                    await self.fetch_balance_kraken()
                elif broker == BrokerType.MT5 and self._mt5_executor:
                    await self.fetch_balance_mt5()
    
    async def _process_symbol(self, symbol: str, loop_count: int) -> None:
        """
        Process a single symbol across all brokers.
        
        Args:
            symbol: Trading symbol
            loop_count: Current loop iteration
        """
        logger.debug("[PROCESS] %s | Starting", symbol)
        
        # Fetch candles from all brokers in parallel
        candles_by_broker = await self.fetch_candles_all(symbol, "15m", 200)
        
        if not candles_by_broker:
            logger.warning("[PROCESS] %s | No candle data available", symbol)
            return
        
        # Generate signals for each broker's data
        signals: Dict[BrokerType, Optional[Signal]] = {}
        
        for broker, candles in candles_by_broker.items():
            if candles:
                signal = self.compute_signals(symbol, candles, broker)
                signals[broker] = signal
        
        # Execute on brokers with valid entry signals
        if is_multi_broker_mirror_enabled():
            # Mirror mode: execute AT MOST ONE mirrored signal per symbol per cycle
            # (prevents 2x duplicate orders when both Kraken & MT5 candles emit a signal)
            preferred_order = [BrokerType.KRAKEN, BrokerType.MT5, BrokerType.BITGET]
            chosen: Optional[Tuple[BrokerType, Signal]] = None
            for b in preferred_order:
                sig = signals.get(b)
                if sig and sig.is_entry:
                    chosen = (b, sig)
                    break
            if chosen is not None:
                await self._execute_signal(chosen[0], chosen[1])
        else:
            for broker, signal in signals.items():
                if signal and signal.is_entry:
                    await self._execute_signal(broker, signal)
    
    async def _execute_signal(self, broker: BrokerType, signal: Signal) -> None:
        """
        Execute a trading signal on a specific broker.
        
        If MULTI_BROKER_MIRROR=true, duplicates the order to ALL connected brokers.
        
        Args:
            broker: Primary target broker (for source identification)
            signal: Signal to execute
        """
        # Check if mirror mode is enabled
        mirror_mode = is_multi_broker_mirror_enabled()
        
        if mirror_mode:
            logger.info("[MIRROR_MODE] Signal %s %s will be executed on ALL connected brokers",
                       signal.symbol, signal.signal_type.value)
            await self._execute_signal_mirrored(signal)
        else:
            await self._execute_signal_single(broker, signal)
    
    async def _execute_signal_mirrored(self, signal: Signal) -> None:
        """
        Execute signal on ALL connected brokers (mirror mode).
        
        Best-effort: if one broker fails, others continue.
        """
        tasks = []
        brokers_to_execute = []
        
        # Execute on Bitget if available
        if self._bitget_executor and self.config.bitget_enabled:
            brokers_to_execute.append(BrokerType.BITGET)
            tasks.append(self._execute_signal_single(BrokerType.BITGET, signal))
        
        # Execute on Kraken if available and enabled
        if self._kraken_executor and self.config.kraken_enabled:
            status = self._broker_status.get(BrokerType.KRAKEN)
            if status and status.connected:
                brokers_to_execute.append(BrokerType.KRAKEN)
                tasks.append(self._execute_signal_single(BrokerType.KRAKEN, signal))
            else:
                logger.warning("[MIRROR_MODE] Kraken not connected, skipping (partial_fill_brokers)")

        # Execute on MT5 if available and enabled
        if self._mt5_executor and self.config.mt5_enabled:
            status = self._broker_status.get(BrokerType.MT5)
            if status and status.connected:
                brokers_to_execute.append(BrokerType.MT5)
                tasks.append(self._execute_signal_single(BrokerType.MT5, signal))
            else:
                logger.warning("[MIRROR_MODE] MT5 not connected, skipping")
        
        if not tasks:
            logger.error("[MIRROR_MODE] No brokers available for execution!")
            return
        
        logger.info("[MIRROR_MODE] Executing %s %s on brokers: %s",
                   signal.symbol, signal.signal_type.value,
                   [b.value for b in brokers_to_execute])
        
        # Execute concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Log results
        success_count = 0
        for i, result in enumerate(results):
            broker = brokers_to_execute[i]
            if isinstance(result, Exception):
                logger.error("[MIRROR_MODE] %s | %s execution failed: %s",
                           signal.symbol, broker.value, result)
            else:
                success_count += 1
        
        if success_count == 0:
            logger.error("[MIRROR_MODE] ALL brokers failed for %s! status=all_failed",
                        signal.symbol)
        elif success_count < len(brokers_to_execute):
            logger.warning("[MIRROR_MODE] %s | partial_fill_brokers: %d/%d succeeded",
                          signal.symbol, success_count, len(brokers_to_execute))
        else:
            logger.info("[MIRROR_MODE] %s | SUCCESS on all %d brokers",
                       signal.symbol, success_count)
    
    @staticmethod
    def _calculate_risk_based_quantity(
        *,
        available_balance: float,
        risk_per_trade_pct: float,
        entry_price: Optional[float],
        stop_loss: Optional[float],
        leverage: int,
        symbol: str,
        broker: BrokerType,
    ) -> Tuple[bool, float, float, Optional[str]]:
        """
        Phase 1.8 (P1): Pure risk-based position sizing without leverage risk multiplication.

        Mathematical invariant (Kraken linear contracts, multiplier = 1.0):
          risk_amount = available_balance * risk_per_trade_pct
          risk_per_unit = abs(entry_price - stop_loss)
          raw_qty = risk_amount / risk_per_unit      (NEVER multiplied by leverage!)
          required_margin = (qty * entry_price) / leverage

        Returns:
            (ok, quantity, required_margin, rejection_reason)
        """
        import math

        if (
            available_balance is None
            or isinstance(available_balance, bool)
            or not math.isfinite(float(available_balance))
            or float(available_balance) <= 0
        ):
            return False, 0.0, 0.0, "INVALID_BALANCE"

        if (
            risk_per_trade_pct is None
            or isinstance(risk_per_trade_pct, bool)
            or not math.isfinite(float(risk_per_trade_pct))
            or float(risk_per_trade_pct) <= 0
        ):
            return False, 0.0, 0.0, "INVALID_RISK_PCT"

        if (
            entry_price is None
            or isinstance(entry_price, bool)
            or not math.isfinite(float(entry_price))
            or float(entry_price) <= 0
        ):
            return False, 0.0, 0.0, "INVALID_ENTRY_PRICE"

        if (
            stop_loss is None
            or isinstance(stop_loss, bool)
            or not math.isfinite(float(stop_loss))
            or float(stop_loss) <= 0
        ):
            return False, 0.0, 0.0, "INVALID_STOP_LOSS"

        # Phase 1.9: Strict fail-closed leverage validation (NEVER silently coerce to 1x)
        if leverage is None or isinstance(leverage, bool):
            return False, 0.0, 0.0, "INVALID_LEVERAGE"
        try:
            lev_f = float(leverage)
        except (TypeError, ValueError):
            return False, 0.0, 0.0, "INVALID_LEVERAGE"
        if not math.isfinite(lev_f) or lev_f <= 0 or not lev_f.is_integer():
            return False, 0.0, 0.0, "INVALID_LEVERAGE"
        eff_leverage = int(lev_f)

        entry_f = float(entry_price)
        sl_f = float(stop_loss)
        bal_f = float(available_balance)
        risk_amount = bal_f * float(risk_per_trade_pct)
        price_diff = abs(entry_f - sl_f)

        if not math.isfinite(price_diff) or price_diff <= 0:
            return False, 0.0, 0.0, "INVALID_SL_DISTANCE"

        contract_multiplier = 1.0
        if broker == BrokerType.KRAKEN:
            from backend.execution.kraken_futures_executor import (
                _floor_to_precision,
                _instrument_qty_rules,
                to_kraken_symbol,
            )
            from backend.kraken_contracts import KRAKEN_CONTRACT_SPECS

            kraken_sym = to_kraken_symbol(symbol)
            spec = KRAKEN_CONTRACT_SPECS.get(kraken_sym)
            if not isinstance(spec, dict):
                return False, 0.0, 0.0, f"UNSUPPORTED_SYMBOL: {kraken_sym}"
            raw_mult = spec.get("contract_size")
            if raw_mult is None or isinstance(raw_mult, bool):
                return False, 0.0, 0.0, "INVALID_CONTRACT_MULTIPLIER"
            try:
                contract_multiplier = float(raw_mult)
            except (TypeError, ValueError):
                return False, 0.0, 0.0, "INVALID_CONTRACT_MULTIPLIER"
            if not math.isfinite(contract_multiplier) or contract_multiplier <= 0:
                return False, 0.0, 0.0, "INVALID_CONTRACT_MULTIPLIER"

        risk_per_unit = price_diff * contract_multiplier
        if not math.isfinite(risk_per_unit) or risk_per_unit <= 0:
            return False, 0.0, 0.0, "INVALID_SL_DISTANCE"

        # Risk-based quantity: NEVER multiply by leverage (leverage affects margin, not SL loss budget)
        raw_qty = risk_amount / risk_per_unit

        if broker == BrokerType.KRAKEN:
            qty = _floor_to_precision(kraken_sym, raw_qty, force_min=False)
            _prec, min_qty = _instrument_qty_rules(kraken_sym)
            if qty < min_qty:
                return (
                    False,
                    0.0,
                    0.0,
                    f"BELOW_MIN_RISK_BUDGET: floored qty={qty} < min_qty={min_qty} "
                    f"(min_qty_risk=${min_qty * risk_per_unit:.4f} > budget=${risk_amount:.4f})",
                )
        elif broker == BrokerType.MT5:
            # Cap MT5 quantity to MT5_MAX_LOT_SIZE before margin check & use MT5 leverage
            mt5_max_lot = float(os.getenv("MT5_MAX_LOT_SIZE", "0.10"))
            qty = math.floor(raw_qty * 1e6) / 1e6
            if mt5_max_lot > 0:
                qty = min(qty, mt5_max_lot)
            if qty <= 0:
                return False, 0.0, 0.0, "QUANTITY_ZERO_AFTER_ROUNDING"
            eff_leverage = max(eff_leverage, int(os.getenv("MT5_LEVERAGE", "100")))
        else:
            qty = math.floor(raw_qty * 1e6) / 1e6
            if qty <= 0:
                return False, 0.0, 0.0, "QUANTITY_ZERO_AFTER_ROUNDING"

        actual_risk = qty * risk_per_unit
        if actual_risk > risk_amount + 1e-6:
            return (
                False,
                0.0,
                0.0,
                f"RISK_EXCEEDS_BUDGET: actual_risk=${actual_risk:.4f} > budget=${risk_amount:.4f}",
            )

        required_margin = (qty * entry_f * contract_multiplier) / eff_leverage
        if required_margin > bal_f + 1e-6:
            return (
                False,
                0.0,
                required_margin,
                f"INSUFFICIENT_MARGIN: required=${required_margin:.4f} > available=${bal_f:.4f} (leverage={eff_leverage}x)",
            )

        return True, float(qty), float(required_margin), None

    async def _execute_signal_single(self, broker: BrokerType, signal: Signal) -> None:
        """
        Execute a trading signal on a single broker.
        
        Args:
            broker: Target broker
            signal: Signal to execute
        """
        # Per-(broker, symbol) cooldown guard to prevent stacking orders every loop
        cooldown_sec = float(os.getenv("TRADE_COOLDOWN_SEC", "300"))
        if not hasattr(self, "_last_order_ts"):
            self._last_order_ts: Dict[Tuple[BrokerType, str], float] = {}
        cd_key = (broker, signal.symbol)
        now_ts = time.time()
        last_ts = self._last_order_ts.get(cd_key, 0.0)
        if cooldown_sec > 0 and (now_ts - last_ts) < cooldown_sec:
            logger.info(
                "[EXECUTE] %s | Cooldown active on %s (%.0fs remaining) — skipping duplicate entry",
                signal.symbol, broker.value, cooldown_sec - (now_ts - last_ts),
            )
            return

        if broker == BrokerType.KRAKEN:
            reconcile_ready, reconcile_reason = self._is_kraken_reconciliation_ready()
            if not reconcile_ready:
                logger.warning(
                    "[EXECUTE] %s | Blocked Kraken signal before reconciliation completion: %s",
                    signal.symbol, reconcile_reason,
                )
                return

        # Check balance
        balance = self._balances.get(broker)
        if not balance or balance.available < self.config.min_balance_usd:
            logger.warning("[EXECUTE] %s | Insufficient balance on %s (%.2f < %.2f)",
                          signal.symbol, broker.value,
                          balance.available if balance else 0,
                          self.config.min_balance_usd)
            return

        # Phase 1.8 (P1): Strict risk-based position sizing (no leverage risk multiplication)
        effective_leverage = signal.leverage or self.config.default_leverage
        size_ok, quantity, required_margin, size_err = self._calculate_risk_based_quantity(
            available_balance=balance.available,
            risk_per_trade_pct=self.config.risk_per_trade_pct,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            leverage=effective_leverage,
            symbol=signal.symbol,
            broker=broker,
        )
        if not size_ok:
            logger.error(
                "[EXECUTE] %s | Refused %s signal on %s: %s",
                signal.symbol, signal.signal_type.value, broker.value, size_err,
            )
            return

        risk_amount = balance.available * self.config.risk_per_trade_pct
        logger.info(
            "[EXECUTE] %s | %s %s @ %.2f | qty=%.6f | risk=$%.2f | margin=$%.2f (%dx)",
            signal.symbol, signal.signal_type.value, broker.value,
            signal.entry_price or 0, quantity, risk_amount, required_margin, effective_leverage,
        )

        # Execute order
        if broker == BrokerType.BITGET:
            result = await self.execute_order_bitget(
                symbol=signal.symbol,
                side=signal.side,
                quantity=quantity,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            )
        elif broker == BrokerType.MT5:
            result = await self.execute_order_mt5(
                symbol=signal.symbol,
                side=signal.side,
                quantity=quantity,
                price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            )
        else:
            result = await self.execute_order_kraken(
                symbol=signal.symbol,
                side=signal.side,
                quantity=quantity,
                price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                required_margin=required_margin,
            )
        
        if result.success:
            self._last_order_ts[cd_key] = time.time()
            logger.info("[EXECUTE] %s | Order successful on %s | order_id=%s",
                       signal.symbol, broker.value, result.order_id)
        else:
            logger.error("[EXECUTE] %s | Order failed on %s | error=%s",
                        signal.symbol, broker.value, result.error)
    
    async def _handle_reconnection(self) -> None:
        """Handle broker reconnection on failures with mandatory Kraken REST reconciliation (P2-1, serialized)."""
        async with self._reconnect_lock:
            await self._handle_reconnection_locked()

    async def _handle_reconnection_locked(self) -> None:
        """Internal serialized reconnection handler (caller holds self._reconnect_lock)."""
        logger.warning("[RECONNECT] Attempting to reconnect brokers...")

        wrapper = _resolve_kraken_wrapper(self._kraken_executor)
        await self.stop_kraken_book_feed()
        if wrapper is not None:
            if hasattr(wrapper, "on_ws_disconnect"):
                wrapper.on_ws_disconnect("ENGINE_RECONNECTION_REQUIRED")
            else:
                wrapper._global_entry_lock = True
                wrapper._startup_reconciliation_completed = False
            _KRAKEN_RECONCILE_REPORTS.pop(wrapper, None)
            self._kraken_reconciliation_done = False

        for attempt in range(self.config.max_reconnect_attempts):
            await asyncio.sleep(self.config.reconnect_delay_sec)

            try:
                # Re-initialize failed connections
                if self._kraken_executor:
                    status = self._broker_status.get(BrokerType.KRAKEN)
                    if status and not status.connected:
                        await self._kraken_executor.connect()
                        self._broker_status[BrokerType.KRAKEN].connected = True
                        ws_client = getattr(self._kraken_executor, "_client", None)
                        if wrapper is not None and ws_client is not None and hasattr(wrapper, "bind_ws_client"):
                            wrapper.bind_ws_client(ws_client)
                        logger.info("[RECONNECT] Kraken reconnected — reconnecting WS book feed & running mandatory REST reconciliation")
                        if wrapper is not None:
                            wrapper._ws_connected = True
                        book_ok = await self.start_kraken_book_feed()
                        if not book_ok:
                            logger.critical("[RECONNECT] Kraken WS book feed reconnection failed — OPEN MARKET remains blocked")
                        reconcile_report = await self.run_kraken_startup_reconciliation()
                        if reconcile_report.get("startup_secure") is not True:
                            logger.critical(
                                "[RECONNECT] Kraken post-reconnect reconciliation NOT secure (status=%s); global lock preserved.",
                                reconcile_report.get("status"),
                            )
                            return

                logger.info("[RECONNECT] Reconnection successful after %d attempts", attempt + 1)
                return

            except Exception as e:
                logger.warning("[RECONNECT] Attempt %d failed: %s", attempt + 1, e)

        logger.error("[RECONNECT] Failed after %d attempts — keeping Kraken global entry lock active", self.config.max_reconnect_attempts)
    
    # ──────────────────────────────────────────────────────────────────────────
    #  STATUS & DIAGNOSTICS
    # ──────────────────────────────────────────────────────────────────────────
    
    def get_status(self) -> Dict[str, Any]:
        """Get engine status and statistics."""
        return {
            "running": self._running,
            "brokers": {
                broker.value: {
                    "connected": status.connected,
                    "last_heartbeat": status.last_heartbeat.isoformat() if status.last_heartbeat else None,
                    "error": status.error,
                    "balance": {
                        "available": self._balances.get(broker, Balance(broker, 0, 0)).available,
                        "total": self._balances.get(broker, Balance(broker, 0, 0)).total,
                        "unrealized_pnl": self._balances.get(broker, Balance(broker, 0, 0)).unrealized_pnl,
                        "currency": self._balances.get(broker, Balance(broker, 0, 0)).currency,
                    } if broker in self._balances else None,
                }
                for broker, status in self._broker_status.items()
            },
            "statistics": {
                "total_signals": self._stats.get("signals_generated", 0),
                "total_orders": self._stats.get("orders_sent", 0),
                "successful_orders": self._stats.get("orders_filled", 0),
                "failed_orders": self._stats.get("orders_failed", 0),
            },
            "config": {
                "symbols": self.config.symbols,
                "loop_interval_sec": self.config.loop_interval_sec,
                "risk_per_trade_pct": self.config.risk_per_trade_pct,
            },
        }
    
    def get_balances(self) -> Dict[str, Balance]:
        """Get all cached balances."""
        return {broker.value: balance for broker, balance in self._balances.items()}


# ══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def create_engine_from_env() -> MultiBrokerEngine:
    """
    Create and initialize engine from environment variables.
    
    Returns:
        Initialized MultiBrokerEngine ready for trading.
    """
    config = MultiBrokerConfig.from_env()
    engine = MultiBrokerEngine(config)
    await engine.initialize()
    return engine


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    """Main entry point for standalone execution."""
    import signal as sig
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    
    engine = await create_engine_from_env()
    
    # Graceful shutdown handler
    def shutdown_handler(signum, frame):
        logger.info("[MAIN] Shutdown signal received")
        asyncio.create_task(engine.shutdown())
    
    sig.signal(sig.SIGINT, shutdown_handler)
    sig.signal(sig.SIGTERM, shutdown_handler)
    
    try:
        await engine.run_strategy_loop()
    finally:
        await engine.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
