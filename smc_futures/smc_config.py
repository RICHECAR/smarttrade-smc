"""
SMC Multi-Timeframe Strategy Configuration
Loaded from .env with sensible defaults.
"""

import os
import sys
import logging
from core.env_loader import safe_load_dotenv

safe_load_dotenv()

logger = logging.getLogger(__name__)


def _bool(val: str) -> bool:
    return val.strip().lower() in ("true", "1", "yes")

def is_kraken_execution_enabled() -> bool:
    """Check if Kraken is explicitly enabled for real execution."""
    return _bool(os.getenv("KRAKEN_EXECUTION_ENABLED", "false"))


def _parse_multi_broker_mode(val: str) -> str:
    """
    Parse MULTI_BROKER_MODE: 'disabled' | 'fallback' | 'mirror' | 'split'.

    Modes:
      - 'disabled': Single-broker only (default)
      - 'fallback': Primary first, secondary only if primary fails
      - 'mirror':   Execute concurrently on all brokers (same qty)
      - 'split':    Execute concurrently, split qty across brokers (avoid double risk)
    """
    v = val.strip().lower()
    if v in ("disabled", "off", "false", "0", "no", ""):
        return "disabled"
    if v in ("fallback", "backup"):
        return "fallback"
    if v in ("mirror", "dual", "true", "1", "yes"):
        return "mirror"
    if v in ("split", "divide"):
        return "split"
    raise ValueError(
        f"CRITICAL CONFIG ERROR: MULTI_BROKER_MODE must be 'disabled', 'fallback', 'mirror' or 'split'. Got: '{val}'\n"
        f"  disabled = Single-broker only (Bitget)\n"
        f"  fallback = Primary first, secondary only if primary fails\n"
        f"  mirror   = Execute on all brokers concurrently (same qty)\n"
        f"  split    = Execute on all brokers, qty divided across brokers"
    )


# Parse MULTI_BROKER_MODE early (needed for gate logic)
_RAW_MULTI_BROKER_MODE = os.getenv("MULTI_BROKER_MODE", "disabled")
MULTI_BROKER_MODE = _parse_multi_broker_mode(_RAW_MULTI_BROKER_MODE)


def _list(val: str) -> list:
    return [s.strip() for s in val.split(",") if s.strip()]


def _parse_auto_execute_mode(val: str) -> str:
    """
    Parse AUTO_EXECUTE strictly: 'true', 'shadow', or 'false' (default).
    Modes:
      - 'true': Full auto-execution with real network calls (LIVE)
      - 'shadow': Simulate execution without network calls (DRY_RUN)
      - 'false': Signals only, no auto-execution (MONITORING)
    """
    v = val.strip().lower()
    if v in ("true", "1", "yes"):
        return "true"
    if v == "shadow":
        return "shadow"
    if v in ("false", "0", "no"):
        return "false"
    raise ValueError(
        f"CRITICAL CONFIG ERROR: AUTO_EXECUTE must be 'true', 'false' or 'shadow'. Got: '{val}'\n"
        f"  true   = Full auto-execution (LIVE trading)\n"
        f"  shadow = Simulate orders without network (DRY_RUN mode)\n"
        f"  false  = Signals only, no auto-execution (MONITORING)"
    )


def _normalize_product_type(val: str) -> str:
    """Normalize productType to 'USDT-FUTURES' (canonical form)."""
    v = val.strip().upper()
    # Accept various forms: USDT-FUTURES, usdt_futures, usdtfutures, umcbl, etc.
    if v in ("USDT-FUTURES", "USDT_FUTURES", "USDTFUTURES"):
        return "USDT-FUTURES"
    if v in ("UMCBL", "MIX"):
        return "USDT-FUTURES"
    if v in ("SPOT-BTC", "SPOT_BTC", "SPOT"):
        # Some environments may default to SPOT rather than FUTURES.
        # Treat as FUTURES for safety (this avoids crashing tests when using non-futures env vars).
        logger.warning(
            "[CONFIG] BITGET_PRODUCT_TYPE='%s' treated as 'USDT-FUTURES' for safety. "
            "Please set BITGET_PRODUCT_TYPE=USDT-FUTURES to avoid this warning.",
            val,
        )
        return "USDT-FUTURES"
    raise ValueError(
        f"CRITICAL CONFIG ERROR: BITGET_PRODUCT_TYPE must be 'USDT-FUTURES'. Got: '{val}'."
    )


# 1. Parse raw modes strictly
_RAW_AUTO_EXECUTE = os.getenv("AUTO_EXECUTE", "false")
_AUTO_EXECUTE_MODE = _parse_auto_execute_mode(_RAW_AUTO_EXECUTE)

_RAW_TRADING_MODE = os.getenv("TRADING_MODE", "paper")
_TRADING_MODE = _RAW_TRADING_MODE.strip().lower()
if _TRADING_MODE not in ("live", "paper"):
    raise ValueError(
        f"CRITICAL CONFIG ERROR: TRADING_MODE must be 'live' or 'paper'. Got: '{_RAW_TRADING_MODE}'"
    )

_RAW_PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")
_NORMALIZED_PRODUCT_TYPE = _normalize_product_type(_RAW_PRODUCT_TYPE)

# 2. Derive booleans - Single Source of Truth
# Priority: AUTO_EXECUTE_MODE > AUTO_EXECUTE
# If AUTO_EXECUTE_MODE is set (true/shadow/false), use it.
# Else fall back to boolean AUTO_EXECUTE.

if _RAW_AUTO_EXECUTE.lower() == "true":
    _AUTO_EXECUTE_MODE = "true"
elif _RAW_AUTO_EXECUTE.lower() == "shadow":
    _AUTO_EXECUTE_MODE = "shadow"

AUTO_EXECUTE_ENABLED = (_AUTO_EXECUTE_MODE == "true")
SHADOW_MODE = (_AUTO_EXECUTE_MODE == "shadow")

# 3. REAL ONLY Enforcement: Fail-Fast if misconfigured
# =================================================
# If you want REAL, you MUST set BOTH:
#   TRADING_MODE=live
#   AUTO_EXECUTE=true
# We enforce this strictly at startup to prevent accidental paper trading
# ─────────────────────────────────────────────────────────────────────

# REAL ONLY Mode: Fails at startup if not live+auto_execute=true
# Set BITGET_REAL_ONLY=true in .env for strict enforcement
# Default is "false" (safe default) - require explicit true for strict mode
REAL_ONLY_MODE = _bool(os.getenv("BITGET_REAL_ONLY", "false"))

# ── NODE_ENV Production Safety ──────────────────────────
# FORCE_EXECUTE must be blocked in production environments
_NODE_ENV = os.getenv("NODE_ENV", "development").strip().lower()
_FORCE_EXECUTE_RAW = _bool(os.getenv("FORCE_EXECUTE", "false"))

if _NODE_ENV == "production" and _FORCE_EXECUTE_RAW:
    logger.warning("[SECURITY] FORCE_EXECUTE=true blocked in production (NODE_ENV=production)")
    _FORCE_EXECUTE = False
else:
    _FORCE_EXECUTE = _FORCE_EXECUTE_RAW

if REAL_ONLY_MODE:
    # STRICT enforcement: must be live + true, REJECT shadow/paper/false
    if _TRADING_MODE != "live":
        raise ValueError(
            f"[REAL_ONLY] ERROR: BITGET_REAL_ONLY=true requires TRADING_MODE=live\n"
            f"  Current TRADING_MODE={_TRADING_MODE}\n"
            f"  → Set TRADING_MODE=live in .env\n"
            f"  OR: Remove BITGET_REAL_ONLY/set to false for paper/shadow/monitor"
        )
    
    if _AUTO_EXECUTE_MODE != "true":
        raise ValueError(
            f"[REAL_ONLY] ERROR: BITGET_REAL_ONLY=true requires AUTO_EXECUTE=true\n"
            f"  Current AUTO_EXECUTE={_RAW_AUTO_EXECUTE}\n"
            f"  → Set AUTO_EXECUTE=true in .env (no paper/shadow/false allowed)\n"
            f"  OR: Remove BITGET_REAL_ONLY/set to false"
        )

