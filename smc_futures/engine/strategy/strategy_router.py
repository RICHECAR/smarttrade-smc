"""Strategy Router - Multi-Strategy Selection
=============================================
Selects the primary strategy based on session time, then evaluates
overlay strategies (funding reversion, VWAP reversion, sweep scalping)
based on market context (regime, funding rate, VWAP distance).

Routing priority:
  1. Extreme funding rate → FundingReversionStrategy (any session)
  2. Session-based primary: Asia/Weekend → AsiaWeekendStrategy
     London/NY → DefaultSMCStrategy
  3. Overlay candidates evaluated in parallel:
     - RANGE/LOW_VOL regime → VWAPReversionStrategy
     - Liquidity sweep detected → LiquiditySweepScalpStrategy
"""

import logging
from datetime import datetime, time
from enum import Enum
from typing import List, Optional, Tuple
from dataclasses import dataclass, field

# Python 3.9+ has zoneinfo built-in
import sys
if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    # Fallback for older Python versions (requires: pip install backports.zoneinfo)
    from backports.zoneinfo import ZoneInfo  # type: ignore

logger = logging.getLogger(__name__)


class StrategyType(Enum):
    """Available strategy types."""
    DEFAULT_SMC = "DefaultSMCStrategy"
    ASIA_WEEKEND = "AsiaWeekendStrategy"
    FUNDING_REVERSION = "FundingReversionStrategy"
    VWAP_REVERSION = "VWAPReversionStrategy"
    LIQUIDITY_SWEEP_SCALP = "LiquiditySweepScalpStrategy"
    LIQUIDATION_CASCADE = "LiquidationCascadeStrategy"
    ORDERBOOK_IMBALANCE = "OrderBookImbalanceStrategy"


class SessionType(Enum):
    """Trading session types."""
    ASIA = "asia"
    LONDON = "london"
    NEW_YORK = "ny"
    LONDON_NY_OVERLAP = "london_ny_overlap"
    WEEKEND = "weekend"
    OFF = "off"


@dataclass
class RouterDecision:
    """Result of strategy routing decision."""
    strategy: StrategyType
    session: SessionType
    reason: str
    local_time: str
    timezone: str
    hour: int
    is_weekend: bool
    overlay_strategies: List[StrategyType] = field(default_factory=list)


