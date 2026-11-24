"""
Test Suite for SMC Multi-Timeframe Strategy (V3 — Production Hardened)
═══════════════════════════════════════════════════════════════════════
Original 7 suites (27 checks) + 12 new failure-mode tests = ~40 checks.

Tests:
 1. HTF candle gate (< 300 candles → NEUTRAL, trading_disabled)
 2. Counter-trend rejection (bearish HTF + long signal → blocked)
 3. Session filter rejection (Asian session)
 4. Golden combo (4H bullish + 15m OB + 5m CHoCH → CREATED)
 5. Neutral permissive mode
 6. Trade validator (R:R + sizing)
 7. Config loading
 8.  [NEW] is_uptrend=None → no crash (FM-2)
 9.  [NEW] Sweep sellside triggers LONG (FM-3)
 10. [NEW] Sweep buyside triggers SHORT (FM-3)
 11. [NEW] Entry zone None price skipped (FM-4)
 12. [NEW] Zero balance rejected (FM-5)
 13. [NEW] Weekend session rejected (FM-7)
 14. [NEW] Neutral permissive halves risk (FM-5 variant)
 15. [NEW] Bearish signal construction
 16. [NEW] DecisionTrace
"""

import sys
import os
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("TEST_SMC_MTF")

sys.path.insert(0, os.getcwd())

# ── Imports ──
from smc_futures.engine.strategy.mtf_bias import MTFBiasEngine, Bias
from smc_futures.engine.strategy.signal_builder import SignalBuilder, SignalResult
from smc_futures.engine.strategy.session_filter import SessionFilter
from smc_futures.engine.strategy.trade_validator import SMCTradeValidator
from smc_futures.engine.utils.reason_codes import ReasonCode
from smc_futures.smc_config import SMC_CONFIG
from smc_futures.smc_logger import DecisionTrace, new_trace

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        logger.info(f"✅ PASS: {label}")
    else:
        FAIL += 1
        logger.error(f"❌ FAIL: {label} | {detail}")


# ═══════════════════════════════════════════════════
#  TEST 1: HTF Candle Gate
# ═══════════════════════════════════════════════════
def test_htf_candle_gate():
    logger.info("\n═══ TEST 1: HTF Candle Gate ═══")
    engine = MTFBiasEngine(min_htf_candles=300, mode="prod")

    engine.update_htf_context({
        "candle_count": 120,
        "is_uptrend": True,
        "structure_strength": 0.8,
    })

    check(
        "HTF bias is NEUTRAL when candles < 300",
        engine.current_bias == Bias.NEUTRAL,
        f"Got {engine.current_bias}",
    )
    check(
        "Trading disabled in prod mode",
        engine.trading_disabled is True,
        f"Got {engine.trading_disabled}",
    )
    check(
        "Rejection reason is HTF_INSUFFICIENT_DATA",
        engine.rejection_reason == ReasonCode.HTF_INSUFFICIENT_DATA,
        f"Got {engine.rejection_reason}",
    )

    engine.update_htf_context({
        "candle_count": 500,
        "is_uptrend": True,
        "structure_strength": 0.8,
    })

    check(
        "HTF bias is BULLISH after 500 candles",
        engine.current_bias == Bias.BULLISH,
        f"Got {engine.current_bias}",
    )
    check(
        "Trading re-enabled",
        engine.trading_disabled is False,
        f"Got {engine.trading_disabled}",
    )


