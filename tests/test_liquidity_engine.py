"""Test LiquidityEngine initialization and basic functionality"""

import pytest
from smc_futures.engine.strategy.liquidity import LiquidityEngine


class TestLiquidityEngineInit:
    """Test LiquidityEngine accepts all config parameters"""

    def test_default_init(self):
        """Default initialization without args"""
        le = LiquidityEngine()
        assert le.lookback_period == 50
        assert le.min_wick_atr == 0.3
        assert le.allow_equal_hl is True
        assert le.require_close_beyond is False

    def test_custom_min_wick_atr(self):
        """Custom min_wick_atr parameter"""
        le = LiquidityEngine(min_wick_atr=0.5)
        assert le.min_wick_atr == 0.5

    def test_full_config(self):
        """All parameters explicitly set"""
        le = LiquidityEngine(
            lookback_period=100,
            cluster_tolerance=0.005,
            min_wick_atr=0.2,
            allow_equal_hl=False,
            require_close_beyond=True,
        )
        assert le.lookback_period == 100
        assert le.cluster_tolerance == 0.005
        assert le.min_wick_atr == 0.2
        assert le.allow_equal_hl is False
        assert le.require_close_beyond is True

    def test_get_current_state_includes_config(self):
        """get_current_state returns config echo"""
        le = LiquidityEngine(min_wick_atr=0.4)
        state = le.get_current_state()
        
        assert "_config" in state
        assert state["_config"]["min_wick_atr"] == 0.4
        assert "atr" in state
        assert "min_wick_threshold" in state


class TestMultipleEngineInstances:
    """Test multiple LiquidityEngine instances for multi-symbol"""

    def test_four_symbols(self):
        """Create 4 engines like orchestrator does"""
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
        engines = {}
        
        for sym in symbols:
            engines[sym] = LiquidityEngine(
                lookback_period=50,
                cluster_tolerance=0.003,
                min_wick_atr=0.3,
                allow_equal_hl=True,
                require_close_beyond=False,
            )
        
        assert len(engines) == 4
        for sym, eng in engines.items():
            assert eng.min_wick_atr == 0.3, f"{sym} failed"