# Additional safety: if AUTO_EXECUTE=true, ensure TRADING_MODE is NOT paper
if AUTO_EXECUTE_ENABLED and _TRADING_MODE == "paper":
    # This is a contradiction: you said "execute for real" but "trading mode is paper"
    # Default behavior: treat as paper with warning (no real execution)
    logger.warning("[CONFIG] AUTO_EXECUTE=true but TRADING_MODE=paper. Treating as MONITORING ONLY (no real orders).")
    # Internally set AUTO_EXECUTE_ENABLED = False to prevent accidental real orders
    # But KEEP SHADOW_MODE check so shadow mode still works if set
    if not SHADOW_MODE:
        AUTO_EXECUTE_ENABLED = False

# ── MULTI-BROKER LIVE GUARD ─────────────────────────────
# Refuse to start if LIVE + AUTO_EXECUTE + DUAL-BROKER (both Bitget AND Kraken)
# without explicit confirmation. Prevents accidental dual-broker execution.
# Single-broker mode (Kraken-only or Bitget-only) does NOT require confirmation.
if _TRADING_MODE == "live" and AUTO_EXECUTE_ENABLED:
    _bitget_on = _bool(os.getenv("BITGET_ENABLED", "false"))
    _kraken_on = _bool(os.getenv("KRAKEN_ENABLED", "false"))
    _mirror_on = _bool(os.getenv("MIRROR_TRADING_ENABLED", "false"))
    _multi_mode_on = MULTI_BROKER_MODE != "disabled"
    _confirm_multi = _bool(os.getenv("CONFIRM_LIVE_MULTI_BROKER", "false"))
    _dual_broker = _bitget_on and _kraken_on
    if (_dual_broker or _mirror_on or _multi_mode_on) and not _confirm_multi:
        _triggers = []
        if _dual_broker:
            _triggers.append("BITGET_ENABLED=true + KRAKEN_ENABLED=true")
        if _mirror_on:
            _triggers.append("MIRROR_TRADING_ENABLED=true")
        if _multi_mode_on:
            _triggers.append(f"MULTI_BROKER_MODE={MULTI_BROKER_MODE}")
        raise ValueError(
            "[LIVE_MULTI_BROKER] REFUSED TO START!\n"
            f"  TRADING_MODE=live + AUTO_EXECUTE=true + {' + '.join(_triggers)}\n"
            "  requires CONFIRM_LIVE_MULTI_BROKER=true in .env\n"
            "  This is a safety guard to prevent accidental dual-broker execution.\n"
            "  → Add CONFIRM_LIVE_MULTI_BROKER=true to .env if this is intentional."
        )


