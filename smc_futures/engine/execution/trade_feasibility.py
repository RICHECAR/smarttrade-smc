"""
Trade Feasibility Checker - Pre-flight validation before sizing
================================================================
Validates if a trade is feasible BEFORE computing position size.

Key Functions:
- is_trade_feasible(): Full pre-flight check
- compute_min_risk_for_symbol(): Minimum risk required for a symbol
- compute_min_balance_for_symbol(): Minimum balance required
- get_feasible_symbols(): Filter symbols by account constraints

This module prevents the "QTY_REJECTED" scenario by checking UPFRONT
whether the account can support the minimum position for a symbol.

Author: SMC Trading Bot Framework
"""

import logging
import os
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple
from enum import Enum

from smc_futures.engine.execution.contract_specs import (
    get_contract_specs,
    ContractSpecs,
    CONTRACT_SPECS_REGISTRY,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

class FeasibilityConfig:
    """
    Configuration for feasibility checks.
    
    Attributes:
        max_risk_deviation_pct: Max allowed increase when clamping to min_qty
        allow_clamp: Whether to allow clamping to min_qty at all
        hard_risk_cap_pct: Absolute max risk per trade (5% default)
        min_rr_ratio: Minimum reward/risk ratio required (1.5 default)
        require_notional_margin: % margin above min_notional required (20% default)
    """
    max_risk_deviation_pct: float = float(os.getenv("MAX_RISK_DEVIATION_PCT", "500"))
    allow_clamp: bool = True
    hard_risk_cap_pct: float = 5.0  # 5% max risk per trade
    min_rr_ratio: float = 1.5
    require_notional_margin: float = 20.0  # 20% above min_notional


# Global config instance
_config = FeasibilityConfig()


def get_feasibility_config() -> FeasibilityConfig:
    """Get the global feasibility configuration."""
    return _config


def set_feasibility_config(**kwargs) -> None:
    """Update feasibility configuration."""
    global _config
    for key, value in kwargs.items():
        if hasattr(_config, key):
            setattr(_config, key, value)
            logger.info(f"[FEASIBILITY] Config updated: {key}={value}")


# ═══════════════════════════════════════════════════════════════════════════════
# MINIMUM VIABLE RISK CALCULATOR
# ═══════════════════════════════════════════════════════════════════════════════

def compute_min_viable_risk_usd(
    symbol: str,
    entry_price: float,
    stop_price: float,
    leverage: int = 10,
    min_notional_override: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Compute the MINIMUM risk in USD required to trade a symbol.
    
    This is the absolute floor - below this, the order will be rejected
    because min_qty or min_notional cannot be met.
    
    Use this to:
    - Filter symbols that can be traded with current balance
    - Show users why their trade was rejected
    - Calculate minimum balance needed for a given risk %
    
    Args:
        symbol: Trading pair (e.g., "XRPUSDT")
        entry_price: Entry price
        stop_price: Stop loss price
        leverage: Account leverage (affects margin, not min_risk)
        min_notional_override: Override min_notional if known
    
    Returns:
        Dict with:
            - min_risk_usd: Minimum risk amount in USD
            - min_qty: Minimum quantity for symbol
            - min_notional: Minimum notional for symbol
            - stop_distance: Distance from entry to stop
            - stop_pct: Stop as percentage of entry
            - min_balance_1pct: Min balance needed for 1% risk
            - min_balance_2pct: Min balance needed for 2% risk
            - limiting_factor: "min_qty" or "min_notional"
    
    Example:
        >>> result = compute_min_viable_risk_usd("ETHUSDT", 3500.0, 3465.0)  # 1% stop
        >>> print(f"Min risk: ${result['min_risk_usd']:.2f}")
        >>> print(f"Min balance for 1% risk: ${result['min_balance_1pct']:.0f}")
    """
    specs = get_contract_specs(symbol)
    stop_distance = abs(entry_price - stop_price)
    stop_pct = (stop_distance / entry_price) * 100 if entry_price > 0 else 0
    
    # Minimum risk from min_qty constraint
    min_risk_from_qty = specs.min_qty * stop_distance
    
    # Minimum risk from min_notional constraint
    min_notional = min_notional_override if min_notional_override is not None else specs.min_notional
    min_qty_from_notional = min_notional / entry_price if entry_price > 0 else 0
    min_risk_from_notional = min_qty_from_notional * stop_distance
    
    # Take the larger constraint
    if min_risk_from_qty >= min_risk_from_notional:
        min_risk_usd = min_risk_from_qty
        limiting_factor = "min_qty"
        effective_min_qty = specs.min_qty
    else:
        min_risk_usd = min_risk_from_notional
        limiting_factor = "min_notional"
        effective_min_qty = min_qty_from_notional
    
    return {
        "symbol": symbol,
        "min_risk_usd": round(min_risk_usd, 4),
        "min_qty": specs.min_qty,
        "min_notional": min_notional,
        "effective_min_qty": round(effective_min_qty, 6),
        "stop_distance": round(stop_distance, 6),
        "stop_pct": round(stop_pct, 2),
        "min_balance_1pct": round(min_risk_usd / 0.01, 2) if min_risk_usd > 0 else 0,
        "min_balance_2pct": round(min_risk_usd / 0.02, 2) if min_risk_usd > 0 else 0,
        "min_balance_5pct": round(min_risk_usd / 0.05, 2) if min_risk_usd > 0 else 0,
        "limiting_factor": limiting_factor,
        "entry_price": entry_price,
        "leverage": leverage,
    }


def log_skip_small_size(
    symbol: str,
    raw_qty: float,
    min_qty: float,
    risk_amount: float,
    deviation_pct: float,
    balance: float,
    min_balance_needed: float,
) -> None:
    """
    Log a single consolidated message when a signal is skipped due to small size.
    
    This provides clear actionable information to the user.
    
    Args:
        symbol: Trading pair
        raw_qty: Calculated quantity (too small)
        min_qty: Exchange minimum quantity
        risk_amount: Intended risk in USD
        deviation_pct: Risk deviation % if we clamped
        balance: Current balance
        min_balance_needed: Minimum balance to trade this symbol
    """
    max_dev = float(os.getenv("MAX_RISK_DEVIATION_PCT", "500"))
    logger.warning(
        f"[SKIPPED_SMALL_SIZE] {symbol} | "
        f"qty={raw_qty:.6f} < min={min_qty} | "
        f"risk=${risk_amount:.2f} | "
        f"deviation={deviation_pct:.0f}% (max={max_dev:.0f}%) | "
        f"balance=${balance:.0f} | "
        f"min_balance_needed=${min_balance_needed:.0f}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════

class RejectionReason(Enum):
    """Enumeration of rejection reasons for clear error messages."""
    FEASIBLE = "FEASIBLE"
    BALANCE_TOO_LOW = "BALANCE_TOO_LOW"
    RISK_TOO_SMALL = "RISK_TOO_SMALL"
    STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
    QTY_BELOW_MIN = "QTY_BELOW_MIN"
    NOTIONAL_BELOW_MIN = "NOTIONAL_BELOW_MIN"
    CLAMP_EXCEEDS_DEVIATION = "CLAMP_EXCEEDS_DEVIATION"
    HARD_RISK_CAP_EXCEEDED = "HARD_RISK_CAP_EXCEEDED"
    RR_RATIO_TOO_LOW = "RR_RATIO_TOO_LOW"
    INVALID_PARAMS = "INVALID_PARAMS"


@dataclass
class FeasibilityResult:
    """
    Result of a feasibility check.
    
    Attributes:
        feasible: Whether the trade is feasible
        reason: RejectionReason enum
        message: Human-readable explanation
        details: Dict with diagnostic data
        suggested_action: Optional suggestion for making trade feasible
    """
    feasible: bool
    reason: RejectionReason
    message: str
    details: Dict[str, Any]
    suggested_action: Optional[str] = None
    
    def __bool__(self) -> bool:
        return self.feasible
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "feasible": self.feasible,
            "reason": self.reason.value,
            "message": self.message,
            "details": self.details,
            "suggested_action": self.suggested_action,
        }


@dataclass
class MinimumRequirements:
    """
    Minimum requirements for trading a symbol.
    
    Attributes:
        symbol: Trading pair
        min_qty: Bitget minimum quantity
        min_notional: Bitget minimum notional ($)
        min_risk_usdt: Minimum risk in USDT to trade this symbol
        min_balance_1pct: Minimum balance at 1% risk
        min_balance_2pct: Minimum balance at 2% risk
        typical_stop_pct: Typical stop distance for this asset class
    """
    symbol: str
    min_qty: float
    min_notional: float
    min_risk_usdt: float
    min_balance_1pct: float
    min_balance_2pct: float
    typical_stop_pct: float


# ═══════════════════════════════════════════════════════════════════════════════
# CORE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def compute_min_risk_for_symbol(
    symbol: str,
    entry_price: float,
    stop_price: float,
) -> Tuple[float, Dict[str, Any]]:
    """
    Calculate the MINIMUM risk required to trade a symbol.
    
    This is the risk when trading exactly min_qty.
    
    Args:
        symbol: Trading pair (e.g., "XRPUSDT")
        entry_price: Entry price
        stop_price: Stop loss price
    
    Returns:
        Tuple of (min_risk_usdt, details_dict)
    
    Example:
        >>> min_risk, details = compute_min_risk_for_symbol("XRPUSDT", 2.50, 2.48)
        >>> print(f"Min risk: ${min_risk:.2f}")
        Min risk: $0.02  # 1 XRP * $0.02 stop distance
    """
    specs = get_contract_specs(symbol)
    stop_distance = abs(entry_price - stop_price)
    
    if stop_distance <= 0:
        return 0.0, {"error": "stop_distance must be > 0"}
    
    # Minimum possible risk = min_qty * stop_distance
    min_risk = specs.min_qty * stop_distance
    
    # Also check notional constraint
    min_notional_qty = specs.min_notional / entry_price if entry_price > 0 else 0
    min_qty_effective = max(specs.min_qty, min_notional_qty)
    min_risk_effective = min_qty_effective * stop_distance
    
    details = {
        "symbol": symbol,
        "min_qty": specs.min_qty,
        "min_notional": specs.min_notional,
        "min_notional_qty": round(min_notional_qty, 6),
        "min_qty_effective": round(min_qty_effective, 6),
        "entry_price": entry_price,
        "stop_price": stop_price,
        "stop_distance": round(stop_distance, 6),
        "stop_pct": round((stop_distance / entry_price) * 100, 2),
        "min_risk_usdt": round(min_risk_effective, 4),
    }
    
    return min_risk_effective, details


def compute_min_balance_for_symbol(
    symbol: str,
    entry_price: float,
    stop_price: float,
    risk_pct: float = 0.01,
) -> Tuple[float, Dict[str, Any]]:
    """
    Calculate minimum balance required to trade a symbol at given risk %.
    
    Args:
        symbol: Trading pair
        entry_price: Entry price
        stop_price: Stop loss price
        risk_pct: Risk percentage (0.01 = 1%)
    
    Returns:
        Tuple of (min_balance_usdt, details_dict)
    
    Example:
        >>> min_bal, _ = compute_min_balance_for_symbol("XRPUSDT", 2.50, 2.48, 0.01)
        >>> print(f"Min balance for 1% risk: ${min_bal:.0f}")
        Min balance for 1% risk: $2  # Very small because XRP cheap
    """
    min_risk, details = compute_min_risk_for_symbol(symbol, entry_price, stop_price)
    
    if risk_pct <= 0:
        return float('inf'), {"error": "risk_pct must be > 0"}
    
    # min_balance = min_risk / risk_pct
    min_balance = min_risk / risk_pct
    
    details["risk_pct"] = risk_pct
    details["min_balance_usdt"] = round(min_balance, 2)
    
    return min_balance, details


def is_trade_feasible(
    symbol: str,
    balance: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    take_profit: Optional[float] = None,
    leverage: int = 10,
    config: Optional[FeasibilityConfig] = None,
) -> FeasibilityResult:
    """
    Complete pre-flight feasibility check for a trade.
    
    Checks:
    1. Hard risk cap (max 5% per trade)
    2. Minimum quantity achievable
    3. Risk deviation if clamping needed
    4. Notional minimum
    5. R:R ratio (if TP provided)
    
    Args:
        symbol: Trading pair
        balance: Account balance in USDT
        risk_pct: Risk percentage (0.01 = 1%)
        entry_price: Entry price
        stop_price: Stop loss price
        take_profit: Optional take profit price
        leverage: Leverage (for margin check, doesn't affect risk)
        config: Optional FeasibilityConfig override
    
    Returns:
        FeasibilityResult with feasibility status and diagnostics
    
    Example:
        >>> result = is_trade_feasible("XRPUSDT", 100, 0.01, 2.50, 2.48)
        >>> if not result.feasible:
        ...     print(f"Trade rejected: {result.message}")
    """
    cfg = config or get_feasibility_config()
    specs = get_contract_specs(symbol)
    
    details = {
        "symbol": symbol,
        "balance": balance,
        "risk_pct": risk_pct,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "min_qty": specs.min_qty,
        "min_notional": specs.min_notional,
        "qty_step": specs.qty_step,
    }
    
    # ─────────────────────────────────────────────────────────────
    # Check 0: Validate inputs
    # ─────────────────────────────────────────────────────────────
    if balance <= 0 or entry_price <= 0 or stop_price <= 0:
        return FeasibilityResult(
            feasible=False,
            reason=RejectionReason.INVALID_PARAMS,
            message="Balance, entry, and stop must be > 0",
            details=details,
        )
    
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return FeasibilityResult(
            feasible=False,
            reason=RejectionReason.INVALID_PARAMS,
            message="Entry and stop prices must be different",
            details=details,
        )
    
    details["stop_distance"] = round(stop_distance, 6)
    details["stop_pct"] = round((stop_distance / entry_price) * 100, 4)
    
    # ─────────────────────────────────────────────────────────────
    # Check 1: Hard risk cap
    # ─────────────────────────────────────────────────────────────
    if risk_pct > cfg.hard_risk_cap_pct / 100:
        return FeasibilityResult(
            feasible=False,
            reason=RejectionReason.HARD_RISK_CAP_EXCEEDED,
            message=f"Risk {risk_pct*100:.1f}% exceeds hard cap {cfg.hard_risk_cap_pct}%",
            details=details,
            suggested_action=f"Reduce risk_pct to <= {cfg.hard_risk_cap_pct}%",
        )
    
    # ─────────────────────────────────────────────────────────────
    # Check 2: Calculate raw quantity
    # ─────────────────────────────────────────────────────────────
    risk_amount = balance * risk_pct
    raw_qty = risk_amount / stop_distance
    
    details["risk_amount"] = round(risk_amount, 4)
    details["raw_qty"] = round(raw_qty, 6)
    
    # ─────────────────────────────────────────────────────────────
    # Check 3: Quantity vs min_qty
    # ─────────────────────────────────────────────────────────────
    if raw_qty < specs.min_qty:
        # Calculate how much we'd need to clamp
        deviation_pct = ((specs.min_qty - raw_qty) / raw_qty) * 100 if raw_qty > 0 else float('inf')
        details["deviation_pct"] = round(deviation_pct, 1)
        
        if not cfg.allow_clamp or deviation_pct > cfg.max_risk_deviation_pct:
            # Calculate what would make this feasible
            min_risk_needed, _ = compute_min_risk_for_symbol(symbol, entry_price, stop_price)
            min_balance_needed = min_risk_needed / risk_pct if risk_pct > 0 else float('inf')
            
            details["min_risk_needed"] = round(min_risk_needed, 4)
            details["min_balance_needed"] = round(min_balance_needed, 2)
            
            # Single consolidated log for the rejection
            log_skip_small_size(
                symbol=symbol,
                raw_qty=raw_qty,
                min_qty=specs.min_qty,
                risk_amount=risk_amount,
                deviation_pct=deviation_pct,
                balance=balance,
                min_balance_needed=min_balance_needed,
            )
            
            return FeasibilityResult(
                feasible=False,
                reason=RejectionReason.CLAMP_EXCEEDS_DEVIATION,
                message=(
                    f"raw_qty={raw_qty:.6f} < min_qty={specs.min_qty} | "
                    f"Clamp would increase risk by {deviation_pct:.1f}% (max={cfg.max_risk_deviation_pct}%)"
                ),
                details=details,
                suggested_action=(
                    f"Increase balance to ${min_balance_needed:.0f} or "
                    f"increase risk to {(min_risk_needed/balance)*100:.1f}% or "
                    f"widen stop to {(risk_amount/specs.min_qty):.4f}$"
                ),
            )
        
        # Clamping allowed and within tolerance
        details["clamped_qty"] = specs.min_qty
        details["clamped_risk"] = round(specs.min_qty * stop_distance, 4)
        details["clamped_risk_pct"] = round((specs.min_qty * stop_distance / balance) * 100, 2)
    
    # ─────────────────────────────────────────────────────────────
    # Check 4: Notional minimum
    # ─────────────────────────────────────────────────────────────
    effective_qty = max(raw_qty, specs.min_qty)
    notional = effective_qty * entry_price
    details["notional"] = round(notional, 2)
    
    min_notional_with_margin = specs.min_notional * (1 + cfg.require_notional_margin / 100)
    
    if notional < specs.min_notional:
        return FeasibilityResult(
            feasible=False,
            reason=RejectionReason.NOTIONAL_BELOW_MIN,
            message=f"Notional ${notional:.2f} < min ${specs.min_notional}",
            details=details,
            suggested_action=f"Trade requires at least ${specs.min_notional} notional",
        )
    
    # ─────────────────────────────────────────────────────────────
    # Check 5: R:R ratio (if TP provided)
    # ─────────────────────────────────────────────────────────────
    if take_profit is not None:
        reward_distance = abs(take_profit - entry_price)
        rr_ratio = reward_distance / stop_distance if stop_distance > 0 else 0
        details["rr_ratio"] = round(rr_ratio, 2)
        
        if rr_ratio < cfg.min_rr_ratio:
            return FeasibilityResult(
                feasible=False,
                reason=RejectionReason.RR_RATIO_TOO_LOW,
                message=f"R:R ratio {rr_ratio:.2f} < minimum {cfg.min_rr_ratio}",
                details=details,
                suggested_action=f"Improve R:R to at least {cfg.min_rr_ratio}:1",
            )
    
    # ─────────────────────────────────────────────────────────────
    # All checks passed!
    # ─────────────────────────────────────────────────────────────
    logger.info(
        f"[FEASIBILITY] ✓ {symbol} | balance=${balance:.0f} risk={risk_pct*100:.1f}% | "
        f"raw_qty={raw_qty:.6f} min_qty={specs.min_qty} | FEASIBLE"
    )
    
    return FeasibilityResult(
        feasible=True,
        reason=RejectionReason.FEASIBLE,
        message="Trade is feasible",
        details=details,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_symbol_requirements(
    symbol: str,
    typical_stop_pct: float = 1.0,
) -> MinimumRequirements:
    """
    Get minimum requirements for a symbol based on typical stop distance.
    
    Args:
        symbol: Trading pair
        typical_stop_pct: Typical stop distance as % of price (1.0 = 1%)
    
    Returns:
        MinimumRequirements dataclass
    """
    specs = get_contract_specs(symbol)
    
    # Assume typical price (we don't have real-time price here)
    # This is for estimation only
    TYPICAL_PRICES = {
        "BTCUSDT": 95000.0,
        "ETHUSDT": 3500.0,
        "SOLUSDT": 180.0,
        "XRPUSDT": 2.50,
        "DOGEUSDT": 0.35,
        "AVAXUSDT": 40.0,
        "LINKUSDT": 25.0,
        "ADAUSDT": 1.0,
        "DOTUSDT": 8.0,
    }
    typical_price = TYPICAL_PRICES.get(symbol.upper(), 100.0)
    
    # Calculate typical stop distance
    stop_distance = typical_price * (typical_stop_pct / 100)
    
    # Min risk = min_qty * stop_distance
    min_risk = specs.min_qty * stop_distance
    
    # Also consider notional constraint
    min_notional_qty = specs.min_notional / typical_price if typical_price > 0 else 0
    min_qty_effective = max(specs.min_qty, min_notional_qty)
    min_risk_effective = min_qty_effective * stop_distance
    
    return MinimumRequirements(
        symbol=symbol,
        min_qty=specs.min_qty,
        min_notional=specs.min_notional,
        min_risk_usdt=round(min_risk_effective, 4),
        min_balance_1pct=round(min_risk_effective / 0.01, 2),
        min_balance_2pct=round(min_risk_effective / 0.02, 2),
        typical_stop_pct=typical_stop_pct,
    )


def get_feasible_symbols(
    balance: float,
    risk_pct: float = 0.01,
    typical_stop_pct: float = 1.0,
) -> List[str]:
    """
    Get list of symbols that are feasible for given balance and risk.
    
    Args:
        balance: Account balance in USDT
        risk_pct: Risk percentage
        typical_stop_pct: Typical stop distance as % of price
    
    Returns:
        List of feasible symbol names
    """
    feasible = []
    risk_amount = balance * risk_pct
    
    for symbol in CONTRACT_SPECS_REGISTRY.keys():
        reqs = get_symbol_requirements(symbol, typical_stop_pct)
        
        if risk_amount >= reqs.min_risk_usdt:
            feasible.append(symbol)
    
    return feasible


def get_infeasible_symbols_report(
    balance: float,
    risk_pct: float = 0.01,
    typical_stop_pct: float = 1.0,
) -> Dict[str, Dict[str, Any]]:
    """
    Get report of why certain symbols are not feasible.
    
    Returns dict mapping symbol -> reason details.
    """
    risk_amount = balance * risk_pct
    report = {}
    
    for symbol in CONTRACT_SPECS_REGISTRY.keys():
        reqs = get_symbol_requirements(symbol, typical_stop_pct)
        
        if risk_amount < reqs.min_risk_usdt:
            report[symbol] = {
                "min_risk_required": reqs.min_risk_usdt,
                "your_risk_amount": round(risk_amount, 4),
                "shortfall": round(reqs.min_risk_usdt - risk_amount, 4),
                "min_balance_needed": reqs.min_balance_1pct if risk_pct == 0.01 else round(reqs.min_risk_usdt / risk_pct, 2),
                "min_risk_pct_needed": round((reqs.min_risk_usdt / balance) * 100, 2) if balance > 0 else 100,
            }
    
    return report


# ═══════════════════════════════════════════════════════════════════════════════
# PRE-FLIGHT WRAPPER (Drop-in for OrderManager)
# ═══════════════════════════════════════════════════════════════════════════════

async def pre_flight_check(
    symbol: str,
    balance: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    take_profit: Optional[float] = None,
    raise_on_fail: bool = False,
) -> FeasibilityResult:
    """
    Async wrapper for is_trade_feasible, suitable for OrderManager integration.
    
    Args:
        symbol: Trading pair
        balance: Account balance
        risk_pct: Risk percentage
        entry_price: Entry price
        stop_price: Stop loss price
        take_profit: Optional take profit
        raise_on_fail: If True, raise ValueError on infeasible trade
    
    Returns:
        FeasibilityResult
    
    Raises:
        ValueError: If raise_on_fail=True and trade is infeasible
    """
    result = is_trade_feasible(
        symbol=symbol,
        balance=balance,
        risk_pct=risk_pct,
        entry_price=entry_price,
        stop_price=stop_price,
        take_profit=take_profit,
    )
    
    if not result.feasible:
        logger.warning(
            f"[PRE_FLIGHT] ✗ {symbol} | {result.reason.value} | {result.message}"
        )
        
        if raise_on_fail:
            raise ValueError(f"Trade infeasible: {result.message}")
    
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("\n" + "="*70)
    print("TRADE FEASIBILITY CHECKER - SELF TEST")
    print("="*70)
    
    # Test 1: Small account with XRP (the problem case)
    print("\n=== Test 1: Small account ($100) + XRP 1% stop ===")
    result = is_trade_feasible(
        symbol="XRPUSDT",
        balance=100.0,
        risk_pct=0.01,  # 1% = $1 risk
        entry_price=2.50,
        stop_price=2.475,  # 1% stop = $0.025 distance
    )
    print(f"  Feasible: {result.feasible}")
    print(f"  Reason: {result.reason.value}")
    print(f"  Message: {result.message}")
    if result.suggested_action:
        print(f"  Suggestion: {result.suggested_action}")
    print(f"  Details: raw_qty={result.details.get('raw_qty')}, min_qty={result.details.get('min_qty')}")
    
    # Test 2: Large account with XRP
    print("\n=== Test 2: Large account ($10,000) + XRP 1% stop ===")
    result = is_trade_feasible(
        symbol="XRPUSDT",
        balance=10000.0,
        risk_pct=0.01,  # 1% = $100 risk
        entry_price=2.50,
        stop_price=2.475,
    )
    print(f"  Feasible: {result.feasible}")
    print(f"  Reason: {result.reason.value}")
    
    # Test 3: Get feasible symbols for small account
    print("\n=== Test 3: Feasible symbols for $100 account at 1% risk ===")
    feasible = get_feasible_symbols(100.0, 0.01, 1.0)
    print(f"  Feasible: {feasible}")
    
    infeasible_report = get_infeasible_symbols_report(100.0, 0.01, 1.0)
    print(f"  Infeasible: {list(infeasible_report.keys())}")
    for sym, reason in infeasible_report.items():
        print(f"    {sym}: need ${reason['min_risk_required']:.2f} risk, have ${reason['your_risk_amount']:.2f}")
    
    # Test 4: Minimum requirements
    print("\n=== Test 4: XRP minimum requirements ===")
    reqs = get_symbol_requirements("XRPUSDT", 1.0)
    print(f"  min_qty: {reqs.min_qty}")
    print(f"  min_notional: ${reqs.min_notional}")
    print(f"  min_risk (1% stop): ${reqs.min_risk_usdt}")
    print(f"  min_balance (1% risk): ${reqs.min_balance_1pct}")
    print(f"  min_balance (2% risk): ${reqs.min_balance_2pct}")
    
    # Test 5: Hard risk cap
    print("\n=== Test 5: Hard risk cap (10% risk) ===")
    result = is_trade_feasible(
        symbol="BTCUSDT",
        balance=10000.0,
        risk_pct=0.10,  # 10% - exceeds 5% cap
        entry_price=95000.0,
        stop_price=94000.0,
    )
    print(f"  Feasible: {result.feasible}")
    print(f"  Reason: {result.reason.value}")
    
    print("\n" + "="*70)
    print("TESTS COMPLETE")
    print("="*70)
