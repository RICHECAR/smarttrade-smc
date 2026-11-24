"""
Manual Trade Execution API - Execute signals manually from UI

Endpoints:
- POST /api/manual/preflight - Check if trade can be executed (dry validation)
- POST /api/manual/execute - Execute a manual trade (supports multi-broker)

Features:
- Uses same pipeline as bot (contract_specs, sizing, preflight)
- Per-symbol lock to prevent double execution
- Position check to prevent stacking
- Multi-broker execution support (Bitget + Kraken Futures)
- Detailed error reasons returned to frontend

Author: SMC Trading Bot Framework
"""

import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from smc_futures.engine.execution.contract_specs import get_contract_specs
from smc_futures.engine.strategy.session_filter import SessionFilter
from smc_futures.smc_config import SMC_CONFIG

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/manual", tags=["manual-execution"])


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

def is_multi_broker_enabled() -> bool:
    """Check if multi-broker execution is enabled."""
    return os.getenv("MULTI_BROKER_EXECUTION", "0").lower() in ("1", "true", "yes")


def is_manual_all_brokers_enabled() -> bool:
    """
    Check if manual trades should execute on all enabled brokers.
    
    Env var: MANUAL_EXECUTE_ALL_BROKERS=1
    When set, manual trades will route to both Bitget AND Kraken (if Kraken enabled).
    """
    return os.getenv("MANUAL_EXECUTE_ALL_BROKERS", "0").lower() in ("1", "true", "yes")


def is_kraken_enabled() -> bool:
    """Check if Kraken Futures is enabled."""
    return os.getenv("KRAKEN_ENABLED", "0").lower() in ("1", "true", "yes")


def get_enabled_brokers() -> List[str]:
    """
    Get list of brokers for manual trade execution.
    
    Kraken is the primary and default broker.
    Multi-broker mode (legacy) can re-enable Bitget if explicitly configured.
    """
    # Debug log the flag values
    manual_all = is_manual_all_brokers_enabled()
    kraken_en = is_kraken_enabled()
    multi_broker = is_multi_broker_enabled()
    
    logger.debug(
        "[MANUAL_API] get_enabled_brokers: MANUAL_EXECUTE_ALL_BROKERS=%s, KRAKEN_ENABLED=%s, MULTI_BROKER_EXECUTION=%s",
        manual_all, kraken_en, multi_broker
    )
    
    # Multi-broker mode: include Bitget only if explicitly requested
    if multi_broker and manual_all:
        logger.info("[MANUAL_API] Multi-broker enabled: returning ['kraken', 'bitget']")
        return ["kraken", "bitget"]
    
    logger.debug("[MANUAL_API] Default broker: returning ['kraken']")
    return ["kraken"]


def is_manual_trades_allowed() -> bool:
    """
    Check if manual trades are enabled AND trading mode is live.
    
    Security: Block real orders when TRADING_MODE != live, even if ALLOW_MANUAL_TRADES=true.
    This prevents accidental real order execution in paper/test environments.
    """
    # Check explicit permission
    allowed = os.getenv("ALLOW_MANUAL_TRADES", "1").lower() in ("1", "true", "yes")
    if not allowed:
        return False
    
    # Security: Must be in live mode to execute real orders
    trading_mode = SMC_CONFIG.get("TRADING_MODE", "paper")
    if trading_mode != "live":
        logger.warning(f"[MANUAL_API] Blocked: TRADING_MODE={trading_mode} (requires 'live')")
        return False
    
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# PER-SYMBOL EXECUTION LOCK
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SymbolLockManager:
    """
    Manages per-symbol locks to prevent concurrent executions.
    Thread-safe via asyncio.Lock per symbol.
    """
    _locks: Dict[str, asyncio.Lock] = field(default_factory=dict)
    _executing: Dict[str, bool] = field(default_factory=dict)
    
    def get_lock(self, symbol: str) -> asyncio.Lock:
        """Get or create a lock for a symbol."""
        if symbol not in self._locks:
            self._locks[symbol] = asyncio.Lock()
        return self._locks[symbol]
    
    def is_executing(self, symbol: str) -> bool:
        """Check if a symbol is currently being executed."""
        return self._executing.get(symbol, False)
    
    def set_executing(self, symbol: str, value: bool) -> None:
        """Set execution state for a symbol."""
        self._executing[symbol] = value


# Singleton instance
_lock_manager = SymbolLockManager()


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL REFERENCES (Set during app startup)
# ═══════════════════════════════════════════════════════════════════════════════