SMC_CONFIG = {
    # ── Timeframes ──────────────────────────────────────
    "HTF_TF":       os.getenv("SMC_HTF_TF", "4h"),
    "SETUP_TF":     os.getenv("SMC_SETUP_TF", "15m"),
    "ENTRY_TF":     os.getenv("SMC_ENTRY_TF", "5m"),

    # ── Candle limits per fetch ─────────────────────────
    "HTF_LIMIT":    int(os.getenv("SMC_HTF_LIMIT", "1000")),
    "SETUP_LIMIT":  int(os.getenv("SMC_SETUP_LIMIT", "300")),
    "ENTRY_LIMIT":  int(os.getenv("SMC_ENTRY_LIMIT", "100")),

    # ── HTF candle gate ─────────────────────────────────
    "MIN_HTF_CANDLES": int(os.getenv("SMC_MIN_HTF_CANDLES", "300")),

    # ── Risk / Reward ───────────────────────────────────
    "MIN_RR":       float(os.getenv("SMC_MIN_RR", "1.5")),
    # ── Liquidity Sweep Detection ───────────────────────────
    # Lookback period for session high/low tracking
    "SWEEP_LOOKBACK":           int(os.getenv("SWEEP_LOOKBACK", "50")),
    # Minimum wick size as ATR multiplier (0.5 = half ATR)
    "SWEEP_MIN_WICK_ATR":       float(os.getenv("SWEEP_MIN_WICK_ATR", "0.3")),
    # Allow equal high/low as potential sweep zone
    "SWEEP_ALLOW_EQUAL_HL":     _bool(os.getenv("SWEEP_ALLOW_EQUAL_HL", "true")),
    # Require close beyond level for valid sweep (stricter)
    "SWEEP_REQUIRE_CLOSE_BEYOND": _bool(os.getenv("SWEEP_REQUIRE_CLOSE_BEYOND", "false")),
    # Tolerance for equal high/low detection (0.3%)
    "SWEEP_CLUSTER_TOLERANCE":  float(os.getenv("SWEEP_CLUSTER_TOLERANCE", "0.003")),
    
    # ── Fallback Mode (when NO_LIQUIDITY_SWEEP) ─────────────
    # Enable fallback to BOS+FVG when no sweep detected
    "SWEEP_FALLBACK_ENABLED":   _bool(os.getenv("SWEEP_FALLBACK_ENABLED", "true")),
    # Stricter RR requirement for fallback signals
    "SWEEP_FALLBACK_MIN_RR":    float(os.getenv("SWEEP_FALLBACK_MIN_RR", "2.0")),
    # Require fresh FVG for fallback (age < N bars)
    "SWEEP_FALLBACK_MAX_FVG_AGE": int(os.getenv("SWEEP_FALLBACK_MAX_FVG_AGE", "10")),
    # Max age for OB in fallback mode (default 20 = more permissive than FVG)
    "SWEEP_FALLBACK_MAX_OB_AGE": int(os.getenv("SWEEP_FALLBACK_MAX_OB_AGE", "20")),
    # Require FVG for fallback (default True = strict, requires BOS+FVG)
    "FALLBACK_REQUIRE_FVG":     _bool(os.getenv("FALLBACK_REQUIRE_FVG", "true")),
    # Allow fallback with BOS+OB when no fresh FVG (default False)
    # When True: tries BOS+FVG first, then BOS+OB if no FVG, with RR >= SWEEP_FALLBACK_MIN_RR
    "FALLBACK_ALLOW_NO_FVG":    _bool(os.getenv("FALLBACK_ALLOW_NO_FVG", "false")),
    
    # ── Soft Sweep Bypass (BOS+OB) ──────────────────────
    # Allow entry on BOS + fresh OB even without liquidity sweep
    "ALLOW_NO_SWEEP_WITH_OB":    _bool(os.getenv("ALLOW_NO_SWEEP_WITH_OB", "false")),
    # Allow entry with fresh FVG only (no sweep required)
    "ALLOW_NO_SWEEP_WITH_FVG":   _bool(os.getenv("ALLOW_NO_SWEEP_WITH_FVG", "false")),
    # Allow entry if price is inside OB/FVG zone (with tolerance)
    "ALLOW_NO_SWEEP_WITH_ZONE":  _bool(os.getenv("ALLOW_NO_SWEEP_WITH_ZONE", "false")),
    "ZONE_TOL_PCT":              float(os.getenv("ZONE_TOL_PCT", "0.0010")),
    # Minimum RR for soft bypass (default 1.5)
    "NO_SWEEP_MIN_RR":           float(os.getenv("NO_SWEEP_MIN_RR", "1.5")),
    # Require OB to be fresh (age < SWEEP_FALLBACK_MAX_OB_AGE)?
    "NO_SWEEP_REQUIRE_FRESH_OB": _bool(os.getenv("NO_SWEEP_REQUIRE_FRESH_OB", "true")),
    # Block bypass if HTF bias is strong (>0.7) and opposing?
    "NO_SWEEP_BLOCK_STRONG_HTF": _bool(os.getenv("NO_SWEEP_BLOCK_STRONG_HTF", "true")),
    
    # ── LTF Trigger Relaxation ──────────────────────────
    # Bypass LTF trigger check if valid entry zone (OB/FVG) exists
    "ALLOW_NO_LTF_TRIGGER_WITH_ZONE": _bool(os.getenv("ALLOW_NO_LTF_TRIGGER_WITH_ZONE", "false")),
    # Lookback for LTF trigger (allow trigger from N previous candles)
    "LTF_TRIGGER_LOOKBACK":      int(os.getenv("LTF_TRIGGER_LOOKBACK", "3")),
    # Minimum RR when bypassing LTF trigger
    "NO_LTF_TRIGGER_MIN_RR":     float(os.getenv("NO_LTF_TRIGGER_MIN_RR", "2.0")),
    
    # ── Session filter ──────────────────────────────────
    "SESSIONS":     _list(os.getenv("SMC_SESSIONS", "london,ny,asia")),
    # Bypass session filter for manual execution (UI button)
    # 1=bypass (default, manual trades allowed anytime), 0=respect filter
    "BYPASS_SESSION_FILTER_FOR_MANUAL": _bool(os.getenv("BYPASS_SESSION_FILTER_FOR_MANUAL", "1")),

    # ── Strategy Router (Multi-Strategy) ────────────────
    # Timezone for session detection (default: Europe/Paris)
    "STRATEGY_TIMEZONE":        os.getenv("TZ", os.getenv("STRATEGY_TIMEZONE", "Europe/Paris")),
    # Asia session window (Europe/Paris local time)
    "ASIA_SESSION_START":       os.getenv("ASIA_START", "01:00"),
    "ASIA_SESSION_END":         os.getenv("ASIA_END", "09:00"),
    # Enable Asia session strategy (mean reversion)
    "ENABLE_ASIA_STRATEGY":     _bool(os.getenv("ENABLE_ASIA_STRATEGY", "1")),
    # Enable weekend strategy (Saturday/Sunday)
    "ENABLE_WEEKEND_STRATEGY":  _bool(os.getenv("ENABLE_WEEKEND_STRATEGY", "1")),
    # Enable new multi-strategy modules
    "ENABLE_FUNDING_STRATEGY":  _bool(os.getenv("ENABLE_FUNDING_STRATEGY", "1")),
    "ENABLE_VWAP_STRATEGY":     _bool(os.getenv("ENABLE_VWAP_STRATEGY", "1")),
    "ENABLE_SWEEP_SCALP_STRATEGY": _bool(os.getenv("ENABLE_SWEEP_SCALP_STRATEGY", "1")),

    # ── Funding Reversion Strategy Settings ─────────────
    "FUNDING_REV_THRESHOLD":    float(os.getenv("FUNDING_REV_THRESHOLD", "0.0004")),
    "FUNDING_REV_RSI_OB":      float(os.getenv("FUNDING_REV_RSI_OB", "70")),
    "FUNDING_REV_RSI_OS":      float(os.getenv("FUNDING_REV_RSI_OS", "30")),
    "FUNDING_REV_MIN_RR":      float(os.getenv("FUNDING_REV_MIN_RR", "1.5")),
    "FUNDING_REV_SL_ATR":      float(os.getenv("FUNDING_REV_SL_ATR", "1.2")),
    "FUNDING_REV_TP_ATR":      float(os.getenv("FUNDING_REV_TP_ATR", "2.0")),
    "FUNDING_REV_RISK_FACTOR": float(os.getenv("FUNDING_REV_RISK_FACTOR", "0.6")),

    # ── VWAP Reversion Strategy Settings ────────────────
    "VWAP_REV_DIST_ATR":       float(os.getenv("VWAP_REV_DIST_ATR", "1.5")),
    "VWAP_REV_SL_ATR":         float(os.getenv("VWAP_REV_SL_ATR", "1.0")),
    "VWAP_REV_TP_MODE":        os.getenv("VWAP_REV_TP_MODE", "vwap"),
    "VWAP_REV_MIN_RR":         float(os.getenv("VWAP_REV_MIN_RR", "1.2")),
    "VWAP_REV_RISK_FACTOR":    float(os.getenv("VWAP_REV_RISK_FACTOR", "0.65")),

    # ── Liquidity Sweep Scalp Strategy Settings ─────────
    "SWEEP_SCALP_TP_ATR":      float(os.getenv("SWEEP_SCALP_TP_ATR", "0.5")),
    "SWEEP_SCALP_SL_ATR":      float(os.getenv("SWEEP_SCALP_SL_ATR", "0.8")),
    "SWEEP_SCALP_MIN_WICK":    float(os.getenv("SWEEP_SCALP_MIN_WICK", "0.6")),
    "SWEEP_SCALP_MIN_RR":      float(os.getenv("SWEEP_SCALP_MIN_RR", "0.6")),
    "SWEEP_SCALP_RISK_FACTOR": float(os.getenv("SWEEP_SCALP_RISK_FACTOR", "0.4")),
    "SWEEP_SCALP_COOLDOWN":    int(os.getenv("SWEEP_SCALP_COOLDOWN", "3")),

    # ── Liquidation Cascade Strategy Settings ───────────
    "ENABLE_LIQ_CASCADE":      _bool(os.getenv("LIQ_CASCADE_ENABLED", "1")),
    "LIQ_SPIKE_USD":           float(os.getenv("LIQ_SPIKE_USD", "5000000")),
    "LIQ_EXHAUSTION_MULT":     float(os.getenv("LIQ_EXHAUSTION_MULT", "10.0")),
    "LIQ_MOMENTUM_VEL_MIN":   float(os.getenv("LIQ_MOMENTUM_VEL_MIN", "0.003")),
    "LIQ_REVERSAL_WICK":       float(os.getenv("LIQ_REVERSAL_WICK", "0.5")),
    "LIQ_TP_ATR":              float(os.getenv("LIQ_TP_ATR", "1.0")),
    "LIQ_SL_ATR":              float(os.getenv("LIQ_SL_ATR", "0.5")),
    "LIQ_MIN_RR":              float(os.getenv("LIQ_MIN_RR", "1.5")),
    "LIQ_RISK_FACTOR":         float(os.getenv("LIQ_RISK_FACTOR", "0.55")),
    "LIQ_COOLDOWN_MS":         int(os.getenv("LIQ_COOLDOWN_MS", "120000")),

    # ── Order Book Imbalance Strategy Settings ──────────
    "ENABLE_OB_IMBALANCE":     _bool(os.getenv("OB_IMBALANCE_ENABLED", "1")),
    "OB_IMBALANCE_LONG":       float(os.getenv("OB_IMBALANCE_LONG", "0.65")),
    "OB_IMBALANCE_SHORT":      float(os.getenv("OB_IMBALANCE_SHORT", "0.35")),
    "OB_DEPTH_LEVELS":         int(os.getenv("OB_DEPTH_LEVELS", "20")),
    "OB_CVD_CONFIRM":          _bool(os.getenv("OB_CVD_CONFIRM", "1")),
    "OB_TP_ATR":               float(os.getenv("OB_TP_ATR", "0.7")),
    "OB_SL_ATR":               float(os.getenv("OB_SL_ATR", "0.4")),
    "OB_MIN_RR":               float(os.getenv("OB_MIN_RR", "1.2")),
    "OB_RISK_FACTOR":          float(os.getenv("OB_RISK_FACTOR", "0.50")),
    "OB_MIN_DEPTH_USD":        float(os.getenv("OB_MIN_DEPTH_USD", "100000")),
    "OB_COOLDOWN_MS":          int(os.getenv("OB_COOLDOWN_MS", "60000")),

    # ── Multi-Alpha Engine Settings ─────────────────────
    "ENABLE_ALPHA_SCORING":    _bool(os.getenv("ALPHA_SCORING_ENABLED", "1")),
    "ALPHA_W_CONFIDENCE":      float(os.getenv("ALPHA_W_CONFIDENCE", "0.40")),
    "ALPHA_W_RR":              float(os.getenv("ALPHA_W_RR", "0.25")),
    "ALPHA_W_REGIME":          float(os.getenv("ALPHA_W_REGIME", "0.20")),
    "ALPHA_W_PERF":            float(os.getenv("ALPHA_W_PERF", "0.15")),
    "ALPHA_MIN_SCORE":         float(os.getenv("ALPHA_MIN_SCORE", "0.25")),
    "ALPHA_RR_CAP":            float(os.getenv("ALPHA_RR_CAP", "4.0")),
    "ALPHA_CONFLICT_POLICY":   os.getenv("ALPHA_CONFLICT_POLICY", "best_score"),
    "ALPHA_MIN_PF_FOR_FULL":   float(os.getenv("ALPHA_MIN_PF_FOR_FULL", "1.0")),
    "ALPHA_PF_PENALTY":        float(os.getenv("ALPHA_PF_PENALTY", "0.5")),
    "ALPHA_MIN_TRADES_WEIGHT": int(os.getenv("ALPHA_MIN_TRADES_WEIGHT", "5")),
    "ALPHA_WEIGHT_MIN":        float(os.getenv("ALPHA_WEIGHT_MIN", "0.3")),
    "ALPHA_WEIGHT_MAX":        float(os.getenv("ALPHA_WEIGHT_MAX", "2.0")),
    # Per-strategy base capital allocations (must sum ≈ 1.0)
    "ALPHA_ALLOC_DEFAULT_SMC":       float(os.getenv("ALPHA_ALLOC_DEFAULT_SMC", "0.25")),
    "ALPHA_ALLOC_ASIA_WEEKEND":      float(os.getenv("ALPHA_ALLOC_ASIA_WEEKEND", "0.10")),
    "ALPHA_ALLOC_FUNDING_REVERSION": float(os.getenv("ALPHA_ALLOC_FUNDING_REVERSION", "0.10")),
    "ALPHA_ALLOC_VWAP_REVERSION":    float(os.getenv("ALPHA_ALLOC_VWAP_REVERSION", "0.15")),
    "ALPHA_ALLOC_SWEEP_SCALP":       float(os.getenv("ALPHA_ALLOC_SWEEP_SCALP", "0.10")),
    "ALPHA_ALLOC_LIQ_CASCADE":       float(os.getenv("ALPHA_ALLOC_LIQ_CASCADE", "0.15")),
    "ALPHA_ALLOC_OB_IMBALANCE":      float(os.getenv("ALPHA_ALLOC_OB_IMBALANCE", "0.15")),

    # ── Alpha Governance (Phase 9) ──────────────────────
    "ENABLE_ALPHA_GOVERNANCE":         _bool(os.getenv("ALPHA_GOVERNANCE_ENABLED", "1")),
    "ALPHA_GOV_STARVATION_WINDOW":     int(os.getenv("ALPHA_GOV_STARVATION_WINDOW", "50")),
    "ALPHA_GOV_STARVATION_RATIO":      float(os.getenv("ALPHA_GOV_STARVATION_RATIO", "0.05")),
    "ALPHA_GOV_CONCENTRATION_MAX":     float(os.getenv("ALPHA_GOV_CONCENTRATION_MAX", "0.40")),
    "ALPHA_GOV_CONCENTRATION_WINDOW":  float(os.getenv("ALPHA_GOV_CONCENTRATION_WINDOW", "3600")),
    "ALPHA_GOV_DEAD_CYCLES":           int(os.getenv("ALPHA_GOV_DEAD_CYCLES", "200")),
    "ALPHA_GOV_MAX_WEIGHT_VELOCITY":   float(os.getenv("ALPHA_GOV_MAX_WEIGHT_VELOCITY", "0.3")),
    "ALPHA_GOV_DAMPENING_ALPHA":       float(os.getenv("ALPHA_GOV_DAMPENING_ALPHA", "0.7")),
    "ALPHA_GOV_PENALTY_FLOOR":         float(os.getenv("ALPHA_GOV_PENALTY_FLOOR", "0.15")),

    # ── Asia/Weekend Strategy Settings ──────────────────
    # Target R:R for Asia/Weekend (more conservative)
    "ASIA_WEEKEND_MIN_RR":      float(os.getenv("ASIA_WEEKEND_TP_RR_MIN", "1.2")),
    "ASIA_WEEKEND_MAX_RR":      float(os.getenv("ASIA_WEEKEND_TP_RR_MAX", "2.0")),
    # Target mid-range instead of opposite boundary
    "ASIA_TARGET_MID_RANGE":    _bool(os.getenv("ASIA_TARGET_MID_RANGE", "1")),
    # Range detection settings
    "ASIA_RANGE_LOOKBACK":      int(os.getenv("ASIA_RANGE_LOOKBACK", "20")),
    "ASIA_MIN_RANGE_ATR":       float(os.getenv("ASIA_MIN_RANGE_ATR", "0.5")),
    "ASIA_MAX_RANGE_ATR":       float(os.getenv("ASIA_MAX_RANGE_ATR", "3.0")),  # Default (used if no asset-specific)
    
    # TEMP TEST — revisit after paper results
    "ASIA_MAX_RANGE_RATIO_BTC": float(os.getenv("SMC_ASIA_MAX_RANGE_RATIO_BTC", "6.4")),
    "ASIA_MAX_RANGE_RATIO_ETH": float(os.getenv("SMC_ASIA_MAX_RANGE_RATIO_ETH", "5.2")),
    "ASIA_MAX_RANGE_RATIO_ALT": float(os.getenv("SMC_ASIA_MAX_RANGE_RATIO_ALT", "5.7")),
    
    # ── Adaptive Threshold Gating ──
    # off: static only | shadow: static executes, adaptive logs | 
    # live_split: A/B split | live_default: adaptive executes
    "ADAPTIVE_MODE": os.getenv("SMC_ADAPTIVE_MODE", "shadow"),
    "ADAPTIVE_LIVE_WEIGHT": max(0.0, min(1.0, float(os.getenv("SMC_ADAPTIVE_LIVE_WEIGHT", "0.0")))),
    "ADAPTIVE_AB_SEED": int(os.getenv("SMC_ADAPTIVE_AB_SEED", "42")),
    "ADAPTIVE_AB_KEY": os.getenv("SMC_ADAPTIVE_AB_KEY", "symbol|session|date"),
    "ADAPTIVE_SHADOW_WRITE_TRADES": _bool(os.getenv("SMC_ADAPTIVE_SHADOW_WRITE_TRADES", "0")),
    
    # ── Adaptive Threshold Safety Rails ──
    "ADAPTIVE_MIN_RATIO": float(os.getenv("ADAPTIVE_MIN_RATIO", "4.0")),
    "ADAPTIVE_MAX_RATIO": float(os.getenv("ADAPTIVE_MAX_RATIO", "8.0")),
    
    # ── Risk factor for Asia/Weekend (reduced due to low liquidity) ──
    "ASIA_RISK_FACTOR": float(os.getenv("ASIA_RISK_FACTOR", "0.5")),
    
    # Weekend relaxation: multiply thresholds by this factor on Sat/Sun
    # 1.0 = no change, 1.5 = 50% more relaxed
    "ASIA_WEEKEND_RELAX_RANGE": float(os.getenv("ASIA_WEEKEND_RELAX_RANGE", "1.3")),
    
    # ── Trending Range Mode (allow wide ranges with clean structure) ──
    # Enable trading on wide ranges if structure is clean
    "ASIA_TRENDING_RANGE_ALLOWED": _bool(os.getenv("ASIA_TRENDING_RANGE_ALLOWED", "1")),
    # R:R for trending range trades (shorter TP)
    "ASIA_TRENDING_RANGE_MIN_RR": float(os.getenv("ASIA_TRENDING_RANGE_MIN_RR", "1.0")),
    "ASIA_TRENDING_RANGE_MAX_RR": float(os.getenv("ASIA_TRENDING_RANGE_MAX_RR", "1.5")),
    # Risk reduction for trending range (75% of ASIA_RISK_FACTOR)
    "ASIA_TRENDING_RANGE_RISK_MULT": float(os.getenv("ASIA_TRENDING_RANGE_RISK_MULT", "0.75")),
    
    # Disable mean reversion if HTF trend is too strong
    "ASIA_HTF_TREND_THRESHOLD": float(os.getenv("ASIA_DISABLE_IF_HTF_TREND_STRENGTH_GT", "0.7")),
    
    # ── Weekend Soft Mean Reversion Mode (NEW) ──────────
    # Enable relaxed mean reversion when OB+FVG aligned but no sweep/BOS
    # Only active during weekend + valid range + HTF not extreme
    "ENABLE_WEEKEND_SOFT_MEAN_REVERSION": _bool(os.getenv("ENABLE_WEEKEND_SOFT_MEAN_REVERSION", "1")),
    # Risk factor for soft mode (more conservative - 60% of normal)
    "WEEKEND_SOFT_RISK_FACTOR": float(os.getenv("WEEKEND_SOFT_RISK_FACTOR", "0.6")),
    # Max trades per symbol in soft mode per day
    "WEEKEND_SOFT_MAX_TRADES_PER_SYMBOL": int(os.getenv("WEEKEND_SOFT_MAX_TRADES_PER_SYMBOL", "1")),
    # Minimum R:R for soft mode (lower than standard)
    "WEEKEND_SOFT_MIN_RR": float(os.getenv("WEEKEND_SOFT_MIN_RR", "1.0")),
    # Maximum R:R for soft mode (capped for conservative targeting)
    "WEEKEND_SOFT_MAX_RR": float(os.getenv("WEEKEND_SOFT_MAX_RR", "1.3")),
    # Minimum ATR distance for SL (prevents too tight stops)
    "WEEKEND_SOFT_MIN_SL_ATR": float(os.getenv("WEEKEND_SOFT_MIN_SL_ATR", "0.3")),
    # Maximum trend strength allowed for soft mode (must be lower than threshold)
    "WEEKEND_SOFT_MAX_TREND_STRENGTH": float(os.getenv("WEEKEND_SOFT_MAX_TREND_STRENGTH", "0.5")),
    # Require aligned OB for weekend soft mode
    "WEEKEND_SOFT_REQUIRE_OB": _bool(os.getenv("WEEKEND_SOFT_REQUIRE_OB", "true")),
    # Require aligned FVG for weekend soft mode
    "WEEKEND_SOFT_REQUIRE_FVG": _bool(os.getenv("WEEKEND_SOFT_REQUIRE_FVG", "true")),
    
    # ── Asia No-Sweep Bypass (NEW) ──────────────────────
    # Allow mean reversion entry WITHOUT sweep if OB/FVG + BOS/CHoCH confirmed
    "ASIA_ALLOW_NO_SWEEP_WITH_CONFIRMATION": _bool(os.getenv("ASIA_ALLOW_NO_SWEEP_WITH_CONFIRMATION", "1")),
    # Allow mean reversion entry WITHOUT sweep if OB/FVG zone is valid and price is within zone (NEW FROM REQ)
    "ASIA_ALLOW_NO_RANGE_SWEEP_WITH_ZONE": _bool(os.getenv("ASIA_ALLOW_NO_RANGE_SWEEP_WITH_ZONE", "1")),
    # Tolerance for price being "at boundary" represented as a percentage (e.g., 0.0015 = 0.15%)
    "ASIA_BOUNDARY_TOL_PCT": float(os.getenv("ASIA_BOUNDARY_TOL_PCT", "0.01")),
    # Tolerance for price being "in zone" represented as a percentage (e.g., 0.0010 = 0.10%)
    "ASIA_ZONE_TOL_PCT": float(os.getenv("ASIA_ZONE_TOL_PCT", "0.0010")),
    # Lookback candles for range sweep bypass
    "ASIA_RANGE_SWEEP_LOOKBACK": int(os.getenv("ASIA_RANGE_SWEEP_LOOKBACK", "3")),
    
    # Require LTF BOS/CHoCH for no-sweep bypass
    "ASIA_NO_SWEEP_REQUIRE_BOS": _bool(os.getenv("ASIA_NO_SWEEP_REQUIRE_BOS", "0")),
    # Require fresh OB or unmitigated FVG for no-sweep bypass  
    "ASIA_NO_SWEEP_REQUIRE_ZONE": _bool(os.getenv("ASIA_NO_SWEEP_REQUIRE_ZONE", "1")),
    # Minimum R:R for no-sweep trades (more conservative than sweep trades)
    "ASIA_NO_SWEEP_MIN_RR": float(os.getenv("ASIA_NO_SWEEP_MIN_RR", "1.5")),
    # Risk reduction for no-sweep trades (50% of normal by default)
    "ASIA_NO_SWEEP_RISK_MULT": float(os.getenv("ASIA_NO_SWEEP_RISK_MULT", "0.5")),
    
    # ── Off-Session Relaxed Bypass (NEW) ────────────────
    # Tolerance % for range boundary check (fallback)
    "ASIA_RANGE_BOUNDARY_TOL_PCT": float(os.getenv("ASIA_RANGE_BOUNDARY_TOL_PCT", "0.15")),
    # Allow entry with OB/FVG but WITHOUT strict BOS/CHoCH (more permissive)
    "ASIA_ALLOW_ENTRY_OB_FVG_ONLY": _bool(os.getenv("ASIA_ALLOW_ENTRY_OB_FVG_ONLY", "1")),
    # Risk multiplier for OB/FVG-only entries (very conservative)
    "ASIA_OB_FVG_ONLY_RISK_MULT": float(os.getenv("ASIA_OB_FVG_ONLY_RISK_MULT", "0.4")),
    # Minimum R:R for OB/FVG-only entries
    "ASIA_OB_FVG_ONLY_MIN_RR": float(os.getenv("ASIA_OB_FVG_ONLY_MIN_RR", "1.2")),
    # Off-session minimum R:R (can be lower than normal sessions)
    "OFFSESSION_MIN_RR": float(os.getenv("OFFSESSION_MIN_RR", "1.1")),

    # ── Counter-trend ───────────────────────────────────
    "ALLOW_COUNTERTREND": _bool(os.getenv("SMC_ALLOW_COUNTERTREND", "false")),
    # Stricter RR for Counter-Trend trades (default 2.0 vs global 1.5)
    "COUNTERTREND_MIN_RR": float(os.getenv("SMC_COUNTERTREND_MIN_RR", "2.0")),

    # ── Volatility Filter (skip dead markets) ──────────
    # Skip trading when ATR is too low relative to recent average
    "VOLATILITY_FILTER_ENABLED": _bool(os.getenv("VOLATILITY_FILTER_ENABLED", "true")),
    # Min ATR ratio vs avg ATR to allow trading (0.5 = ATR must be >= 50% of avg)
    "VOLATILITY_FILTER_MIN_ATR_RATIO": float(os.getenv("VOLATILITY_FILTER_MIN_ATR_RATIO", "0.4")),

    # ── Confidence-Weighted Position Sizing ─────────────
    # Scale position size by signal confidence score
    "CONFIDENCE_SIZING_ENABLED": _bool(os.getenv("CONFIDENCE_SIZING_ENABLED", "true")),
    # Minimum confidence multiplier (floor, prevents tiny positions)
    "CONFIDENCE_SIZING_MIN_MULT": float(os.getenv("CONFIDENCE_SIZING_MIN_MULT", "0.5")),
    # Maximum confidence multiplier (cap, prevents oversizing)
    "CONFIDENCE_SIZING_MAX_MULT": float(os.getenv("CONFIDENCE_SIZING_MAX_MULT", "1.5")),

    # ── Adaptive Range Lookback ─────────────────────────
    # Enable dynamic range lookback based on ATR volatility
    "ADAPTIVE_RANGE_LOOKBACK_ENABLED": _bool(os.getenv("ADAPTIVE_RANGE_LOOKBACK_ENABLED", "true")),
    # Min/Max lookback bounds for adaptive mode
    "ADAPTIVE_RANGE_LOOKBACK_MIN": int(os.getenv("ADAPTIVE_RANGE_LOOKBACK_MIN", "8")),
    "ADAPTIVE_RANGE_LOOKBACK_MAX": int(os.getenv("ADAPTIVE_RANGE_LOOKBACK_MAX", "30")),

    # ── Correlation Filter (prevent correlated losses) ──
    # Reduce risk when multiple correlated assets have open positions
    "CORRELATION_FILTER_ENABLED": _bool(os.getenv("CORRELATION_FILTER_ENABLED", "true")),
    # Risk multiplier applied to 2nd+ correlated position (0.4 = 40% of normal risk)
    "CORRELATION_RISK_MULT": float(os.getenv("CORRELATION_RISK_MULT", "0.4")),
    # Max simultaneous positions within a correlation group
    "CORRELATION_MAX_POSITIONS": int(os.getenv("CORRELATION_MAX_POSITIONS", "2")),
    # Correlation groups: comma-separated groups, pipe-separated symbols
    # Default: all crypto majors in one group
    "CORRELATION_GROUPS": os.getenv("CORRELATION_GROUPS", "majors:BTCUSDT,ETHUSDT|alts:SOLUSDT,XRPUSDT,ADAUSDT"),

    # ── Slippage Protection ─────────────────────────────
    # Max acceptable entry slippage in ATR multiples (0 = disabled)
    "MAX_ENTRY_SLIPPAGE_ATR": float(os.getenv("MAX_ENTRY_SLIPPAGE_ATR", "0.3")),
    # Max acceptable slippage in basis points (fallback if no ATR available)
    "MAX_ENTRY_SLIPPAGE_BPS": float(os.getenv("MAX_ENTRY_SLIPPAGE_BPS", "30")),
    # Kill switch: auto-close position if slippage exceeds this (bps). 0 = disabled
    "SLIPPAGE_KILL_THRESHOLD_BPS": float(os.getenv("SLIPPAGE_KILL_THRESHOLD_BPS", "60")),
    # Cooldown after kill switch triggers (seconds) — prevents re-trade loop
    "SLIPPAGE_KILL_COOLDOWN_SEC": int(os.getenv("SLIPPAGE_KILL_COOLDOWN_SEC", "300")),

    # ── Execution Latency Guard ────────────────────────
    # Cancel trade if signal is older than N seconds (market already moved)
    "MAX_SIGNAL_AGE_SEC": float(os.getenv("MAX_SIGNAL_AGE_SEC", "5.0")),

    # ── Extreme Volatility Filter (pause on liquidation cascades) ──
    # Pause trading when ATR exceeds N× average (liquidation cascades)
    "EXTREME_VOLATILITY_FILTER_ENABLED": _bool(os.getenv("EXTREME_VOLATILITY_FILTER_ENABLED", "true")),
    # ATR ratio above which trading is paused (2.0 = ATR is 2× the average)
    "EXTREME_VOLATILITY_MAX_ATR_RATIO": float(os.getenv("EXTREME_VOLATILITY_MAX_ATR_RATIO", "2.5")),
    # Rolling window for ATR mean calculation (used by vol filter + regime detection)
    "ATR_MEAN_WINDOW": int(os.getenv("ATR_MEAN_WINDOW", "100")),

    # ── Regime Filter (market regime detection + strategy gating) ──
    "REGIME_FILTER_ENABLED": _bool(os.getenv("REGIME_FILTER_ENABLED", "true")),
    # Consecutive HH/HL or LH/LL count to declare TREND regime
    "REGIME_TREND_THRESHOLD": int(os.getenv("REGIME_TREND_THRESHOLD", "4")),
    # ATR ratio thresholds for volatility regimes
    "REGIME_HIGH_VOL_ATR_RATIO": float(os.getenv("REGIME_HIGH_VOL_ATR_RATIO", "2.0")),
    "REGIME_LOW_VOL_ATR_RATIO": float(os.getenv("REGIME_LOW_VOL_ATR_RATIO", "0.5")),
    # Lookback for directional strength calculation
    "REGIME_LOOKBACK": int(os.getenv("REGIME_LOOKBACK", "20")),
    # Range compression ratio: range_size/ATR < threshold → breakout imminent
    "REGIME_COMPRESSION_RATIO": float(os.getenv("REGIME_COMPRESSION_RATIO", "1.0")),

    # ── Liquidity Trap Filter ─────────────────────────
    # Block entry if price is within N×ATR of a high-score liquidity cluster
    "LIQUIDITY_TRAP_MIN_ATR_DIST": float(os.getenv("LIQUIDITY_TRAP_MIN_ATR_DIST", "0.3")),

    # ── Liquidity Heatmap ──────────────────────────────
    "LIQUIDITY_HEATMAP_ENABLED": _bool(os.getenv("LIQUIDITY_HEATMAP_ENABLED", "true")),
    "LIQUIDITY_HEATMAP_CLUSTER_PCT": float(os.getenv("LIQUIDITY_HEATMAP_CLUSTER_PCT", "0.001")),
    "LIQUIDITY_HEATMAP_MIN_CLUSTER_SIZE": int(os.getenv("LIQUIDITY_HEATMAP_MIN_CLUSTER_SIZE", "3")),
    "LIQUIDITY_HEATMAP_BREAKOUT_SL_ATR": float(os.getenv("LIQUIDITY_HEATMAP_BREAKOUT_SL_ATR", "0.5")),
    "LIQUIDITY_HEATMAP_MAX_LEVELS": int(os.getenv("LIQUIDITY_HEATMAP_MAX_LEVELS", "20")),
    # Scoring weights
    "LIQUIDITY_WEIGHT_EQUAL_HL": float(os.getenv("LIQUIDITY_WEIGHT_EQUAL_HL", "2.0")),
    "LIQUIDITY_WEIGHT_CLUSTER": float(os.getenv("LIQUIDITY_WEIGHT_CLUSTER", "3.0")),
    "LIQUIDITY_WEIGHT_BREAKOUT": float(os.getenv("LIQUIDITY_WEIGHT_BREAKOUT", "1.5")),
    "LIQUIDITY_WEIGHT_HTF": float(os.getenv("LIQUIDITY_WEIGHT_HTF", "2.0")),

    # ── Operation mode ──────────────────────────────────
    # "prod"              → strict, trading disabled when HTF NEUTRAL
    # "debug_permissive"  → allows neutral-bias trades, halved risk, extra logs
    "MODE":         os.getenv("SMC_MODE", "prod"),

    # Position Guardian (Safety Net)
    "GUARDIAN_ENABLED": _bool(os.getenv("GUARDIAN_ENABLED", "true")),
    "GUARDIAN_INTERVAL_SEC": int(os.getenv("GUARDIAN_INTERVAL_SEC", "30")),
    "GUARDIAN_MAX_LOSS_PCT": float(os.getenv("GUARDIAN_MAX_LOSS_PCT", "3.0")),
    "GUARDIAN_MAX_AGE_HOURS": float(os.getenv("GUARDIAN_MAX_AGE_HOURS", "24.0")),
    "GUARDIAN_AUTO_CLOSE": _bool(os.getenv("GUARDIAN_AUTO_CLOSE", "true")),

    # ── Symbols ─────────────────────────────────────────
    "SYMBOLS":      _list(os.getenv("BOT_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT")),
    "DISABLED_SYMBOLS": _list(os.getenv("DISABLED_SYMBOLS", "")),

    # ── Loop cadence ────────────────────────────────────
    "HTF_REFRESH_INTERVAL": int(os.getenv("SMC_HTF_REFRESH", "15")),
    "LOOP_SLEEP_SECONDS":   int(os.getenv("SMC_LOOP_SLEEP", "60")),

    # ── Risk sizing ─────────────────────────────────────
    "RISK_PER_TRADE_PCT":   float(os.getenv("RISK_PCT", os.getenv("BOT_RISK_PCT", "0.01"))), # Default safer 1%
    "DEFAULT_LEVERAGE":     int(os.getenv("BOT_LEVERAGE", "3")),
    
    # ── Auto Risk Upscale (REMOVED) ─────────────────────────
    # [INSTITUTIONAL AUDIT]: This anti-quant feature is HARD DELETED. 
    # Never override the user's risk. If raw_qty < minQty, the signal must be dropped.
    "AUTO_RISK_UPSCALE": False,
    
    # ── Small Account Mode ──────────────────────────────
    # Optimized for growing small capital (~$20-100) without deposits
    "SMALL_ACCOUNT_MODE":       _bool(os.getenv("SMALL_ACCOUNT_MODE", "false")),
    # Starting risk % for small accounts (progressive increase)
    "SMALL_ACC_RISK_START":     float(os.getenv("SMALL_ACC_RISK_START", "1.5")),
    # Maximum risk % allowed in small account mode
    "SMALL_ACC_RISK_MAX":       float(os.getenv("SMALL_ACC_RISK_MAX", "3.0")),
    # Risk increment step when auto-tuning (0.25% per rejection)
    "SMALL_ACC_RISK_STEP":      float(os.getenv("SMALL_ACC_RISK_STEP", "0.25")),

    # ── Neutral-bias permissive ─────────────────────────
    "NEUTRAL_PERMISSIVE":   _bool(os.getenv("SMC_NEUTRAL_PERMISSIVE", "true")),
    "NEUTRAL_RR_OVERRIDE":  float(os.getenv("SMC_NEUTRAL_RR", "3.0")),
    "NEUTRAL_RISK_FACTOR":  float(os.getenv("SMC_NEUTRAL_RISK_FACTOR", "0.5")),

    # ── Auto-Execution & Safeguards ─────────────────────
    # Standardized 3-state logic
    "AUTO_EXECUTE_MODE":    _AUTO_EXECUTE_MODE,
    "AUTO_EXECUTE_ENABLED": AUTO_EXECUTE_ENABLED,
    "SHADOW_MODE":          SHADOW_MODE,
    
    # Limits
    "MAX_TRADES_PER_DAY":   int(os.getenv("MAX_TRADES_PER_DAY", os.getenv("SMC_MAX_TRADES_DAY", "12"))),
    # Read MAX_DAILY_LOSS_PCT first (as documented in ENV_VARS.md), fallback to SMC_ prefix
    "MAX_DAILY_LOSS_PCT":   float(os.getenv("MAX_DAILY_LOSS_PCT", os.getenv("SMC_MAX_DAILY_LOSS_PCT", "3.0"))),
    "MAX_OPEN_TRADES_PER_SYMBOL": int(os.getenv("MAX_OPEN_TRADES_PER_SYMBOL", "1")),
    "TRADE_COOLDOWN_SEC":   int(os.getenv("TRADE_COOLDOWN_SEC", os.getenv("SMC_TRADE_COOLDOWN", "300"))),
    
    # Order Params
    "ORDER_TYPE":           os.getenv("SMC_ORDER_TYPE", "market"),
    "LIMIT_OFFSET_BPS":     int(os.getenv("SMC_LIMIT_OFFSET_BPS", "5")),
    
    # Global
    "FORCE_TRADE_ENABLED":  _bool(os.getenv("FORCE_TRADE_ENABLED", "false")),
    "TRADING_MODE":         _TRADING_MODE,
    "BITGET_PRODUCT_TYPE":  _NORMALIZED_PRODUCT_TYPE,
    
    # ── Force Execute Mode ──────────────────────
    # FORCE_EXECUTE=1 bypasses soft filters (cooldown, MIN_RR, session)
    # but NOT hard filters (minNotional, margin, kill switch)
    # WARNING: Hard-blocked when NODE_ENV=production (see top of file)
    "FORCE_EXECUTE":        _FORCE_EXECUTE,

    # ── Price source (Bitget V2) ─────────────────────
    "PRICE_SOURCE":         os.getenv("SMC_PRICE_SOURCE", "mark"),   # mark | last | index
    # [INSTITUTIONAL AUDIT]: 2000ms max allowed for a tick to be considered fresh.
    "STALE_THRESHOLD_MS":   min(2000, int(os.getenv("SMC_STALE_THRESHOLD_MS", "1000"))),
    
    # ── Entry Zone Relaxation ───────────────────────────
    # If True, bypasses NO_ENTRY_ZONE and trades at current price (if bias/trigger valid)
    "FORCE_ENTRY_ZONE":             _bool(os.getenv("FORCE_ENTRY_ZONE", "false")),
    # ATR multiplier to expand OB/FVG zones (0.0 = exact, 0.5 = 50% ATR buffer)
    "ENTRY_ZONE_PADDING_ATR":       float(os.getenv("ENTRY_ZONE_PADDING_ATR", "0.0")),
    # Max distance (in ATRs) from current price to valid zone to consider it "reachable"
    "ENTRY_ZONE_MAX_DISTANCE_ATR":  float(os.getenv("ENTRY_ZONE_MAX_DISTANCE_ATR", "100.0")),
    # Allow non-fresh (mitigated/old) FVGs to be valid zones
    "ENTRY_ZONE_ACCEPT_NONFRESH_FVG": _bool(os.getenv("ENTRY_ZONE_ACCEPT_NONFRESH_FVG", "false")),
    
    # ── Entry Zone Fallback (ATR-based zone when obs=0 & fvgs=0) ────
    # Enables intelligent fallback: creates ATR zone if bias+BOS confirmed but no OB/FVG
    "ENTRY_ZONE_FALLBACK_MODE":     _bool(os.getenv("ENTRY_ZONE_FALLBACK_MODE", "false")),
    # ATR multiplier for the entry zone width (+/- X * ATR around current price)
    "ENTRY_FALLBACK_ATR_MULT":      float(os.getenv("ENTRY_FALLBACK_ATR_MULT", "0.25")),
    # ATR multiplier for Stop Loss distance (SL = price +/- X * ATR)
    "ENTRY_FALLBACK_SL_ATR_MULT":   float(os.getenv("ENTRY_FALLBACK_SL_ATR_MULT", "1.5")),
    # Minimum R:R required for fallback ATR trades (stricter than normal)
    "ENTRY_FALLBACK_RR_MIN":        float(os.getenv("ENTRY_FALLBACK_RR_MIN", "2.0")),
    
    # ── Multi-Broker Safety ──────────────────────────────
    # MULTI_BROKER_MODE: disabled | fallback | mirror | split
    # This is the SINGLE SOURCE OF TRUTH for multi-broker behavior.
    "MULTI_BROKER_MODE":            MULTI_BROKER_MODE,
    # Explicit confirmation flag for live + auto + Kraken (fail-fast at startup)
    "CONFIRM_LIVE_MULTI_BROKER":    _bool(os.getenv("CONFIRM_LIVE_MULTI_BROKER", "false")),
    # Legacy compat: kept for backward reads, but MULTI_BROKER_MODE takes priority
    "MIRROR_TRADING_ENABLED":       MULTI_BROKER_MODE == "mirror" or _bool(os.getenv("MIRROR_TRADING_ENABLED", "false")),
    "KRAKEN_EXECUTION_ENABLED":     is_kraken_execution_enabled(),
    "KRAKEN_ENABLED":               _bool(os.getenv("KRAKEN_ENABLED", "false")),
    "BITGET_ENABLED":               _bool(os.getenv("BITGET_ENABLED", "false")),
    # EXECUTION_BROKER_MODE: KRAKEN_ONLY | BITGET_ONLY | MULTI_BROKER
    # Determines which broker's account state gates trades.
    # Auto-resolved from *_ENABLED flags if not set.
    "EXECUTION_BROKER_MODE":        os.getenv("EXECUTION_BROKER_MODE", "").upper() or None,
}

