"""
Small Account Manager — Intelligent Symbol Eligibility & Risk Scaling
══════════════════════════════════════════════════════════════════════

Designed for growing small accounts (~$20-100) without deposits.

Features:
- Real-time symbol eligibility calculation based on equity/minQty/minNotional
- Progressive risk auto-tuning (1.5% → 3.0% by 0.25% increments)
- Auto-disable ineligible symbols (BTC/ETH early, enable as equity grows)
- Dynamic cooldown to prevent overtrading
- Endpoint data for /api/v1/status

Usage:
    from smc_futures.engine.small_account_manager import SmallAccountManager
    
    sam = SmallAccountManager(config)
    eligible = sam.get_eligible_symbols(equity=23.0, prices={"XRPUSDT": 0.55, ...})
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

from smc_futures.engine.execution.contract_specs import get_contract_specs, CONTRACT_SPECS_REGISTRY

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SymbolEligibility:
    """Eligibility status for a single symbol."""
    symbol: str
    eligible: bool
    reason: str  # "ELIGIBLE", "MIN_QTY_TOO_HIGH", "MIN_NOTIONAL_TOO_HIGH", "PRICE_UNKNOWN"
    
    # Calculated values
    min_equity_needed: float  # Minimum equity to be eligible at current price
    current_max_qty: float    # Max qty achievable with current equity/risk
    min_qty_required: float   # Exchange minimum
    min_notional: float
    current_price: float
    
    # Risk info
    risk_pct_used: float
    risk_pct_needed: float  # Risk % needed to reach minQty (None if impossible)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "eligible": self.eligible,
            "reason": self.reason,
            "min_equity_needed": round(self.min_equity_needed, 2),
            "current_max_qty": round(self.current_max_qty, 6),
            "min_qty_required": self.min_qty_required,
            "min_notional": self.min_notional,
            "current_price": self.current_price,
            "risk_pct_used": round(self.risk_pct_used, 2),
            "risk_pct_needed": round(self.risk_pct_needed, 2) if self.risk_pct_needed else None,
        }


@dataclass
class SmallAccountStatus:
    """Overall status for small account mode."""
    enabled: bool
    equity: float
    base_risk_pct: float
    current_risk_pct: float
    max_risk_pct: float
    eligible_symbols: List[str]
    ineligible_symbols: List[str]
    symbol_details: Dict[str, SymbolEligibility]
    next_thresholds: Dict[str, float]  # symbol -> equity needed
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "small_account_mode": self.enabled,
            "equity": round(self.equity, 2),
            "risk_pct": {
                "base": self.base_risk_pct,
                "current": round(self.current_risk_pct, 2),
                "max": self.max_risk_pct,
            },
            "eligible_symbols": self.eligible_symbols,
            "ineligible_symbols": self.ineligible_symbols,
            "symbol_eligibility": {s: e.to_dict() for s, e in self.symbol_details.items()},
            "next_equity_thresholds": {s: round(v, 2) for s, v in self.next_thresholds.items()},
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Small Account Manager
# ═══════════════════════════════════════════════════════════════════════════════

class SmallAccountManager:
    """
    Manages symbol eligibility and risk scaling for small accounts.
    """
    
    def __init__(self, config: Dict[str, Any]):
        # Config
        self.enabled = config.get("SMALL_ACCOUNT_MODE", False)
        self.risk_start = config.get("SMALL_ACC_RISK_START", 1.5)
        self.risk_max = config.get("SMALL_ACC_RISK_MAX", 3.0)
        self.risk_step = config.get("SMALL_ACC_RISK_STEP", 0.25)
        self.leverage = config.get("DEFAULT_LEVERAGE", 5)
        self.max_daily_loss_pct = config.get("MAX_DAILY_LOSS_PCT", 3.0)
        self.base_cooldown_sec = config.get("TRADE_COOLDOWN_SEC", 300)
        
        # State
        self._current_risk_pct = self.risk_start
        self._rejection_counts: Dict[str, int] = {}  # symbol -> consecutive rejections
        self._last_eligibility_check: float = 0
        self._cached_eligibility: Dict[str, SymbolEligibility] = {}
        self._disabled_symbols: set = set()
        
        # Dynamic cooldown state
        self._last_trade_time: float = 0
        self._current_cooldown: int = self.base_cooldown_sec
        
        if self.enabled:
            logger.info(
                f"[SMALL_ACC] Mode ENABLED | risk_start={self.risk_start}%"
                f" | risk_max={self.risk_max}% | step={self.risk_step}%"
            )
    
    # ─────────────────────────────────────────────────────────────────
    # Symbol Eligibility Calculation
    # ─────────────────────────────────────────────────────────────────
    
    def calculate_symbol_eligibility(
        self,
        symbol: str,
        equity: float,
        current_price: float,
        risk_pct: Optional[float] = None,
        typical_sl_pct: float = 1.5,  # Typical SL distance as % of entry
    ) -> SymbolEligibility:
        """
        Calculate if a symbol is eligible for trading given current equity.
        
        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            equity: Current account equity in USDT
            current_price: Current market price
            risk_pct: Risk % to use (or current auto-tuned value)
            typical_sl_pct: Typical stop loss distance (1.5% = tight SL)
        
        Returns:
            SymbolEligibility with all details
        """
        if risk_pct is None:
            risk_pct = self._current_risk_pct
        
        specs = get_contract_specs(symbol)
        
        if current_price <= 0:
            return SymbolEligibility(
                symbol=symbol,
                eligible=False,
                reason="PRICE_UNKNOWN",
                min_equity_needed=float('inf'),
                current_max_qty=0,
                min_qty_required=specs.min_qty,
                min_notional=specs.min_notional,
                current_price=0,
                risk_pct_used=risk_pct,
                risk_pct_needed=None,
            )
        
        # Calculate maximum achievable qty with current equity/risk
        # Formula: qty = risk_amount / risk_distance
        #          risk_amount = equity * (risk_pct / 100)
        #          risk_distance = current_price * (typical_sl_pct / 100)
        risk_amount = equity * (risk_pct / 100.0)
        risk_distance = current_price * (typical_sl_pct / 100.0)
        
        if risk_distance <= 0:
            risk_distance = current_price * 0.01  # Fallback 1%
        
        current_max_qty = risk_amount / risk_distance
        
        # Check minQty constraint
        min_qty = specs.min_qty
        min_notional = specs.min_notional
        
        # Calculate minimum equity needed for this symbol
        # min_qty = (min_equity * risk_pct/100) / risk_distance
        # min_equity = (min_qty * risk_distance * 100) / risk_pct
        min_equity_for_qty = (min_qty * risk_distance * 100.0) / risk_pct
        
        # Also check notional constraint
        # notional = qty * price >= min_notional
        # qty >= min_notional / price
        min_qty_for_notional = min_notional / current_price if current_price > 0 else float('inf')
        effective_min_qty = max(min_qty, min_qty_for_notional)
        
        min_equity_for_notional = (min_qty_for_notional * risk_distance * 100.0) / risk_pct
        min_equity_needed = max(min_equity_for_qty, min_equity_for_notional)
        
        # Calculate risk_pct needed to reach minQty with current equity
        # min_qty = (equity * needed_pct/100) / risk_distance
        # needed_pct = (min_qty * risk_distance * 100) / equity
        if equity > 0:
            risk_pct_needed = (effective_min_qty * risk_distance * 100.0) / equity
        else:
            risk_pct_needed = float('inf')
        
        # Determine eligibility
        if current_max_qty >= effective_min_qty:
            eligible = True
            reason = "ELIGIBLE"
        elif risk_pct_needed <= self.risk_max:
            # Could be eligible with risk upscale
            eligible = True
            reason = "ELIGIBLE_WITH_UPSCALE"
        else:
            eligible = False
            if current_max_qty < min_qty:
                reason = "MIN_QTY_TOO_HIGH"
            else:
                reason = "MIN_NOTIONAL_TOO_HIGH"
        
        return SymbolEligibility(
            symbol=symbol,
            eligible=eligible,
            reason=reason,
            min_equity_needed=min_equity_needed,
            current_max_qty=current_max_qty,
            min_qty_required=effective_min_qty,
            min_notional=min_notional,
            current_price=current_price,
            risk_pct_used=risk_pct,
            risk_pct_needed=risk_pct_needed if risk_pct_needed <= 100 else None,
        )
    
    def get_eligible_symbols(
        self,
        equity: float,
        prices: Dict[str, float],
        all_symbols: Optional[List[str]] = None,
        force_refresh: bool = False,
    ) -> Tuple[List[str], List[str], Dict[str, SymbolEligibility]]:
        """
        Get lists of eligible and ineligible symbols.
        
        Args:
            equity: Current account equity
            prices: Dict of symbol -> current price
            all_symbols: Symbols to check (defaults to registry)
            force_refresh: Force recalculation even if cached
        
        Returns:
            (eligible_symbols, ineligible_symbols, details)
        """
        now = time.time()
        
        # Use cache if recent (< 60s) and not forced
        if not force_refresh and (now - self._last_eligibility_check) < 60:
            eligible = [s for s, e in self._cached_eligibility.items() if e.eligible]
            ineligible = [s for s, e in self._cached_eligibility.items() if not e.eligible]
            return eligible, ineligible, self._cached_eligibility
        
        if all_symbols is None:
            all_symbols = list(CONTRACT_SPECS_REGISTRY.keys())
        
        eligible = []
        ineligible = []
        details = {}
        
        for symbol in all_symbols:
            price = prices.get(symbol, 0)
            elig = self.calculate_symbol_eligibility(symbol, equity, price)
            details[symbol] = elig
            
            if elig.eligible:
                eligible.append(symbol)
                if symbol in self._disabled_symbols:
                    self._disabled_symbols.remove(symbol)
                    logger.info(f"[SYMBOL_ELIGIBLE] {symbol} | equity=${equity:.2f} >= ${elig.min_equity_needed:.2f}")
            else:
                ineligible.append(symbol)
                if symbol not in self._disabled_symbols:
                    self._disabled_symbols.add(symbol)
                    logger.warning(
                        f"[SYMBOL_INELIGIBLE] {symbol} | {elig.reason}"
                        f" | equity=${equity:.2f} < ${elig.min_equity_needed:.2f} needed"
                        f" | price=${price:.4f}"
                    )
        
        self._last_eligibility_check = now
        self._cached_eligibility = details
        
        return eligible, ineligible, details
    
    # ─────────────────────────────────────────────────────────────────
    # Progressive Risk Auto-Tuning
    # ─────────────────────────────────────────────────────────────────
    
    def handle_rejection(self, symbol: str, reason: str) -> Optional[float]:
        """
        Handle a minQty/minNotional rejection.
        May increase risk_pct progressively.
        
        Returns:
            New risk_pct if adjusted, None otherwise
        """
        if not self.enabled:
            return None
        
        if reason not in ("MIN_QTY_TOO_HIGH", "BELOW_MIN_QTY", "SYMBOL_INELIGIBLE"):
            return None
        
        # Track rejections
        self._rejection_counts[symbol] = self._rejection_counts.get(symbol, 0) + 1
        consecutive = self._rejection_counts[symbol]
        
        # After 2 consecutive rejections on eligible symbol, try increasing risk
        if consecutive >= 2:
            old_risk = self._current_risk_pct
            new_risk = min(old_risk + self.risk_step, self.risk_max)
            
            if new_risk > old_risk:
                self._current_risk_pct = new_risk
                logger.info(
                    f"[RISK_AUTOTUNE] {symbol} | {consecutive} rejections"
                    f" | risk: {old_risk:.2f}% -> {new_risk:.2f}%"
                )
                self._rejection_counts[symbol] = 0  # Reset counter
                return new_risk
            else:
                # Already at max - symbol truly ineligible
                logger.warning(
                    f"[RISK_AUTOTUNE] {symbol} | At max risk {self.risk_max}%"
                    f" | Symbol remains ineligible"
                )
        
        return None
    
    def handle_success(self, symbol: str):
        """Reset rejection counter on successful trade."""
        if symbol in self._rejection_counts:
            self._rejection_counts[symbol] = 0
    
    def get_current_risk_pct(self) -> float:
        """Get current auto-tuned risk percentage."""
        return self._current_risk_pct
    
    def reset_risk_to_base(self):
        """Reset risk to starting value (e.g., after equity increase)."""
        if self._current_risk_pct != self.risk_start:
            logger.info(
                f"[RISK_RESET] {self._current_risk_pct:.2f}% -> {self.risk_start:.2f}%"
            )
            self._current_risk_pct = self.risk_start
    
    # ─────────────────────────────────────────────────────────────────
    # Dynamic Cooldown
    # ─────────────────────────────────────────────────────────────────
    
    def get_dynamic_cooldown(self, equity: float, win_streak: int = 0, loss_streak: int = 0) -> int:
        """
        Calculate dynamic cooldown based on account state.
        
        - Small equity: longer cooldown (protect capital)
        - Loss streak: longer cooldown (avoid tilt)
        - Win streak: slight reduction (momentum)
        """
        base = self.base_cooldown_sec
        
        # Equity factor: smaller accounts get longer cooldown
        if equity < 50:
            equity_factor = 1.5
        elif equity < 100:
            equity_factor = 1.2
        else:
            equity_factor = 1.0
        
        # Streak factors
        if loss_streak >= 3:
            streak_factor = 2.0  # Double cooldown on losing streak
        elif loss_streak >= 2:
            streak_factor = 1.5
        elif win_streak >= 3:
            streak_factor = 0.8  # Slight reduction on winning streak
        else:
            streak_factor = 1.0
        
        cooldown = int(base * equity_factor * streak_factor)
        self._current_cooldown = cooldown
        
        return cooldown
    
    def record_trade(self):
        """Record a trade for cooldown tracking."""
        self._last_trade_time = time.time()
    
    def get_cooldown_remaining(self) -> int:
        """Get seconds remaining in cooldown."""
        elapsed = time.time() - self._last_trade_time
        remaining = max(0, self._current_cooldown - int(elapsed))
        return remaining
    
    # ─────────────────────────────────────────────────────────────────
    # Status & Reporting
    # ─────────────────────────────────────────────────────────────────
    
    def get_status(
        self,
        equity: float,
        prices: Dict[str, float],
        all_symbols: Optional[List[str]] = None,
    ) -> SmallAccountStatus:
        """
        Get full status for API endpoint.
        """
        eligible, ineligible, details = self.get_eligible_symbols(equity, prices, all_symbols)
        
        # Calculate next thresholds (equity needed for each ineligible symbol)
        next_thresholds = {}
        for symbol in ineligible:
            if symbol in details:
                next_thresholds[symbol] = details[symbol].min_equity_needed
        
        # Sort by threshold (easiest to reach first)
        next_thresholds = dict(sorted(next_thresholds.items(), key=lambda x: x[1]))
        
        return SmallAccountStatus(
            enabled=self.enabled,
            equity=equity,
            base_risk_pct=self.risk_start,
            current_risk_pct=self._current_risk_pct,
            max_risk_pct=self.risk_max,
            eligible_symbols=sorted(eligible),
            ineligible_symbols=sorted(ineligible, key=lambda s: details[s].min_equity_needed),
            symbol_details=details,
            next_thresholds=next_thresholds,
        )
    
    def is_symbol_enabled(self, symbol: str) -> bool:
        """Check if a symbol is currently enabled (not auto-disabled)."""
        return symbol not in self._disabled_symbols
    
    def get_disabled_symbols(self) -> List[str]:
        """Get list of auto-disabled symbols."""
        return list(self._disabled_symbols)


# ═══════════════════════════════════════════════════════════════════════════════
# Utility Functions
# ═══════════════════════════════════════════════════════════════════════════════

def estimate_equity_thresholds(
    risk_pct: float = 1.5,
    leverage: int = 5,
    typical_sl_pct: float = 1.5,
) -> Dict[str, float]:
    """
    Estimate minimum equity needed for each symbol in registry.
    Useful for documentation/UI.
    
    Returns:
        Dict of symbol -> minimum equity in USDT
    """
    thresholds = {}
    
    # Typical prices (approximate)
    typical_prices = {
        "BTCUSDT": 95000,
        "ETHUSDT": 3200,
        "SOLUSDT": 150,
        "XRPUSDT": 0.55,
        "DOGEUSDT": 0.08,
        "AVAXUSDT": 35,
        "LINKUSDT": 15,
        "MATICUSDT": 0.80,
        "ADAUSDT": 0.45,
        "DOTUSDT": 7.0,
    }
    
    for symbol, specs in CONTRACT_SPECS_REGISTRY.items():
        price = typical_prices.get(symbol, 100)
        min_qty = specs.min_qty
        
        # risk_distance = price * sl_pct / 100
        # qty = (equity * risk_pct / 100) / risk_distance
        # min_equity = (min_qty * risk_distance * 100) / risk_pct
        risk_distance = price * (typical_sl_pct / 100)
        min_equity = (min_qty * risk_distance * 100) / risk_pct
        
        thresholds[symbol] = min_equity
    
    return dict(sorted(thresholds.items(), key=lambda x: x[1]))


def get_recommended_symbols_for_equity(equity: float) -> List[str]:
    """Get recommended symbols for a given equity level."""
    thresholds = estimate_equity_thresholds()
    return [symbol for symbol, min_eq in thresholds.items() if min_eq <= equity]
