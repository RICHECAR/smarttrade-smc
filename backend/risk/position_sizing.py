"""
Auto Position Sizing Module - EUR/USDT Balance to Position Quantity

Calculates position size based on:
- Stop loss distance (real risk)
- Account balance in EUR or USDT
- Leverage (clamped to max 10x)
- Bitget contract specs (qty_step, min_qty, min_notional)
- PRE-FLIGHT FEASIBILITY CHECK (rejects infeasible trades upfront)

IMPORTANT: Leverage does NOT change your risk amount. It only determines
how much margin is required. A 1% risk trade loses 1% whether at 1x or 10x.

Configuration via environment variables:
  - MAX_RISK_DEVIATION_PCT: Maximum allowed deviation when clamping (default: 200)
  - MIN_RISK_USD: Minimum risk amount in USD to avoid tiny orders (default: 0.50)

Usage:
    from backend.risk.position_sizing import compute_position_size, SizeResult
    
    result = compute_position_size(
        balance=1000.0,         # Account balance
        balance_ccy="EUR",      # EUR or USDT
        risk_pct=0.01,          # 1% risk
        entry_price=50000.0,    # BTC entry
        stop_price=49500.0,     # SL at 49500
        leverage=10,
        fx_eurusdt=1.08,        # Required for EUR accounts
    )
    
    print(f"Qty: {result.qty}, Risk: {result.risk_amount} {result.currency}")

Author: SMC Trading Bot Framework
"""

import os
from dataclasses import dataclass
from math import floor
from typing import Optional, Literal, Tuple, Dict, Any
import logging

from core.env_loader import safe_load_dotenv

safe_load_dotenv()

logger = logging.getLogger(__name__)

# Type alias for currency
Currency = Literal["EUR", "USDT"]


# ═══════════════════════════════════════════════════════════════════════════════
# RISK CONFIGURATION FROM ENVIRONMENT
# ═══════════════════════════════════════════════════════════════════════════════

def get_max_risk_deviation_pct() -> float:
    """
    Get maximum allowed risk deviation percentage from environment.
    
    When raw_qty < min_qty, this determines how much "extra risk" is acceptable
    when clamping up to min_qty.
    
    Default: 500% (allows trades where clamping increases risk up to 6x)
    Previous default was 50% which rejected many valid small-account trades.
    
    Set via: MAX_RISK_DEVIATION_PCT=500
    """
    try:
        return float(os.getenv("MAX_RISK_DEVIATION_PCT", "500"))
    except (ValueError, TypeError):
        return 500.0


def get_min_risk_usd() -> float:
    """
    Get minimum risk amount in USD from environment.
    
    If calculated risk_amount is below this value, the trade is rejected
    as it would result in an unmanageably small position.
    
    Default: 0.50 USD
    
    Set via: MIN_RISK_USD=0.50
    """
    try:
        return float(os.getenv("MIN_RISK_USD", "0.50"))
    except (ValueError, TypeError):
        return 0.50


@dataclass(frozen=True)
class SizeResult:
    """
    Immutable result of position size calculation.
    
    Attributes:
        qty: Position quantity (base asset units, e.g., BTC)
        notional: Trade notional value in quote currency (USDT)
        leverage_used: Actual leverage used (clamped to max)
        risk_amount: Risk amount in account currency (EUR or USDT)
        stop_distance: Distance between entry and stop in quote currency
        currency: Account currency (EUR or USDT)
        margin_required: Required margin = notional / leverage
        risk_in_quote: Risk amount converted to quote currency (USDT)
        feasibility_status: "OK", "CLAMPED", "REJECTED", or None
        rejection_reason: Reason for rejection if applicable
    """
    qty: float
    notional: float
    leverage_used: int
    risk_amount: float
    stop_distance: float
    currency: Currency
    margin_required: float = 0.0
    risk_in_quote: float = 0.0
    feasibility_status: Optional[str] = None
    rejection_reason: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
# PRE-FLIGHT FEASIBILITY CHECK
# ═══════════════════════════════════════════════════════════════════════════════

