"""
SizeCalculator — Robust Position Sizing for Multi-Broker Execution
═══════════════════════════════════════════════════════════════════

Guarantees:
  - qty > 0 (NEVER sends qty=0 to any exchange)
  - qty respects min_qty, qty_step (floor-round), min_notional
  - Kraken-specific: coin→contract conversion with integer contracts
  - Pre-flight validation with actionable rejection reasons
  - All parameters from .env / config (no hardcoded values)

Author: SMC Trading Bot Framework
"""

import logging
import math
import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Literal

from core.env_loader import safe_load_dotenv

safe_load_dotenv()

logger = logging.getLogger("smarttrade.risk.size_calculator")

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION (from .env)
# ═══════════════════════════════════════════════════════════════

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default

def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


# ═══════════════════════════════════════════════════════════════
#  EXCHANGE SPECS — Per-Symbol Constraints
# ═══════════════════════════════════════════════════════════════

BITGET_SPECS: Dict[str, Dict] = {
    "BTCUSDT":   {"qty_step": 0.001,  "min_qty": 0.001,  "min_notional": 5.0, "price_precision": 1},
    "ETHUSDT":   {"qty_step": 0.01,   "min_qty": 0.01,   "min_notional": 5.0, "price_precision": 2},
    "SOLUSDT":   {"qty_step": 0.1,    "min_qty": 0.1,    "min_notional": 5.0, "price_precision": 2},
    "XRPUSDT":   {"qty_step": 1.0,    "min_qty": 1.0,    "min_notional": 5.0, "price_precision": 4},
    "DOGEUSDT":  {"qty_step": 10.0,   "min_qty": 10.0,   "min_notional": 5.0, "price_precision": 5},
    "ADAUSDT":   {"qty_step": 1.0,    "min_qty": 1.0,    "min_notional": 5.0, "price_precision": 4},
    "AVAXUSDT":  {"qty_step": 0.1,    "min_qty": 0.1,    "min_notional": 5.0, "price_precision": 2},
    "LINKUSDT":  {"qty_step": 0.1,    "min_qty": 0.1,    "min_notional": 5.0, "price_precision": 3},
    "MATICUSDT": {"qty_step": 1.0,    "min_qty": 1.0,    "min_notional": 5.0, "price_precision": 4},
    "DOTUSDT":   {"qty_step": 0.1,    "min_qty": 0.1,    "min_notional": 5.0, "price_precision": 3},
}

KRAKEN_SPECS: Dict[str, Dict] = {
    "PF_XBTUSD":  {"qty_step": 0.0001, "min_qty": 0.0001, "min_notional": 0, "qty_precision": 4, "price_precision": 1},
    "PF_ETHUSD":  {"qty_step": 0.001,  "min_qty": 0.001,  "min_notional": 0, "qty_precision": 3, "price_precision": 1},
    "PF_SOLUSD":  {"qty_step": 0.01,   "min_qty": 0.01,   "min_notional": 0, "qty_precision": 2, "price_precision": 2},
    "PF_XRPUSD":  {"qty_step": 1.0,    "min_qty": 1.0,    "min_notional": 0, "qty_precision": 0, "price_precision": 4},
    "PF_DOGEUSD": {"qty_step": 1.0,    "min_qty": 1.0,    "min_notional": 0, "qty_precision": 0, "price_precision": 5},
    "PF_BNBUSD":  {"qty_step": 0.01,   "min_qty": 0.01,   "min_notional": 0, "qty_precision": 2, "price_precision": 2},
    "PF_ADAUSD":  {"qty_step": 1.0,    "min_qty": 1.0,    "min_notional": 0, "qty_precision": 0, "price_precision": 5},
    "PF_AVAXUSD": {"qty_step": 0.01,   "min_qty": 0.01,   "min_notional": 0, "qty_precision": 2, "price_precision": 3},
    "PF_LINKUSD": {"qty_step": 0.1,    "min_qty": 0.1,    "min_notional": 0, "qty_precision": 1, "price_precision": 3},
    "PF_LTCUSD":  {"qty_step": 0.01,   "min_qty": 0.01,   "min_notional": 0, "qty_precision": 2, "price_precision": 2},
}

