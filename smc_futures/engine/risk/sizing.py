import math
from dataclasses import dataclass
from typing import Dict, Any, Tuple, Optional

@dataclass
class SymbolRules:
    qty_step: float        # pas de quantité (ex: 0.001)
    min_qty: float         # quantité minimale (ex: 0.001)
    min_notional: float    # notional minimal (ex: 5.0 USDT) - si applicable

def floor_to_step(value: float, step: float) -> float:
    """Arrondi par défaut 'exchange-friendly' : floor au pas."""
    if step <= 0:
        return value
    # Use a small epsilon to avoid floating point issues where 0.999999... gets floored down
    return math.floor((value / step) + 1e-10) * step

def calc_order_qty(
    balance_usdt: float,
    risk_pct: float,
    leverage: float,
    price: float,
    rules: SymbolRules,
    allocation_mode: str = "margin",  # "margin" = risk_pct% en marge ; "notional" = risk_pct% en notional
) -> Tuple[float, Dict[str, Any]]:
    """
    Retourne (qty_finale, debug).
    - allocation_mode="margin": margin_alloc = balance * risk_pct ; notional = margin_alloc * leverage
    - allocation_mode="notional": notional_alloc = balance * risk_pct ; margin = notional / leverage
    """
    debug: Dict[str, Any] = {}

    if balance_usdt <= 0 or price <= 0 or leverage <= 0 or risk_pct <= 0:
        return 0.0, {
            "reason": "invalid_inputs", 
            "balance": balance_usdt, 
            "price": price, 
            "leverage": leverage, 
            "risk_pct": risk_pct
        }

    if allocation_mode == "margin":
        margin_alloc = balance_usdt * (risk_pct / 100.0)
        notional = margin_alloc * leverage
    else:
        notional = balance_usdt * (risk_pct / 100.0)
        margin_alloc = notional / leverage

    raw_qty = notional / price

    debug.update({
        "balance_usdt": balance_usdt,
        "risk_pct": risk_pct,
        "leverage": leverage,
        "price": price,
        "allocation_mode": allocation_mode,
        "margin_alloc": margin_alloc,
        "notional": notional,
        "raw_qty": raw_qty,
        "rules": rules.__dict__,
    })

    # 1) Arrondi au step
    qty = floor_to_step(raw_qty, rules.qty_step)
    debug["qty_after_step_floor"] = qty

    # 2) Check minQty
    if qty < rules.min_qty:
        debug["reason"] = "qty_below_min_qty_after_rounding"
        return 0.0, debug

    # 3) Check minNotional
    notional_after = qty * price
    debug["notional_after_rounding"] = notional_after

    if rules.min_notional > 0 and notional_after < rules.min_notional:
        # Tentative d'ajustement vers le haut si la marge le permet
        required_notional = rules.min_notional * 1.01 # +1% buffer
        new_raw_qty = required_notional / price
        new_qty = floor_to_step(new_raw_qty, rules.qty_step)
        
        # Si on est toujours en dessous après floor, on prend le step au dessus
        if new_qty * price < rules.min_notional:
            new_qty += rules.qty_step
            
        new_notional = new_qty * price
        new_margin = new_notional / leverage
        
        # On vérifie si on ne dépasse pas trop le budget initial (ex: +20% flex)
        if new_margin <= margin_alloc * 1.2 or new_margin <= balance_usdt * 0.5:
             qty = new_qty
             debug["qty_upgraded_to_min_notional"] = qty
             debug["notional_after_upgrade"] = new_notional
        else:
            debug["reason"] = "notional_below_min_notional_after_rounding"
            return 0.0, debug

    debug["reason"] = "ok"
    return qty, debug
