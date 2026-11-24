"""
Unit tests for position_sizing.py focusing on pre-flight checks and small account clamping.
"""
import pytest
from backend.risk.position_sizing import (
    compute_position_size_safe,
    get_min_risk_usd,
    get_max_risk_deviation_pct
)

def test_clamping_logic_acceptable_deviation(monkeypatch):
    """
    Test when raw qty is < min_qty but deviation is within MAX_RISK_DEVIATION_PCT.
    It should return status="CLAMPED" and qty clamped to min_qty.
    """
    # Mock environment settings
    monkeypatch.setenv("MAX_RISK_DEVIATION_PCT", "5000") # Huge allowance
    monkeypatch.setenv("MIN_RISK_USD", "0.5")
    
    # Let's say we have a 100 USDT account, 1% risk = 1 USDT.
    # Symbol "BTCUSDT" (min_qty 0.001)
    # Entry 50000, Stop 49000 (distance = 1000)
    # Raw qty = 1 / 1000 = 0.001 (meets min_qty)
    
    # What if distance is 5000?
    # Raw qty = 1 / 5000 = 0.0002.
    # min_qty is 0.001. Deviation = (0.001 - 0.0002)/0.0002 * 100 = 400%
    # With MAX_RISK_DEVIATION_PCT=5000, it should be CLAMPED to 0.001
    
    result, status, details = compute_position_size_safe(
        symbol="BTCUSDT",
        balance=100.0,
        balance_ccy="USDT",
        risk_pct=0.01,
        entry_price=50000.0,
        stop_price=45000.0,
        leverage=10
    )
    
    assert result is not None
    assert status == "CLAMPED"
    assert result.qty == 0.001
    assert result.risk_amount > 1.0 # Actual risk is 0.001 * 5000 = 5 USDT

def test_clamping_logic_unacceptable_deviation(monkeypatch):
    """
    Test when raw qty is < min_qty and deviation exceeds MAX_RISK_DEVIATION_PCT.
    It should return status="QTY_BELOW_MIN_CLAMP_EXCEEDS_MAX" and reject trade.
    """
    monkeypatch.setenv("MAX_RISK_DEVIATION_PCT", "100") # Strict allowance (100% max increase)
    monkeypatch.setenv("MIN_RISK_USD", "0.5")
    
    # Same scenario: 100 USDT account, 1% risk (1 USDT).
    # Entry 50000, Stop 40000 (distance = 10000)
    # Raw qty = 1 / 10000 = 0.0001
    # min_qty is 0.001. Deviation = 900%
    # With max deviation = 100%, it should be rejected.
    
    result, status, details = compute_position_size_safe(
        symbol="BTCUSDT",
        balance=100.0,
        balance_ccy="USDT",
        risk_pct=0.01,
        entry_price=50000.0,
        stop_price=40000.0,
        leverage=10
    )
    
    assert result is None
    assert status == "QTY_BELOW_MIN_CLAMP_EXCEEDS_MAX"

def test_notional_clamping(monkeypatch):
    """
    Test notional clamping. If clamped qty * entry_price < min_notional,
    it should further clamp the qty to meet min_notional.
    """
    monkeypatch.setenv("MAX_RISK_DEVIATION_PCT", "5000") # Huge allowance
    monkeypatch.setenv("MIN_RISK_USD", "0.5")
    
    # 20 USDT account, 1% risk = 0.2 USDT -> REJECTED because 0.2 < 0.5 (MIN_RISK_USD)
    # Let's use 100 USDT, 1% risk = 1 USDT.
    # Symbol "XRPUSDT" (min_qty 1.0, min_notional 5.0)
    # Entry 1.0, Stop 0.5 (distance = 0.5)
    # Raw qty = 1 / 0.5 = 2.0. (Meets min_qty 1.0)
    # Notional = 2.0 * 1.0 = 2.0 USDT < min_notional (5.0)
    # So qty should be clamped to 5.0 / 1.0 = 5.0
    
    result, status, details = compute_position_size_safe(
        symbol="XRPUSDT",
        balance=100.0,
        balance_ccy="USDT",
        risk_pct=0.01,
        entry_price=1.0,
        stop_price=0.5,
        leverage=10
    )
    
    assert result is not None
    assert status == "CLAMPED"
    assert result.qty == 5.0
    assert result.notional >= 5.0