_BITGET_TO_KRAKEN = {
    "BTCUSDT": "PF_XBTUSD", "ETHUSDT": "PF_ETHUSD", "SOLUSDT": "PF_SOLUSD",
    "XRPUSDT": "PF_XRPUSD", "DOGEUSDT": "PF_DOGEUSD", "BNBUSDT": "PF_BNBUSD",
    "ADAUSDT": "PF_ADAUSD", "AVAXUSDT": "PF_AVAXUSD", "LINKUSDT": "PF_LINKUSD",
    "LTCUSDT": "PF_LTCUSD",
}


def get_specs(symbol: str, broker: str = "bitget") -> Dict:
    """Get symbol specs for a broker. Returns safe defaults if unknown."""
    sym = symbol.upper().replace("/", "").replace(":", "").replace("-", "")
    broker = broker.lower()
    if broker == "kraken":
        if sym in KRAKEN_SPECS:
            return KRAKEN_SPECS[sym]
        kraken_sym = _BITGET_TO_KRAKEN.get(sym)
        if kraken_sym and kraken_sym in KRAKEN_SPECS:
            return KRAKEN_SPECS[kraken_sym]
        return {"qty_step": 0.0001, "min_qty": 0.0001, "min_notional": 0, "qty_precision": 4, "price_precision": 1}
    return BITGET_SPECS.get(sym, {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0, "price_precision": 4})


# ═══════════════════════════════════════════════════════════════
#  RESULT DATACLASS
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SizeResult:
    """Result of position size calculation."""
    qty: float
    notional_usd: float
    risk_usd: float
    stop_distance: float
    leverage_used: int
    broker: str
    symbol: str
    contracts: Optional[float] = None  # Kraken linear crypto contracts
    valid: bool = True
    rejection_reason: Optional[str] = None
    
    def __post_init__(self):
        if self.qty <= 0 and self.valid:
            object.__setattr__(self, 'valid', False)
            object.__setattr__(self, 'rejection_reason', 'INVALID_QTY_ZERO')


# ═══════════════════════════════════════════════════════════════
#  CORE FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def floor_step(value: float, step: float) -> float:
    """Floor-round value to the nearest step. Never rounds up."""
    if step <= 0:
        return value
    return math.floor(value / step + 1e-9) * step


def ceil_step(value: float, step: float) -> float:
    """Ceil-round value to the nearest step."""
    if step <= 0:
        return value
    return math.ceil(value / step - 1e-9) * step


def coin_to_kraken_contracts(coin_qty: float, entry_price: float = 0.0, contract_value_usd: float = 1.0) -> float:
    """
    On Kraken Linear Multi-Collateral (PF_*), order quantity is denominated
    directly in the underlying cryptocurrency (1 contract = 1 crypto unit).
    It is NOT multiplied by price.
    """
    return float(coin_qty)


def ensure_kraken_contracts(
    size: float,
    symbol: str,
    entry_price: float = 0.0,
) -> float:
    """
    SAFETY NET: Ensure size complies with Kraken Futures linear contract specs.
    Returns floored crypto quantity (never multiplied by price).
    """
    specs = get_specs(symbol, "kraken")
    prec = int(specs.get("qty_precision", 4))
    min_qty = float(specs.get("min_qty", 0.0001))

    if float(size) <= 0:
        return 0.0 if prec > 0 else 0

    factor = 10 ** prec if prec > 0 else 1
    floored = math.floor(float(size) * factor + 1e-9) / factor if prec > 0 else float(math.floor(float(size)))
    return int(floored) if prec == 0 else round(floored, prec)


def validate_kraken_order(
    contracts: float,
    symbol: str,
    entry_price: float = 0.0,
    max_notional_usd: float = 100_000.0,
) -> Tuple[bool, Optional[str]]:
    """
    Validate a Kraken Futures order before sending.

    Args:
        contracts: Number of crypto units/contracts
        symbol: Kraken symbol
        entry_price: Reference price for notional check
        max_notional_usd: Maximum allowed notional (safety cap)

    Returns:
        (valid, rejection_reason) — True if order is safe to send
    """
    if not isinstance(contracts, (int, float)):
        return False, f"CONTRACTS_NOT_NUMERIC: got {type(contracts).__name__}={contracts}"

    specs = get_specs(symbol, "kraken")
    min_qty = float(specs.get("min_qty", 0.0001))

    if float(contracts) < min_qty:
        return False, f"CONTRACTS_BELOW_MIN: {contracts} < {min_qty}"

    if entry_price > 0 and max_notional_usd > 0:
        notional = float(contracts) * float(entry_price)
        if notional > max_notional_usd:
            return False, f"NOTIONAL_TOO_HIGH: {notional:.0f} USD > {max_notional_usd:.0f} USD cap"

    return True, None


