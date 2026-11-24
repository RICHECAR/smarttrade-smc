"""
Trade Validator — Standalone R:R + Risk Validation
═══════════════════════════════════════════════════
Extracted from the orchestrator for clean separation of concerns.
Every decision is logged; never returns silent None.
"""

import logging
from typing import Dict, Optional

from smc_futures.engine.utils.reason_codes import ReasonCode

logger = logging.getLogger(__name__)


class SMCTradeValidator:
    """
    Validates a trade signal before execution:
      1. Risk-Reward ratio check (against MIN_RR)
      2. Position sizing with AUTO_RISK_UPSCALE for qty < minQty
      3. Structured [RISK] logging
    """

    def __init__(
        self,
        min_rr: float = 1.5,
        risk_per_trade_pct: float = 1.0,
        max_leverage: int = 10,
        account_balance: float = 0.0,
        auto_risk_upscale: bool = False,
        auto_risk_upscale_max_pct: float = 3.0,
    ):
        self.min_rr = min_rr
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_leverage = max_leverage
        self.account_balance = account_balance
        self.auto_risk_upscale = auto_risk_upscale
        self.auto_risk_upscale_max_pct = auto_risk_upscale_max_pct

    def validate(
        self,
        symbol: str,
        signal_dict: Dict,
        current_price: float,
        leverage: int = 10,
        rules: Optional[Dict] = None,
        neutral_override: bool = False,
    ) -> Dict:
        """
        Validate signal and compute position sizing.

        Returns:
            {
                "valid": bool,
                "reason": str | None,
                "quantity": float,
                "sl": float,
                "tp": float,
                "rr": float,
                "risk_amount": float,
            }
        """
        result: Dict = {
            "valid": False,
            "reason": None,
            "quantity": 0.0,
            "sl": 0.0,
            "tp": 0.0,
            "rr": 0.0,
            "risk_amount": 0.0,
        }

        entry  = signal_dict.get("entry", current_price)
        sl     = signal_dict.get("stop_loss", 0)
        tp     = signal_dict.get("take_profit", 0)
        direction = signal_dict.get("direction", "long")

        if entry <= 0 or sl <= 0 or tp <= 0:
            result["reason"] = str(ReasonCode.SIGNAL_BUILD_FAILED)
            logger.warning(
                f"[RISK] {symbol} | REJECTED"
                f" | reason={ReasonCode.SIGNAL_BUILD_FAILED}"
                f" | entry={entry} sl={sl} tp={tp}"
            )
            return result

        # FM-5: Zero balance guard
        if self.account_balance <= 0:
            result["reason"] = f"{ReasonCode.INSUFFICIENT_BALANCE} (balance={self.account_balance})"
            logger.warning(
                f"[RISK] {symbol} | REJECTED"
                f" | reason={ReasonCode.INSUFFICIENT_BALANCE}"
                f" | balance={self.account_balance}"
            )
            return result

        # ── R:R Calculation ──────────────────────
        if direction == "long":
            risk  = entry - sl
            reward = tp - entry
        else:
            risk  = sl - entry
            reward = entry - tp

        if risk <= 0:
            result["reason"] = str(ReasonCode.LOW_RR)
            logger.warning(
                f"[RISK] {symbol} | REJECTED"
                f" | reason={ReasonCode.LOW_RR}"
                f" | risk={risk:.2f} (zero/negative)"
            )
            return result

        rr = reward / risk
        required_rr = self.min_rr
        if neutral_override:
            required_rr = 3.0  # Stricter R:R for neutral bias

        # Precision tolerance
        rr_epsilon = 1e-6
        effective_rr = rr + rr_epsilon

        result["rr"] = rr
        result["sl"] = sl
        result["tp"] = tp

        if effective_rr < required_rr:
            result["reason"] = f"{ReasonCode.LOW_RR} ({rr:.4f} < {required_rr})"
            logger.warning(
                f"[RISK] {symbol} | REJECTED"
                f" | reason={ReasonCode.LOW_RR}"
                f" | rr={rr:.8f} (eff={effective_rr:.8f}) < {required_rr}"
            )
            return result

        # ── Position Sizing ──────────────────────
        effective_leverage = min(leverage, self.max_leverage)
        risk_pct = self.risk_per_trade_pct
        if neutral_override:
            risk_pct *= 0.5  # Halve risk on neutral

        risk_amount = self.account_balance * (risk_pct / 100.0)
        quantity = risk_amount / risk if risk > 0 else 0

        # Cap quantity to available leverage margin (80% utilization leaves 20% fee/buffer headroom)
        if entry > 0 and self.account_balance > 0 and effective_leverage > 0:
            max_margin_notional = self.account_balance * effective_leverage * 0.80
            max_margin_qty = max_margin_notional / entry
            if quantity > max_margin_qty:
                quantity = max_margin_qty
                risk_amount = quantity * risk

        # Apply symbol rules if provided
        if rules:
            min_qty  = rules.get("min_qty", 0.001)
            qty_step = rules.get("qty_step", 0.001)
            min_notional = rules.get("min_notional", 5.0)

            # Round to step
            if qty_step > 0:
                quantity = int(quantity / qty_step) * qty_step
            
            # ═══════════════════════════════════════════════════════════════
            # AUTO_RISK_UPSCALE: bump risk% so qty reaches min_qty
            # ═══════════════════════════════════════════════════════════════

            if quantity < min_qty:
                if self.auto_risk_upscale and risk > 0:
                    required_risk_amount = min_qty * risk
                    required_pct = (required_risk_amount / self.account_balance) * 100
                    if required_pct <= self.auto_risk_upscale_max_pct:
                        logger.info(
                            f"[RISK] {symbol} | AUTO_UPSCALE"
                            f" | qty={quantity:.6f} < min={min_qty}"
                            f" | risk_pct {risk_pct:.2f}% -> {required_pct:.2f}%"
                        )
                        risk_pct = required_pct
                        risk_amount = self.account_balance * (risk_pct / 100.0)
                        quantity = risk_amount / risk
                        if qty_step > 0:
                            quantity = int(quantity / qty_step) * qty_step
                    else:
                        result["reason"] = f"SYMBOL_INELIGIBLE (need {required_pct:.1f}% > max {self.auto_risk_upscale_max_pct}%)"
                        logger.warning(
                            f"[RISK] {symbol} | SYMBOL_INELIGIBLE"
                            f" | required_risk={required_pct:.2f}%"
                            f" | max={self.auto_risk_upscale_max_pct}%"
                        )
                        return result

            # Floor checks (after potential upscale)
            if quantity < min_qty:
                result["reason"] = f"{ReasonCode.INSUFFICIENT_BALANCE} (qty={quantity:.6f} < min={min_qty})"
                logger.warning(
                    f"[RISK] {symbol} | REJECTED"
                    f" | reason={ReasonCode.INSUFFICIENT_BALANCE}"
                    f" | qty={quantity:.6f} < min_qty={min_qty}"
                )
                return result

            notional = quantity * entry
            if notional < min_notional:
                result["reason"] = f"{ReasonCode.INSUFFICIENT_BALANCE} (notional={notional:.2f} < {min_notional})"
                logger.warning(
                    f"[RISK] {symbol} | REJECTED"
                    f" | reason={ReasonCode.INSUFFICIENT_BALANCE}"
                    f" | notional={notional:.2f}"
                )
                return result

        result["valid"] = True
        result["quantity"] = quantity
        result["risk_amount"] = risk_amount

        logger.info(
            f"[RISK] {symbol}"
            f" | qty={quantity:.6f}"
            f" | sl={sl:.2f}"
            f" | tp={tp:.2f}"
            f" | rr={rr:.2f}"
            f" | risk_usd={risk_amount:.2f}"
            f" | leverage={effective_leverage}"
        )
        return result

    def update_balance(self, new_balance: float) -> None:
        """Update account balance (e.g., after trade or periodic refresh)."""
        self.account_balance = new_balance
