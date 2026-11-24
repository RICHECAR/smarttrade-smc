import pytest
import time
from unittest.mock import patch, MagicMock
from smc_futures.engine.risk.capital_allocator import CapitalAllocator, CapitalRegime

# Define a mock for the metrics to avoid global state issues
@pytest.fixture
def mock_metrics():
    with patch('smc_futures.engine.risk.capital_allocator.ORPHAN_DETECTED_TOTAL') as m1, \
         patch('smc_futures.engine.risk.capital_allocator.LATE_FILL_TOTAL') as m2, \
         patch('smc_futures.engine.risk.capital_allocator.STALE_REJECTION_RATE') as m3:
        
        m1._value.get.return_value = 0
        m2._value.get.return_value = 0
        m3.labels.return_value._value.get.return_value = 0.0
        yield m1, m2, m3

def test_initial_regime(mock_metrics):
    allocator = CapitalAllocator(initial_regime=CapitalRegime.MICRO)
    assert allocator.regime == CapitalRegime.MICRO
    params = allocator.get_allocation_params("BTCUSDT", 10000.0, 1.0)
    assert params["regime"] == "MICRO"
    assert params["risk_multiplier"] == 0.25
    assert params["execution_allowed"] is True

def test_frozen_on_incident(mock_metrics):
    m1, m2, m3 = mock_metrics
    allocator = CapitalAllocator(initial_regime=CapitalRegime.NORMAL)
    
    # Mock an orphan incident
    m1._value.get.return_value = 1
    
    params = allocator.get_allocation_params("BTCUSDT", 10000.0, 1.0)
    assert allocator.regime == CapitalRegime.FROZEN
    assert params["regime"] == "FROZEN"
    assert params["risk_multiplier"] == 0.0
    assert params["execution_allowed"] is False

def test_retraction_on_stale(mock_metrics):
    m1, m2, m3 = mock_metrics
    allocator = CapitalAllocator(initial_regime=CapitalRegime.NORMAL)
    
    # Mock high stale rate (> 20%)
    m3.labels.return_value._value.get.return_value = 0.25
    
    params = allocator.get_allocation_params("BTCUSDT", 10000.0, 1.0)
    assert allocator.regime == CapitalRegime.RETRACTION
    assert params["risk_multiplier"] < 0.1 # Should be penalised further by quality score
    assert params["execution_allowed"] is True

def test_progressive_scaling_flow(mock_metrics):
    m1, m2, m3 = mock_metrics
    # 1. Start in MICRO
    allocator = CapitalAllocator(initial_regime=CapitalRegime.MICRO)
    m3.labels.return_value._value.get.return_value = 0.01 # Perfect infra
    
    # 2. Try to scale to NORMAL before 24h (should fail)
    allocator.get_allocation_params("BTCUSDT", 10000.0, 1.0)
    assert allocator.regime == CapitalRegime.MICRO
    
    # 3. Fast-forward 25h
    allocator.last_regime_change_ts -= (25 * 3600)
    allocator.get_allocation_params("BTCUSDT", 10000.0, 1.0)
    assert allocator.regime == CapitalRegime.NORMAL
    
    # 4. Try to scale to EXPANSION before 72h (should fail)
    allocator.get_allocation_params("BTCUSDT", 10000.0, 1.0)
    assert allocator.regime == CapitalRegime.NORMAL
    
    # 5. Fast-forward 73h
    allocator.last_regime_change_ts -= (73 * 3600)
    allocator.get_allocation_params("BTCUSDT", 10000.0, 1.0)
    assert allocator.regime == CapitalRegime.EXPANSION

if __name__ == "__main__":
    pytest.main([__file__])
