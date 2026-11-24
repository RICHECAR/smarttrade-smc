"""
SMC Signal Builder — 3-Tier MTF Version
════════════════════════════════════════
Combines: HTF Bias + 15m POI zones + 5m Trigger → Trade Signal
Every decision is logged with a reason code; no silent return None.
"""

from typing import Dict, Optional, List, Any
from dataclasses import dataclass, field
from enum import Enum
import logging

from smc_futures.engine.utils.reason_codes import ReasonCode

logger = logging.getLogger(__name__)

# Epsilon for floating-point RR comparison (avoids 1.9999999 < 2.0 false rejections)
RR_EPSILON = 1e-6


# ──────────────────────────────────────────────────
#  Data classes
# ──────────────────────────────────────────────────

class SignalDirection(Enum):
    LONG  = "long"
    SHORT = "short"
    HOLD  = "hold"


@dataclass
class SMCSignal:
    """Complete SMC trade signal."""
    direction: SignalDirection
    entry_price: float
    stop_loss: float
    take_profit: float

    # Setup details
    setup_type: str                          # "OB", "FVG", "Sweep+OB" …
    trigger_type: str = ""                   # "choch", "bos", "sweep_flip"
    liquidity_trigger: Optional[str] = None

    # Quality
    confidence_score: float  = 0.5
    risk_reward_ratio: float = 1.0
    invalidation_level: float = 0.0

    # Context
    mtf_bias: str            = "neutral"
    structure_strength: float = 0.5
    timestamp: int           = 0
    timeframe: str           = "5m"


@dataclass
class SignalResult:
    """
    Standardized result for signal generation.
    Eliminates silent failures by explicitly tracking rejection reasons.
    """
    symbol: str
    signal: Optional[SMCSignal] = None
    status: str = "NO_SIGNAL"          # NO_SIGNAL | REJECTED | CREATED
    reasons: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────
#  Builder
# ──────────────────────────────────────────────────

