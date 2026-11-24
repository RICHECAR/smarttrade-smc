"""
SMC Multi-Timeframe Strategy Configuration — REAL ONLY Mode
Loaded from .env with sensible defaults.

REAL ONLY PRINCIPLES:
  1. TRADING_MODE must be "live" (no paper allowed)
  2. AUTO_EXECUTE must be "true" (no shadow/false allowed)
  3. Fail-fast on startup if misconfigured
  4. BITGET_PRODUCT_TYPE always "USDT-FUTURES"
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()


def _bool(val: str) -> bool:
    return val.strip().lower() in ("true", "1", "yes")


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
AUTO_EXECUTE_ENABLED = (_AUTO_EXECUTE_MODE == "true")
SHADOW_MODE = (_AUTO_EXECUTE_MODE == "shadow")

# 3. REAL ONLY Enforcement: Fail-Fast if misconfigured
# ═════════════════════════════════════════════════════
# If you want REAL trading, you MUST set BOTH:
#   TRADING_MODE=live
#   AUTO_EXECUTE=true
# We enforce this strictly at startup to prevent accidental paper trading

# Optional: Require explicit ENV var to enable REAL ONLY strict mode
REAL_ONLY_MODE = _bool(os.getenv("BITGET_REAL_ONLY", "false"))

if REAL_ONLY_MODE:
    # STRICT enforcement: must be live + true, REJECT shadow/paper/false
    if _TRADING_MODE != "live":
        raise ValueError(
            f"[REAL_ONLY] CRITICAL ERROR: BITGET_REAL_ONLY=true requires TRADING_MODE=live\n"
            f"  Current TRADING_MODE={_TRADING_MODE}\n"
            f"  → Set TRADING_MODE=live in .env\n"
            f"  OR: Remove BITGET_REAL_ONLY or set to false"
        )
    
    if _AUTO_EXECUTE_MODE != "true":
        raise ValueError(
            f"[REAL_ONLY] CRITICAL ERROR: BITGET_REAL_ONLY=true requires AUTO_EXECUTE=true\n"
            f"  Current AUTO_EXECUTE={_RAW_AUTO_EXECUTE}\n"
            f"  → Set AUTO_EXECUTE=true in .env (NO paper/shadow/false)\n"
            f"  OR: Remove BITGET_REAL_ONLY or set to false"
        )

# Additional safety: if AUTO_EXECUTE=true, ensure TRADING_MODE is NOT paper
if AUTO_EXECUTE_ENABLED and _TRADING_MODE == "paper":
    # Contradiction: said "execute" but mode is paper → force MONITORING ONLY
    print("[CONFIG_WARN] AUTO_EXECUTE=true but TRADING_MODE=paper → Disabling AUTO_EXECUTE for safety", file=sys.stderr)
    if not SHADOW_MODE:
        AUTO_EXECUTE_ENABLED = False


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

    # ── Session filter ──────────────────────────────────
    "SESSIONS":     _list(os.getenv("SMC_SESSIONS", "london,ny")),

    # ── Counter-trend ───────────────────────────────────
    "ALLOW_COUNTERTREND": _bool(os.getenv("SMC_ALLOW_COUNTERTREND", "false")),

    # ── Operation mode ──────────────────────────────────
    "MODE":         os.getenv("SMC_MODE", "prod"),

    # ── Symbols ─────────────────────────────────────────
    "SYMBOLS":      _list(os.getenv("BOT_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT")),

    # ── Loop cadence ────────────────────────────────────
    "HTF_REFRESH_INTERVAL": int(os.getenv("SMC_HTF_REFRESH", "15")),
    "LOOP_SLEEP_SECONDS":   int(os.getenv("SMC_LOOP_SLEEP", "60")),

    # ── Risk sizing ─────────────────────────────────────
    "RISK_PER_TRADE_PCT":   float(os.getenv("RISK_PCT", os.getenv("BOT_RISK_PCT", "0.01"))),
    "DEFAULT_LEVERAGE":     int(os.getenv("BOT_LEVERAGE", "3")),

    # ── Neutral-bias permissive ─────────────────────────
    "NEUTRAL_PERMISSIVE":   _bool(os.getenv("SMC_NEUTRAL_PERMISSIVE", "true")),
    "NEUTRAL_RR_OVERRIDE":  float(os.getenv("SMC_NEUTRAL_RR", "3.0")),
    "NEUTRAL_RISK_FACTOR":  float(os.getenv("SMC_NEUTRAL_RISK_FACTOR", "0.5")),

    # ── Auto-Execution & Safeguards ─────────────────────
    "AUTO_EXECUTE_MODE":    _AUTO_EXECUTE_MODE,
    "AUTO_EXECUTE_ENABLED": AUTO_EXECUTE_ENABLED,
    "SHADOW_MODE":          SHADOW_MODE,
    "REAL_ONLY_MODE":       REAL_ONLY_MODE,
    
    # Limits
    "MAX_TRADES_PER_DAY":   int(os.getenv("MAX_TRADES_PER_DAY", os.getenv("SMC_MAX_TRADES_DAY", "1"))),
    "MAX_DAILY_LOSS_PCT":   float(os.getenv("SMC_MAX_DAILY_LOSS_PCT", "2.0")),
    "MAX_OPEN_TRADES_PER_SYMBOL": int(os.getenv("MAX_OPEN_TRADES_PER_SYMBOL", "1")),
    "TRADE_COOLDOWN_SEC":   int(os.getenv("TRADE_COOLDOWN_SEC", os.getenv("SMC_TRADE_COOLDOWN", "300"))),
    
    # Order Params
    "ORDER_TYPE":           os.getenv("SMC_ORDER_TYPE", "market"),
    "LIMIT_OFFSET_BPS":     int(os.getenv("SMC_LIMIT_OFFSET_BPS", "5")),
    
    # Global
    "FORCE_TRADE_ENABLED":  _bool(os.getenv("FORCE_TRADE_ENABLED", "false")),
    "TRADING_MODE":         _TRADING_MODE,
    "BITGET_PRODUCT_TYPE":  _NORMALIZED_PRODUCT_TYPE,

    # ── Price source (Bitget V2) ─────────────────────
    "PRICE_SOURCE":         os.getenv("SMC_PRICE_SOURCE", "mark"),
    "STALE_THRESHOLD_MS":   int(os.getenv("SMC_STALE_THRESHOLD_MS", "10000")),
}

# ── Startup Validation ──────────────────────────────
print("\n" + "="*70, file=sys.stderr)
print("[BOOT] SMC Trading Bot Configuration", file=sys.stderr)
print("="*70, file=sys.stderr)

print(f"  TRADING_MODE:       {SMC_CONFIG['TRADING_MODE'].upper()}", file=sys.stderr)
print(f"  AUTO_EXECUTE_MODE:  {_AUTO_EXECUTE_MODE.upper()}", file=sys.stderr)
print(f"  REAL_ONLY_MODE:     {REAL_ONLY_MODE}", file=sys.stderr)
print(f"  BITGET_PRODUCT:     {SMC_CONFIG['BITGET_PRODUCT_TYPE']}", file=sys.stderr)
print(f"  SYMBOLS:            {', '.join(SMC_CONFIG['SYMBOLS'])}", file=sys.stderr)
print(f"  MAX_TRADES/DAY:     {SMC_CONFIG['MAX_TRADES_PER_DAY']}", file=sys.stderr)
print(f"  MIN_RR:             {SMC_CONFIG['MIN_RR']}", file=sys.stderr)

if SMC_CONFIG["TRADING_MODE"] == "live":
    if AUTO_EXECUTE_ENABLED:
        print("\n🔴 [CRITICAL] LIVE TRADING + AUTO-EXECUTE ENABLED. REAL FUNDS AT RISK!", file=sys.stderr)
        print("    Verify this is intentional before proceeding.", file=sys.stderr)
    elif SHADOW_MODE:
        print("\n🟡 [WARNING] LIVE MODE (SHADOW). Orders will be simulated (DRY_RUN).", file=sys.stderr)
    else:
        print("\n🟠 [CAUTION] LIVE MODE (MONITORING ONLY). Signals broadcast, no execution.", file=sys.stderr)
else:
    print("\n🟢 [INFO] PAPER TRADING. No real funds at risk.", file=sys.stderr)

print("="*70, file=sys.stderr)