# ── Startup Validation ──────────────────────────────
# Fail fast if configuration is invalid / dangerous
print("\n" + "="*70, file=sys.stderr)
print("[BOOT] SMC Trading Bot Configuration", file=sys.stderr)
print("="*70, file=sys.stderr)

print(f"  TRADING_MODE:       {SMC_CONFIG['TRADING_MODE'].upper()}", file=sys.stderr)
print(f"  AUTO_EXECUTE_MODE:  {_AUTO_EXECUTE_MODE.upper()}", file=sys.stderr)
print(f"  FORCE_EXECUTE:      {SMC_CONFIG['FORCE_EXECUTE']}", file=sys.stderr)
print(f"  FORCE_ENTRY_ZONE:   {SMC_CONFIG['FORCE_ENTRY_ZONE']} (Bypass NO_ENTRY_ZONE)", file=sys.stderr)
print(f"  BITGET_PRODUCT:     {SMC_CONFIG['BITGET_PRODUCT_TYPE']}", file=sys.stderr)
print(f"  SYMBOLS:            {', '.join(SMC_CONFIG['SYMBOLS'])}", file=sys.stderr)

# Apply DISABLED_SYMBOLS filter
_disabled = set(s.upper() for s in SMC_CONFIG.get("DISABLED_SYMBOLS", []))
if _disabled:
    _before = SMC_CONFIG["SYMBOLS"]
    SMC_CONFIG["SYMBOLS"] = [s for s in _before if s.upper() not in _disabled]
    _removed = [s for s in _before if s.upper() in _disabled]
    for _sym in _removed:
        print(f"  ⛔ {_sym} disabled by DISABLED_SYMBOLS", file=sys.stderr)
    print(f"  ACTIVE SYMBOLS:     {', '.join(SMC_CONFIG['SYMBOLS'])}", file=sys.stderr)

