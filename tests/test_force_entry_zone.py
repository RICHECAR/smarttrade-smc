"""
Tests for FORCE_ENTRY_ZONE and ENTRY_ZONE_FALLBACK_MODE functionality.

These tests verify:
1. FORCE_ENTRY_ZONE=1 bypasses NO_ENTRY_ZONE and creates ATR-based zone
2. ENTRY_ZONE_FALLBACK_MODE=1 creates ATR zone if bias+BOS confirmed
3. Without flags, obs=0 & fvgs=0 results in NO_SIGNAL
4. Hard safety filters are NEVER bypassed (tested separately)
"""

import sys
import os
import pytest

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from smc_futures.engine.strategy.signal_builder import SignalBuilder, SignalResult


# ──────────────────────────────────────────────────────────────
#  Fixtures for common test states
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def bullish_mtf_state():
    """Strong bullish bias from HTF"""
    return {"bias": "bullish", "bias_strength": 0.8, "trading_disabled": False}


@pytest.fixture
def bearish_mtf_state():
    """Strong bearish bias from HTF"""
    return {"bias": "bearish", "bias_strength": 0.8, "trading_disabled": False}


@pytest.fixture
def neutral_mtf_state():
    """Neutral bias - no clear direction"""
    return {"bias": "neutral", "bias_strength": 0.5, "trading_disabled": False}


@pytest.fixture
def empty_ob_state():
    """No Order Blocks detected"""
    return {"blocks": []}


@pytest.fixture
def empty_fvg_state():
    """No Fair Value Gaps detected"""
    return {"fvgs": []}


@pytest.fixture
def valid_liquidity_state():
    """Liquidity state with sweep and ATR"""
    return {
        "sweep_history": [{"type": "sellside", "price": 95.0}],
        "atr": 100.0,  # $100 ATR for easy math
        "session_low": 90.0,
        "session_high": 110.0,
    }


@pytest.fixture
def structure_with_bos():
    """Structure state with BOS confirmed"""
    return {"recent_bos": 1, "recent_choch": 0, "structure_strength": 0.7}


@pytest.fixture
def structure_without_bos():
    """Structure state without BOS"""
    return {"recent_bos": 0, "recent_choch": 0, "structure_strength": 0.3}


# ──────────────────────────────────────────────────────────────
#  Test 1: FORCE_ENTRY_ZONE bypasses NO_ENTRY_ZONE
# ──────────────────────────────────────────────────────────────

def test_force_entry_zone_bypasses_no_entry_zone(
    bullish_mtf_state, empty_ob_state, empty_fvg_state, 
    valid_liquidity_state, structure_with_bos
):
    """
    GIVEN: obs=0, fvgs=0, FORCE_ENTRY_ZONE=True
    WHEN: build_signal is called
    THEN: Signal status should be CREATED (not NO_SIGNAL)
    AND: Signal should have ATR_FALLBACK in setup_type
    AND: Reasons should contain FORCE_ENTRY_ZONE bypass message
    """
    sb = SignalBuilder(
        force_entry_zone=True,
        entry_fallback_atr_mult=0.25,
        entry_fallback_sl_atr_mult=1.5,
        entry_fallback_rr_min=2.0,
    )
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state=structure_with_bos,
        liquidity_state=valid_liquidity_state,
        ob_state=empty_ob_state,
        fvg_state=empty_fvg_state,
        mtf_state=bullish_mtf_state,
        current_price=100.0,
        timestamp=1000,
    )
    
    assert result.status == "CREATED", f"Expected CREATED, got {result.status}. Reasons: {result.reasons}"
    assert result.signal is not None, "Signal should not be None"
    assert "ATR_FALLBACK" in result.signal.setup_type, f"Expected ATR_FALLBACK in setup_type, got {result.signal.setup_type}"
    assert any("FORCE_ENTRY_ZONE" in r for r in result.reasons), f"Expected FORCE_ENTRY_ZONE in reasons: {result.reasons}"


