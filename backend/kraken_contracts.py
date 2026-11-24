"""
Kraken Contracts — Central contract model for Kraken Futures.
═════════════════════════════════════════════════════════════

Single source of truth for:
  - Contract specs per symbol (value, min_size, step, precision)
  - Coin → contract conversion
  - Pre-send validation (fail-closed)
  - SL/TP price coherence checks

All Kraken Futures symbols use USD-denominated contracts:
  PF_XBTUSD: 1 contract = 1 USD of BTC exposure
  PF_ETHUSD: 1 contract = 1 USD of ETH exposure
  etc.

Author: SMC Trading Bot Framework
"""

import logging
import math
from typing import Dict, Optional, Tuple

logger = logging.getLogger("smarttrade.kraken_contracts")


# ═══════════════════════════════════════════════════════════════
#  CONTRACT SPECIFICATIONS (Kraken Futures Linear Multi-Collateral PF_*)
# ═══════════════════════════════════════════════════════════════

KRAKEN_CONTRACT_SPECS: Dict[str, Dict] = {
    "PF_XBTUSD":  {"contract_type": "linear", "contract_size": 1.0, "min_size": 0.0001, "step_size": 0.0001, "qty_precision": 4, "price_tick": 1.0},
    "PF_ETHUSD":  {"contract_type": "linear", "contract_size": 1.0, "min_size": 0.001,  "step_size": 0.001,  "qty_precision": 3, "price_tick": 0.1},
    "PF_SOLUSD":  {"contract_type": "linear", "contract_size": 1.0, "min_size": 0.01,   "step_size": 0.01,   "qty_precision": 2, "price_tick": 0.01},
    "PF_XRPUSD":  {"contract_type": "linear", "contract_size": 1.0, "min_size": 1.0,    "step_size": 1.0,    "qty_precision": 0, "price_tick": 0.0001},
    "PF_DOGEUSD": {"contract_type": "linear", "contract_size": 1.0, "min_size": 1.0,    "step_size": 1.0,    "qty_precision": 0, "price_tick": 0.00001},
    "PF_BNBUSD":  {"contract_type": "linear", "contract_size": 1.0, "min_size": 0.01,   "step_size": 0.01,   "qty_precision": 2, "price_tick": 0.01},
    "PF_ADAUSD":  {"contract_type": "linear", "contract_size": 1.0, "min_size": 1.0,    "step_size": 1.0,    "qty_precision": 0, "price_tick": 0.00001},
    "PF_AVAXUSD": {"contract_type": "linear", "contract_size": 1.0, "min_size": 0.01,   "step_size": 0.01,   "qty_precision": 2, "price_tick": 0.001},
    "PF_LINKUSD": {"contract_type": "linear", "contract_size": 1.0, "min_size": 0.1,    "step_size": 0.1,    "qty_precision": 1, "price_tick": 0.001},
    "PF_LTCUSD":  {"contract_type": "linear", "contract_size": 1.0, "min_size": 0.01,   "step_size": 0.01,   "qty_precision": 2, "price_tick": 0.01},
    "PF_DOTUSD":  {"contract_type": "linear", "contract_size": 1.0, "min_size": 0.1,    "step_size": 0.1,    "qty_precision": 1, "price_tick": 0.0001},
    "PF_NEARUSD": {"contract_type": "linear", "contract_size": 1.0, "min_size": 0.1,    "step_size": 0.1,    "qty_precision": 1, "price_tick": 0.001},
    "PF_ARBUSD":  {"contract_type": "linear", "contract_size": 1.0, "min_size": 1.0,    "step_size": 1.0,    "qty_precision": 0, "price_tick": 0.0001},
    "PF_OPUSD":   {"contract_type": "linear", "contract_size": 1.0, "min_size": 0.1,    "step_size": 0.1,    "qty_precision": 1, "price_tick": 0.001},
    "PF_MATICUSD":{"contract_type": "linear", "contract_size": 1.0, "min_size": 1.0,    "step_size": 1.0,    "qty_precision": 0, "price_tick": 0.0001},
}

# Default specs for unknown symbols (conservative linear default)
_DEFAULT_SPECS = {"contract_type": "linear", "contract_size": 1.0, "min_size": 1.0, "step_size": 1.0, "qty_precision": 0, "price_tick": 0.01}