_executor = None  # Bitget executor
_kraken_executor = None  # Kraken executor
_multi_order_executor = None  # Multi-broker executor
_order_manager = None
_app_context = None


def set_manual_executor(executor):
    """Set the Bitget executor reference."""
    global _executor, _multi_order_executor
    _executor = executor
    logger.info("[MANUAL_API] Bitget executor registered")
    
    # Update multi-order executor
    if _multi_order_executor:
        _multi_order_executor.set_bitget_executor(executor)


def set_manual_kraken_executor(executor):
    """Set the Kraken executor reference."""
    global _kraken_executor, _multi_order_executor
    _kraken_executor = executor
    logger.info("[MANUAL_API] Kraken executor registered")
    
    # Update multi-order executor
    if _multi_order_executor:
        _multi_order_executor.set_kraken_executor(executor)


def set_manual_multi_executor(executor):
    """Set the multi-order executor reference."""
    global _multi_order_executor
    _multi_order_executor = executor
    
    # Wire up existing executors
    if _executor:
        executor.set_bitget_executor(_executor)
    if _kraken_executor:
        executor.set_kraken_executor(_kraken_executor)
    
    logger.info("[MANUAL_API] Multi-order executor registered")


def get_multi_order_executor():
    """Get or create multi-order executor."""
    global _multi_order_executor
    if _multi_order_executor is None:
        from backend.services.multi_order_executor import MultiOrderExecutor
        _multi_order_executor = MultiOrderExecutor()
        logger.info(
            "[MANUAL_API] Creating MultiOrderExecutor: _executor=%s, _kraken_executor=%s",
            _executor is not None, _kraken_executor is not None
        )
        if _executor:
            _multi_order_executor.set_bitget_executor(_executor)
            logger.info("[MANUAL_API] Bitget executor registered on MultiOrderExecutor")
        if _kraken_executor:
            _multi_order_executor.set_kraken_executor(_kraken_executor)
            logger.info("[MANUAL_API] Kraken executor registered on MultiOrderExecutor")
    return _multi_order_executor


def set_manual_order_manager(order_mgr):
    """Set the OrderManager reference."""
    global _order_manager
    _order_manager = order_mgr
    logger.info("[MANUAL_API] OrderManager registered")


def set_manual_app_context(ctx):
    """Set the app context reference."""
    global _app_context
    _app_context = ctx
    logger.info("[MANUAL_API] AppContext registered")


# ═══════════════════════════════════════════════════════════════════════════════
# REQUEST/RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class ManualTradeIntent(BaseModel):
    """Trade intent from frontend."""
    symbol: str = Field(..., description="Trading pair (e.g., XRPUSDT)")
    side: str = Field(..., description="'long' or 'short'")
    entry_price: float = Field(..., gt=0, description="Entry price")
    stop_loss: float = Field(..., gt=0, description="Stop loss price")
    take_profit: Optional[float] = Field(None, gt=0, description="Take profit price")
    leverage: int = Field(10, ge=1, le=125, description="Leverage (1-125)")
    risk_pct: float = Field(1.0, ge=0.1, le=5.0, description="Risk percentage (0.1-5%)")
    # Multi-broker support
    brokers: Optional[List[str]] = Field(
        None, 
        description="Brokers to execute on: ['bitget', 'kraken']. Defaults to config."
    )


class PreflightResponse(BaseModel):
    """Response for preflight check."""
    can_execute: bool
    reason: str
    computed_qty: Optional[float] = None
    min_qty: Optional[float] = None
    min_notional: Optional[float] = None
    notional: Optional[float] = None
    risk_amount: Optional[float] = None
    risk_deviation_pct: Optional[float] = None
    max_risk_deviation_pct: float = 50.0
    account_balance: Optional[float] = None
    has_open_position: bool = False
    is_locked: bool = False
    # Session filter info
    session_allowed: bool = True
    session_name: Optional[str] = None
    timezone_used: str = "UTC"
    session_reason: Optional[str] = None
    # Multi-broker support
    brokers_enabled: Optional[List[str]] = None
    multi_broker_mode: bool = False


class BrokerResult(BaseModel):
    """Result from a single broker execution."""
    broker: str
    success: bool
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    error: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None