print(f"  MAX_TRADES/DAY:     {SMC_CONFIG['MAX_TRADES_PER_DAY']}", file=sys.stderr)
print(f"  MIN_RR:             {SMC_CONFIG['MIN_RR']}", file=sys.stderr)
print(f"  SWEEP_FALLBACK:     {SMC_CONFIG['SWEEP_FALLBACK_ENABLED']} (RR>={SMC_CONFIG['SWEEP_FALLBACK_MIN_RR']})", file=sys.stderr)
print(f"  FALLBACK_ALLOW_NO_FVG: {SMC_CONFIG['FALLBACK_ALLOW_NO_FVG']}", file=sys.stderr)
print(f"  MIRROR_TRADING:     {SMC_CONFIG['MIRROR_TRADING_ENABLED']}", file=sys.stderr)
print(f"  CONFIRM_MULTI:      {SMC_CONFIG['CONFIRM_LIVE_MULTI_BROKER']}", file=sys.stderr)

# ── Multi-Broker Diagnostic Block ──────────────────────────
_kraken_on = _bool(os.getenv("KRAKEN_ENABLED", "false"))
_kraken_exec = _bool(os.getenv("KRAKEN_EXECUTION_ENABLED", "false"))
_kraken_key = bool(os.getenv("KRAKEN_API_KEY") or os.getenv("EXEC_KRAKEN_API_KEY"))
_kraken_secret = bool(os.getenv("KRAKEN_API_SECRET") or os.getenv("EXEC_KRAKEN_API_SECRET"))
_bitget_on = _bool(os.getenv("BITGET_ENABLED", "false"))
_exec_brokers = []
if _bitget_on:
    _exec_brokers.append("bitget")