# Symbol mapping: Bitget → Kraken
BITGET_TO_KRAKEN = {
    "BTCUSDT": "PF_XBTUSD",  "ETHUSDT": "PF_ETHUSD",  "SOLUSDT": "PF_SOLUSD",
    "XRPUSDT": "PF_XRPUSD",  "DOGEUSDT": "PF_DOGEUSD", "BNBUSDT": "PF_BNBUSD",
    "ADAUSDT": "PF_ADAUSD",  "AVAXUSDT": "PF_AVAXUSD", "LINKUSDT": "PF_LINKUSD",
    "LTCUSDT": "PF_LTCUSD",  "DOTUSDT": "PF_DOTUSD",  "NEARUSDT": "PF_NEARUSD",
    "ARBUSDT": "PF_ARBUSD",  "OPUSDT": "PF_OPUSD",    "MATICUSDT": "PF_MATICUSD",
}

KRAKEN_TO_BITGET = {v: k for k, v in BITGET_TO_KRAKEN.items()}


def get_contract_specs(symbol: str) -> Dict:
    """Get contract specs for a Kraken Futures symbol. Uses defaults for unknown symbols."""
    sym = symbol.upper()
    if sym in KRAKEN_CONTRACT_SPECS:
        return KRAKEN_CONTRACT_SPECS[sym]
    # Try mapping from Bitget format
    kraken_sym = BITGET_TO_KRAKEN.get(sym)
    if kraken_sym and kraken_sym in KRAKEN_CONTRACT_SPECS:
        return KRAKEN_CONTRACT_SPECS[kraken_sym]
    logger.warning("[KRAKEN_CONTRACTS] Unknown symbol %s — using default specs", sym)
    return _DEFAULT_SPECS.copy()


def to_kraken_symbol(symbol: str) -> str:
    """Convert any symbol format to Kraken Futures format (PF_XBTUSD)."""
    sym = symbol.upper().replace("/", "").replace(":", "").replace("-", "")
    if sym.startswith(("PF_", "PI_", "FF_", "FI_")):
        return sym
    return BITGET_TO_KRAKEN.get(sym, sym)


def from_kraken_symbol(symbol: str) -> str:
    """Convert Kraken symbol back to Bitget/canonical format."""
    return KRAKEN_TO_BITGET.get(symbol.upper(), symbol)


# ═══════════════════════════════════════════════════════════════
#  QUANTITY NORMALIZATION (LINEAR CONTRACTS — UNDERLYING CRYPTO)
# ═══════════════════════════════════════════════════════════════

def ensure_contracts(
    quantity: float,
    symbol: str,
    price: float = 0.0,
) -> float:
    """
    Ensure quantity complies with Kraken Futures specs (linear contracts).

    On Kraken Linear Multi-Collateral (PF_*), order size is denominated
    directly in the underlying cryptocurrency (e.g. 0.005 BTC, 0.15 ETH).
    It is NEVER multiplied by price to make fake 1-USD contracts.

    Args:
        quantity: Raw crypto coin quantity (e.g. 0.005 BTC)
        symbol: Kraken symbol for specs lookup
        price: Optional reference price (kept for signature compatibility)

    Returns:
        Normalized float/int quantity, floored to instrument precision.
    """
    specs = get_contract_specs(symbol)
    prec = int(specs.get("qty_precision", 0))
    min_size = float(specs.get("min_size", 1.0))

    if quantity <= 0:
        return 0.0 if prec > 0 else 0

    factor = 10 ** prec if prec > 0 else 1
    floored = math.floor(float(quantity) * factor + 1e-9) / factor if prec > 0 else float(math.floor(float(quantity)))
    return int(floored) if prec == 0 else round(floored, prec)


def coin_to_contracts(
    coin_qty: float,
    price: float = 0.0,
    symbol: str = "PF_XBTUSD",
) -> float:
    """
    On Kraken Linear Multi-Collateral (PF_*), order quantity is denominated
    directly in the underlying cryptocurrency.
    Formula: returns ensure_contracts(coin_qty, symbol).
    """
    return ensure_contracts(coin_qty, symbol, price)


# ═══════════════════════════════════════════════════════════════
#  PRE-SEND VALIDATION (FAIL-CLOSED)
# ═══════════════════════════════════════════════════════════════

