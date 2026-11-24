"""
Broker Router — Unified Multi-Broker Execution Interface
═════════════════════════════════════════════════════════

Central router for executing trades on multiple brokers simultaneously.
This is the SINGLE SOURCE OF TRUTH for multi-broker execution.

Features:
  - Unified interface for all executors (Bitget, Kraken, etc.)
  - Concurrent execution via asyncio.gather
  - REQUIRE_BOTH_BROKERS mode: rollback if any broker fails
  - Per-broker error capture and logging
  - Health check across all brokers

Usage:
    from backend.execution.broker_router import get_broker_router, BrokerRouter
    
    router = get_broker_router()
    router.register_executor("bitget", bitget_exec)
    router.register_executor("kraken", kraken_exec)
    
    results = await router.execute_on_brokers(
        signal=signal,
        brokers=["bitget", "kraken"]
    )
    # results = {
    #     "bitget": BrokerResult(success=True, order_id="123"),
    #     "kraken": BrokerResult(success=True, order_id="456"),
    # }

Author: SMC Trading Bot Framework
"""

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple
from enum import Enum

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("smarttrade.broker_router")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

def is_require_both_brokers() -> bool:
    """
    Check if REQUIRE_BOTH_BROKERS mode is enabled.
    
    When True: If any broker fails, cancel/rollback others.
    When False: Best-effort execution (partial success allowed).
    """
    return os.getenv("REQUIRE_BOTH_BROKERS", "false").lower() in ("1", "true", "yes")


def is_multi_broker_enabled() -> bool:
    """Check if multi-broker execution is enabled."""
    mirror = os.getenv("MULTI_BROKER_MIRROR", "0").lower() in ("1", "true", "yes")
    dual = os.getenv("DUAL_EXECUTION", "0").lower() in ("1", "true", "yes")
    multi = os.getenv("MULTI_BROKER_EXECUTION", "0").lower() in ("1", "true", "yes")
    return mirror or dual or multi


def is_bitget_enabled() -> bool:
    """Check if Bitget is enabled."""
    return os.getenv("BITGET_ENABLED", "false").lower() in ("1", "true", "yes")


def is_kraken_enabled() -> bool:
    """Check if Kraken is enabled."""
    return os.getenv("KRAKEN_ENABLED", "true").lower() in ("1", "true", "yes")


def is_mt5_enabled() -> bool:
    """Check if MetaTrader 5 is enabled."""
    return os.getenv("MT5_ENABLED", "false").lower() in ("1", "true", "yes")


def get_execution_timeout() -> float:
    """Get per-broker execution timeout in seconds."""
    return float(os.getenv("BROKER_EXEC_TIMEOUT", "30.0"))


def get_default_brokers() -> List[str]:
    """
    Get list of default brokers for execution.
    
    Respects BITGET_ENABLED, KRAKEN_ENABLED, and MT5_ENABLED env vars.
    Falls back to DEFAULT_BROKER if only one is active.
    """
    brokers = []
    if is_bitget_enabled():
        brokers.append("bitget")
    if is_kraken_enabled():
        brokers.append("kraken")
    if is_mt5_enabled():
        brokers.append("mt5")
    if not brokers:
        # Fallback to DEFAULT_BROKER
        brokers.append(os.getenv("DEFAULT_BROKER", "kraken"))
    return brokers


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