# ═══════════════════════════════════════════════════
#  TEST 2: Counter-Trend Rejection
# ═══════════════════════════════════════════════════
def test_countertrend_rejection():
    logger.info("\n═══ TEST 2: Counter-Trend Rejection ═══")
    engine = MTFBiasEngine(min_htf_candles=100, mode="prod", allow_countertrend=False)

    engine.update_htf_context({
        "candle_count": 500,
        "is_uptrend": False,
        "structure_strength": 0.8,
    })

    engine.update_ltf_data({
        "is_uptrend": True,
        "recent_bos": 1,
        "recent_choch": 0,
        "structure_strength": 0.6,
    })

    trigger = engine.check_ltf_trigger("BTCUSDT")

    check(
        "Counter-trend trigger is rejected",
        trigger["confirmed"] is False,
        f"Got confirmed={trigger['confirmed']}",
    )
    check(
        "Reason is COUNTERTREND_BLOCKED",
        trigger["reason"] == ReasonCode.COUNTERTREND_BLOCKED,
        f"Got reason={trigger['reason']}",
    )


# ═══════════════════════════════════════════════════
#  TEST 3: Session Filter
# ═══════════════════════════════════════════════════
def test_session_filter():
    logger.info("\n═══ TEST 3: Session Filter ═══")
    sf = SessionFilter(allowed_sessions=["london", "ny"])

    asian_ts = int(datetime(2026, 1, 15, 3, 0, 0).timestamp() * 1000)
    check("Asian session rejected", sf.is_in_session(asian_ts) is False)
    check("Session name is ASIAN/OFF", sf.get_session_name(asian_ts) == "ASIAN/OFF")

    london_ts = int(datetime(2026, 1, 15, 10, 0, 0).timestamp() * 1000)
    check("London session allowed", sf.is_in_session(london_ts) is True)

    overlap_ts = int(datetime(2026, 1, 15, 14, 0, 0).timestamp() * 1000)
    check("London-NY overlap allowed", sf.is_in_session(overlap_ts) is True)
    check(
        "Session name is LONDON_NY_OVERLAP",
        sf.get_session_name(overlap_ts) == "LONDON_NY_OVERLAP",
        f"Got {sf.get_session_name(overlap_ts)}",
    )


# ═══════════════════════════════════════════════════
#  TEST 4: Golden Combo (corrected: sellside sweep → LONG)
# ═══════════════════════════════════════════════════
def test_golden_combo():
    logger.info("\n═══ TEST 4: Golden Combo ═══")
    sb = SignalBuilder(min_rr_ratio=1.5, allow_countertrend=False)

    current_price = 50000.0
    timestamp = int(datetime.now().timestamp() * 1000)

    mtf_state = {
        "bias": "bullish",
        "bias_strength": 0.8,
        "trading_disabled": False,
    }
    ltf_trigger = {
        "confirmed": True,
        "trigger_type": "choch",
        "direction": "long",
        "reason": None,
    }
    structure_state = {
        "structure_strength": 0.8,
        "recent_choch": 1,
        "is_uptrend": True,
    }
    # FM-3 FIX: sellside sweep for LONG (institution hunts longs below → reversal up)
    liquidity_state = {
        "sweep_history": [{"type": "sellside"}],
        "session_low": 47500.0,
    }
    ob_state = {
        "blocks": [{"type": "bullish", "low": 49000.0, "high": 49200.0, "confidence": 0.9, "freshness": 1.0}],
    }
    fvg_state = {"fvgs": []}

    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state=structure_state,
        liquidity_state=liquidity_state,
        ob_state=ob_state,
        fvg_state=fvg_state,
        mtf_state=mtf_state,
        current_price=current_price,
        timestamp=timestamp,
        ltf_trigger=ltf_trigger,
    )

    check(
        "Golden combo produces CREATED signal",
        result.status == "CREATED",
        f"Got status={result.status}, reasons={result.reasons}",
    )
    check(
        "Signal direction is LONG",
        result.signal is not None and result.signal.direction.value == "long",
        f"Got signal={result.signal}",
    )
    if result.signal:
        check("Signal trigger is choch", result.signal.trigger_type == "choch")
        check(f"R:R >= 1.5 (got {result.signal.risk_reward_ratio:.2f})", result.signal.risk_reward_ratio >= 1.5)