def validate_order(
    symbol: str,
    side: str,
    contracts: float,
    price: float = 0.0,
    order_type: str = "market",
    reduce_only: bool = False,
    intent: str = "open",
    max_notional_usd: float = 100_000.0,
) -> Tuple[bool, Optional[str]]:
    """
    Validate a Kraken Futures order before sending. FAIL-CLOSED.

    Blocks:
      - contracts < min_size
      - contracts <= 0
      - price <= 0 for limit orders
      - unknown symbol (warning only, not blocked)
      - reduce_only on open → blocked
      - notional exceeds safety cap

    Returns:
        (valid, error_message) — True if order is safe to send
    """
    # Type check (accepts float and int)
    if not isinstance(contracts, (int, float)):
        return False, f"INVALID_SIZE: contracts must be number, got {type(contracts).__name__}={contracts}"

    specs = get_contract_specs(symbol)
    min_size = float(specs["min_size"])

    # Min size
    if float(contracts) < min_size:
        return False, f"INVALID_SIZE: {contracts} < min_size={min_size}"

    # Side
    if side.lower() not in ("buy", "sell"):
        return False, f"INVALID_SIDE: '{side}' — must be 'buy' or 'sell'"

    # Price for limit
    if order_type.lower() == "limit" and (price is None or price <= 0):
        return False, f"INVALID_PRICE: limit order requires price > 0, got {price}"

    # Reduce-only coherence
    if intent.lower() == "open" and reduce_only:
        return False, "REDUCE_ONLY_ON_OPEN: reduce_only=True forbidden on open intent"

    # Notional safety cap (Linear: notional = contracts * price)
    if max_notional_usd > 0 and price > 0:
        notional = float(contracts) * float(price)
        if notional > max_notional_usd:
            return False, f"NOTIONAL_TOO_HIGH: {notional:.0f} USD > {max_notional_usd:.0f} USD cap"

    return True, None


# ═══════════════════════════════════════════════════════════════
#  SL / TP VALIDATION
# ═══════════════════════════════════════════════════════════════

def validate_sl_tp(
    entry_price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    side: str,
) -> Tuple[bool, Optional[str]]:
    """
    Validate SL/TP prices are coherent with trade direction.

    Rules:
      LONG (buy): SL < entry, TP > entry
      SHORT (sell): SL > entry, TP < entry

    Returns:
        (valid, error_message)
    """
    side_lower = side.lower()
    is_long = side_lower in ("buy", "long")

    if stop_loss is not None and stop_loss > 0:
        if is_long and stop_loss >= entry_price:
            return False, (
                f"SL_INVALID_LONG: SL={stop_loss} must be < entry={entry_price}"
            )
        if not is_long and stop_loss <= entry_price:
            return False, (
                f"SL_INVALID_SHORT: SL={stop_loss} must be > entry={entry_price}"
            )

    if take_profit is not None and take_profit > 0:
        if is_long and take_profit <= entry_price:
            return False, (
                f"TP_INVALID_LONG: TP={take_profit} must be > entry={entry_price}"
            )
        if not is_long and take_profit >= entry_price:
            return False, (
                f"TP_INVALID_SHORT: TP={take_profit} must be < entry={entry_price}"
            )

    return True, None


def align_price_to_tick(
    price: float,
    symbol: str,
    side: str,
    protection_type: str = "sl",
    entry_price: Optional[float] = None,
) -> Tuple[bool, float, Optional[str]]:
    """
    Align a protection price (SL or TP) to the exact price_tick of the Kraken contract specs.
    Enforces risk-safe rounding:
      - For SL on LONG (buy): round DOWN (floor) so SL moves further away from entry,
        never closer.
      - For SL on SHORT (sell): round UP (ceil) so SL moves further away from entry,
        never closer.
      - For TP on LONG (buy): round DOWN (floor) so TP is conservative and closer/easier to reach.
      - For TP on SHORT (sell): round UP (ceil) so TP is conservative and closer/easier to reach.

    Revalidates coherent direction against entry_price if provided.
    Refuses if price_tick is missing, invalid, or price <= 0, or if the contract is unknown
    (strict: never invents a tick size).
    """
    if price is None or price <= 0 or not math.isfinite(price):
        return False, 0.0, f"INVALID_PRICE: price={price} must be finite positive"

    sym = symbol.upper()
    kraken_sym = sym if sym in KRAKEN_CONTRACT_SPECS else BITGET_TO_KRAKEN.get(sym)
    if not kraken_sym or kraken_sym not in KRAKEN_CONTRACT_SPECS:
        return False, 0.0, f"UNKNOWN_CONTRACT_TICK: symbol {symbol} has no official contract specs (refusing invented tick)"

    specs = KRAKEN_CONTRACT_SPECS[kraken_sym]
    tick = specs.get("price_tick")
    if tick is None or tick <= 0 or not math.isfinite(tick):
        return False, 0.0, f"INVALID_TICK_SPEC: no valid price_tick for symbol {symbol}"

    side_lower = side.lower()
    is_long = side_lower in ("buy", "long")
    inv = 1.0 / tick

    prot = protection_type.lower()
    if prot == "sl":
        if is_long:
            aligned = math.floor(price * inv + 1e-9) / inv
        else:
            aligned = math.ceil(price * inv - 1e-9) / inv
    elif prot == "tp":
        if is_long:
            aligned = math.floor(price * inv + 1e-9) / inv
        else:
            aligned = math.ceil(price * inv - 1e-9) / inv
    else:
        return False, 0.0, f"UNKNOWN_PROTECTION_TYPE: {protection_type}"

    prec = max(0, int(round(-math.log10(tick)))) if tick < 1.0 else 0
    aligned = round(aligned, prec)
    if prec == 0:
        aligned = float(int(aligned))

    if aligned <= 0:
        return False, 0.0, f"ROUNDED_PRICE_NON_POSITIVE: aligned={aligned} from price={price}"

    # Revalidation against entry_price if provided
    if entry_price is not None and entry_price > 0:
        valid, err = validate_sl_tp(
            entry_price=entry_price,
            stop_loss=aligned if prot == "sl" else None,
            take_profit=aligned if prot == "tp" else None,
            side=side,
        )
        if not valid:
            return False, 0.0, f"POST_ALIGNMENT_VALIDATION_FAILED: {err} (aligned={aligned}, original={price})"

    return True, aligned, None



