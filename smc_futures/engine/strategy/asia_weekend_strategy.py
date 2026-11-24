"""
Asia/Weekend Strategy — Range Mean Reversion for Low-Volume Sessions
════════════════════════════════════════════════════════════════════
Specialized strategy for:
    - Asia session (01:00 - 09:00 Europe/Paris)
    - Weekend trading (Saturday/Sunday)

Uses range-bound mean reversion approach:
    1. Detect consolidation range on HTF/MTF
    2. Wait for sweep of range boundary
    3. Confirm reversal with LTF structure (CHoCH/BOS)
    4. Enter on OB/FVG in reversal direction
    5. Target mid-range or opposite boundary

More conservative than London/NY SMC strategy due to lower liquidity.
"""

import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from enum import Enum

from smc_futures.engine.strategy.signal_builder import SMCSignal, SignalResult, SignalDirection
from smc_futures.engine.strategy.range_detector import RangeDetector, RangeState, RangeSweep
from smc_futures.risk.adaptive_thresholds import AdaptiveThresholdManager
from smc_futures.smc_config import SMC_CONFIG
from smc_futures.engine.ab_allocator import select_engine
from datetime import datetime

logger = logging.getLogger(__name__)


class AsiaRejectReason(Enum):
    """Rejection reasons specific to Asia/Weekend strategy."""
    NO_RANGE = "NO_VALID_RANGE"
    RANGE_TOO_SMALL = "RANGE_TOO_SMALL"
    RANGE_TOO_LARGE = "RANGE_TOO_LARGE"
    NO_SWEEP = "NO_RANGE_SWEEP"
    SWEEP_NOT_CONFIRMED = "SWEEP_NOT_CONFIRMED"
    NO_LTF_REVERSAL = "NO_LTF_REVERSAL"
    NO_ENTRY_ZONE = "NO_ENTRY_ZONE"
    HTF_TREND_TOO_STRONG = "HTF_TREND_TOO_STRONG"
    RR_TOO_LOW = "RR_TOO_LOW"
    WEEKEND_DISABLED = "WEEKEND_DISABLED"
    # Soft mode specific
    SOFT_MODE_DISABLED = "SOFT_MODE_DISABLED"
    SOFT_NO_OB_FVG_ALIGNED = "SOFT_NO_OB_FVG_ALIGNED"
    SOFT_SL_TOO_TIGHT = "SOFT_SL_TOO_TIGHT"
    SOFT_VOLATILITY_TOO_HIGH = "SOFT_VOLATILITY_TOO_HIGH"
    SOFT_MAX_TRADES_REACHED = "SOFT_MAX_TRADES_REACHED"


@dataclass
class AsiaWeekendSignalResult(SignalResult):
    """Extended signal result with Asia-specific metadata."""
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    range_mid: Optional[float] = None
    sweep_side: Optional[str] = None
    trend_strength: Optional[float] = None
    entry_mode: Optional[str] = None  # "sweep", "no_sweep_bypass", "soft_mean_reversion"
    strategy_name: str = "AsiaWeekendStrategy"


