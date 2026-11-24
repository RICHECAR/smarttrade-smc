"""
OrderIntent — Unified order intent abstraction for multi-exchange trading.
══════════════════════════════════════════════════════════════════════════

Provides:
  - OrderIntent enum (open, close, reduce, stop_loss, take_profit)
  - Strict validation: open orders NEVER get reduce_only=True
  - Intent-to-params mapper per exchange (Bitget, Kraken)
  - Pre-send validation with explicit error logging

Usage:
    intent = OrderIntent.OPEN
    params = validate_and_map_order(
        intent=intent,
        symbol="BTCUSDT",
        side="buy",
        quantity=0.001,
        exchange="kraken",
    )
    # params["reduce_only"] is guaranteed False for OPEN intent

Author: SMC Trading Bot Framework
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger("smarttrade.order_intent")


class OrderIntent(str, Enum):
    """
    Business-level order intent.

    OPEN        — New position entry. reduce_only MUST be False.
    CLOSE       — Full position exit. reduce_only SHOULD be True.
    REDUCE      — Partial position reduction. reduce_only SHOULD be True.
    STOP_LOSS   — Protective stop-loss order. reduce_only True on Kraken.
    TAKE_PROFIT — Take-profit order. reduce_only True on Kraken.
    """
    OPEN = "open"
    CLOSE = "close"
    REDUCE = "reduce"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


# Which intents allow reduce_only=True
_REDUCE_ONLY_ALLOWED = {
    OrderIntent.CLOSE,
    OrderIntent.REDUCE,
    OrderIntent.STOP_LOSS,
    OrderIntent.TAKE_PROFIT,
}

# Which intents REQUIRE reduce_only=True on Kraken Futures
_KRAKEN_REDUCE_ONLY_REQUIRED = {
    OrderIntent.CLOSE,
    OrderIntent.REDUCE,
    OrderIntent.STOP_LOSS,
    OrderIntent.TAKE_PROFIT,
}


class OrderIntentError(Exception):
    """Raised when an order intent is invalid or inconsistent."""
    pass


class InvalidReduceOnlyUsageError(OrderIntentError):
    """Raised when reduce_only=True is used on an OPEN intent."""
    pass


class InvalidKrakenQuantityError(OrderIntentError):
    """Raised when quantity is invalid for Kraken (<=0, non-integer contracts, etc.)."""
    pass


class InvalidProtectionOrderError(OrderIntentError):
    """Raised when SL/TP price is incoherent with position side."""
    pass


@dataclass
class ValidatedOrderParams:
    """Order parameters after intent validation."""
    intent: OrderIntent
    symbol: str
    side: str
    quantity: float
    order_type: str = "market"
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    reduce_only: bool = False
    exchange: str = "bitget"
    leverage: int = 1
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> Dict[str, Any]:
        """Structured log-friendly dict."""
        return {
            "intent": self.intent.value,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.quantity,
            "order_type": self.order_type,
            "price": self.price,
            "trigger_price": self.trigger_price,
            "reduce_only": self.reduce_only,
            "exchange": self.exchange,
            "leverage": self.leverage,
        }


def resolve_reduce_only(
    intent: OrderIntent,
    exchange: str = "bitget",
    explicit_reduce_only: Optional[bool] = None,
) -> bool:
    """
    Resolve the reduce_only flag based on intent and exchange.

    Rules:
      - OPEN intent → ALWAYS False, regardless of explicit_reduce_only.
      - CLOSE/REDUCE/SL/TP on Kraken → True (required by exchange).
      - CLOSE/REDUCE/SL/TP on Bitget → True (enforced via tradeSide="close").
      - If explicit_reduce_only is provided and conflicts → log warning + override.

    Returns:
        Correct reduce_only value.
    """
    exchange = exchange.lower()

    if intent == OrderIntent.OPEN:
        if explicit_reduce_only is True:
            logger.error(
                "[ORDER_INTENT] BLOCKED: reduce_only=True on OPEN intent! "
                "Forcing reduce_only=False to prevent invalid order."
            )
        return False

    if intent in _REDUCE_ONLY_ALLOWED:
        if exchange == "kraken" and intent in _KRAKEN_REDUCE_ONLY_REQUIRED:
            return True
        # For Bitget, reduce_only is handled via tradeSide="close"
        # but we still set it for safety
        return True

    # Default: no reduce_only
    return False


def validate_order_intent(
    intent: OrderIntent,
    symbol: str,
    side: str,
    quantity: float,
    order_type: str = "market",
    price: Optional[float] = None,
    trigger_price: Optional[float] = None,
    reduce_only: Optional[bool] = None,
    exchange: str = "bitget",
    leverage: int = 1,
    **extra,
) -> ValidatedOrderParams:
    """
    Validate and build order parameters with intent-based reduce_only resolution.

    Raises:
        OrderIntentError: If order is structurally invalid (e.g., zero qty, missing price).
    """
    # ── Basic validations ──
    if quantity <= 0:
        raise OrderIntentError(
            f"Invalid quantity {quantity} for {intent.value} on {symbol}: must be > 0"
        )

    if order_type == "limit" and price is None:
        raise OrderIntentError(
            f"Limit order on {symbol} requires a price"
        )

    if intent in (OrderIntent.STOP_LOSS, OrderIntent.TAKE_PROFIT) and trigger_price is None:
        raise OrderIntentError(
            f"{intent.value} order on {symbol} requires a trigger_price"
        )

    # ── Resolve reduce_only ──
    resolved_reduce_only = resolve_reduce_only(intent, exchange, reduce_only)

    params = ValidatedOrderParams(
        intent=intent,
        symbol=symbol,
        side=side.lower(),
        quantity=quantity,
        order_type=order_type.lower(),
        price=price,
        trigger_price=trigger_price,
        reduce_only=resolved_reduce_only,
        exchange=exchange.lower(),
        leverage=leverage,
        extra=extra,
    )

    logger.info(
        "[ORDER_INTENT] VALIDATED | %s %s %s | qty=%.6f | reduce_only=%s | exchange=%s",
        intent.value.upper(),
        side.upper(),
        symbol,
        quantity,
        resolved_reduce_only,
        exchange,
    )

    return params


def get_close_side(position_side: str) -> str:
    """Get the order side needed to close a position."""
    position_side = position_side.lower()
    if position_side in ("long", "buy"):
        return "sell"
    elif position_side in ("short", "sell"):
        return "buy"
    raise OrderIntentError(f"Unknown position side: {position_side}")


def map_to_kraken_params(params: ValidatedOrderParams) -> Dict[str, Any]:
    """
    Map validated order params to Kraken Futures REST API format.

    Kraken Futures REST /sendorder expects:
      - symbol: PF_XBTUSD format
      - side: "buy" or "sell"
      - orderType: "mkt", "lmt", "stp", "take_profit"
      - size: number of contracts
      - limitPrice: for limit/TP orders
      - stopPrice: for stop orders
      - reduceOnly: "true" or absent
    """
    order_type_map = {
        "market": "mkt",
        "limit": "lmt",
    }

    if params.intent == OrderIntent.STOP_LOSS:
        kraken_type = "stp"
    elif params.intent == OrderIntent.TAKE_PROFIT:
        kraken_type = "take_profit"
    else:
        kraken_type = order_type_map.get(params.order_type, "mkt")

    data: Dict[str, Any] = {
        "symbol": params.symbol,
        "side": params.side,
        "orderType": kraken_type,
        "size": params.quantity,
    }

    if params.price is not None and kraken_type in ("lmt", "take_profit"):
        data["limitPrice"] = params.price

    if params.trigger_price is not None and kraken_type in ("stp", "take_profit"):
        data["stopPrice"] = params.trigger_price

    if params.reduce_only:
        data["reduceOnly"] = "true"

    return data


def map_to_bitget_params(params: ValidatedOrderParams) -> Dict[str, Any]:
    """
    Map validated order params to Bitget Futures API format.
    """
    trade_side = "close" if params.intent != OrderIntent.OPEN else "open"

    data: Dict[str, Any] = {
        "symbol": params.symbol,
        "side": params.side,
        "orderType": params.order_type,
        "size": str(params.quantity),
        "tradeSide": trade_side,
    }

    if params.price is not None:
        data["price"] = str(params.price)

    return data


# ═══════════════════════════════════════════════════════════════════
#  KRAKEN ORDER PAYLOAD CENTRAL VALIDATION
# ═══════════════════════════════════════════════════════════════════

def validate_kraken_order_payload(
    intent: OrderIntent,
    symbol: str,
    side: str,
    quantity: float,
    order_type: str = "market",
    price: Optional[float] = None,
    trigger_price: Optional[float] = None,
    reduce_only: Optional[bool] = None,
    entry_price: Optional[float] = None,
    position_side: Optional[str] = None,
) -> ValidatedOrderParams:
    """
    Central pre-send validation for ALL Kraken Futures orders.

    Validates:
      1. reduce_only is NEVER True on OPEN intent
      2. quantity > 0 and is integer (Kraken uses integer contracts)
      3. SL/TP trigger_price is on the correct side of entry_price
      4. symbol is non-empty

    Raises:
        InvalidReduceOnlyUsageError: reduce_only=True on OPEN
        InvalidKrakenQuantityError: qty <= 0 or non-integer contracts
        InvalidProtectionOrderError: SL/TP price incoherent with position side
        OrderIntentError: other structural issues

    Returns:
        ValidatedOrderParams ready for map_to_kraken_params()
    """
    # ── 1. Block reduce_only on OPEN ──
    if intent == OrderIntent.OPEN and reduce_only is True:
        raise InvalidReduceOnlyUsageError(
            f"reduce_only=True is FORBIDDEN on OPEN intent for {symbol}. "
            "This would prevent opening a new position on Kraken."
        )

    # ── 2. Quantity validation ──
    if quantity <= 0:
        raise InvalidKrakenQuantityError(
            f"Kraken order quantity must be > 0, got {quantity} for {symbol}"
        )
    if quantity != int(quantity):
        raise InvalidKrakenQuantityError(
            f"Kraken Futures requires integer contract qty, got {quantity} for {symbol}. "
            "Convert coins to contracts before calling this function."
        )

    # ── 3. Symbol must be non-empty ──
    if not symbol or not symbol.strip():
        raise OrderIntentError("Symbol is empty or blank")

    # ── 4. SL/TP price coherence ──
    if intent == OrderIntent.STOP_LOSS and trigger_price is not None and entry_price is not None:
        _validate_sl_price(trigger_price, entry_price, position_side, symbol)

    if intent == OrderIntent.TAKE_PROFIT and trigger_price is not None and entry_price is not None:
        _validate_tp_price(trigger_price, entry_price, position_side, symbol)

    # ── 5. Delegate to standard validation ──
    return validate_order_intent(
        intent=intent,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        price=price,
        trigger_price=trigger_price,
        reduce_only=reduce_only,
        exchange="kraken",
    )


def _validate_sl_price(
    sl_price: float,
    entry_price: float,
    position_side: Optional[str],
    symbol: str,
) -> None:
    """Validate SL price is on correct side of entry."""
    if position_side is None:
        return  # Cannot validate without knowing position side

    pos = position_side.lower()
    if pos in ("long", "buy"):
        # SL for long must be BELOW entry
        if sl_price >= entry_price:
            raise InvalidProtectionOrderError(
                f"SL for LONG {symbol}: trigger_price={sl_price} must be < entry_price={entry_price}"
            )
    elif pos in ("short", "sell"):
        # SL for short must be ABOVE entry
        if sl_price <= entry_price:
            raise InvalidProtectionOrderError(
                f"SL for SHORT {symbol}: trigger_price={sl_price} must be > entry_price={entry_price}"
            )


def _validate_tp_price(
    tp_price: float,
    entry_price: float,
    position_side: Optional[str],
    symbol: str,
) -> None:
    """Validate TP price is on correct side of entry."""
    if position_side is None:
        return

    pos = position_side.lower()
    if pos in ("long", "buy"):
        # TP for long must be ABOVE entry
        if tp_price <= entry_price:
            raise InvalidProtectionOrderError(
                f"TP for LONG {symbol}: trigger_price={tp_price} must be > entry_price={entry_price}"
            )
    elif pos in ("short", "sell"):
        # TP for short must be BELOW entry
        if tp_price >= entry_price:
            raise InvalidProtectionOrderError(
                f"TP for SHORT {symbol}: trigger_price={tp_price} must be < entry_price={entry_price}"
            )