class SignalBuilder:
    """
    Builds SMC signals from:
        • mtf_state   — bias + entry zones  (from MTFBiasEngine)
        • ltf_trigger — 5m confirmation     (from MTFBiasEngine.check_ltf_trigger)
        • ob/fvg/liq  — raw engine states   (15m)
    
    Fallback Mode:
        When no liquidity sweep is detected, can still generate signals
        if BOS + fresh FVG is present, with stricter RR requirements.
    """

    def __init__(
        self,
        min_rr_ratio: float = 1.5,
        neutral_permissive: bool = False,
        allow_countertrend: bool = False,
        countertrend_min_rr: float = 2.0,
        # Fallback mode config
        sweep_fallback_enabled: bool = True,
        sweep_fallback_min_rr: float = 2.0,
        sweep_fallback_max_fvg_age: int = 10,
        sweep_fallback_max_ob_age: int = 20,
        # Fallback FVG requirements
        fallback_require_fvg: bool = True,
        fallback_allow_no_fvg: bool = False,
        # Force execute config
        force_execute: bool = False,
        # Soft bypass: allow BOS+OB without sweep (small account / growth mode)
        # Soft bypass keys
        allow_no_sweep_with_ob: bool = False,
        allow_no_sweep_with_fvg: bool = False,
        allow_no_sweep_with_zone: bool = False,
        no_sweep_min_rr: float = 1.5,
        no_sweep_require_fresh_ob: bool = True,
        no_sweep_block_strong_htf: bool = True,
        # LTF trigger relaxation
        allow_no_ltf_trigger_with_zone: bool = False,
        ltf_trigger_lookback: int = 3,
        no_ltf_trigger_min_rr: float = 2.0,
        # NO_ENTRY_ZONE relaxation
        force_entry_zone: bool = False,
        entry_zone_padding: float = 0.0,
        entry_zone_max_dist: float = 100.0,
        entry_zone_nonfresh: bool = False,
        # Entry zone ATR fallback (when obs=0 & fvgs=0)
        entry_fallback_mode: bool = False,
        entry_fallback_atr_mult: float = 0.25,
        entry_fallback_sl_atr_mult: float = 1.5,
        entry_fallback_rr_min: float = 2.0,
    ):
        self.min_rr_ratio = min_rr_ratio
        self.neutral_permissive = neutral_permissive
        self.allow_countertrend = allow_countertrend
        self.countertrend_min_rr = countertrend_min_rr
        
        # Fallback mode settings
        self.sweep_fallback_enabled = sweep_fallback_enabled
        self.sweep_fallback_min_rr = sweep_fallback_min_rr
        self.sweep_fallback_max_fvg_age = sweep_fallback_max_fvg_age
        self.sweep_fallback_max_ob_age = sweep_fallback_max_ob_age
        self.fallback_require_fvg = fallback_require_fvg
        self.fallback_allow_no_fvg = fallback_allow_no_fvg
        self.force_execute = force_execute
        
        # Soft bypass: allow BOS+OB or BOS+FVG without sweep
        self.allow_no_sweep_with_ob = allow_no_sweep_with_ob
        self.allow_no_sweep_with_fvg = allow_no_sweep_with_fvg
        self.allow_no_sweep_with_zone = allow_no_sweep_with_zone
        self.no_sweep_min_rr = no_sweep_min_rr
        self.no_sweep_require_fresh_ob = no_sweep_require_fresh_ob
        self.no_sweep_block_strong_htf = no_sweep_block_strong_htf
        
        # LTF trigger relaxation
        self.allow_no_ltf_trigger_with_zone = allow_no_ltf_trigger_with_zone
        self.ltf_trigger_lookback = ltf_trigger_lookback
        self.no_ltf_trigger_min_rr = no_ltf_trigger_min_rr
        
        # Entry relaxation
        self.force_entry_zone = force_entry_zone
        self.entry_zone_padding = entry_zone_padding
        self.entry_zone_max_dist = entry_zone_max_dist
        self.entry_zone_nonfresh = entry_zone_nonfresh
        
        # Entry zone ATR fallback
        self.entry_fallback_mode = entry_fallback_mode
        self.entry_fallback_atr_mult = entry_fallback_atr_mult
        self.entry_fallback_sl_atr_mult = entry_fallback_sl_atr_mult
        self.entry_fallback_rr_min = entry_fallback_rr_min
        
        self.last_signal: Optional[SMCSignal] = None
        self.signal_history: List[SMCSignal] = []

    # ──────────────────────────────────────────
    #  Main entry point
    # ──────────────────────────────────────────

    def build_signal(
        self,
        symbol: str,
        structure_state: Dict,
        liquidity_state: Dict,
        ob_state: Dict,
        fvg_state: Dict,
        mtf_state: Dict,
        current_price: float,
        timestamp: int,
        ltf_trigger: Optional[Dict] = None,
    ) -> SignalResult:
        """
        Build signal from all SMC components.
        Returns SignalResult (never None).
        """
        result = SignalResult(symbol=symbol)

        # ── Step 1: HTF bias ─────────────────────
        bias = mtf_state.get("bias", "neutral")
        bias_strength = mtf_state.get("bias_strength", 0.5)
        trading_disabled = mtf_state.get("trading_disabled", False)

        if trading_disabled:
            result.status = "REJECTED"
            result.reasons.append(f"{ReasonCode.HTF_INSUFFICIENT_DATA}")
            logger.info(
                f"[FILTER] {symbol} | REJECTED"
                f" | reason={ReasonCode.HTF_INSUFFICIENT_DATA}"
                f" | bias={bias}"
            )
            return result

        # ── NEUTRAL handling ─────────────────────
        is_neutral_setup = False
        required_rr = self.min_rr_ratio

        if bias == "neutral":
            if not self.neutral_permissive:
                result.status = "NO_SIGNAL"
                result.reasons.append(f"{ReasonCode.HTF_NEUTRAL_PROD} (strict mode)")
                return result

            # Permissive mode: override with LTF direction
            is_neutral_setup = True
            required_rr = 3.0

            if structure_state.get("recent_choch", 0) == 0:
                result.status = "NO_SIGNAL"
                result.reasons.append(f"{ReasonCode.HTF_NEUTRAL}: Missing LTF CHoCH")
                return result

            if not fvg_state.get("fvgs"):
                result.status = "NO_SIGNAL"
                result.reasons.append(f"{ReasonCode.HTF_NEUTRAL}: Missing LTF FVG")
                return result

            bias = "bullish" if structure_state.get("is_uptrend") else "bearish"
            result.reasons.append(f"NEUTRAL_OVERRIDE→{bias.upper()} via LTF Structure")

        elif bias_strength < 0.5:
            result.status = "NO_SIGNAL"
            result.reasons.append(f"{ReasonCode.WEAK_BIAS} ({bias_strength:.2f})")
            return result

        # ── Step 2: LTF trigger (5m) ─────────────
        trigger_type = None
        is_ltf_trigger_bypassed = False
        is_counter_trend = False
        
        if ltf_trigger is not None:
            if not ltf_trigger.get("confirmed"):
                reason = ltf_trigger.get("reason", ReasonCode.NO_LTF_TRIGGER)
                
                # ── LTF Trigger Bypass Check ─────────────
                # Allow bypass if valid entry zone exists AND bypass flag enabled
                if self.allow_no_ltf_trigger_with_zone:
                    import os
                    has_bos = structure_state.get("recent_bos", 0) > 0 or structure_state.get("recent_choch", 0) > 0
                    all_obs = ob_state.get("blocks", [])
                    all_fvgs = fvg_state.get("fvgs", [])
                    
                    zone_tol_pct = float(os.getenv("ZONE_TOL_PCT", "0.0015"))
                    is_in_zone = False
                    zone_type_hit = None
                    
                    # Fresh zones aligned with bias
                    fresh_obs = [
                        ob for ob in all_obs
                        if ob.get("age_bars", 999) <= self.sweep_fallback_max_ob_age
                        and ((bias == "bullish" and ob.get("type") == "bullish")
                             or (bias == "bearish" and ob.get("type") == "bearish"))
                    ]
                    fresh_fvgs = [
                        fvg for fvg in all_fvgs
                        if fvg.get("age_bars", 999) <= self.sweep_fallback_max_fvg_age
                        and ((bias == "bullish" and fvg.get("type") == "bullish")
                             or (bias == "bearish" and fvg.get("type") == "bearish"))
                    ]
                    
                    # Check if price is in OB zone
                    for ob in fresh_obs:
                        ob_high = ob.get("top", ob.get("high", ob.get("level", 0)))
                        ob_low = ob.get("bottom", ob.get("low", ob.get("level", 0)))
                        # Fix single level OBs
                        if ob_high == ob_low:
                            ob_high = ob_high * 1.0005
                            ob_low = ob_low * 0.9995
                        if ob_low * (1 - zone_tol_pct) <= current_price <= ob_high * (1 + zone_tol_pct):
                            is_in_zone = True
                            zone_type_hit = "OB"
                            break
                            
                    # Check if price is in FVG zone
                    if not is_in_zone:
                        for fvg in fresh_fvgs:
                            fvg_high = fvg.get("top", 0)
                            fvg_low = fvg.get("bottom", 0)
                            if fvg_low * (1 - zone_tol_pct) <= current_price <= fvg_high * (1 + zone_tol_pct):
                                is_in_zone = True
                                zone_type_hit = "FVG"
                                break
                    
                    # Bypass allowed if: BOS confirmed + price genuinely in zone
                    if has_bos and is_in_zone:
                        logger.info(
                            f"[LTF_TRIGGER] bypass=1 allow_no_ltf_with_zone=1 zone_hit=1 lookback={self.ltf_trigger_lookback}"
                        )
                        is_ltf_trigger_bypassed = True
                        trigger_type = f"zone_bypass_{zone_type_hit.lower()}"
                        result.reasons.append(
                            f"LTF_TRIGGER_BYPASS: {zone_type_hit}+BOS (no_ltf_trigger, RR>={self.no_ltf_trigger_min_rr})"
                        )
                    else:
                        missing_parts = []
                        if not has_bos:
                            missing_parts.append("no_bos")
                        if not is_in_zone:
                            missing_parts.append("not_in_zone")
                            
                        logger.info(
                            f"[LTF_TRIGGER] rejected: {', '.join(missing_parts)} (no_retest / no_displacement / no_engulf)"
                        )
                        result.status = "NO_SIGNAL"
                        result.reasons.append(f"{reason} (bypass_failed: {', '.join(missing_parts)})")
                        return result
                else:
                    # No bypass allowed - standard reject
                    logger.info(
                        f"[SIGNAL_REJECTED] {symbol} | reason={reason}"
                        f" | hint: enable ALLOW_NO_LTF_TRIGGER_WITH_ZONE=1 to allow zone-based bypass"
                    )
                    result.status = "NO_SIGNAL"
                    result.reasons.append(str(reason))
                    return result
            else:
                trigger_type = ltf_trigger.get("trigger_type", "unknown")

            # Counter-trend guard via trigger direction (skip if bypassed)
            if not is_ltf_trigger_bypassed:
                trigger_dir = ltf_trigger.get("direction")
                expected_dir = "long" if bias == "bullish" else "short"
                is_counter_trend = False

                if trigger_dir != expected_dir:
                    if not self.allow_countertrend:
                        result.status = "REJECTED"
                        result.reasons.append(
                            f"{ReasonCode.COUNTERTREND_BLOCKED}"
                            f" | htf={bias.upper()} vs trigger={trigger_dir}"
                        )
                        logger.info(
                            f"[FILTER] {symbol} | REJECTED"
                            f" | reason={ReasonCode.COUNTERTREND_BLOCKED}"
                            f" | htf_bias={bias.upper()}"
                            f" | trigger={trigger_dir}"
                        )
                        return result
                    else:
                        # SMART CT: Allow but maintain strict RR
                        is_counter_trend = True
                        result.reasons.append(f"COUNTER_TREND_ALLOWED (htf={bias}, trigger={trigger_dir})")

        # ── Step 3: Liquidity sweep ──────────────
        recent_sweeps = liquidity_state.get("sweep_history", [])
        is_fallback_mode = False
        
        # Log sweep check details for debugging
        session_high = liquidity_state.get("session_high", 0)
        session_low = liquidity_state.get("session_low", 0)
        atr = liquidity_state.get("atr", 0)
        min_wick = liquidity_state.get("min_wick_threshold", 0)
        
        recent_sweeps = liquidity_state.get("sweep_history", [])
        
        logger.debug(
            f"[SWEEP_STATE] {symbol} | sweeps={len(recent_sweeps)}"
            f" | session_high={session_high:.2f} | session_low={session_low:.2f}"
            f" | atr={atr:.4f} | min_wick={min_wick:.4f}"
        )
        
        # Track soft bypass state for RR enforcement
        is_soft_bypass = False
        
        if not recent_sweeps:
            # No sweep detected - check bypass/fallback modes
            
            # 1) FORCE_EXECUTE: brutal bypass (dangerous, not recommended)
            if self.force_execute:
                logger.warning(
                    f"[FORCE_EXECUTE] {symbol} | bypassed_reason=NO_LIQUIDITY_SWEEP"
                    f" | session_high={session_high:.2f} | session_low={session_low:.2f}"
                    f" | action=PROCEEDING_WITHOUT_SWEEP"
                )
                is_fallback_mode = True
                result.reasons.append("FORCE_EXECUTE: bypassed_reason=NO_LIQUIDITY_SWEEP")
            
            # 2) ALLOW_NO_SWEEP_WITH_OB / ALLOW_NO_SWEEP_WITH_FVG / ALLOW_NO_SWEEP_WITH_ZONE
            elif self.allow_no_sweep_with_ob or self.allow_no_sweep_with_fvg or self.allow_no_sweep_with_zone:
                # Check 1: BOS/CHoCH confirmed?
                has_bos = structure_state.get("recent_bos", 0) > 0 or structure_state.get("recent_choch", 0) > 0
                
                # Check 2: Valid zones present?
                all_obs = ob_state.get("blocks", [])
                all_fvgs = fvg_state.get("fvgs", [])
                
                # Filter OBs by bias and freshness
                valid_obs = []
                if self.allow_no_sweep_with_ob or self.allow_no_sweep_with_zone:
                    for ob in all_obs:
                        # Direction match
                        if not ((bias == "bullish" and ob.get("type") == "bullish") or 
                                (bias == "bearish" and ob.get("type") == "bearish")):
                            continue
                        
                        # Freshness check (if enforced)
                        if self.no_sweep_require_fresh_ob:
                             if ob.get("age_bars", 999) > self.sweep_fallback_max_ob_age:
                                 continue
                        
                        valid_obs.append(ob)
                
                # Filter FVGs by bias and freshness
                valid_fvgs = []
                if self.allow_no_sweep_with_fvg or self.allow_no_sweep_with_zone:
                    for fvg in all_fvgs:
                        # Direction match
                        if not ((bias == "bullish" and fvg.get("type") == "bullish") or 
                                (bias == "bearish" and fvg.get("type") == "bearish")):
                            continue
                        
                        # Freshness check
                        if fvg.get("age_bars", 999) > self.sweep_fallback_max_fvg_age:
                            continue
                        
                        valid_fvgs.append(fvg)

                is_htf_blocked = False
                trigger_dir = ltf_trigger.get("direction") if ltf_trigger else None
                expected_dir = "long" if bias == "bullish" else "short"
                
                if self.no_sweep_block_strong_htf and trigger_dir and trigger_dir != expected_dir:
                     if bias_strength >= 0.7:
                         is_htf_blocked = True

                has_valid_zone = bool(valid_obs) or bool(valid_fvgs)
                zone_type = "OB" if valid_obs else ("FVG" if valid_fvgs else "none")
                
                # CHECK 3: Price in Zone (for ZONE bypass)
                import os
                zone_tol_pct = float(os.getenv("ZONE_TOL_PCT", "0.0010"))
                price_in_zone = False
                zone_hit_mode = "none"
                if self.allow_no_sweep_with_zone and has_valid_zone:
                    for ob in valid_obs:
                        top = ob.get("top", ob.get("high", 0))
                        bottom = ob.get("bottom", ob.get("low", 0))
                        if top == bottom:  # single level fallback
                            top *= 1.0005
                            bottom *= 0.9995
                        if bottom * (1 - zone_tol_pct) <= current_price <= top * (1 + zone_tol_pct):
                            price_in_zone = True
                            zone_hit_mode = "OB"
                            break
                    if not price_in_zone:
                        for fvg in valid_fvgs:
                            top = getattr(fvg, "top", fvg.get("top", 0))
                            bottom = getattr(fvg, "bottom", fvg.get("bottom", 0))
                            if bottom * (1 - zone_tol_pct) <= current_price <= top * (1 + zone_tol_pct):
                                price_in_zone = True
                                zone_hit_mode = "FVG"
                                break

                # Approve bypass if conditions are met
                if has_bos and not is_htf_blocked and (
                    (self.allow_no_sweep_with_zone and price_in_zone) or 
                    (has_valid_zone and not self.allow_no_sweep_with_zone)
                ):
                    if self.allow_no_sweep_with_zone:
                        logger.info(
                            f"[DEFAULT_STRAT] bypass=1 reason=ZONE_ENTRY_NO_SWEEP mode={zone_hit_mode}"
                        )
                    else:
                        logger.info(
                            f"[FALLBACK] {symbol} | bypass_sweep=1"
                            f" | reason=BOS+{zone_type}"
                            f" | bos={has_bos} | fresh_obs={len(valid_obs)} | fresh_fvgs={len(valid_fvgs)}"
                            f" | bias={bias} ({bias_strength:.2f})"
                            f" | RR_required>={self.no_sweep_min_rr}"
                        )
                    is_fallback_mode = True
                    is_soft_bypass = True
                    # To reduce risk as requested, we encode it in setup_type later
                    if self.allow_no_sweep_with_zone:
                        result.reasons.append(f"SWEEP_ZONE_BYPASS: BOS+{zone_hit_mode} (price_in_zone, risk_reduced)")
                    else:
                        result.reasons.append(f"SWEEP_SOFT_BYPASS: BOS+{zone_type} (RR>={self.no_sweep_min_rr})")
                
                else:
                    # Log explicit rejection details
                    reasons = []
                    if not has_bos: reasons.append("no_bos")
                    if not has_valid_zone: reasons.append("no_valid_zone")
                    if self.allow_no_sweep_with_zone and not price_in_zone and has_valid_zone:
                         reasons.append("price_not_in_zone")
                    if is_htf_blocked: reasons.append(f"strong_htf_block({bias_strength:.2f})")
                    
                    logger.info(
                        f"[DEFAULT_STRAT] {symbol} reject_detail: sweep=0 bos={1 if has_bos else 0} obs={len(valid_obs)} fvgs={len(valid_fvgs)} "
                        f"price_in_zone={1 if price_in_zone else 0} zone_tol={zone_tol_pct} reasons={','.join(reasons)}"
                    )
                    
                    logger.info(
                        f"[FALLBACK] {symbol} | bypass_sweep=0"
                        f" | reason={', '.join(reasons)}"
                        f" | bos={has_bos} | fresh_obs={len(valid_obs)} | fresh_fvgs={len(valid_fvgs)}"
                        f" | hint: check ALLOW_NO_SWEEP_WITH_ZONE or ZONE_TOL_PCT"
                    )
            
            # 3) SWEEP_FALLBACK: standard fallback mode (BOS+FVG or BOS+OB with stricter RR)
            if not is_fallback_mode and self.sweep_fallback_enabled:
                # Check fallback conditions: BOS confirmed + fresh FVG (or OB if allowed)
                has_bos = structure_state.get("recent_bos", 0) > 0 or structure_state.get("recent_choch", 0) > 0
                
                # Get all FVGs/OBs for diagnostic
                all_fvgs = fvg_state.get("fvgs", [])
                all_obs = ob_state.get("blocks", [])
                
                fresh_fvgs = [
                    fvg for fvg in all_fvgs
                    if fvg.get("age_bars", 999) <= self.sweep_fallback_max_fvg_age
                    and ((bias == "bullish" and fvg.get("type") == "bullish") 
                         or (bias == "bearish" and fvg.get("type") == "bearish"))
                ]
                
                # Check for fresh Order Blocks (separate age threshold, more permissive)
                fresh_obs = [
                    ob for ob in all_obs
                    if ob.get("age_bars", 999) <= self.sweep_fallback_max_ob_age
                    and ((bias == "bullish" and ob.get("type") == "bullish")
                         or (bias == "bearish" and ob.get("type") == "bearish"))
                ]
                
                # Log diagnostics when no zones found
                if not fresh_fvgs and not fresh_obs:
                    # Find oldest available zones for hint
                    matching_fvgs = [f for f in all_fvgs if (bias == "bullish" and f.get("type") == "bullish") or (bias == "bearish" and f.get("type") == "bearish")]
                    matching_obs = [o for o in all_obs if (bias == "bullish" and o.get("type") == "bullish") or (bias == "bearish" and o.get("type") == "bearish")]
                    
                    oldest_fvg_age = min([f.get("age_bars", 999) for f in matching_fvgs], default=999)
                    oldest_ob_age = min([o.get("age_bars", 999) for o in matching_obs], default=999)
                    
                    logger.debug(
                        f"[ZONE_DIAG] {symbol} | total_fvgs={len(all_fvgs)} total_obs={len(all_obs)}"
                        f" | matching_{bias}_fvgs={len(matching_fvgs)} matching_{bias}_obs={len(matching_obs)}"
                        f" | oldest_fvg_age={oldest_fvg_age} (max={self.sweep_fallback_max_fvg_age})"
                        f" | oldest_ob_age={oldest_ob_age} (max={self.sweep_fallback_max_ob_age})"
                    )
                
                # Primary fallback: BOS + FVG
                if has_bos and fresh_fvgs:
                    logger.info(
                        f"[SWEEP_FALLBACK] {symbol} | NO_SWEEP but BOS+FVG detected"
                        f" | bos={has_bos} | fresh_fvgs={len(fresh_fvgs)}"
                        f" | using stricter RR={self.sweep_fallback_min_rr}"
                    )
                    is_fallback_mode = True
                    result.reasons.append(f"FALLBACK_MODE: BOS+FVG (no sweep, RR>={self.sweep_fallback_min_rr})")
                
                # Secondary fallback: BOS + OB (when no FVG and FALLBACK_ALLOW_NO_FVG=True)
                elif has_bos and fresh_obs and self.fallback_allow_no_fvg:
                    logger.info(
                        f"[FALLBACK_BOS_OB] {symbol} | NO_SWEEP, no fresh FVG, but BOS+OB detected"
                        f" | bos={has_bos} | fresh_obs={len(fresh_obs)} | fresh_fvgs=0"
                        f" | using stricter RR={self.sweep_fallback_min_rr}"
                    )
                    is_fallback_mode = True
                    result.reasons.append(f"FALLBACK_MODE: BOS+OB (no sweep/fvg, RR>={self.sweep_fallback_min_rr})")
                
                else:
                    # No valid fallback conditions met
                    # Suggest better alternatives based on available zones
                    hint = "enable ALLOW_NO_SWEEP_WITH_OB=1 (safe bypass)"
                    if has_bos and fresh_obs and not self.allow_no_sweep_with_ob:
                        hint = "enable ALLOW_NO_SWEEP_WITH_OB=1 to use available OBs"
                    elif not self.fallback_allow_no_fvg and has_bos and fresh_obs:
                        hint = "enable FALLBACK_ALLOW_NO_FVG=1 or ALLOW_NO_SWEEP_WITH_OB=1"
                    
                    logger.info(
                        f"[SIGNAL_REJECTED] {symbol} | reason=NO_LIQUIDITY_SWEEP"
                        f" | session_high={session_high:.2f} | session_low={session_low:.2f}"
                        f" | fallback_check: bos={has_bos} fresh_fvgs={len(fresh_fvgs)} fresh_obs={len(fresh_obs)}"
                        f" | hint: {hint}"
                    )
                    result.status = "NO_SIGNAL"
                    result.reasons.append(
                        f"{ReasonCode.NO_LIQUIDITY_SWEEP}"
                        f" (session_H={session_high:.1f} L={session_low:.1f}, fallback_failed)"
                    )
                    return result
            
            # If no bypass/fallback mode was activated, reject
            if not is_fallback_mode:
                logger.info(
                    f"[SIGNAL_REJECTED] {symbol} | reason=NO_LIQUIDITY_SWEEP"
                    f" | session_high={session_high:.2f} | session_low={session_low:.2f}"
                    f" | fallback=disabled or conditions not met"
                )
                result.status = "NO_SIGNAL"
                result.reasons.append(str(ReasonCode.NO_LIQUIDITY_SWEEP))
                return result
        else:
            # Sweep detected - validate alignment with bias
            last_sweep = recent_sweeps[-1]
            sweep_type = last_sweep.get("type")
            sweep_wick = last_sweep.get("wick_size", 0)
            
            logger.info(
                f"[SWEEP_DETECTED] {symbol} | type={sweep_type}"
                f" | price={last_sweep.get('price', 0):.2f}"
                f" | wick={sweep_wick:.4f} | displacement={last_sweep.get('displacement', 0):.4f}"
            )

            if bias == "bullish" and sweep_type != "sellside":
                result.status = "REJECTED"
                result.reasons.append(
                    f"{ReasonCode.SWEEP_MISMATCH} (bias=BULL sweep={sweep_type}, need=sellside)"
                )
                return result
            if bias == "bearish" and sweep_type != "buyside":
                result.status = "REJECTED"
                result.reasons.append(
                    f"{ReasonCode.SWEEP_MISMATCH} (bias=BEAR sweep={sweep_type}, need=buyside)"
                )
                return result

        # ── Step 4: Entry zone (OB / FVG) ────────
        # ── Step 4: Entry zone (OB / FVG) ────────
        entry_zone = self._find_entry_zone(bias, ob_state, fvg_state)
        is_atr_fallback_zone = False
        
        if not entry_zone:
            # ── Enhanced diagnostic for NO_ENTRY_ZONE ──
            atr = liquidity_state.get("atr", 0)
            all_obs = ob_state.get("blocks", [])
            all_fvgs = fvg_state.get("fvgs", [])
            ob_count = len(all_obs)
            fvg_count = len(all_fvgs)
            
            # Count matching zones by bias
            matching_obs = [o for o in all_obs if o.get("type") == bias]
            matching_fvgs = [f for f in all_fvgs if f.get("type") == bias]
            
            # Analyze why zones are filtered out (Counts)
            zone_rejection_reasons = []
            
            # OB Analysis
            if ob_count > 0 and len(matching_obs) == 0:
                zone_rejection_reasons.append(f"obs_wrong_type({ob_count})")
            elif matching_obs:
                stale_obs = len([o for o in matching_obs if o.get("age_bars", 0) > self.sweep_fallback_max_ob_age])
                if stale_obs > 0:
                    zone_rejection_reasons.append(f"obs_stale({stale_obs}/{len(matching_obs)} > {self.sweep_fallback_max_ob_age})")
            
            # FVG Analysis
            if fvg_count > 0 and len(matching_fvgs) == 0:
                zone_rejection_reasons.append(f"fvgs_wrong_type({fvg_count})")
            elif matching_fvgs:
                stale_fvgs = len([f for f in matching_fvgs if f.get("age_bars", 0) > self.sweep_fallback_max_fvg_age])
                mitigated_fvgs = len([f for f in matching_fvgs if f.get("mitigated")])
                
                if stale_fvgs > 0:
                    zone_rejection_reasons.append(f"fvgs_stale({stale_fvgs}/{len(matching_fvgs)} > {self.sweep_fallback_max_fvg_age})")
                
                if mitigated_fvgs > 0 and not self.entry_zone_nonfresh:
                    zone_rejection_reasons.append(f"fvgs_mitigated({mitigated_fvgs}/{len(matching_fvgs)})")

            rejection_hint = ", ".join(zone_rejection_reasons) if zone_rejection_reasons else "no_valid_zones"
            
            logger.info(
                f"[NO_ENTRY_ZONE] {symbol} | bias={bias}"
                f" | price={current_price:.2f} | atr={atr:.4f}"
                f" | obs_total={ob_count} obs_matching={len(matching_obs)}"
                f" | fvgs_total={fvg_count} fvgs_matching={len(matching_fvgs)}"
                f" | rejection_hint=[{rejection_hint}]"
                f" | force_entry={self.force_entry_zone} | fallback_mode={self.entry_fallback_mode}"
            )
            
            # ── OPTION 1: FORCE_ENTRY_ZONE bypass (brute force) ──
            if self.force_entry_zone:
                # Create ATR-based synthetic entry zone
                entry_zone = self._build_atr_fallback_zone(
                    current_price=current_price,
                    atr=atr,
                    bias=bias,
                    zone_mult=self.entry_fallback_atr_mult,
                    sl_mult=self.entry_fallback_sl_atr_mult,
                )
                is_atr_fallback_zone = True
                
                logger.warning(
                    f"[FORCE_ENTRY_ZONE] {symbol} | bypassed_reason=NO_ENTRY_ZONE"
                    f" | bias={bias} | price={current_price:.2f}"
                    f" | zone_low={entry_zone.get('zone_low', 0):.2f}"
                    f" | zone_high={entry_zone.get('zone_high', 0):.2f}"
                    f" | sl_offset={entry_zone.get('sl_offset', 0):.4f}"
                )
                result.reasons.append(
                    f"FORCE_ENTRY_ZONE: bypassed_reason=NO_ENTRY_ZONE bias={bias} "
                    f"zone=[{entry_zone.get('zone_low', 0):.2f}-{entry_zone.get('zone_high', 0):.2f}]"
                )
            
            # ── OPTION 2: ENTRY_FALLBACK_MODE (intelligent, requires bias+BOS) ──
            elif self.entry_fallback_mode:
                # Check conditions: bias defined (not neutral) AND structure confirms
                has_bos = structure_state.get("recent_bos", 0) > 0 or structure_state.get("recent_choch", 0) > 0
                structure_strength = structure_state.get("structure_strength", 0)
                
                # Fallback allowed if: solid bias + (BOS or strong structure)
                fallback_allowed = (bias != "neutral") and (has_bos or structure_strength >= 0.6)
                
                if fallback_allowed:
                    entry_zone = self._build_atr_fallback_zone(
                        current_price=current_price,
                        atr=atr,
                        bias=bias,
                        zone_mult=self.entry_fallback_atr_mult,
                        sl_mult=self.entry_fallback_sl_atr_mult,
                    )
                    is_atr_fallback_zone = True
                    
                    logger.info(
                        f"[ENTRY_FALLBACK] {symbol} | bias={bias} | bos={has_bos}"
                        f" | structure_strength={structure_strength:.2f}"
                        f" | using_atr_zone | price={current_price:.2f}"
                        f" | zone=[{entry_zone.get('zone_low', 0):.2f}-{entry_zone.get('zone_high', 0):.2f}]"
                        f" | rr_min_required={self.entry_fallback_rr_min}"
                    )
                    result.reasons.append(
                        f"ENTRY_FALLBACK: ATR zone (no OB/FVG, bos={has_bos}) "
                        f"RR>={self.entry_fallback_rr_min}"
                    )
                else:
                    logger.info(
                        f"[ENTRY_FALLBACK_DENIED] {symbol} | bias={bias}"
                        f" | has_bos={has_bos} | structure_strength={structure_strength:.2f}"
                        f" | reason=insufficient_confirmation"
                    )
                    result.status = "NO_SIGNAL"
                    result.reasons.append(
                        f"{ReasonCode.NO_ENTRY_ZONE} (bias={bias}, "
                        f"fallback_denied: bos={has_bos}, strength={structure_strength:.2f})"
                    )
                    return result
            
            # ── OPTION 3: No bypass, strict rejection ──
            else:
                result.status = "NO_SIGNAL"
                result.reasons.append(
                    f"{ReasonCode.NO_ENTRY_ZONE} (bias={bias}, hint={rejection_hint})"
                )
                return result

        # ── Step 5: Build signal params ──────────
        signal = None
        if bias == "bullish":
            signal = self._build_bullish_signal(
                current_price, entry_zone, liquidity_state,
                structure_state, bias_strength, timestamp, trigger_type,
            )
        else:
            signal = self._build_bearish_signal(
                current_price, entry_zone, liquidity_state,
                structure_state, bias_strength, timestamp, trigger_type,
            )

        if not signal:
            result.status = "REJECTED"
            result.reasons.append(str(ReasonCode.SIGNAL_BUILD_FAILED))
            return result

        if is_neutral_setup:
            signal.mtf_bias = "neutral_override"
        
        if is_fallback_mode:
            signal.setup_type = f"FALLBACK_{signal.setup_type}"
            
        if any("SWEEP_ZONE_BYPASS" in r for r in result.reasons):
            signal.confidence_score *= 0.85
            signal.setup_type = signal.setup_type.replace("FALLBACK_", "ZONE_BYPASS_")

        # Tag ATR fallback signals distinctly
        if is_atr_fallback_zone:
            signal.setup_type = f"ATR_FALLBACK_{signal.setup_type}" if not signal.setup_type.startswith("ATR") else signal.setup_type

        # ── Step 5b: Liquidity Heatmap Boost ─────
        heatmap = liquidity_state.get("heatmap")
        if heatmap and signal:
            target_above = heatmap.get("major_target_above")
            target_below = heatmap.get("major_target_below")
            direction_val = signal.direction.value

            # Long → TP towards sellside liquidity above
            if direction_val == "long" and target_above:
                score = target_above.get("score", 0)
                if score >= 6.0:
                    signal.confidence_score = min(1.0, signal.confidence_score + 0.15)
                    signal.liquidity_trigger = f"HEATMAP_TARGET_ABOVE:{target_above['price']}"
                    result.reasons.append(f"HEATMAP_BOOST(above={target_above['price']},score={score:.1f})")
                elif score >= 3.0:
                    signal.confidence_score = min(1.0, signal.confidence_score + 0.05)

            # Short → TP towards buyside liquidity below
            elif direction_val == "short" and target_below:
                score = target_below.get("score", 0)
                if score >= 6.0:
                    signal.confidence_score = min(1.0, signal.confidence_score + 0.15)
                    signal.liquidity_trigger = f"HEATMAP_TARGET_BELOW:{target_below['price']}"
                    result.reasons.append(f"HEATMAP_BOOST(below={target_below['price']},score={score:.1f})")
                elif score >= 3.0:
                    signal.confidence_score = min(1.0, signal.confidence_score + 0.05)

        # ── Step 6: R:R validation ───────────────
        # Determine required RR based on mode
        final_required_rr = required_rr
        if is_fallback_mode and not self.force_execute:
            if is_soft_bypass:
                # Soft bypass (BOS+OB): use no_sweep_min_rr
                final_required_rr = max(required_rr, self.no_sweep_min_rr)
            else:
                # Standard sweep fallback: stricter RR (default 2.0)
                final_required_rr = max(required_rr, self.sweep_fallback_min_rr)
        
        if is_counter_trend:
             # Counter-trend requires stricter RR
             final_required_rr = max(final_required_rr, self.countertrend_min_rr)
        
        # ATR fallback also requires stricter R:R (even with force_entry)
        if is_atr_fallback_zone:
            final_required_rr = max(final_required_rr, self.entry_fallback_rr_min)
        
        # Use epsilon to avoid false rejections like 1.9999999 < 2.0
        if signal.risk_reward_ratio >= (final_required_rr - RR_EPSILON):
            self.last_signal = signal
            self.signal_history.append(signal)
            if len(self.signal_history) > 200:
                self.signal_history = self.signal_history[-100:]

            result.signal = signal
            result.status = "CREATED"

            mode_tag = ""
            if is_soft_bypass:
                mode_tag = "[SOFT_BYPASS]"
            elif is_fallback_mode:
                mode_tag = "[FALLBACK]"
            logger.info(
                f"[SIGNAL] {symbol} | FOUND {mode_tag}"
                f" | direction={signal.direction.value.upper()}"
                f" | entry_zone={entry_zone.get('type')}@{entry_zone.get('price', 0):.2f}"
                f" | trigger={trigger_type or '15m_close'}"
                f" | rr={signal.risk_reward_ratio:.2f} (req={final_required_rr})"
                f" | confidence={signal.confidence_score:.2f}"
            )
            return result
        else:
            result.status = "REJECTED"
            result.reasons.append(
                f"{ReasonCode.LOW_RR}"
                f" ({signal.risk_reward_ratio:.2f} < {final_required_rr})"
                f" [neutral={is_neutral_setup}, fallback={is_fallback_mode}]"
            )
            return result

    # ──────────────────────────────────────────
    #  Entry zone finder
    # ──────────────────────────────────────────

    def _find_entry_zone(
        self, bias: str, ob_state: Dict, fvg_state: Dict,
    ) -> Optional[Dict]:
        """Find nearest valid entry zone (OB or FVG)."""
        zones: List[Dict] = []

        # ── Order Block Filtering ──
        for ob in ob_state.get("blocks", []):
            # 1. Bias match
            if not ((bias == "bullish" and ob.get("type") == "bullish") or 
                    (bias == "bearish" and ob.get("type") == "bearish")):
                continue
                
            # 2. Age check
            age = ob.get("age_bars", 0)
            if age > self.sweep_fallback_max_ob_age:
                # Log only once per loop to avoid spam if needed, or debug level
                # logger.debug(f"[ZONE_SKIP] OB stale age={age} > {self.sweep_fallback_max_ob_age}")
                continue

            zones.append({
                "type": "order_block",
                "price": ob.get("low") if bias == "bullish" else ob.get("high"),
                "confidence": ob.get("confidence", 0.5),
                "freshness": max(0.1, 1.0 - (age / (self.sweep_fallback_max_ob_age + 1))),
                "age": age
            })

        # ── FVG Filtering ──
        for fvg in fvg_state.get("fvgs", []):
            # 1. Bias match
            if not ((bias == "bullish" and fvg.get("type") == "bullish") or 
                    (bias == "bearish" and fvg.get("type") == "bearish")):
                continue
                
            # 2. Mitigation check
            if fvg.get("mitigated") and not self.entry_zone_nonfresh:
                continue
                
            # 3. Age check
            age = fvg.get("age_bars", 0)
            if age > self.sweep_fallback_max_fvg_age:
                continue

            zones.append({
                "type": "fvg",
                "price": fvg.get("bottom") if bias == "bullish" else fvg.get("top"),
                "confidence": 0.7,
                "freshness": max(0.1, 1.0 - (age / (self.sweep_fallback_max_fvg_age + 1))),
                "age": age,
                "mitigated": fvg.get("mitigated", False)
            })

        if not zones:
            return None
        # FM-4: guard against None prices from malformed OB/FVG data
        zones = [z for z in zones if z.get("price") is not None and z["price"] > 0]
        if not zones:
            return None
            
        # Optional: Filter by distance if max_dist is set (and valid ATR)
        # Note: current_price isn't passed here, so we skip exact distance check for selection
        # Instead we rely on the builder to validate reachability or padding.
        
        return max(zones, key=lambda z: z["confidence"] * z["freshness"])

    # ──────────────────────────────────────────
    #  ATR Fallback Zone Builder
    # ──────────────────────────────────────────

    def _build_atr_fallback_zone(
        self,
        current_price: float,
        atr: float,
        bias: str,
        zone_mult: float = 0.25,
        sl_mult: float = 1.5,
    ) -> Dict:
        """
        Build a synthetic entry zone based on ATR when no OB/FVG is available.
        
        Args:
            current_price: Current market price (entry point)
            atr: Average True Range for volatility scaling
            bias: 'bullish' or 'bearish' - determines SL/TP direction
            zone_mult: ATR multiplier for zone width (+/- zone_mult * ATR)
            sl_mult: ATR multiplier for stop loss distance
        
        Returns:
            Dict with zone parameters including computed SL/TP offsets
        """
        # Ensure ATR is valid (minimum 0.01% of price)
        if atr <= 0:
            atr = current_price * 0.001  # Fallback: 0.1% of price
        
        # Zone width: +/- zone_mult * ATR
        zone_offset = zone_mult * atr
        zone_low = current_price - zone_offset
        zone_high = current_price + zone_offset
        
        # Stop loss offset: sl_mult * ATR from entry
        sl_offset = sl_mult * atr
        
        # Compute SL and TP based on bias
        if bias == "bullish":
            sl_price = current_price - sl_offset
            # TP at minimum R:R (will be re-calculated in signal builder for exact R:R)
            tp_offset = sl_offset * self.entry_fallback_rr_min
            tp_price = current_price + tp_offset
        else:  # bearish
            sl_price = current_price + sl_offset
            tp_offset = sl_offset * self.entry_fallback_rr_min
            tp_price = current_price - tp_offset
        
        return {
            "type": "ATR_FALLBACK",
            "price": current_price,  # Entry at current price
            "zone_low": zone_low,
            "zone_high": zone_high,
            "sl_offset": sl_offset,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "confidence": 0.4,  # Lower confidence than OB/FVG zones
            "freshness": 1.0,   # Freshly created
            "is_atr_fallback": True,
        }

    # ──────────────────────────────────────────
    #  Signal builders
    # ──────────────────────────────────────────

    def _build_bullish_signal(
        self,
        current_price: float,
        entry_zone: Dict,
        liquidity_state: Dict,
        structure_state: Dict,
        bias_strength: float,
        timestamp: int,
        trigger_type: Optional[str] = None,
    ) -> Optional[SMCSignal]:
        entry_price = entry_zone["price"]
        
        # Check if price broke invalidation level (with padding)
        atr = liquidity_state.get("atr", 0)
        padding = self.entry_zone_padding * atr
        
        # ATR Fallback zones don't need price validation (entry IS current price)
        is_atr_fallback = entry_zone.get("is_atr_fallback", False)
        
        if not is_atr_fallback:
            # Normal check: if current_price < entry_price: return None
            # Relaxed check: if current_price < entry_price - padding: return None
            if current_price < (entry_price - padding):
                # Price fell through the floor (plus padding buffer)
                return None

        # ── Compute SL / TP ──
        if is_atr_fallback:
            # Use pre-computed values from ATR fallback zone
            stop_loss = entry_zone.get("sl_price", entry_price * 0.97)
            take_profit = entry_zone.get("tp_price", entry_price * 1.03)
        else:
            # Standard OB/FVG logic
            session_low = liquidity_state.get("session_low", current_price * 0.98)
            stop_loss = min(session_low, entry_price * 0.97)
            
            risk = entry_price - stop_loss
            if risk <= 0:
                return None
            rr_mult = min(3.0, 1.5 + bias_strength)
            take_profit = entry_price + risk * rr_mult

        risk = entry_price - stop_loss
        if risk <= 0:
            return None
        rr_ratio = (take_profit - entry_price) / risk

        confidence = entry_zone.get("confidence", 0.5) * 0.6 + bias_strength * 0.4

        return SMCSignal(
            direction=SignalDirection.LONG,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            setup_type=entry_zone.get("type", "unknown"),
            trigger_type=trigger_type or "15m_close",
            liquidity_trigger="buyside_sweep",
            confidence_score=confidence,
            risk_reward_ratio=rr_ratio,
            invalidation_level=stop_loss,
            mtf_bias="bullish",
            structure_strength=structure_state.get("structure_strength", 0.5),
            timestamp=timestamp,
        )

    def _build_bearish_signal(
        self,
        current_price: float,
        entry_zone: Dict,
        liquidity_state: Dict,
        structure_state: Dict,
        bias_strength: float,
        timestamp: int,
        trigger_type: Optional[str] = None,
    ) -> Optional[SMCSignal]:
        entry_price = entry_zone["price"]
        
        # Check if price broke invalidation level (with padding)
        atr = liquidity_state.get("atr", 0)
        padding = self.entry_zone_padding * atr
        
        # ATR Fallback zones don't need price validation (entry IS current price)
        is_atr_fallback = entry_zone.get("is_atr_fallback", False)
        
        if not is_atr_fallback:
            # Normal check: if current_price > entry_price: return None
            # Relaxed check: if current_price > entry_price + padding: return None
            if current_price > (entry_price + padding):
                # Price broke through the ceiling (plus padding buffer)
                return None

        # ── Compute SL / TP ──
        if is_atr_fallback:
            # Use pre-computed values from ATR fallback zone
            stop_loss = entry_zone.get("sl_price", entry_price * 1.03)
            take_profit = entry_zone.get("tp_price", entry_price * 0.97)
        else:
            # Standard OB/FVG logic
            session_high = liquidity_state.get("session_high", current_price * 1.02)
            stop_loss = max(session_high, entry_price * 1.03)

            risk = stop_loss - entry_price
            if risk <= 0:
                return None
            rr_mult = min(3.0, 1.5 + bias_strength)
            take_profit = entry_price - risk * rr_mult

        risk = stop_loss - entry_price
        if risk <= 0:
            return None
        rr_ratio = (entry_price - take_profit) / risk

        confidence = entry_zone.get("confidence", 0.5) * 0.6 + bias_strength * 0.4

        return SMCSignal(
            direction=SignalDirection.SHORT,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            setup_type=entry_zone.get("type", "unknown"),
            trigger_type=trigger_type or "15m_close",
            liquidity_trigger="sellside_sweep",
            confidence_score=confidence,
            risk_reward_ratio=rr_ratio,
            invalidation_level=stop_loss,
            mtf_bias="bearish",
            structure_strength=structure_state.get("structure_strength", 0.5),
            timestamp=timestamp,
        )

    # ──────────────────────────────────────────
    #  Accessors
    # ──────────────────────────────────────────

    def get_last_signal(self) -> Optional[Dict]:
        """Get last signal as dict."""
        if not self.last_signal:
            return None
        s = self.last_signal
        return {
            "direction": s.direction.value,
            "entry": s.entry_price,
            "stop_loss": s.stop_loss,
            "take_profit": s.take_profit,
            "setup": s.setup_type,
            "trigger": s.trigger_type,
            "confidence": s.confidence_score,
            "rr_ratio": s.risk_reward_ratio,
            "invalidation": s.invalidation_level,
            "bias": s.mtf_bias,
            "timestamp": s.timestamp,
        }
