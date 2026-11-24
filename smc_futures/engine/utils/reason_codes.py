"""
Standardized Reason Codes for SMC Multi-TF Strategy.
Every rejection / filter / skip MUST reference one of these codes.
"""
from enum import Enum


class ReasonCode(Enum):
    # ── Market & Data ───────────────────────────────────
    MARKET_CLOSED          = "MARKET_CLOSED"
    INSUFFICIENT_DATA      = "INSUFFICIENT_DATA"
    HTF_DATA_MISSING       = "HTF_DATA_MISSING"
    HTF_INSUFFICIENT_DATA  = "HTF_INSUFFICIENT_DATA"

    # ── HTF / Bias ──────────────────────────────────────
    HTF_NEUTRAL            = "HTF_NEUTRAL"
    HTF_NEUTRAL_PROD       = "HTF_NEUTRAL_PROD"
    WEAK_BIAS              = "WEAK_BIAS"

    # ── Session ─────────────────────────────────────────
    SESSION_FILTER         = "SESSION_FILTER"
    SESSION_OUTSIDE        = "SESSION_OUTSIDE"

    # ── Counter-trend ───────────────────────────────────
    COUNTER_TREND          = "COUNTER_TREND"
    COUNTERTREND_BLOCKED   = "COUNTERTREND_BLOCKED"

    # ── MTF Setup (15m) ────────────────────────────────
    NO_SETUP               = "NO_SETUP"
    NO_ENTRY_ZONE          = "NO_ENTRY_ZONE"
    NO_POI                 = "NO_POI"

    # ── LTF Trigger (5m) ───────────────────────────────
    NO_LTF_TRIGGER         = "NO_LTF_TRIGGER"
    NO_5M_CHOCH            = "NO_5M_CHOCH"
    NO_5M_BOS              = "NO_5M_BOS"
    LTF_NO_CONFIRMATION    = "LTF_NO_CONFIRMATION"

    # ── Liquidity ───────────────────────────────────────
    NO_LIQUIDITY_SWEEP     = "NO_LIQUIDITY_SWEEP"
    SWEEP_MISMATCH         = "SWEEP_MISMATCH"
    MISSING_LTF_CONFIRMATION = "MISSING_LTF_CONFIRMATION"

    # ── Signal Validation ───────────────────────────────
    LOW_RR                 = "LOW_RR"
    SIGNAL_BUILD_FAILED    = "SIGNAL_BUILD_FAILED"

    # ── Execution & Risk ────────────────────────────────
    RISK_TOO_HIGH          = "RISK_TOO_HIGH"
    MAX_POSITIONS_REACHED  = "MAX_POSITIONS_REACHED"
    INSUFFICIENT_BALANCE   = "INSUFFICIENT_BALANCE"
    COOLDOWN_ACTIVE        = "COOLDOWN_ACTIVE"
    POSITION_ALREADY_OPEN  = "POSITION_ALREADY_OPEN"
    DUPLICATE_SIGNAL       = "DUPLICATE_SIGNAL"
    VALIDATION_FAILED      = "VALIDATION_FAILED"
    
    # ── System ──────────────────────────────────────────
    ERROR                  = "SYSTEM_ERROR"
    OK                     = "OK"

    def __str__(self) -> str:
        return self.value


class ExitAction(Enum):
    """
    Taxonomy for every exit point in the decision pipeline.
    SKIP   = no decision possible (missing data, warmup, no trigger)
    REJECT = deliberate negative decision (counter-trend, low R:R, sweep mismatch)
    BLOCK  = guard-rail protection (active trade, stale bias, cooldown, balance=0)
    PASS   = signal created → forwarded to next stage
    ERROR  = exception caught
    """
    SKIP    = "SKIP"
    REJECT  = "REJECT"
    BLOCK   = "BLOCK"
    PASS    = "PASS"
    ERROR   = "ERROR"

    def __str__(self) -> str:
        return self.value


class DecisionCategory(Enum):
    """
    High-level classification for Metrics & Alerting.
    Used to distinguish between 'Strategy is filtering' (REJECT) vs 'System not ready' (SKIP).
    """
    PASS             = "PASS"             # Good signal
    REJECT_STRATEGY  = "REJECT_STRATEGY"  # Strategy Logic (Filter)
    SKIP_NOT_READY   = "SKIP_NOT_READY"   # Pre-requisites not met
    BLOCK_GUARD      = "BLOCK_GUARD"      # Operational Guard-rail
    ERROR            = "ERROR"            # System Error