class StrategyRouter:
    """
    Routes to appropriate strategy based on Europe/Paris local time.
    
    Session Windows (Europe/Paris):
        - Asia:      01:00 - 09:00 (Tokyo open to European pre-market)
        - London:    09:00 - 14:00 (London open, pre-NY)
        - Overlap:   14:00 - 17:30 (London + NY highest volume)
        - NY:        17:30 - 22:00 (NY afternoon)
        - Off:       22:00 - 01:00 (low volume transition)
        - Weekend:   Saturday/Sunday (all day)
    
    Strategy Mapping:
        - Asia/Weekend/Off → AsiaWeekendStrategy (range mean reversion)
        - London/NY/Overlap → DefaultSMCStrategy (trend following SMC)
    """
    
    # Session windows (hour, minute) in Europe/Paris
    SESSION_WINDOWS = {
        SessionType.ASIA:       (time(1, 0), time(9, 0)),
        SessionType.LONDON:     (time(9, 0), time(14, 0)),
        SessionType.LONDON_NY_OVERLAP: (time(14, 0), time(17, 30)),
        SessionType.NEW_YORK:   (time(17, 30), time(22, 0)),
        SessionType.OFF:        (time(22, 0), time(1, 0)),  # Wraps around midnight
    }
    
    # Map sessions to strategies
    SESSION_STRATEGY_MAP = {
        SessionType.ASIA:       StrategyType.ASIA_WEEKEND,
        SessionType.LONDON:     StrategyType.DEFAULT_SMC,
        SessionType.LONDON_NY_OVERLAP: StrategyType.DEFAULT_SMC,
        SessionType.NEW_YORK:   StrategyType.DEFAULT_SMC,
        SessionType.OFF:        StrategyType.ASIA_WEEKEND,
        SessionType.WEEKEND:    StrategyType.ASIA_WEEKEND,
    }
    
    def __init__(
        self,
        timezone: str = "Europe/Paris",
        asia_start: str = "01:00",
        asia_end: str = "09:00",
        enable_weekend_strategy: bool = True,
        enable_asia_strategy: bool = True,
        enable_funding_strategy: bool = True,
        enable_vwap_strategy: bool = True,
        enable_sweep_scalp_strategy: bool = True,
        enable_liquidation_cascade: bool = True,
        enable_orderbook_imbalance: bool = True,
        funding_threshold: float = 0.0004,
        liq_spike_usd: float = 5_000_000,
        ob_imbalance_long: float = 0.65,
        ob_imbalance_short: float = 0.35,
    ):
        self.tz = ZoneInfo(timezone)
        self.timezone_name = timezone
        self.enable_weekend_strategy = enable_weekend_strategy
        self.enable_asia_strategy = enable_asia_strategy
        self.enable_funding_strategy = enable_funding_strategy
        self.enable_vwap_strategy = enable_vwap_strategy
        self.enable_sweep_scalp_strategy = enable_sweep_scalp_strategy
        self.enable_liquidation_cascade = enable_liquidation_cascade
        self.enable_orderbook_imbalance = enable_orderbook_imbalance
        self.funding_threshold = funding_threshold
        self.liq_spike_usd = liq_spike_usd
        self.ob_imbalance_long = ob_imbalance_long
        self.ob_imbalance_short = ob_imbalance_short
        
        # Parse custom Asia session times
        self.asia_start = self._parse_time(asia_start)
        self.asia_end = self._parse_time(asia_end)
        
        # Update session windows with custom Asia times
        self.SESSION_WINDOWS[SessionType.ASIA] = (self.asia_start, self.asia_end)
        
        logger.info(
            f"[STRATEGY_ROUTER] Initialized | tz={timezone}"
            f" | asia={asia_start}-{asia_end}"
            f" | weekend_enabled={enable_weekend_strategy}"
            f" | asia_enabled={enable_asia_strategy}"
        )
    
    def _parse_time(self, time_str: str) -> time:
        """Parse HH:MM string to time object."""
        parts = time_str.split(":")
        return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    
    def _is_time_in_range(self, t: time, start: time, end: time) -> bool:
        """Check if time is within range, handling midnight wraparound."""
        if start <= end:
            return start <= t < end
        else:
            # Range wraps around midnight (e.g., 22:00 - 01:00)
            return t >= start or t < end
    
    def get_current_session(self, timestamp_ms: Optional[int] = None) -> Tuple[SessionType, datetime]:
        """
        Determine current session based on Paris time.
        
        Args:
            timestamp_ms: Optional timestamp in milliseconds (uses current time if None)
        
        Returns:
            Tuple of (SessionType, datetime in Paris timezone)
        """
        if timestamp_ms:
            dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.tz)
        else:
            dt = datetime.now(self.tz)
        
        # Check weekend first
        if dt.weekday() >= 5:  # Saturday = 5, Sunday = 6
            return SessionType.WEEKEND, dt
        
        current_time = dt.time()
        
        # Check each session window
        for session, (start, end) in self.SESSION_WINDOWS.items():
            if self._is_time_in_range(current_time, start, end):
                return session, dt
        
        # Default to OFF if no match
        return SessionType.OFF, dt
    
    def route(self, timestamp_ms: Optional[int] = None, symbol: str = "") -> RouterDecision:
        """
        Route to appropriate strategy based on current time.
        
        Args:
            timestamp_ms: Optional timestamp in milliseconds
            symbol: Symbol being traded (for logging)
        
        Returns:
            RouterDecision with selected strategy and metadata
        """
        session, dt = self.get_current_session(timestamp_ms)
        is_weekend = session == SessionType.WEEKEND
        
        # Determine strategy
        if is_weekend and not self.enable_weekend_strategy:
            # Weekend strategy disabled, use default but mark as weekend
            strategy = StrategyType.DEFAULT_SMC
            reason = "weekend_strategy_disabled"
        elif session == SessionType.ASIA and not self.enable_asia_strategy:
            # Asia strategy disabled, use default
            strategy = StrategyType.DEFAULT_SMC
            reason = "asia_strategy_disabled"
        else:
            strategy = self.SESSION_STRATEGY_MAP.get(session, StrategyType.DEFAULT_SMC)
            reason = f"{session.value}_session"
        
        decision = RouterDecision(
            strategy=strategy,
            session=session,
            reason=reason,
            local_time=dt.strftime("%Y-%m-%d %H:%M:%S"),
            timezone=self.timezone_name,
            hour=dt.hour,
            is_weekend=is_weekend,
        )
        
        logger.info(
            f"[STRATEGY_ROUTER] {symbol if symbol else 'GLOBAL'}"
            f" | selected={strategy.value}"
            f" | reason={reason}"
            f" | session={session.value}"
            f" | local_time={decision.local_time}"
            f" | tz={self.timezone_name}"
        )
        
        return decision
    
    def should_use_asia_weekend_strategy(self, timestamp_ms: Optional[int] = None) -> bool:
        """
        Quick check if AsiaWeekendStrategy should be used.
        
        Args:
            timestamp_ms: Optional timestamp in milliseconds
        
        Returns:
            True if AsiaWeekendStrategy should be used
        """
        decision = self.route(timestamp_ms)
        return decision.strategy == StrategyType.ASIA_WEEKEND
    
    def get_session_info(self, timestamp_ms: Optional[int] = None) -> dict:
        """
        Get detailed session info for API/logging.
        
        Returns dict with:
            - session: Current session name
            - strategy: Selected strategy name
            - local_time: Local time string
            - timezone: Timezone name
            - is_weekend: Boolean
            - next_session: Next session name and time
        """
        decision = self.route(timestamp_ms)
        
        return {
            "session": decision.session.value,
            "strategy": decision.strategy.value,
            "local_time": decision.local_time,
            "timezone": decision.timezone,
            "hour": decision.hour,
            "is_weekend": decision.is_weekend,
            "reason": decision.reason,
        }

    def route_with_context(
        self,
        timestamp_ms: Optional[int] = None,
        symbol: str = "",
        regime: str = "RANGE",
        funding_rate: float = 0.0,
    ) -> RouterDecision:
        """
        Enhanced routing that considers market context in addition to session.

        Priority:
          1. Extreme funding → FUNDING_REVERSION (overrides session primary)
          2. Session-based primary (unchanged)
          3. Overlay strategies appended based on regime

        Args:
            timestamp_ms: Candle timestamp
            symbol: Trading pair
            regime: Current MarketRegime value (TREND/RANGE/HIGH_VOL/LOW_VOL)
            funding_rate: Current funding rate (e.g. 0.0005 = 0.05%)

        Returns:
            RouterDecision with primary strategy + overlay_strategies list
        """
        decision = self.route(timestamp_ms, symbol)
        overlays: List[StrategyType] = []

        # 1. Funding rate override — extreme funding takes priority
        if (
            self.enable_funding_strategy
            and abs(funding_rate) >= self.funding_threshold
        ):
            decision.strategy = StrategyType.FUNDING_REVERSION
            decision.reason = f"extreme_funding({funding_rate:.6f})"
            logger.info(
                f"[STRATEGY_ROUTER] {symbol} | FUNDING_OVERRIDE"
                f" | funding={funding_rate:.6f}"
            )

        # 2. VWAP overlay in RANGE / LOW_VOL
        if (
            self.enable_vwap_strategy
            and regime in ("RANGE", "LOW_VOL")
            and decision.strategy != StrategyType.FUNDING_REVERSION
        ):
            overlays.append(StrategyType.VWAP_REVERSION)

        # 3. Sweep scalp overlay (any non-HIGH_VOL regime)
        if self.enable_sweep_scalp_strategy and regime != "HIGH_VOL":
            overlays.append(StrategyType.LIQUIDITY_SWEEP_SCALP)

        # 4. Liquidation cascade overlay — always eligible (event-driven)
        if self.enable_liquidation_cascade:
            overlays.append(StrategyType.LIQUIDATION_CASCADE)

        # 5. Order book imbalance overlay — any regime
        if self.enable_orderbook_imbalance:
            overlays.append(StrategyType.ORDERBOOK_IMBALANCE)

        decision.overlay_strategies = overlays
        return decision