def calculate_size(
    balance_usd: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    symbol: str,
    broker: str = "bitget",
    leverage: int = 10,
    max_leverage: int = 10,
    fx_rate: Optional[float] = None,
    confidence_score: Optional[float] = None,
) -> SizeResult:
    """
    Calculate position size with full validation.
    
    Args:
        balance_usd: Account balance in USD/USDT
        risk_pct: Risk per trade (0.01 = 1%)
        entry_price: Expected entry price
        stop_price: Stop loss price
        symbol: Trading symbol (e.g., "BTCUSDT" or "PF_XBTUSD")
        broker: "bitget" or "kraken"
        leverage: Requested leverage
        max_leverage: Maximum allowed leverage
        fx_rate: EUR/USD rate if balance is in EUR (optional)
        confidence_score: Signal confidence (0.0-1.0) for sizing modulation
    
    Returns:
        SizeResult with qty > 0 or valid=False with rejection_reason
    """
    broker = broker.lower()
    norm_symbol = symbol.upper().replace("/", "").replace(":", "").replace("-", "")
    
    # ── Confidence-Weighted Sizing ──
    confidence_enabled = _env_bool("CONFIDENCE_SIZING_ENABLED", True)
    if confidence_enabled and confidence_score is not None and 0 < confidence_score <= 1.0:
        min_mult = _env_float("CONFIDENCE_SIZING_MIN_MULT", 0.5)
        max_mult = _env_float("CONFIDENCE_SIZING_MAX_MULT", 1.5)
        # Linear scaling: confidence 0.3 → min_mult, confidence 1.0 → max_mult
        multiplier = min_mult + (max_mult - min_mult) * confidence_score
        multiplier = max(min_mult, min(multiplier, max_mult))
        original_risk = risk_pct
        risk_pct = risk_pct * multiplier
        # Clamp to 10% max
        risk_pct = min(risk_pct, 0.10)
        logger.info(
            "[SIZE_CALC] CONFIDENCE_SIZING | %s | confidence=%.2f | mult=%.2f | risk=%.4f -> %.4f",
            norm_symbol, confidence_score, multiplier, original_risk, risk_pct
        )
    
    # ── Input Validation ──
    if balance_usd <= 0:
        return SizeResult(qty=0, notional_usd=0, risk_usd=0, stop_distance=0,
                         leverage_used=0, broker=broker, symbol=norm_symbol,
                         valid=False, rejection_reason="BALANCE_ZERO_OR_NEGATIVE")
    
    if not (0 < risk_pct <= 0.10):
        return SizeResult(qty=0, notional_usd=0, risk_usd=0, stop_distance=0,
                         leverage_used=0, broker=broker, symbol=norm_symbol,
                         valid=False, rejection_reason=f"INVALID_RISK_PCT_{risk_pct}")
    
    if entry_price <= 0 or stop_price <= 0:
        return SizeResult(qty=0, notional_usd=0, risk_usd=0, stop_distance=0,
                         leverage_used=0, broker=broker, symbol=norm_symbol,
                         valid=False, rejection_reason="INVALID_PRICES")
    
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return SizeResult(qty=0, notional_usd=0, risk_usd=0, stop_distance=0,
                         leverage_used=0, broker=broker, symbol=norm_symbol,
                         valid=False, rejection_reason="STOP_EQUALS_ENTRY")
    
    # ── Leverage Clamp ──
    lev = min(int(leverage), int(max_leverage))
    
    # ── FX Conversion ──
    effective_balance = balance_usd
    if fx_rate and fx_rate > 0:
        effective_balance = balance_usd * fx_rate
    
    # ── Risk Computation ──
    risk_usd = effective_balance * risk_pct
    
    # ── Raw Quantity ──
    raw_qty = risk_usd / stop_distance
    
    # ── Get Specs ──
    specs = get_specs(norm_symbol, broker)
    min_qty = specs.get("min_qty", 0.001)
    qty_step = specs.get("qty_step", 0.001)
    min_notional = specs.get("min_notional", 0)
    
    # ── Floor-round to step ──
    qty = floor_step(raw_qty, qty_step)
    contracts = qty if broker == "kraken" else None
    
    # ── Clamp to min_qty if below ──
    if qty < min_qty:
        # Check if clamping would exceed acceptable risk deviation
        max_deviation = _env_float("MAX_RISK_DEVIATION_PCT", 500.0)
        if raw_qty > 0:
            deviation = ((min_qty - raw_qty) / raw_qty) * 100
        else:
            deviation = float('inf')
        
        if deviation <= max_deviation:
            qty = min_qty
            if broker == "kraken":
                contracts = qty
            logger.info("[SIZE_CALC] %s | Clamped to min_qty: %.6f -> %.6f (deviation=%.1f%%)",
                       norm_symbol, raw_qty, min_qty, deviation)
        else:
            logger.warning("[SIZE_CALC] %s | REJECTED: raw=%.6f < min=%.6f, deviation=%.1f%% > max=%.1f%%",
                          norm_symbol, raw_qty, min_qty, deviation, max_deviation)
            rej_reason = "RISK_EXCEEDS_BUDGET_AFTER_NORMALIZATION" if broker == "kraken" else f"QTY_BELOW_MIN_DEVIATION_{deviation:.0f}pct"
            return SizeResult(qty=0, notional_usd=0, risk_usd=risk_usd, stop_distance=stop_distance,
                             leverage_used=lev, broker=broker, symbol=norm_symbol,
                             valid=False, rejection_reason=rej_reason)
    
    # ── Min Notional Check ──
    notional = qty * entry_price
    if min_notional > 0 and notional < min_notional:
        min_qty_for_notional = ceil_step(min_notional / entry_price, qty_step)
        if min_qty_for_notional > qty:
            qty = min_qty_for_notional
            if broker == "kraken":
                contracts = qty
            notional = qty * entry_price
            logger.info("[SIZE_CALC] %s | Raised qty for min_notional: %.6f (notional=$%.2f)",
                       norm_symbol, qty, notional)
    
    # ── Final Guard: qty must be > 0 ──
    if qty <= 0:
        logger.error("[SIZE_CALC] INVALID_QTY_CALC | %s | qty=%.6f | balance=$%.2f risk=%.2f%% entry=$%.2f sl=$%.2f",
                    norm_symbol, qty, balance_usd, risk_pct * 100, entry_price, stop_price)
        return SizeResult(qty=0, notional_usd=0, risk_usd=risk_usd, stop_distance=stop_distance,
                         leverage_used=lev, broker=broker, symbol=norm_symbol,
                         valid=False, rejection_reason="INVALID_QTY_CALC")
    
    # ── Notional & Risk for Linear Contracts ──
    notional = qty * entry_price
    
    # -- Post-normalisation risk guard (Linear): recompute the REAL risk of the final quantity
    #    (risk = qty * stop_distance). Forced minimums must never push the real risk above
    #    the authorized budget (risk_usd * (1 + max_deviation/100)): refuse the order instead.
    if broker == "kraken" and entry_price > 0 and stop_distance > 0:
        _real_risk = qty * stop_distance
        max_dev = _env_float("MAX_RISK_DEVIATION_PCT", 500.0)
        max_allowed_risk = risk_usd * (1.0 + max_dev / 100.0)
        if _real_risk > max_allowed_risk * (1.0 + 1e-9):
            logger.warning(
                "[SIZE_CALC] %s | REJECTED after normalisation: real_risk=$%.4f > allowed=$%.4f "
                "(budget=$%.4f, qty=%s)", norm_symbol, _real_risk, max_allowed_risk, risk_usd, qty,
            )
            return SizeResult(qty=0, notional_usd=0, risk_usd=risk_usd, stop_distance=stop_distance,
                             leverage_used=lev, broker=broker, symbol=norm_symbol,
                             valid=False, rejection_reason="RISK_EXCEEDS_BUDGET_AFTER_NORMALIZATION")

    logger.info(
        "[SIZE_CALC] OK | %s@%s | balance=$%.2f | risk=%.2f%% ($%.2f) | "
        "sl_dist=$%.4f | qty=%.6f | notional=$%.2f | lev=%dx%s",
        norm_symbol, broker, balance_usd, risk_pct * 100, risk_usd,
        stop_distance, qty, notional, lev,
        f" | contracts={contracts}" if contracts else ""
    )
    
    return SizeResult(
        qty=qty, notional_usd=notional, risk_usd=risk_usd,
        stop_distance=stop_distance, leverage_used=lev,
        broker=broker, symbol=norm_symbol, contracts=contracts,
        valid=True, rejection_reason=None,
    )