# ═══════════════════════════════════════════════════
#  TEST 5: Neutral Permissive Mode
# ═══════════════════════════════════════════════════
def test_neutral_permissive():
    logger.info("\n═══ TEST 5: Neutral Permissive ═══")
    sb_strict = SignalBuilder(min_rr_ratio=1.5, neutral_permissive=False)
    result = sb_strict.build_signal(
        symbol="BTCUSDT",
        structure_state={},
        liquidity_state={"sweep_history": [{"type": "sellside"}]},
        ob_state={"blocks": [{"type": "bullish", "low": 49000.0, "confidence": 0.9}]},
        fvg_state={"fvgs": []},
        mtf_state={"bias": "neutral", "bias_strength": 0.5},
        current_price=50000.0,
        timestamp=0,
    )
    check("Strict mode rejects neutral bias", result.status == "NO_SIGNAL", f"Got {result.status}")


# ═══════════════════════════════════════════════════
#  TEST 6: Trade Validator
# ═══════════════════════════════════════════════════
def test_trade_validator():
    logger.info("\n═══ TEST 6: Trade Validator ═══")
    tv = SMCTradeValidator(min_rr=1.5, risk_per_trade_pct=1.0, max_leverage=10, account_balance=10000.0)

    result = tv.validate(
        symbol="BTCUSDT",
        signal_dict={"direction": "long", "entry": 50000.0, "stop_loss": 49000.0, "take_profit": 53000.0},
        current_price=50000.0,
    )
    check("Valid signal passes validation", result["valid"] is True, f"Got valid={result['valid']}, reason={result['reason']}")
    check(f"R:R = {result['rr']:.2f} >= 1.5", result["rr"] >= 1.5)

    result2 = tv.validate(
        symbol="BTCUSDT",
        signal_dict={"direction": "long", "entry": 50000.0, "stop_loss": 49000.0, "take_profit": 50500.0},
        current_price=50000.0,
    )
    check("Low R:R rejected", result2["valid"] is False)


# ═══════════════════════════════════════════════════
#  TEST 7: Config Loading
# ═══════════════════════════════════════════════════
def test_config():
    logger.info("\n═══ TEST 7: Config Loading ═══")
    check("HTF_TF is 4h", SMC_CONFIG["HTF_TF"] == "4h")
    check("SETUP_TF is 15m", SMC_CONFIG["SETUP_TF"] == "15m")
    check("ENTRY_TF is 5m", SMC_CONFIG["ENTRY_TF"] == "5m")
    check("MIN_HTF_CANDLES is 300", SMC_CONFIG["MIN_HTF_CANDLES"] == 300)
    check("MIN_RR is 1.5", SMC_CONFIG["MIN_RR"] == 1.5)
    check("SESSIONS includes london", "london" in SMC_CONFIG["SESSIONS"])
    check("ALLOW_COUNTERTREND is False", SMC_CONFIG["ALLOW_COUNTERTREND"] is False)


# ═══════════════════════════════════════════════════
#  TEST 8: is_uptrend=None → LTF_NO_CONFIRMATION (FM-2)
# ═══════════════════════════════════════════════════
def test_is_uptrend_none_no_crash():
    logger.info("\n═══ TEST 8: is_uptrend=None (FM-2) ═══")
    engine = MTFBiasEngine(min_htf_candles=100, mode="prod", allow_countertrend=False)

    engine.update_htf_context({
        "candle_count": 500,
        "is_uptrend": True,
        "structure_strength": 0.8,
    })

    # Feed LTF data with is_uptrend=None (undetermined)
    engine.update_ltf_data({
        "is_uptrend": None,
        "recent_bos": 0,
        "recent_choch": 0,
        "structure_strength": 0.0,
    })

    trigger = engine.check_ltf_trigger("BTCUSDT")

    check(
        "is_uptrend=None → not confirmed (no crash)",
        trigger["confirmed"] is False,
        f"Got {trigger}",
    )
    check(
        "Reason is LTF_NO_CONFIRMATION",
        trigger["reason"] == ReasonCode.LTF_NO_CONFIRMATION,
        f"Got {trigger['reason']}",
    )


