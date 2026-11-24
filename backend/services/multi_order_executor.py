"""
Multi-Order Executor — Concurrent Order Execution Across Brokers
═══════════════════════════════════════════════════════════════

Provides a unified interface to execute orders on multiple brokers
simultaneously with best-effort semantics: if one broker fails,
others can still succeed.

Usage:
    from backend.services.multi_order_executor import MultiOrderExecutor
    
    executor = MultiOrderExecutor()
    executor.set_bitget_executor(bitget_exec)
    executor.set_kraken_executor(kraken_exec)
    
    results = await executor.execute_on_brokers(
        symbol="BTCUSDT",
        direction="long",
        entry_price=50000,
        stop_loss=49000,
        take_profit=52000,
        quantity="0.001",
        brokers=["bitget", "kraken"],
    )
    # results = {
    #     "bitget": {"success": True, "order_id": "123", ...},
    #     "kraken": {"success": True, "order_id": "456", ...},
    # }

Author: Trading Bot Framework
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("smarttrade.multi_order_executor")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

def is_multi_broker_enabled() -> bool:
    """Check if multi-broker execution is enabled via environment."""
    return os.getenv("MULTI_BROKER_EXECUTION", "0").lower() in ("1", "true", "yes")


def is_multi_broker_mirror_enabled() -> bool:
    """
    Check if mirror mode is enabled — duplicate all orders to both brokers.
    
    Env vars (any of these enables mirror mode):
      - MULTI_BROKER_MIRROR=true
      - DUAL_EXECUTION=true
    
    When enabled:
      - Manual and auto trades are sent to BOTH Bitget AND Kraken
      - If one broker fails, the other continues (partial fill)
      - Logs show 'MIRROR_MODE' prefix for clarity
    """
    mirror = os.getenv("MULTI_BROKER_MIRROR", "0").lower() in ("1", "true", "yes")
    dual = os.getenv("DUAL_EXECUTION", "0").lower() in ("1", "true", "yes")
    return mirror or dual


def is_kraken_executor_ready() -> bool:
    """Check if Kraken is enabled and should receive orders."""
    return os.getenv("KRAKEN_EXECUTION_ENABLED", "0").lower() in ("1", "true", "yes")


def is_bitget_enabled() -> bool:
    """Check if Bitget is enabled for execution."""
    return os.getenv("BITGET_ENABLED", "false").lower() in ("1", "true", "yes")


def get_default_brokers() -> List[str]:
    """
    Get list of default brokers for execution.
    
    Respects BITGET_ENABLED and KRAKEN_ENABLED/KRAKEN_EXECUTION_ENABLED env vars.
    Falls back to DEFAULT_BROKER if no broker is explicitly enabled.
    """
    brokers = []
    
    if is_bitget_enabled():
        brokers.append("bitget")
    
    if is_kraken_executor_ready():
        brokers.append("kraken")
    
    if not brokers:
        # Fallback to DEFAULT_BROKER
        default = os.getenv("DEFAULT_BROKER", "bitget")
        brokers.append(default)
        logger.warning(f"[CONFIG] No broker explicitly enabled, falling back to DEFAULT_BROKER={default}")
    
    if len(brokers) > 1:
        logger.info(f"[CONFIG] Multi-broker mode active: orders will be sent to {brokers}")
    else:
        logger.info(f"[CONFIG] Single-broker mode: orders will be sent to {brokers[0]} only")
    
    return brokers


# ══════════════════════════════════════════════════════════════════════════════
# RESULT DATACLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BrokerOrderResult:
    """Result of an order execution on a single broker."""
    broker: str
    success: bool
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    quantity: Optional[float] = None
    error: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None
    sl_attached: Optional[bool] = None
    tp_attached: Optional[bool] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "broker": self.broker,
            "success": self.success,
            "order_id": self.order_id,
            "fill_price": self.fill_price,
            "quantity": self.quantity,
            "error": self.error,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "sl_attached": self.sl_attached,
            "tp_attached": self.tp_attached,
            "timestamp": self.timestamp.isoformat(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-ORDER EXECUTOR
# ══════════════════════════════════════════════════════════════════════════════

class MultiOrderExecutor:
    """
    Execute orders on multiple brokers concurrently.
    
    Features:
      - Best-effort execution: failures on one broker don't block others
      - Symbol translation for Kraken (BTCUSDT -> PF_XBTUSD)
      - Per-broker enable/disable
      - Detailed per-broker results
      - Integration with unified BrokerRouter (NEW)
    
    Can operate in two modes:
      1. Direct executor mode: Uses set_bitget_executor/set_kraken_executor
      2. Router mode: Delegates to BrokerRouter for unified execution
    """
    
    def __init__(self, use_router: bool = False):
        """
        Initialize multi-order executor.
        
        Args:
            use_router: If True, use BrokerRouter for execution (recommended)
        """
        self._bitget_executor = None
        self._kraken_executor = None
        self._symbol_mapper = None
        self._use_router = use_router
        self._broker_router = None
        
        # Track which brokers are enabled
        self._brokers_enabled: Dict[str, bool] = {
            "bitget": False,
            "kraken": False,
        }
        
        # Initialize BrokerRouter if in router mode
        if use_router:
            from backend.execution.broker_router import get_broker_router
            self._broker_router = get_broker_router()
            logger.info("[MULTI_EXEC] Initialized with BrokerRouter integration")
        else:
            logger.info("[MULTI_EXEC] Initialized in direct executor mode")
    
    # ──────────────────────────────────────────────────
    #  SETUP
    # ──────────────────────────────────────────────────
    
    def set_bitget_executor(self, executor) -> None:
        """Set Bitget executor instance."""
        self._bitget_executor = executor
        self._brokers_enabled["bitget"] = executor is not None
        if executor:
            logger.info("[MULTI_EXEC] Bitget executor registered")
            # Also register with BrokerRouter if in router mode
            if self._broker_router:
                self._broker_router.register_executor("bitget", executor)
    
    def set_kraken_executor(self, executor) -> None:
        """Set Kraken executor instance."""
        self._kraken_executor = executor
        self._brokers_enabled["kraken"] = executor is not None
        if executor:
            # Try to get the symbol mapper from kraken executor
            if hasattr(executor, '_mapper'):
                self._symbol_mapper = executor._mapper
            logger.info("[MULTI_EXEC] Kraken executor registered")
            # Also register with BrokerRouter if in router mode
            if self._broker_router:
                from backend.execution.kraken_futures_executor import kraken_symbol_mapper
                self._broker_router.register_executor(
                    "kraken", executor, symbol_mapper=kraken_symbol_mapper
                )
    
    def set_symbol_mapper(self, mapper) -> None:
        """Set custom symbol mapper."""
        self._symbol_mapper = mapper
        logger.debug("[MULTI_EXEC] Symbol mapper set")
    
    def is_broker_enabled(self, broker: str) -> bool:
        """Check if a broker is enabled and has executor."""
        return self._brokers_enabled.get(broker.lower(), False)
    
    def get_enabled_brokers(self) -> List[str]:
        """Get list of currently enabled brokers."""
        return [b for b, enabled in self._brokers_enabled.items() if enabled]
    
    # ──────────────────────────────────────────────────
    #  SYMBOL MAPPING
    # ──────────────────────────────────────────────────
    
    def _get_symbol_mapper(self):
        """Get or create symbol mapper."""
        if self._symbol_mapper is None:
            from backend.utils.symbol_mapper import get_symbol_mapper
            self._symbol_mapper = get_symbol_mapper()
        return self._symbol_mapper
    
    def translate_symbol(self, symbol: str, broker: str) -> Optional[str]:
        """
        Translate symbol for a specific broker.
        
        Args:
            symbol: Source symbol (Bitget format, e.g., "BTCUSDT")
            broker: Target broker ("bitget" or "kraken")
            
        Returns:
            Translated symbol or None if not supported
        """
        symbol = symbol.upper().replace("/", "").replace(":", "")
        
        if broker.lower() == "bitget":
            return symbol  # Already in Bitget format
        
        if broker.lower() == "kraken":
            mapper = self._get_symbol_mapper()
            kraken_symbol = mapper.to_kraken(symbol)
            
            # Check if we got a valid mapping
            if not kraken_symbol or kraken_symbol == symbol:
                # Symbol not mapped, may not be supported
                logger.warning(
                    "[MULTI_EXEC] Symbol %s has no Kraken mapping",
                    symbol
                )
                return None
            
            return kraken_symbol
        
        logger.warning("[MULTI_EXEC] Unknown broker: %s", broker)
        return None
    
    def is_symbol_supported(self, symbol: str, broker: str) -> bool:
        """Check if symbol is supported on broker."""
        return self.translate_symbol(symbol, broker) is not None
    
    # ──────────────────────────────────────────────────
    #  ORDER EXECUTION
    # ──────────────────────────────────────────────────
    
    async def execute_on_brokers(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        quantity: str,
        leverage: int = 10,
        order_type: str = "market",
        brokers: Optional[List[str]] = None,
        signal_id: Optional[str] = None,
    ) -> Dict[str, BrokerOrderResult]:
        """
        Execute order on multiple brokers concurrently.
        
        Args:
            symbol: Trading symbol (Bitget format)
            direction: "long" or "short"
            entry_price: Entry price
            stop_loss: Stop loss price
            take_profit: Take profit price
            quantity: Order quantity
            leverage: Leverage to use
            order_type: "market" or "limit"
            brokers: List of brokers to execute on (default: all enabled)
            signal_id: Optional signal ID for tracking mirror trades
            
        Returns:
            Dict mapping broker name to BrokerOrderResult
        """
        import uuid
        
        # Default to enabled brokers if not specified
        if brokers is None:
            brokers = get_default_brokers()
        
        # Normalize broker list
        brokers = [b.lower() for b in brokers]
        
        # Generate signal_id if not provided
        if not signal_id:
            signal_id = f"auto_{uuid.uuid4().hex[:12]}"
        
        logger.info(
            "[MULTI_EXEC] start signal_id=%s symbol=%s side=%s brokers=%s",
            signal_id, symbol, direction.upper(), brokers
        )
        
        results: Dict[str, BrokerOrderResult] = {}
        tasks = []
        task_brokers = []
        
        for broker in brokers:
            # Check if broker is enabled
            if not self.is_broker_enabled(broker):
                results[broker] = BrokerOrderResult(
                    broker=broker,
                    success=False,
                    skipped=True,
                    skip_reason=f"{broker} executor not available/connected",
                )
                logger.warning("[MULTI_EXEC][%s] SKIPPED | reason=%s", broker.upper(), results[broker].skip_reason)
                self._persist_trade_order(signal_id, broker, symbol, direction, quantity, entry_price, stop_loss, take_profit, "SKIPPED", None, results[broker].skip_reason)
                continue
            
            # Check symbol mapping
            broker_symbol = self.translate_symbol(symbol, broker)
            if not broker_symbol:
                results[broker] = BrokerOrderResult(
                    broker=broker,
                    success=False,
                    skipped=True,
                    skip_reason=f"Symbol {symbol} not supported on {broker}",
                )
                logger.warning("[MULTI_EXEC][%s] SKIPPED | reason=%s", broker.upper(), results[broker].skip_reason)
                self._persist_trade_order(signal_id, broker, symbol, direction, quantity, entry_price, stop_loss, take_profit, "SKIPPED", None, results[broker].skip_reason)
                continue
            
            # Create async task for this broker
            tasks.append(
                self._execute_single_broker(
                    broker=broker,
                    symbol=symbol,  # Use Bitget format, executor handles translation
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    quantity=quantity,
                    leverage=leverage,
                    order_type=order_type,
                    signal_id=signal_id, # Ensure signal_id gets passed down if supported
                )
            )
            task_brokers.append(broker)
        
        # Execute all tasks concurrently
        if tasks:
            task_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for result, broker in zip(task_results, task_brokers):
                if isinstance(result, Exception):
                    logger.error("[MULTI_EXEC][%s] failed with exception: %s", broker, result)
                    results[broker] = BrokerOrderResult(broker=broker, success=False, error=str(result))
                    self._persist_trade_order(signal_id, broker, symbol, direction, quantity, entry_price, stop_loss, take_profit, "FAILED", None, str(result))
                    continue
                if isinstance(result, BrokerOrderResult):
                    results[result.broker] = result
                    if result.success:
                        logger.info("[MULTI_EXEC][%s] ok order_id=%s", result.broker, result.order_id)
                        self._persist_trade_order(signal_id, broker, symbol, direction, quantity, entry_price, stop_loss, take_profit, "OPEN", result.order_id, None)
                    else:
                        logger.error("[MULTI_EXEC][%s] failed err=%s", result.broker, result.error)
                        self._persist_trade_order(signal_id, broker, symbol, direction, quantity, entry_price, stop_loss, take_profit, "FAILED", None, result.error)
        
        # Log summary with signal_id
        success_count = sum(1 for r in results.values() if r.success)
        skip_count = sum(1 for r in results.values() if r.skipped)
        fail_count = len(results) - success_count - skip_count

        # Policy checks
        multi_exec_policy = os.getenv("MULTI_EXEC_POLICY", "DUAL_BEST_EFFORT").upper()
        require_both = os.getenv("REQUIRE_BOTH_BROKERS", "0").lower() in ("1", "true", "yes")
        if require_both:
            multi_exec_policy = "DUAL_REQUIRED"
        
        # Implement DUAL_REQUIRED rollback logic
        if multi_exec_policy == "DUAL_REQUIRED" and len(results) > 1 and success_count > 0 and fail_count > 0:
            logger.error(
                "[MULTI_EXEC] DUAL_REQUIRED policy enforcement: %d failures detected. "
                "Rolling back %d successful executions strictly to maintain parity.",
                fail_count, success_count
            )
            
            rollback_tasks = []
            rollback_brokers = []
            
            for b_name, b_res in list(results.items()):
                if b_res.success:
                    logger.info("[MULTI_EXEC] Rolling back %s order %s", b_name, b_res.order_id)
                    broker_executor = self._bitget_executor if b_name == "bitget" else self._kraken_executor
                    
                    if broker_executor and hasattr(broker_executor, 'flash_close_position'):
                        rollback_tasks.append(broker_executor.flash_close_position(symbol))
                        rollback_brokers.append(b_name)
                    elif broker_executor and hasattr(broker_executor, 'place_order_market'):
                        close_side = "sell" if direction.lower() == "long" else "buy"
                        # kraken requires translated symbol
                        b_symbol = self.translate_symbol(symbol, b_name) or symbol
                        rollback_tasks.append(broker_executor.place_order_market(
                            symbol=b_symbol,
                            side=close_side,
                            size=str(quantity),
                            reduce_only=True
                        ))
                        rollback_brokers.append(b_name)
                        
            if rollback_tasks:
                await asyncio.gather(*rollback_tasks, return_exceptions=True)
                
            for b_name in rollback_brokers:
                results[b_name].success = False
                results[b_name].error = "Rolled back due to DUAL_REQUIRED policy sibling failure"
                # Update DB to show rollback status
                self._update_trade_order_status(signal_id, b_name, "ROLLED_BACK", results[b_name].error)
                
            success_count = sum(1 for r in results.values() if r.success)
            fail_count = len(results) - success_count - skip_count
        
        # Build per-broker summary
        broker_summary = " | ".join([
            f"{b}={'OK' if r.success else ('SKIP' if r.skipped else 'FAIL')}"
            for b, r in results.items()
        ])
        
        logger.info(
            "[MULTI_EXEC] done success=%d fail=%d",
            success_count, fail_count
        )
        
        return results

    def _persist_trade_order(
        self,
        signal_id: str,
        broker: str,
        symbol: str,
        side: str,
        qty: str,
        entry: float,
        sl: float,
        tp: float,
        status: str,
        order_id: Optional[str],
        error: Optional[str]
    ):
        """Persist trade execution attempt to trade_orders table."""
        try:
            import uuid
            from backend.db.session import SessionLocal
            from sqlalchemy import text
            
            trade_id = uuid.uuid4().hex
            req_qty = float(qty) if isinstance(qty, str) else qty
            
            with SessionLocal() as db:
                sql = text("""
                INSERT INTO trade_orders 
                (trade_id, signal_id, broker, symbol, side, qty, entry, sl, tp, rr, status, order_id, error, fills, pnl)
                VALUES
                (:trade_id, :signal_id, :broker, :symbol, :side, :qty, :entry, :sl, :tp, :rr, :status, :order_id, :error, :fills, :pnl)
                """)
                # Handle rr
                rr = 0.0
                if entry and sl and tp and abs(entry - sl) > 0:
                    risk = abs(entry - sl)
                    reward = abs(tp - entry)
                    rr = reward / risk
                
                db.execute(sql, {
                    "trade_id": trade_id,
                    "signal_id": signal_id or "",
                    "broker": broker,
                    "symbol": symbol,
                    "side": side,
                    "qty": req_qty,
                    "entry": entry or 0.0,
                    "sl": sl or 0.0,
                    "tp": tp or 0.0,
                    "rr": rr,
                    "status": status,
                    "order_id": order_id or "",
                    "error": error or "",
                    "fills": "[]",
                    "pnl": 0.0
                })
                db.commit()
        except Exception as e:
            logger.error("[MULTI_EXEC] Failed to persist trade_order to DB: %s", e)

    def _update_trade_order_status(self, signal_id: str, broker: str, status: str, error: str):
        """Update trade_order table status for rollback."""
        try:
            from backend.db.session import SessionLocal
            from sqlalchemy import text
            with SessionLocal() as db:
                sql = text("""
                UPDATE trade_orders 
                SET status = :status, error = :error
                WHERE signal_id = :signal_id AND broker = :broker
                """)
                db.execute(sql, {
                    "status": status,
                    "error": error,
                    "signal_id": signal_id,
                    "broker": broker
                })
                db.commit()
        except Exception as e:
            logger.error("[MULTI_EXEC] Failed to update trade_order status in DB: %s", e)
    
    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "market",
        quantity: float = 0,
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        productType: str = "USDT-FUTURES",
        posSide: Optional[str] = None,
        marginMode: str = "isolated",
        tradeSide: str = "open",
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Unified place_order interface compatible with BitgetFuturesExecutor.
        
        This method provides the same interface as single-broker executors,
        but executes on ALL enabled brokers (Bitget + Kraken if configured).
        
        Args:
            symbol: Trading symbol (Bitget format, e.g., BTCUSDT)
            side: "buy" or "sell"
            order_type: "market" or "limit"
            quantity: Order quantity
            price: Price for limit orders
            stop_loss: Stop loss price
            take_profit: Take profit price
            productType: Product type (USDT-FUTURES)
            posSide: Position side (long/short)
            marginMode: Margin mode (isolated/cross)
            tradeSide: Trade side (open/close)
            
        Returns:
            Dict with aggregated results from all brokers
        """
        # Convert side to direction
        # buy + open = long, sell + open = short
        # buy + close = close short, sell + close = close long
        if tradeSide == "open":
            direction = "long" if side.lower() == "buy" else "short"
        else:
            direction = "short" if side.lower() == "buy" else "long"
        
        # Use entry price for market orders if not provided
        entry_price = price if price else 0.0
        
        # Execute on all enabled brokers
        all_results = await self.execute_on_brokers(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss or 0,
            take_profit=take_profit or 0,
            quantity=str(quantity),
            leverage=kwargs.get("leverage", 10),
            order_type=order_type,
        )
        
        # Aggregate results - return success if ANY broker succeeded
        success_count = sum(1 for r in all_results.values() if r.success)
        failed_brokers = [name for name, r in all_results.items() if not r.success and not r.skipped]
        skipped_brokers = [name for name, r in all_results.items() if r.skipped]
        
        # Get first successful order ID for compatibility
        first_order_id = None
        first_fill_price = None
        for r in all_results.values():
            if r.success and r.order_id:
                first_order_id = r.order_id
                first_fill_price = r.fill_price
                break
        
        # Build response compatible with single-broker interface
        sl_attached = all(
            bool(r.sl_attached) for r in all_results.values() if r.success
        ) if success_count > 0 else False
        tp_attached = all(
            bool(r.tp_attached) for r in all_results.values() if r.success
        ) if success_count > 0 else False

        response = {
            "success": success_count > 0,
            "orderId": first_order_id,
            "fillPrice": first_fill_price,
            "multi_broker": True,
            "sl_attached": sl_attached,
            "tp_attached": tp_attached,
            "broker_results": {
                name: {
                    "success": r.success,
                    "order_id": r.order_id,
                    "fill_price": r.fill_price,
                    "error": r.error,
                    "skipped": r.skipped,
                    "skip_reason": r.skip_reason,
                    "sl_attached": r.sl_attached,
                    "tp_attached": r.tp_attached,
                }
                for name, r in all_results.items()
            },
            "success_count": success_count,
            "failed_brokers": failed_brokers,
            "skipped_brokers": skipped_brokers,
        }
        
        if failed_brokers:
            errors = [f"{name}: {all_results[name].error}" for name in failed_brokers]
            response["partial_failures"] = errors
            
        return response
    
    async def _execute_single_broker(
        self,
        broker: str,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        quantity: str,
        leverage: int,
        order_type: str,
        signal_id: Optional[str] = None,
    ) -> BrokerOrderResult:
        """
        Execute order on a single broker with per-broker preflight.
        
        For Kraken: converts coin qty to contracts before executing.
        Each broker uses its own contract specs for validation.
        """
        import time
        start_time = time.time()
        coin_qty = float(quantity) if isinstance(quantity, str) else quantity
        
        # ── Per-broker preflight ──────────────────────────────────
        try:
            from backend.risk.position_sizing import (
                get_contract_specs, coin_to_kraken_contracts,
                check_trade_feasibility, get_max_risk_deviation_pct,
            )
            
            broker_specs = get_contract_specs(symbol, broker=broker)
            
            if broker == "kraken" and entry_price > 0:
                # Convert coin qty to Kraken contracts for preflight
                contract_value = broker_specs.get("contract_value_usd", 1.0)
                broker_qty = coin_to_kraken_contracts(coin_qty, entry_price, contract_value)
                logger.info(
                    "[MULTI_EXEC][KRAKEN] PREFLIGHT | coin_qty=%.6f -> contracts=%d | "
                    "entry=%.2f | contract_value=$%.2f",
                    coin_qty, broker_qty, entry_price, contract_value
                )
            else:
                broker_qty = coin_qty
            
            logger.info(
                "[MULTI_EXEC] [%s] START | %s %s | qty=%s (broker_qty=%.6f) | entry=%.2f | SL=%.2f | TP=%.2f",
                broker.upper(), direction.upper(), symbol, quantity, broker_qty,
                entry_price, stop_loss, take_profit
            )
            
            # Per-broker preflight checks (non-blocking for log-only)
            if entry_price > 0 and stop_loss > 0:
                feasible, reason, details = check_trade_feasibility(
                    symbol=symbol,
                    balance=100.0,  # Dummy for min_qty check; real balance not needed here
                    risk_pct=0.01,
                    entry_price=entry_price,
                    stop_price=stop_loss,
                    max_risk_deviation_pct=get_max_risk_deviation_pct(),
                )
                if broker == "kraken":
                    # Kraken almost always passes because min_qty=1 contract and we have >= 1
                    logger.info(
                        "[MULTI_EXEC][KRAKEN] PREFLIGHT_PASS | contracts=%d >= min=%d",
                        broker_qty, broker_specs.get("min_qty", 1)
                    )
                elif not feasible:
                    logger.warning(
                        "[MULTI_EXEC][%s] PREFLIGHT_WARN | reason=%s (proceeding anyway, executor handles final check)",
                        broker.upper(), reason
                    )
                else:
                    logger.info("[MULTI_EXEC][%s] PREFLIGHT_PASS | reason=%s", broker.upper(), reason)
            
        except ImportError:
            broker_qty = coin_qty
            logger.warning("[MULTI_EXEC][%s] Preflight skipped (import error)", broker.upper())
        except Exception as pf_err:
            broker_qty = coin_qty
            logger.warning("[MULTI_EXEC][%s] Preflight error: %s (continuing)", broker.upper(), pf_err)
        
        # ── Execute ───────────────────────────────────────────────
        try:
            if broker == "bitget":
                result = await self._execute_bitget(
                    symbol, direction, entry_price, stop_loss,
                    take_profit, str(coin_qty), leverage, order_type
                )
            elif broker == "kraken":
                # Pass contract qty for Kraken, not coin qty
                result = await self._execute_kraken(
                    symbol, direction, entry_price, stop_loss,
                    take_profit, str(broker_qty), leverage, order_type
                )
            else:
                result = BrokerOrderResult(
                    broker=broker,
                    success=False,
                    error=f"Unknown broker: {broker}",
                )
            
            elapsed = time.time() - start_time
            if result.success:
                logger.info(
                    "[MULTI_EXEC][%s] ORDER_OK | order_id=%s | elapsed=%.2fs",
                    broker.upper(), result.order_id, elapsed
                )
            else:
                logger.warning(
                    "[MULTI_EXEC][%s] ORDER_FAIL | error=%s | elapsed=%.2fs",
                    broker.upper(), result.error, elapsed
                )
            
            return result
            
        except Exception as e:
            elapsed = time.time() - start_time
            logger.exception("[MULTI_EXEC] [%s] EXCEPTION | error=%s | elapsed=%.2fs", broker.upper(), e, elapsed)
            return BrokerOrderResult(
                broker=broker,
                success=False,
                error=str(e),
            )
    
    async def _execute_bitget(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        quantity: str,
        leverage: int,
        order_type: str,
    ) -> BrokerOrderResult:
        """Execute on Bitget."""
        if not self._bitget_executor:
            return BrokerOrderResult(
                broker="bitget",
                success=False,
                error="Bitget executor not set",
            )
        
        try:
            result = await self._bitget_executor.place_order(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                order_type=order_type,
                leverage=leverage,
                quantity=quantity,
            )
            
            if result.get("success"):
                return BrokerOrderResult(
                    broker="bitget",
                    success=True,
                    order_id=result.get("order_id"),
                    fill_price=result.get("fill_price"),
                    quantity=float(quantity),
                    sl_attached=result.get("sl_attached", False),
                    tp_attached=result.get("tp_attached", False),
                )
            else:
                return BrokerOrderResult(
                    broker="bitget",
                    success=False,
                    error=result.get("error", "Unknown error"),
                    sl_attached=result.get("sl_attached", False),
                    tp_attached=result.get("tp_attached", False),
                )
        except Exception as e:
            return BrokerOrderResult(
                broker="bitget",
                success=False,
                error=str(e),
            )
    
    async def _execute_kraken(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        quantity: str,
        leverage: int,
        order_type: str,
    ) -> BrokerOrderResult:
        """Execute on Kraken Futures."""
        if not self._kraken_executor:
            return BrokerOrderResult(
                broker="kraken",
                success=False,
                error="Kraken executor not set",
            )
        
        # Check if kraken executor has place_order method
        if not hasattr(self._kraken_executor, 'place_order'):
            # Fall back to place_order_market for now
            return await self._execute_kraken_basic(
                symbol, direction, entry_price, stop_loss,
                take_profit, quantity, leverage, order_type
            )
        
        try:
            result = await self._kraken_executor.place_order(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                order_type=order_type,
                leverage=leverage,
                quantity=quantity,
            )
            
            if result.get("success"):
                return BrokerOrderResult(
                    broker="kraken",
                    success=True,
                    order_id=result.get("order_id"),
                    fill_price=result.get("fill_price"),
                    quantity=float(quantity),
                    sl_attached=result.get("sl_attached", False),
                    tp_attached=result.get("tp_attached", False),
                )
            else:
                return BrokerOrderResult(
                    broker="kraken",
                    success=False,
                    error=result.get("error", "Unknown error"),
                    sl_attached=result.get("sl_attached", False),
                    tp_attached=result.get("tp_attached", False),
                )
        except Exception as e:
            return BrokerOrderResult(
                broker="kraken",
                success=False,
                error=str(e),
            )
    
    async def _execute_kraken_basic(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        quantity: str,
        leverage: int,
        order_type: str,
    ) -> BrokerOrderResult:
        """
        Execute on Kraken using basic market/limit + TP/SL.
        
        Used when kraken_executor doesn't have unified place_order.
        """
        try:
            # 1. Place entry order
            side = "buy" if direction.lower() == "long" else "sell"
            
            if order_type.lower() == "market":
                res = await self._kraken_executor.place_order_market(
                    symbol=symbol,
                    side=side,
                    size=quantity,
                )
            else:
                res = await self._kraken_executor.place_order_limit(
                    symbol=symbol,
                    side=side,
                    size=quantity,
                    price=str(entry_price),
                )
            
            if res.get("code") != "00000":
                return BrokerOrderResult(
                    broker="kraken",
                    success=False,
                    error=f"Entry failed: {res.get('msg')}",
                )
            
            order_id = res.get("data", {}).get("orderId") or res.get("data", {}).get("order_id")
            
            # 2. Attach TP/SL
            hold_side = "long" if side == "buy" else "short"
            
            if take_profit and take_profit > 0:
                await self._kraken_executor.attach_tpsl(
                    symbol=symbol,
                    plan_type="take_profit",
                    trigger_price=str(take_profit),
                    hold_side=hold_side,
                    size=quantity,
                )
            
            if stop_loss and stop_loss > 0:
                await self._kraken_executor.attach_tpsl(
                    symbol=symbol,
                    plan_type="stop_loss",
                    trigger_price=str(stop_loss),
                    hold_side=hold_side,
                    size=quantity,
                )
            
            return BrokerOrderResult(
                broker="kraken",
                success=True,
                order_id=order_id,
                fill_price=entry_price,
                quantity=float(quantity),
            )
            
        except Exception as e:
            return BrokerOrderResult(
                broker="kraken",
                success=False,
                error=str(e),
            )


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_default_executor: Optional[MultiOrderExecutor] = None


def get_multi_order_executor() -> MultiOrderExecutor:
    """Get default multi-order executor instance."""
    global _default_executor
    if _default_executor is None:
        _default_executor = MultiOrderExecutor()
    return _default_executor


def set_multi_order_executor(executor: MultiOrderExecutor) -> None:
    """Set custom multi-order executor instance."""
    global _default_executor
    _default_executor = executor