# ═══════════════════════════════════════════════════════════════
#  KRAKEN ERROR STATUS MAPPING
# ═══════════════════════════════════════════════════════════════

KRAKEN_STATUS_MAP = {
    "invalidSize": "INVALID_SIZE",
    "insufficientFunds": "INSUFFICIENT_FUNDS",
    "insufficientMargin": "INSUFFICIENT_MARGIN",
    "invalidPrice": "INVALID_PRICE",
    "invalidSymbol": "INVALID_SYMBOL",
    "marketSuspended": "MARKET_CLOSED",
    "rateLimitExceeded": "RATE_LIMITED",
    "apiLimitExceeded": "RATE_LIMITED",
    "maxPositionExceeded": "MAX_POSITION",
    "requiredParameterMissing": "MISSING_PARAMETER",
    "unknownOrderType": "INVALID_ORDER_TYPE",
    "accountInactive": "API_KEY_INVALID",
    "apiKeyNotFound": "API_KEY_MISSING",
}


def classify_kraken_error(status: str, error_message: str = "") -> str:
    """Map Kraken sendStatus status to normalized error code."""
    if status in ("placed", "filled", "partiallyFilled"):
        return "SUCCESS"
    mapped = KRAKEN_STATUS_MAP.get(status)
    if mapped:
        return mapped
    # Fallback: message-based
    msg = error_message.lower()
    if "size" in msg or "quantity" in msg:
        return "INVALID_SIZE"
    if "price" in msg:
        return "INVALID_PRICE"
    if "margin" in msg or "fund" in msg:
        return "INSUFFICIENT_MARGIN"
    return "ORDER_REJECTED"


# ═══════════════════════════════════════════════════════════════
#  RESPONSE NORMALIZATION
# ═══════════════════════════════════════════════════════════════

def normalize_order_response(raw: dict) -> dict:
    """
    Normalize any Kraken order response to standard format.

    Returns:
        {
            "success": bool,
            "order_id": str | None,
            "filled_qty": int,
            "avg_price": float,
            "error_code": str | None,
            "raw_message": str,
        }
    """
    if raw.get("success") is True or raw.get("code") == "00000":
        return {
            "success": True,
            "order_id": (
                raw.get("order_id")
                or raw.get("data", {}).get("orderId")
                or raw.get("sendStatus", {}).get("order_id")
            ),
            "filled_qty": int(raw.get("filled_qty", 0)
                              or raw.get("sendStatus", {}).get("filledSize", 0)),
            "avg_price": float(raw.get("avg_price", 0)
                               or raw.get("sendStatus", {}).get("avgFillPrice", 0)),
            "error_code": None,
            "raw_message": "success",
        }

    # Error path
    send_status = raw.get("sendStatus", {})
    status = send_status.get("status", raw.get("status", ""))
    error_msg = (
        send_status.get("errorMessage")
        or send_status.get("reason")
        or raw.get("error")
        or raw.get("msg")
        or str(status)
    )

    return {
        "success": False,
        "order_id": send_status.get("order_id") or raw.get("order_id"),
        "filled_qty": 0,
        "avg_price": 0.0,
        "error_code": classify_kraken_error(status, error_msg),
        "raw_message": error_msg,
    }