# ═══════════════════════════════════════════════════
#  TEST 9: Sellside sweep triggers LONG (FM-3)
# ═══════════════════════════════════════════════════
def test_sweep_sellside_triggers_long():
    logger.info("\n═══ TEST 9: Sellside Sweep → LONG (FM-3) ═══")
    sb = SignalBuilder(min_rr_ratio=1.5)

    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state={"structure_strength": 0.8, "recent_choch": 1, "is_uptrend": True},
        liquidity_state={"sweep_history": [{"type": "sellside"}], "session_low": 47500.0},
        ob_state={"blocks": [{"type": "bullish", "low": 49000.0, "high": 49200.0, "confidence": 0.9, "freshness": 1.0}]},
        fvg_state={"fvgs": []},
        mtf_state={"bias": "bullish", "bias_strength": 0.8, "trading_disabled": False},
        current_price=50000.0,
        timestamp=int(datetime.now().timestamp() * 1000),
        ltf_trigger={"confirmed": True, "trigger_type": "choch", "direction": "long", "reason": None},
    )

    check(
        "Sellside sweep produces CREATED LONG",
        result.status == "CREATED" and result.signal is not None,
        f"Got status={result.status}, reasons={result.reasons}",
    )

    # Buyside sweep should be REJECTED for bullish
    result2 = sb.build_signal(
        symbol="BTCUSDT",
        structure_state={"structure_strength": 0.8},
        liquidity_state={"sweep_history": [{"type": "buyside"}]},
        ob_state={"blocks": []},
        fvg_state={"fvgs": []},
        mtf_state={"bias": "bullish", "bias_strength": 0.8, "trading_disabled": False},
        current_price=50000.0,
        timestamp=0,
        ltf_trigger={"confirmed": True, "trigger_type": "choch", "direction": "long", "reason": None},
    )
    check(
        "Buyside sweep REJECTED for bullish bias",
        result2.status == "REJECTED",
        f"Got {result2.status}, reasons={result2.reasons}",
    )


# ═══════════════════════════════════════════════════
#  TEST 10: Buyside sweep triggers SHORT (FM-3)
# ═══════════════════════════════════════════════════
def test_sweep_buyside_triggers_short():
    logger.info("\n═══ TEST 10: Buyside Sweep → SHORT (FM-3) ═══")
    sb = SignalBuilder(min_rr_ratio=1.5)

    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state={"structure_strength": 0.8, "recent_choch": 1, "is_uptrend": False},
        liquidity_state={"sweep_history": [{"type": "buyside"}], "session_high": 52000.0},
        ob_state={"blocks": [{"type": "bearish", "high": 51000.0, "low": 50800.0, "confidence": 0.9, "freshness": 1.0}]},
        fvg_state={"fvgs": []},
        mtf_state={"bias": "bearish", "bias_strength": 0.8, "trading_disabled": False},
        current_price=50500.0,
        timestamp=int(datetime.now().timestamp() * 1000),
        ltf_trigger={"confirmed": True, "trigger_type": "choch", "direction": "short", "reason": None},
    )

    check(
        "Buyside sweep produces CREATED SHORT",
        result.status == "CREATED" and result.signal is not None,
        f"Got status={result.status}, reasons={result.reasons}",
    )


