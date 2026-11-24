"""
Mirror Execution Service — Simultaneous Multi-Broker Order Execution
═══════════════════════════════════════════════════════════════════════

Execute trades on multiple brokers simultaneously with:
  - Concurrent order dispatch (asyncio.gather)
  - Per-broker error capture
  - Optional strict mode (rollback on partial failure)
  - Unified trade intent → per-broker results

Configuration via environment variables:
  - MIRROR_TRADES=true       → Enable mirror execution
  - MIRROR_STRICT=false      → If true, cancel on partial failure
  - BASE_RISK_PCT=1.0        → Default risk percentage
  - MIN_NOTIONAL_USDT=5.0    → Minimum notional value

Author: SMC Trading Bot Framework
"""

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Callable

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("smarttrade.mirror_executor")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

def is_mirror_enabled() -> bool:
    """Check if mirror trading is enabled."""
    return os.getenv("MIRROR_TRADES", "false").lower() in ("1", "true", "yes")


def is_mirror_strict() -> bool:
    """
    Check if strict mode is enabled.
    
    Strict mode: If either broker fails, attempt to cancel/close the other.
    Non-strict: Allow partial success, record both results.
    """
    return os.getenv("MIRROR_STRICT", "false").lower() in ("1", "true", "yes")


def get_base_risk_pct() -> float:
    """Get base risk percentage (default 1.0%)."""
    return float(os.getenv("BASE_RISK_PCT", "1.0"))


def get_min_notional() -> float:
    """Get minimum notional value in USDT."""
    return float(os.getenv("MIN_NOTIONAL_USDT", "5.0"))


def get_execution_timeout() -> float:
    """Get per-broker execution timeout in seconds."""
    return float(os.getenv("MIRROR_EXEC_TIMEOUT", "30.0"))


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TradeIntent:
    """
    Unified trade intent that gets dispatched to all brokers.
    
    This is the single source of truth for a trade.
    """
    # Required fields
    symbol: str
    side: str  # "long" or "short"
    quantity: float
    entry_price: float
    stop_loss: float
    
    # Optional fields
    take_profit: Optional[float] = None
    leverage: int = 10
    order_type: str = "market"  # "market" or "limit"
    risk_pct: float = 1.0
    risk_amount: Optional[float] = None
    
    # Metadata
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    source: str = "manual"  # "manual" or "auto"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Targeting (which brokers to execute on)
    target_brokers: List[str] = field(default_factory=lambda: ["bitget", "kraken"])
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "leverage": self.leverage,
            "order_type": self.order_type,
            "risk_pct": self.risk_pct,
            "risk_amount": self.risk_amount,
            "source": self.source,
            "target_brokers": self.target_brokers,
            "timestamp": self.timestamp.isoformat(),
        }
    
    @property
    def rr_ratio(self) -> Optional[float]:
        """Calculate risk:reward ratio."""
        if not self.take_profit or not self.stop_loss:
            return None
        
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.take_profit - self.entry_price)
        
        if risk == 0:
            return None
        return round(reward / risk, 2)


@dataclass
class BrokerExecution:
    """
    Result of executing a trade on a single broker.
    """
    broker: str
    success: bool
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    fill_quantity: Optional[float] = None
    error: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None
    execution_time_ms: Optional[float] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_response: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "broker": self.broker,
            "success": self.success,
            "order_id": self.order_id,
            "fill_price": self.fill_price,
            "fill_quantity": self.fill_quantity,
            "error": self.error,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "execution_time_ms": self.execution_time_ms,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class MirrorResult:
    """
    Combined result of mirror execution across all brokers.
    """
    trade_intent_id: str
    success: bool  # True if at least one broker succeeded
    full_success: bool  # True if ALL brokers succeeded
    broker_results: Dict[str, BrokerExecution] = field(default_factory=dict)
    
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    
    total_execution_time_ms: Optional[float] = None
    strict_mode: bool = False
    rollback_attempted: bool = False
    rollback_success: Optional[bool] = None
    
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_intent_id": self.trade_intent_id,
            "success": self.success,
            "full_success": self.full_success,
            "broker_results": {k: v.to_dict() for k, v in self.broker_results.items()},
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "total_execution_time_ms": self.total_execution_time_ms,
            "strict_mode": self.strict_mode,
            "rollback_attempted": self.rollback_attempted,
            "rollback_success": self.rollback_success,
            "timestamp": self.timestamp.isoformat(),
        }
    
    @property
    def success_brokers(self) -> List[str]:
        """Get list of brokers that succeeded."""
        return [k for k, v in self.broker_results.items() if v.success]
    
    @property
    def failed_brokers(self) -> List[str]:
        """Get list of brokers that failed."""
        return [k for k, v in self.broker_results.items() if not v.success and not v.skipped]


