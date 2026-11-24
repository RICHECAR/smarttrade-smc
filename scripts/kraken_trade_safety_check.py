#!/usr/bin/env python3
"""
Kraken Trade Safety Check — Dry-Run Diagnostic Script
═══════════════════════════════════════════════════════

Validates the entire Kraken order pipeline WITHOUT sending anything to the exchange.
Produces a PASS/FAIL report for each critical check.

Usage:
    python scripts/kraken_trade_safety_check.py

Exit codes:
    0 = all checks passed
    1 = one or more checks failed

Author: SMC Trading Bot Framework
"""

import os
import sys
import importlib
from pathlib import Path

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / "safe-live.env")
load_dotenv(ROOT / ".env", override=True)

# ── Suppress all logging to stderr (prevents PowerShell exit code=1) ──
import logging
logging.disable(logging.CRITICAL)

# ── Fix Windows console encoding ──
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ══════════════════════════════════════════════════════════════
#  RESULTS TRACKER
# ══════════════════════════════════════════════════════════════

_results: list = []


def check(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    _results.append((name, status, detail))
    icon = "\u2705" if passed else "\u274c"
    print(f"  {icon} [{status}] {name}" + (f" — {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════
#  1. IMPORTS & MODULE AVAILABILITY
# ══════════════════════════════════════════════════════════════

print("\n=== 1. Module Import Checks ===")

try:
    from backend.execution.order_intent import (
        OrderIntent,
        OrderIntentError,
        InvalidReduceOnlyUsageError,
        InvalidKrakenQuantityError,
        InvalidProtectionOrderError,
        validate_order_intent,
        validate_kraken_order_payload,
        resolve_reduce_only,
        map_to_kraken_params,
        get_close_side,
    )
    check("order_intent module imports", True)
except Exception as e:
    check("order_intent module imports", False, str(e))

try:
    from backend.execution.kraken_futures_executor import (
        KrakenFuturesExecutorWrapper,
        to_kraken_symbol,
        from_kraken_symbol,
        SYMBOL_MAP_TO_KRAKEN,
    )
    check("kraken_futures_executor imports", True)
except Exception as e:
    check("kraken_futures_executor imports", False, str(e))

try:
    from backend.risk.position_safety_guard import PositionSafetyGuard, GuardConfig
    check("position_safety_guard imports", True)
except Exception as e:
    check("position_safety_guard imports", False, str(e))

try:
    from backend.services.close_position_service import (
        close_position, _detect_broker, _get_executor_for_symbol
    )
    check("close_position_service imports", True)
except Exception as e:
    check("close_position_service imports", False, str(e))


# ══════════════════════════════════════════════════════════════
#  2. OrderIntent VALIDATION
# ══════════════════════════════════════════════════════════════

print("\n=== 2. OrderIntent Validation ===")

# 2a. OPEN must force reduce_only=False
ro = resolve_reduce_only(OrderIntent.OPEN, "kraken", explicit_reduce_only=True)
check("OPEN intent forces reduce_only=False", ro is False, f"got reduce_only={ro}")

# 2b. CLOSE must return reduce_only=True
ro = resolve_reduce_only(OrderIntent.CLOSE, "kraken")
check("CLOSE intent gets reduce_only=True", ro is True)

# 2c. validate_kraken_order_payload blocks OPEN + reduce_only=True
try:
    validate_kraken_order_payload(
        intent=OrderIntent.OPEN, symbol="PF_XBTUSD",
        side="buy", quantity=100, reduce_only=True,
    )
    check("validate_kraken_order_payload blocks OPEN+reduce_only", False,
          "expected InvalidReduceOnlyUsageError")
except InvalidReduceOnlyUsageError:
    check("validate_kraken_order_payload blocks OPEN+reduce_only", True)
except Exception as e:
    check("validate_kraken_order_payload blocks OPEN+reduce_only", False, str(e))

# 2d. Fractional qty rejected
try:
    validate_kraken_order_payload(
        intent=OrderIntent.OPEN, symbol="PF_XBTUSD",
        side="buy", quantity=0.5,
    )
    check("validate_kraken_order_payload blocks fractional qty", False,
          "expected InvalidKrakenQuantityError")
except InvalidKrakenQuantityError:
    check("validate_kraken_order_payload blocks fractional qty", True)
except Exception as e:
    check("validate_kraken_order_payload blocks fractional qty", False, str(e))

# 2e. SL on wrong side rejected
try:
    validate_kraken_order_payload(
        intent=OrderIntent.STOP_LOSS, symbol="PF_XBTUSD",
        side="sell", quantity=100, trigger_price=55000,
        entry_price=50000, position_side="long",
    )
    check("SL above entry for LONG rejected", False,
          "expected InvalidProtectionOrderError")
except InvalidProtectionOrderError:
    check("SL above entry for LONG rejected", True)
except Exception as e:
    check("SL above entry for LONG rejected", False, str(e))

# 2f. TP on wrong side rejected
try:
    validate_kraken_order_payload(
        intent=OrderIntent.TAKE_PROFIT, symbol="PF_XBTUSD",
        side="sell", quantity=100, trigger_price=45000,
        entry_price=50000, position_side="long",
    )
    check("TP below entry for LONG rejected", False,
          "expected InvalidProtectionOrderError")
except InvalidProtectionOrderError:
    check("TP below entry for LONG rejected", True)
except Exception as e:
    check("TP below entry for LONG rejected", False, str(e))


# ══════════════════════════════════════════════════════════════
#  3. SYMBOL MAPPING
# ══════════════════════════════════════════════════════════════

print("\n=== 3. Symbol Mapping ===")

for bitget, expected_kraken in [
    ("BTCUSDT", "PF_XBTUSD"),
    ("ETHUSDT", "PF_ETHUSD"),
    ("SOLUSDT", "PF_SOLUSD"),
]:
    result = to_kraken_symbol(bitget)
    check(f"{bitget} -> {expected_kraken}", result == expected_kraken, f"got {result}")

# No double-prefix
result = to_kraken_symbol("PF_XBTUSD")
check("PF_XBTUSD not double-prefixed", result == "PF_XBTUSD", f"got {result}")

# Round-trip
for sym in ["BTCUSDT", "ETHUSDT"]:
    kraken = to_kraken_symbol(sym)
    back = from_kraken_symbol(kraken)
    check(f"Round-trip {sym} -> {kraken} -> {back}", back == sym)


# ══════════════════════════════════════════════════════════════
#  4. SAFETY GUARD CONFIG
# ══════════════════════════════════════════════════════════════

print("\n=== 4. SafetyGuard Config ===")

cfg = GuardConfig()
check("guard enabled", cfg.enabled is True)
check("emergency_close_on_sl_fail enabled", cfg.emergency_close_on_sl_fail is True)
check("protection_repair_cooldown_sec > 0", cfg.protection_repair_cooldown_sec > 0,
      f"value={cfg.protection_repair_cooldown_sec}")
check("max_protection_failures > 0", cfg.max_protection_failures > 0,
      f"value={cfg.max_protection_failures}")
check("force_close_if_unprotected enabled", cfg.force_close_if_unprotected is True)
check("retry_count > 0", cfg.retry_count > 0, f"value={cfg.retry_count}")


# ══════════════════════════════════════════════════════════════
#  5. BROKER DETECTION
# ══════════════════════════════════════════════════════════════

print("\n=== 5. Broker Detection ===")

for sym, expected_broker in [
    ("PF_XBTUSD", "kraken"),
    ("PI_ETHUSD", "kraken"),
    ("BTCUSDT", "bitget"),
    ("ETHUSDT", "bitget"),
]:
    result = _detect_broker(sym)
    check(f"_detect_broker({sym}) = {expected_broker}", result == expected_broker,
          f"got {result}")


# ══════════════════════════════════════════════════════════════
#  6. ENV VARIABLE AUDIT
# ══════════════════════════════════════════════════════════════

print("\n=== 6. .env Variable Audit ===")

required_vars = [
    "TRADING_ALLOW_REDUCE_ONLY_FOR_OPEN",
    "KRAKEN_STRICT_ORDER_VALIDATION",
    "KRAKEN_RETRY_ON_ORDER_REJECT",
    "RISK_GUARD_ENABLED",
    "RISK_EMERGENCY_CLOSE_ON_SL_FAIL",
    "RISK_PROTECTION_REPAIR_COOLDOWN_SEC",
    "RISK_MAX_PROTECTION_FAILURES",
    "RISK_FORCE_CLOSE_IF_UNPROTECTED",
    "KRAKEN_CONFIRM_CLOSE_RETRIES",
    "KRAKEN_CONFIRM_PROTECTION_RETRIES",
]

for var in required_vars:
    val = os.getenv(var)
    check(f"{var} is set", val is not None, f"value={val}")

# Safety-critical: TRADING_ALLOW_REDUCE_ONLY_FOR_OPEN must be false
val = os.getenv("TRADING_ALLOW_REDUCE_ONLY_FOR_OPEN", "true").lower()
check("TRADING_ALLOW_REDUCE_ONLY_FOR_OPEN == false", val == "false", f"value={val}")


# ══════════════════════════════════════════════════════════════
#  7. KRAKEN MAP-TO-PARAMS DRY RUN
# ══════════════════════════════════════════════════════════════

print("\n=== 7. Kraken Order Payload Dry Run ===")

# Simulate a full OPEN order payload
params = validate_order_intent(
    intent=OrderIntent.OPEN, symbol="PF_XBTUSD",
    side="buy", quantity=100, exchange="kraken",
)
kraken_payload = map_to_kraken_params(params)
check("OPEN payload has no reduceOnly", "reduceOnly" not in kraken_payload,
      f"payload={kraken_payload}")
check("OPEN payload orderType == mkt", kraken_payload.get("orderType") == "mkt")
check("OPEN payload size == 100", kraken_payload.get("size") == 100)

# Simulate a CLOSE order payload
params = validate_order_intent(
    intent=OrderIntent.CLOSE, symbol="PF_XBTUSD",
    side="sell", quantity=100, exchange="kraken",
)
kraken_payload = map_to_kraken_params(params)
check("CLOSE payload has reduceOnly='true'", kraken_payload.get("reduceOnly") == "true")

# Simulate a STOP_LOSS payload
params = validate_order_intent(
    intent=OrderIntent.STOP_LOSS, symbol="PF_XBTUSD",
    side="sell", quantity=100, trigger_price=48000.0, exchange="kraken",
)
kraken_payload = map_to_kraken_params(params)
check("SL payload orderType == stp", kraken_payload.get("orderType") == "stp")
check("SL payload has stopPrice", kraken_payload.get("stopPrice") == 48000.0)
check("SL payload reduceOnly == 'true'", kraken_payload.get("reduceOnly") == "true")


# ══════════════════════════════════════════════════════════════
#  FINAL REPORT
# ══════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
total = len(_results)
passed = sum(1 for _, s, _ in _results if s == "PASS")
failed = total - passed

if failed == 0:
    print(f"\u2705 ALL {total} CHECKS PASSED — Kraken pipeline is safe for production.")
else:
    print(f"\u274c {failed}/{total} CHECKS FAILED — DO NOT GO LIVE until fixed:")
    for name, status, detail in _results:
        if status == "FAIL":
            print(f"   \u274c {name}: {detail}")

print("=" * 60)
sys.exit(0 if failed == 0 else 1)