class ExecuteResponse(BaseModel):
    """Response for execute request."""
    success: bool
    reason: str
    order_id: Optional[str] = None
    symbol: Optional[str] = None
    side: Optional[str] = None
    qty: Optional[float] = None
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trade_id: Optional[str] = None
    # Multi-broker results
    multi_broker: bool = False
    broker_results: Optional[Dict[str, BrokerResult]] = None
    brokers_success: Optional[int] = None
    brokers_failed: Optional[int] = None
    brokers_skipped: Optional[int] = None


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

async def get_account_balance() -> float:
    """
    Get current account equity from executor (used for risk sizing).
    Returns equity (preferred) or available balance as fallback.
    """
    if not _executor:
        logger.warning("[MANUAL] No executor set, returning 0 balance")
        return 0.0
    
    try:
        # Call the correct method: get_account_balance() returns {"available", "equity", ...}
        result = await _executor.get_account_balance(marginCoin="USDT")
        
        available = float(result.get("available", 0.0) or 0.0)
        equity = float(result.get("equity", 0.0) or 0.0)
        
        # Prefer equity for risk sizing (includes unrealized PnL), fallback to available
        balance = equity if equity > 0 else available
        
        if balance > 0:
            logger.info(f"[MANUAL] Balance OK: equity={equity:.2f} available={available:.2f} → using {balance:.2f} USDT")
        else:
            error_msg = result.get("error")
            if error_msg:
                logger.warning(f"[MANUAL] Balance API error: {error_msg}")
            else:
                logger.warning("[MANUAL] Balance returned 0 - check API credentials or account")
        
        return balance
    except AttributeError as e:
        logger.error(f"[MANUAL] Executor missing get_account_balance method: {e}")
        return 0.0
    except Exception as e:
        logger.error(f"[MANUAL] Failed to get balance: {e}")
        return 0.0


async def check_open_position(symbol: str) -> bool:
    """Check if there's an open position for the symbol."""
    if not _executor:
        return False
    
    try:
        result = await _executor.get_positions(symbol)
        if result.get("code") == "00000":
            positions = result.get("positions", []) or result.get("data", [])
            for pos in positions:
                pos_symbol = pos.get("symbol", "").upper()
                pos_size = float(pos.get("total", 0) or pos.get("size", 0) or 0)
                if pos_symbol == symbol.upper() and pos_size > 0:
                    return True
        return False
    except Exception as e:
        logger.error(f"[MANUAL] Failed to check positions: {e}")
        return False


def compute_quantity(
    symbol: str,
    entry_price: float,
    stop_loss: float,
    account_balance: float,
    risk_pct: float,
    leverage: int,
) -> Dict[str, Any]:
    """
    Compute position size using the same logic as the bot.
    
    Returns dict with: qty, min_qty, min_notional, notional, risk_amount, status, reason
    """
    specs = get_contract_specs(symbol)
    
    # Calculate risk amount
    risk_amount = account_balance * (risk_pct / 100.0)
    
    # Calculate stop distance
    stop_distance = abs(entry_price - stop_loss)
    stop_distance_pct = stop_distance / entry_price if entry_price > 0 else 0
    
    if stop_distance <= 0:
        return {
            "qty": None,
            "status": "REJECTED",
            "reason": "INVALID_STOP_DISTANCE",
            "details": {"stop_distance": stop_distance},
        }
    
    # Calculate raw quantity: qty = risk_amount / stop_distance
    # With leverage: effective qty = (risk_amount * leverage) / stop_distance
    raw_qty = risk_amount / stop_distance
    
    # Normalize quantity using contract specs
    normalized_qty, status, details = specs.normalize_quantity(
        raw_qty=raw_qty,
        entry_price=entry_price,
        max_risk_deviation_pct=50.0,
    )
    
    # Calculate notional
    notional = (normalized_qty or raw_qty) * entry_price if (normalized_qty or raw_qty) else 0
    
    return {
        "qty": normalized_qty,
        "raw_qty": raw_qty,
        "min_qty": specs.min_qty,
        "min_notional": specs.min_notional,
        "notional": notional,
        "risk_amount": risk_amount,
        "stop_distance": stop_distance,
        "status": status,
        "reason": details.get("rejection_reason") or (f"raw_qty_{raw_qty:.6f}_min_qty_{specs.min_qty}" if status == "REJECTED" else None),
        "details": details,
        "risk_deviation_pct": details.get("deviation_pct") or details.get("notional_deviation_pct") or 0,
    }