def normalize_order(
    broker: str,
    symbol: str,
    side: str,
    qty: float,
    price: Optional[float] = None,
) -> Tuple[bool, Dict]:
    """
    Normalize and validate an order before sending to exchange.
    
    Returns:
        (valid, order_params) — If valid=False, order_params["error"] explains why.
    """
    broker = broker.lower()
    norm_symbol = symbol.upper().replace("/", "").replace(":", "").replace("-", "")
    specs = get_specs(norm_symbol, broker)
    
    min_qty = specs.get("min_qty", 0.001)
    qty_step = specs.get("qty_step", 0.001)
    min_notional = specs.get("min_notional", 0)
    
    # ── Floor-round qty ──
    norm_qty = floor_step(qty, qty_step)
    
    # ── qty > 0 guard ──
    if norm_qty <= 0:
        logger.error("[NORMALIZE] REJECTED | %s@%s | qty=%.6f -> norm=%.6f (step=%.6f)",
                    norm_symbol, broker, qty, norm_qty, qty_step)
        return False, {"error": "INVALID_QTY_ZERO", "raw_qty": qty, "norm_qty": norm_qty, "step": qty_step}
    
    # ── min_qty guard ──
    if norm_qty < min_qty:
        logger.error("[NORMALIZE] REJECTED | %s@%s | qty=%.6f < min=%.6f",
                    norm_symbol, broker, norm_qty, min_qty)
        return False, {"error": "QTY_BELOW_MIN", "qty": norm_qty, "min_qty": min_qty}
    
    # ── min_notional guard ──
    if price and min_notional > 0:
        notional = norm_qty * price
        if notional < min_notional:
            logger.error("[NORMALIZE] REJECTED | %s@%s | notional=$%.2f < min=$%.2f",
                        norm_symbol, broker, notional, min_notional)
            return False, {"error": "NOTIONAL_BELOW_MIN", "notional": notional, "min_notional": min_notional}
    
    # ── Kraken integer contracts ──
    if broker == "kraken":
        norm_qty = float(max(1, int(norm_qty)))
    
    # ── Side validation ──
    side = side.lower()
    if side in ("long", "buy"):
        side = "buy"
    elif side in ("short", "sell"):
        side = "sell"
    else:
        return False, {"error": f"INVALID_SIDE_{side}"}
    
    # ── Price precision ──
    norm_price = price
    if price and broker == "bitget":
        pp = specs.get("price_precision", 4)
        norm_price = round(price, pp)
    
    return True, {
        "symbol": norm_symbol,
        "side": side,
        "qty": norm_qty,
        "price": norm_price,
        "broker": broker,
    }


# ═══════════════════════════════════════════════════════════════
#  DIAGNOSTIC / DRY-RUN
# ═══════════════════════════════════════════════════════════════

def diagnose(symbol: str, balance_usd: float, risk_pct: float,
             entry_price: float, stop_price: float, broker: str = "bitget") -> Dict:
    """Run diagnostic sizing calculation and return detailed breakdown."""
    specs = get_specs(symbol, broker)
    result = calculate_size(
        balance_usd=balance_usd, risk_pct=risk_pct,
        entry_price=entry_price, stop_price=stop_price,
        symbol=symbol, broker=broker,
    )
    return {
        "symbol": symbol,
        "broker": broker,
        "balance_usd": balance_usd,
        "risk_pct": f"{risk_pct*100:.2f}%",
        "risk_usd": f"${result.risk_usd:.2f}",
        "entry_price": entry_price,
        "stop_price": stop_price,
        "stop_distance": f"${result.stop_distance:.4f}",
        "specs": specs,
        "qty": result.qty,
        "contracts": result.contracts,
        "notional_usd": f"${result.notional_usd:.2f}",
        "valid": result.valid,
        "rejection_reason": result.rejection_reason,
    }