def test_force_entry_zone_bearish(
    bearish_mtf_state, empty_ob_state, empty_fvg_state,
    structure_with_bos
):
    """
    GIVEN: Bearish bias, obs=0, fvgs=0, FORCE_ENTRY_ZONE=True
    WHEN: build_signal is called
    THEN: Signal should be SHORT direction with ATR_FALLBACK
    """
    sb = SignalBuilder(
        force_entry_zone=True,
        entry_fallback_atr_mult=0.25,
        entry_fallback_sl_atr_mult=1.5,
        entry_fallback_rr_min=2.0,
    )
    
    # Bearish liquidity state (buyside sweep for bearish)
    liquidity_state = {
        "sweep_history": [{"type": "buyside", "price": 105.0}],
        "atr": 100.0,
        "session_low": 90.0,
        "session_high": 110.0,
    }
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state=structure_with_bos,
        liquidity_state=liquidity_state,
        ob_state=empty_ob_state,
        fvg_state=empty_fvg_state,
        mtf_state=bearish_mtf_state,
        current_price=100.0,
        timestamp=1000,
    )
    
    assert result.status == "CREATED", f"Expected CREATED, got {result.status}. Reasons: {result.reasons}"
    assert result.signal.direction.value == "short", f"Expected short, got {result.signal.direction.value}"


# ──────────────────────────────────────────────────────────────
#  Test 2: Without flags, NO_SIGNAL is returned
# ──────────────────────────────────────────────────────────────

def test_no_flags_returns_no_signal(
    bullish_mtf_state, empty_ob_state, empty_fvg_state,
    valid_liquidity_state, structure_with_bos
):
    """
    GIVEN: obs=0, fvgs=0, FORCE_ENTRY_ZONE=False, ENTRY_ZONE_FALLBACK_MODE=False
    WHEN: build_signal is called
    THEN: Signal status should be NO_SIGNAL
    AND: Reasons should contain NO_ENTRY_ZONE
    """
    sb = SignalBuilder(
        force_entry_zone=False,
        entry_fallback_mode=False,
    )
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state=structure_with_bos,
        liquidity_state=valid_liquidity_state,
        ob_state=empty_ob_state,
        fvg_state=empty_fvg_state,
        mtf_state=bullish_mtf_state,
        current_price=100.0,
        timestamp=1000,
    )
    
    assert result.status == "NO_SIGNAL", f"Expected NO_SIGNAL, got {result.status}"
    assert any("NO_ENTRY_ZONE" in r for r in result.reasons), f"Expected NO_ENTRY_ZONE in reasons: {result.reasons}"


# ──────────────────────────────────────────────────────────────
#  Test 3: ENTRY_ZONE_FALLBACK_MODE with bias + BOS
# ──────────────────────────────────────────────────────────────

def test_entry_fallback_mode_with_bos(
    bullish_mtf_state, empty_ob_state, empty_fvg_state,
    valid_liquidity_state, structure_with_bos
):
    """
    GIVEN: obs=0, fvgs=0, ENTRY_ZONE_FALLBACK_MODE=True, bias=bullish, BOS=True
    WHEN: build_signal is called
    THEN: Signal status should be CREATED with ATR_FALLBACK
    AND: Reasons should contain ENTRY_FALLBACK message
    """
    sb = SignalBuilder(
        force_entry_zone=False,
        entry_fallback_mode=True,
        entry_fallback_atr_mult=0.25,
        entry_fallback_sl_atr_mult=1.5,
        entry_fallback_rr_min=2.0,
    )
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state=structure_with_bos,
        liquidity_state=valid_liquidity_state,
        ob_state=empty_ob_state,
        fvg_state=empty_fvg_state,
        mtf_state=bullish_mtf_state,
        current_price=100.0,
        timestamp=1000,
    )
    
    assert result.status == "CREATED", f"Expected CREATED, got {result.status}. Reasons: {result.reasons}"
    assert "ATR_FALLBACK" in result.signal.setup_type, f"Expected ATR_FALLBACK, got {result.signal.setup_type}"
    assert any("ENTRY_FALLBACK" in r for r in result.reasons), f"Expected ENTRY_FALLBACK in reasons: {result.reasons}"