if _kraken_on and _kraken_exec:
    _exec_brokers.append("kraken")
if not _exec_brokers:
    _exec_brokers.append("bitget")  # legacy default

from smc_futures.risk.account_state import resolve_execution_broker_mode as _rebm
_resolved_mode = SMC_CONFIG.get("EXECUTION_BROKER_MODE") or _rebm()

print(f"\n  [MULTI-BROKER DIAGNOSTIC]", file=sys.stderr)
print(f"    EXECUTION_BROKER_MODE:      {_resolved_mode}", file=sys.stderr)
print(f"    BITGET_ENABLED:             {_bitget_on}", file=sys.stderr)
print(f"    MULTI_BROKER_MODE:          {MULTI_BROKER_MODE.upper()}", file=sys.stderr)
print(f"    KRAKEN_ENABLED:             {_kraken_on}", file=sys.stderr)
print(f"    KRAKEN_EXECUTION_ENABLED:   {_kraken_exec}", file=sys.stderr)
print(f"    KRAKEN_API_KEY set:         {_kraken_key}", file=sys.stderr)
print(f"    KRAKEN_API_SECRET set:      {_kraken_secret}", file=sys.stderr)
print(f"    MIRROR_TRADING_ENABLED:     {SMC_CONFIG['MIRROR_TRADING_ENABLED']}", file=sys.stderr)
print(f"    CONFIRM_LIVE_MULTI_BROKER:  {SMC_CONFIG['CONFIRM_LIVE_MULTI_BROKER']}", file=sys.stderr)
print(f"    exec_brokers:               {_exec_brokers}", file=sys.stderr)
print(f"    [BITGET] mode=EXECUTE", file=sys.stderr)
if _kraken_on:
    _kr_mode = "EXECUTE" if _kraken_exec else "READ_ONLY"
    print(f"    [KRAKEN] mode={_kr_mode}", file=sys.stderr)