def check_trade_feasibility(
    symbol: str,
    balance: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    max_risk_deviation_pct: Optional[float] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Pre-flight check: Is this trade feasible before sizing?
    
    This prevents the "QTY_REJECTED" scenario by checking UPFRONT.
    
    Args:
        symbol: Trading pair (e.g., "XRPUSDT")
        balance: Account balance in USDT
        risk_pct: Risk percentage (0.01 = 1%)
        entry_price: Entry price
        stop_price: Stop loss price
        max_risk_deviation_pct: Max allowed clamp deviation (None = use env config)
    
    Returns:
        Tuple of (is_feasible, reason, details)
    
    Example:
        >>> feasible, reason, details = check_trade_feasibility(
        ...     "XRPUSDT", 100.0, 0.01, 2.50, 2.48
        ... )
        >>> if not feasible:
        ...     print(f"Skip trade: {reason}")
    """
    # Use env config if not explicitly provided
    if max_risk_deviation_pct is None:
        max_risk_deviation_pct = get_max_risk_deviation_pct()
    
    min_risk_usd = get_min_risk_usd()
    
    specs = get_contract_specs(symbol)
    
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return False, "INVALID_STOP", {"error": "stop == entry"}
    
    risk_amount = balance * risk_pct
    
    # Check MIN_RISK_USD
    if risk_amount < min_risk_usd:
        logger.warning(
            f"[PREFLIGHT] ✗ {symbol} | risk_amount=${risk_amount:.4f} < min=${min_risk_usd:.2f} | "
            f"Consider increasing balance or risk_pct"
        )
        return False, "RISK_BELOW_MIN_USD", {
            "symbol": symbol,
            "risk_amount": round(risk_amount, 4),
            "min_risk_usd": min_risk_usd,
            "suggested_risk_pct": round((min_risk_usd / balance) * 100, 2) if balance > 0 else 100,
        }
    
    raw_qty = risk_amount / stop_distance
    
    details = {
        "symbol": symbol,
        "balance": balance,
        "risk_pct": risk_pct,
        "risk_amount": round(risk_amount, 4),
        "entry_price": entry_price,
        "stop_price": stop_price,
        "stop_distance": round(stop_distance, 6),
        "raw_qty": round(raw_qty, 6),
        "min_qty": specs["min_qty"],
        "min_notional": specs["min_notional"],
    }
    
    # Check 2: Notional achievable?
    # Determine the absolute minimum quantity needed for this trade to be accepted by the exchange
    min_qty_for_notional = specs["min_notional"] / entry_price if entry_price > 0 else 0
    required_qty = max(specs["min_qty"], min_qty_for_notional)
    
    # Calculate the deviation if we were to clamp to required_qty
    if raw_qty < required_qty:
        deviation_pct = ((required_qty - raw_qty) / raw_qty) * 100 if raw_qty > 0 else float('inf')
        details["deviation_pct"] = round(deviation_pct, 1)
        
        if deviation_pct > max_risk_deviation_pct:
            min_risk_needed = required_qty * stop_distance
            min_balance_needed = min_risk_needed / risk_pct if risk_pct > 0 else float('inf')
            
            details["min_risk_needed"] = round(min_risk_needed, 4)
            details["min_balance_needed"] = round(min_balance_needed, 2)
            details["suggested_risk_pct"] = round((min_risk_needed / balance) * 100, 2) if balance > 0 else 100
            
            error_code = "QTY_BELOW_MIN_CLAMP_EXCEEDS_MAX" if required_qty == specs["min_qty"] else "NOTIONAL_BELOW_MIN_CLAMP_EXCEEDS_MAX"
            
            logger.warning(
                f"[PREFLIGHT] ✗ {symbol} | raw_qty={raw_qty:.6f} < required={required_qty:.6f} | "
                f"deviation={deviation_pct:.1f}% > max={max_risk_deviation_pct}% | "
                f"Need balance=${min_balance_needed:.0f} or risk={details['suggested_risk_pct']:.1f}%"
            )
            return False, error_code, details
            
    details["notional"] = round(max(raw_qty, required_qty) * entry_price, 2)
    
    logger.debug(f"[PREFLIGHT] ✓ {symbol} | raw_qty={raw_qty:.6f} | FEASIBLE")
    return True, "FEASIBLE", details


def get_min_requirements_for_trade(
    symbol: str,
    entry_price: float,
    stop_price: float,
    risk_pct: float = 0.01,
) -> Dict[str, float]:
    """
    Get minimum balance/risk requirements for a trade.
    
    Returns:
        Dict with min_risk_usdt, min_balance_usdt, etc.
    """
    specs = get_contract_specs(symbol)
    stop_distance = abs(entry_price - stop_price)
    
    if stop_distance <= 0:
        return {"error": "invalid stop distance"}
    
    min_risk = specs["min_qty"] * stop_distance
    min_balance = min_risk / risk_pct if risk_pct > 0 else float('inf')
    
    return {
        "symbol": symbol,
        "min_qty": specs["min_qty"],
        "min_notional": specs["min_notional"],
        "stop_distance": round(stop_distance, 6),
        "min_risk_usdt": round(min_risk, 4),
        "min_balance_at_risk_pct": round(min_balance, 2),
        "risk_pct": risk_pct,
    }


def _round_step(x: float, step: float) -> float:
    """
    Round down to nearest step (Bitget qty_step compliance).
    
    Example: _round_step(0.00567, 0.001) = 0.005
    """
    if step <= 0:
        return x
    return floor(x / step) * step


def compute_position_size(
    balance: float,
    balance_ccy: Currency,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    leverage: int,
    *,
    max_leverage: int = 10,
    qty_step: float = 0.001,
    min_qty: float = 0.0,
    min_notional: float = 0.0,
    fx_eurusdt: Optional[float] = None,  # USDT per 1 EUR, e.g., 1.08
) -> SizeResult:
    """
    Calculate position size based on stop loss distance.
    
    The core formula:
        qty = risk_in_quote / stop_distance
        
    Where:
        - risk_in_quote = balance * risk_pct * fx_rate (if EUR)
        - stop_distance = abs(entry - stop)
    
    Args:
        balance: Account balance in balance_ccy
        balance_ccy: Account currency ("EUR" or "USDT")
        risk_pct: Risk percentage per trade (e.g., 0.01 for 1%)
        entry_price: Entry price in quote currency (USDT)
        stop_price: Stop loss price in quote currency (USDT)
        leverage: Desired leverage
        max_leverage: Maximum allowed leverage (default 10)
        qty_step: Bitget qty_step from contract specs
        min_qty: Bitget minimum order quantity
        min_notional: Bitget minimum notional value (USD)
        fx_eurusdt: USD per 1 EUR exchange rate (required for EUR accounts)
    
    Returns:
        SizeResult with calculated qty, notional, etc.
    
    Raises:
        ValueError: If inputs are invalid
    
    Example:
        >>> result = compute_position_size(
        ...     balance=1000.0,
        ...     balance_ccy="EUR",
        ...     risk_pct=0.01,      # 1% = 10 EUR risk
        ...     entry_price=50000,
        ...     stop_price=49500,   # 500 USDT stop distance
        ...     leverage=10,
        ...     fx_eurusdt=1.08,
        ... )
        >>> print(result.qty)  # ~0.0216 BTC
    """
    
    # Input validation
    if balance <= 0:
        raise ValueError("balance must be > 0")
    
    if not (0 < risk_pct <= 0.05):
        raise ValueError("risk_pct must be in (0, 0.05] (max 5%)")
    
    if entry_price <= 0 or stop_price <= 0:
        raise ValueError("prices must be > 0")
    
    # Calculate stop distance
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        raise ValueError("stop_distance must be > 0 (entry != stop)")
    
    # Clamp leverage
    leverage_used = min(int(leverage), int(max_leverage))
    
    # Calculate risk amount in account currency
    risk_amount = balance * risk_pct
    
    # Convert risk to quote currency (USDT) if needed
    # Assumption: entry/stop prices are in USDT (Bitget USDT-FUTURES)
    if balance_ccy == "EUR":
        if fx_eurusdt is None or fx_eurusdt <= 0:
            raise ValueError("fx_eurusdt required when balance_ccy == 'EUR'")
        risk_in_quote = risk_amount * fx_eurusdt  # EUR -> USDT
        logger.debug(f"[SIZING] EUR->USDT: {risk_amount:.2f} EUR × {fx_eurusdt} = {risk_in_quote:.2f} USDT")
    else:
        risk_in_quote = risk_amount
    
    # Core sizing formula: qty = risk / stop_distance
    # This ensures we lose exactly risk_amount if stop is hit
    qty = risk_in_quote / stop_distance
    
    # Round down to qty_step
    # Note: if raw qty < min_qty, _round_step might make it 0 before we clamp it.
    qty = _round_step(qty, qty_step)
    
    # Check min_qty and clamp if needed (pre-flight check ensures we don't exceed max deviation)
    if qty < min_qty and min_qty > 0:
        logger.warning(f"[SIZING] qty={qty:.6f} < min_qty={min_qty}. Clamping to {min_qty}.")
        qty = min_qty

    # Recalculate actual risk based on final clamped qty
    actual_risk_in_quote = qty * stop_distance
    if balance_ccy == "EUR":
        actual_risk_amount = actual_risk_in_quote / fx_eurusdt if fx_eurusdt else actual_risk_in_quote
    else:
        actual_risk_amount = actual_risk_in_quote
    
    # Calculate notional and margin
    notional = qty * entry_price
    margin_required = notional / leverage_used if leverage_used > 0 else notional
    
    # Check min_notional
    if notional > 0 and notional < min_notional:
        # We also need to clamp notional if it's below min_notional.
        # This means increasing qty until notional >= min_notional.
        import math
        min_qty_for_notional = min_notional / entry_price
        if qty_step > 0:
            min_qty_for_notional = math.ceil(min_qty_for_notional / qty_step) * qty_step
            
        if min_qty_for_notional > qty:
            logger.warning(f"[SIZING] notional={notional:.2f} < min_notional={min_notional}. Clamping qty to {min_qty_for_notional:.6f}.")
            qty = min_qty_for_notional
            notional = qty * entry_price
            margin_required = notional / leverage_used if leverage_used > 0 else notional
            actual_risk_in_quote = qty * stop_distance
            actual_risk_amount = (actual_risk_in_quote / fx_eurusdt) if (balance_ccy == "EUR" and fx_eurusdt) else actual_risk_in_quote

    
    logger.info(
        f"[SIZING] {balance_ccy} balance={balance:.2f}, risk={risk_pct*100:.2f}% "
        f"-> risk_amt={actual_risk_amount:.2f} {balance_ccy}, stop_dist={stop_distance:.2f}, "
        f"qty={qty:.6f}, notional={notional:.2f} USDT, margin={margin_required:.2f}, lev={leverage_used}x"
    )
    
    return SizeResult(
        qty=qty,
        notional=notional,
        leverage_used=leverage_used,
        risk_amount=actual_risk_amount,
        stop_distance=stop_distance,
        currency=balance_ccy,
        margin_required=margin_required,
        risk_in_quote=actual_risk_in_quote,
    )


def compute_position_size_from_balance_usdt(
    balance_usdt: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    leverage: int = 10,
    *,
    max_leverage: int = 10,
    qty_step: float = 0.001,
    min_qty: float = 0.0,
    min_notional: float = 5.0,
) -> SizeResult:
    """
    Convenience wrapper for USDT balance accounts.
    
    Most common use case for Bitget USDT-M Futures.
    """
    return compute_position_size(
        balance=balance_usdt,
        balance_ccy="USDT",
        risk_pct=risk_pct,
        entry_price=entry_price,
        stop_price=stop_price,
        leverage=leverage,
        max_leverage=max_leverage,
        qty_step=qty_step,
        min_qty=min_qty,
        min_notional=min_notional,
        fx_eurusdt=None,
    )


def compute_position_size_from_balance_eur(
    balance_eur: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    leverage: int = 10,
    fx_eurusdt: float = 1.08,
    *,
    max_leverage: int = 10,
    qty_step: float = 0.001,
    min_qty: float = 0.0,
    min_notional: float = 5.0,
) -> SizeResult:
    """
    Convenience wrapper for EUR balance accounts.
    
    Requires fx_eurusdt conversion rate.
    """
    return compute_position_size(
        balance=balance_eur,
        balance_ccy="EUR",
        risk_pct=risk_pct,
        entry_price=entry_price,
        stop_price=stop_price,
        leverage=leverage,
        max_leverage=max_leverage,
        qty_step=qty_step,
        min_qty=min_qty,
        min_notional=min_notional,
        fx_eurusdt=fx_eurusdt,
    )


# ============================================================================
# CONTRACT SPECS PER BROKER
# ============================================================================

# Bitget contract specs (qty in coins, e.g. 0.001 BTC)
BITGET_CONTRACT_SPECS = {
    "BTCUSDT": {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0},
    "ETHUSDT": {"qty_step": 0.01, "min_qty": 0.01, "min_notional": 5.0},
    "SOLUSDT": {"qty_step": 0.1, "min_qty": 0.1, "min_notional": 5.0},
    "XRPUSDT": {"qty_step": 1.0, "min_qty": 1.0, "min_notional": 5.0},
    "DOGEUSDT": {"qty_step": 10.0, "min_qty": 10.0, "min_notional": 5.0},
}

# Keep backward compat alias
DEFAULT_CONTRACT_SPECS = BITGET_CONTRACT_SPECS

# Kraken Futures contract specs
# Kraken PF_* contracts are USD-denominated: qty is in number of contracts.
# 1 contract ≈ 1 USD notional (for most pairs).
# Min order = 1 contract, qty_step = 1 contract.
KRAKEN_CONTRACT_SPECS = {
    "PF_XBTUSD":  {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_ETHUSD":  {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_SOLUSD":  {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_XRPUSD":  {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_DOGEUSD": {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_BNBUSD":  {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_ADAUSD":  {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_AVAXUSD": {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_LINKUSD": {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_LTCUSD":  {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_DOTUSD":  {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_MATICUSD":{"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_ARBUSD":  {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_OPUSD":   {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
    "PF_NEARUSD": {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1},
}

# Map Bitget symbols to Kraken symbols for spec lookup
_BITGET_TO_KRAKEN_SYMBOL = {
    "BTCUSDT": "PF_XBTUSD", "ETHUSDT": "PF_ETHUSD", "SOLUSDT": "PF_SOLUSD",
    "XRPUSDT": "PF_XRPUSD", "DOGEUSDT": "PF_DOGEUSD", "BNBUSDT": "PF_BNBUSD",
    "ADAUSDT": "PF_ADAUSD", "AVAXUSDT": "PF_AVAXUSD", "LINKUSDT": "PF_LINKUSD",
    "LTCUSDT": "PF_LTCUSD", "DOTUSDT": "PF_DOTUSD", "MATICUSDT": "PF_MATICUSD",
    "ARBUSDT": "PF_ARBUSD", "OPUSDT": "PF_OPUSD", "NEARUSDT": "PF_NEARUSD",
}


def get_contract_specs(symbol: str, broker: str = "bitget") -> dict:
    """
    Get contract specifications for a symbol on a specific broker.
    
    Args:
        symbol: Trading pair (Bitget format, e.g. "BTCUSDT" or Kraken "PF_XBTUSD")
        broker: "bitget" or "kraken"
    
    Returns dict with qty_step, min_qty, min_notional.
    For Kraken, also includes contract_value_usd.
    """
    broker = broker.lower()
    sym = symbol.upper()
    
    if broker == "kraken":
        # Direct Kraken symbol lookup
        if sym in KRAKEN_CONTRACT_SPECS:
            return KRAKEN_CONTRACT_SPECS[sym]
        # Try converting from Bitget format
        kraken_sym = _BITGET_TO_KRAKEN_SYMBOL.get(sym)
        if kraken_sym and kraken_sym in KRAKEN_CONTRACT_SPECS:
            return KRAKEN_CONTRACT_SPECS[kraken_sym]
        # Default Kraken specs (1 USD per contract)
        return {"qty_step": 1, "min_qty": 1, "min_notional": 0, "contract_value_usd": 1}
    
    # Bitget (default)
    return BITGET_CONTRACT_SPECS.get(
        sym,
        {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0}
    )


def coin_to_kraken_contracts(coin_qty: float, entry_price: float, contract_value_usd: float = 1.0) -> int:
    """
    Convert coin-denominated quantity to Kraken Futures contract count.
    
    Kraken Futures PF_* contracts are USD-denominated:
      notional_usd = coin_qty * entry_price
      num_contracts = notional_usd / contract_value_usd
    
    Args:
        coin_qty: Quantity in coins (e.g. 0.002 BTC)
        entry_price: Entry price in USD (e.g. 50000)
        contract_value_usd: Value of 1 contract in USD (default: 1)
    
    Returns:
        Number of contracts (minimum 1)
    """
    notional_usd = coin_qty * entry_price
    contracts = notional_usd / contract_value_usd
    return max(1, round(contracts))


def compute_position_size_for_symbol(
    symbol: str,
    balance: float,
    balance_ccy: Currency,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    leverage: int = 10,
    fx_eurusdt: Optional[float] = None,
    contract_specs: Optional[dict] = None,
) -> SizeResult:
    """
    High-level sizing function with automatic contract spec lookup.
    
    Args:
        symbol: Trading pair symbol (e.g., "BTCUSDT")
        balance: Account balance
        balance_ccy: Account currency
        risk_pct: Risk percentage
        entry_price: Entry price
        stop_price: Stop loss price
        leverage: Desired leverage
        fx_eurusdt: EUR/USDT rate (required for EUR)
        contract_specs: Optional override for contract specs
    
    Returns:
        SizeResult with position sizing details
    """
    specs = contract_specs or get_contract_specs(symbol)
    
    return compute_position_size(
        balance=balance,
        balance_ccy=balance_ccy,
        risk_pct=risk_pct,
        entry_price=entry_price,
        stop_price=stop_price,
        leverage=leverage,
        qty_step=specs["qty_step"],
        min_qty=specs["min_qty"],
        min_notional=specs["min_notional"],
        fx_eurusdt=fx_eurusdt,
    )


def compute_position_size_safe(
    symbol: str,
    balance: float,
    balance_ccy: Currency,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    leverage: int = 10,
    fx_eurusdt: Optional[float] = None,
    max_risk_deviation_pct: Optional[float] = None,
    reject_infeasible: bool = True,
) -> Tuple[Optional[SizeResult], str, Dict[str, Any]]:
    """
    🛡️ SAFE Position sizing with PRE-FLIGHT feasibility check.
    
    This is the RECOMMENDED function to use. It checks if the trade
    is feasible BEFORE computing the size, preventing "QTY_REJECTED"
    errors and risk violation scenarios.
    
    Args:
        symbol: Trading pair (e.g., "BTCUSDT")
        balance: Account balance
        balance_ccy: "EUR" or "USDT"
        risk_pct: Risk percentage (0.01 = 1%)
        entry_price: Entry price
        stop_price: Stop loss price
        leverage: Desired leverage
        fx_eurusdt: EUR/USDT rate (required for EUR)
        max_risk_deviation_pct: Max allowed deviation when clamping
        reject_infeasible: If True, return None for infeasible trades
    
    Returns:
        Tuple of (SizeResult or None, status, details)
        - status: "FEASIBLE", "CLAMPED", "REJECTED"
        - details: Dict with diagnostic info
    
    Example:
        >>> result, status, details = compute_position_size_safe(
        ...     "XRPUSDT", 100.0, "USDT", 0.01, 2.50, 2.48
        ... )
        >>> if result is None:
        ...     print(f"Trade rejected: {status} - {details.get('suggested_risk_pct')}% needed")
        >>> else:
        ...     print(f"Qty: {result.qty}")
    """
    # Convert balance to USDT for feasibility check
    if balance_ccy == "EUR":
        if fx_eurusdt is None or fx_eurusdt <= 0:
            return None, "INVALID_FX", {"error": "fx_eurusdt required for EUR"}
        balance_usdt = balance * fx_eurusdt
    else:
        balance_usdt = balance
    
    # Step 1: Pre-flight feasibility check
    feasible, reason, details = check_trade_feasibility(
        symbol=symbol,
        balance=balance_usdt,
        risk_pct=risk_pct,
        entry_price=entry_price,
        stop_price=stop_price,
        max_risk_deviation_pct=max_risk_deviation_pct,
    )
    
    if not feasible:
        if reject_infeasible:
            logger.warning(
                f"[SIZING_SAFE] ✗ {symbol} | REJECTED | {reason} | "
                f"bal=${balance_usdt:.0f} risk={risk_pct*100:.1f}% | "
                f"Need: ${details.get('min_balance_needed', 'N/A')} or "
                f"{details.get('suggested_risk_pct', 'N/A')}% risk"
            )
            return None, reason, details
    
    # Step 2: Compute position size
    try:
        result = compute_position_size_for_symbol(
            symbol=symbol,
            balance=balance,
            balance_ccy=balance_ccy,
            risk_pct=risk_pct,
            entry_price=entry_price,
            stop_price=stop_price,
            leverage=leverage,
            fx_eurusdt=fx_eurusdt,
        )
        
        # Enrich details
        details["computed_qty"] = result.qty
        details["computed_notional"] = result.notional
        details["margin_required"] = result.margin_required
        
        # Determine final status
        if result.qty <= 0:
            return None, "REJECTED_ZERO_QTY", details
        
        specs = get_contract_specs(symbol)
        raw_qty = details.get("raw_qty", 0)
        
        if result.qty > raw_qty * 1.001:  # Small tolerance
            status = "CLAMPED"
            logger.info(
                f"[SIZING_SAFE] ⚠ {symbol} | CLAMPED | raw={raw_qty:.6f} -> final={result.qty:.6f}"
            )
        else:
            status = "FEASIBLE"
            logger.info(
                f"[SIZING_SAFE] ✓ {symbol} | OK | qty={result.qty:.6f} | notional=${result.notional:.2f}"
            )
        
        return result, status, details
        
    except Exception as e:
        logger.error(f"[SIZING_SAFE] Exception for {symbol}: {e}")
        details["exception"] = str(e)
        return None, "EXCEPTION", details


# ============================================================================
# SELF-TEST
# ============================================================================

if __name__ == "__main__":
    # Test EUR account
    print("\n=== Test: EUR Account, BTCUSDT ===")
    result = compute_position_size(
        balance=1000.0,
        balance_ccy="EUR",
        risk_pct=0.01,          # 1% = 10 EUR
        entry_price=50000.0,
        stop_price=49500.0,     # 500 USDT stop distance
        leverage=10,
        qty_step=0.001,
        min_qty=0.001,
        min_notional=5.0,
        fx_eurusdt=1.08,
    )
    print(f"  Risk: {result.risk_amount:.2f} {result.currency}")
    print(f"  Risk in USDT: {result.risk_in_quote:.2f}")
    print(f"  Stop Distance: {result.stop_distance:.2f} USDT")
    print(f"  Qty: {result.qty:.6f}")
    print(f"  Notional: {result.notional:.2f} USDT")
    print(f"  Leverage: {result.leverage_used}x")
    print(f"  Margin Required: {result.margin_required:.2f} USDT")
    
    # Test USDT account
    print("\n=== Test: USDT Account, ETHUSDT ===")
    result2 = compute_position_size_from_balance_usdt(
        balance_usdt=500.0,
        risk_pct=0.02,          # 2% = 10 USDT
        entry_price=3000.0,
        stop_price=2950.0,      # 50 USDT stop distance
        leverage=5,
        qty_step=0.01,
        min_qty=0.01,
        min_notional=5.0,
    )
    print(f"  Risk: {result2.risk_amount:.2f} {result2.currency}")
    print(f"  Stop Distance: {result2.stop_distance:.2f} USDT")
    print(f"  Qty: {result2.qty:.4f}")
    print(f"  Notional: {result2.notional:.2f} USDT")
    print(f"  Leverage: {result2.leverage_used}x")
    
    # Test symbol helper
    print("\n=== Test: Auto Symbol Specs ===")
    result3 = compute_position_size_for_symbol(
        symbol="SOLUSDT",
        balance=100.0,
        balance_ccy="USDT",
        risk_pct=0.01,
        entry_price=150.0,
        stop_price=147.0,
        leverage=10,
    )
    print(f"  SOLUSDT qty: {result3.qty}")
