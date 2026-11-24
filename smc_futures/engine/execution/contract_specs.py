"""
Contract Specifications for Bitget USDT-FUTURES
================================================
This module provides contract constraints (minQty, minNotional, qtyStep, priceScale)
used to validate order sizes BEFORE sending to the exchange.

Usage:
    from smc_futures.engine.execution.contract_specs import ContractSpecs, get_contract_specs
    
    specs = get_contract_specs("SOLUSDT")
    qty = specs.normalize_quantity(0.0042, entry_price=143.5)  # Returns normalized qty or None if invalid
"""

import logging
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional, Dict, Any, Tuple
import math

logger = logging.getLogger(__name__)


def _get_max_risk_deviation_pct() -> float:
    """Read MAX_RISK_DEVIATION_PCT from env (default 500)."""
    return float(os.getenv("MAX_RISK_DEVIATION_PCT", "500"))


def _get_max_risk_usd_cap() -> float:
    """Read MAX_RISK_USD_CAP from env (default 5.0 USDT)."""
    return float(os.getenv("MAX_RISK_USD_CAP", "5.0"))

# ═══════════════════════════════════════════════════════════════════════════════
# Contract Specifications Data Class
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ContractSpecs:
    """
    Contract specifications for a trading pair.
    
    Attributes:
        symbol: Trading pair (e.g., "BTCUSDT")
        min_qty: Minimum quantity allowed (e.g., 0.001 for BTC)
        min_notional: Minimum notional value in USDT (e.g., 5.0)
        qty_step: Quantity step/tick size (e.g., 0.001)
        price_scale: Number of decimal places for price (e.g., 1 for BTC)
        qty_scale: Number of decimal places for quantity
        contract_size: Size of one contract (usually 1.0 for USDT-FUTURES)
    """
    symbol: str
    min_qty: float
    min_notional: float
    qty_step: float
    price_scale: int
    qty_scale: int
    contract_size: float = 1.0
    
    def normalize_quantity(
        self,
        raw_qty: float,
        entry_price: float,
        max_risk_deviation_pct: float = None
    ) -> Tuple[Optional[float], str, Dict[str, Any]]:
        # Use env-based default if not explicitly provided
        if max_risk_deviation_pct is None:
            max_risk_deviation_pct = _get_max_risk_deviation_pct()
        """
        Normalize quantity to meet exchange requirements.
        
        Args:
            raw_qty: Calculated raw quantity
            entry_price: Entry price for notional calculation
            max_risk_deviation_pct: Max % increase allowed when clamping to minQty
        
        Returns:
            Tuple of (normalized_qty or None, status, details)
            - status: "OK", "CLAMPED", "REJECTED"
            - details: Dict with diagnostic info
        """
        details = {
            "raw_qty": raw_qty,
            "entry_price": entry_price,
            "min_qty": self.min_qty,
            "min_notional": self.min_notional,
            "qty_step": self.qty_step,
        }
        
        # Step 1: Round to qty_step
        rounded_qty = self._round_to_step(raw_qty)
        details["rounded_qty"] = rounded_qty
        
        # Step 2: Check if rounded qty >= min_qty
        if rounded_qty < self.min_qty:
            # Need to clamp up to min_qty
            clamped_qty = self.min_qty
            
            # Calculate risk deviation
            if raw_qty > 0:
                deviation_pct = ((clamped_qty - raw_qty) / raw_qty) * 100
            else:
                deviation_pct = 100.0
            
            details["clamped_qty"] = clamped_qty
            details["deviation_pct"] = deviation_pct
            
            # ── AUTO-ADJUST (Option A): bump to min_qty if within risk cap ──
            # Instead of rejecting on deviation %, check if the actual USD risk
            # is acceptable. This allows small accounts to trade.
            risk_usd_cap = _get_max_risk_usd_cap()
            # Estimate new risk: clamped_qty * stop_distance (approx 1% of entry)
            estimated_stop_distance = entry_price * 0.01  # conservative 1% estimate
            new_risk_usd = clamped_qty * estimated_stop_distance
            
            if risk_usd_cap > 0 and new_risk_usd > risk_usd_cap:
                # Bumped risk exceeds absolute USD cap → reject
                min_balance_needed = (self.min_qty * abs(entry_price - (entry_price * 0.99))) / 0.01
                logger.warning(
                    f"[AUTO_SIZE_CAP] {self.symbol} | "
                    f"qty={raw_qty:.6f} < min={self.min_qty} | "
                    f"bumped_risk=${new_risk_usd:.2f} > MAX_RISK_USD_CAP=${risk_usd_cap:.2f} | "
                    f"deviation={deviation_pct:.0f}% | REJECTING"
                )
                details["rejection_reason"] = (
                    f"auto_size_risk_${new_risk_usd:.2f}_exceeds_cap_${risk_usd_cap:.2f}"
                )
                details["status"] = "RISK_CAP_EXCEEDED"
                details["suggested_min_balance"] = round(min_balance_needed, 2)
                return None, "REJECTED", details
            
            if deviation_pct > max_risk_deviation_pct and risk_usd_cap <= 0:
                # Only reject on deviation% if risk cap is disabled (0)
                min_balance_needed = (self.min_qty * abs(entry_price - (entry_price * 0.99))) / 0.01
                logger.warning(
                    f"[SKIPPED_SMALL_SIZE] {self.symbol} | "
                    f"qty={raw_qty:.6f} < min={self.min_qty} | "
                    f"deviation={deviation_pct:.0f}% > max={max_risk_deviation_pct:.0f}% | "
                    f"ACTION: Increase balance or risk%, or trade different symbol"
                )
                details["rejection_reason"] = (
                    f"risk_deviation_{deviation_pct:.1f}%_exceeds_max_{max_risk_deviation_pct}%"
                )
                details["status"] = "SKIPPED_SMALL_SIZE"
                details["suggested_min_balance"] = round(min_balance_needed, 2)
                return None, "REJECTED", details
            
            # Accept clamping — AUTO-ADJUST to min_qty
            rounded_qty = clamped_qty
            logger.info(
                f"[AUTO_SIZE] {self.symbol} | bumped qty {raw_qty:.6f} -> min_qty={clamped_qty}"
                f" | deviation={deviation_pct:.1f}% | new_risk_usd≈${new_risk_usd:.2f}"
            )
        
        # Step 3: Check notional
        notional = rounded_qty * entry_price
        details["notional"] = notional
        
        if notional < self.min_notional:
            # Notional too small - calculate minimum qty needed
            min_qty_for_notional = self.min_notional / entry_price
            min_qty_for_notional = self._round_to_step(min_qty_for_notional, round_up=True)
            
            details["min_qty_for_notional"] = min_qty_for_notional
            
            # Check if this exceeds risk tolerance
            if raw_qty > 0:
                deviation_pct = ((min_qty_for_notional - raw_qty) / raw_qty) * 100
            else:
                deviation_pct = 100.0
            
            details["notional_deviation_pct"] = deviation_pct
            
            if deviation_pct > max_risk_deviation_pct:
                logger.warning(
                    f"[QTY_REJECTED] {self.symbol} | notional=${notional:.2f} < min=${self.min_notional}"
                    f" | need qty={min_qty_for_notional:.6f} but deviation={deviation_pct:.1f}%"
                )
                details["rejection_reason"] = f"notional_${notional:.2f}_below_min_${self.min_notional}"
                return None, "REJECTED", details
            
            # Accept notional clamp
            rounded_qty = max(rounded_qty, min_qty_for_notional)
            logger.info(
                f"[QTY_CLAMPED_NOTIONAL] {self.symbol} | notional was ${notional:.2f}"
                f" | new_qty={rounded_qty:.6f} | new_notional=${rounded_qty * entry_price:.2f}"
            )
            details["clamped_for_notional"] = True
        
        # Final formatting
        final_qty = self._format_qty(rounded_qty)
        details["final_qty"] = final_qty
        
        status = "OK" if rounded_qty == self._round_to_step(raw_qty) else "CLAMPED"
        
        logger.info(
            f"[QTY_CHECK] {self.symbol} | {status} | raw={raw_qty:.6f} -> final={final_qty}"
            f" | notional=${rounded_qty * entry_price:.2f}"
        )
        
        return float(final_qty), status, details
    
    def _round_to_step(self, qty: float, round_up: bool = False) -> float:
        """Round quantity to the nearest step."""
        if self.qty_step <= 0:
            return qty
        
        # Use Decimal for precision
        d_qty = Decimal(str(qty))
        d_step = Decimal(str(self.qty_step))
        
        if round_up:
            # Round up to next step
            rounded = (d_qty / d_step).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * d_step
            if rounded < d_qty:
                rounded += d_step
        else:
            # Round down to step
            rounded = (d_qty / d_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * d_step
        
        return float(rounded)
    
    def _format_qty(self, qty: float) -> str:
        """Format quantity with correct decimal places."""
        return f"{qty:.{self.qty_scale}f}"
    
    def format_price(self, price: float) -> str:
        """Format price with correct decimal places."""
        return f"{price:.{self.price_scale}f}"
    
    def validate_qty_pre_flight(
        self,
        qty: float,
        entry_price: float
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pre-flight validation of quantity (without modifying it).
        
        Returns:
            Tuple of (is_valid, reason, details)
        """
        details = {
            "qty": qty,
            "entry_price": entry_price,
            "min_qty": self.min_qty,
            "min_notional": self.min_notional,
        }
        
        if qty < self.min_qty:
            return False, "BELOW_MIN_QTY", details
        
        notional = qty * entry_price
        details["notional"] = notional
        
        if notional < self.min_notional:
            return False, "BELOW_MIN_NOTIONAL", details
        
        # Check step alignment
        rounded = self._round_to_step(qty)
        if abs(rounded - qty) > 1e-10:
            details["rounded_qty"] = rounded
            return False, "NOT_STEP_ALIGNED", details
        
        return True, "OK", details


# ═══════════════════════════════════════════════════════════════════════════════
# Contract Specifications Registry (Static Mapping)
# ═══════════════════════════════════════════════════════════════════════════════

# Bitget USDT-FUTURES contract specifications
# Source: https://www.bitget.com/api-doc/contract/market/Get-All-Symbols-Contracts
# Updated: 2026-02

CONTRACT_SPECS_REGISTRY: Dict[str, ContractSpecs] = {
    # Major pairs
    "BTCUSDT": ContractSpecs(
        symbol="BTCUSDT",
        min_qty=0.001,
        min_notional=5.0,
        qty_step=0.001,
        price_scale=1,
        qty_scale=3,
    ),
    "ETHUSDT": ContractSpecs(
        symbol="ETHUSDT",
        min_qty=0.01,
        min_notional=5.0,
        qty_step=0.01,
        price_scale=2,
        qty_scale=2,
    ),
    "SOLUSDT": ContractSpecs(
        symbol="SOLUSDT",
        min_qty=0.1,
        min_notional=5.0,
        qty_step=0.1,
        price_scale=2,
        qty_scale=1,
    ),
    "XRPUSDT": ContractSpecs(
        symbol="XRPUSDT",
        min_qty=1.0,
        min_notional=5.0,
        qty_step=1.0,
        price_scale=4,
        qty_scale=0,
    ),
    # Additional common pairs
    "DOGEUSDT": ContractSpecs(
        symbol="DOGEUSDT",
        min_qty=10.0,
        min_notional=5.0,
        qty_step=1.0,
        price_scale=5,
        qty_scale=0,
    ),
    "AVAXUSDT": ContractSpecs(
        symbol="AVAXUSDT",
        min_qty=0.1,
        min_notional=5.0,
        qty_step=0.1,
        price_scale=2,
        qty_scale=1,
    ),
    "LINKUSDT": ContractSpecs(
        symbol="LINKUSDT",
        min_qty=0.1,
        min_notional=5.0,
        qty_step=0.1,
        price_scale=3,
        qty_scale=1,
    ),
    "MATICUSDT": ContractSpecs(
        symbol="MATICUSDT",
        min_qty=1.0,
        min_notional=5.0,
        qty_step=1.0,
        price_scale=4,
        qty_scale=0,
    ),
    "ADAUSDT": ContractSpecs(
        symbol="ADAUSDT",
        min_qty=1.0,
        min_notional=5.0,
        qty_step=1.0,
        price_scale=4,
        qty_scale=0,
    ),
    "DOTUSDT": ContractSpecs(
        symbol="DOTUSDT",
        min_qty=0.1,
        min_notional=5.0,
        qty_step=0.1,
        price_scale=3,
        qty_scale=1,
    ),
}

# Default specs for unknown symbols (conservative)
DEFAULT_CONTRACT_SPECS = ContractSpecs(
    symbol="DEFAULT",
    min_qty=0.1,
    min_notional=5.0,
    qty_step=0.01,
    price_scale=4,
    qty_scale=2,
)


def get_contract_specs(symbol: str) -> ContractSpecs:
    """
    Get contract specifications for a symbol.
    Falls back to DEFAULT_CONTRACT_SPECS if symbol not in registry.
    """
    clean_symbol = symbol.replace("/", "").replace(":", "").upper()
    
    if clean_symbol in CONTRACT_SPECS_REGISTRY:
        return CONTRACT_SPECS_REGISTRY[clean_symbol]
    
    logger.warning(
        f"[CONTRACT_SPECS] {clean_symbol} not in registry, using defaults"
        f" (min_qty={DEFAULT_CONTRACT_SPECS.min_qty}, min_notional={DEFAULT_CONTRACT_SPECS.min_notional})"
    )
    
    # Return a copy with the correct symbol
    return ContractSpecs(
        symbol=clean_symbol,
        min_qty=DEFAULT_CONTRACT_SPECS.min_qty,
        min_notional=DEFAULT_CONTRACT_SPECS.min_notional,
        qty_step=DEFAULT_CONTRACT_SPECS.qty_step,
        price_scale=DEFAULT_CONTRACT_SPECS.price_scale,
        qty_scale=DEFAULT_CONTRACT_SPECS.qty_scale,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Dynamic Contract Specs Fetcher (API-based)
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_contract_specs_from_api(
    symbol: str,
    product_type: str = "USDT-FUTURES"
) -> Optional[ContractSpecs]:
    """
    Fetch contract specifications from Bitget API.
    
    Endpoint: GET /api/v2/mix/market/contracts
    
    Returns ContractSpecs or None if fetch fails.
    """
    import httpx
    
    try:
        url = "https://api.bitget.com/api/v2/mix/market/contracts"
        params = {"productType": product_type}
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        
        data = resp.json()
        if data.get("code") != "00000":
            logger.error(f"[CONTRACT_FETCH] API error: {data.get('msg')}")
            return None
        
        contracts = data.get("data", [])
        clean_symbol = symbol.replace("/", "").replace(":", "").upper()
        
        for contract in contracts:
            if contract.get("symbol", "").upper() == clean_symbol:
                # Parse contract details
                min_trade_num = float(contract.get("minTradeNum", 0.001))
                size_multiplier = float(contract.get("sizeMultiplier", 1))
                price_place = int(contract.get("pricePlace", 4))
                volume_place = int(contract.get("volumePlace", 4))
                min_trade_usdt = float(contract.get("minTradeUSDT", 5.0))
                
                # Calculate qty_step from volumePlace
                qty_step = 10 ** (-volume_place) if volume_place > 0 else 1.0
                
                specs = ContractSpecs(
                    symbol=clean_symbol,
                    min_qty=min_trade_num,
                    min_notional=min_trade_usdt,
                    qty_step=qty_step,
                    price_scale=price_place,
                    qty_scale=volume_place,
                    contract_size=size_multiplier,
                )
                
                logger.info(
                    f"[CONTRACT_FETCH] {clean_symbol} | min_qty={min_trade_num}"
                    f" | min_notional=${min_trade_usdt} | qty_step={qty_step}"
                )
                
                # Update registry
                CONTRACT_SPECS_REGISTRY[clean_symbol] = specs
                return specs
        
        logger.warning(f"[CONTRACT_FETCH] {clean_symbol} not found in API response")
        return None
        
    except Exception as e:
        logger.error(f"[CONTRACT_FETCH] Failed for {symbol}: {e}")
        return None


async def refresh_all_contract_specs(symbols: list, product_type: str = "USDT-FUTURES") -> int:
    """
    Refresh contract specs for multiple symbols.
    Returns count of successfully updated specs.
    """
    updated = 0
    for symbol in symbols:
        specs = await fetch_contract_specs_from_api(symbol, product_type)
        if specs:
            updated += 1
    
    logger.info(f"[CONTRACT_REFRESH] Updated {updated}/{len(symbols)} contract specs")
    return updated
