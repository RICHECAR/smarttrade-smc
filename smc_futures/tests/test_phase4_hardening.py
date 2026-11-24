import pytest
import time
from unittest.mock import patch, MagicMock
from smc_futures.engine.risk.global_risk_engine import GlobalRiskEngine
from smc_futures.observability.metrics import ORPHAN_DETECTED_TOTAL, LATE_FILL_TOTAL

@pytest.fixture
def risk_engine():
    # Force fresh instance for testing
    engine = GlobalRiskEngine()
    engine.kill_switch_active = False
    engine.last_failsafe_timestamp_ms = 0
    engine.cooldown_duration_ms = 60000
    
    # Reset prometheus mocks technically
    ORPHAN_DETECTED_TOTAL._value.set(0)
    LATE_FILL_TOTAL._value.set(0)
    
    return engine

def test_execution_mode_lock(risk_engine):
    """Ensure ANY intent trying to use Strict Mirror is forced into Asymmetric phase 4 mode."""
    intent = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": 1.0,
        "spread_bps": 2.0,
        "timestamp_ms": int(time.time() * 1000),
        "execution_mode": "STRICT_MIRROR" # Auto-execute attacker
    }
    
    is_valid, reason = risk_engine.validate_intent(intent)
    
    assert is_valid is True
    # The mode must be forcefully locked to Asymmetric Maker-Taker
    assert intent.get("execution_mode") == "ASYMMETRIC_MAKER_TAKER"

def test_cooldown_enforced(risk_engine):
    """Ensure that after a failsafe trigger, intents are blocked during the duration."""
    # Simulate a Failsafe that just happened
    risk_engine.trigger_cooldown("MOCK_FAILSAFE_TEST")
    
    intent = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": 1.0,
        "spread_bps": 2.0,
        "timestamp_ms": int(time.time() * 1000)
    }
    
    is_valid, reason = risk_engine.validate_intent(intent)
    
    assert is_valid is False
    assert "COOLDOWN_ACTIVE" in reason
    
    # Force time forwarding
    risk_engine.last_failsafe_timestamp_ms = int(time.time() * 1000) - 61000 # 61s ago
    
    # Update timestamp to pass the stale check again
    intent["timestamp_ms"] = int(time.time() * 1000)
    is_valid_again, _ = risk_engine.validate_intent(intent)
    assert is_valid_again is True

def test_orphan_freeze_behavior(risk_engine):
    """Ensure that the engine hard-locks forever if the prometheus metrics report an orphan."""
    intent = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": 1.0,
        "spread_bps": 2.0,
        "timestamp_ms": int(time.time() * 1000)
    }
    
    assert risk_engine.validate_intent(intent)[0] is True
    
    # Simulate Async metrics catching an orphan
    ORPHAN_DETECTED_TOTAL.inc()
    
    is_valid, reason = risk_engine.validate_intent(intent)
    assert is_valid is False
    assert "orphan" in reason.lower() or "CAPITAL_BLOCKED" in reason
    assert risk_engine.kill_switch_active is True