def test_entry_fallback_mode_without_bos(
    bullish_mtf_state, empty_ob_state, empty_fvg_state,
    valid_liquidity_state, structure_without_bos
):
    """
    GIVEN: obs=0, fvgs=0, ENTRY_ZONE_FALLBACK_MODE=True, bias=bullish, BOS=False, weak structure
    WHEN: build_signal is called
    THEN: Signal status should be NO_SIGNAL (fallback denied)
    """
    sb = SignalBuilder(
        force_entry_zone=False,
        entry_fallback_mode=True,
        entry_fallback_atr_mult=0.25,
        entry_fallback_sl_atr_mult=1.5,
        entry_fallback_rr_min=2.0,
    )
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state=structure_without_bos,
        liquidity_state=valid_liquidity_state,
        ob_state=empty_ob_state,
        fvg_state=empty_fvg_state,
        mtf_state=bullish_mtf_state,
        current_price=100.0,
        timestamp=1000,
    )
    
    assert result.status == "NO_SIGNAL", f"Expected NO_SIGNAL, got {result.status}"
    assert any("fallback_denied" in r or "NO_ENTRY_ZONE" in r for r in result.reasons), \
        f"Expected fallback_denied or NO_ENTRY_ZONE in reasons: {result.reasons}"


# ──────────────────────────────────────────────────────────────
#  Test 4: Neutral bias blocks fallback (requires bias)
# ──────────────────────────────────────────────────────────────

def test_entry_fallback_mode_neutral_bias(
    neutral_mtf_state, empty_ob_state, empty_fvg_state,
    valid_liquidity_state, structure_with_bos
):
    """
    GIVEN: obs=0, fvgs=0, ENTRY_ZONE_FALLBACK_MODE=True, bias=neutral
    WHEN: build_signal is called
    THEN: Signal status should be NO_SIGNAL (neutral bias blocks fallback)
    
    Note: Neutral bias is handled earlier in the flow before NO_ENTRY_ZONE
    unless neutral_permissive=True and other conditions are met.
    """
    sb = SignalBuilder(
        force_entry_zone=False,
        entry_fallback_mode=True,
        neutral_permissive=False,  # Strict neutral handling
    )
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state=structure_with_bos,
        liquidity_state=valid_liquidity_state,
        ob_state=empty_ob_state,
        fvg_state=empty_fvg_state,
        mtf_state=neutral_mtf_state,
        current_price=100.0,
        timestamp=1000,
    )
    
    # Neutral bias is rejected before reaching NO_ENTRY_ZONE logic
    assert result.status == "NO_SIGNAL", f"Expected NO_SIGNAL, got {result.status}"


# ──────────────────────────────────────────────────────────────
#  Test 5: ATR-based SL/TP calculation
# ──────────────────────────────────────────────────────────────

def test_atr_fallback_zone_sl_tp_calculation(
    bullish_mtf_state, empty_ob_state, empty_fvg_state,
    structure_with_bos
):
    """
    GIVEN: FORCE_ENTRY_ZONE=True, ATR=100, price=1000
    WHEN: build_signal creates ATR fallback zone
    THEN: SL should be price - (SL_MULT * ATR) = 1000 - 150 = 850
    AND: TP should give at least RR_MIN of 2.0
    """
    sb = SignalBuilder(
        force_entry_zone=True,
        entry_fallback_atr_mult=0.25,       # Zone: 1000 +/- 25
        entry_fallback_sl_atr_mult=1.5,     # SL: 1000 - 150 = 850
        entry_fallback_rr_min=2.0,          # TP: 1000 + 300 = 1300
    )
    
    liquidity_state = {
        "sweep_history": [{"type": "sellside", "price": 950.0}],
        "atr": 100.0,
        "session_low": 900.0,
        "session_high": 1100.0,
    }
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state=structure_with_bos,
        liquidity_state=liquidity_state,
        ob_state=empty_ob_state,
        fvg_state=empty_fvg_state,
        mtf_state=bullish_mtf_state,
        current_price=1000.0,
        timestamp=1000,
    )
    
    assert result.status == "CREATED", f"Expected CREATED, got {result.status}. Reasons: {result.reasons}"
    
    signal = result.signal
    assert signal.entry_price == 1000.0, f"Entry should be 1000, got {signal.entry_price}"
    assert signal.stop_loss == 850.0, f"SL should be 850, got {signal.stop_loss}"
    assert signal.take_profit == 1300.0, f"TP should be 1300, got {signal.take_profit}"
    assert signal.risk_reward_ratio >= 2.0, f"RR should be >= 2.0, got {signal.risk_reward_ratio}"