# ══════════════════════════════════════════════════════════════════════════════
# MIRROR EXECUTION SERVICE
# ══════════════════════════════════════════════════════════════════════════════

class MirrorExecutionService:
    """
    Service for executing trades on multiple brokers simultaneously.
    
    Usage:
        service = MirrorExecutionService()
        
        # Register executors
        service.register_executor("bitget", bitget_executor)
        service.register_executor("kraken", kraken_executor)
        
        # Execute trade
        intent = TradeIntent(
            symbol="BTCUSDT",
            side="long",
            quantity=0.001,
            entry_price=50000,
            stop_loss=49000,
            take_profit=52000,
        )
        
        result = await service.execute_trade(intent)
        
        print(f"Success brokers: {result.success_brokers}")
        print(f"Failed brokers: {result.failed_brokers}")
    """
    
    def __init__(self, use_router: bool = True):
        """
        Initialize mirror execution service.
        
        Args:
            use_router: If True, delegate executor registration to BrokerRouter.
                        This ensures all execution paths share the same executors.
        """
        self._executors: Dict[str, Any] = {}
        self._enabled_brokers: set = set()
        
        # Callbacks for persistence
        self._on_trade_started: Optional[Callable] = None
        self._on_trade_completed: Optional[Callable] = None
        
        # Optional BrokerRouter integration
        self._use_router = use_router
        self._router = None
        if use_router:
            try:
                from backend.execution.broker_router import get_broker_router
                self._router = get_broker_router()
                logger.info("[MIRROR] BrokerRouter integration enabled")
            except ImportError:
                logger.warning("[MIRROR] BrokerRouter not available, using standalone mode")
                self._use_router = False
        
        logger.info(
            "[MIRROR] Service initialized | mirror_enabled=%s | strict_mode=%s | use_router=%s",
            is_mirror_enabled(), is_mirror_strict(), self._use_router
        )
    
    # ──────────────────────────────────────────────────────────────────────────
    # EXECUTOR REGISTRATION
    # ──────────────────────────────────────────────────────────────────────────
    
    def register_executor(self, broker: str, executor: Any) -> None:
        """
        Register an executor for a broker.
        
        Executor must implement:
          - execute_order(symbol, side, size, order_type, price, stop_loss, take_profit, leverage)
          - test_connection() -> Dict
        
        If BrokerRouter integration is enabled, also registers with the router.
        """
        broker = broker.lower()
        self._executors[broker] = executor
        self._enabled_brokers.add(broker)
        logger.info("[MIRROR] Registered executor: %s", broker)
        
        # Also register with BrokerRouter if available
        if self._use_router and self._router:
            try:
                self._router.register_executor(broker, executor)
                logger.info("[MIRROR] Executor also registered with BrokerRouter: %s", broker)
            except Exception as e:
                logger.warning("[MIRROR] Failed to register with BrokerRouter: %s", e)
    
    def unregister_executor(self, broker: str) -> None:
        """Unregister an executor."""
        broker = broker.lower()
        if broker in self._executors:
            del self._executors[broker]
            self._enabled_brokers.discard(broker)
            logger.info("[MIRROR] Unregistered executor: %s", broker)
    
    def get_enabled_brokers(self) -> List[str]:
        """Get list of enabled brokers with registered executors."""
        return list(self._enabled_brokers)
    
    def is_broker_enabled(self, broker: str) -> bool:
        """Check if a broker is enabled."""
        return broker.lower() in self._enabled_brokers
    
    # ──────────────────────────────────────────────────────────────────────────
    # CALLBACKS
    # ──────────────────────────────────────────────────────────────────────────
    
    def set_on_trade_started(self, callback: Callable) -> None:
        """Set callback for when trade execution starts."""
        self._on_trade_started = callback
    
    def set_on_trade_completed(self, callback: Callable) -> None:
        """Set callback for when trade execution completes."""
        self._on_trade_completed = callback
    
    # ──────────────────────────────────────────────────────────────────────────
    # MAIN EXECUTION METHOD
    # ──────────────────────────────────────────────────────────────────────────
    
    async def execute_trade(self, intent: TradeIntent) -> MirrorResult:
        """
        Execute a trade on all target brokers simultaneously.
        
        This is the SINGLE entry point for ALL order placements.
        Both manual and auto trades should call this method.
        
        Args:
            intent: TradeIntent with all trade parameters
            
        Returns:
            MirrorResult with per-broker execution details
        """
        start_time = datetime.now(timezone.utc)
        
        logger.info(
            "[MIRROR] ════════════════════════════════════════════════════════════"
        )
        logger.info(
            "[MIRROR] DISPATCH STARTED | intent_id=%s | %s %s | qty=%s",
            intent.id, intent.side.upper(), intent.symbol, intent.quantity
        )
        logger.info(
            "[MIRROR] target_brokers=%s | enabled=%s | mirror=%s | strict=%s",
            intent.target_brokers, list(self._enabled_brokers),
            is_mirror_enabled(), is_mirror_strict()
        )
        
        # Callback: trade started
        if self._on_trade_started:
            try:
                await self._on_trade_started(intent)
            except Exception as e:
                logger.warning("[MIRROR] on_trade_started callback error: %s", e)
        
        # Determine which brokers to execute on
        target_brokers = intent.target_brokers if intent.target_brokers else ["bitget"]
        
        # If mirror mode is enabled, always use both brokers
        if is_mirror_enabled():
            target_brokers = ["bitget", "kraken"]
        
        # Filter to only enabled brokers
        active_brokers = [b for b in target_brokers if b in self._enabled_brokers]
        
        if not active_brokers:
            logger.error("[MIRROR] No active brokers available!")
            return MirrorResult(
                trade_intent_id=intent.id,
                success=False,
                full_success=False,
                failed_count=len(target_brokers),
            )
        
        # Execute on all brokers concurrently
        broker_results: Dict[str, BrokerExecution] = {}
        
        # Create tasks for concurrent execution
        tasks = []
        for broker in active_brokers:
            task = self._execute_on_broker(broker, intent)
            tasks.append((broker, task))
        
        # Add skipped entries for brokers not in enabled list
        for broker in target_brokers:
            if broker not in active_brokers:
                broker_results[broker] = BrokerExecution(
                    broker=broker,
                    success=False,
                    skipped=True,
                    skip_reason=f"{broker} executor not registered/available",
                )
        
        # Execute all tasks concurrently with timeout
        exec_timeout = get_execution_timeout()
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*[t[1] for t in tasks], return_exceptions=True),
                timeout=exec_timeout
            )
            
            for i, (broker, _) in enumerate(tasks):
                result = results[i]
                if isinstance(result, Exception):
                    broker_results[broker] = BrokerExecution(
                        broker=broker,
                        success=False,
                        error=f"Exception: {result}",
                    )
                else:
                    broker_results[broker] = result
                    
        except asyncio.TimeoutError:
            logger.error("[MIRROR] Execution timeout after %ss", exec_timeout)
            for broker, _ in tasks:
                if broker not in broker_results:
                    broker_results[broker] = BrokerExecution(
                        broker=broker,
                        success=False,
                        error=f"Timeout after {exec_timeout}s",
                    )
        
        # Count results
        success_count = sum(1 for r in broker_results.values() if r.success)
        failed_count = sum(1 for r in broker_results.values() if not r.success and not r.skipped)
        skipped_count = sum(1 for r in broker_results.values() if r.skipped)
        
        end_time = datetime.now(timezone.utc)
        total_time_ms = (end_time - start_time).total_seconds() * 1000
        
        # Build result
        mirror_result = MirrorResult(
            trade_intent_id=intent.id,
            success=success_count > 0,
            full_success=success_count == len(active_brokers) and failed_count == 0,
            broker_results=broker_results,
            success_count=success_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            total_execution_time_ms=total_time_ms,
            strict_mode=is_mirror_strict(),
            timestamp=end_time,
        )
        
        # Strict mode: attempt rollback on partial failure
        if is_mirror_strict() and not mirror_result.full_success and success_count > 0:
            logger.warning(
                "[MIRROR] STRICT_MODE: Partial failure detected. Attempting rollback..."
            )
            mirror_result.rollback_attempted = True
            mirror_result.rollback_success = await self._attempt_rollback(
                intent, mirror_result.success_brokers
            )
        
        # Log result
        logger.info(
            "[MIRROR] DISPATCH COMPLETED | intent_id=%s | success=%s | "
            "full_success=%s | success_brokers=%s | failed_brokers=%s | "
            "time_ms=%.0f",
            intent.id, mirror_result.success, mirror_result.full_success,
            mirror_result.success_brokers, mirror_result.failed_brokers,
            total_time_ms
        )
        logger.info(
            "[MIRROR] MirrorResult: success_brokers=%s, failed_brokers=%s",
            mirror_result.success_brokers, mirror_result.failed_brokers
        )
        logger.info(
            "[MIRROR] ════════════════════════════════════════════════════════════"
        )
        
        # Callback: trade completed
        if self._on_trade_completed:
            try:
                await self._on_trade_completed(intent, mirror_result)
            except Exception as e:
                logger.warning("[MIRROR] on_trade_completed callback error: %s", e)
        
        return mirror_result
    
    # ──────────────────────────────────────────────────────────────────────────
    # PER-BROKER EXECUTION
    # ──────────────────────────────────────────────────────────────────────────
    
    async def _execute_on_broker(
        self, 
        broker: str, 
        intent: TradeIntent
    ) -> BrokerExecution:
        """Execute trade on a single broker."""
        start_time = datetime.now(timezone.utc)
        
        logger.info("[MIRROR] BROKER_START | %s | %s %s", broker.upper(), intent.side, intent.symbol)
        
        executor = self._executors.get(broker)
        if not executor:
            return BrokerExecution(
                broker=broker,
                success=False,
                skipped=True,
                skip_reason=f"No executor registered for {broker}",
            )
        
        try:
            # Call the executor's execute_order method
            result = await executor.execute_order(
                symbol=intent.symbol,
                side=intent.side,
                size=intent.quantity,
                order_type=intent.order_type,
                price=intent.entry_price if intent.order_type == "limit" else None,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
                leverage=intent.leverage,
            )
            
            end_time = datetime.now(timezone.utc)
            exec_time_ms = (end_time - start_time).total_seconds() * 1000
            
            if result.get("success"):
                logger.info(
                    "[MIRROR] %s ORDER PLACED | order_id=%s | time=%.0fms",
                    broker.upper(), result.get("order_id"), exec_time_ms
                )
                return BrokerExecution(
                    broker=broker,
                    success=True,
                    order_id=result.get("order_id"),
                    fill_price=result.get("fill_price") or result.get("price"),
                    fill_quantity=result.get("fill_quantity") or result.get("size"),
                    execution_time_ms=exec_time_ms,
                    raw_response=result,
                )
            else:
                logger.error(
                    "[MIRROR] %s ORDER FAILED | error=%s",
                    broker.upper(), result.get("error")
                )
                return BrokerExecution(
                    broker=broker,
                    success=False,
                    error=result.get("error"),
                    execution_time_ms=exec_time_ms,
                    raw_response=result,
                )
                
        except Exception as e:
            logger.error("[MIRROR] %s EXCEPTION | %s", broker.upper(), e)
            end_time = datetime.now(timezone.utc)
            exec_time_ms = (end_time - start_time).total_seconds() * 1000
            
            return BrokerExecution(
                broker=broker,
                success=False,
                error=str(e),
                execution_time_ms=exec_time_ms,
            )
    
    # ──────────────────────────────────────────────────────────────────────────
    # ROLLBACK (STRICT MODE)
    # ──────────────────────────────────────────────────────────────────────────
    
    async def _attempt_rollback(
        self, 
        intent: TradeIntent, 
        success_brokers: List[str]
    ) -> bool:
        """
        Attempt to rollback/close positions on successful brokers.
        
        Used in STRICT mode when partial failure occurs.
        """
        logger.warning(
            "[MIRROR] ROLLBACK | Attempting to close positions on: %s",
            success_brokers
        )
        
        rollback_success = True
        
        for broker in success_brokers:
            executor = self._executors.get(broker)
            if not executor:
                continue
            
            try:
                # Try to close the position by placing opposite order
                close_side = "short" if intent.side == "long" else "long"
                
                result = await executor.execute_order(
                    symbol=intent.symbol,
                    side=close_side,
                    size=intent.quantity,
                    order_type="market",
                )
                
                if result.get("success"):
                    logger.info("[MIRROR] ROLLBACK_OK | %s position closed", broker.upper())
                else:
                    logger.error(
                        "[MIRROR] ROLLBACK_FAILED | %s | %s",
                        broker.upper(), result.get("error")
                    )
                    rollback_success = False
                    
            except Exception as e:
                logger.error("[MIRROR] ROLLBACK_ERROR | %s | %s", broker.upper(), e)
                rollback_success = False
        
        return rollback_success
    
    # ──────────────────────────────────────────────────────────────────────────
    # UTILITY METHODS
    # ──────────────────────────────────────────────────────────────────────────
    
    async def test_all_connections(self) -> Dict[str, Dict[str, Any]]:
        """Test connections to all registered brokers."""
        results = {}
        
        for broker, executor in self._executors.items():
            try:
                if hasattr(executor, 'test_connection'):
                    result = await executor.test_connection()
                    results[broker] = result
                else:
                    results[broker] = {
                        "success": True,
                        "note": "Executor does not have test_connection method"
                    }
            except Exception as e:
                results[broker] = {
                    "success": False,
                    "error": str(e)
                }
        
        return results
    
    def get_status(self) -> Dict[str, Any]:
        """Get service status."""
        return {
            "mirror_enabled": is_mirror_enabled(),
            "strict_mode": is_mirror_strict(),
            "enabled_brokers": list(self._enabled_brokers),
            "registered_executors": list(self._executors.keys()),
            "base_risk_pct": get_base_risk_pct(),
            "min_notional": get_min_notional(),
            "execution_timeout": get_execution_timeout(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

_service_instance: Optional[MirrorExecutionService] = None


def get_mirror_execution_service() -> MirrorExecutionService:
    """Get singleton instance of MirrorExecutionService."""
    global _service_instance
    if _service_instance is None:
        _service_instance = MirrorExecutionService()
    return _service_instance


def configure_mirror_service(
    bitget_executor: Any = None,
    kraken_executor: Any = None,
) -> MirrorExecutionService:
    """
    Configure the mirror execution service with executors.
    
    Call this during app startup.
    """
    service = get_mirror_execution_service()
    
    if bitget_executor:
        service.register_executor("bitget", bitget_executor)
    
    if kraken_executor:
        service.register_executor("kraken", kraken_executor)
    
    return service


# ══════════════════════════════════════════════════════════════════════════════
# QUICK HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def execute_mirror_trade(
    symbol: str,
    side: str,
    quantity: float,
    entry_price: float,
    stop_loss: float,
    take_profit: Optional[float] = None,
    leverage: int = 10,
    source: str = "manual",
) -> MirrorResult:
    """
    Quick helper to execute a mirror trade.
    
    Uses the singleton service instance.
    """
    service = get_mirror_execution_service()
    
    intent = TradeIntent(
        symbol=symbol,
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        leverage=leverage,
        source=source,
    )
    
    return await service.execute_trade(intent)
