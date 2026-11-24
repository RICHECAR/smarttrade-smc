"""
Risk Management Engine for Futures Trading
Handles leverage, liquidation distance, position sizing, margin mode
"""

from typing import Dict, Optional
from dataclasses import dataclass
from enum import Enum

class MarginMode(Enum):
    ISOLATED = "isolated"
    CROSS = "cross"

class PositionMode(Enum):
    ONE_WAY = "one_way"
    HEDGE = "hedge"

@dataclass
class PositionSize:
    """Calculated position size for trade"""
    quantity: float
    notional_usd: float
    leverage: float
    margin_required: float
    liquidation_distance: float
    liquidation_percent: float
    is_safe: bool
    warning: Optional[str] = None

class FuturesRiskEngine:
    """
    Risk management for USDT Perpetual Futures
    - Leverage-aware sizing
    - Liquidation protection
    - Margin calculations
    - Dynamic leverage adjustment
    """
    
    def __init__(
        self,
        account_balance: float,
        risk_per_trade_percent: float = 1.0,
        max_leverage: float = 10.0,
        liquidation_buffer: float = 0.10  # 10% minimum distance to liquidation
    ):
        self.account_balance = account_balance
        self.risk_per_trade_percent = risk_per_trade_percent
        self.max_leverage = max_leverage
        self.liquidation_buffer = liquidation_buffer
        
        self.active_positions: Dict[str, Dict] = {}
        self.total_margin_used = 0.0
        
    def calculate_position_size(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        leverage: float,
        min_notional: float = 10.0
    ) -> PositionSize:
        """
        Calculate safe position size for Futures trade
        
        Returns:
            PositionSize with quantity, checks liquidation distance
        """
        
        # Risk per trade in USD
        risk_usd = self.account_balance * (self.risk_per_trade_percent / 100)
        
        # Distance to stop loss
        risk_distance = abs(entry_price - stop_loss)
        
        if risk_distance == 0:
            return PositionSize(
                quantity=0,
                notional_usd=0,
                leverage=leverage,
                margin_required=0,
                liquidation_distance=0,
                liquidation_percent=0,
                is_safe=False,
                warning="Invalid stop loss (0 distance)"
            )
        
        # Quantity needed = risk_usd / risk_distance_per_contract
        quantity = risk_usd / risk_distance
        
        # Notional value
        notional_usd = quantity * entry_price
        
        # Margin required = notional / leverage
        margin_required = notional_usd / leverage
        
        # Check if account has enough margin
        available_margin = self.account_balance - self.total_margin_used
        
        if margin_required > available_margin:
            # Reduce quantity
            quantity = (available_margin * 0.9) * leverage / entry_price
            notional_usd = quantity * entry_price
            margin_required = notional_usd / leverage
        
        # Calculate liquidation distance
        liquidation_price = self._calculate_liquidation_price(
            entry_price=entry_price,
            leverage=leverage,
            side="long" if entry_price > stop_loss else "short"
        )
        
        liquidation_distance = abs(entry_price - liquidation_price)
        liquidation_percent = (liquidation_distance / entry_price) * 100
        
        # Check if liquidation is too close to SL
        sl_distance = abs(entry_price - stop_loss)
        buffer_check = liquidation_distance > (sl_distance * (1 + self.liquidation_buffer))
        
        # Minimum notional check (exchange requirement)
        notional_valid = notional_usd >= min_notional
        
        is_safe = buffer_check and notional_valid and margin_required <= available_margin
        
        warning = None
        if not buffer_check:
            warning = f"Liquidation too close to SL ({liquidation_percent:.1f}% vs required {self.liquidation_buffer*100:.1f}%)"
        elif not notional_valid:
            warning = f"Notional ${notional_usd:.2f} below minimum ${min_notional}"
        elif not (margin_required <= available_margin):
            warning = f"Insufficient margin: need ${margin_required:.2f}, have ${available_margin:.2f}"
        
        return PositionSize(
            quantity=quantity,
            notional_usd=notional_usd,
            leverage=leverage,
            margin_required=margin_required,
            liquidation_distance=liquidation_distance,
            liquidation_percent=liquidation_percent,
            is_safe=is_safe,
            warning=warning
        )
    
    def _calculate_liquidation_price(
        self,
        entry_price: float,
        leverage: float,
        side: str = "long"
    ) -> float:
        """
        Simplified liquidation price calculation
        Actual formula depends on funding rates and maintenance margin
        """
        
        # Typical maintenance margin = 0.5% - 1%
        maintenance_margin = 0.01
        
        if side == "long":
            liquidation_price = entry_price / (1 + maintenance_margin - (1/leverage))
        else:  # short
            liquidation_price = entry_price / (1 - maintenance_margin + (1/leverage))
        
        return liquidation_price
    
    def check_funding_rate_filter(
        self,
        current_funding_rate: float,
        max_acceptable_rate: float = 0.005  # 0.5%
    ) -> bool:
        """
        Don't trade if funding rate is extreme
        Protects against large funding rate payments
        """
        return abs(current_funding_rate) <= max_acceptable_rate
    
    def check_volatility_filter(
        self,
        atr_percent: float,  # ATR as % of price
        max_volatility_percent: float = 2.0
    ) -> bool:
        """
        Don't trade if volatility is too high
        """
        return atr_percent <= max_volatility_percent
    
    def add_position(self, symbol: str, position_data: Dict) -> None:
        """Track active position"""
        self.active_positions[symbol] = position_data
        self.total_margin_used += position_data.get("margin_required", 0)
    
    def close_position(self, symbol: str) -> None:
        """Remove position from tracking"""
        if symbol in self.active_positions:
            margin = self.active_positions[symbol].get("margin_required", 0)
            self.total_margin_used -= margin
            del self.active_positions[symbol]
    
    def get_account_state(self) -> Dict:
        """Export current account/risk state"""
        margin_percent = (self.total_margin_used / self.account_balance * 100) if self.account_balance else 0
        
        return {
            "balance": self.account_balance,
            "margin_used": self.total_margin_used,
            "margin_percent": margin_percent,
            "margin_available": self.account_balance - self.total_margin_used,
            "active_positions": len(self.active_positions),
            "risk_per_trade": self.risk_per_trade_percent,
            "max_leverage": self.max_leverage,
        }

