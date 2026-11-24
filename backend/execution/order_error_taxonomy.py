"""
Order Error Taxonomy — Classifies every possible broker failure.
═══════════════════════════════════════════════════════════════════

Every order failure MUST map to exactly one ErrorCode so that:
  - Logs are machine-parseable
  - Alerts can be auto-routed by severity
  - Post-mortem requires zero guesswork

Author: SMC Trading Bot Framework
"""

from enum import Enum
from typing import Any, Dict, Optional


class ErrorSeverity(Enum):
    """Severity levels for order errors."""
    FATAL = "fatal"        # Requires immediate human intervention
    HIGH = "high"          # Order failed, may need manual review
    MEDIUM = "medium"      # Recoverable, auto-retry possible
    LOW = "low"            # Informational, no action needed


class ErrorCode(Enum):
    """Normalized error codes for all broker failures."""

    # ── Pre-Broker Validation (100s) ──────────────────────────────
    ZERO_QUANTITY = "ZERO_QUANTITY"             # qty <= 0
    INVALID_SYMBOL = "INVALID_SYMBOL"           # symbol not in mapping
    INVALID_SIDE = "INVALID_SIDE"               # side not buy/sell
    INVALID_ORDER_TYPE = "INVALID_ORDER_TYPE"   # type not market/limit
    MISSING_LIMIT_PRICE = "MISSING_LIMIT_PRICE" # limit order without price
    FRACTIONAL_CONTRACTS = "FRACTIONAL_QTY"     # Kraken requires integer
    REDUCE_ONLY_ON_OPEN = "REDUCE_ONLY_OPEN"   # reduce_only=True on open intent

    # ── Broker Connection (200s) ──────────────────────────────────
    BROKER_NOT_CONNECTED = "BROKER_DISCONNECTED"
    API_KEY_MISSING = "API_KEY_MISSING"
    API_KEY_INVALID = "API_KEY_INVALID"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"         # Client lacks required method
    REST_EXCEPTION = "REST_EXCEPTION"           # Network / timeout / HTTP error

    # ── Broker Rejection (300s) ────────────────────────────────────
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    MAX_POSITION_EXCEEDED = "MAX_POSITION"
    RATE_LIMITED = "RATE_LIMITED"
    MARKET_CLOSED = "MARKET_CLOSED"
    INVALID_PRICE = "INVALID_PRICE"             # Price out of range
    ORDER_REJECTED = "ORDER_REJECTED"           # Generic Kraken rejection

    # ── Post-Execution (400s) ──────────────────────────────────────
    SL_ATTACH_FAILED = "SL_ATTACH_FAILED"
    TP_ATTACH_FAILED = "TP_ATTACH_FAILED"
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"         # SL failed → forced close
    POSITION_VERIFY_FAILED = "POS_VERIFY_FAIL"

    # ── Catch-all ──────────────────────────────────────────────────
    UNKNOWN = "UNKNOWN"


# Severity mapping
ERROR_SEVERITY: Dict[ErrorCode, ErrorSeverity] = {
    ErrorCode.ZERO_QUANTITY: ErrorSeverity.HIGH,
    ErrorCode.INVALID_SYMBOL: ErrorSeverity.HIGH,
    ErrorCode.INVALID_SIDE: ErrorSeverity.HIGH,
    ErrorCode.INVALID_ORDER_TYPE: ErrorSeverity.MEDIUM,
    ErrorCode.MISSING_LIMIT_PRICE: ErrorSeverity.HIGH,
    ErrorCode.FRACTIONAL_CONTRACTS: ErrorSeverity.MEDIUM,
    ErrorCode.REDUCE_ONLY_ON_OPEN: ErrorSeverity.HIGH,
    ErrorCode.BROKER_NOT_CONNECTED: ErrorSeverity.FATAL,
    ErrorCode.API_KEY_MISSING: ErrorSeverity.FATAL,
    ErrorCode.API_KEY_INVALID: ErrorSeverity.FATAL,
    ErrorCode.NOT_IMPLEMENTED: ErrorSeverity.FATAL,
    ErrorCode.REST_EXCEPTION: ErrorSeverity.HIGH,
    ErrorCode.INSUFFICIENT_MARGIN: ErrorSeverity.HIGH,
    ErrorCode.MAX_POSITION_EXCEEDED: ErrorSeverity.MEDIUM,
    ErrorCode.RATE_LIMITED: ErrorSeverity.MEDIUM,
    ErrorCode.MARKET_CLOSED: ErrorSeverity.LOW,
    ErrorCode.INVALID_PRICE: ErrorSeverity.MEDIUM,
    ErrorCode.ORDER_REJECTED: ErrorSeverity.HIGH,
    ErrorCode.SL_ATTACH_FAILED: ErrorSeverity.FATAL,
    ErrorCode.TP_ATTACH_FAILED: ErrorSeverity.HIGH,
    ErrorCode.EMERGENCY_CLOSE: ErrorSeverity.FATAL,
    ErrorCode.POSITION_VERIFY_FAILED: ErrorSeverity.HIGH,
    ErrorCode.UNKNOWN: ErrorSeverity.HIGH,
}