def test_atr_fallback_zone_bearish_sl_tp(
    bearish_mtf_state, empty_ob_state, empty_fvg_state,
    structure_with_bos
):
    """
    GIVEN: Bearish bias, FORCE_ENTRY_ZONE=True, ATR=100, price=1000
    WHEN: build_signal creates ATR fallback zone
    THEN: SL should be price + (SL_MULT * ATR) = 1000 + 150 = 1150
    AND: TP should be price - risk * RR_MIN = 700
    """
    sb = SignalBuilder(
        force_entry_zone=True,
        entry_fallback_atr_mult=0.25,
        entry_fallback_sl_atr_mult=1.5,
        entry_fallback_rr_min=2.0,
    )
    
    liquidity_state = {
        "sweep_history": [{"type": "buyside", "price": 1050.0}],
        "atr": 100.0,
        "session_low": 900.0,
        "session_high": 1100.0,
    }
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state=structure_with_bos,
        liquidity_state=liquidity_state,
        ob_state=empty_ob_state,
        fvg_state=empty_fvg_state,
        mtf_state=bearish_mtf_state,
        current_price=1000.0,
        timestamp=1000,
    )
    
    assert result.status == "CREATED", f"Expected CREATED, got {result.status}. Reasons: {result.reasons}"
    
    signal = result.signal
    assert signal.entry_price == 1000.0, f"Entry should be 1000, got {signal.entry_price}"
    assert signal.stop_loss == 1150.0, f"SL should be 1150, got {signal.stop_loss}"
    assert signal.take_profit == 700.0, f"TP should be 700, got {signal.take_profit}"


# ──────────────────────────────────────────────────────────────
#  Test 6: RR minimum enforcement for ATR fallback
# ──────────────────────────────────────────────────────────────

def test_atr_fallback_rr_minimum_enforced():
    """
    GIVEN: entry_fallback_rr_min=3.0 (stricter)
    WHEN: ATR fallback signal is created
    THEN: The required RR should be at least 3.0
    """
    sb = SignalBuilder(
        force_entry_zone=True,
        entry_fallback_atr_mult=0.25,
        entry_fallback_sl_atr_mult=1.5,
        entry_fallback_rr_min=3.0,  # Stricter RR
        min_rr_ratio=1.5,           # Base RR
    )
    
    liquidity_state = {
        "sweep_history": [{"type": "sellside", "price": 950.0}],
        "atr": 100.0,
        "session_low": 900.0,
        "session_high": 1100.0,
    }
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state={"recent_bos": 1, "structure_strength": 0.7},
        liquidity_state=liquidity_state,
        ob_state={"blocks": []},
        fvg_state={"fvgs": []},
        mtf_state={"bias": "bullish", "bias_strength": 0.8},
        current_price=1000.0,
        timestamp=1000,
    )
    
    assert result.status == "CREATED", f"Expected CREATED, got {result.status}. Reasons: {result.reasons}"
    # The ATR fallback zone ensures RR >= entry_fallback_rr_min
    assert result.signal.risk_reward_ratio >= 3.0, \
        f"RR should be >= 3.0, got {result.signal.risk_reward_ratio}"


# ──────────────────────────────────────────────────────────────
#  Test 7: Enhanced diagnostic logging in NO_ENTRY_ZONE
# ──────────────────────────────────────────────────────────────

def test_no_entry_zone_diagnostic_in_reasons():
    """
    GIVEN: obs with wrong type, fvgs with wrong type
    WHEN: NO_ENTRY_ZONE is triggered (no flags set)
    THEN: Reasons should contain diagnostic hint about why zones were rejected
    """
    sb = SignalBuilder(
        force_entry_zone=False,
        entry_fallback_mode=False,
    )
    
    # Bullish bias but bearish OB/FVG (wrong type)
    ob_state = {"blocks": [{"type": "bearish", "high": 1050, "confidence": 0.8}]}
    fvg_state = {"fvgs": [{"type": "bearish", "top": 1040, "age_bars": 5}]}
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state={"recent_bos": 1, "structure_strength": 0.7},
        liquidity_state={
            "sweep_history": [{"type": "sellside", "price": 950.0}],
            "atr": 100.0,
            "session_low": 900.0,
        },
        ob_state=ob_state,
        fvg_state=fvg_state,
        mtf_state={"bias": "bullish", "bias_strength": 0.8},
        current_price=1000.0,
        timestamp=1000,
    )
    
    assert result.status == "NO_SIGNAL", f"Expected NO_SIGNAL, got {result.status}"
    # Check that diagnostic info is in reasons
    reason_str = " ".join(result.reasons)
    assert "NO_ENTRY_ZONE" in reason_str, f"Expected NO_ENTRY_ZONE in reasons: {result.reasons}"


