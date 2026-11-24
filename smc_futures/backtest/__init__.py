"""SMC Backtester — Legacy + Realistic Framework + Autopsy + New Alphas + Real Data"""

# Legacy (kept for backward compat)
from .backtester import SMCBacktester, BacktestMetrics, BacktestTrade

# ── Realistic Backtest Framework (Chantier 8) ──
from .market_model import MarketModel, FillResult, SymbolProfile, get_symbol_profile
from .engine import (
    RealisticBacktestEngine, RealisticBacktestConfig,
    BTCandle, BacktestSignal, RealisticTrade, EngineState,
)
from .walk_forward import WalkForwardValidator, WalkForwardConfig, WalkForwardResult
from .monte_carlo import MonteCarloSimulator, MonteCarloConfig, MonteCarloResult
from .metrics import MetricsCalculator, EdgeValidator, EdgeVerdict, DetailedMetrics
from .pipeline import BacktestPipeline, PipelineConfig, PipelineResult

# ── Edge Autopsy (Chantier 9) ──
from .autopsy import (
    PnLDecomposer, TradeDecomposition, CostAttribution,
    FalseEdgeDetector, FalseEdgeScore,
    DecisionMatrix, DecisionVerdict,
    AutopsyResult, run_autopsy,
)
from .segmentation import SegmentationEngine, SegmentStats, SegmentationResult
from .survival_filter import SurvivalFilterEngine, FilterResult, SurvivalResult

# ── New Alpha Generation (Chantier 10) ──
from .alpha_contextual_reversal import (
    make_contextual_reversal_fn, ContextualReversalConfig,
)
from .alpha_momentum_breakout import (
    make_momentum_breakout_fn, MomentumBreakoutConfig,
)
from .alpha_liquidity_smc import (
    make_liquidity_smc_fn, LiquiditySMCConfig,
)
from .alpha_combiner import (
    make_combined_signal_fn, CombinerConfig,
    make_reversal_only_fn, make_breakout_only_fn, make_smc_only_fn,
)

# ── Real Data Pipeline (Chantier 11) ──
from .real_data_loader import (
    RealDataLoader, DataValidator, LoadResult, ValidationResult,
    KrakenFetcher, SQLiteCacheLoader, SQLiteCacheWriter,
)
from .microstructure_model import (
    EnhancedMarketModel, MicrostructureConfig, EntryContext,
    classify_alpha_b_entry,
)

# ── Structural Edge Alphas (Chantier 12) ──
from .alpha_trend_following import (
    make_trend_following_fn, TrendFollowingConfig,
)
from .alpha_volatility_expansion import (
    make_volatility_expansion_fn, VolatilityExpansionConfig,
)
from .alpha_extreme_events import (
    make_extreme_events_fn, ExtremeEventsConfig,
)

# ── HTF Repositionnement (Chantier 13) ──
from .alpha_htf_trend import (
    make_htf_trend_fn, HTFTrendConfig,
)
from .alpha_long_only import (
    make_long_only_fn, LongOnlyConfig,
)
from .alpha_funding_arb import (
    make_funding_arb_fn, FundingConfig,
)

# ── Structural Arbitrage Engine (Chantier 14) ──
from .arb_engine import (
    ArbBar, ArbTrade, ArbConfig, ArbState, ArbMetrics, compute_arb_metrics,
)
from .arb_funding import (
    FundingArbConfig, simulate_funding_rates, build_arb_bars_funding, run_funding_arb,
)
from .arb_basis import (
    BasisArbConfig, simulate_spot_prices, build_arb_bars_basis, run_basis_arb,
)
from .arb_cross_exchange import (
    CrossExConfig, simulate_exchange_b_prices, build_arb_bars_cross_ex, run_cross_exchange_arb,
)
from .arb_pairs import (
    PairsConfig, build_arb_bars_pairs, run_pairs_trading,
)

__all__ = [
    # Legacy
    "SMCBacktester", "BacktestMetrics", "BacktestTrade",
    # Market Model
    "MarketModel", "FillResult", "SymbolProfile", "get_symbol_profile",
    # Engine
    "RealisticBacktestEngine", "RealisticBacktestConfig",
    "BTCandle", "BacktestSignal", "RealisticTrade", "EngineState",
    # Walk-Forward
    "WalkForwardValidator", "WalkForwardConfig", "WalkForwardResult",
    # Monte Carlo
    "MonteCarloSimulator", "MonteCarloConfig", "MonteCarloResult",
    # Metrics & Verdict
    "MetricsCalculator", "EdgeValidator", "EdgeVerdict", "DetailedMetrics",
    # Pipeline
    "BacktestPipeline", "PipelineConfig", "PipelineResult",
    # Autopsy (Chantier 9)
    "PnLDecomposer", "TradeDecomposition", "CostAttribution",
    "FalseEdgeDetector", "FalseEdgeScore",
    "DecisionMatrix", "DecisionVerdict",
    "AutopsyResult", "run_autopsy",
    "SegmentationEngine", "SegmentStats", "SegmentationResult",
    "SurvivalFilterEngine", "FilterResult", "SurvivalResult",
    # New Alphas (Chantier 10)
    "make_contextual_reversal_fn", "ContextualReversalConfig",
    "make_momentum_breakout_fn", "MomentumBreakoutConfig",
    "make_liquidity_smc_fn", "LiquiditySMCConfig",
    "make_combined_signal_fn", "CombinerConfig",
    "make_reversal_only_fn", "make_breakout_only_fn", "make_smc_only_fn",
    # Real Data Pipeline (Chantier 11)
    "RealDataLoader", "DataValidator", "LoadResult", "ValidationResult",
    "KrakenFetcher", "SQLiteCacheLoader", "SQLiteCacheWriter",
    "EnhancedMarketModel", "MicrostructureConfig", "EntryContext",
    "classify_alpha_b_entry",
    # Structural Edge Alphas (Chantier 12)
    "make_trend_following_fn", "TrendFollowingConfig",
    "make_volatility_expansion_fn", "VolatilityExpansionConfig",
    "make_extreme_events_fn", "ExtremeEventsConfig",
    # HTF Repositionnement (Chantier 13)
    "make_htf_trend_fn", "HTFTrendConfig",
    "make_long_only_fn", "LongOnlyConfig",
    "make_funding_arb_fn", "FundingConfig",
    # Structural Arbitrage (Chantier 14)
    "ArbBar", "ArbTrade", "ArbConfig", "ArbState", "ArbMetrics", "compute_arb_metrics",
    "FundingArbConfig", "simulate_funding_rates", "build_arb_bars_funding", "run_funding_arb",
    "BasisArbConfig", "simulate_spot_prices", "build_arb_bars_basis", "run_basis_arb",
    "CrossExConfig", "simulate_exchange_b_prices", "build_arb_bars_cross_ex", "run_cross_exchange_arb",
    "PairsConfig", "build_arb_bars_pairs", "run_pairs_trading",
]