# ═══════════════════════════════════════════════════
#  TEST 11: Entry zone None price skipped (FM-4)
# ═══════════════════════════════════════════════════
def test_entry_zone_none_price():
    logger.info("\n═══ TEST 11: Entry Zone None Price (FM-4) ═══")
    sb = SignalBuilder(min_rr_ratio=1.5)

    # OB with missing "low" key → price=None
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state={"structure_strength": 0.8},
        liquidity_state={"sweep_history": [{"type": "sellside"}]},
        ob_state={"blocks": [{"type": "bullish", "confidence": 0.9, "freshness": 1.0}]},  # no "low"!
        fvg_state={"fvgs": []},
        mtf_state={"bias": "bullish", "bias_strength": 0.8, "trading_disabled": False},
        current_price=50000.0,
        timestamp=0,
        ltf_trigger={"confirmed": True, "trigger_type": "choch", "direction": "long", "reason": None},
    )

    check(
        "None price OB→ NO_SIGNAL (no crash)",
        result.status in ("NO_SIGNAL", "REJECTED"),
        f"Got status={result.status}, reasons={result.reasons}",
    )


# ═══════════════════════════════════════════════════
#  TEST 12: Zero balance rejected (FM-5)
# ═══════════════════════════════════════════════════
def test_zero_balance_rejected():
    logger.info("\n═══ TEST 12: Zero Balance (FM-5) ═══")
    tv = SMCTradeValidator(min_rr=1.5, risk_per_trade_pct=1.0, max_leverage=10, account_balance=0.0)

    result = tv.validate(
        symbol="BTCUSDT",
        signal_dict={"direction": "long", "entry": 50000.0, "stop_loss": 49000.0, "take_profit": 53000.0},
        current_price=50000.0,
    )

    check(
        "Zero balance → valid=False",
        result["valid"] is False,
        f"Got valid={result['valid']}",
    )
    check(
        "Reason contains INSUFFICIENT_BALANCE",
        "INSUFFICIENT_BALANCE" in str(result["reason"]),
        f"Got reason={result['reason']}",
    )