else:
    print(f"    [KRAKEN] mode=DISABLED (KRAKEN_ENABLED=false)", file=sys.stderr)

# Show gate chain status
_gates_ok = True
_gate_report = []
if not _kraken_on:
    _gate_report.append("FAIL: KRAKEN_ENABLED=false -> executor not registered")
    _gates_ok = False
else:
    _gate_report.append("OK: KRAKEN_ENABLED=true")
if not _kraken_exec:
    _gate_report.append("FAIL: KRAKEN_EXECUTION_ENABLED=false -> read-only")
    _gates_ok = False
else:
    _gate_report.append("OK: KRAKEN_EXECUTION_ENABLED=true")
if not _kraken_key or not _kraken_secret:
    _gate_report.append("FAIL: KRAKEN API keys not set -> cannot connect")
    _gates_ok = False
else:
    _gate_report.append("OK: KRAKEN API keys present")
if MULTI_BROKER_MODE == "disabled" and not SMC_CONFIG["MIRROR_TRADING_ENABLED"]:
    _gate_report.append("FAIL: MULTI_BROKER_MODE=disabled & MIRROR=false -> single-broker path")
    _gates_ok = False
else:
    _gate_report.append(f"OK: MULTI_BROKER_MODE={MULTI_BROKER_MODE}")