class TradeValidator:
    """
    Pre-execution validation for SMC trades
    Checks all conditions before sending order
    """
    
    def __init__(self, risk_engine: FuturesRiskEngine):
        self.risk_engine = risk_engine
        
    def validate_signal(
        self,
        signal: Dict,
        current_price: float,
        symbol: str,
        leverage: float,
        funding_rate: float = 0.0,
        atr_percent: float = 0.0
    ) -> Dict:
        """
        Validate trade signal before execution
        
        Returns validation result with pass/fail and warnings
        """
        
        result = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "position_size": None
        }
        
        # Check 1: Basic signal validity
        if not signal:
            result["valid"] = False
            result["errors"].append("No signal provided")
            return result
        
        entry = signal.get("entry")
        stop_loss = signal.get("stop_loss")
        rr_ratio = signal.get("rr_ratio", 0)
        confidence = signal.get("confidence", 0)
        
        # Check 2: Signal quality
        if confidence < 0.5:
            result["warnings"].append(f"Low confidence signal: {confidence:.2f}")
        
        if rr_ratio < 1.5:
            result["valid"] = False
            result["errors"].append(f"RR ratio too low: {rr_ratio:.2f} (min 1.5)")
        
        # Check 3: Position sizing
        pos_size = self.risk_engine.calculate_position_size(
            symbol=symbol,
            entry_price=entry,
            stop_loss=stop_loss,
            leverage=leverage
        )
        
        result["position_size"] = {
            "quantity": pos_size.quantity,
            "notional": pos_size.notional_usd,
            "margin_required": pos_size.margin_required,
            "liquidation_distance_percent": pos_size.liquidation_percent
        }
        
        if not pos_size.is_safe:
            result["valid"] = False
            if pos_size.warning:
                result["errors"].append(pos_size.warning)
        
        # Check 4: Funding rate
        if not self.risk_engine.check_funding_rate_filter(funding_rate):
            result["warnings"].append(f"High funding rate: {funding_rate*100:.2f}%")
        
        # Check 5: Volatility
        if not self.risk_engine.check_volatility_filter(atr_percent):
            result["warnings"].append(f"High volatility: {atr_percent:.2f}%")
        
        # Check 6: Price didn't gap past entry
        if signal.get("direction") == "long" and current_price > entry * 1.01:
            result["warnings"].append("Entry price already exceeded (potential slippage)")
        elif signal.get("direction") == "short" and current_price < entry * 0.99:
            result["warnings"].append("Entry price already exceeded (potential slippage)")
        
        return result