class AsiaWeekendStrategy:
    """
    Mean reversion strategy for low-volume sessions.
    
    Logic:
        1. Detect HTF/MTF range (consolidation zone)
        2. Wait for sweep of range high/low (liquidity grab)
        3. Confirm with LTF reversal signal (CHoCH or BOS against sweep)
        4. Enter on OB/FVG in reversal direction
        5. Conservative targets: mid-range (TP1), opposite boundary (TP2)
    
    Filters:
        - Range too small → Skip (no room for trade)
        - Range too large → Skip (trending market, not ranging)
        - HTF trend too strong → Skip (use default SMC instead)
        - No sweep confirmation → Skip (need rejection candle)
    """
    
    def __init__(
        self,
        # Target R:R settings (more conservative)
        min_rr: float = 1.2,
        max_rr: float = 2.0,
        target_mid_range: bool = True,  # TP at mid-range
        
        # Range detection
        range_lookback: int = 20,
        min_range_atr: float = 0.5,
        max_range_atr: float = 3.0,
        
        # Trend filter
        htf_trend_threshold: float = 0.7,
        
        # Entry requirements
        require_sweep_confirmation: bool = True,
        require_ltf_structure: bool = True,
        require_entry_zone: bool = False,  # Fallback to RANGE_BOUNDARY entry
        
        # Risk modifiers
        risk_factor: float = 0.75,  # Reduced risk for low liquidity
    ):
        self.min_rr = min_rr
        self.max_rr = max_rr
        self.target_mid_range = target_mid_range
        
        self.htf_trend_threshold = htf_trend_threshold
        self.require_sweep_confirmation = require_sweep_confirmation
        self.require_ltf_structure = require_ltf_structure
        self.require_entry_zone = require_entry_zone
        self.risk_factor = risk_factor
        
        # Range detector
        self.range_detector = RangeDetector(
            lookback_bars=range_lookback,
            min_range_atr=min_range_atr,
            max_range_atr=max_range_atr,
            htf_trend_threshold=htf_trend_threshold,
        )
        
        # Phase 7: Adaptive Thresholds
        self.adaptive_manager = AdaptiveThresholdManager()
        self.atr_history = []  # To track avg_atr
        self.max_atr_history = int(SMC_CONFIG.get("ATR_MEAN_WINDOW", 100))

        # Phase 8: A-B Testing
        from backend.services.ab_testing_service import ABTestingService
        from pathlib import Path
        _root = Path(__file__).resolve().parent.parent.parent.parent
        self.ab_test_service = ABTestingService(_root / "reports" / "ab_tests")
        
        logger.info(
            f"[ASIA_WEEKEND_STRATEGY] Initialized"
            f" | min_rr={min_rr} | max_rr={max_rr}"
            f" | trend_threshold={htf_trend_threshold}"
            f" | risk_factor={risk_factor}"
        )
    
    # ------------------------------------------------------------------ #
    #               Asset-Specific Threshold Configuration                #
    # ------------------------------------------------------------------ #
    
    def _get_asset_class(self, symbol: str) -> str:
        """Determine asset class from symbol."""
        sym_upper = symbol.upper().replace("USDT", "").replace("_UMCBL", "")
        if sym_upper in ("BTC", "BTCUSDT"):
            return "BTC"
        elif sym_upper in ("ETH", "ETHUSDT"):
            return "ETH"
        else:
            return "ALT"
    
    def _configure_threshold_for_symbol(
        self,
        symbol: str,
        is_weekend: bool = False,
        session_name: str = "asia",
        current_atr: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Calculate thresholds for both Static (A) and Adaptive (B) engines.
        
        Returns:
            Dict containing: 
            - 'static': float
            - 'adaptive': Optional[float]
            - 'source_a': str
            - 'source_b': Optional[str]
        """
        from smc_futures.smc_config import SMC_CONFIG
        
        # 1. Calculate Static Threshold (A)
        ASIA_MAX_RANGE_RATIO_BTC = SMC_CONFIG.get("ASIA_MAX_RANGE_RATIO_BTC", 6.0)
        ASIA_MAX_RANGE_RATIO_ETH = SMC_CONFIG.get("ASIA_MAX_RANGE_RATIO_ETH", 5.0)
        ASIA_MAX_RANGE_RATIO_ALT = SMC_CONFIG.get("ASIA_MAX_RANGE_RATIO_ALT", 4.0)
        ASIA_WEEKEND_RELAX_RANGE = SMC_CONFIG.get("ASIA_WEEKEND_RELAX_RANGE", 1.3)
        ASIA_WEEKEND_RELAX_RANGE_ALT = SMC_CONFIG.get("ASIA_WEEKEND_RELAX_RANGE_ALT", 1.6)
        
        asset_class = self._get_asset_class(symbol)
        
        if asset_class == "BTC":
            base_threshold = ASIA_MAX_RANGE_RATIO_BTC
        elif asset_class == "ETH":
            base_threshold = ASIA_MAX_RANGE_RATIO_ETH
        else:
            base_threshold = ASIA_MAX_RANGE_RATIO_ALT
            
        source_a = f"{asset_class}_PROFILE"
        
        if is_weekend:
            if asset_class == "ALT":
                static_val = base_threshold * ASIA_WEEKEND_RELAX_RANGE_ALT
                source_a = "ALT_WEEKEND_RELAX"
            else:
                static_val = base_threshold * ASIA_WEEKEND_RELAX_RANGE
                source_a = f"{asset_class}_WEEKEND"
        else:
            static_val = base_threshold

        # 2. Calculate Adaptive Threshold (B)
        adaptive_val = None
        source_b = None
        
        if current_atr > 0 and self.atr_history:
            avg_atr = sum(self.atr_history) / len(self.atr_history)
            adaptive_val = self.adaptive_manager.get_threshold(
                symbol=symbol,
                session_str=session_name,
                current_atr=current_atr,
                avg_atr=avg_atr,
                recent_winrate=None
            )
            source_b = f"ADAPTIVE_{session_name.upper()}"

        return {
            "static": static_val,
            "adaptive": adaptive_val,
            "source_a": source_a,
            "source_b": source_b,
            "asset_class": asset_class
        }
    
    def _try_trending_range_mode(
        self,
        symbol: str,
        range_state: RangeState,
        threshold_info: dict,
        structure_state: Dict[str, Any],
    ) -> bool:
        """
        Try trending range mode when range is large but structure allows.
        
        Trending range mode allows larger ranges IF:
            - ASIA_TRENDING_RANGE_ALLOWED is enabled
            - Range ratio is within allowed trending range bounds
            - Structure shows clear direction (not choppy)
        
        Returns:
            True if trending range mode is allowed, False otherwise
        """
        from smc_futures.smc_config import SMC_CONFIG
        
        # Get trending range settings with safe defaults
        ASIA_TRENDING_RANGE_ALLOWED = SMC_CONFIG.get("ASIA_TRENDING_RANGE_ALLOWED", True)
        ASIA_TRENDING_RANGE_MIN_RR = SMC_CONFIG.get("ASIA_TRENDING_RANGE_MIN_RR", 1.0)
        ASIA_TRENDING_RANGE_MAX_RR = SMC_CONFIG.get("ASIA_TRENDING_RANGE_MAX_RR", 1.5)
        ASIA_TRENDING_RANGE_RISK_MULT = SMC_CONFIG.get("ASIA_TRENDING_RANGE_RISK_MULT", 0.75)
        
        if not ASIA_TRENDING_RANGE_ALLOWED:
            return False
        
        # Check if it's a RANGE_TOO_LARGE rejection specifically
        if range_state.current_range and range_state.current_range.invalidation_reason:
            if "RANGE_TOO_LARGE" not in range_state.current_range.invalidation_reason:
                return False
        else:
            return False
        
        ratio = threshold_info.get("last_ratio", 0)
        threshold = threshold_info.get("max_range_atr", 3.0)
        
        # Allow trending range if within extended bounds
        # E.g., threshold=6 allows up to 6, trending mode allows 6-9 (1.5x)
        trending_max = threshold * 1.5  # 50% extension for trending ranges
        
        if ratio > trending_max:
            logger.debug(
                f"[ASIA_STRAT] {symbol} | TRENDING_RANGE_DENIED"
                f" | ratio={ratio:.2f}"
                f" | trending_max={trending_max:.1f}"
            )
            return False
        
        # Check structure state for clean direction
        htf_bias = structure_state.get("htf_bias", "neutral")
        structure_quality = structure_state.get("structure_quality", 0.0)
        
        # Require clear direction for trending range
        if htf_bias == "neutral" or structure_quality < 0.5:
            logger.debug(
                f"[ASIA_STRAT] {symbol} | TRENDING_RANGE_DENIED"
                f" | reason=UNCLEAR_STRUCTURE"
                f" | htf_bias={htf_bias}"
                f" | quality={structure_quality:.2f}"
            )
            return False
        
        # Temporarily modify risk settings for trending range
        self._trending_range_active = True
        self._original_min_rr = self.min_rr
        self._original_max_rr = self.max_rr
        self._original_risk_factor = self.risk_factor
        
        self.min_rr = ASIA_TRENDING_RANGE_MIN_RR
        self.max_rr = ASIA_TRENDING_RANGE_MAX_RR
        self.risk_factor = self.risk_factor * ASIA_TRENDING_RANGE_RISK_MULT
        
        logger.info(
            f"[ASIA_STRAT] {symbol} | TRENDING_RANGE_ALLOWED"
            f" | ratio={ratio:.2f}"
            f" | threshold={threshold:.1f}"
            f" | htf_bias={htf_bias}"
            f" | new_risk_factor={self.risk_factor:.2f}"
        )
        
        return True
    
    def _try_range_too_large_bypass(
        self,
        symbol: str,
        range_state: RangeState,
        threshold_info: dict,
        structure_state: Dict[str, Any],
        ob_state: Dict[str, Any],
        fvg_state: Dict[str, Any],
        current_price: float,
    ) -> bool:
        """
        Attempt to bypass RANGE_TOO_LARGE if safe conditions are met.
        
        Requirements:
            - ASIA_ALLOW_RANGE_TOO_LARGE is enabled
            - Clear direction (bias is NOT neutral)
            - Fresh zone exists at range boundary
        """
        from smc_futures.smc_config import SMC_CONFIG
        
        if not SMC_CONFIG.get("ASIA_ALLOW_RANGE_TOO_LARGE", False):
            return False
            
        # 1. Check if it's RANGE_TOO_LARGE
        if range_state.current_range and range_state.current_range.invalidation_reason:
            if "RANGE_TOO_LARGE" not in range_state.current_range.invalidation_reason:
                return False
        else:
            return False
            
        # 2. Check bias (must be clear for safe bypass)
        htf_bias = structure_state.get("htf_bias", "neutral")
        if htf_bias == "neutral":
            return False
            
        # 3. Check for zone at boundary
        range_obj = range_state.current_range
        boundary_tol = SMC_CONFIG.get("ASIA_BOUNDARY_TOL_PCT", 0.01)
        near_high = current_price >= range_obj.high * (1 - boundary_tol)
        near_low = current_price <= range_obj.low * (1 + boundary_tol)
        
        if not near_high and not near_low:
            return False
            
        # 4. Determine direction
        direction = SignalDirection.SHORT if near_high else SignalDirection.LONG
        
        # 5. Find entry zone
        entry = self._find_entry_zone(ob_state, fvg_state, direction, current_price, range_obj)
        if not entry:
            return False
            
        logger.info(
            f"[ASIA_STRAT] {symbol} | RANGE_BYPASS_TRIGGERED"
            f" | ratio={threshold_info['last_ratio']:.2f}"
            f" | bias={htf_bias}"
            f" | direction={direction.value}"
        )
        return True
    
    def _reset_trending_range_mode(self) -> None:
        """Reset settings after trending range signal is processed."""
        if getattr(self, '_trending_range_active', False):
            self.min_rr = getattr(self, '_original_min_rr', self.min_rr)
            self.max_rr = getattr(self, '_original_max_rr', self.max_rr)
            self.risk_factor = getattr(self, '_original_risk_factor', self.risk_factor)
            self._trending_range_active = False
    
    def build_signal(
        self,
        symbol: str,
        candles_htf: List[dict],       # HTF candles (1H/4H)
        candles_mtf: List[dict],       # MTF candles (15m)
        candles_ltf: List[dict],       # LTF candles (5m)
        structure_state: Dict[str, Any],
        liquidity_state: Dict[str, Any],
        ob_state: Dict[str, Any],
        fvg_state: Dict[str, Any],
        current_price: float,
        timestamp: int,
        atr: float = 0.0,
        is_weekend: bool = False,      # NEW: weekend flag for threshold relaxation
    ) -> AsiaWeekendSignalResult:
        """
        Build a mean reversion signal for Asia/Weekend session.
        
        Flow:
            1. Configure asset-specific threshold
            2. Update range detector with MTF candles
            3. Check for valid range (or try trending range mode)
            4. Check for sweep of range boundary
            5. Check LTF structure for reversal confirmation
            6. Find entry zone (OB/FVG)
            7. Calculate R:R and generate signal
        """
        result = AsiaWeekendSignalResult(
            symbol=symbol,
            signal=None,
            status="NO_SIGNAL",
            reasons=[],
            strategy_name="AsiaWeekendStrategy",
        )
        
        # Import config early to avoid UnboundLocalError (multiple local imports in this method)
        from smc_futures.smc_config import SMC_CONFIG
        
        # 0. Track ATR history for adaptive calculations
        if atr <= 0 and candles_mtf:
            # Simple ATR calculation using range detector's logic if possible
            atr = self.range_detector._calculate_atr(candles_mtf)

        if atr > 0:
            self.atr_history.append(atr)
            if len(self.atr_history) > self.max_atr_history:
                self.atr_history.pop(0)

        # 0b. Volatility filter: skip dead markets
        if not self.range_detector.check_volatility_filter(atr):
            result.status = "REJECTED"
            result.reasons.append("DEAD_MARKET_LOW_VOLATILITY")
            logger.info(f"[ASIA_STRAT] {symbol} | REJECTED | reason=DEAD_MARKET_LOW_VOLATILITY")
            return result

        # 1. Configure Static (A) vs Adaptive (B) thresholds
        current_session = "asia" # AsiaWeekend is session-locked
        
        thresholds = self._configure_threshold_for_symbol(
            symbol=symbol, 
            is_weekend=is_weekend,
            session_name=current_session,
            current_atr=atr
        )
        
        # 2. Update range detector with MTF candles to get ratio
        range_state = self.range_detector.update(candles_mtf, atr)
        ratio = range_state.current_range.range_atr_ratio if range_state and range_state.current_range else 0.0
        
        # Extract threshold values
        static_val = thresholds["static"]
        adaptive_val = thresholds["adaptive"]
        ab_mode = SMC_CONFIG.get("ADAPTIVE_MODE", "off")
        
        # Determine internal actions for analytics
        action_a = ratio <= static_val
        action_b = adaptive_val is not None and ratio <= adaptive_val
        
        # Construct Decision Traces
        avg_atr = sum(self.atr_history) / len(self.atr_history) if self.atr_history else 0.0
        features_dict = {"atr": atr, "avg_atr": avg_atr}
        trace_a = {
            "engine": "A",
            "accept": action_a,
            "reason": "OK" if action_a else "RANGE_TOO_LARGE",
            "threshold": static_val,
            "range_ratio": ratio,
            "features": features_dict
        }
        trace_b = {
            "engine": "B",
            "accept": action_b,
            "reason": "OK" if action_b else "RANGE_TOO_LARGE",
            "threshold": adaptive_val,
            "range_ratio": ratio,
            "features": features_dict
        } if adaptive_val is not None else None

        # Deterministic Allocation
        seed = SMC_CONFIG.get("ADAPTIVE_AB_SEED", 42)
        date_str = datetime.now().strftime("%Y-%m-%d")
        # Key format: "symbol|session|date"
        allocation_key_template = SMC_CONFIG.get("ADAPTIVE_AB_KEY", "symbol|session|date")
        key = allocation_key_template.replace("symbol", symbol).replace("session", current_session).replace("date", date_str)
        weight = SMC_CONFIG.get("ADAPTIVE_LIVE_WEIGHT", 0.0)
        
        # Selection Logic
        selected_engine = "A"
        if ab_mode == "live_default":
            selected_engine = "B" if adaptive_val is not None else "A"
        elif ab_mode == "live_split":
            selected_engine = select_engine(key, seed, weight) if adaptive_val is not None else "A"
        elif ab_mode == "shadow":
            selected_engine = "A"
        # ab_mode == "off": selected_engine stays "A"
        
        # Final values for execution
        selected_threshold = adaptive_val if selected_engine == "B" and adaptive_val is not None else static_val
        source = thresholds["source_b"] if selected_engine == "B" else thresholds["source_a"]

        # Record A/B metadata for signal result
        ab_metadata = {
            "mode": ab_mode,
            "selected": selected_engine,
            "A": trace_a,
            "B": trace_b,
            "key": key,
            "seed": seed,
            "weight": weight
        }
        result.metadata["ab_test"] = ab_metadata

        # Record A/B decision if mode != off
        if ab_mode != "off" and adaptive_val is not None:
            self.ab_test_service.record_decision(symbol, ab_metadata)

        # Log comparison for audit
        if ab_mode != "off" and adaptive_val is not None:
            logger.info(
                f"[A/B_TEST] mode={ab_mode} symbol={symbol} session={current_session} "
                f"A={'ACCEPT' if action_a else 'REJECT'}({static_val:.2f}) "
                f"B={'ACCEPT' if action_b else 'REJECT'}({adaptive_val:.2f}) "
                f"selected={selected_engine} reason={trace_a['reason'] if selected_engine=='A' else trace_b['reason']} "
                f"key={key} seed={seed} weight={weight}"
            )

        # 4. Final Application to Range Detector for Signal Logic
        self.range_detector.set_max_range_atr(selected_threshold, source)
        
        # Final update to ensure range_state reflects the selected threshold filters
        range_state = self.range_detector.update(candles_mtf, atr)
        threshold_info = self.range_detector.get_threshold_info()
        
        result.range_high = range_state.current_range.high if range_state.current_range else None
        result.range_low = range_state.current_range.low if range_state.current_range else None
        result.range_mid = range_state.current_range.mid if range_state.current_range else None
        result.trend_strength = range_state.trend_strength
        
        # 3. Check for valid range (or try trending range mode)
        range_valid = range_state.current_range and range_state.current_range.is_valid
        
        # Trending range mode: if range is large but structure is clean
        trending_range_allowed = False
        range_bypass_active = False
        if not range_valid and range_state.current_range:
            trending_range_allowed = self._try_trending_range_mode(
                symbol, range_state, threshold_info, structure_state
            )
            # NEW: Secondary bypass for range too large
            if not trending_range_allowed:
                range_bypass_active = self._try_range_too_large_bypass(
                    symbol, range_state, threshold_info, structure_state,
                    ob_state, fvg_state, current_price
                )
        
        if not range_valid and not trending_range_allowed and not range_bypass_active:
            reason = range_state.current_range.invalidation_reason if range_state.current_range else "NO_RANGE_DETECTED"
            result.status = "REJECTED"
            result.reasons.append(str(AsiaRejectReason.NO_RANGE.value))
            logger.info(
                f"[ASIA_STRAT] {symbol} | REJECTED"
                f" | reason={reason}"
                f" | range_ratio={threshold_info['last_ratio']:.2f}"
                f" | threshold={selected_threshold:.1f}"
                f" | allowed_by={source}"
            )
            return result
        
        # Log success case
        ratio = threshold_info['last_ratio']
        if range_bypass_active:
            range_mode_label = "RANGE_BYPASS"
        else:
            range_mode_label = "TRENDING_RANGE" if trending_range_allowed else "NORMAL_RANGE"
            
        logger.info(
            f"[ASIA_STRAT] {symbol} | RANGE_OK"
            f" | mode={range_mode_label}"
            f" | ratio={ratio:.2f}"
            f" | threshold={selected_threshold:.1f}"
            f" | allowed_by={source}"
        )
        
        # 4. Check trend strength (if too trendy, skip mean reversion)
        if range_state.trend_strength > self.htf_trend_threshold:
            result.status = "REJECTED"
            result.reasons.append(str(AsiaRejectReason.HTF_TREND_TOO_STRONG.value))
            logger.info(
                f"[ASIA_STRAT] {symbol} | REJECTED"
                f" | reason=HTF_TREND_TOO_STRONG"
                f" | strength={range_state.trend_strength:.2f}"
                f" | threshold={self.htf_trend_threshold}"
            )
            self._reset_trending_range_mode()
            return result
        
        # 4b. Check for range sweep (or apply no-sweep bypass, or try soft mean reversion)
        recent_sweep = self.range_detector.has_recent_sweep()
        no_sweep_bypass_active = False
        no_sweep_direction = None
        soft_mean_reversion_active = False
        soft_mr_direction = None
        bypass_result = {}  # Initialize for later use in RR calculation
        
        if not recent_sweep:
            # Try no-sweep bypass with OB/FVG + BOS/CHoCH confirmation
            bypass_result = self._try_no_sweep_bypass(
                symbol=symbol,
                structure_state=structure_state,
                ob_state=ob_state,
                fvg_state=fvg_state,
                current_price=current_price,
                range_state=range_state,
            )
            
            if bypass_result["allowed"]:
                no_sweep_bypass_active = True
                no_sweep_direction = bypass_result["direction"]
                logger.info(
                    f"[ASIA_STRAT] {symbol} | NO_SWEEP_BYPASS_ACTIVE"
                    f" | direction={no_sweep_direction.value}"
                    f" | trigger={bypass_result['trigger']}"
                    f" | zone_type={bypass_result['zone_type']}"
                    f" | risk_mult={bypass_result.get('risk_mult', 0.5)}"
                )
            else:
                # Fallback: Try SOFT MEAN REVERSION (weekend only)
                if is_weekend:
                    soft_result = self._try_soft_mean_reversion(
                        symbol=symbol,
                        ob_state=ob_state,
                        fvg_state=fvg_state,
                        structure_state=structure_state,
                        current_price=current_price,
                        range_state=range_state,
                        atr=atr,
                    )
                    
                    if soft_result["allowed"]:
                        soft_mean_reversion_active = True
                        soft_mr_direction = soft_result["direction"]
                        logger.info(
                            f"[ASIA_STRAT] {symbol} | SOFT_MEAN_REVERSION_ACTIVE"
                            f" | direction={soft_mr_direction.value}"
                            f" | ob_type={soft_result.get('ob_type', 'N/A')}"
                            f" | fvg_type={soft_result.get('fvg_type', 'N/A')}"
                            f" | risk_factor={soft_result.get('risk_factor', 0.6):.2f}"
                        )
                    else:
                        # All bypass modes failed
                        result.status = "NO_SIGNAL"
                        result.reasons.append(str(AsiaRejectReason.NO_SWEEP.value))
                        if bypass_result.get("reason"):
                            result.reasons.append(f"NO_BYPASS: {bypass_result['reason']}")
                        if soft_result.get("reason"):
                            result.reasons.append(f"NO_SOFT: {soft_result['reason']}")
                        logger.debug(
                            f"[ASIA_STRAT] {symbol} | NO_SIGNAL"
                            f" | reason=NO_RANGE_SWEEP"
                            f" | bypass={bypass_result.get('reason', 'disabled')}"
                            f" | soft={soft_result.get('reason', 'disabled')}"
                        )
                        self._reset_trending_range_mode()
                        return result
                else:
                    # Not weekend, no soft mode available
                    result.status = "NO_SIGNAL"
                    result.reasons.append(str(AsiaRejectReason.NO_SWEEP.value))
                    if bypass_result.get("reason"):
                        result.reasons.append(f"NO_BYPASS: {bypass_result['reason']}")
                    logger.debug(f"[ASIA_STRAT] {symbol} | NO_SIGNAL | reason=NO_RANGE_SWEEP | bypass={bypass_result.get('reason', 'disabled')}")
                    self._reset_trending_range_mode()
                    return result
        
        # Set sweep side and direction based on sweep, bypass, or soft mode
        if recent_sweep:
            result.sweep_side = recent_sweep.sweep_side
            result.entry_mode = "sweep"
            # 5. Check sweep confirmation (close back inside range)
            if self.require_sweep_confirmation and not recent_sweep.closed_inside:
                result.status = "NO_SIGNAL"
                result.reasons.append(str(AsiaRejectReason.SWEEP_NOT_CONFIRMED.value))
                logger.debug(
                    f"[ASIA_STRAT] {symbol} | NO_SIGNAL"
                    f" | reason=SWEEP_NOT_CONFIRMED"
                    f" | sweep_side={recent_sweep.sweep_side}"
                )
                self._reset_trending_range_mode()
                return result
            
            # 6. Determine trade direction (opposite of sweep)
            # Swept high → Expect SHORT (sell into the sweep)
            # Swept low → Expect LONG (buy into the sweep)
            if recent_sweep.sweep_side == "high":
                direction = SignalDirection.SHORT
            else:
                direction = SignalDirection.LONG
        elif no_sweep_bypass_active:
            # No-sweep bypass: direction determined by confirmation
            direction = no_sweep_direction
            result.sweep_side = "no_sweep_bypass"
            result.entry_mode = "no_sweep_bypass"
        elif soft_mean_reversion_active:
            # Soft mean reversion: direction from OB+FVG alignment
            direction = soft_mr_direction
            result.sweep_side = "soft_mean_reversion"
            result.entry_mode = "soft_mean_reversion"
        else:
            # Should not reach here
            result.status = "NO_SIGNAL"
            result.reasons.append("NO_VALID_ENTRY_MODE")
            return result
        
        # 7. Check LTF structure for reversal (CHoCH/BOS)
        # Skip if already confirmed in no-sweep bypass OR in soft mean reversion mode
        if self.require_ltf_structure and not no_sweep_bypass_active and not soft_mean_reversion_active:
            ltf_confirmed = self._check_ltf_reversal(
                structure_state, 
                direction,
                liquidity_state
            )
            if not ltf_confirmed:
                result.status = "NO_SIGNAL"
                result.reasons.append(str(AsiaRejectReason.NO_LTF_REVERSAL.value))
                logger.debug(
                    f"[ASIA_STRAT] {symbol} | NO_SIGNAL"
                    f" | reason=NO_LTF_REVERSAL"
                    f" | expected_dir={direction.value}"
                )
                self._reset_trending_range_mode()
                return result
        
        # 8. Find entry zone (OB or FVG)
        entry_zone = self._find_entry_zone(
            ob_state, 
            fvg_state, 
            direction, 
            current_price,
            range_state.current_range,
        )
        
        if self.require_entry_zone and entry_zone is None:
            result.status = "REJECTED"
            result.reasons.append(str(AsiaRejectReason.NO_ENTRY_ZONE.value))
            logger.info(
                f"[ASIA_STRAT] {symbol} | REJECTED"
                f" | reason=NO_ENTRY_ZONE"
                f" | direction={direction.value}"
            )
            self._reset_trending_range_mode()
            return result
        
        # 9. Calculate entry, SL, TP
        entry_price = entry_zone.get("entry", current_price) if entry_zone else current_price
        
        range_obj = range_state.current_range
        
        # Stop loss: beyond the sweep level (or range boundary if no sweep)
        if recent_sweep:
            sweep_ref = recent_sweep.sweep_price
        else:
            # No sweep (bypass / soft mode): use range boundary
            sweep_ref = range_obj.low if direction == SignalDirection.LONG else range_obj.high
        
        if direction == SignalDirection.LONG:
            stop_loss = min(range_obj.low, sweep_ref) - (atr * 0.2)
        else:
            stop_loss = max(range_obj.high, sweep_ref) + (atr * 0.2)
        
        # Take profit: mid-range or opposite boundary
        if self.target_mid_range:
            take_profit = range_obj.mid
        else:
            if direction == SignalDirection.LONG:
                take_profit = range_obj.high
            else:
                take_profit = range_obj.low
        
        # 10. Calculate R:R
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        rr_ratio = reward / risk if risk > 0 else 0
        
        # Set min/max RR based on entry mode
        effective_min_rr = SMC_CONFIG.get("OFFSESSION_MIN_RR", self.min_rr)  # Use OFFSESSION_MIN_RR
        effective_max_rr = self.max_rr
        effective_risk_mult = 1.0
        
        if no_sweep_bypass_active:
            # Check if OB/FVG-only mode (more conservative)
            if bypass_result.get("trigger") == "ob_fvg_only":
                effective_min_rr = SMC_CONFIG.get("ASIA_OB_FVG_ONLY_MIN_RR", 1.2)
                effective_risk_mult = bypass_result.get("risk_mult", SMC_CONFIG.get("ASIA_OB_FVG_ONLY_RISK_MULT", 0.4))
            else:
                effective_min_rr = SMC_CONFIG.get("ASIA_NO_SWEEP_MIN_RR", 1.5)
                effective_risk_mult = bypass_result.get("risk_mult", SMC_CONFIG.get("ASIA_NO_SWEEP_RISK_MULT", 0.5))
        elif soft_mean_reversion_active:
            effective_min_rr = SMC_CONFIG.get("WEEKEND_SOFT_MIN_RR", 1.0)
            effective_max_rr = SMC_CONFIG.get("WEEKEND_SOFT_MAX_RR", 1.3)
        
        # Check minimum R:R
        if rr_ratio < effective_min_rr:
            result.status = "REJECTED"
            result.reasons.append(f"RR_TOO_LOW_{rr_ratio:.2f}_MIN_{effective_min_rr}")
            logger.info(
                f"[ASIA_STRAT] {symbol} | REJECTED"
                f" | reason=RR_TOO_LOW"
                f" | rr={rr_ratio:.2f} < {effective_min_rr}"
                f" | mode={result.entry_mode}"
            )
            self._reset_trending_range_mode()
            return result
        
        # For soft mode: also check max RR (conservative targeting)
        if soft_mean_reversion_active and rr_ratio > effective_max_rr:
            result.status = "REJECTED"
            result.reasons.append(f"RR_TOO_HIGH_FOR_SOFT_{rr_ratio:.2f}_MAX_{effective_max_rr}")
            logger.info(
                f"[ASIA_STRAT] {symbol} | REJECTED"
                f" | reason=RR_TOO_HIGH_FOR_SOFT_MODE"
                f" | rr={rr_ratio:.2f} > {effective_max_rr}"
            )
            self._reset_trending_range_mode()
            return result
        
        # Cap R:R at max for conservative targeting (non-soft modes)
        if not soft_mean_reversion_active and rr_ratio > self.max_rr:
            # Adjust TP to cap R:R
            if direction == SignalDirection.LONG:
                take_profit = entry_price + (risk * self.max_rr)
            else:
                take_profit = entry_price - (risk * self.max_rr)
            rr_ratio = self.max_rr
        
        # 11. Build signal (handle sweep, no-sweep, and soft modes)
        if recent_sweep:
            sweep_side = recent_sweep.sweep_side
            setup_suffix = f"SWEEP_{sweep_side.upper()}"
            trigger_type = "range_sweep_reversal"
            confidence = 0.6
        elif no_sweep_bypass_active:
            bypass_trigger = bypass_result.get("trigger", "bos")
            if bypass_trigger == "ob_fvg_only":
                sweep_side = "ob_fvg_only"
                setup_suffix = "OB_FVG_ONLY"
                trigger_type = "ob_fvg_alignment_only"
                confidence = 0.4  # Lower confidence for OB/FVG-only
            else:
                sweep_side = "no_sweep_bypass"
                setup_suffix = "NO_SWEEP_REVERSAL"
                trigger_type = "no_sweep_ob_fvg_confirmation"
                confidence = 0.5
        else:  # soft_mean_reversion_active
            sweep_side = "soft_mean_reversion"
            setup_suffix = "SOFT_MEAN_REVERSION"
            trigger_type = "soft_ob_fvg_alignment"
            confidence = 0.45  # Lower confidence for soft mode
            result.soft_mode_active = True
        
        # Get risk factor based on entry mode
        risk_factor = 1.0
        if soft_mean_reversion_active:
            risk_factor = SMC_CONFIG.get("WEEKEND_SOFT_RISK_FACTOR", 0.6)
        elif no_sweep_bypass_active:
            risk_factor = effective_risk_mult  # Uses value from bypass_result
        
        signal = SMCSignal(
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            setup_type=f"ASIA_RANGE_{setup_suffix}",
            trigger_type=trigger_type,
            liquidity_trigger=f"sweep_{sweep_side}" if recent_sweep else trigger_type,
            confidence_score=confidence,
            risk_reward_ratio=rr_ratio,
            invalidation_level=stop_loss,
            mtf_bias="ranging",
            structure_strength=1 - range_state.trend_strength,
            timestamp=timestamp,
            timeframe="5m",
        )
        
        # Store risk factor in signal metadata for position sizing
        if hasattr(signal, 'metadata'):
            signal.metadata['risk_factor'] = risk_factor
        elif hasattr(signal, '__dict__'):
            signal.risk_factor = risk_factor
        
        result.signal = signal
        result.status = "CREATED"
        
        logger.info(
            f"[ASIA_STRAT] {symbol} | SIGNAL_CREATED"
            f" | dir={direction.value}"
            f" | entry={entry_price:.5f}"
            f" | sl={stop_loss:.5f}"
            f" | tp={take_profit:.5f}"
            f" | rr={rr_ratio:.2f}"
            f" | mode={result.entry_mode}"
            f" | sweep_side={sweep_side}"
            f" | range=[{range_obj.low:.5f}-{range_obj.high:.5f}]"
            f" | confidence={confidence:.2f}"
            f" | risk_factor={risk_factor:.2f}"
        )
        
        # Reset trending range mode if it was activated
        self._reset_trending_range_mode()
        
        return result
    
    def _check_ltf_reversal(
        self,
        structure_state: Dict[str, Any],
        expected_direction: SignalDirection,
        liquidity_state: Dict[str, Any],
    ) -> bool:
        """
        Check if LTF structure confirms reversal.
        
        For LONG: Need bullish CHoCH/BOS (break above recent high after sweep low)
        For SHORT: Need bearish CHoCH/BOS (break below recent low after sweep high)
        """
        if not structure_state:
            return False
        
        # Check for BOS/CHoCH
        recent_bos = structure_state.get("last_bos", {})
        recent_choch = structure_state.get("last_choch", {})
        
        # Determine structural confirmation
        bos_direction = recent_bos.get("direction", "")
        choch_direction = recent_choch.get("direction", "")
        
        if expected_direction == SignalDirection.LONG:
            # Need bullish structure break
            return bos_direction == "bullish" or choch_direction == "bullish"
        else:
            # Need bearish structure break
            return bos_direction == "bearish" or choch_direction == "bearish"
    
    def _try_no_sweep_bypass(
        self,
        symbol: str,
        structure_state: Dict[str, Any],
        ob_state: Dict[str, Any],
        fvg_state: Dict[str, Any],
        current_price: float,
        range_state,
    ) -> Dict[str, Any]:
        """
        Attempt no-sweep bypass when OB/FVG + BOS/CHoCH confirms reversal.
        
        This allows mean reversion entry WITHOUT a range sweep if:
            1. ASIA_ALLOW_NO_SWEEP_WITH_CONFIRMATION is enabled
            2. Valid OB or FVG exists near range boundary
            3. LTF BOS/CHoCH confirms reversal direction (or fallback to OB/FVG only)
            4. Price is at range extreme (near high or low, configurable tolerance)
        
        Returns:
            {
                "allowed": bool,
                "direction": SignalDirection or None,
                "trigger": str (bos/choch/ob_fvg_only),
                "zone_type": str (ob/fvg),
                "reason": str (if not allowed),
                "risk_mult": float (risk multiplier for this entry)
            }
        """
        from smc_futures.smc_config import SMC_CONFIG
        
        # Check if bypass is enabled
        allow_bypass = SMC_CONFIG.get("ASIA_ALLOW_NO_SWEEP_WITH_CONFIRMATION", False)
        allow_ob_fvg_only = SMC_CONFIG.get("ASIA_ALLOW_ENTRY_OB_FVG_ONLY", False)
        allow_zone_bypass = SMC_CONFIG.get("ASIA_ALLOW_NO_RANGE_SWEEP_WITH_ZONE", False)
        
        if not allow_bypass and not allow_ob_fvg_only and not allow_zone_bypass:
            return {"allowed": False, "reason": "DISABLED"}
        
        require_bos = SMC_CONFIG.get("ASIA_NO_SWEEP_REQUIRE_BOS", True)
        require_zone = SMC_CONFIG.get("ASIA_NO_SWEEP_REQUIRE_ZONE", True)
        
        # Check range validity
        if not range_state or not range_state.current_range:
            return {"allowed": False, "reason": "NO_VALID_RANGE"}
        
        range_obj = range_state.current_range
        range_high = range_obj.high
        range_low = range_obj.low
        range_size = range_high - range_low
        
        # Determine price position relative to range
        boundary_tol = SMC_CONFIG.get("ASIA_BOUNDARY_TOL_PCT", 0.01)
        near_high_threshold = range_high * (1 - boundary_tol)
        near_low_threshold = range_low * (1 + boundary_tol)
        
        near_high = current_price >= near_high_threshold
        near_low = current_price <= near_low_threshold
        
        if not near_high and not near_low:
            return {"allowed": False, "reason": f"PRICE_NOT_AT_RANGE_BOUNDARY_TOL_{boundary_tol}"}
        
        # Determine potential direction based on price position
        # Near high → Expect SHORT (reversal down)
        # Near low → Expect LONG (reversal up)
        potential_direction = SignalDirection.SHORT if near_high else SignalDirection.LONG
        
        # Check for valid entry zone (OB or FVG) FIRST
        zone_type = None
        zone_info = None
        
        obs = ob_state.get("blocks", []) if ob_state else []
        fvgs = fvg_state.get("gaps", []) if fvg_state else []
        
        target_ob_type = "bearish" if potential_direction == SignalDirection.SHORT else "bullish"
        target_fvg_type = "bearish" if potential_direction == SignalDirection.SHORT else "bullish"
        
        zone_tol = SMC_CONFIG.get("ASIA_ZONE_TOL_PCT", 0.0010)
        
        def price_in_zone(high: float, low: float) -> bool:
            return low * (1 - zone_tol) <= current_price <= high * (1 + zone_tol)
        
        # Find OB near boundary
        for ob in obs:
            if ob.get("type") == target_ob_type:
                ob_high = ob.get("high", 0)
                ob_low = ob.get("low", 0)
                if potential_direction == SignalDirection.SHORT:
                    if ob_low >= near_high_threshold:
                        if allow_zone_bypass and not price_in_zone(ob_high, ob_low):
                            pass # must be in zone if zone bypass
                        else:
                            zone_type = "ob"
                            zone_info = ob
                            break
                else:
                    if ob_high <= near_low_threshold:
                        if allow_zone_bypass and not price_in_zone(ob_high, ob_low):
                            pass
                        else:
                            zone_type = "ob"
                            zone_info = ob
                            break
        
        # If no OB, check for FVG
        if not zone_type:
            for fvg in fvgs:
                if fvg.get("type") == target_fvg_type:
                    fvg_is_fresh = not fvg.get("mitigated", True)
                    if fvg_is_fresh:
                        fvg_high = fvg.get("high", 0)
                        fvg_low = fvg.get("low", 0)
                        if potential_direction == SignalDirection.SHORT and fvg_low >= near_high_threshold:
                           if allow_zone_bypass and not price_in_zone(fvg_high, fvg_low):
                               continue
                           zone_type = "fvg"
                           zone_info = fvg
                           break
                        elif potential_direction == SignalDirection.LONG and fvg_high <= near_low_threshold:
                           if allow_zone_bypass and not price_in_zone(fvg_high, fvg_low):
                               continue
                           zone_type = "fvg"
                           zone_info = fvg
                           break
        
        # Check structure confirmation (BOS/CHoCH)
        trigger_type = None
        has_structure = False
        
        # Required for BOS + ZONE
        recent_bos = structure_state.get("last_bos", {}) if structure_state else {}
        recent_choch = structure_state.get("last_choch", {}) if structure_state else {}
        
        bos_direction = recent_bos.get("direction", "")
        choch_direction = recent_choch.get("direction", "")
        
        expected_dir = "bearish" if potential_direction == SignalDirection.SHORT else "bullish"
        
        if choch_direction == expected_dir:
            trigger_type = "choch"
            has_structure = True
        elif bos_direction == expected_dir:
            trigger_type = "bos"
            has_structure = True
            
        if allow_zone_bypass and zone_type:
            # ZONE_ENTRY_NO_SWEEP bypass: Valid Zone detected, Price in Zone, proper bias
            htf_bias = structure_state.get("htf_bias", "neutral")
            
            # Allow execution if bias is aligned or neutral
            if htf_bias in [expected_dir, "neutral"]:
                logger.info(
                    f"[ASIA_STRAT] {symbol} bypass=1 reason=ZONE_ENTRY_NO_SWEEP allow_env=ASIA_ALLOW_NO_RANGE_SWEEP_WITH_ZONE"
                    f" | reject_detail: no_range_sweep=1 zone_ok=1 price_in_zone=1 boundary_dist=OK tol={boundary_tol}"
                    f" | zone={zone_type}"
                )
                return {
                    "allowed": True,
                    "direction": potential_direction,
                    "trigger": trigger_type or "zone_only",
                    "zone_type": zone_type,
                    "risk_mult": SMC_CONFIG.get("ASIA_NO_SWEEP_RISK_MULT", 0.5),
                }

        # Original Bypass (allow_bypass with structure)
        if not require_bos and allow_bypass:
            trigger_type = "structure_not_required"
            has_structure = True
            
        if has_structure and zone_type and allow_bypass:
            logger.info(
                f"[ASIA_NO_SWEEP_BYPASS] {symbol} | ALLOWED (structure)"
                f" | direction={potential_direction.value}"
                f" | trigger={trigger_type}"
                f" | zone={zone_type}"
                f" | near_high={near_high}"
                f" | near_low={near_low}"
            )
            return {
                "allowed": True,
                "direction": potential_direction,
                "trigger": trigger_type,
                "zone_type": zone_type,
                "risk_mult": SMC_CONFIG.get("ASIA_NO_SWEEP_RISK_MULT", 0.5),
            }
        
        # FALLBACK: OB/FVG-only mode
        if allow_ob_fvg_only and zone_type:
            logger.info(
                f"[ASIA_NO_SWEEP_BYPASS] {symbol} | ALLOWED (ob_fvg_only fallback)"
                f" | direction={potential_direction.value}"
                f" | zone={zone_type}"
                f" | near_high={near_high}"
                f" | near_low={near_low}"
                f" | risk_mult={SMC_CONFIG.get('ASIA_OB_FVG_ONLY_RISK_MULT', 0.4)}"
            )
            return {
                "allowed": True,
                "direction": potential_direction,
                "trigger": "ob_fvg_only",
                "zone_type": zone_type,
                "risk_mult": SMC_CONFIG.get("ASIA_OB_FVG_ONLY_RISK_MULT", 0.4),
            }

        
        # All modes failed
        if not zone_type and require_zone:
            return {"allowed": False, "reason": "NO_VALID_OB_OR_FVG_ZONE"}
        
        expected_dir = "bearish" if potential_direction == SignalDirection.SHORT else "bullish"
        return {"allowed": False, "reason": f"NO_{expected_dir.upper()}_BOS_OR_CHOCH"}
    
    def _try_soft_mean_reversion(
        self,
        symbol: str,
        ob_state: Dict[str, Any],
        fvg_state: Dict[str, Any],
        current_price: float,
        range_state,
        *,
        structure_state: Optional[Dict[str, Any]] = None,
        atr: Optional[float] = None,
        is_weekend: bool = True,
        htf_trend_strength: float = 0.5,
    ) -> Dict[str, Any]:
        """
        Attempt soft mean reversion for weekend trading with relaxed conditions.
        
        This is a WEEKEND-ONLY mode that allows entry WITHOUT sweep AND without
        BOS/CHoCH structure confirmation, relying on OB+FVG alignment only.
        
        Conditions:
            1. ENABLE_WEEKEND_SOFT_MEAN_REVERSION is enabled
            2. Is weekend (Saturday/Sunday)
            3. Valid range exists with low trend strength (<= 0.7)
            4. BOTH OB and FVG must align in same direction
            5. Price near range boundary
            6. Trade limit not exceeded for this symbol
        
        Trade is taken with:
            - Lower confidence (0.45)
            - Reduced risk (WEEKEND_SOFT_RISK_FACTOR, default 0.6x)
            - Conservative RR (1.0-1.3)
        
        Returns:
            {
                "allowed": bool,
                "direction": SignalDirection or None,
                "reason": str (if not allowed)
            }
        """
        from smc_futures.smc_config import SMC_CONFIG
        
        # Check if soft mode is enabled
        if not SMC_CONFIG.get("ENABLE_WEEKEND_SOFT_MEAN_REVERSION", False):
            return {"allowed": False, "reason": "SOFT_MODE_DISABLED"}
        
        # Must be weekend
        if not is_weekend:
            return {"allowed": False, "reason": "NOT_WEEKEND"}
        
        # Check range validity
        if not range_state or not range_state.current_range:
            return {"allowed": False, "reason": "NO_VALID_RANGE"}
        
        # Check trend strength - must be ranging (low trend)
        max_trend = 0.7
        if htf_trend_strength > max_trend:
            return {"allowed": False, "reason": f"TREND_TOO_STRONG_{htf_trend_strength:.2f}"}
        
        range_obj = range_state.current_range
        range_high = range_obj.high
        range_low = range_obj.low
        range_size = range_high - range_low
        
        # Check price at range boundary
        near_boundary_pct = 0.20  # More lenient for soft mode (20%)
        near_high_threshold = range_high - range_size * near_boundary_pct
        near_low_threshold = range_low + range_size * near_boundary_pct
        
        near_high = current_price >= near_high_threshold
        near_low = current_price <= near_low_threshold
        
        if not near_high and not near_low:
            return {"allowed": False, "reason": "PRICE_NOT_AT_RANGE_BOUNDARY"}
        
        # Potential direction based on price position
        potential_direction = SignalDirection.SHORT if near_high else SignalDirection.LONG
        
        # Check trade limit per symbol
        max_trades = SMC_CONFIG.get("WEEKEND_SOFT_MAX_TRADES_PER_SYMBOL", 1)
        soft_trade_key = f"soft_mr_{symbol}"
        current_count = getattr(self, '_soft_mr_trade_counts', {}).get(soft_trade_key, 0)
        
        if current_count >= max_trades:
            return {"allowed": False, "reason": f"SOFT_MODE_LIMIT_REACHED_{max_trades}"}
        
        # CRITICAL: Require BOTH OB and FVG aligned
        require_ob = SMC_CONFIG.get("WEEKEND_SOFT_REQUIRE_OB", True)
        require_fvg = SMC_CONFIG.get("WEEKEND_SOFT_REQUIRE_FVG", True)
        
        obs = ob_state.get("blocks", []) if ob_state else []
        fvgs = fvg_state.get("gaps", []) if fvg_state else []
        
        target_ob_type = "bearish" if potential_direction == SignalDirection.SHORT else "bullish"
        target_fvg_type = "bearish" if potential_direction == SignalDirection.SHORT else "bullish"
        
        has_aligned_ob = False
        has_aligned_fvg = False
        
        # Check for aligned OB
        if require_ob:
            for ob in obs:
                if ob.get("type") == target_ob_type:
                    ob_high = ob.get("high", 0)
                    ob_low = ob.get("low", 0)
                    # OB should be near the boundary we're trading from
                    if potential_direction == SignalDirection.SHORT:
                        if ob_low >= near_high_threshold:
                            has_aligned_ob = True
                            break
                    else:
                        if ob_high <= near_low_threshold:
                            has_aligned_ob = True
                            break
            
            if not has_aligned_ob:
                return {"allowed": False, "reason": f"NO_ALIGNED_{target_ob_type.upper()}_OB"}
        else:
            has_aligned_ob = True
        
        # Check for aligned FVG
        if require_fvg:
            for fvg in fvgs:
                fvg_type = fvg.get("type", "")
                fvg_fresh = not fvg.get("mitigated", True)
                
                if fvg_type == target_fvg_type and fvg_fresh:
                    has_aligned_fvg = True
                    break
            
            if not has_aligned_fvg:
                return {"allowed": False, "reason": f"NO_ALIGNED_{target_fvg_type.upper()}_FVG"}
        else:
            has_aligned_fvg = True
        
        # Both conditions satisfied
        # Increment trade count for this symbol
        if not hasattr(self, '_soft_mr_trade_counts'):
            self._soft_mr_trade_counts = {}
        self._soft_mr_trade_counts[soft_trade_key] = current_count + 1
        
        logger.info(
            f"[ASIA_SOFT_MEAN_REVERSION] {symbol} | ALLOWED"
            f" | direction={potential_direction.value}"
            f" | near_high={near_high}"
            f" | near_low={near_low}"
            f" | has_ob={has_aligned_ob}"
            f" | has_fvg={has_aligned_fvg}"
            f" | trade_count={current_count + 1}/{max_trades}"
        )
        
        return {
            "allowed": True,
            "direction": potential_direction,
        }
    
    def _find_entry_zone(
        self,
        ob_state: Dict[str, Any],
        fvg_state: Dict[str, Any],
        direction: SignalDirection,
        current_price: float,
        range_level,
    ) -> Optional[Dict[str, Any]]:
        """
        Find valid entry zone (OB or FVG) for the trade direction.
        
        Prioritizes:
            1. Fresh OB in direction
            2. Fresh FVG in direction
            3. Price at range boundary (if no OB/FVG)
        """
        entry_zone = None
        
        # Check Order Blocks
        obs = ob_state.get("blocks", []) if ob_state else []
        for ob in obs:
            ob_type = ob.get("type", "")
            ob_high = ob.get("high", 0)
            ob_low = ob.get("low", 0)
            
            # Match direction
            if direction == SignalDirection.LONG and ob_type == "bullish":
                if ob_low <= current_price <= ob_high * 1.01:
                    entry_zone = {
                        "type": "OB",
                        "entry": (ob_high + ob_low) / 2,
                        "zone_high": ob_high,
                        "zone_low": ob_low,
                    }
                    break
            elif direction == SignalDirection.SHORT and ob_type == "bearish":
                if ob_low * 0.99 <= current_price <= ob_high:
                    entry_zone = {
                        "type": "OB",
                        "entry": (ob_high + ob_low) / 2,
                        "zone_high": ob_high,
                        "zone_low": ob_low,
                    }
                    break
        
        if entry_zone:
            return entry_zone
        
        # Check FVGs
        fvgs = fvg_state.get("gaps", []) if fvg_state else []
        for fvg in fvgs:
            fvg_type = fvg.get("type", "")
            fvg_high = fvg.get("high", 0)
            fvg_low = fvg.get("low", 0)
            
            if direction == SignalDirection.LONG and fvg_type == "bullish":
                if fvg_low <= current_price <= fvg_high * 1.01:
                    entry_zone = {
                        "type": "FVG",
                        "entry": (fvg_high + fvg_low) / 2,
                        "zone_high": fvg_high,
                        "zone_low": fvg_low,
                    }
                    break
            elif direction == SignalDirection.SHORT and fvg_type == "bearish":
                if fvg_low * 0.99 <= current_price <= fvg_high:
                    entry_zone = {
                        "type": "FVG",
                        "entry": (fvg_high + fvg_low) / 2,
                        "zone_high": fvg_high,
                        "zone_low": fvg_low,
                    }
                    break
        
        if entry_zone:
            return entry_zone
        
        # Fallback: use current price if near range boundary
        if range_level:
            boundary_tolerance = range_level.range_size * 0.1  # 10% of range
            
            if direction == SignalDirection.LONG:
                if abs(current_price - range_level.low) < boundary_tolerance:
                    return {
                        "type": "RANGE_BOUNDARY",
                        "entry": current_price,
                        "zone_high": range_level.low + boundary_tolerance,
                        "zone_low": range_level.low,
                    }
            else:
                if abs(current_price - range_level.high) < boundary_tolerance:
                    return {
                        "type": "RANGE_BOUNDARY",
                        "entry": current_price,
                        "zone_high": range_level.high,
                        "zone_low": range_level.high - boundary_tolerance,
                    }
        
        return None
    
    def reset_soft_mode_counts(self) -> None:
        """Reset soft mode trade counts. Call at start of weekend."""
        if hasattr(self, '_soft_mr_trade_counts'):
            self._soft_mr_trade_counts.clear()
            logger.info("[ASIA_STRAT] Soft mode trade counts reset")
    
    def get_state(self) -> Dict[str, Any]:
        """Get current strategy state for debugging/logging."""
        range_state = self.range_detector.get_current_state()
        
        soft_mode_counts = getattr(self, '_soft_mr_trade_counts', {})
        
        return {
            "strategy": "AsiaWeekendStrategy",
            "is_ranging": range_state.is_ranging,
            "trend_strength": range_state.trend_strength,
            "range": {
                "high": range_state.current_range.high if range_state.current_range else None,
                "low": range_state.current_range.low if range_state.current_range else None,
                "mid": range_state.current_range.mid if range_state.current_range else None,
                "valid": range_state.current_range.is_valid if range_state.current_range else False,
            },
            "recent_sweeps": [
                {
                    "side": s.sweep_side,
                    "confirmed": s.closed_inside,
                    "valid": s.is_valid_sweep,
                }
                for s in range_state.recent_sweeps
            ],
            "soft_mode_trade_counts": soft_mode_counts,
        }