print(f"    --- GATE CHAIN {'ALL OK' if _gates_ok else 'BLOCKED'} ---", file=sys.stderr)
for _gr in _gate_report:
    print(f"      {_gr}", file=sys.stderr)
if not _gates_ok:
    print(f"    >>> Kraken will NOT execute trades until all gates are OK <<<", file=sys.stderr)
    print(f"    Required .env for multi-broker:", file=sys.stderr)
    print(f"      KRAKEN_ENABLED=true", file=sys.stderr)
    print(f"      KRAKEN_EXECUTION_ENABLED=true", file=sys.stderr)
    print(f"      KRAKEN_API_KEY=<your_key>", file=sys.stderr)
    print(f"      KRAKEN_API_SECRET=<your_secret>", file=sys.stderr)
    print(f"      MULTI_BROKER_MODE=mirror  # or fallback or split", file=sys.stderr)
    print(f"      CONFIRM_LIVE_MULTI_BROKER=true  # required for live mode", file=sys.stderr)

# Asia/Weekend Strategy thresholds (NEW)
print(f"\n  [ASIA STRATEGY]", file=sys.stderr)
print(f"    ENABLE_ASIA_STRATEGY:     {SMC_CONFIG.get('ENABLE_ASIA_STRATEGY', True)}", file=sys.stderr)
print(f"    ENABLE_WEEKEND_STRATEGY:  {SMC_CONFIG.get('ENABLE_WEEKEND_STRATEGY', True)}", file=sys.stderr)
print(f"    ASIA_MAX_RANGE_RATIO_BTC: {SMC_CONFIG.get('ASIA_MAX_RANGE_RATIO_BTC', 6.0)}", file=sys.stderr)
print(f"    ASIA_MAX_RANGE_RATIO_ETH: {SMC_CONFIG.get('ASIA_MAX_RANGE_RATIO_ETH', 5.0)}", file=sys.stderr)
print(f"    ASIA_MAX_RANGE_RATIO_ALT: {SMC_CONFIG.get('ASIA_MAX_RANGE_RATIO_ALT', 4.0)}", file=sys.stderr)
print(f"    ASIA_WEEKEND_RELAX_RANGE: {SMC_CONFIG.get('ASIA_WEEKEND_RELAX_RANGE', 1.3)}", file=sys.stderr)
print(f"    ASIA_TRENDING_RANGE:      {SMC_CONFIG.get('ASIA_TRENDING_RANGE_ALLOWED', True)}", file=sys.stderr)

if SMC_CONFIG["TRADING_MODE"] == "live":
    if AUTO_EXECUTE_ENABLED:
        print("\n🔴 [CRITICAL] LIVE TRADING + AUTO-EXECUTE ENABLED. REAL FUNDS AT RISK!", file=sys.stderr)
        print("    Verify this is intentional before proceeding.", file=sys.stderr)
    elif SHADOW_MODE:
        print("\n🟡 [WARNING] LIVE MODE (SHADOW). Orders will be simulated (DRY_RUN), no funds at risk.", file=sys.stderr)
    else:
        print("\n🟠 [CAUTION] LIVE MODE (MONITORING ONLY). Auto-execute disabled, signals broadcast only.", file=sys.stderr)
else:
    print("\n🟢 [INFO] PAPER TRADING. No real funds at risk.", file=sys.stderr)

print("="*70, file=sys.stderr)