# ──────────────────────────────────────────────────────────────
#  Test 8: Valid OB/FVG still works normally
# ──────────────────────────────────────────────────────────────

def test_valid_ob_fvg_works_without_fallback():
    """
    GIVEN: Valid bullish OB, FORCE_ENTRY_ZONE=False
    WHEN: build_signal is called
    THEN: Signal should be CREATED using OB (not ATR fallback)
    """
    sb = SignalBuilder(
        force_entry_zone=False,
        entry_fallback_mode=False,
    )
    
    # Valid bullish OB
    ob_state = {
        "blocks": [{
            "type": "bullish",
            "low": 980.0,
            "high": 990.0,
            "confidence": 0.8,
            "freshness": 1.0,
            "age_bars": 3,
        }]
    }
    
    result = sb.build_signal(
        symbol="BTCUSDT",
        structure_state={"recent_bos": 1, "structure_strength": 0.7},
        liquidity_state={
            "sweep_history": [{"type": "sellside", "price": 950.0}],
            "atr": 20.0,
            "session_low": 960.0,
            "session_high": 1020.0,
        },
        ob_state=ob_state,
        fvg_state={"fvgs": []},
        mtf_state={"bias": "bullish", "bias_strength": 0.8},
        current_price=1000.0,
        timestamp=1000,
    )
    
    assert result.status == "CREATED", f"Expected CREATED, got {result.status}. Reasons: {result.reasons}"
    assert "ATR_FALLBACK" not in result.signal.setup_type, \
        f"Should use OB, not ATR fallback. Got: {result.signal.setup_type}"
    assert "order_block" in result.signal.setup_type.lower(), \
        f"Expected order_block in setup_type, got {result.signal.setup_type}"


# ──────────────────────────────────────────────────────────────
#  Test 9: _build_atr_fallback_zone helper method
# ──────────────────────────────────────────────────────────────

def test_build_atr_fallback_zone_method():
    """
    Test the _build_atr_fallback_zone helper method directly
    """
    sb = SignalBuilder(
        entry_fallback_atr_mult=0.25,
        entry_fallback_sl_atr_mult=1.5,
        entry_fallback_rr_min=2.0,
    )
    
    zone = sb._build_atr_fallback_zone(
        current_price=1000.0,
        atr=100.0,
        bias="bullish",
        zone_mult=0.25,
        sl_mult=1.5,
    )
    
    assert zone["type"] == "ATR_FALLBACK"
    assert zone["price"] == 1000.0
    assert zone["zone_low"] == 975.0    # 1000 - 0.25*100
    assert zone["zone_high"] == 1025.0  # 1000 + 0.25*100
    assert zone["sl_offset"] == 150.0   # 1.5 * 100
    assert zone["sl_price"] == 850.0    # 1000 - 150 (bullish SL below)
    assert zone["tp_price"] == 1300.0   # 1000 + 150*2 (RR=2)
    assert zone["is_atr_fallback"] is True
    assert zone["confidence"] == 0.4    # Lower confidence


def test_build_atr_fallback_zone_bearish():
    """
    Test ATR fallback zone for bearish direction
    """
    sb = SignalBuilder(
        entry_fallback_rr_min=2.0,
    )
    
    zone = sb._build_atr_fallback_zone(
        current_price=1000.0,
        atr=100.0,
        bias="bearish",
        zone_mult=0.25,
        sl_mult=1.5,
    )
    
    assert zone["sl_price"] == 1150.0   # 1000 + 150 (bearish SL above)
    assert zone["tp_price"] == 700.0    # 1000 - 150*2 (RR=2)


def test_build_atr_fallback_zone_zero_atr_fallback():
    """
    Test ATR fallback zone with zero ATR (should use price-based fallback)
    """
    sb = SignalBuilder(
        entry_fallback_rr_min=2.0,
    )
    
    zone = sb._build_atr_fallback_zone(
        current_price=1000.0,
        atr=0.0,  # Zero ATR
        bias="bullish",
        zone_mult=0.25,
        sl_mult=1.5,
    )
    
    # Should fallback to 0.1% of price = 1.0
    assert zone["sl_offset"] == 1.5  # 1.5 * 1.0
    assert zone["sl_price"] == 998.5  # 1000 - 1.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
