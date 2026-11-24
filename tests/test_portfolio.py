"""
Tests unitaires — Portfolio Allocation Engine
══════════════════════════════════════════════
Tests for features, signals, allocator, risk, metrics, backtest.
Uses synthetic data: no yfinance dependency.
"""

import numpy as np
import pandas as pd
import pytest

# ── Synthetic price data fixture ─────────────────────────────────

def _make_prices(n_days=500, n_assets=10, seed=42) -> pd.DataFrame:
    """Generate synthetic price data with different behaviours."""
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    tickers = [f"ASSET_{i}" for i in range(n_assets)] + ["SPY"]

    data = {}
    for i, t in enumerate(tickers):
        drift = 0.0003 * (i - n_assets // 2)  # Some positive, some negative
        vol = 0.01 + 0.005 * i
        returns = drift + vol * rng.randn(n_days)
        prices_arr = 100.0 * np.exp(np.cumsum(returns))
        data[t] = prices_arr

    return pd.DataFrame(data, index=dates)


@pytest.fixture
def prices():
    return _make_prices()


# ══════════════════════════════════════════════════════════════════
# FEATURES
# ══════════════════════════════════════════════════════════════════

class TestFeatures:
    def test_compute_momentum(self, prices):
        from portfolio.features import compute_momentum
        mom = compute_momentum(prices, windows=(21, 63))
        assert "mom_21" in mom
        assert "mom_63" in mom
        assert mom["mom_21"].shape == prices.shape
        # First 21 rows should be NaN for mom_21
        assert mom["mom_21"].iloc[:21].isna().all().all()
        # After warmup, values should exist
        assert not mom["mom_21"].iloc[25:].isna().all().all()

    def test_compute_volatility(self, prices):
        from portfolio.features import compute_volatility
        vol = compute_volatility(prices, window=63)
        assert vol.shape == prices.shape
        # Vol should be positive after warmup
        valid = vol.iloc[70:].dropna(how="all")
        assert (valid > 0).all().all()

    def test_compute_drawdown(self, prices):
        from portfolio.features import compute_drawdown
        dd = compute_drawdown(prices, window=252)
        assert dd.shape == prices.shape
        # Drawdown should be <= 0 everywhere
        assert (dd.dropna() <= 0).all().all()

    def test_compute_trend_strength(self, prices):
        from portfolio.features import compute_trend_strength
        # Use small window for speed
        r2 = compute_trend_strength(prices[["SPY", "ASSET_0"]], window=21)
        assert r2.shape[0] == prices.shape[0]
        # R² should be in [0, 1]
        valid = r2.dropna()
        assert (valid >= -0.01).all().all()  # Tiny float tolerance
        assert (valid <= 1.01).all().all()

    def test_compute_relative_strength(self, prices):
        from portfolio.features import compute_relative_strength
        rs = compute_relative_strength(prices, benchmark="SPY", window=63)
        assert rs.shape == prices.shape
        # SPY vs itself should be ~1.0
        spy_rs = rs["SPY"].dropna()
        np.testing.assert_allclose(spy_rs.values, 1.0, atol=1e-10)

    def test_compute_all_features(self, prices):
        from portfolio.features import compute_all_features, FeatureConfig
        cfg = FeatureConfig(momentum_windows=(21, 63), trend_window=21)
        feats = compute_all_features(prices[["SPY", "ASSET_0", "ASSET_1"]], cfg)
        assert "mom_21" in feats
        assert "volatility" in feats
        assert "drawdown" in feats
        assert "trend_r2" in feats
        assert "rel_strength" in feats


# ══════════════════════════════════════════════════════════════════
# SIGNALS
# ══════════════════════════════════════════════════════════════════

class TestSignals:
    def test_composite_score_range(self, prices):
        from portfolio.features import compute_all_features, FeatureConfig
        from portfolio.signals import compute_composite_score, SignalConfig

        cfg = FeatureConfig(momentum_windows=(21, 63, 126), trend_window=21)
        feats = compute_all_features(prices, cfg)
        composite = compute_composite_score(feats, SignalConfig())

        valid = composite.dropna(how="all")
        assert len(valid) > 0
        # Scores should be in [0, 1] (from cross-sectional ranking)
        vals = valid.values.flatten()
        vals = vals[~np.isnan(vals)]
        assert vals.min() >= -0.01
        assert vals.max() <= 1.01

    def test_select_top_n(self, prices):
        from portfolio.features import compute_all_features, FeatureConfig
        from portfolio.signals import compute_composite_score, select_top_n

        cfg = FeatureConfig(momentum_windows=(21, 63, 126), trend_window=21)
        feats = compute_all_features(prices, cfg)
        composite = compute_composite_score(feats)
        mask = select_top_n(composite, n=3)

        # Each row should have at most 3 True values
        row_sums = mask.sum(axis=1).dropna()
        valid_rows = row_sums[row_sums > 0]
        assert (valid_rows <= 4).all()  # Allow ties


# ══════════════════════════════════════════════════════════════════
# ALLOCATOR
# ══════════════════════════════════════════════════════════════════

class TestAllocator:
    def test_equal_weight(self):
        from portfolio.allocator import allocate_equal, AllocatorConfig
        cfg = AllocatorConfig(top_n=5, max_weight=0.25)
        w = allocate_equal(["A", "B", "C", "D", "E"], cfg)
        assert len(w) == 5
        assert abs(sum(w.values()) - 1.0) < 1e-10
        assert all(v <= 0.25 + 1e-10 for v in w.values())

    def test_score_weighted(self):
        from portfolio.allocator import allocate_score_weighted, AllocatorConfig
        cfg = AllocatorConfig(top_n=3, max_weight=0.50)
        scores = pd.Series({"A": 0.9, "B": 0.5, "C": 0.1})
        w = allocate_score_weighted(["A", "B", "C"], scores, cfg)
        assert len(w) == 3
        assert abs(sum(w.values()) - 1.0) < 0.05  # Close to 1 after capping
        assert w["A"] >= w["B"] >= w["C"]

    def test_max_weight_cap(self):
        from portfolio.allocator import compute_allocation, AllocatorConfig, WeightMethod
        cfg = AllocatorConfig(top_n=3, max_weight=0.40, method=WeightMethod.SCORE_WEIGHTED)
        scores = pd.Series({"A": 0.99, "B": 0.01, "C": 0.005})
        w = compute_allocation(scores, config=cfg)
        assert all(v <= 0.40 + 0.01 for v in w.values())

    def test_inverse_vol(self):
        from portfolio.allocator import allocate_inverse_vol, AllocatorConfig
        cfg = AllocatorConfig(top_n=3, max_weight=0.50)
        vol = pd.Series({"A": 0.10, "B": 0.20, "C": 0.40})
        w = allocate_inverse_vol(["A", "B", "C"], vol, cfg)
        assert w["A"] > w["B"] > w["C"]  # Lower vol → higher weight


# ══════════════════════════════════════════════════════════════════
# RISK
# ══════════════════════════════════════════════════════════════════

class TestRisk:
    def test_vol_scaling(self):
        from portfolio.risk import vol_scaling_factor, RiskConfig
        cfg = RiskConfig(vol_target=0.10, vol_lookback=63)
        # Simulate high-vol returns
        rng = np.random.RandomState(0)
        rets = pd.Series(rng.randn(100) * 0.02)  # ~32% annual vol
        factor = vol_scaling_factor(rets, cfg)
        assert 0.0 < factor < 1.0  # Should scale down

    def test_drawdown_stop(self):
        from portfolio.risk import check_drawdown_stop, RiskConfig
        cfg = RiskConfig(max_drawdown=0.10)
        # Equity that drops 15%
        eq = pd.Series([1.0, 0.95, 0.90, 0.85])
        assert check_drawdown_stop(eq, cfg)  # Should trigger

    def test_drawdown_recovery(self):
        from portfolio.risk import check_drawdown_recovery, RiskConfig
        cfg = RiskConfig(drawdown_recovery=0.05)
        eq = pd.Series([1.0, 0.85, 0.90, 0.96])
        assert check_drawdown_recovery(eq, cfg)  # -4% is above -5% threshold


# ══════════════════════════════════════════════════════════════════
# METRICS
# ══════════════════════════════════════════════════════════════════

class TestMetrics:
    def test_cagr(self):
        from portfolio.metrics import compute_cagr
        dates = pd.bdate_range("2020-01-01", periods=504)
        # Perfect 10% CAGR over ~2 years
        eq = pd.Series(np.linspace(1.0, 1.21, 504), index=dates)
        cagr = compute_cagr(eq)
        assert 0.08 < cagr < 0.12  # Approximately 10%

    def test_sharpe(self):
        from portfolio.metrics import compute_sharpe
        rng = np.random.RandomState(1)
        rets = pd.Series(0.0005 + 0.01 * rng.randn(500))
        sharpe = compute_sharpe(rets, rf_annual=0.0)
        assert sharpe > 0  # Positive drift → positive Sharpe

    def test_max_drawdown(self):
        from portfolio.metrics import compute_max_drawdown
        eq = pd.Series([1.0, 1.1, 1.05, 0.90, 0.95, 1.0])
        dd = compute_max_drawdown(eq)
        # Peak was 1.1, trough was 0.9 → DD = 18.18%
        assert abs(dd - 0.1818) < 0.01


# ══════════════════════════════════════════════════════════════════
# BACKTEST (integration test with synthetic data)
# ══════════════════════════════════════════════════════════════════

class TestBacktest:
    def test_backtest_runs(self):
        """Full integration test: backtest completes with synthetic data."""
        from portfolio.backtest import run_backtest, BacktestConfig
        from portfolio.features import FeatureConfig
        from portfolio.allocator import AllocatorConfig, WeightMethod

        prices = _make_prices(n_days=500, n_assets=8, seed=123)
        config = BacktestConfig(
            feature_config=FeatureConfig(
                momentum_windows=(21, 63, 126),
                trend_window=21,
                volatility_window=42,
            ),
            allocator_config=AllocatorConfig(
                top_n=3,
                max_weight=0.40,
                method=WeightMethod.SCORE_WEIGHTED,
            ),
            rebal_freq="M",
            warmup=150,  # Reduced for synthetic data
            cost_bps=5.0,
        )

        result = run_backtest(prices, config)

        # Basic sanity
        assert len(result.equity_curve) > 100
        assert result.equity_curve.iloc[0] > 0
        assert result.equity_curve.iloc[-1] > 0
        assert result.strategy_metrics.cagr is not None
        assert result.strategy_metrics.sharpe is not None
        assert result.strategy_metrics.max_drawdown >= 0

    def test_backtest_benchmark_exists(self):
        """Benchmark equity curve should exist and be reasonable."""
        from portfolio.backtest import run_backtest, BacktestConfig
        from portfolio.features import FeatureConfig

        prices = _make_prices(n_days=400, n_assets=5, seed=99)
        config = BacktestConfig(
            feature_config=FeatureConfig(
                momentum_windows=(21, 63),
                trend_window=21,
            ),
            warmup=130,
            rebal_freq="M",
        )
        result = run_backtest(prices, config)
        assert len(result.benchmark_equity) > 100
        assert result.benchmark_metrics.cagr != 0.0