def classify_error(result: Dict[str, Any]) -> ErrorCode:
    """Classify a broker response into an ErrorCode."""
    if result.get("success") is True or result.get("code") == "00000":
        return None  # Not an error

    code = str(result.get("code", "")).upper()
    msg = str(result.get("msg", "") or result.get("error", "")).lower()

    # Direct code match
    code_map = {
        "NOT_IMPLEMENTED": ErrorCode.NOT_IMPLEMENTED,
        "REST_EXCEPTION": ErrorCode.REST_EXCEPTION,
        "EXCEPTION": ErrorCode.REST_EXCEPTION,
        "NO_POSITION": ErrorCode.ZERO_QUANTITY,
        "UNKNOWN_RESPONSE": ErrorCode.UNKNOWN,
    }
    if code in code_map:
        return code_map[code]

    # Message-based classification
    if "insufficient" in msg or "margin" in msg:
        return ErrorCode.INSUFFICIENT_MARGIN
    if "rate" in msg and "limit" in msg:
        return ErrorCode.RATE_LIMITED
    if "invalid" in msg and "symbol" in msg:
        return ErrorCode.INVALID_SYMBOL
    if "invalid" in msg and "price" in msg:
        return ErrorCode.INVALID_PRICE
    if "api key" in msg or "authentication" in msg or "nonce" in msg:
        return ErrorCode.API_KEY_INVALID
    if "market" in msg and "closed" in msg:
        return ErrorCode.MARKET_CLOSED

    return ErrorCode.ORDER_REJECTED


def validate_pre_broker(
    symbol: str,
    side: str,
    quantity: float,
    order_type: str = "market",
    price: Optional[float] = None,
    reduce_only: bool = False,
    intent: Optional[str] = None,
    valid_symbols: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    """
    Pre-broker validation gate.

    Returns None if order is valid, or a failure dict with ErrorCode if blocked.
    """
    side_lower = side.lower()
    otype = order_type.lower()

    if quantity <= 0:
        return _reject(ErrorCode.ZERO_QUANTITY, f"quantity={quantity}")

    # Check precision: integer if prec=0, or float up to qty_precision
    try:
        from backend.kraken_contracts import get_contract_specs
        specs = get_contract_specs(symbol)
        prec = specs.get("qty_precision", 0)
    except Exception:
        prec = 4  # Default fallback safe precision

    if prec == 0:
        if quantity != int(quantity):
            return _reject(ErrorCode.FRACTIONAL_CONTRACTS, f"quantity={quantity} not integer for {symbol}")
    else:
        if abs(quantity - round(quantity, prec)) > 1e-9:
            return _reject(ErrorCode.FRACTIONAL_CONTRACTS, f"quantity={quantity} exceeds precision {prec} for {symbol}")

    if side_lower not in ("buy", "sell"):
        return _reject(ErrorCode.INVALID_SIDE, f"side={side}")

    if otype not in ("market", "limit"):
        return _reject(ErrorCode.INVALID_ORDER_TYPE, f"order_type={order_type}")

    if otype == "limit" and price is None:
        return _reject(ErrorCode.MISSING_LIMIT_PRICE, "limit order without price")

    if valid_symbols and symbol not in valid_symbols:
        return _reject(ErrorCode.INVALID_SYMBOL, f"symbol={symbol}")

    if intent == "open" and reduce_only:
        return _reject(ErrorCode.REDUCE_ONLY_ON_OPEN, "reduce_only=True on open intent")

    return None


def _reject(code: ErrorCode, detail: str) -> Dict[str, Any]:
    severity = ERROR_SEVERITY.get(code, ErrorSeverity.HIGH)
    return {
        "code": code.value,
        "error_code": code,
        "severity": severity.value,
        "msg": f"PRE_BROKER_REJECT: {code.value} — {detail}",
        "success": False,
        "data": None,
    }