def check_session_info() -> Dict[str, Any]:
    """
    Check current session status for manual trade.
    
    Returns dict with:
        - allowed: bool (True if inside allowed sessions)
        - session_name: str (LONDON, NEW_YORK, LONDON_NY_OVERLAP, ASIAN/OFF)
        - timezone: str (always "UTC")
        - reason: str | None (rejection reason if not allowed)
    
    Session windows (UTC):
        - london: 08:00 - 16:00
        - ny:     13:00 - 21:00
        - overlap: 13:00 - 16:00 (highest volume)
    """
    allowed_sessions = SMC_CONFIG.get("SESSIONS", ["london", "ny"])
    session_filter = SessionFilter(allowed_sessions=allowed_sessions)
    
    # Use current UTC timestamp
    now_utc = datetime.now(timezone.utc)
    timestamp_ms = int(now_utc.timestamp() * 1000)
    
    is_allowed = session_filter.is_in_session(timestamp_ms)
    session_name = session_filter.get_session_name(timestamp_ms)
    
    # Determine rejection reason
    reason = None
    if not is_allowed:
        if now_utc.weekday() >= 5:
            reason = "WEEKEND_NO_TRADING"
        else:
            reason = "OUTSIDE_ALLOWED_SESSIONS"
    
    return {
        "allowed": is_allowed,
        "session_name": session_name,
        "timezone": "UTC",
        "reason": reason,
        "hour_utc": now_utc.hour,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# API ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/preflight", response_model=PreflightResponse)
async def preflight_check(intent: ManualTradeIntent) -> PreflightResponse:
    """
    Pre-flight check for manual trade execution.
    
    Validates:
    - Manual trades enabled
    - No open position on symbol
    - No execution in progress (lock)
    - Quantity meets min_qty and min_notional
    - Risk deviation within limits
    """
    symbol = intent.symbol.upper().replace("/", "").replace(":", "")
    
    logger.info(
        f"[MANUAL_PREFLIGHT] {symbol} {intent.side.upper()} @ {intent.entry_price}"
        f" | SL={intent.stop_loss} | TP={intent.take_profit}"
    )
    
    # 0. Check session status (informational + optional blocking)
    session_info = check_session_info()
    bypass_session = SMC_CONFIG.get("BYPASS_SESSION_FILTER_FOR_MANUAL", True)
    
    # If bypass disabled and outside session, block execution
    if not bypass_session and not session_info["allowed"]:
        logger.info(
            f"[MANUAL_PREFLIGHT] {symbol} | BLOCKED | reason=SESSION_OUTSIDE"
            f" | session={session_info['session_name']} | hour={session_info['hour_utc']}"
        )
        return PreflightResponse(
            can_execute=False,
            reason="SESSION_OUTSIDE",
            session_allowed=False,
            session_name=session_info["session_name"],
            timezone_used=session_info["timezone"],
            session_reason=session_info["reason"],
        )
    
    # 1. Check if manual trades are enabled
    if not is_manual_trades_allowed():
        return PreflightResponse(
            can_execute=False,
            reason="MANUAL_TRADES_DISABLED",
            session_allowed=session_info["allowed"],
            session_name=session_info["session_name"],
            timezone_used=session_info["timezone"],
            session_reason=session_info["reason"],
        )
    
    # 2. Check if executor is available
    if not _executor:
        return PreflightResponse(
            can_execute=False,
            reason="EXECUTOR_NOT_AVAILABLE",
            session_allowed=session_info["allowed"],
            session_name=session_info["session_name"],
            timezone_used=session_info["timezone"],
            session_reason=session_info["reason"],
        )
    
    # 3. Check per-symbol lock
    if _lock_manager.is_executing(symbol):
        return PreflightResponse(
            can_execute=False,
            reason="EXECUTION_IN_PROGRESS",
            is_locked=True,
            session_allowed=session_info["allowed"],
            session_name=session_info["session_name"],
            timezone_used=session_info["timezone"],
            session_reason=session_info["reason"],
        )
    
    # 4. Check for open position
    has_position = await check_open_position(symbol)
    if has_position:
        return PreflightResponse(
            can_execute=False,
            reason="POSITION_ALREADY_OPEN",
            has_open_position=True,
            session_allowed=session_info["allowed"],
            session_name=session_info["session_name"],
            timezone_used=session_info["timezone"],
            session_reason=session_info["reason"],
        )
    
    # 5. Get account balance
    balance = await get_account_balance()
    if balance <= 0:
        return PreflightResponse(
            can_execute=False,
            reason="ZERO_BALANCE",
            account_balance=0,
            session_allowed=session_info["allowed"],
            session_name=session_info["session_name"],
            timezone_used=session_info["timezone"],
            session_reason=session_info["reason"],
        )
    
    # 6. Compute quantity
    qty_result = compute_quantity(
        symbol=symbol,
        entry_price=intent.entry_price,
        stop_loss=intent.stop_loss,
        account_balance=balance,
        risk_pct=intent.risk_pct,
        leverage=intent.leverage,
    )
    
    if qty_result["qty"] is None:
        # Build detailed reason message
        raw_qty = qty_result.get("raw_qty", 0)
        risk_deviation = qty_result.get("risk_deviation_pct") or qty_result.get("details", {}).get("deviation_pct", 0)
        reason = qty_result.get("reason") or f"raw_qty_{raw_qty:.6f}_invalid"
        
        # Make reason more human-readable
        if "risk_deviation" in reason:
            reason = f"raw_qty {raw_qty:.6f} < min_qty {qty_result.get('min_qty')} (risk deviation {risk_deviation:.1f}%)"
        elif "notional" in reason:
            reason = f"notional ${qty_result.get('notional', 0):.2f} < min ${qty_result.get('min_notional', 5)}"
        elif raw_qty < qty_result.get("min_qty", 0):
            reason = f"raw_qty {raw_qty:.6f} < min_qty {qty_result.get('min_qty')} (risk deviation {risk_deviation:.1f}%)"
        
        return PreflightResponse(
            can_execute=False,
            reason=reason,
            min_qty=qty_result.get("min_qty"),
            min_notional=qty_result.get("min_notional"),
            computed_qty=raw_qty,
            notional=qty_result.get("notional"),
            risk_amount=qty_result.get("risk_amount"),
            risk_deviation_pct=risk_deviation,
            account_balance=balance,
            session_allowed=session_info["allowed"],
            session_name=session_info["session_name"],
            timezone_used=session_info["timezone"],
            session_reason=session_info["reason"],
        )
    
    # 7. Check risk deviation
    details = qty_result.get("details", {})
    risk_deviation = details.get("risk_deviation_pct", 0)
    
    if qty_result["status"] == "REJECTED":
        return PreflightResponse(
            can_execute=False,
            reason=qty_result.get("reason", "RISK_DEVIATION_EXCEEDS_MAX"),
            computed_qty=qty_result["qty"],
            min_qty=qty_result["min_qty"],
            min_notional=qty_result["min_notional"],
            notional=qty_result["notional"],
            risk_amount=qty_result["risk_amount"],
            risk_deviation_pct=risk_deviation,
            account_balance=balance,
            session_allowed=session_info["allowed"],
            session_name=session_info["session_name"],
            timezone_used=session_info["timezone"],
            session_reason=session_info["reason"],
        )
    
    # All checks passed
    return PreflightResponse(
        can_execute=True,
        reason="OK",
        computed_qty=qty_result["qty"],
        min_qty=qty_result["min_qty"],
        min_notional=qty_result["min_notional"],
        notional=qty_result["notional"],
        risk_amount=qty_result["risk_amount"],
        risk_deviation_pct=risk_deviation,
        account_balance=balance,
        has_open_position=False,
        is_locked=False,
        session_allowed=session_info["allowed"],
        session_name=session_info["session_name"],
        timezone_used=session_info["timezone"],
        session_reason=session_info["reason"],
    )


@router.post("/execute", response_model=ExecuteResponse)
async def execute_manual_trade(intent: ManualTradeIntent) -> ExecuteResponse:
    """
    Execute a manual trade on one or more brokers.
    
    Supports multi-broker execution when:
    - MANUAL_EXECUTE_ALL_BROKERS=1 AND KRAKEN_ENABLED=1, OR
    - MULTI_BROKER_EXECUTION=1 env var is set, OR
    - brokers=['bitget','kraken'] is explicitly passed in request
    
    Uses the same pipeline as the bot:
    1. Pre-flight checks
    2. Acquire per-symbol lock
    3. Compute quantity and validate
    4. Execute on broker(s) via asyncio.gather
    5. Persist to SQLite
    6. Return per-broker results
    """
    symbol = intent.symbol.upper().replace("/", "").replace(":", "")
    side = intent.side.lower()
    
    # Determine brokers to use
    # Priority: explicit request > MANUAL_EXECUTE_ALL_BROKERS > MULTI_BROKER_EXECUTION > default
    brokers = intent.brokers if intent.brokers else get_enabled_brokers()
    use_multi_broker = len(brokers) > 1
    
    logger.info(
        f"[MANUAL_EXECUTE] {symbol} {side.upper()} @ {intent.entry_price}"
        f" | SL={intent.stop_loss} | TP={intent.take_profit}"
        f" | brokers={brokers} | multi={use_multi_broker}"
    )
    
    # 1. Check if manual trades are enabled
    if not is_manual_trades_allowed():
        return ExecuteResponse(
            success=False,
            reason="MANUAL_TRADES_DISABLED",
            multi_broker=use_multi_broker,
        )
    
    # 2. Check at least one executor is available
    if not _executor and not _kraken_executor:
        return ExecuteResponse(
            success=False,
            reason="NO_EXECUTOR_AVAILABLE",
            multi_broker=use_multi_broker,
        )
    
    # 3. Acquire per-symbol lock
    lock = _lock_manager.get_lock(symbol)
    
    if _lock_manager.is_executing(symbol):
        return ExecuteResponse(
            success=False,
            reason="EXECUTION_IN_PROGRESS",
            multi_broker=use_multi_broker,
        )
    
    async with lock:
        _lock_manager.set_executing(symbol, True)
        
        try:
            # 4. Verify no open position (on Bitget - primary)
            if _executor:
                has_position = await check_open_position(symbol)
                if has_position:
                    return ExecuteResponse(
                        success=False,
                        reason="POSITION_ALREADY_OPEN",
                        multi_broker=use_multi_broker,
                    )
            
            # 5. Get balance and compute quantity
            balance = await get_account_balance()
            if balance <= 0:
                return ExecuteResponse(
                    success=False,
                    reason="ZERO_BALANCE",
                    multi_broker=use_multi_broker,
                )
            
            qty_result = compute_quantity(
                symbol=symbol,
                entry_price=intent.entry_price,
                stop_loss=intent.stop_loss,
                account_balance=balance,
                risk_pct=intent.risk_pct,
                leverage=intent.leverage,
            )
            
            if qty_result["qty"] is None:
                return ExecuteResponse(
                    success=False,
                    reason=qty_result.get("reason", "QTY_VALIDATION_FAILED"),
                    multi_broker=use_multi_broker,
                )
            
            quantity = qty_result["qty"]
            
            # ══════════════════════════════════════════════════════════════════
            # EXECUTION PATH: Multi-broker or single-broker
            # ══════════════════════════════════════════════════════════════════
            
            logger.info(
                "[MANUAL_EXECUTE] Execution path: use_multi_broker=%s | brokers=%s | "
                "_executor=%s | _kraken_executor=%s",
                use_multi_broker, brokers,
                _executor is not None, _kraken_executor is not None
            )
            
            if use_multi_broker:
                # Multi-broker execution
                return await _execute_multi_broker(
                    symbol=symbol,
                    side=side,
                    intent=intent,
                    quantity=quantity,
                    brokers=brokers,
                )
            else:
                # Single-broker execution (original behavior)
                return await _execute_single_broker(
                    symbol=symbol,
                    side=side,
                    intent=intent,
                    quantity=quantity,
                )
            
        except Exception as e:
            logger.error(f"[MANUAL_EXECUTE] Exception: {e}", exc_info=True)
            return ExecuteResponse(
                success=False,
                reason=str(e),
                multi_broker=use_multi_broker,
            )
        
        finally:
            _lock_manager.set_executing(symbol, False)


async def _execute_single_broker(
    symbol: str,
    side: str,
    intent: ManualTradeIntent,
    quantity: float,
) -> ExecuteResponse:
    """Execute on single broker (Bitget) - original behavior."""
    
    # Set leverage
    if hasattr(_executor, "set_leverage"):
        lev_result = await _executor.set_leverage(symbol, intent.leverage)
        if lev_result.get("code") != "00000":
            logger.warning(f"[MANUAL] Leverage set warning: {lev_result.get('msg')}")
    
    # Place order
    result = await _executor.place_order(
        symbol=symbol,
        direction=side,
        entry_price=intent.entry_price,
        stop_loss=intent.stop_loss,
        take_profit=intent.take_profit or 0,
        order_type="market",
        leverage=intent.leverage,
        quantity=str(quantity),
    )
    
    if not result.get("success"):
        error_msg = result.get("error", "Unknown execution error")
        logger.error(f"[MANUAL_EXECUTE] FAILED | {symbol} | {error_msg}")
        return ExecuteResponse(
            success=False,
            reason=error_msg,
            multi_broker=False,
        )
    
    order_id = result.get("order_id") or result.get("entry_order_id")
    fill_price = result.get("fill_price") or result.get("price_executed") or intent.entry_price
    
    logger.info(
        f"[MANUAL_EXECUTE] SUCCESS | {symbol} {side.upper()}"
        f" | order_id={order_id} | qty={quantity}"
    )
    
    # Persist to SQLite
    trade_id = await _persist_trade(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        fill_price=fill_price,
        intent=intent,
        broker="kraken",
    )
    
    return ExecuteResponse(
        success=True,
        reason="OK",
        order_id=order_id,
        symbol=symbol,
        side=side,
        qty=quantity,
        entry_price=fill_price,
        stop_loss=intent.stop_loss,
        take_profit=intent.take_profit,
        trade_id=trade_id,
        multi_broker=False,
    )


async def _execute_multi_broker(
    symbol: str,
    side: str,
    intent: ManualTradeIntent,
    quantity: float,
    brokers: List[str],
) -> ExecuteResponse:
    """Execute on multiple brokers concurrently."""
    
    executor = get_multi_order_executor()
    
    # Execute on all specified brokers
    results = await executor.execute_on_brokers(
        symbol=symbol,
        direction=side,
        entry_price=intent.entry_price,
        stop_loss=intent.stop_loss,
        take_profit=intent.take_profit or 0,
        quantity=str(quantity),
        leverage=intent.leverage,
        order_type="market",
        brokers=brokers,
    )
    
    # Convert results to response format
    broker_results = {}
    success_count = 0
    fail_count = 0
    skip_count = 0
    first_order_id = None
    first_fill_price = None
    
    for broker_name, result in results.items():
        broker_results[broker_name] = BrokerResult(
            broker=broker_name,
            success=result.success,
            order_id=result.order_id,
            fill_price=result.fill_price,
            error=result.error,
            skipped=result.skipped,
            skip_reason=result.skip_reason,
        )
        
        if result.success:
            success_count += 1
            if first_order_id is None:
                first_order_id = result.order_id
                first_fill_price = result.fill_price
        elif result.skipped:
            skip_count += 1
        else:
            fail_count += 1
    
    # Determine overall success
    overall_success = success_count > 0
    
    if overall_success:
        reason = f"OK ({success_count} broker(s) succeeded)"
    elif skip_count == len(brokers):
        reason = "ALL_BROKERS_SKIPPED"
    else:
        reason = f"ALL_BROKERS_FAILED ({fail_count} failed, {skip_count} skipped)"
    
    logger.info(
        f"[MANUAL_EXECUTE_MULTI] {symbol} {side.upper()}"
        f" | success={success_count} fail={fail_count} skip={skip_count}"
    )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # PERSIST TRADES FOR ALL BROKERS (SUCCESS + FAILED) WITH SHARED SIGNAL_ID
    # ═══════════════════════════════════════════════════════════════════════════
    import uuid
    signal_id = f"mirror_{uuid.uuid4().hex[:12]}"  # Shared signal_id for all brokers
    
    trade_id = None
    parent_id = None
    
    # First, persist primary broker (Kraken) - success or failure
    if "kraken" in broker_results:
        kraken_result = broker_results["kraken"]
        if kraken_result.success:
            trade_id = await _persist_trade(
                order_id=kraken_result.order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=kraken_result.fill_price or intent.entry_price,
                intent=intent,
                broker="kraken",
                signal_id=signal_id,
                status="open",
            )
            parent_id = trade_id
        elif not kraken_result.skipped:
            # Persist FAILED trade for visibility in dashboard
            trade_id = await _persist_trade(
                order_id=f"FAILED_{signal_id}_kraken",
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=0,
                intent=intent,
                broker="kraken",
                signal_id=signal_id,
                status="failed",
                error_message=kraken_result.error,
            )
            parent_id = trade_id
    
    # Then persist all other broker trades (success AND failures)
    for broker_name, result in broker_results.items():
        if broker_name == "kraken":
            continue  # Already persisted
        
        if result.skipped:
            continue  # Don't persist skipped brokers
        
        if result.success:
            await _persist_trade(
                order_id=result.order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=result.fill_price or intent.entry_price,
                intent=intent,
                broker=broker_name,
                parent_trade_id=parent_id,
                signal_id=signal_id,
                status="open",
            )
        else:
            # Persist FAILED trade for this broker
            await _persist_trade(
                order_id=f"FAILED_{signal_id}_{broker_name}",
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=0,
                intent=intent,
                broker=broker_name,
                parent_trade_id=parent_id,
                signal_id=signal_id,
                status="failed",
                error_message=result.error,
            )
    
    logger.info(
        f"[LIVE_DB] MIRROR TRADES PERSISTED | signal_id={signal_id}"
        f" | success={success_count} | failed={fail_count}"
    )
    
    return ExecuteResponse(
        success=overall_success,
        reason=reason,
        order_id=first_order_id,
        symbol=symbol,
        side=side,
        qty=quantity,
        entry_price=first_fill_price or intent.entry_price,
        stop_loss=intent.stop_loss,
        take_profit=intent.take_profit,
        trade_id=trade_id,
        multi_broker=True,
        broker_results=broker_results,
        brokers_success=success_count,
        brokers_failed=fail_count,
        brokers_skipped=skip_count,
    )


async def _persist_trade(
    order_id: str,
    symbol: str,
    side: str,
    quantity: float,
    fill_price: float,
    intent: ManualTradeIntent,
    broker: str = "kraken",
    parent_trade_id: Optional[str] = None,
    signal_id: Optional[str] = None,
    status: str = "open",
    error_message: Optional[str] = None,
) -> Optional[str]:
    """
    Persist trade to SQLite database with broker tracking.
    
    Supports persisting both successful AND failed trades for mirror visibility.
    
    Args:
        order_id: Broker order ID (or FAILED_xxx for failed trades)
        symbol: Trading symbol
        side: long/short
        quantity: Trade quantity
        fill_price: Entry price (0 for failed)
        intent: Original trade intent
        broker: Broker name (bitget/kraken)
        parent_trade_id: Parent trade ID for mirror linking
        signal_id: Shared signal ID for mirror group
        status: Trade status (open/closed/failed)
        error_message: Error message if trade failed
    """
    try:
        from backend.services.trade_persistence_hook import on_trade_opened
        
        # Generate signal_id if not provided
        if not signal_id:
            signal_id = f"manual_{broker}_{datetime.now(timezone.utc).timestamp()}"
        
        trade = await on_trade_opened(
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=quantity,
            entry=fill_price,
            stop=intent.stop_loss,
            tp=intent.take_profit or 0,
            leverage=intent.leverage,
            session_name="manual",
            preset=f"manual_button_{broker}",
            signal_id=signal_id,
            broker=broker,
            parent_trade_id=parent_trade_id,
            status=status,
            error_message=error_message,
        )
        
        if trade:
            status_emoji = "✓" if status == "open" else "✗"
            logger.info(
                f"[LIVE_DB] TRADE CREATED {status_emoji} | trade_id={trade.id}"
                f" | broker={broker} | signal_id={signal_id} | status={status}"
            )
            return trade.id
            
    except Exception as e:
        logger.error(f"[LIVE_DB] Persistence error: {e}")
    
    return None


@router.get("/status")
async def get_manual_status() -> Dict[str, Any]:
    """
    Get manual execution status.
    
    Returns:
    - enabled: Whether manual trades are allowed
    - executor_ready: Whether executor is available
    - locked_symbols: List of symbols currently executing
    - multi_broker_enabled: Whether multi-broker mode is active
    - brokers: Status of each broker
    """
    locked = [s for s, v in _lock_manager._executing.items() if v]
    
    # Get multi-order executor to check broker status
    multi_exec = get_multi_order_executor()
    
    # Get enabled brokers based on env config
    env_brokers = get_enabled_brokers()
    
    return {
        "enabled": is_manual_trades_allowed(),
        "executor_ready": _executor is not None,
        "kraken_executor_ready": _kraken_executor is not None,
        "order_manager_ready": _order_manager is not None,
        "locked_symbols": locked,
        # Env flags
        "env_flags": {
            "MANUAL_EXECUTE_ALL_BROKERS": is_manual_all_brokers_enabled(),
            "KRAKEN_ENABLED": is_kraken_enabled(),
            "MULTI_BROKER_EXECUTION": is_multi_broker_enabled(),
        },
        # Broker status from env config
        "multi_broker_enabled": len(env_brokers) > 1,
        "env_enabled_brokers": env_brokers,
        # Broker status from executor registration
        "brokers": {
            "bitget": multi_exec.is_broker_enabled("bitget"),
            "kraken": multi_exec.is_broker_enabled("kraken"),
        },
        "enabled_brokers": multi_exec.get_enabled_brokers(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

