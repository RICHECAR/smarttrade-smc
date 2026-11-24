import numpy as np
import logging
from typing import Dict, Any, List
from scipy import stats
from sklearn.linear_model import HuberRegressor
from smc_futures.observability.metrics import (
    VOLATILITY_SIGMA, STALE_REJECTION_RATE, CORRELATION_PEARSON,
    CORRELATION_SPEARMAN, REGRESSION_SLOPE, REGRESSION_R2,
    LATENCY_BURST_GAUGE, LATENCY_P95_GAUGE
)
from smc_futures.engine.analysis.volatility import calculate_log_return_sigma

logger = logging.getLogger(__name__)

class LatencyVolatilityAnalyzer:
    """
    Analyzes the relationship between market volatility and execution latency.
    """
    def __init__(self, symbol: str, window_size: int = 60):
        self.symbol = symbol
        self.window_size = window_size
        self.prices: List[float] = []
        self.latencies: List[float] = []
        self.stale_events: List[int] = [] # 1 if stale, 0 if not
        
        # Buffer for regression/correlation
        self.sigma_series: List[float] = []
        self.latency_p95_series: List[float] = []

    def update(self, price: float, latency_ms: float, is_stale: bool):
        """Update metrics with new data point."""
        self.prices.append(price)
        self.latencies.append(latency_ms)
        self.stale_events.append(1 if is_stale else 0)
        
        if len(self.prices) > self.window_size:
            self.prices.pop(0)
            self.latencies.pop(0)
            self.stale_events.pop(0)
            
        # Calculate Volatility Sigma
        sigma = calculate_log_return_sigma(self.prices, window=min(len(self.prices)-1, 20))
        if sigma is not None:
            VOLATILITY_SIGMA.labels(symbol=self.symbol).set(sigma)
            self.sigma_series.append(sigma)
            
        # Record Latency P95
        p95 = np.percentile(self.latencies, 95) if self.latencies else 0
        LATENCY_P95_GAUGE.labels(symbol=self.symbol).set(p95)
        self.latency_p95_series.append(p95)
        
        # Keep series buffers sane
        if len(self.sigma_series) > self.window_size:
            self.sigma_series.pop(0)
            self.latency_p95_series.pop(0)
            
        # Stale Rejection Rate
        stale_rate = np.mean(self.stale_events) if self.stale_events else 0
        STALE_REJECTION_RATE.labels(symbol=self.symbol).set(stale_rate)
        
        # Periodic Correlation Check
        if len(self.sigma_series) >= 20:
             self._run_diagnostics()

    def _run_diagnostics(self):
        """Run correlation and regression analysis."""
        try:
            # Sync series and filter out any NaNs
            valid_x = []
            valid_y = []
            for sx, sy in zip(self.sigma_series, self.latency_p95_series):
                if np.isfinite(sx) and np.isfinite(sy):
                    valid_x.append(sx)
                    valid_y.append(sy)
            
            if len(valid_x) < 10:
                return

            x = np.array(valid_x).reshape(-1, 1)
            y = np.array(valid_y)
            
            # Check for zero variance (ConstantInputWarning)
            if np.std(valid_x) < 1e-9 or np.std(valid_y) < 1e-9:
                return

            # Pearson / Spearman
            pearson, _ = stats.pearsonr(valid_x, valid_y)
            spearman, _ = stats.spearmanr(valid_x, valid_y)
            
            CORRELATION_PEARSON.labels(symbol=self.symbol).set(pearson)
            CORRELATION_SPEARMAN.labels(symbol=self.symbol).set(spearman)
            
            # Huber Regression (Robust to outliers)
            model = HuberRegressor()
            model.fit(x, y)
            
            slope = model.coef_[0]
            r2 = model.score(x, y)
            
            REGRESSION_SLOPE.labels(symbol=self.symbol).set(slope)
            REGRESSION_R2.labels(symbol=self.symbol).set(r2)
            
            # Detect Latency Burst (High slope + High R2)
            if slope > 5000 and r2 > 0.6: # placeholder thresholds
                LATENCY_BURST_GAUGE.labels(symbol=self.symbol).set(1)
                logger.warning(f"[ANALYSIS] {self.symbol} PHYSICAL LIMIT SUSPECTED (Slope={slope:.2f}, R2={r2:.2f})")
            else:
                LATENCY_BURST_GAUGE.labels(symbol=self.symbol).set(0)
                
        except Exception as e:
            logger.error(f"[ANALYSIS] Diagnostics failed: {e}")

    def get_summary(self) -> Dict[str, Any]:
        """Return diagnostic summary."""
        return {
            "symbol": self.symbol,
            "stale_rate": np.mean(self.stale_events) if self.stale_events else 0,
            "latency_p95": np.percentile(self.latencies, 95) if self.latencies else 0,
            "sigma": self.sigma_series[-1] if self.sigma_series else 0,
            "sample_count": len(self.prices)
        }