class BrokerStatus(str, Enum):
    """Broker execution status."""
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class BrokerResult:
    """Result from a single broker execution."""
    broker: str
    status: BrokerStatus
    success: bool = False
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    quantity: Optional[float] = None
    fee: Optional[float] = None
    error: Optional[str] = None
    skip_reason: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_response: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "broker": self.broker,
            "status": self.status.value,
            "success": self.success,
            "order_id": self.order_id,
            "fill_price": self.fill_price,
            "quantity": self.quantity,
            "fee": self.fee,
            "error": self.error,
            "skip_reason": self.skip_reason,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class TradeIntent:
    """
    Unified trade intent that gets dispatched to all brokers.
    Single source of truth for a trade order.
    """
    # Required fields
    symbol: str
    side: str  # "long" or "short" or "buy" or "sell"
    quantity: float
    entry_price: float
    stop_loss: float
    
    # Optional fields
    take_profit: Optional[float] = None
    leverage: int = 10
    order_type: str = "market"  # "market" or "limit"
    reduce_only: bool = False
    
    # Risk info
    risk_pct: float = 1.0
    risk_amount: Optional[float] = None
    
    # Metadata
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    signal_id: Optional[str] = None
    source: str = "manual"  # "manual" or "auto"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Targeting (which brokers to execute on)
    target_brokers: List[str] = field(default_factory=get_default_brokers)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "leverage": self.leverage,
            "order_type": self.order_type,
            "reduce_only": self.reduce_only,
            "risk_pct": self.risk_pct,
            "risk_amount": self.risk_amount,
            "source": self.source,
            "target_brokers": self.target_brokers,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class MultiExecutionResult:
    """Result from multi-broker execution."""
    signal_id: str
    intent: TradeIntent
    results: Dict[str, BrokerResult]
    overall_success: bool
    partial_success: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results.values() if r.success)
    
    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results.values() if r.status == BrokerStatus.FAILED)
    
    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.results.values() if r.status == BrokerStatus.SKIPPED)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "overall_success": self.overall_success,
            "partial_success": self.partial_success,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "results": {k: v.to_dict() for k, v in self.results.items()},
            "timestamp": self.timestamp.isoformat(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# EXECUTOR PROTOCOL
# ══════════════════════════════════════════════════════════════════════════════

class ExecutorProtocol(Protocol):
    """Protocol that all broker executors must implement."""
    
    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        leverage: int = 10,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """Place an order on the broker."""
        ...
    
    async def get_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open positions."""
        ...
    
    async def close_position(
        self,
        symbol: str,
        quantity: Optional[float] = None,
        reduce_only: bool = True,
    ) -> Dict[str, Any]:
        """Close a position."""
        ...
    
    async def health_check(self) -> Dict[str, Any]:
        """Check broker connection health."""
        ...
    
    def normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol for this broker."""
        ...


# ══════════════════════════════════════════════════════════════════════════════
# BROKER ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class BrokerRouter:
    """
    Central router for multi-broker order execution.
    
    This is the SINGLE interface for executing trades across multiple brokers.
    All other multi-broker systems should delegate to this router.
    """

    # ── Idempotency ledger (persisted to disk) ──
    _LEDGER_DIR = Path(os.getenv("DATA_DIR", "data")) / "execution_ledger"
    
    def __init__(self):
        """Initialize broker router."""
        self._executors: Dict[str, Any] = {}
        self._symbol_mappers: Dict[str, Callable[[str], str]] = {}
        self._execution_timeout = get_execution_timeout()
        self._require_both = is_require_both_brokers()
        self._inflight: Set[str] = set()  # intent_ids currently executing
        self._lock = asyncio.Lock()
        
        # Ensure ledger directory exists AND is writable
        self._LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        self._ledger_writable = self._validate_ledger_dir()
        
        logger.info(
            "[BROKER_ROUTER] Initialized | require_both=%s | timeout=%.1fs | ledger_writable=%s",
            self._require_both, self._execution_timeout, self._ledger_writable
        )
    
    def _validate_ledger_dir(self) -> bool:
        """Validate ledger directory is writable. CRITICAL for trade safety."""
        test_file = self._LEDGER_DIR / "_write_test.tmp"
        try:
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
            return True
        except Exception as e:
            logger.critical(
                "[BROKER_ROUTER] LEDGER DIRECTORY NOT WRITABLE: %s — "
                "trades will be BLOCKED until resolved", e
            )
            return False
    
    # ──────────────────────────────────────────────────
    #  REGISTRATION
    # ──────────────────────────────────────────────────
    
    def register_executor(
        self,
        broker_name: str,
        executor: Any,
        symbol_mapper: Optional[Callable[[str], str]] = None,
    ) -> None:
        """
        Register an executor for a broker.
        
        Args:
            broker_name: Broker identifier ("bitget", "kraken")
            executor: Executor instance implementing place_order, etc.
            symbol_mapper: Optional function to map symbols for this broker
        """
        broker_name = broker_name.lower()
        self._executors[broker_name] = executor
        
        if symbol_mapper:
            self._symbol_mappers[broker_name] = symbol_mapper
        
        logger.info("[BROKER_ROUTER] Registered executor: %s", broker_name)
    
    def unregister_executor(self, broker_name: str) -> None:
        """Unregister an executor."""
        broker_name = broker_name.lower()
        if broker_name in self._executors:
            del self._executors[broker_name]
            logger.info("[BROKER_ROUTER] Unregistered executor: %s", broker_name)
    
    def get_executor(self, broker_name: str) -> Optional[Any]:
        """Get executor by broker name."""
        return self._executors.get(broker_name.lower())
    
    def get_registered_brokers(self) -> List[str]:
        """Get list of registered broker names."""
        return list(self._executors.keys())
    
    def is_broker_available(self, broker_name: str) -> bool:
        """Check if broker is registered and has executor."""
        return broker_name.lower() in self._executors

    # ──────────────────────────────────────────────────
    #  IDEMPOTENCY LEDGER
    # ──────────────────────────────────────────────────

    def _ledger_key(self, intent_id: str, broker: str) -> str:
        return f"{intent_id}_{broker}"

    def _ledger_path(self, key: str) -> Path:
        return self._LEDGER_DIR / f"{key}.json"

    def _record_intent_sent(self, intent_id: str, broker: str, intent: "TradeIntent") -> None:
        """Record that we are ABOUT to send an order (pre-flight ledger).
        
        Raises RuntimeError if ledger is not writable — trade MUST NOT proceed
        without a durable intent record.
        """
        key = self._ledger_key(intent_id, broker)
        payload = {
            "intent_id": intent_id,
            "broker": broker,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "status": "SENT",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "order_id": None,
        }
        try:
            self._ledger_path(key).write_text(json.dumps(payload), encoding="utf-8")
        except Exception as e:
            logger.critical("[BROKER_ROUTER] LEDGER WRITE FAILED %s: %s — BLOCKING TRADE", key, e)
            self._ledger_writable = False
            raise RuntimeError(f"Ledger write failed — trade blocked for safety: {e}")

    def _record_intent_result(self, intent_id: str, broker: str, result: "BrokerResult") -> None:
        """Update ledger with execution result."""
        key = self._ledger_key(intent_id, broker)
        path = self._ledger_path(key)
        try:
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
            else:
                payload = {"intent_id": intent_id, "broker": broker}
            payload["status"] = result.status.value
            payload["order_id"] = result.order_id
            payload["success"] = result.success
            payload["completed_at"] = datetime.now(timezone.utc).isoformat()
            if result.error:
                payload["error"] = result.error
            path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception as e:
            logger.error("[BROKER_ROUTER] Failed to update ledger %s: %s", key, e)

    def _get_ledger_entry(self, intent_id: str, broker: str) -> Optional[Dict[str, Any]]:
        """Read ledger entry. Returns None if not found."""
        key = self._ledger_key(intent_id, broker)
        path = self._ledger_path(key)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.error("[BROKER_ROUTER] Ledger read failed %s: %s — possible corruption", key, e)
                return None
        return None
    
    # ──────────────────────────────────────────────────
    #  SYMBOL MAPPING
    # ──────────────────────────────────────────────────
    
    def get_broker_symbol(self, symbol: str, broker_name: str) -> str:
        """
        Get symbol in broker-specific format.
        
        Args:
            symbol: Canonical symbol (e.g., "BTCUSDT")
            broker_name: Target broker
            
        Returns:
            Broker-specific symbol (e.g., "PF_XBTUSD" for Kraken)
        """
        broker_name = broker_name.lower()
        mapper = self._symbol_mappers.get(broker_name)
        
        if mapper:
            return mapper(symbol)
        
        # Default: use symbol as-is
        return symbol.upper().replace("/", "").replace(":", "")
    
    # ──────────────────────────────────────────────────
    #  EXECUTION
    # ──────────────────────────────────────────────────
    
    async def execute_intent(
        self,
        intent: TradeIntent,
        brokers: Optional[List[str]] = None,
    ) -> MultiExecutionResult:
        """
        Execute a trade intent on multiple brokers.
        
        Args:
            intent: TradeIntent with all order details
            brokers: Override brokers (default: intent.target_brokers)
            
        Returns:
            MultiExecutionResult with per-broker results
        """
        target_brokers = brokers or intent.target_brokers or get_default_brokers()
        signal_id = intent.signal_id or intent.id

        # ── Duplicate execution guard ──
        async with self._lock:
            if intent.id in self._inflight:
                logger.warning("[BROKER_ROUTER] DUPLICATE blocked: intent %s already in-flight", intent.id)
                return MultiExecutionResult(
                    signal_id=signal_id,
                    intent=intent,
                    results={b: BrokerResult(broker=b, status=BrokerStatus.SKIPPED, error="duplicate in-flight")
                             for b in target_brokers},
                    overall_success=False,
                    partial_success=False,
                )
            self._inflight.add(intent.id)

        try:
            return await self._execute_intent_inner(intent, target_brokers, signal_id)
        finally:
            self._inflight.discard(intent.id)

    async def _execute_intent_inner(
        self,
        intent: TradeIntent,
        target_brokers: List[str],
        signal_id: str,
    ) -> MultiExecutionResult:
        """Inner execution logic (after duplicate guard)."""
        
        logger.info(
            "[BROKER_ROUTER] Execute %s %s | qty=%.6f | brokers=%s | signal_id=%s",
            intent.side.upper(), intent.symbol, intent.quantity,
            target_brokers, signal_id
        )
        
        results: Dict[str, BrokerResult] = {}
        tasks = []
        broker_task_map = []
        
        for broker in target_brokers:
            broker = broker.lower()
            
            # Check if broker is available
            if not self.is_broker_available(broker):
                results[broker] = BrokerResult(
                    broker=broker,
                    status=BrokerStatus.SKIPPED,
                    success=False,
                    skip_reason=f"{broker} executor not registered",
                )
                logger.warning("[BROKER_ROUTER] %s skipped: not registered", broker)
                continue
            
            # Create task for this broker
            # Pre-flight: record intent in ledger BEFORE sending
            self._record_intent_sent(intent.id, broker, intent)
            task = self._execute_single_broker(broker, intent)
            tasks.append(task)
            broker_task_map.append(broker)
        
        # Execute all tasks concurrently with timeout
        if tasks:
            try:
                task_results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self._execution_timeout
                )
                
                for broker, result in zip(broker_task_map, task_results):
                    if isinstance(result, Exception):
                        logger.error("[BROKER_ROUTER] %s exception: %s", broker, result)
                        results[broker] = BrokerResult(
                            broker=broker,
                            status=BrokerStatus.FAILED,
                            success=False,
                            error=str(result),
                        )
                    elif isinstance(result, BrokerResult):
                        results[broker] = result
                        
            except asyncio.TimeoutError:
                logger.error("[BROKER_ROUTER] Execution timeout after %.1fs", self._execution_timeout)
                for broker in broker_task_map:
                    if broker not in results:
                        # After timeout, try to poll broker for actual order status
                        recovered = await self._check_order_after_timeout(broker, intent)
                        if recovered and recovered.success:
                            logger.warning(
                                "[BROKER_ROUTER] %s: order CONFIRMED after timeout (order_id=%s)",
                                broker, recovered.order_id,
                            )
                            results[broker] = recovered
                        else:
                            results[broker] = BrokerResult(
                                broker=broker,
                                status=BrokerStatus.TIMEOUT,
                                success=False,
                                error=f"Timeout after {self._execution_timeout}s — order status UNKNOWN",
                            )
        
        # Determine overall success
        success_count = sum(1 for r in results.values() if r.success)
        all_success = success_count == len(target_brokers)
        partial_success = success_count > 0 and not all_success
        
        # REQUIRE_BOTH_BROKERS mode: rollback on partial failure
        if self._require_both and partial_success:
            logger.warning(
                "[BROKER_ROUTER] REQUIRE_BOTH_BROKERS: partial success → rolling back"
            )
            await self._rollback_successful(results, intent)
            # Mark successful ones as cancelled
            for broker, result in results.items():
                if result.success:
                    results[broker] = BrokerResult(
                        broker=broker,
                        status=BrokerStatus.CANCELLED,
                        success=False,
                        order_id=result.order_id,
                        error="Rolled back due to REQUIRE_BOTH_BROKERS policy",
                    )
            all_success = False
            partial_success = False
        
        # Log summary
        status_summary = " | ".join(
            f"{b}={r.status.value.upper()}" + (f" ({r.error})" if r.error else "")
            for b, r in results.items()
        )
        logger.info("[BROKER_ROUTER] Complete | %s", status_summary)
        
        return MultiExecutionResult(
            signal_id=signal_id,
            intent=intent,
            results=results,
            overall_success=all_success,
            partial_success=partial_success,
        )
    
    async def execute_on_brokers(
        self,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        stop_loss: float,
        take_profit: Optional[float] = None,
        leverage: int = 10,
        order_type: str = "market",
        brokers: Optional[List[str]] = None,
        signal_id: Optional[str] = None,
        source: str = "manual",
    ) -> MultiExecutionResult:
        """
        Convenience method to execute without creating TradeIntent.
        
        This is the primary method for external callers.
        """
        intent = TradeIntent(
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=leverage,
            order_type=order_type,
            signal_id=signal_id or str(uuid.uuid4())[:12],
            source=source,
            target_brokers=brokers or get_default_brokers(),
        )
        
        return await self.execute_intent(intent, brokers)
    
    async def _execute_single_broker(
        self,
        broker: str,
        intent: TradeIntent,
    ) -> BrokerResult:
        """Execute order on a single broker with rate-limit retry."""
        executor = self._executors.get(broker)
        if not executor:
            return BrokerResult(
                broker=broker,
                status=BrokerStatus.SKIPPED,
                success=False,
                skip_reason="No executor",
            )
        
        # Get broker-specific symbol
        broker_symbol = self.get_broker_symbol(intent.symbol, broker)
        
        # Normalize side
        side = intent.side.lower()
        if side == "long":
            side = "buy"
        elif side == "short":
            side = "sell"
        
        # Retry loop for rate-limit errors (max 3 attempts, exponential backoff)
        _RATE_LIMIT_RETRIES = 3
        _RATE_LIMIT_BACKOFF = [1.0, 3.0, 7.0]
        
        for attempt in range(_RATE_LIMIT_RETRIES):
            try:
                response = await self._call_executor(executor, broker_symbol, side, intent)
                
                # Parse response (different executors have different formats)
                result = self._parse_executor_response(broker, response)
                self._record_intent_result(intent.id, broker, result)
                return result

            except asyncio.CancelledError:
                logger.warning("[BROKER_ROUTER] %s task cancelled — order status UNKNOWN", broker)
                raise  # Must propagate CancelledError
            except Exception as e:
                # Rate limit retry (check class name to avoid hard import dependency)
                is_rate_limit = type(e).__name__ == "KrakenRateLimitError" or "rate limit" in str(e).lower()
                if is_rate_limit and attempt < _RATE_LIMIT_RETRIES - 1:
                    wait = _RATE_LIMIT_BACKOFF[attempt]
                    logger.warning(
                        "[BROKER_ROUTER] %s rate limited (attempt %d/%d) — retrying in %.1fs",
                        broker, attempt + 1, _RATE_LIMIT_RETRIES, wait
                    )
                    await asyncio.sleep(wait)
                    continue
                
                logger.error("[BROKER_ROUTER] %s execution error: %s", broker, e, exc_info=True)
                fail_result = BrokerResult(
                    broker=broker,
                    status=BrokerStatus.FAILED,
                    success=False,
                    error=str(e),
                )
                self._record_intent_result(intent.id, broker, fail_result)
                return fail_result

        # Should not reach here, but safety fallback
        fail_result = BrokerResult(
            broker=broker, status=BrokerStatus.FAILED, success=False,
            error=f"Rate limit retries exhausted ({_RATE_LIMIT_RETRIES} attempts)",
        )
        self._record_intent_result(intent.id, broker, fail_result)
        return fail_result

    async def _call_executor(
        self, executor: Any, broker_symbol: str, side: str, intent: TradeIntent,
    ) -> Dict[str, Any]:
        """Dispatch to the appropriate executor method."""
        if hasattr(executor, 'place_order'):
            return await executor.place_order(
                symbol=broker_symbol,
                side=side,
                quantity=intent.quantity,
                order_type=intent.order_type,
                price=intent.entry_price if intent.order_type == "limit" else None,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
                leverage=intent.leverage,
                reduce_only=intent.reduce_only,
            )
        elif hasattr(executor, 'place_order_market') and intent.order_type == "market":
            return await executor.place_order_market(
                symbol=broker_symbol,
                side=side,
                size=str(intent.quantity),
                reduce_only=intent.reduce_only,
            )
        elif hasattr(executor, 'place_order_limit') and intent.order_type == "limit":
            return await executor.place_order_limit(
                symbol=broker_symbol,
                side=side,
                size=str(intent.quantity),
                price=str(intent.entry_price),
                reduce_only=intent.reduce_only,
            )
        else:
            raise RuntimeError("Executor does not implement place_order methods")
    
    def _parse_executor_response(
        self,
        broker: str,
        response: Dict[str, Any],
    ) -> BrokerResult:
        """Parse executor response into BrokerResult."""
        # Handle various response formats
        if response is None:
            return BrokerResult(
                broker=broker,
                status=BrokerStatus.FAILED,
                success=False,
                error="Null response from executor",
            )
        
        # Bitget format: {"code": "00000", "data": {"orderId": "..."}}
        if "code" in response:
            success = response.get("code") == "00000"
            data = response.get("data", {})
            
            if isinstance(data, dict):
                order_id = data.get("orderId") or data.get("order_id")
                fill_price = data.get("fillPrice") or data.get("fill_price")
                quantity = data.get("size") or data.get("quantity")
            else:
                order_id = None
                fill_price = None
                quantity = None
            
            return BrokerResult(
                broker=broker,
                status=BrokerStatus.SUCCESS if success else BrokerStatus.FAILED,
                success=success,
                order_id=str(order_id) if order_id else None,
                fill_price=float(fill_price) if fill_price else None,
                quantity=float(quantity) if quantity else None,
                error=response.get("msg") if not success else None,
                raw_response=response,
            )
        
        # Generic format: {"success": bool, "order_id": "..."}
        if "success" in response:
            success = response.get("success", False)
            return BrokerResult(
                broker=broker,
                status=BrokerStatus.SUCCESS if success else BrokerStatus.FAILED,
                success=success,
                order_id=response.get("order_id"),
                fill_price=response.get("fill_price"),
                quantity=response.get("quantity"),
                error=response.get("error"),
                raw_response=response,
            )
        
        # Kraken format: {"result": "success", "orderId": "..."}
        if "result" in response:
            success = response.get("result") == "success"
            return BrokerResult(
                broker=broker,
                status=BrokerStatus.SUCCESS if success else BrokerStatus.FAILED,
                success=success,
                order_id=response.get("orderId") or response.get("order_id"),
                error=response.get("error"),
                raw_response=response,
            )
        
        # Unknown format - FAIL-CLOSED: do not assume success from unknown response
        logger.error(
            "[BROKER_ROUTER] UNKNOWN response format from %s — treating as FAILED "
            "(keys=%s). Fix _parse_executor_response to handle this format.",
            broker, list(response.keys())
        )
        return BrokerResult(
            broker=broker,
            status=BrokerStatus.FAILED,
            success=False,
            error=f"Unknown response format (keys: {list(response.keys())})",
            raw_response=response,
        )
    
    async def _rollback_successful(
        self,
        results: Dict[str, BrokerResult],
        intent: TradeIntent,
    ) -> None:
        """Rollback successful orders when REQUIRE_BOTH_BROKERS fails."""
        rollback_timeout = min(self._execution_timeout, 15.0)
        for broker, result in results.items():
            if result.success and result.order_id:
                executor = self._executors.get(broker)
                if executor and hasattr(executor, 'cancel_order'):
                    try:
                        broker_symbol = self.get_broker_symbol(intent.symbol, broker)
                        await asyncio.wait_for(
                            executor.cancel_order(broker_symbol, result.order_id),
                            timeout=rollback_timeout,
                        )
                        logger.info("[BROKER_ROUTER] Rolled back %s order %s", broker, result.order_id)
                    except asyncio.TimeoutError:
                        logger.critical(
                            "[BROKER_ROUTER] ROLLBACK TIMEOUT on %s order %s — ORPHANED POSITION",
                            broker, result.order_id,
                        )
                    except Exception as e:
                        logger.error("[BROKER_ROUTER] Failed to rollback %s: %s", broker, e)

    async def _check_order_after_timeout(
        self,
        broker: str,
        intent: TradeIntent,
    ) -> Optional[BrokerResult]:
        """
        After a timeout, try to check if the order actually went through.
        Queries open positions on the broker to find a match for this specific intent.

        ── P0-002 FIX ──────────────────────────────────────────────────────────
        Previous: returned SUCCESS for ANY existing position on the symbol.
        Fixed:    matches position against intent symbol + side + quantity (±30%).
        A position for a DIFFERENT symbol or on the WRONG side must NOT return SUCCESS.
        ─────────────────────────────────────────────────────────────────────────

        Returns BrokerResult if a matching position found, None otherwise.
        """
        executor = self._executors.get(broker)
        if not executor:
            return None
        try:
            broker_symbol = self.get_broker_symbol(intent.symbol, broker)
            expected_side_raw = intent.side.lower()
            expected_side = "long" if expected_side_raw in ("buy", "long") else "short"
            expected_fill_side = "buy" if expected_side in ("long", "buy") else "sell"

            # 1. First, check GET /derivatives/api/v3/fills to find an exact fill with real order_id
            if hasattr(executor, 'get_fills'):
                try:
                    fills = await asyncio.wait_for(executor.get_fills(), timeout=5.0)
                    for fill in (fills or []):
                        fill_symbol = (fill.get("symbol") or "").upper()
                        fill_side = (fill.get("side") or "").lower()
                        fill_size = float(fill.get("size", 0) or 0)
                        
                        sym_match = (fill_symbol == broker_symbol.upper() or fill_symbol == intent.symbol.upper())
                        side_match = fill_side in (expected_fill_side, expected_side_raw)
                        qty_match = True
                        if intent.quantity and intent.quantity > 0:
                            qty_match = abs(fill_size - intent.quantity) / intent.quantity <= 0.30

                        if sym_match and side_match and qty_match:
                            real_order_id = fill.get("order_id")
                            fill_price = float(fill.get("price", 0) or 0)
                            logger.warning(
                                "[BROKER_ROUTER] %s: order CONFIRMED via fills after timeout "
                                "(order_id=%s | symbol=%s | side=%s | price=%.2f)",
                                broker, real_order_id, fill_symbol, fill_side, fill_price,
                            )
                            return BrokerResult(
                                broker=broker,
                                status=BrokerStatus.SUCCESS,
                                success=True,
                                order_id=real_order_id,
                                fill_price=fill_price,
                                error="Confirmed via fills after timeout",
                            )
                except Exception as fills_err:
                    logger.debug("[BROKER_ROUTER] Fills check skipped/failed: %s", fills_err)

            # 2. Check open positions as secondary confirmation
            if hasattr(executor, 'get_positions'):
                positions = await asyncio.wait_for(
                    executor.get_positions(broker_symbol),
                    timeout=10.0,
                )
                if not positions:
                    return None

                # Look for a position that matches symbol + side + qty (within ±30%)
                for pos in positions:
                    pos_symbol = (pos.get("symbol") or "").upper()
                    pos_side   = (pos.get("side")   or "").lower()
                    pos_size   = float(pos.get("size", 0) or 0)

                    # Symbol check — accept Kraken native format or standard format
                    symbol_match = (
                        pos_symbol == broker_symbol.upper()
                        or pos_symbol == intent.symbol.upper()
                    )
                    # Side check
                    side_match = pos_side in (expected_side, expected_side_raw)
                    # Quantity check: within ±30% of intent quantity
                    if intent.quantity and intent.quantity > 0:
                        qty_ratio = abs(pos_size - intent.quantity) / intent.quantity
                        qty_match = qty_ratio <= 0.30
                    else:
                        qty_match = pos_size > 0

                    if symbol_match and side_match and qty_match:
                        logger.warning(
                            "[BROKER_ROUTER] %s: order CONFIRMED via position check after timeout "
                            "(symbol=%s | side=%s | size=%.4f | intent_qty=%.4f)",
                            broker, pos_symbol, pos_side, pos_size, intent.quantity or 0,
                        )
                        # We have a confirmed fill — but we don't have a real order_id.
                        # Return success; the caller will record it. order_id is None
                        # so persistence must not invent a broker_order_id.
                        return BrokerResult(
                            broker=broker,
                            status=BrokerStatus.SUCCESS,
                            success=True,
                            order_id=None,  # not known after timeout
                            error="Confirmed via position check after timeout",
                        )

                # Positions exist but none match this intent
                logger.warning(
                    "[BROKER_ROUTER] %s: positions found after timeout but NONE match "
                    "intent (symbol=%s | side=%s | qty=%s) — returning UNKNOWN",
                    broker, broker_symbol, expected_side, intent.quantity,
                )
        except Exception as e:
            logger.error("[BROKER_ROUTER] %s post-timeout check failed: %s", broker, e)
        return None
    
    # ──────────────────────────────────────────────────
    #  POSITIONS
    # ──────────────────────────────────────────────────
    
    async def get_positions(
        self,
        broker: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get open positions.
        
        Args:
            broker: Specific broker (or None for all)
            symbol: Specific symbol (or None for all)
            
        Returns:
            Dict mapping broker -> list of positions
        """
        positions: Dict[str, List[Dict[str, Any]]] = {}
        
        brokers = [broker.lower()] if broker else list(self._executors.keys())
        
        for b in brokers:
            executor = self._executors.get(b)
            if not executor:
                continue
            
            try:
                if hasattr(executor, 'get_positions'):
                    broker_symbol = self.get_broker_symbol(symbol, b) if symbol else None
                    pos = await executor.get_positions(broker_symbol)
                    positions[b] = pos if isinstance(pos, list) else [pos]
                else:
                    positions[b] = []
            except Exception as e:
                logger.error("[BROKER_ROUTER] Failed to get positions from %s: %s", b, e)
                positions[b] = []
        
        return positions
    
    async def close_position(
        self,
        symbol: str,
        broker: Optional[str] = None,
        quantity: Optional[float] = None,
    ) -> Dict[str, BrokerResult]:
        """
        Close position on one or all brokers.
        
        Args:
            symbol: Symbol to close
            broker: Specific broker (or None for all)
            quantity: Quantity to close (or None for full position)
            
        Returns:
            Dict mapping broker -> close result
        """
        results: Dict[str, BrokerResult] = {}
        brokers = [broker.lower()] if broker else list(self._executors.keys())
        
        for b in brokers:
            executor = self._executors.get(b)
            if not executor:
                continue
            
            try:
                broker_symbol = self.get_broker_symbol(symbol, b)
                
                if hasattr(executor, 'close_position'):
                    response = await executor.close_position(broker_symbol, quantity)
                    results[b] = self._parse_executor_response(b, response)
                else:
                    results[b] = BrokerResult(
                        broker=b,
                        status=BrokerStatus.SKIPPED,
                        success=False,
                        skip_reason="No close_position method",
                    )
            except Exception as e:
                logger.error("[BROKER_ROUTER] Failed to close on %s: %s", b, e)
                results[b] = BrokerResult(
                    broker=b,
                    status=BrokerStatus.FAILED,
                    success=False,
                    error=str(e),
                )
        
        return results
    
    # ──────────────────────────────────────────────────
    #  HEALTH CHECK
    # ──────────────────────────────────────────────────
    
    async def health_check(self) -> Dict[str, Dict[str, Any]]:
        """
        Health check all registered brokers.
        
        Returns:
            Dict mapping broker -> health status
        """
        status: Dict[str, Dict[str, Any]] = {}
        
        for broker, executor in self._executors.items():
            try:
                if hasattr(executor, 'health_check'):
                    result = await executor.health_check()
                    status[broker] = {
                        "healthy": result.get("success", True),
                        "latency_ms": result.get("latency_ms"),
                        "error": result.get("error"),
                    }
                elif hasattr(executor, 'is_connected'):
                    connected = executor.is_connected()
                    status[broker] = {
                        "healthy": connected,
                        "connected": connected,
                    }
                else:
                    status[broker] = {"healthy": True, "assumed": True}
            except Exception as e:
                status[broker] = {"healthy": False, "error": str(e)}
        
        return status
    
    async def get_balances(self) -> Dict[str, Dict[str, Any]]:
        """Get balances from all brokers."""
        balances: Dict[str, Dict[str, Any]] = {}
        
        for broker, executor in self._executors.items():
            try:
                if hasattr(executor, 'get_balance'):
                    balance = await executor.get_balance()
                    balances[broker] = balance if isinstance(balance, dict) else {"total": balance}
                else:
                    balances[broker] = {}
            except Exception as e:
                logger.error("[BROKER_ROUTER] Failed to get balance from %s: %s", broker, e)
                balances[broker] = {"error": str(e)}
        
        return balances


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

_broker_router: Optional[BrokerRouter] = None


def get_broker_router() -> BrokerRouter:
    """Get or create the singleton BrokerRouter instance."""
    global _broker_router
    if _broker_router is None:
        _broker_router = BrokerRouter()
    return _broker_router


def reset_broker_router() -> None:
    """Reset the singleton (for testing)."""
    global _broker_router
    _broker_router = None
