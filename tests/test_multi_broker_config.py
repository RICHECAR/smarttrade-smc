"""
Test Multi-Broker Configuration
================================
Simple tests to verify multi-broker setup works correctly.

Run: python -m pytest tests/test_multi_broker_config.py -v
"""

import pytest
import sys
import os

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_multi_broker_config_creation():
    """Test that MultiBrokerConfig can be created with all parameters."""
    from smc_futures.engine.multi_broker_engine import MultiBrokerConfig
    
    config = MultiBrokerConfig(
        default_broker="kraken",
        bitget_enabled=True,
        kraken_enabled=True,
        bitget_symbols=["BTCUSDT", "ETHUSDT"],
        kraken_symbols=["PI_XBTUSD", "PI_ETHUSD"],
        max_position_size_pct=0.1,
        loop_interval_sec=60,
    )
    
    assert config.default_broker == "kraken"
    assert config.bitget_enabled is True
    assert config.kraken_enabled is True
    assert config.bitget_symbols == ["BTCUSDT", "ETHUSDT"]
    assert config.kraken_symbols == ["PI_XBTUSD", "PI_ETHUSD"]
    assert config.max_position_size_pct == 0.1
    assert config.loop_interval_sec == 60


def test_multi_broker_config_from_env():
    """Test that MultiBrokerConfig can be loaded from environment."""
    from smc_futures.engine.multi_broker_engine import MultiBrokerConfig
    
    # Set test environment
    os.environ["BOT_SYMBOLS"] = "BTCUSDT,SOLUSDT"
    os.environ["KRAKEN_ENABLED"] = "true"
    os.environ["BOT_LEVERAGE"] = "5"
    
    config = MultiBrokerConfig.from_env()
    
    assert config.symbols == ["BTCUSDT", "SOLUSDT"]
    assert config.kraken_enabled is True
    assert config.default_leverage == 5


def test_multi_broker_engine_import():
    """Test that MultiBrokerEngine can be imported and instantiated."""
    from smc_futures.engine.multi_broker_engine import MultiBrokerEngine, MultiBrokerConfig
    
    config = MultiBrokerConfig(
        bitget_enabled=True,
        kraken_enabled=False,
    )
    
    engine = MultiBrokerEngine(config=config)
    
    assert engine is not None
    assert engine._running is False


def test_api_routes_import():
    """Test that multi-broker routes can be imported."""
    from backend.routes.multi_broker import router, set_multi_broker_engine
    
    assert router is not None
    assert callable(set_multi_broker_engine)


def test_api_smc_engine_import():
    """Test that api_smc_engine imports without error."""
    from smc_futures.api_smc_engine import app
    
    assert app is not None


def test_multi_broker_loop_import():
    """Test that multi_broker_loop can be imported."""
    from smc_futures.multi_broker_loop import SMCMultiBrokerStrategy, run_multi_broker_trading
    
    assert SMCMultiBrokerStrategy is not None
    assert callable(run_multi_broker_trading)


if __name__ == "__main__":
    # Run tests
    print("Testing Multi-Broker Configuration...")
    
    test_multi_broker_config_creation()
    print("✓ MultiBrokerConfig creation OK")
    
    test_multi_broker_config_from_env()
    print("✓ MultiBrokerConfig from_env OK")
    
    test_multi_broker_engine_import()
    print("✓ MultiBrokerEngine import OK")
    
    test_api_routes_import()
    print("✓ API routes import OK")
    
    test_api_smc_engine_import()
    print("✓ API SMC Engine import OK")
    
    test_multi_broker_loop_import()
    print("✓ Multi-broker loop import OK")
    
    print("\n" + "="*50)
    print("All multi-broker tests PASSED!")
    print("="*50)