# ═══════════════════════════════════════════════════
#  TEST 13: Weekend session rejected (FM-7)
# ═══════════════════════════════════════════════════
def test_weekend_session_rejected():
    logger.info("\n═══ TEST 13: Weekend Session (FM-7) ═══")
    sf = SessionFilter(allowed_sessions=["london", "ny"])

    # Saturday 14:00 UTC (2026-01-17 is a Saturday)
    sat_ts = int(datetime(2026, 1, 17, 14, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    check(
        "Saturday 14h UTC rejected",
        sf.is_in_session(sat_ts) is False,
        f"Got {sf.is_in_session(sat_ts)} (weekday={datetime(2026, 1, 17).weekday()})",
    )

    # Sunday 10:00 UTC (2026-01-18 is a Sunday)
    sun_ts = int(datetime(2026, 1, 18, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    check(
        "Sunday 10h UTC rejected",
        sf.is_in_session(sun_ts) is False,
    )

    # Monday 10:00 UTC should pass
    mon_ts = int(datetime(2026, 1, 19, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    check(
        "Monday 10h UTC allowed",
        sf.is_in_session(mon_ts) is True,
    )


# ═══════════════════════════════════════════════════
#  TEST 14: Neutral permissive halves risk (FM-5 variant)
# ═══════════════════════════════════════════════════
def test_neutral_permissive_halves_risk():
    logger.info("\n═══ TEST 14: Neutral Halves Risk ═══")
    tv = SMCTradeValidator(min_rr=1.5, risk_per_trade_pct=2.0, max_leverage=10, account_balance=10000.0)

    # Normal risk
    r1 = tv.validate(
        symbol="BTCUSDT",
        signal_dict={"direction": "long", "entry": 50000.0, "stop_loss": 49000.0, "take_profit": 53000.0},
        current_price=50000.0,
        neutral_override=False,
    )

    # Neutral override → halved risk
    r2 = tv.validate(
        symbol="BTCUSDT",
        signal_dict={"direction": "long", "entry": 50000.0, "stop_loss": 49000.0, "take_profit": 55000.0},
        current_price=50000.0,
        neutral_override=True,
    )

    check(
        f"Normal risk_amount={r1['risk_amount']:.2f}",
        r1["valid"] is True,
    )
    check(
        f"Neutral risk_amount={r2['risk_amount']:.2f} is half of normal",
        abs(r2["risk_amount"] - r1["risk_amount"] / 2) < 0.01,
        f"Normal={r1['risk_amount']}, Neutral={r2['risk_amount']}",
    )


# ═══════════════════════════════════════════════════
#  TEST 15: Bearish signal construction
# ═══════════════════════════════════════════════════
def test_bearish_signal_construction():
    logger.info("\n═══ TEST 15: Bearish Signal ═══")
    sb = SignalBuilder(min_rr_ratio=1.5)

    result = sb.build_signal(
        symbol="ETHUSDT",
        structure_state={"structure_strength": 0.8, "is_uptrend": False},
        liquidity_state={"sweep_history": [{"type": "buyside"}], "session_high": 4200.0},
        ob_state={"blocks": [{"type": "bearish", "high": 4100.0, "low": 4050.0, "confidence": 0.85, "freshness": 1.0}]},
        fvg_state={"fvgs": []},
        mtf_state={"bias": "bearish", "bias_strength": 0.8, "trading_disabled": False},
        current_price=4050.0,
        timestamp=int(datetime.now().timestamp() * 1000),
        ltf_trigger={"confirmed": True, "trigger_type": "bos", "direction": "short", "reason": None},
    )

    check(
        "Bearish signal CREATED",
        result.status == "CREATED",
        f"Got {result.status}, reasons={result.reasons}",
    )
    if result.signal:
        check("Direction is SHORT", result.signal.direction.value == "short")
        check("Trigger is bos", result.signal.trigger_type == "bos")
        check(f"SL > entry ({result.signal.stop_loss:.2f} > {result.signal.entry_price:.2f})",
              result.signal.stop_loss > result.signal.entry_price)
        check(f"TP < entry ({result.signal.take_profit:.2f} < {result.signal.entry_price:.2f})",
              result.signal.take_profit < result.signal.entry_price)


# ═══════════════════════════════════════════════════
#  TEST 16: DecisionTrace
# ═══════════════════════════════════════════════════
def test_decision_trace():
    logger.info("\n═══ TEST 16: DecisionTrace ═══")
    trace = new_trace("BTCUSDT")

    trace.add("HTF", "PASS", "BULLISH", candles=512)
    trace.add("SESSION", "PASS", "LONDON_NY_OVERLAP")
    trace.add("MTF", "PASS", "OB_found", ob_count=2)
    trace.add("LTF", "PASS", "choch", direction="long")
    trace.add("RISK", "REJECT", "LOW_RR", rr=1.2)

    check("Trace has 5 steps", len(trace.decisions) == 5)
    check("Final action is REJECT", trace.final_action == "REJECT")
    check("Final layer is RISK", trace.final_layer == "RISK")

    d = trace.to_dict()
    check("to_dict has cycle_id", len(d["cycle_id"]) == 8)
    check("to_dict has symbol", d["symbol"] == "BTCUSDT")

    j = trace.to_json()
    check("to_json is valid JSON", '"type": "decision_trace"' in j)


# ═══════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  SMC Multi-TF Strategy Test Suite (V3 — Production Hardened)")
    logger.info("=" * 60)

    test_htf_candle_gate()
    test_countertrend_rejection()
    test_session_filter()
    test_golden_combo()
    test_neutral_permissive()
    test_trade_validator()
    test_config()

    # New FM tests
    test_is_uptrend_none_no_crash()
    test_sweep_sellside_triggers_long()
    test_sweep_buyside_triggers_short()
    test_entry_zone_none_price()
    test_zero_balance_rejected()
    test_weekend_session_rejected()
    test_neutral_permissive_halves_risk()
    test_bearish_signal_construction()
    test_decision_trace()

    logger.info("\n" + "=" * 60)
    logger.info(f"  RESULTS: {PASS} passed, {FAIL} failed")
    logger.info("=" * 60)

    sys.exit(1 if FAIL > 0 else 0)