# ── Strict Mapping ──────────────────────────────────────
REASON_TO_CATEGORY = {
    # Not Ready / Data Issues
    ReasonCode.MARKET_CLOSED:          DecisionCategory.SKIP_NOT_READY,
    ReasonCode.INSUFFICIENT_DATA:      DecisionCategory.SKIP_NOT_READY,
    ReasonCode.HTF_DATA_MISSING:       DecisionCategory.SKIP_NOT_READY,
    ReasonCode.HTF_INSUFFICIENT_DATA:  DecisionCategory.SKIP_NOT_READY,
    
    # Session (Debatable, but usually SKIP)
    ReasonCode.SESSION_FILTER:         DecisionCategory.SKIP_NOT_READY,
    ReasonCode.SESSION_OUTSIDE:        DecisionCategory.SKIP_NOT_READY,

    # Valid Rejections (Strategy Filters)
    ReasonCode.HTF_NEUTRAL:            DecisionCategory.SKIP_NOT_READY, # Neutral = Wait (usually)
    ReasonCode.HTF_NEUTRAL_PROD:       DecisionCategory.SKIP_NOT_READY,
    ReasonCode.WEAK_BIAS:              DecisionCategory.REJECT_STRATEGY,
    ReasonCode.COUNTER_TREND:          DecisionCategory.REJECT_STRATEGY,
    ReasonCode.COUNTERTREND_BLOCKED:   DecisionCategory.REJECT_STRATEGY,
    ReasonCode.NO_SETUP:               DecisionCategory.SKIP_NOT_READY, # Just waiting
    ReasonCode.NO_ENTRY_ZONE:          DecisionCategory.SKIP_NOT_READY,
    ReasonCode.NO_POI:                 DecisionCategory.SKIP_NOT_READY,
    ReasonCode.NO_LTF_TRIGGER:         DecisionCategory.SKIP_NOT_READY,
    ReasonCode.NO_5M_CHOCH:            DecisionCategory.SKIP_NOT_READY, 
    ReasonCode.NO_5M_BOS:              DecisionCategory.SKIP_NOT_READY,
    ReasonCode.LTF_NO_CONFIRMATION:    DecisionCategory.SKIP_NOT_READY,
    ReasonCode.NO_LIQUIDITY_SWEEP:     DecisionCategory.REJECT_STRATEGY, # If setup was there but no sweep? Or just wait? Strategy usually requires sweep.
    ReasonCode.SWEEP_MISMATCH:         DecisionCategory.REJECT_STRATEGY,
    ReasonCode.MISSING_LTF_CONFIRMATION: DecisionCategory.REJECT_STRATEGY,
    ReasonCode.LOW_RR:                 DecisionCategory.REJECT_STRATEGY,
    ReasonCode.SIGNAL_BUILD_FAILED:    DecisionCategory.REJECT_STRATEGY,
    ReasonCode.VALIDATION_FAILED:      DecisionCategory.REJECT_STRATEGY,

    # Guard Rails
    ReasonCode.RISK_TOO_HIGH:          DecisionCategory.REJECT_STRATEGY,
    ReasonCode.MAX_POSITIONS_REACHED:  DecisionCategory.BLOCK_GUARD,
    ReasonCode.INSUFFICIENT_BALANCE:   DecisionCategory.BLOCK_GUARD,
    ReasonCode.COOLDOWN_ACTIVE:        DecisionCategory.BLOCK_GUARD,
    ReasonCode.POSITION_ALREADY_OPEN:  DecisionCategory.BLOCK_GUARD,
    ReasonCode.DUPLICATE_SIGNAL:       DecisionCategory.BLOCK_GUARD,
    
    # Success
    ReasonCode.OK:                     DecisionCategory.PASS,
    
    # Error
    ReasonCode.ERROR:                  DecisionCategory.ERROR,
}

def get_category(reason: str) -> DecisionCategory:
    """Resolve category from string or ReasonCode."""
    # 1. Try direct match if it's a ReasonCode enum
    if isinstance(reason, ReasonCode):
        return REASON_TO_CATEGORY.get(reason, DecisionCategory.ERROR)
    
    # 2. Try string lookup
    try:
        # Check if reason string matches an enum member
        rc = ReasonCode(reason)
        return REASON_TO_CATEGORY.get(rc, DecisionCategory.ERROR)
    except ValueError:
        pass
        
    # 3. Fallback for free-text reasons
    if "PASS" in reason or "OK" in reason: return DecisionCategory.PASS
    if "Insufficient" in reason or "Wait" in reason: return DecisionCategory.SKIP_NOT_READY
    
    return DecisionCategory.ERROR
