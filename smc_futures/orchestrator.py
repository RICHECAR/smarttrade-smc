"""
SMC Futures Strategy Orchestrator (V3 — 3-Tier MTF)
Coordinates: HTF(4H) Context → MTF(15m) Setup → LTF(5m) Execution
Integrated with FastAPI for Frontend communication.
"""

from typing import Dict, Optional, List, Any, Union, Literal
from decimal import Decimal, ROUND_HALF_UP
import asyncio
import json
import logging
import os
import signal
import sys
import httpx
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from core.env_loader import is_test_mode, safe_load_dotenv
from smc_futures.schemas.orders import DryRunOrderRequest, OrderRequest, ForceOrderRequest
import pandas as pd

# Windows-specific asyncio event loop fix
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Load environment variables (no-op in isolated test mode)
safe_load_dotenv()

BITGET_BASE = "https://api.bitget.com"
DB_PATH = "data/candles.db"

# Module-level timestamp: updated every successful ticker/candle fetch
_last_data_fetch_ts: Optional[datetime] = None

from smc_futures.smc_config import SMC_CONFIG
from smc_futures.settings import settings
from smc_futures.security.redaction import setup_secure_logging

# --- Secure Logging (Phase 4 Hardening) ---
setup_secure_logging(level=logging.INFO)

# --- Context Injection (DI — lazy to prevent premature construction on import) ---
from smc_futures.app_context import build_app_context

_app_ctx = None


def get_app_context():
    """Return the shared AppContext, constructing it lazily on first access."""
    global _app_ctx
    if _app_ctx is None:
        _app_ctx = build_app_context()
        if "ws_manager" in globals() and hasattr(_app_ctx, "set_ws_manager"):
            _app_ctx.set_ws_manager(ws_manager)
    return _app_ctx


def set_app_context(new_ctx) -> None:
    """Explicitly inject or override the shared AppContext (API wiring / tests)."""
    global _app_ctx
    _app_ctx = new_ctx


class _LazyAppContextProxy:
    """Transparent lazy proxy for module-level `ctx` avoiding import-time side effects."""

    def __getattr__(self, name: str):
        return getattr(get_app_context(), name)

    def __setattr__(self, name: str, value):
        setattr(get_app_context(), name, value)

    def __delattr__(self, name: str):
        delattr(get_app_context(), name)

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        if _app_ctx is None:
            return "<LazyAppContextProxy (uninitialized)>"
        return repr(_app_ctx)


ctx = _LazyAppContextProxy()

# Multi-broker configuration
DEFAULT_BROKER = settings.default_broker if hasattr(settings, 'default_broker') else 'bitget'

from smc_futures.engine.strategy.signal_builder import SignalBuilder
from smc_futures.engine.strategy.structure import StructureEngine, Candle
from smc_futures.engine.strategy.liquidity import LiquidityEngine
from smc_futures.engine.strategy.orderblocks import OrderBlockEngine
from smc_futures.engine.strategy.fvg import FVGEngine
from smc_futures.engine.strategy.mtf_bias import MTFBiasEngine
from smc_futures.engine.strategy.session_filter import SessionFilter
from smc_futures.engine.strategy.envelope_strategy import EnvelopeStrategy
from smc_futures.engine.strategy.trade_validator import SMCTradeValidator
from smc_futures.engine.strategy.strategy_router import StrategyRouter, StrategyType
from smc_futures.engine.strategy.asia_weekend_strategy import AsiaWeekendStrategy
from smc_futures.engine.strategy.funding_reversion_strategy import FundingReversionStrategy
from smc_futures.engine.strategy.vwap_reversion_strategy import VWAPReversionStrategy, compute_session_vwap
from smc_futures.engine.strategy.liquidity_sweep_scalp_strategy import LiquiditySweepScalpStrategy
from smc_futures.engine.strategy.liquidation_cascade_strategy import LiquidationCascadeStrategy
from smc_futures.engine.strategy.orderbook_imbalance_strategy import OrderBookImbalanceStrategy
from smc_futures.engine.alpha import StrategyScorer, StrategyCapitalAllocator, StrategyWeightTracker, AlphaGovernor
from smc_futures.engine.utils.reason_codes import ReasonCode, ExitAction, DecisionCategory
from smc_futures.engine.risk.futures_engine import TradeValidator, FuturesRiskEngine, SymbolRules
from smc_futures.engine.risk.global_exposure_guardian import GlobalExposureGuardian, ExposurePosition
from smc_futures.engine.risk.capital_stress_simulator import CapitalStressSimulator
from smc_futures.engine.execution import BitgetFuturesExecutor
from smc_futures.data_downloader import fetch_ohlcv, save_csv

# We import decision_metrics only to ensure it's loaded in context,
# but we RECOMMEND using ctx.metrics everywhere instead.
from smc_futures.smc_logger import new_trace
from backend.ws.manager import ws_manager

# ── Analytics DB instrumentation ─────────────────────────────────────────────
try:
    from backend.services.analytics_service import AnalyticsRecorder as _AnalyticsRec
    _analytics_ok = True
except ImportError:
    _AnalyticsRec = None  # type: ignore
    _analytics_ok = False

# ── Phase 6: Wiring (event bus publish helpers) ──────────────────────────────
try:
    from core.phase6_wiring import publish_event as _p6_publish, FLAGS as _P6_FLAGS
    _p6_wiring_ok = True
except ImportError:
    _p6_wiring_ok = False
    _P6_FLAGS = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SMCFuturesOrchestrator:
    """
    SMC Futures Trading Engine — V3 (3-Tier MTF)
    ═══════════════════════════════════════════════
      TIER 1: HTF (4H)  — context / bias    [htf_*]
      TIER 2: MTF (15m) — setup / POI zones [mtf_*]
      TIER 3: LTF (5m)  — execution trigger [ltf_*]
    """
    
    def __init__(
        self,
        symbol: str,
        account_balance: float = 0.0,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        passphrase: Optional[str] = None,
        live_trading: bool = False,
        strategy_mode: str = "smc",
        timeframe: str = "4h",
        neutral_permissive: bool = False,
        context: Any = None # Dependency Injection
    ):
        self.symbol = symbol
        self.account_balance = account_balance
        self.live_trading = live_trading
        self.strategy_mode = strategy_mode
        self.timeframe = timeframe
        self.neutral_permissive = neutral_permissive
        self.ctx = context or ctx  # Use passed context or fall back to global
        
        # ── TIER 1: HTF Engines (4H) ─────────────
        self.htf_structure_engine = StructureEngine(lookback_period=SMC_CONFIG["HTF_LIMIT"])
        
        # ── TIER 2: MTF Engines (15m) ────────────
        self.mtf_structure_engine = StructureEngine(lookback_period=SMC_CONFIG["SETUP_LIMIT"])
        self.mtf_ob_engine = OrderBlockEngine()
        self.mtf_fvg_engine = FVGEngine()
        self.mtf_liquidity_engine = LiquidityEngine(
            lookback_period=SMC_CONFIG.get("SWEEP_LOOKBACK", 50),
            cluster_tolerance=SMC_CONFIG.get("SWEEP_CLUSTER_TOLERANCE", 0.003),
            min_wick_atr=SMC_CONFIG.get("SWEEP_MIN_WICK_ATR", 0.3),
            allow_equal_hl=SMC_CONFIG.get("SWEEP_ALLOW_EQUAL_HL", True),
            require_close_beyond=SMC_CONFIG.get("SWEEP_REQUIRE_CLOSE_BEYOND", False),
            # Heatmap
            heatmap_enabled=SMC_CONFIG.get("LIQUIDITY_HEATMAP_ENABLED", True),
            heatmap_cluster_pct=SMC_CONFIG.get("LIQUIDITY_HEATMAP_CLUSTER_PCT", 0.001),
            heatmap_min_cluster_size=SMC_CONFIG.get("LIQUIDITY_HEATMAP_MIN_CLUSTER_SIZE", 3),
            heatmap_breakout_sl_atr=SMC_CONFIG.get("LIQUIDITY_HEATMAP_BREAKOUT_SL_ATR", 0.5),
            heatmap_max_levels=SMC_CONFIG.get("LIQUIDITY_HEATMAP_MAX_LEVELS", 20),
            weight_equal_hl=SMC_CONFIG.get("LIQUIDITY_WEIGHT_EQUAL_HL", 2.0),
            weight_cluster_density=SMC_CONFIG.get("LIQUIDITY_WEIGHT_CLUSTER", 3.0),
            weight_breakout=SMC_CONFIG.get("LIQUIDITY_WEIGHT_BREAKOUT", 1.5),
            weight_htf_level=SMC_CONFIG.get("LIQUIDITY_WEIGHT_HTF", 2.0),
        )
        
        # ── TIER 3: LTF Engines (5m) ─────────────
        self.ltf_structure_engine = StructureEngine(lookback_period=SMC_CONFIG["ENTRY_LIMIT"])
        self.ltf_liquidity_engine = LiquidityEngine(
            lookback_period=SMC_CONFIG.get("SWEEP_LOOKBACK", 50),
            cluster_tolerance=SMC_CONFIG.get("SWEEP_CLUSTER_TOLERANCE", 0.003),
            min_wick_atr=SMC_CONFIG.get("SWEEP_MIN_WICK_ATR", 0.3),
            allow_equal_hl=SMC_CONFIG.get("SWEEP_ALLOW_EQUAL_HL", True),
            require_close_beyond=SMC_CONFIG.get("SWEEP_REQUIRE_CLOSE_BEYOND", False),
            # Heatmap
            heatmap_enabled=SMC_CONFIG.get("LIQUIDITY_HEATMAP_ENABLED", True),
            heatmap_cluster_pct=SMC_CONFIG.get("LIQUIDITY_HEATMAP_CLUSTER_PCT", 0.001),
            heatmap_min_cluster_size=SMC_CONFIG.get("LIQUIDITY_HEATMAP_MIN_CLUSTER_SIZE", 3),
            heatmap_breakout_sl_atr=SMC_CONFIG.get("LIQUIDITY_HEATMAP_BREAKOUT_SL_ATR", 0.5),
            heatmap_max_levels=SMC_CONFIG.get("LIQUIDITY_HEATMAP_MAX_LEVELS", 20),
            weight_equal_hl=SMC_CONFIG.get("LIQUIDITY_WEIGHT_EQUAL_HL", 2.0),
            weight_cluster_density=SMC_CONFIG.get("LIQUIDITY_WEIGHT_CLUSTER", 3.0),
            weight_breakout=SMC_CONFIG.get("LIQUIDITY_WEIGHT_BREAKOUT", 1.5),
            weight_htf_level=SMC_CONFIG.get("LIQUIDITY_WEIGHT_HTF", 2.0),
        )
        
        # ── Legacy aliases (for backwards compat) ─
        self.structure_engine = self.ltf_structure_engine
        self.liquidity_engine = self.mtf_liquidity_engine
        self.ob_engine = self.mtf_ob_engine
        self.fvg_engine = self.mtf_fvg_engine
        
        # ── MTF Bias Engine (3-tier coordinator) ──
        self.mtf_engine = MTFBiasEngine(
            min_htf_candles=SMC_CONFIG["MIN_HTF_CANDLES"],
            mode=SMC_CONFIG["MODE"],
            allow_countertrend=SMC_CONFIG["ALLOW_COUNTERTREND"],
        )
        
        # ── Signal Builder ────────────────────────
        self.signal_builder = SignalBuilder(
            min_rr_ratio=SMC_CONFIG["MIN_RR"],
            neutral_permissive=self.neutral_permissive,
            allow_countertrend=SMC_CONFIG["ALLOW_COUNTERTREND"],
            countertrend_min_rr=SMC_CONFIG.get("COUNTERTREND_MIN_RR", 2.0),
            # Sweep fallback config
            sweep_fallback_enabled=SMC_CONFIG.get("SWEEP_FALLBACK_ENABLED", True),
            sweep_fallback_min_rr=SMC_CONFIG.get("SWEEP_FALLBACK_MIN_RR", 2.0),
            sweep_fallback_max_fvg_age=SMC_CONFIG.get("SWEEP_FALLBACK_MAX_FVG_AGE", 10),
            sweep_fallback_max_ob_age=SMC_CONFIG.get("SWEEP_FALLBACK_MAX_OB_AGE", 20),
            # Fallback FVG requirements
            fallback_require_fvg=SMC_CONFIG.get("FALLBACK_REQUIRE_FVG", True),
            fallback_allow_no_fvg=SMC_CONFIG.get("FALLBACK_ALLOW_NO_FVG", False),
            force_execute=SMC_CONFIG.get("FORCE_EXECUTE", False),
            # Soft bypass: BOS+OB/FVG without sweep (small account / growth mode)
            allow_no_sweep_with_ob=SMC_CONFIG.get("ALLOW_NO_SWEEP_WITH_OB", False),
            allow_no_sweep_with_fvg=SMC_CONFIG.get("ALLOW_NO_SWEEP_WITH_FVG", False),
            allow_no_sweep_with_zone=SMC_CONFIG.get("ALLOW_NO_SWEEP_WITH_ZONE", False),
            no_sweep_min_rr=SMC_CONFIG.get("NO_SWEEP_MIN_RR", 1.5),
            no_sweep_require_fresh_ob=SMC_CONFIG.get("NO_SWEEP_REQUIRE_FRESH_OB", True),
            no_sweep_block_strong_htf=SMC_CONFIG.get("NO_SWEEP_BLOCK_STRONG_HTF", True),
            # LTF Trigger relaxation
            allow_no_ltf_trigger_with_zone=SMC_CONFIG.get("ALLOW_NO_LTF_TRIGGER_WITH_ZONE", False),
            ltf_trigger_lookback=SMC_CONFIG.get("LTF_TRIGGER_LOOKBACK", 3),
            no_ltf_trigger_min_rr=SMC_CONFIG.get("NO_LTF_TRIGGER_MIN_RR", 2.0),
            # NO_ENTRY_ZONE relaxation
            force_entry_zone=SMC_CONFIG.get("FORCE_ENTRY_ZONE", False),
            entry_zone_padding=SMC_CONFIG.get("ENTRY_ZONE_PADDING_ATR", 0.0),
            entry_zone_max_dist=SMC_CONFIG.get("ENTRY_ZONE_MAX_DISTANCE_ATR", 100.0),
            entry_zone_nonfresh=SMC_CONFIG.get("ENTRY_ZONE_ACCEPT_NONFRESH_FVG", False),
            # Entry zone ATR fallback (when obs=0 & fvgs=0)
            entry_fallback_mode=SMC_CONFIG.get("ENTRY_ZONE_FALLBACK_MODE", False),
            entry_fallback_atr_mult=SMC_CONFIG.get("ENTRY_FALLBACK_ATR_MULT", 0.25),
            entry_fallback_sl_atr_mult=SMC_CONFIG.get("ENTRY_FALLBACK_SL_ATR_MULT", 1.5),
            entry_fallback_rr_min=SMC_CONFIG.get("ENTRY_FALLBACK_RR_MIN", 2.0),
        )
        
        # ── Session Filter ────────────────────────
        self.session_filter = SessionFilter(
            allowed_sessions=SMC_CONFIG["SESSIONS"],
        )
        
        # ── Strategy Router (Multi-Strategy) ──────
        self.strategy_router = StrategyRouter(
            timezone=SMC_CONFIG.get("STRATEGY_TIMEZONE", "Europe/Paris"),
            asia_start=SMC_CONFIG.get("ASIA_SESSION_START", "01:00"),
            asia_end=SMC_CONFIG.get("ASIA_SESSION_END", "09:00"),
            enable_weekend_strategy=SMC_CONFIG.get("ENABLE_WEEKEND_STRATEGY", True),
            enable_asia_strategy=SMC_CONFIG.get("ENABLE_ASIA_STRATEGY", True),
            enable_funding_strategy=SMC_CONFIG.get("ENABLE_FUNDING_STRATEGY", True),
            enable_vwap_strategy=SMC_CONFIG.get("ENABLE_VWAP_STRATEGY", True),
            enable_sweep_scalp_strategy=SMC_CONFIG.get("ENABLE_SWEEP_SCALP_STRATEGY", True),
            enable_liquidation_cascade=SMC_CONFIG.get("ENABLE_LIQ_CASCADE", True),
            enable_orderbook_imbalance=SMC_CONFIG.get("ENABLE_OB_IMBALANCE", True),
            funding_threshold=SMC_CONFIG.get("FUNDING_REV_THRESHOLD", 0.0004),
            liq_spike_usd=SMC_CONFIG.get("LIQ_SPIKE_USD", 5_000_000),
            ob_imbalance_long=SMC_CONFIG.get("OB_IMBALANCE_LONG", 0.65),
            ob_imbalance_short=SMC_CONFIG.get("OB_IMBALANCE_SHORT", 0.35),
        )
        
        # ── Asia/Weekend Strategy ─────────────────
        self.asia_weekend_strategy = AsiaWeekendStrategy(
            min_rr=SMC_CONFIG.get("ASIA_WEEKEND_MIN_RR", 1.2),
            max_rr=SMC_CONFIG.get("ASIA_WEEKEND_MAX_RR", 2.0),
            target_mid_range=SMC_CONFIG.get("ASIA_TARGET_MID_RANGE", True),
            range_lookback=SMC_CONFIG.get("ASIA_RANGE_LOOKBACK", 20),
            min_range_atr=SMC_CONFIG.get("ASIA_MIN_RANGE_ATR", 0.5),
            max_range_atr=SMC_CONFIG.get("ASIA_MAX_RANGE_ATR", 3.0),
            htf_trend_threshold=SMC_CONFIG.get("ASIA_HTF_TREND_THRESHOLD", 0.7),
            risk_factor=SMC_CONFIG.get("ASIA_RISK_FACTOR", 0.75),
        )
        
        # ── Funding Reversion Strategy ────────────
        self.funding_reversion_strategy = FundingReversionStrategy(
            funding_threshold=SMC_CONFIG.get("FUNDING_REV_THRESHOLD", 0.0004),
            rsi_overbought=SMC_CONFIG.get("FUNDING_REV_RSI_OB", 70.0),
            rsi_oversold=SMC_CONFIG.get("FUNDING_REV_RSI_OS", 30.0),
            min_rr=SMC_CONFIG.get("FUNDING_REV_MIN_RR", 1.5),
            sl_atr_mult=SMC_CONFIG.get("FUNDING_REV_SL_ATR", 1.2),
            tp_atr_mult=SMC_CONFIG.get("FUNDING_REV_TP_ATR", 2.0),
            risk_factor=SMC_CONFIG.get("FUNDING_REV_RISK_FACTOR", 0.6),
        )

        # ── VWAP Reversion Strategy ───────────────
        self.vwap_reversion_strategy = VWAPReversionStrategy(
            distance_atr_mult=SMC_CONFIG.get("VWAP_REV_DIST_ATR", 1.5),
            sl_atr_mult=SMC_CONFIG.get("VWAP_REV_SL_ATR", 1.0),
            tp_mode=SMC_CONFIG.get("VWAP_REV_TP_MODE", "vwap"),
            min_rr=SMC_CONFIG.get("VWAP_REV_MIN_RR", 1.2),
            risk_factor=SMC_CONFIG.get("VWAP_REV_RISK_FACTOR", 0.65),
        )

        # ── Liquidity Sweep Scalp Strategy ────────
        self.liquidity_sweep_scalp_strategy = LiquiditySweepScalpStrategy(
            tp_atr_mult=SMC_CONFIG.get("SWEEP_SCALP_TP_ATR", 0.5),
            sl_atr_mult=SMC_CONFIG.get("SWEEP_SCALP_SL_ATR", 0.8),
            min_wick_ratio=SMC_CONFIG.get("SWEEP_SCALP_MIN_WICK", 0.6),
            min_rr=SMC_CONFIG.get("SWEEP_SCALP_MIN_RR", 0.6),
            risk_factor=SMC_CONFIG.get("SWEEP_SCALP_RISK_FACTOR", 0.4),
            cooldown_candles=SMC_CONFIG.get("SWEEP_SCALP_COOLDOWN", 3),
        )

        # ── Trade Validator ────────────────────────
        # SMC_CONFIG["RISK_PER_TRADE_PCT"] is a fraction (e.g. 0.03 = 3%), while
        # SMCTradeValidator & FuturesRiskEngine expect percentage points (e.g. 3.0 = 3%).
        _raw_risk_cfg = float(SMC_CONFIG["RISK_PER_TRADE_PCT"])
        _risk_pct_points = _raw_risk_cfg * 100.0 if 0.0 < _raw_risk_cfg < 0.25 else _raw_risk_cfg
        self.smc_validator = SMCTradeValidator(
            min_rr=SMC_CONFIG["MIN_RR"],
            risk_per_trade_pct=_risk_pct_points,
            max_leverage=SMC_CONFIG["DEFAULT_LEVERAGE"],
            account_balance=account_balance,
        )
        
        # Mean Reversion Envelope Module
        self.envelope_engine = EnvelopeStrategy(base_window=5, envelopes=[0.07, 0.10, 0.12])
        
        # Risk Management (legacy)
        self.risk_engine = FuturesRiskEngine(
            account_balance=account_balance,
            risk_per_trade_percent=_risk_pct_points,
            max_leverage=float(SMC_CONFIG["DEFAULT_LEVERAGE"]),
        )
        self.validator = TradeValidator(self.risk_engine)

        # ── Global Exposure Guardian (Phase 5 guardrail)
        self.exposure_guardian = GlobalExposureGuardian()

        # ── Capital Stress Simulator (Phase 6 guardrail)
        self.capital_stress_simulator = CapitalStressSimulator()

        # ── Regime Filter ─────────────────────────
        from smc_futures.risk.regime_filter import RegimeFilter
        self.regime_filter = RegimeFilter(SMC_CONFIG)

        # ── Multi-Alpha Engine ────────────────────
        from smc_futures.observability.performance_governor import governor as _governor
        self._alpha_enabled = SMC_CONFIG.get("ENABLE_ALPHA_SCORING", True)
        self._weight_tracker = StrategyWeightTracker(
            governor_ref=_governor,
            min_weight=SMC_CONFIG.get("ALPHA_WEIGHT_MIN", 0.3),
            max_weight=SMC_CONFIG.get("ALPHA_WEIGHT_MAX", 2.0),
            min_trades_for_weight=SMC_CONFIG.get("ALPHA_MIN_TRADES_WEIGHT", 5),
        )
        self._strategy_scorer = StrategyScorer(
            w_confidence=SMC_CONFIG.get("ALPHA_W_CONFIDENCE", 0.40),
            w_rr=SMC_CONFIG.get("ALPHA_W_RR", 0.25),
            w_regime=SMC_CONFIG.get("ALPHA_W_REGIME", 0.20),
            w_weight=SMC_CONFIG.get("ALPHA_W_PERF", 0.15),
            rr_cap=SMC_CONFIG.get("ALPHA_RR_CAP", 4.0),
            conflict_policy=SMC_CONFIG.get("ALPHA_CONFLICT_POLICY", "best_score"),
            min_score=SMC_CONFIG.get("ALPHA_MIN_SCORE", 0.25),
        )
        _alloc_map = {
            "DefaultSMC": SMC_CONFIG.get("ALPHA_ALLOC_DEFAULT_SMC", 0.25),
            "AsiaWeekend": SMC_CONFIG.get("ALPHA_ALLOC_ASIA_WEEKEND", 0.10),
            "FundingReversion": SMC_CONFIG.get("ALPHA_ALLOC_FUNDING_REVERSION", 0.10),
            "VWAPReversion": SMC_CONFIG.get("ALPHA_ALLOC_VWAP_REVERSION", 0.15),
            "SweepScalp": SMC_CONFIG.get("ALPHA_ALLOC_SWEEP_SCALP", 0.10),
            "LiqCascade": SMC_CONFIG.get("ALPHA_ALLOC_LIQ_CASCADE", 0.15),
            "OBImbalance": SMC_CONFIG.get("ALPHA_ALLOC_OB_IMBALANCE", 0.15),
        }
        self._strategy_capital_allocator = StrategyCapitalAllocator(
            weight_tracker=self._weight_tracker,
            base_allocations=_alloc_map,
            min_pf_for_full=SMC_CONFIG.get("ALPHA_MIN_PF_FOR_FULL", 1.0),
            penalty_below_pf=SMC_CONFIG.get("ALPHA_PF_PENALTY", 0.5),
        )

        # ── Alpha Governance (Phase 9) ────────────
        self._alpha_gov_enabled = SMC_CONFIG.get("ENABLE_ALPHA_GOVERNANCE", True)
        self._alpha_governor = AlphaGovernor(
            weight_tracker=self._weight_tracker,
            capital_allocator=self._strategy_capital_allocator,
            starvation_window=SMC_CONFIG.get("ALPHA_GOV_STARVATION_WINDOW", 50),
            starvation_accept_ratio=SMC_CONFIG.get("ALPHA_GOV_STARVATION_RATIO", 0.05),
            concentration_max_pct=SMC_CONFIG.get("ALPHA_GOV_CONCENTRATION_MAX", 0.40),
            concentration_window_s=SMC_CONFIG.get("ALPHA_GOV_CONCENTRATION_WINDOW", 3600.0),
            dead_strategy_cycles=SMC_CONFIG.get("ALPHA_GOV_DEAD_CYCLES", 200),
            max_weight_velocity=SMC_CONFIG.get("ALPHA_GOV_MAX_WEIGHT_VELOCITY", 0.3),
            dampening_alpha=SMC_CONFIG.get("ALPHA_GOV_DAMPENING_ALPHA", 0.7),
            combined_penalty_floor=SMC_CONFIG.get("ALPHA_GOV_PENALTY_FLOOR", 0.15),
        )
        # ATR history for avg_atr computation (shared across strategies)
        self._atr_mean_window: int = SMC_CONFIG.get("ATR_MEAN_WINDOW", 100)
        self._atr_history: List[float] = []
        # Cached regime from previous cycle for routing context
        self._cached_regime: str = "RANGE"
        
        # ════════════════════════════════════════════════════════════════════
        # Execution Setup - Multi-broker support
        # ════════════════════════════════════════════════════════════════════
        self.executor = None  # Primary executor (legacy compat)
        self.multi_executor = None  # Multi-broker executor for parallel execution
        self.executors: Dict[str, Any] = {}  # All available executors
        self.active_broker = DEFAULT_BROKER
        
        # Check if multi-broker mirror mode is enabled
        # Priority: MULTI_BROKER_MODE > legacy flags
        # AUTO-ENABLE: if KRAKEN_ENABLED=true + KRAKEN_EXECUTION_ENABLED=true, activate
        kraken_env_enabled = os.getenv("KRAKEN_ENABLED", "0").lower() in ("1", "true", "yes")
        kraken_exec_enabled = os.getenv("KRAKEN_EXECUTION_ENABLED", "false").lower() in ("1", "true", "yes")
        multi_broker_mode = SMC_CONFIG.get("MULTI_BROKER_MODE", "disabled")
        self.multi_broker_mode = multi_broker_mode  # Store for execution path selection
        self.multi_broker_enabled = (
            multi_broker_mode != "disabled" or
            os.getenv("MULTI_BROKER_MIRROR", "0").lower() in ("1", "true", "yes") or
            os.getenv("DUAL_EXECUTION", "0").lower() in ("1", "true", "yes") or
            os.getenv("MULTI_BROKER_EXECUTION", "0").lower() in ("1", "true", "yes") or
            (kraken_env_enabled and kraken_exec_enabled)  # Auto-enable only if Kraken execution is configured
        )
        
        if kraken_env_enabled and self.multi_broker_enabled:
            logger.info(f"[ORCH] {symbol} | Multi-broker AUTO-ENABLED via KRAKEN_ENABLED=true")
        
        # ── Step 1: Setup PRIMARY executor (Bitget only if BITGET_ENABLED=true) ──
        bitget_env_enabled = os.getenv("BITGET_ENABLED", "true").lower() in ("1", "true", "yes")
        if bitget_env_enabled and api_key and secret_key and passphrase:
            # Explicit credentials provided - use Bitget
            self.executor = BitgetFuturesExecutor(api_key, secret_key, passphrase)
            self.executors["bitget"] = self.executor
            self.active_broker = "bitget"
        elif live_trading:
            # Try to get executor from Context based on default broker
            self.executor = self.ctx.get_active_executor(DEFAULT_BROKER)
            
            if not self.executor and bitget_env_enabled:
                # Fallback: try Bitget from env
                env_key = os.getenv("BITGET_API_KEY")
                env_secret = os.getenv("BITGET_API_SECRET")
                env_pass = os.getenv("BITGET_PASSPHRASE")
                if env_key and env_secret and env_pass:
                    self.executor = BitgetFuturesExecutor(env_key, env_secret, env_pass)
                    self.active_broker = "bitget"
            elif self.executor:
                self.active_broker = DEFAULT_BROKER
            
            # Add primary executor to dict under its real broker key (never mislabel Kraken as bitget)
            if self.executor:
                _primary_key = getattr(self.executor, "broker_name", None) or self.active_broker or "bitget"
                if _primary_key == "bitget" and not bitget_env_enabled:
                    pass
                else:
                    self.executors[_primary_key] = self.executor
        
        # ── Step 1b: Always register Kraken executor if KRAKEN_ENABLED + KRAKEN_EXECUTION_ENABLED ──
        if kraken_env_enabled and kraken_exec_enabled:
            _k_exec = self.ctx.executors.get("kraken_futures")
            if _k_exec:
                self.executors["kraken"] = _k_exec
                if not self.executor or not bitget_env_enabled:
                    self.executor = _k_exec
                    self.active_broker = "kraken"
                logger.info(f"[ORCH] {symbol} | Kraken executor registered (primary={self.active_broker})")
        
        # ── Step 2: Setup MULTI-BROKER executor (runs only if enabled and >1 broker active) ──
        if self.multi_broker_enabled and self.executor and bitget_env_enabled:
            kraken_enabled = os.getenv("KRAKEN_ENABLED", "0").lower() in ("1", "true", "yes")
            
            try:
                from backend.services.multi_order_executor import MultiOrderExecutor
                self.multi_executor = MultiOrderExecutor()
                
                # Register Bitget
                self.multi_executor.set_bitget_executor(self.executor)
                
                # Register Kraken if enabled
                if kraken_enabled:
                    # First try from context (already initialized in api_smc_engine)
                    kraken_executor = self.ctx.executors.get("kraken_futures")
                    
                    if not kraken_executor:
                        # Fallback: try to create Kraken executor
                        logger.warning(f"[ORCH] {symbol} | Kraken not in ctx.executors, attempting creation...")
                        try:
                            from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor
                            kraken_key = os.getenv("KRAKEN_API_KEY")
                            kraken_secret = os.getenv("KRAKEN_API_SECRET")
                            if kraken_key and kraken_secret:
                                kraken_executor = KrakenFuturesExecutor(kraken_key, kraken_secret)
                                self.ctx.executors["kraken_futures"] = kraken_executor
                                logger.info(f"[ORCH] {symbol} | Kraken executor created from env")
                        except Exception as e:
                            logger.warning(f"[ORCH] {symbol} | Failed to create Kraken executor: {e}")
                    
                    if kraken_executor:
                        if kraken_exec_enabled:
                            self.executors["kraken"] = kraken_executor
                            self.multi_executor.set_kraken_executor(kraken_executor)
                            logger.info(f"[ORCH] {symbol} | Kraken executor registered for mirror trading")
                        else:
                            logger.info(f"[ORCH] {symbol} | Kraken active in READ-ONLY mode (KRAKEN_EXECUTION_ENABLED=false)")
                    else:
                        logger.warning(f"[ORCH] {symbol} | Kraken enabled but no executor available")
                
            except ImportError as e:
                logger.warning(f"[ORCH] {symbol} | MultiOrderExecutor not available: {e}")
                self.multi_executor = None
        
        # Log executor status and set active_broker appropriately
        if self.executor:
            active_brokers = list(self.executors.keys())
            if self.multi_broker_enabled and self.multi_executor and len(active_brokers) > 1:
                # Multi-broker is fully ready
                self.active_broker = "multi"
                logger.info(
                    f"[ORCH] {symbol} | Executor ready | broker=multi | brokers={active_brokers} | multi_executor=OK"
                )
            elif self.multi_broker_enabled and len(active_brokers) == 1:
                # Multi-broker enabled but only one broker available (degraded)
                logger.warning(
                    f"[ORCH] {symbol} | Executor ready | broker={self.active_broker} | multi_broker_enabled=True | "
                    f"DEGRADED: only {active_brokers} available (check Kraken credentials/connection)"
                )
            else:
                logger.info(f"[ORCH] {symbol} | Executor ready | broker={self.active_broker}")
        else:
            logger.warning(f"[ORCH] {symbol} | No executor configured - paper trading only")
        
        # State tracking
        self.last_signal: Optional[Dict] = None
        self.active_trade: Optional[Dict] = None
        self.signal_history: List[Dict] = []
        self.trade_log: List[Dict] = []
        self.last_trade_time = 0
        self.executed_signals = set()
        # FM-1: HTF stale tracking - Initialize to now to prevent false STALE on cold start
        # Will be updated to actual candle time after first successful HTF fetch
        self._last_htf_update_ts: int = int(time.time() * 1000)
        self._last_htf_candle_time: int = 0  # Track actual HTF candle timestamp
        self._htf_fetch_count: int = 0  # Track number of successful HTF fetches
        self._ltf_warmup_done: bool = False  # FM-9: cold start guard

    # ──────────────────────────────────────────────────
    #  TIER 1 — Process HTF Context (4H)
    # ──────────────────────────────────────────────────

    def process_htf_context(self, symbol: str, candles_data: List[Dict]):
        """
        Process Higher Timeframe (4H) candles to update Bias Context.
        Handles bulk history initialization or incremental updates.
        """
        if not candles_data:
            logger.warning(f"[HTF] {symbol} | NO_CANDLES_RECEIVED - HTF refresh skipped")
            return

        current_len = len(self.htf_structure_engine.candle_history)
        
        if current_len == 0:
            logger.info(f"🧊 [HTF] Cold starting {symbol} structure with {len(candles_data)} candles")
            for c_data in candles_data:
                candle = self._parse_candle(c_data)
                self.htf_structure_engine.add_candle(candle)
        else:
            last_candle = self._parse_candle(candles_data[-1])
            last_stored = self.htf_structure_engine.candle_history[-1]
            if last_candle.timestamp > last_stored.timestamp:
                self.htf_structure_engine.add_candle(last_candle)
        
        htf_state = self.htf_structure_engine.get_current_state()
        htf_state['candle_count'] = len(self.htf_structure_engine.candle_history)
        
        self.mtf_engine.update_htf_context(htf_state)
        
        # FM-1: Update HTF tracking timestamps
        now_ms = int(time.time() * 1000)
        self._last_htf_update_ts = now_ms
        self._htf_fetch_count += 1
        
        # Track last HTF candle time for diagnostics
        if self.htf_structure_engine.candle_history:
            last_htf_candle = self.htf_structure_engine.candle_history[-1]
            self._last_htf_candle_time = last_htf_candle.timestamp
            candle_age_sec = (now_ms - self._last_htf_candle_time) / 1000
            
            logger.info(
                f"[HTF_REFRESH] {symbol} | OK"
                f" | candles={len(self.htf_structure_engine.candle_history)}"
                f" | last_candle_age_sec={candle_age_sec:.0f}"
                f" | fetch_count={self._htf_fetch_count}"
                f" | bias={self.mtf_engine.current_bias.value}"
            )

    # ──────────────────────────────────────────────────
    #  TIER 2 — Process MTF Setup (15m)
    # ──────────────────────────────────────────────────

    def process_mtf_setup(self, symbol: str, candles_data: List[Dict]):
        """
        Process 15m candles to detect POI zones (OB + FVG) aligned with HTF bias.
        """
        if not candles_data:
            return

        current_len = len(self.mtf_structure_engine.candle_history)
        
        if current_len == 0:
            logger.info(f"🧊 [MTF] Cold starting {symbol} 15m structure with {len(candles_data)} candles")
            for c_data in candles_data:
                candle = self._parse_candle(c_data)
                self.mtf_structure_engine.add_candle(candle)
                self.mtf_ob_engine.add_candle(candle)
                self.mtf_fvg_engine.add_candle(candle)
                self.mtf_liquidity_engine.add_candle(candle)
        else:
            last_candle = self._parse_candle(candles_data[-1])
            last_stored = self.mtf_structure_engine.candle_history[-1]
            if last_candle.timestamp > last_stored.timestamp:
                self.mtf_structure_engine.add_candle(last_candle)
                self.mtf_ob_engine.add_candle(last_candle)
                self.mtf_fvg_engine.add_candle(last_candle)
                self.mtf_liquidity_engine.add_candle(last_candle)
        
        mtf_structure = self.mtf_structure_engine.get_current_state()
        mtf_ob = self.mtf_ob_engine.get_current_state()
        mtf_fvg = self.mtf_fvg_engine.get_current_state()
        
        self.mtf_engine.update_mtf_setup(mtf_structure, mtf_ob, mtf_fvg)

    # ──────────────────────────────────────────────────
    #  TIER 3 — Process LTF Execution (5m)
    # ──────────────────────────────────────────────────

    async def process_ltf_execution(self, symbol: str, candles_data: List[Dict]) -> Optional[Dict]:
        """
        Process 5m candles: feed LTF engines, check trigger, build signal.
        Returns signal dict if a valid trade is found, None otherwise.

        **No silent returns**: every exit is logged with ExitAction + ReasonCode.
        """
        # [REFACTOR] Use injected context metrics
        metrics = self.ctx.metrics

        trace = new_trace(symbol)

        # ── Guard: no data ────────────────────────
        if not candles_data:
            trace.add("DATA", ExitAction.SKIP.value, str(ReasonCode.INSUFFICIENT_DATA))
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None

        if self.strategy_mode == "envelope":
            trace.add("MODE", ExitAction.SKIP.value, "ENVELOPE_MODE")
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None

        # ── Feed 5m candles ──────────────────────
        current_len = len(self.ltf_structure_engine.candle_history)

        if current_len == 0:
            logger.info(f"🧊 [LTF] Cold starting {symbol} 5m structure with {len(candles_data)} candles")
            for c_data in candles_data:
                candle = self._parse_candle(c_data)
                self.ltf_structure_engine.add_candle(candle)
                self.ltf_liquidity_engine.add_candle(candle)
        else:
            last_candle = self._parse_candle(candles_data[-1])
            last_stored = self.ltf_structure_engine.candle_history[-1]
            if last_candle.timestamp > last_stored.timestamp:
                self.ltf_structure_engine.add_candle(last_candle)
                self.ltf_liquidity_engine.add_candle(last_candle)

        if not self.ltf_structure_engine.candle_history:
            trace.add("DATA", ExitAction.SKIP.value, str(ReasonCode.INSUFFICIENT_DATA), detail="empty_after_feed")
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None

        # FM-9: cold start — skip first batch
        if current_len == 0:
            self._ltf_warmup_done = False
            trace.add("WARMUP", ExitAction.SKIP.value, "COLD_START_BATCH")
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None
        if not self._ltf_warmup_done:
            self._ltf_warmup_done = True
            logger.info(f"🔥 [LTF] {symbol} warmup complete, signals enabled next candle")
            trace.add("WARMUP", ExitAction.SKIP.value, "WARMUP_DONE")
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None

        candle = self.ltf_structure_engine.candle_history[-1]
        
        # Phase 4.5 Latency & Volatility Analysis
        analyzer = self.ctx.get_analyzer(symbol)
        analyzer.update(price=candle.close, latency_ms=0, is_stale=False)
        
        now = int(time.time() * 1000)

        # FM-1: Enhanced stale HTF bias guard with detailed diagnostics
        if self._last_htf_update_ts > 0:
            htf_age_ms = now - self._last_htf_update_ts
            htf_age_sec = htf_age_ms / 1000
            max_htf_age_ms = SMC_CONFIG["HTF_REFRESH_INTERVAL"] * SMC_CONFIG["LOOP_SLEEP_SECONDS"] * 1000 * 1.5
            max_htf_age_sec = max_htf_age_ms / 1000
            
            if htf_age_ms > max_htf_age_ms:
                # Calculate next expected refresh
                loop_sleep = SMC_CONFIG["LOOP_SLEEP_SECONDS"]
                htf_interval = SMC_CONFIG["HTF_REFRESH_INTERVAL"]
                next_refresh_sec = htf_interval * loop_sleep - (htf_age_sec % (htf_interval * loop_sleep))
                
                # Log with full diagnostics
                logger.warning(
                    f"[STALE] {symbol} | HTF_DATA_STALE"
                    f" | htf_age_sec={htf_age_sec:.0f}"
                    f" | threshold_sec={max_htf_age_sec:.0f}"
                    f" | bias={self.mtf_engine.current_bias.value}"
                    f" | htf_fetch_count={self._htf_fetch_count}"
                    f" | last_htf_candle_age_sec={(now - self._last_htf_candle_time)/1000:.0f if self._last_htf_candle_time else 'N/A'}"
                    f" | next_refresh_in_sec={next_refresh_sec:.0f}"
                )
                trace.add("HTF", ExitAction.BLOCK.value, "STALE_BIAS", 
                         htf_age_sec=int(htf_age_sec),
                         threshold_sec=int(max_htf_age_sec),
                         htf_fetch_count=self._htf_fetch_count)
                metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                return None

        # ── Strategy Router (Multi-Strategy) ─────────────
        router_decision = self.strategy_router.route_with_context(
            timestamp_ms=candle.timestamp, symbol=symbol,
            regime=self._cached_regime,
        )
        trace.add("ROUTER", ExitAction.PASS.value, router_decision.reason,
                  strategy=router_decision.strategy.value,
                  session=router_decision.session.value)
        
        # If Asia/Weekend strategy selected, use different signal flow
        if router_decision.strategy == StrategyType.ASIA_WEEKEND:
            return await self._process_asia_weekend_signal(
                symbol=symbol,
                candle=candle,
                trace=trace,
                metrics=metrics,
                now=now,
                is_weekend=router_decision.is_weekend,
            )

        # Funding Reversion strategy
        if router_decision.strategy == StrategyType.FUNDING_REVERSION:
            return await self._process_funding_reversion_signal(
                symbol=symbol,
                candle=candle,
                trace=trace,
                metrics=metrics,
                now=now,
            )

        # VWAP Reversion strategy
        if router_decision.strategy == StrategyType.VWAP_REVERSION:
            return await self._process_vwap_reversion_signal(
                symbol=symbol,
                candle=candle,
                trace=trace,
                metrics=metrics,
                now=now,
            )

        # Liquidity Sweep Scalp strategy
        if router_decision.strategy == StrategyType.LIQUIDITY_SWEEP_SCALP:
            return await self._process_sweep_scalp_signal(
                symbol=symbol,
                candle=candle,
                trace=trace,
                metrics=metrics,
                now=now,
            )

        # Liquidation Cascade strategy
        if router_decision.strategy == StrategyType.LIQUIDATION_CASCADE:
            return await self._process_liquidation_cascade_signal(
                symbol=symbol,
                candle=candle,
                trace=trace,
                metrics=metrics,
                now=now,
            )

        # Order Book Imbalance strategy
        if router_decision.strategy == StrategyType.ORDERBOOK_IMBALANCE:
            return await self._process_orderbook_imbalance_signal(
                symbol=symbol,
                candle=candle,
                trace=trace,
                metrics=metrics,
                now=now,
            )

        # ── Overlay strategies (evaluate all, pick best, shadow rest) ───
        if router_decision.overlay_strategies:
            # Phase 1: probe all overlays in score-only mode
            candidates = []
            for overlay in router_decision.overlay_strategies:
                probe = await self._dispatch_overlay(
                    overlay, symbol, candle, trace, metrics, now,
                    score_only=True,
                )
                if probe is not None and probe.get("_candidate"):
                    probe["_overlay_type"] = overlay
                    candidates.append(probe)

            if candidates:
                # Phase 2: sort by alpha score descending, pick best
                candidates.sort(key=lambda c: c.get("score", 0), reverse=True)
                best = candidates[0]

                # Phase 3: execute only the best overlay in full mode
                overlay_result = await self._dispatch_overlay(
                    best["_overlay_type"], symbol, candle, trace, metrics, now,
                )

                # Phase 4: record shadows for the runners-up
                if overlay_result is not None and self._alpha_gov_enabled:
                    for c in candidates[1:]:
                        self._alpha_governor.record_shadow(
                            c["strategy"], c.get("score", 0)
                        )
                        logger.info(
                            f"[SHADOW] {symbol} | {c['strategy']}"
                            f" score={c.get('score', 0):.3f} (outscored by {best['strategy']})"
                        )

                if overlay_result is not None:
                    return overlay_result

        # ── Default SMC Strategy: Session filter gate ───
        if not self.session_filter.check_and_log(symbol, candle.timestamp):
            trace.add("SESSION", ExitAction.REJECT.value, str(ReasonCode.SESSION_OUTSIDE))
            metrics.safe_record(symbol, ExitAction.REJECT.value, trace)
            return None
        trace.add("SESSION", ExitAction.PASS.value, self.session_filter.get_session_name(candle.timestamp))

        # ── Position dedup / cooldown ────────────
        if self.active_trade:
            logger.debug(f"[GUARD] {symbol} | BLOCKED | reason=ACTIVE_TRADE")
            trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.POSITION_ALREADY_OPEN))
            metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
            return None
        if (now - self.last_trade_time) < 60_000:
            trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.COOLDOWN_ACTIVE),
                       cooldown_remaining_ms=60_000 - (now - self.last_trade_time))
            metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
            return None
        trace.add("GUARD", ExitAction.PASS.value, "NO_POSITION_NO_COOLDOWN")

        # ── Update LTF data in MTF engine ────────
        ltf_state = self.ltf_structure_engine.get_current_state()
        self.mtf_engine.update_ltf_data(ltf_state)

        # ── Check 5m trigger ─────────────────────
        ltf_trigger = self.mtf_engine.check_ltf_trigger(symbol)
        trace.add("LTF_TRIGGER", ExitAction.PASS.value if ltf_trigger.get("confirmed") else ExitAction.SKIP.value,
                   str(ltf_trigger.get("reason", "CONFIRMED")),
                   trigger_type=ltf_trigger.get("trigger_type"),
                   direction=ltf_trigger.get("direction"))

        # ── Get all states for signal builder ────
        mtf_state = self.mtf_engine.get_current_state()
        structure_state = ltf_state
        # Feed HTF swing levels into liquidity heatmap
        htf_struct = self.htf_structure_engine.get_current_state()
        self.ltf_liquidity_engine.update_htf_levels(htf_struct)
        liquidity_state = self.ltf_liquidity_engine.get_current_state()
        ob_state = self.mtf_ob_engine.get_current_state()
        fvg_state = self.mtf_fvg_engine.get_current_state()

        # ── Build signal ─────────────────────────
        result = self.signal_builder.build_signal(
            symbol=symbol,
            structure_state=structure_state,
            liquidity_state=liquidity_state,
            ob_state=ob_state,
            fvg_state=fvg_state,
            mtf_state=mtf_state,
            current_price=candle.close,
            timestamp=candle.timestamp,
            ltf_trigger=ltf_trigger,
        )

        if result.status != "CREATED" or not result.signal:
            action = ExitAction.REJECT.value if result.status == "REJECTED" else ExitAction.SKIP.value
            rejection_reasons = "; ".join(result.reasons) if result.reasons else result.status
            # Build gate diagnostics from trace steps
            gates_passed = [s["stage"] for s in trace.steps if s.get("action") == ExitAction.PASS.value]
            gates_failed = [s["stage"] for s in trace.steps if s.get("action") in (ExitAction.REJECT.value, ExitAction.BLOCK.value)]
            logger.warning(
                f"[SIGNAL_REJECTED] {symbol} | status={result.status} | reasons={rejection_reasons}"
                f" | gates_passed={gates_passed} | gates_failed={gates_failed}"
            )
            # ── DB: record signal rejection ──
            if _analytics_ok and _AnalyticsRec:
                try:
                    first_reason = result.reasons[0] if result.reasons else "UNKNOWN"
                    _AnalyticsRec.record_signal_decision(
                        symbol=symbol,
                        decision="REJECT",
                        reason=first_reason,
                        side=mtf_state.get("bias"),
                        regime=mtf_state.get("regime"),
                        rr_estimated=None,
                    )
                except Exception:
                    pass
            trace.add("SIGNAL", action, rejection_reasons)
            metrics.safe_record(symbol, action, trace)
            return None
        
        logger.info(
            f"[SIGNAL_ACCEPTED] {symbol} | dir={result.signal.direction.value}"
            f" | rr={result.signal.risk_reward_ratio:.2f}"
            f" | entry={result.signal.entry_price} | sl={result.signal.stop_loss} | tp={result.signal.take_profit}"
        )
        # ── DB: record signal accept ──
        if _analytics_ok and _AnalyticsRec:
            try:
                _AnalyticsRec.record_signal_decision(
                    symbol=symbol,
                    decision="ACCEPT",
                    side=result.signal.direction.value,
                    regime=mtf_state.get("regime"),
                    rr_estimated=result.signal.risk_reward_ratio,
                    edge_score=getattr(result.signal, "confidence_score", None),
                )
            except Exception:
                pass
        trace.add("SIGNAL", ExitAction.PASS.value, "CREATED",
                   direction=result.signal.direction.value, rr=result.signal.risk_reward_ratio)

        # Publish SIGNAL_GENERATED event (unconditional)
        if _p6_wiring_ok:
            try:
                from core.events.event_types import TradeEvent, EventType
                _p6_publish(TradeEvent(
                    EventType.SIGNAL_GENERATED,
                    symbol=symbol,
                    broker="bitget",
                    side=result.signal.direction.value,
                    price=float(result.signal.entry_price),
                    extra={"rr": result.signal.risk_reward_ratio,
                           "setup": result.signal.setup_type,
                           "confidence": result.signal.confidence_score},
                ))
            except Exception:
                pass

        # ── Validate risk ────────────────────────
        signal = result.signal

        # ── Track ATR history for regime detection + extreme vol filter ──
        current_atr = float(liquidity_state.get("atr", 0))
        if current_atr > 0:
            self._atr_history.append(current_atr)
            if len(self._atr_history) > self._atr_mean_window:
                self._atr_history = self._atr_history[-self._atr_mean_window:]
        avg_atr = sum(self._atr_history) / len(self._atr_history) if self._atr_history else current_atr

        signal_dict = {
            "direction": signal.direction.value,
            "entry": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "setup": signal.setup_type,
            "trigger": signal.trigger_type,
            "confidence": signal.confidence_score,
            "rr_ratio": signal.risk_reward_ratio,
            "invalidation": signal.invalidation_level,
            "bias": signal.mtf_bias,
            "timestamp": signal.timestamp,
            "t_signal": time.time(),  # Capture actual decision time for latency tracking
            "atr": current_atr,  # For slippage ATR detection
            "atr_info": {"current_atr": current_atr, "avg_atr": avg_atr},
        }

        # ── Regime Filter (strategy gating) ──────
        regime = self.regime_filter.detect(
            candles=list(self.ltf_structure_engine.candle_history),
            current_atr=current_atr,
            avg_atr=avg_atr,
        )
        signal_dict["regime"] = regime.value
        self._cached_regime = regime.value
        allowed, conf_mult, regime_reason = self.regime_filter.gate_strategy(regime, "DefaultSMCStrategy")
        if not allowed:
            logger.warning(f"[REGIME_GATE] {symbol} | BLOCKED | {regime_reason}")
            trace.add("REGIME", ExitAction.BLOCK.value, regime_reason)
            metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
            return None
        if conf_mult < 1.0:
            original_conf = signal_dict["confidence"]
            signal_dict["confidence"] = original_conf * conf_mult
            logger.info(f"[REGIME] {symbol} | confidence {original_conf:.2f} → {signal_dict['confidence']:.2f} ({regime_reason})")

        # ── Directional Liquidity Trap Filter ─────
        trap_atr_dist = SMC_CONFIG.get("LIQUIDITY_TRAP_MIN_ATR_DIST", 0.3)
        if current_atr > 0 and trap_atr_dist > 0:
            heatmap = liquidity_state.get("heatmap", {})
            heatmap_levels = heatmap.get("levels", [])
            entry_price = float(signal.entry_price)
            direction = signal.direction.value.lower()
            for lv in heatmap_levels:
                lv_price = float(lv.get("price", 0))
                lv_score = float(lv.get("score", 0))
                if lv_score >= 4.0:
                    dist = abs(entry_price - lv_price) / current_atr
                    if dist < trap_atr_dist:
                        # Directional check: only block if cluster is IN the trade path
                        # Long: cluster above entry is ahead (TP side), cluster below is protective (SL side)
                        # Short: cluster below entry is ahead, cluster above is protective
                        cluster_is_ahead = (
                            (direction == "long" and lv_price > entry_price)
                            or (direction == "short" and lv_price < entry_price)
                        )
                        cluster_at_entry = abs(lv_price - entry_price) / entry_price < 0.001  # ~same level
                        if cluster_is_ahead or cluster_at_entry:
                            logger.warning(
                                f"[LIQUIDITY_TRAP] {symbol} | entry={entry_price:.4f} too close to"
                                f" cluster={lv_price:.4f} (dist={dist:.2f} ATR < {trap_atr_dist})"
                                f" | score={lv_score:.1f} | dir={direction} — BLOCKED"
                            )
                            trace.add("LIQUIDITY_TRAP", ExitAction.BLOCK.value,
                                      f"entry too close to liquidity cluster (dist={dist:.2f} ATR, dir={direction})")
                            metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                            return None
                        else:
                            logger.info(
                                f"[LIQUIDITY_TRAP] {symbol} | cluster={lv_price:.4f} behind entry"
                                f" ({direction}) — protective, allowing"
                            )

        sig_id = hash(f"{symbol}_{signal.direction.value}_{signal.entry_price}_{signal.timestamp}")
        if sig_id in self.executed_signals:
            trace.add("DEDUP", ExitAction.BLOCK.value, str(ReasonCode.DUPLICATE_SIGNAL))
            metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
            return None

        # Always broadcast accepted SMC V3 signal to UI (even when Start/auto-execute is OFF)
        try:
            if getattr(self.ctx, "ws_manager", None) is not None:
                await self.ctx.ws_manager.broadcast({
                    "type": "signal",
                    "symbol": symbol,
                    "signal": signal_dict,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
        except Exception:
            pass

        # ── Independent MT5 Execution Routing (decoupled from Kraken $46 micro-balance validator) ──
        # Always broadcast accepted SMC V3 signal to UI and route to MT5 independently so MT5
        # applies its own balance, lot cap (MT5_MAX_LOT_SIZE), and max-open guard.
        if SMC_CONFIG.get("AUTO_EXECUTE_ENABLED", False):
            try:
                _mbe = getattr(self.ctx, "_multi_broker_engine", None)
                if _mbe is None:
                    from backend.routes.multi_broker import _engine as _global_mbe
                    _mbe = _global_mbe
                if _mbe is not None and hasattr(_mbe, "dispatch_canonical_smc_signal"):
                    from smc_futures.engine.multi_broker_engine import BrokerType, Signal as MBESignal, SignalType
                    _dir = str(signal.direction.value).strip().lower()
                    _sig_type = SignalType.LONG if _dir in ("long", "buy") else (SignalType.SHORT if _dir in ("short", "sell") else SignalType.NO_SIGNAL)
                    if _sig_type != SignalType.NO_SIGNAL:
                        _canon_id = f"SMCV3_{symbol}_{_dir}_{signal.entry_price}_{signal.stop_loss}_{signal.timestamp}"
                        _mbe_sig = MBESignal(
                            symbol=symbol,
                            signal_type=_sig_type,
                            entry_price=float(signal.entry_price),
                            stop_loss=float(signal.stop_loss) if signal.stop_loss else None,
                            take_profit=float(signal.take_profit) if signal.take_profit else None,
                            leverage=int(SMC_CONFIG.get("DEFAULT_LEVERAGE", 3)),
                            reason=f"SMC_V3:{signal.setup_type}({signal_dict.get('regime', '')})",
                            confidence=float(signal_dict.get("confidence", 0.0)),
                        )
                        await _mbe.dispatch_canonical_smc_signal(
                            _mbe_sig,
                            signal_id=_canon_id,
                            brokers=[BrokerType.MT5],
                        )
            except Exception as mt5_err:
                logger.error(f"❌ [SMC_ROUTER->MT5] Independent MT5 routing error for {symbol}: {mt5_err}")

        is_neutral = mtf_state.get("bias") == "neutral"
        validation = self.smc_validator.validate(
            symbol=symbol,
            signal_dict=signal_dict,
            current_price=candle.close,
            leverage=SMC_CONFIG["DEFAULT_LEVERAGE"],
            neutral_override=is_neutral,
        )

        if not validation["valid"]:
            trace.add("RISK", ExitAction.REJECT.value, str(validation.get("reason", "UNKNOWN")))
            metrics.safe_record(symbol, ExitAction.REJECT.value, trace)
            return None
        trace.add("RISK", ExitAction.PASS.value, f"rr={validation['rr']:.2f}", qty=validation["quantity"])

        # ── Global Exposure Guard (Phase 5)
        try:
            projected_notional = float(signal_dict.get("entry", 0)) * float(validation.get("quantity", 0))
            open_positions = []
            for orch in self.ctx.orchestrators.values():
                active = getattr(orch, "active_trade", None)
                if not active:
                    continue
                qty = float(active.get("qty", 0) or 0)
                price = float(active.get("filled_price") or active.get("entry") or 0)
                if qty <= 0 or price <= 0:
                    continue
                open_positions.append(
                    ExposurePosition(
                        symbol=orch.symbol,
                        side=active.get("direction", "long"),
                        notional_usd=abs(qty * price),
                        broker=getattr(orch, "broker", None),
                    )
                )

            # Include pending orders from OrderManager (if available) so that in-flight exposure is accounted for.
            pending_positions = []
            order_mgr = self.ctx.order_manager
            if hasattr(order_mgr, "pending_orders") and order_mgr.pending_orders:
                for p in order_mgr.pending_orders:
                    if isinstance(p, dict):
                        pending_positions.append(
                            ExposurePosition(
                                symbol=p.get("symbol"),
                                side=p.get("side"),
                                notional_usd=float(p.get("notional_usd", 0)),
                                broker=p.get("broker"),
                                group=p.get("group"),
                            )
                        )
                    elif isinstance(p, ExposurePosition):
                        pending_positions.append(p)

            guard = self.exposure_guardian.check_signal(
                symbol=symbol,
                side=signal_dict.get("direction", "long"),
                notional_usd=projected_notional,
                open_positions=open_positions,
                pending_positions=pending_positions,
            )
            if not guard.get("allowed", False):
                logger.warning(
                    f"[EXPOSURE_GUARD] {symbol} BLOCKED | reason={guard.get('reason')} "
                    f"| current={guard.get('current_total_usd', 0):.2f} "
                    f"| projected={guard.get('projected_total_usd', 0):.2f} "
                    f"| limit={guard.get('limit_usd', 0):.2f}"
                )
                trace.add("EXPOSURE", ExitAction.BLOCK.value, guard.get("reason"), **guard.get("details", {}))
                metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                try:
                    from smc_futures.observability.metrics import EXPOSURE_GUARD_BLOCK_TOTAL
                    EXPOSURE_GUARD_BLOCK_TOTAL.labels(reason=guard.get("reason", "UNKNOWN")).inc()
                except Exception:
                    pass
                return None

            # Handle throttle advisories (soft limits)
            if guard.get("reason", "").startswith("THROTTLE"):
                logger.warning(
                    f"[EXPOSURE_GUARD] {symbol} THROTTLE | reason={guard.get('reason')} "
                    f"| current={guard.get('current_total_usd', 0):.2f} "
                    f"| projected={guard.get('projected_total_usd', 0):.2f} "
                    f"| limit={guard.get('limit_usd', 0):.2f}"
                )
                trace.add("EXPOSURE", ExitAction.THROTTLE.value, guard.get("reason"), **guard.get("details", {}))
                metrics.safe_record(symbol, ExitAction.THROTTLE.value, trace)
                try:
                    from smc_futures.observability.metrics import EXPOSURE_GUARD_THROTTLE_TOTAL
                    EXPOSURE_GUARD_THROTTLE_TOTAL.labels(reason=guard.get("reason", "UNKNOWN")).inc()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[EXPOSURE_GUARD] error evaluating exposure guard: {e}")

        # ── Capital Stress Simulator (Phase 6)
        try:
            stress = self.capital_stress_simulator.check_signal(
                symbol=symbol,
                side=signal_dict.get("direction", "long"),
                notional_usd=projected_notional,
                account_balance=self.account_balance,
                open_positions=open_positions,
                pending_positions=pending_positions,
                leverage=SMC_CONFIG.get("DEFAULT_LEVERAGE", 1),
            )

            try:
                from smc_futures.observability.metrics import (
                    STRESS_SIM_PROJECTED_LOSS_USD,
                    STRESS_SIM_PROJECTED_DD_PCT,
                    STRESS_SIM_PROJECTED_MARGIN_PCT,
                    STRESS_SIM_WORST_SCENARIO,
                )
                STRESS_SIM_PROJECTED_LOSS_USD.labels(symbol=symbol).set(stress.get("projected_loss_usd", 0.0))
                STRESS_SIM_PROJECTED_DD_PCT.labels(symbol=symbol).set(stress.get("projected_dd_pct", 0.0))
                STRESS_SIM_PROJECTED_MARGIN_PCT.labels(symbol=symbol).set(stress.get("projected_margin_pct", 0.0))
                if stress.get("worst_scenario"):
                    STRESS_SIM_WORST_SCENARIO.labels(symbol=symbol, scenario=stress.get("worst_scenario")).set(1)
            except Exception:
                pass

            if not stress.get("allowed", False):
                logger.warning(
                    f"[STRESS_SIM] {symbol} BLOCKED | reason={stress.get('reason')} "
                    f"| projected_loss={stress.get('projected_loss_usd', 0):.2f} "
                    f"| projected_dd={stress.get('projected_dd_pct', 0):.2f}% "
                    f"| worst={stress.get('worst_scenario')}"
                )
                trace.add("STRESS", ExitAction.BLOCK.value, stress.get("reason"), **stress.get("details", {}))
                try:
                    from smc_futures.observability.metrics import STRESS_SIM_BLOCK_TOTAL
                    STRESS_SIM_BLOCK_TOTAL.labels(reason=stress.get("reason", "UNKNOWN")).inc()
                except Exception:
                    pass
                return None

            if stress.get("reason", "").startswith("THROTTLE"):
                logger.warning(
                    f"[STRESS_SIM] {symbol} THROTTLE | reason={stress.get('reason')} "
                    f"| projected_loss={stress.get('projected_loss_usd', 0):.2f} "
                    f"| projected_dd={stress.get('projected_dd_pct', 0):.2f}% "
                    f"| worst={stress.get('worst_scenario')}"
                )
                trace.add("STRESS", ExitAction.THROTTLE.value, stress.get("reason"), **stress.get("details", {}))
                try:
                    from smc_futures.observability.metrics import STRESS_SIM_THROTTLE_TOTAL
                    STRESS_SIM_THROTTLE_TOTAL.labels(reason=stress.get("reason", "UNKNOWN")).inc()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[STRESS_SIM] error evaluating stress sim: {e}")

        # ── Execution / Signal Handoff ─────────────
        # [REFACTOR] Use injected context OrderManager
        order_mgr = self.ctx.order_manager
        
        should_auto_execute = (
            (SMC_CONFIG["AUTO_EXECUTE_ENABLED"] or SMC_CONFIG["SHADOW_MODE"])
            and (self.live_trading or SMC_CONFIG["SHADOW_MODE"])
        )
        
        # Log execution decision details
        logger.info(
            f"[EXEC_DECISION] {symbol} | should_auto_execute={should_auto_execute}"
            f" | AUTO_EXECUTE_ENABLED={SMC_CONFIG['AUTO_EXECUTE_ENABLED']}"
            f" | SHADOW_MODE={SMC_CONFIG['SHADOW_MODE']}"
            f" | live_trading={self.live_trading}"
            f" | TRADING_MODE={SMC_CONFIG['TRADING_MODE']}"
        )
        
        if should_auto_execute:
            logger.info(f"[AUTO_EXEC] HOOK TRIGGERED | {symbol} | signal_id={sig_id}")

            # ════════════════════════════════════════════════════════════════
            # MULTI-BROKER vs SINGLE-BROKER execution path
            # ════════════════════════════════════════════════════════════════
            multi_broker_mode = SMC_CONFIG.get("MULTI_BROKER_MODE", "disabled")
            use_multi_broker = (
                self.multi_broker_enabled and 
                len(self.executors) > 1 and
                hasattr(order_mgr, 'execute_multi_broker') and
                (
                    multi_broker_mode != "disabled" or
                    SMC_CONFIG.get("MIRROR_TRADING_ENABLED", False)
                    # KRAKEN_EXECUTION_ENABLED alone no longer forces multi-broker
                    # when MULTI_BROKER_MODE=disabled → single-broker Kraken path instead
                )
            )
            
            # Diagnostic: log execution path decision with all gate values
            logger.warning(
                f"[EXEC_PATH] {symbol}"
                f" | MULTI_BROKER_MODE={multi_broker_mode}"
                f" | multi_broker_enabled={self.multi_broker_enabled}"
                f" | executors={list(self.executors.keys())}"
                f" | MIRROR={SMC_CONFIG.get('MIRROR_TRADING_ENABLED', False)}"
                f" | KRAKEN_EXEC={SMC_CONFIG.get('KRAKEN_EXECUTION_ENABLED', False)}"
                f" | use_multi_broker={use_multi_broker}"
                f" | balance={self.account_balance}"
                f" | qty={validation['quantity']}"
            )
            
            if not use_multi_broker and self.multi_broker_enabled and len(self.executors) > 1:
                logger.warning(
                    f"[EXEC_PATH] MULTI-BROKER BLOCKED: multi_broker_enabled=True but MULTI_BROKER_MODE={multi_broker_mode}"
                    f" & MIRROR={SMC_CONFIG.get('MIRROR_TRADING_ENABLED', False)}"
                    f" & KRAKEN_EXEC={SMC_CONFIG.get('KRAKEN_EXECUTION_ENABLED', False)}"
                    f" → Set MULTI_BROKER_MODE=mirror|fallback|split in .env"
                )
            elif not use_multi_broker and len(self.executors) <= 1:
                logger.info(
                    f"[EXEC_PATH] SINGLE-BROKER: only {list(self.executors.keys())} available"
                    f" | KRAKEN_ENABLED={SMC_CONFIG.get('KRAKEN_ENABLED', False)}"
                )
            
            if use_multi_broker:
                # Multi-broker execution (mode-aware)
                logger.info(
                    f"[MULTI_EXEC] Using multi-broker path"
                    f" | mode={multi_broker_mode}"
                    f" | brokers={list(self.executors.keys())}"
                )
                multi_result = await order_mgr.execute_multi_broker(
                    symbol=symbol,
                    signal_dict=signal_dict,
                    signal_id=str(sig_id),
                    executors=self.executors,
                    active_trade=self.active_trade,
                    account_balance=self.account_balance,
                    quantity=validation["quantity"],
                    leverage=SMC_CONFIG["DEFAULT_LEVERAGE"],
                    mode=multi_broker_mode,
                )

                # Convert multi-broker result to standard format
                if multi_result.success:
                    # Get first successful result for state tracking
                    first_success = next(
                        (r for r in multi_result.broker_results.values() if r.success),
                        None
                    )
                    exec_result = {
                        "success": True,
                        "entry_order_id": first_success.order_id if first_success else None,
                        "price_executed": first_success.fill_price if first_success else None,
                        "quantity": first_success.quantity if first_success else validation["quantity"],
                        "multi_broker": True,
                        "broker_results": multi_result.broker_results,
                        "signal_id": multi_result.signal_id,
                    }
                else:
                    exec_result = {
                        "success": False,
                        "reason": "MULTI_BROKER_FAILED",
                        "details": {
                            "broker_results": {
                                b: {"success": r.success, "error": r.error}
                                for b, r in multi_result.broker_results.items()
                            }
                        },
                    }
            else:
                # Single-broker execution
                # Pick Kraken executor if KRAKEN_EXECUTION_ENABLED and available
                active_executor = self.executor  # default: Bitget
                active_broker_name = "bitget"
                if SMC_CONFIG.get("KRAKEN_EXECUTION_ENABLED", False) and "kraken" in self.executors:
                    active_executor = self.executors["kraken"]
                    active_broker_name = "kraken"
                logger.info(
                    f"[EXEC_PATH] SINGLE-BROKER | {symbol} | broker={active_broker_name}"
                )
                exec_result = await order_mgr.execute(
                    symbol=symbol,
                    signal_dict=signal_dict,
                    signal_id=str(sig_id),
                    executor=active_executor,
                    active_trade=self.active_trade,
                    account_balance=self.account_balance,
                    quantity=validation["quantity"],
                    leverage=SMC_CONFIG["DEFAULT_LEVERAGE"],
                )

            # Phase 4.5 Latency Tracking
            analyzer = self.ctx.get_analyzer(symbol)
            is_stale = (exec_result.get("error_msg") == "STALE_DATA" or exec_result.get("error") == "STALE_DATA")
            _lat_ms = exec_result.get("latency_ms", 0.0)
            _price = signal_dict.get("entry", 0.0)
            analyzer.update(price=_price, latency_ms=_lat_ms, is_stale=is_stale)

            if exec_result.get("success"):
                # Update orchestrator state
                self.active_trade = signal_dict.copy()
                self.active_trade["order_id"] = exec_result.get("entry_order_id")
                self.active_trade["filled_price"] = exec_result.get("price_executed")
                self.active_trade["qty"] = exec_result.get("quantity", validation.get("quantity"))
                self.last_trade_time = now
                self.executed_signals.add(sig_id)
                self.trade_log.append({
                    "timestamp": datetime.now().isoformat(),
                    "signal": signal_dict,
                    "execution": exec_result,
                })

                # Publish ORDER_FILLED event (unconditional)
                if _p6_wiring_ok:
                    try:
                        from core.events.event_types import TradeEvent, EventType
                        _p6_publish(TradeEvent(
                            EventType.ORDER_FILLED,
                            symbol=symbol,
                            broker="bitget",
                            side=signal_dict.get("direction", ""),
                            price=float(exec_result.get("price_executed", 0) or 0),
                            qty=float(exec_result.get("quantity", 0) or 0),
                            order_id=str(exec_result.get("entry_order_id", "")),
                        ))
                    except Exception:
                        pass

                mode_label = "SHADOW" if SMC_CONFIG.get("SHADOW_MODE") else "FILLED"
                trace.add("EXEC", ExitAction.PASS.value, mode_label,
                          order_id=exec_result.get("entry_order_id"))

                # Store signal as last valid signal
                self.signal_history.append(signal_dict)
                self.last_signal = signal_dict
                return signal_dict  # Hook return

            else:
                # Pre-flight reject or execution error
                reason = exec_result.get("reason", "UNKNOWN")

                # Publish ORDER_REJECTED event (unconditional)
                if _p6_wiring_ok:
                    try:
                        from core.events.event_types import TradeEvent, EventType
                        _p6_publish(TradeEvent(
                            EventType.ORDER_REJECTED,
                            symbol=symbol,
                            broker="bitget",
                            side=signal_dict.get("direction", ""),
                            price=float(signal_dict.get("entry", 0)),
                            qty=float(validation.get("quantity", 0)),
                            extra={"reason": reason},
                        ))
                    except Exception:
                        pass

                if reason == "PAPER_MODE":
                    trace.add("ORDER", ExitAction.SKIP.value, "PAPER_MODE")
                elif reason == "EXECUTION_ERROR":
                    error_msg = exec_result.get("details", {}).get("error", "Unknown")
                    trace.add("EXEC", ExitAction.ERROR.value, error_msg)
                    metrics.safe_record(symbol, ExitAction.ERROR.value, trace)
                    return None
                else:
                    # Guard rejection (Cooldown, Max Trades, Kill Switch, etc.)
                    trace.add("ORDER", ExitAction.BLOCK.value, reason)
                    metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                    return None
                
        else:
            # Signal Only Mode
            logger.info(f"[SIGNAL] {symbol} | manual_mode | signal broadcast")
            trace.add("ORDER", ExitAction.SKIP.value, "MANUAL_MODE")

        self.signal_history.append(signal_dict)
        self.last_signal = signal_dict

        # Cap history
        if len(self.signal_history) > 100:
            self.signal_history.pop(0)
        if len(self.trade_log) > 100:
            self.trade_log.pop(0)
        if len(self.executed_signals) > 500:
            sorted_sigs = sorted(self.executed_signals)
            self.executed_signals = set(sorted_sigs[-200:])

        # ── Record PASS ──────────────────────────
        metrics.safe_record(symbol, ExitAction.PASS.value, trace)
        trace.log(logger)

        return signal_dict

    # ──────────────────────────────────────────────────
    #  Overlay Dispatch Helper
    # ──────────────────────────────────────────────────

    _OVERLAY_DISPATCH = {
        StrategyType.FUNDING_REVERSION: "_process_funding_reversion_signal",
        StrategyType.VWAP_REVERSION: "_process_vwap_reversion_signal",
        StrategyType.LIQUIDITY_SWEEP_SCALP: "_process_sweep_scalp_signal",
        StrategyType.LIQUIDATION_CASCADE: "_process_liquidation_cascade_signal",
        StrategyType.ORDERBOOK_IMBALANCE: "_process_orderbook_imbalance_signal",
    }

    async def _dispatch_overlay(
        self,
        strategy_type: "StrategyType",
        symbol: str,
        candle: Any,
        trace: Any,
        metrics: Any,
        now: int,
        score_only: bool = False,
    ) -> Optional[Dict]:
        """Try an overlay strategy; return signal dict or None."""
        handler_name = self._OVERLAY_DISPATCH.get(strategy_type)
        if not handler_name:
            return None
        handler = getattr(self, handler_name, None)
        if handler is None:
            return None
        try:
            result = await handler(
                symbol=symbol, candle=candle, trace=trace,
                metrics=metrics, now=now, score_only=score_only,
            )
            if result is not None and not score_only:
                logger.info(
                    f"[OVERLAY] {symbol} | {strategy_type.value} produced signal"
                )
            return result
        except Exception as e:
            logger.debug(f"[OVERLAY] {symbol} | {strategy_type.value} error: {e}")
            return None

    async def _process_asia_weekend_signal(
        self,
        symbol: str,
        candle: Any,
        trace: Any,
        metrics: Any,
        now: int,
        is_weekend: bool = False,
    ) -> Optional[Dict]:
        """
        Process signal using Asia/Weekend mean reversion strategy.
        
        Uses range detection + sweep confirmation for entries.
        More conservative R:R targets for low-volume sessions.
        """
        # ── Position dedup / cooldown ────────────
        if self.active_trade:
            logger.debug(f"[ASIA_STRAT] {symbol} | BLOCKED | reason=ACTIVE_TRADE")
            trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.POSITION_ALREADY_OPEN))
            metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
            return None
        if (now - self.last_trade_time) < 60_000:
            trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.COOLDOWN_ACTIVE),
                       cooldown_remaining_ms=60_000 - (now - self.last_trade_time))
            metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
            return None
        trace.add("GUARD", ExitAction.PASS.value, "NO_POSITION_NO_COOLDOWN")

        # ── Get candle history for range detection ────
        mtf_candles = [
            {
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "timestamp": c.timestamp,
            }
            for c in self.mtf_structure_engine.candle_history
        ] if hasattr(self.mtf_structure_engine, 'candle_history') else []
        
        ltf_candles = [
            {
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "timestamp": c.timestamp,
            }
            for c in self.ltf_structure_engine.candle_history
        ] if hasattr(self.ltf_structure_engine, 'candle_history') else []

        # ── Get states ────────────────────────────
        structure_state = self.ltf_structure_engine.get_current_state()
        # Feed HTF swing levels into liquidity heatmap
        htf_struct = self.htf_structure_engine.get_current_state()
        self.ltf_liquidity_engine.update_htf_levels(htf_struct)
        liquidity_state = self.ltf_liquidity_engine.get_current_state()
        ob_state = self.mtf_ob_engine.get_current_state()
        fvg_state = self.mtf_fvg_engine.get_current_state()

        # ── Build Asia/Weekend signal ─────────────
        result = self.asia_weekend_strategy.build_signal(
            symbol=symbol,
            candles_htf=[],  # HTF not used for range strategy
            candles_mtf=mtf_candles,
            candles_ltf=ltf_candles,
            structure_state=structure_state,
            liquidity_state=liquidity_state,
            ob_state=ob_state,
            fvg_state=fvg_state,
            current_price=candle.close,
            timestamp=candle.timestamp,
            atr=0.0,  # Will be calculated internally
            is_weekend=is_weekend,  # Pass weekend flag for threshold relaxation
        )

        # Record A/B test decision if present in metadata
        if hasattr(result, "metadata") and "ab_test" in result.metadata:
            ab = result.metadata["ab_test"]
            # The strategy already records it, but the orchestrator adds an audit log
            a_val = ab['A']['threshold']
            b_val = ab['B']['threshold'] if ab['B'] else 0.0
            a_action = "ACCEPT" if ab['A']['accept'] else "REJECT"
            b_action = "ACCEPT" if (ab['B'] and ab['B']['accept']) else "REJECT"
            
            logger.info(
                f"[A/B_AUDIT] mode={ab['mode']} symbol={symbol} selected={ab['selected']} "
                f"A={a_action}({a_val:.2f}) B={b_action}({b_val:.2f}) "
                f"key={ab['key']} weight={ab['weight']}"
            )

        if result.status != "CREATED" or not result.signal:
            action = ExitAction.REJECT.value if result.status == "REJECTED" else ExitAction.SKIP.value
            rejection_reasons = "; ".join(result.reasons) if result.reasons else result.status
            logger.info(
                f"[ASIA_STRAT] {symbol} | {result.status} | reasons={rejection_reasons}"
            )
            trace.add("SIGNAL", action, rejection_reasons, strategy="AsiaWeekendStrategy")
            metrics.safe_record(symbol, action, trace)
            return None

        logger.info(
            f"[ASIA_STRAT] {symbol} | SIGNAL_CREATED"
            f" | dir={result.signal.direction.value}"
            f" | rr={result.signal.risk_reward_ratio:.2f}"
            f" | entry={result.signal.entry_price}"
            f" | sl={result.signal.stop_loss}"
            f" | tp={result.signal.take_profit}"
            f" | range=[{result.range_low}-{result.range_high}]"
        )
        trace.add("SIGNAL", ExitAction.PASS.value, "CREATED",
                  direction=result.signal.direction.value,
                  rr=result.signal.risk_reward_ratio,
                  strategy="AsiaWeekendStrategy")

        # ── Build signal dict ─────────────────────
        signal = result.signal

        # ── Track ATR for regime detection + extreme vol filter ──
        current_atr = float(liquidity_state.get("atr", 0))
        if current_atr > 0:
            self._atr_history.append(current_atr)
            if len(self._atr_history) > self._atr_mean_window:
                self._atr_history = self._atr_history[-self._atr_mean_window:]
        avg_atr = sum(self._atr_history) / len(self._atr_history) if self._atr_history else current_atr

        signal_dict = {
            "direction": signal.direction.value,
            "entry": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "setup": signal.setup_type,
            "trigger": signal.trigger_type,
            "confidence": signal.confidence_score,
            "rr_ratio": signal.risk_reward_ratio,
            "invalidation": signal.invalidation_level,
            "bias": signal.mtf_bias,
            "timestamp": signal.timestamp,
            "t_signal": time.time(),  # Latency guard — decision time
            "atr": current_atr,  # For slippage ATR detection
            "strategy": "AsiaWeekendStrategy",
            "range_high": result.range_high,
            "range_low": result.range_low,
            "range_mid": result.range_mid,
            "sweep_side": result.sweep_side,
            "atr_info": {"current_atr": current_atr, "avg_atr": avg_atr},
        }

        # ── Regime Filter (strategy gating — Asia) ──
        regime = self.regime_filter.detect(
            candles=list(self.ltf_structure_engine.candle_history),
            current_atr=current_atr,
            avg_atr=avg_atr,
        )
        signal_dict["regime"] = regime.value
        self._cached_regime = regime.value
        allowed, conf_mult, regime_reason = self.regime_filter.gate_strategy(regime, "AsiaWeekendStrategy")
        if not allowed:
            logger.warning(f"[REGIME_GATE] {symbol} | BLOCKED (Asia) | {regime_reason}")
            trace.add("REGIME", ExitAction.BLOCK.value, regime_reason, strategy="AsiaWeekendStrategy")
            metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
            return None
        if conf_mult < 1.0:
            original_conf = signal_dict["confidence"]
            signal_dict["confidence"] = original_conf * conf_mult
            logger.info(f"[REGIME] {symbol} | confidence {original_conf:.2f} → {signal_dict['confidence']:.2f} ({regime_reason})")

        # ── Directional Liquidity Trap Filter (Asia) ─
        trap_atr_dist = SMC_CONFIG.get("LIQUIDITY_TRAP_MIN_ATR_DIST", 0.3)
        if current_atr > 0 and trap_atr_dist > 0:
            heatmap = liquidity_state.get("heatmap", {})
            heatmap_levels = heatmap.get("levels", [])
            entry_price = float(signal.entry_price)
            direction = signal.direction.value.lower()
            for lv in heatmap_levels:
                lv_price = float(lv.get("price", 0))
                lv_score = float(lv.get("score", 0))
                if lv_score >= 4.0:
                    dist = abs(entry_price - lv_price) / current_atr
                    if dist < trap_atr_dist:
                        cluster_is_ahead = (
                            (direction == "long" and lv_price > entry_price)
                            or (direction == "short" and lv_price < entry_price)
                        )
                        cluster_at_entry = abs(lv_price - entry_price) / entry_price < 0.001
                        if cluster_is_ahead or cluster_at_entry:
                            logger.warning(
                                f"[LIQUIDITY_TRAP] {symbol} | entry={entry_price:.4f} too close to"
                                f" cluster={lv_price:.4f} (dist={dist:.2f} ATR < {trap_atr_dist})"
                                f" | score={lv_score:.1f} | dir={direction} — BLOCKED (Asia)"
                            )
                            trace.add("LIQUIDITY_TRAP", ExitAction.BLOCK.value,
                                      f"entry too close to liquidity cluster (dist={dist:.2f} ATR, dir={direction})",
                                      strategy="AsiaWeekendStrategy")
                            metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                            return None
                        else:
                            logger.info(
                                f"[LIQUIDITY_TRAP] {symbol} | cluster={lv_price:.4f} behind entry"
                                f" ({direction}) — protective, allowing (Asia)"
                            )

        sig_id = hash(f"{symbol}_{signal.direction.value}_{signal.entry_price}_{signal.timestamp}")
        if sig_id in self.executed_signals:
            trace.add("DEDUP", ExitAction.BLOCK.value, str(ReasonCode.DUPLICATE_SIGNAL))
            metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
            return None

        # ── Validate risk (use reduced risk factor) ────
        # [PHASE 6] Apply Asia risk factor
        risk_factor = SMC_CONFIG.get("ASIA_RISK_FACTOR", 0.5)
        base_risk = SMC_CONFIG["RISK_PER_TRADE_PCT"]
        adjusted_risk_pct = base_risk * risk_factor
        
        logger.info(
            "[RISK] session=asia strategy=AsiaWeekendStrategy base_risk=%.4f factor=%.2f effective_risk=%.4f",
            base_risk, risk_factor, adjusted_risk_pct
        )
        
        validation = self.smc_validator.validate(
            symbol=symbol,
            signal_dict=signal_dict,
            current_price=candle.close,
            leverage=SMC_CONFIG["DEFAULT_LEVERAGE"],
            neutral_override=False,
        )

        if not validation["valid"]:
            trace.add("RISK", ExitAction.REJECT.value, str(validation.get("reason", "UNKNOWN")))
            metrics.safe_record(symbol, ExitAction.REJECT.value, trace)
            return None
        
        # Adjust quantity by risk factor
        adjusted_qty = validation["quantity"] * risk_factor
        trace.add("RISK", ExitAction.PASS.value, f"rr={validation['rr']:.2f}", 
                  qty=adjusted_qty, risk_factor=risk_factor)

        # ── Execution ─────────────────────────────
        order_mgr = self.ctx.order_manager
        
        should_auto_execute = (
            (SMC_CONFIG["AUTO_EXECUTE_ENABLED"] or SMC_CONFIG["SHADOW_MODE"])
            and (self.live_trading or SMC_CONFIG["SHADOW_MODE"])
        )
        
        logger.info(
            f"[ASIA_EXEC_DECISION] {symbol} | should_auto_execute={should_auto_execute}"
            f" | strategy=AsiaWeekendStrategy"
        )
        
        if should_auto_execute:
            logger.info(f"[ASIA_AUTO_EXEC] HOOK TRIGGERED | {symbol} | signal_id={sig_id}")

            # ════════════════════════════════════════════════════════════════
            # MULTI-BROKER vs SINGLE-BROKER execution path (Asia Strategy)
            # ════════════════════════════════════════════════════════════════
            use_multi_broker = (
                self.multi_broker_enabled and 
                len(self.executors) > 1 and
                hasattr(order_mgr, 'execute_multi_broker') and
                (
                    SMC_CONFIG.get("MIRROR_TRADING_ENABLED", False) or
                    SMC_CONFIG.get("KRAKEN_EXECUTION_ENABLED", False)
                )
            )
            
            if use_multi_broker:
                # Multi-broker mirror execution
                logger.info(
                    f"[ASIA_MULTI_EXEC] Using multi-broker path | brokers={list(self.executors.keys())}"
                )
                multi_result = await order_mgr.execute_multi_broker(
                    symbol=symbol,
                    signal_dict=signal_dict,
                    signal_id=str(sig_id),
                    executors=self.executors,
                    active_trade=self.active_trade,
                    account_balance=self.account_balance,
                    quantity=adjusted_qty,
                    leverage=SMC_CONFIG["DEFAULT_LEVERAGE"],
                )
                
                # Convert multi-broker result to standard format
                if multi_result.success:
                    first_success = next(
                        (r for r in multi_result.broker_results.values() if r.success),
                        None
                    )
                    exec_result = {
                        "success": True,
                        "entry_order_id": first_success.order_id if first_success else None,
                        "price_executed": first_success.fill_price if first_success else None,
                        "quantity": first_success.quantity if first_success else adjusted_qty,
                        "multi_broker": True,
                        "broker_results": multi_result.broker_results,
                        "signal_id": multi_result.signal_id,
                    }
                else:
                    exec_result = {
                        "success": False,
                        "reason": "MULTI_BROKER_FAILED",
                        "details": {
                            "broker_results": {
                                b: {"success": r.success, "error": r.error}
                                for b, r in multi_result.broker_results.items()
                            }
                        },
                    }
            else:
                # Single-broker execution (original behavior)
                exec_result = await order_mgr.execute(
                    symbol=symbol,
                    signal_dict=signal_dict,
                    signal_id=str(sig_id),
                    executor=self.executor,
                    active_trade=self.active_trade,
                    account_balance=self.account_balance,
                    quantity=adjusted_qty,
                    leverage=SMC_CONFIG["DEFAULT_LEVERAGE"],
                )

            if exec_result.get("success"):
                self.active_trade = signal_dict.copy()
                self.active_trade["order_id"] = exec_result.get("entry_order_id")
                self.active_trade["filled_price"] = exec_result.get("price_executed")
                self.last_trade_time = now
                self.executed_signals.add(sig_id)
                self.trade_log.append({
                    "timestamp": datetime.now().isoformat(),
                    "signal": signal_dict,
                    "execution": exec_result,
                    "strategy": "AsiaWeekendStrategy",
                })

                mode_label = "SHADOW" if SMC_CONFIG.get("SHADOW_MODE") else "FILLED"
                trace.add("EXEC", ExitAction.PASS.value, mode_label,
                          order_id=exec_result.get("entry_order_id"),
                          strategy="AsiaWeekendStrategy")

                self.signal_history.append(signal_dict)
                self.last_signal = signal_dict
                
                metrics.safe_record(symbol, ExitAction.PASS.value, trace)
                trace.log(logger)
                return signal_dict

            else:
                reason = exec_result.get("reason", "UNKNOWN")
                if reason == "PAPER_MODE":
                    trace.add("ORDER", ExitAction.SKIP.value, "PAPER_MODE")
                elif reason == "EXECUTION_ERROR":
                    error_msg = exec_result.get("details", {}).get("error", "Unknown")
                    trace.add("EXEC", ExitAction.ERROR.value, error_msg)
                    metrics.safe_record(symbol, ExitAction.ERROR.value, trace)
                    return None
                else:
                    trace.add("ORDER", ExitAction.BLOCK.value, reason)
                    metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                    return None
        else:
            logger.info(f"[ASIA_SIGNAL] {symbol} | manual_mode | signal broadcast")
            trace.add("ORDER", ExitAction.SKIP.value, "MANUAL_MODE")

        self.signal_history.append(signal_dict)
        self.last_signal = signal_dict

        metrics.safe_record(symbol, ExitAction.PASS.value, trace)
        trace.log(logger)

        return signal_dict

    # ════════════════════════════════════════════════════════════════
    #  _process_funding_reversion_signal
    # ════════════════════════════════════════════════════════════════
    async def _process_funding_reversion_signal(
        self,
        symbol: str,
        candle,
        trace,
        metrics,
        now: int,
        score_only: bool = False,
    ) -> Optional[Dict]:
        """Process signal via Funding Rate Reversion strategy."""
        # ── Guards ────────────────────────────────
        if not score_only:
            if self.active_trade:
                trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.POSITION_ALREADY_OPEN))
                metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                return None
            if (now - self.last_trade_time) < 60_000:
                trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.COOLDOWN_ACTIVE))
                metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                return None

        # ── Fetch funding rate ────────────────────
        funding_rate = 0.0
        try:
            if hasattr(self.executor, 'get_funding_rate'):
                funding_rate = await self.executor.get_funding_rate(symbol) or 0.0
        except Exception as e:
            logger.warning(f"[FUNDING_STRAT] {symbol} | funding_rate fetch error: {e}")
            return None

        # ── Build candle list for RSI ─────────────
        ltf_candles = [
            {"open": c.open, "high": c.high, "low": c.low, "close": c.close, "timestamp": c.timestamp}
            for c in self.ltf_structure_engine.candle_history
        ] if hasattr(self.ltf_structure_engine, 'candle_history') else []

        liquidity_state = self.ltf_liquidity_engine.get_current_state()
        current_atr = float(liquidity_state.get("atr", 0))

        result = self.funding_reversion_strategy.build_signal(
            symbol=symbol,
            funding_rate=funding_rate,
            current_price=candle.close,
            candles=ltf_candles,
            atr=current_atr,
            timestamp=candle.timestamp,
        )

        if result.status != "CREATED" or not result.signal:
            rejection_reasons = "; ".join(result.reasons) if result.reasons else result.status
            logger.info(f"[FUNDING_STRAT] {symbol} | {result.status} | {rejection_reasons}")
            trace.add("SIGNAL", ExitAction.SKIP.value, rejection_reasons, strategy="FundingReversion")
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None

        return await self._finalize_alt_signal(
            symbol=symbol, candle=candle, result=result,
            strategy_name="FundingReversion", current_atr=current_atr,
            risk_factor=SMC_CONFIG.get("FUNDING_REV_RISK_FACTOR", 0.6),
            trace=trace, metrics=metrics, now=now,
            score_only=score_only,
        )

    # ════════════════════════════════════════════════════════════════
    #  _process_vwap_reversion_signal
    # ════════════════════════════════════════════════════════════════
    async def _process_vwap_reversion_signal(
        self,
        symbol: str,
        candle,
        trace,
        metrics,
        now: int,
        score_only: bool = False,
    ) -> Optional[Dict]:
        """Process signal via VWAP Mean Reversion strategy."""
        # ── Guards ────────────────────────────────
        if not score_only:
            if self.active_trade:
                trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.POSITION_ALREADY_OPEN))
                metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                return None
            if (now - self.last_trade_time) < 60_000:
                trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.COOLDOWN_ACTIVE))
                metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                return None

        # ── Compute VWAP from MTF candles ─────────
        mtf_candles = [
            {"open": c.open, "high": c.high, "low": c.low, "close": c.close,
             "volume": getattr(c, 'volume', 0), "timestamp": c.timestamp}
            for c in self.mtf_structure_engine.candle_history
        ] if hasattr(self.mtf_structure_engine, 'candle_history') else []

        vwap = compute_session_vwap(mtf_candles)
        if vwap <= 0:
            trace.add("SIGNAL", ExitAction.SKIP.value, "NO_VWAP_DATA", strategy="VWAPReversion")
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None

        liquidity_state = self.ltf_liquidity_engine.get_current_state()
        current_atr = float(liquidity_state.get("atr", 0))

        result = self.vwap_reversion_strategy.build_signal(
            symbol=symbol,
            current_price=candle.close,
            vwap=vwap,
            atr=current_atr,
            timestamp=candle.timestamp,
        )

        if result.status != "CREATED" or not result.signal:
            rejection_reasons = "; ".join(result.reasons) if result.reasons else result.status
            logger.info(f"[VWAP_STRAT] {symbol} | {result.status} | {rejection_reasons}")
            trace.add("SIGNAL", ExitAction.SKIP.value, rejection_reasons, strategy="VWAPReversion")
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None

        return await self._finalize_alt_signal(
            symbol=symbol, candle=candle, result=result,
            strategy_name="VWAPReversion", current_atr=current_atr,
            risk_factor=SMC_CONFIG.get("VWAP_REV_RISK_FACTOR", 0.65),
            trace=trace, metrics=metrics, now=now,
            score_only=score_only,
        )

    # ════════════════════════════════════════════════════════════════
    #  _process_sweep_scalp_signal
    # ════════════════════════════════════════════════════════════════
    async def _process_sweep_scalp_signal(
        self,
        symbol: str,
        candle,
        trace,
        metrics,
        now: int,
        score_only: bool = False,
    ) -> Optional[Dict]:
        """Process signal via Liquidity Sweep Scalp strategy."""
        # ── Guards ────────────────────────────────
        if not score_only:
            if self.active_trade:
                trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.POSITION_ALREADY_OPEN))
                metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                return None
            if (now - self.last_trade_time) < 60_000:
                trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.COOLDOWN_ACTIVE))
                metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                return None

        # ── Get LTF candles + liquidity state ─────
        ltf_candles = [
            {"open": c.open, "high": c.high, "low": c.low, "close": c.close, "timestamp": c.timestamp}
            for c in self.ltf_structure_engine.candle_history
        ] if hasattr(self.ltf_structure_engine, 'candle_history') else []

        htf_struct = self.htf_structure_engine.get_current_state()
        self.ltf_liquidity_engine.update_htf_levels(htf_struct)
        liquidity_state = self.ltf_liquidity_engine.get_current_state()
        current_atr = float(liquidity_state.get("atr", 0))

        result = self.liquidity_sweep_scalp_strategy.build_signal(
            symbol=symbol,
            current_price=candle.close,
            candles=ltf_candles,
            liquidity_state=liquidity_state,
            atr=current_atr,
            timestamp=candle.timestamp,
        )

        if result.status != "CREATED" or not result.signal:
            rejection_reasons = "; ".join(result.reasons) if result.reasons else result.status
            logger.info(f"[SWEEP_SCALP] {symbol} | {result.status} | {rejection_reasons}")
            trace.add("SIGNAL", ExitAction.SKIP.value, rejection_reasons, strategy="SweepScalp")
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None

        return await self._finalize_alt_signal(
            symbol=symbol, candle=candle, result=result,
            strategy_name="SweepScalp", current_atr=current_atr,
            risk_factor=SMC_CONFIG.get("SWEEP_SCALP_RISK_FACTOR", 0.4),
            trace=trace, metrics=metrics, now=now,
            score_only=score_only,
        )

    # ════════════════════════════════════════════════════════════════
    #  _process_liquidation_cascade_signal
    # ════════════════════════════════════════════════════════════════
    async def _process_liquidation_cascade_signal(
        self,
        symbol: str,
        candle,
        trace,
        metrics,
        now: int,
        score_only: bool = False,
    ) -> Optional[Dict]:
        """Process signal via Liquidation Cascade strategy."""
        # ── Guards ────────────────────────────────
        if not score_only:
            if self.active_trade:
                trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.POSITION_ALREADY_OPEN))
                metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                return None
            if (now - self.last_trade_time) < 60_000:
                trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.COOLDOWN_ACTIVE))
                metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                return None

        # ── Fetch liquidation data ────────────────
        liquidation_volume_usd = 0.0
        liquidation_side = "long"
        try:
            if hasattr(self.executor, 'get_liquidation_data'):
                liq_data = await self.executor.get_liquidation_data(symbol)
                if liq_data:
                    liquidation_volume_usd = float(liq_data.get("volume_usd", 0))
                    liquidation_side = liq_data.get("side", "long")
        except Exception as e:
            logger.warning(f"[LIQ_CASCADE] {symbol} | liq_data fetch error: {e}")

        if liquidation_volume_usd <= 0:
            trace.add("SIGNAL", ExitAction.SKIP.value, "NO_LIQ_DATA", strategy="LiqCascade")
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None

        ltf_candles = [
            {"open": c.open, "high": c.high, "low": c.low, "close": c.close, "timestamp": c.timestamp}
            for c in self.ltf_structure_engine.candle_history
        ] if hasattr(self.ltf_structure_engine, 'candle_history') else []

        liquidity_state = self.ltf_liquidity_engine.get_current_state()
        current_atr = float(liquidity_state.get("atr", 0))

        result = self.liquidation_cascade_strategy.build_signal(
            symbol=symbol,
            current_price=candle.close,
            candles=ltf_candles,
            liquidation_volume_usd=liquidation_volume_usd,
            liquidation_side=liquidation_side,
            atr=current_atr,
            timestamp=candle.timestamp,
        )

        if result.status != "CREATED" or not result.signal:
            rejection_reasons = "; ".join(result.reasons) if result.reasons else result.status
            logger.info(f"[LIQ_CASCADE] {symbol} | {result.status} | {rejection_reasons}")
            trace.add("SIGNAL", ExitAction.SKIP.value, rejection_reasons, strategy="LiqCascade")
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None

        return await self._finalize_alt_signal(
            symbol=symbol, candle=candle, result=result,
            strategy_name="LiqCascade", current_atr=current_atr,
            risk_factor=SMC_CONFIG.get("LIQ_RISK_FACTOR", 0.55),
            trace=trace, metrics=metrics, now=now,
            score_only=score_only,
        )

    # ════════════════════════════════════════════════════════════════
    #  _process_orderbook_imbalance_signal
    # ════════════════════════════════════════════════════════════════
    async def _process_orderbook_imbalance_signal(
        self,
        symbol: str,
        candle,
        trace,
        metrics,
        now: int,
        score_only: bool = False,
    ) -> Optional[Dict]:
        """Process signal via Order Book Imbalance strategy."""
        # ── Guards ────────────────────────────────
        if not score_only:
            if self.active_trade:
                trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.POSITION_ALREADY_OPEN))
                metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                return None
            if (now - self.last_trade_time) < 60_000:
                trace.add("GUARD", ExitAction.BLOCK.value, str(ReasonCode.COOLDOWN_ACTIVE))
                metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                return None

        # ── Fetch order book depth ────────────────
        bid_volume = 0.0
        ask_volume = 0.0
        cvd = None
        try:
            if hasattr(self.executor, 'get_orderbook_depth'):
                ob_data = await self.executor.get_orderbook_depth(
                    symbol, levels=SMC_CONFIG.get("OB_DEPTH_LEVELS", 20)
                )
                if ob_data:
                    bid_volume = float(ob_data.get("bid_volume", 0))
                    ask_volume = float(ob_data.get("ask_volume", 0))
                    cvd = ob_data.get("cvd")
                    if cvd is not None:
                        cvd = float(cvd)
        except Exception as e:
            logger.warning(f"[OBI] {symbol} | orderbook fetch error: {e}")

        if (bid_volume + ask_volume) <= 0:
            trace.add("SIGNAL", ExitAction.SKIP.value, "NO_OB_DATA", strategy="OBImbalance")
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None

        # ── S/R proximity from liquidity engine ───
        liquidity_state = self.ltf_liquidity_engine.get_current_state()
        current_atr = float(liquidity_state.get("atr", 0))
        support_levels = liquidity_state.get("support_levels", [])
        resistance_levels = liquidity_state.get("resistance_levels", [])

        near_support = any(
            abs(candle.close - float(lv)) < current_atr * 0.5
            for lv in support_levels
        ) if support_levels and current_atr > 0 else False
        near_resistance = any(
            abs(candle.close - float(lv)) < current_atr * 0.5
            for lv in resistance_levels
        ) if resistance_levels and current_atr > 0 else False

        result = self.orderbook_imbalance_strategy.build_signal(
            symbol=symbol,
            current_price=candle.close,
            bid_volume=bid_volume,
            ask_volume=ask_volume,
            atr=current_atr,
            timestamp=candle.timestamp,
            cvd=cvd,
            price_near_support=near_support,
            price_near_resistance=near_resistance,
        )

        if result.status != "CREATED" or not result.signal:
            rejection_reasons = "; ".join(result.reasons) if result.reasons else result.status
            logger.info(f"[OBI] {symbol} | {result.status} | {rejection_reasons}")
            trace.add("SIGNAL", ExitAction.SKIP.value, rejection_reasons, strategy="OBImbalance")
            metrics.safe_record(symbol, ExitAction.SKIP.value, trace)
            return None

        return await self._finalize_alt_signal(
            symbol=symbol, candle=candle, result=result,
            strategy_name="OBImbalance", current_atr=current_atr,
            risk_factor=SMC_CONFIG.get("OB_RISK_FACTOR", 0.50),
            trace=trace, metrics=metrics, now=now,
            score_only=score_only,
        )

    # ════════════════════════════════════════════════════════════════
    #  Shared finalization for alternative strategies
    # ════════════════════════════════════════════════════════════════
    async def _finalize_alt_signal(
        self,
        symbol: str,
        candle,
        result,
        strategy_name: str,
        current_atr: float,
        risk_factor: float,
        trace,
        metrics,
        now: int,
        score_only: bool = False,
    ) -> Optional[Dict]:
        """Shared validation → dedup → execution flow for alt strategies."""
        signal = result.signal
        logger.info(
            f"[{strategy_name.upper()}] {symbol} | SIGNAL_CREATED"
            f" | dir={signal.direction.value} | rr={signal.risk_reward_ratio:.2f}"
            f" | entry={signal.entry_price}"
        )
        trace.add("SIGNAL", ExitAction.PASS.value, "CREATED",
                  direction=signal.direction.value, rr=signal.risk_reward_ratio,
                  strategy=strategy_name)

        # ── ATR history ──────────────────────────
        if current_atr > 0:
            self._atr_history.append(current_atr)
            if len(self._atr_history) > self._atr_mean_window:
                self._atr_history = self._atr_history[-self._atr_mean_window:]
        avg_atr = sum(self._atr_history) / len(self._atr_history) if self._atr_history else current_atr

        signal_dict = {
            "direction": signal.direction.value,
            "entry": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "setup": signal.setup_type,
            "trigger": signal.trigger_type,
            "confidence": signal.confidence_score,
            "rr_ratio": signal.risk_reward_ratio,
            "invalidation": signal.invalidation_level,
            "bias": signal.mtf_bias,
            "timestamp": signal.timestamp,
            "t_signal": time.time(),
            "atr": current_atr,
            "strategy": strategy_name,
            "atr_info": {"current_atr": current_atr, "avg_atr": avg_atr},
        }

        # ── Regime Filter ────────────────────────
        regime = self.regime_filter.detect(
            candles=list(self.ltf_structure_engine.candle_history),
            current_atr=current_atr,
            avg_atr=avg_atr,
        )
        signal_dict["regime"] = regime.value
        allowed, conf_mult, regime_reason = self.regime_filter.gate_strategy(regime, strategy_name)
        if not allowed:
            logger.warning(f"[REGIME_GATE] {symbol} | BLOCKED | {regime_reason}")
            trace.add("REGIME", ExitAction.BLOCK.value, regime_reason, strategy=strategy_name)
            metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
            return None
        if conf_mult < 1.0:
            signal_dict["confidence"] *= conf_mult

        # ── Dedup ────────────────────────────────
        sig_id = hash(f"{symbol}_{signal.direction.value}_{signal.entry_price}_{signal.timestamp}")
        if not score_only and sig_id in self.executed_signals:
            trace.add("DEDUP", ExitAction.BLOCK.value, str(ReasonCode.DUPLICATE_SIGNAL))
            metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
            return None

        # ── Alpha Scoring ────────────────────────
        if self._alpha_enabled:
            from smc_futures.observability.metrics import (
                ALPHA_SCORE_HISTOGRAM, ALPHA_SIGNALS_SCORED_TOTAL,
                ALPHA_SIGNALS_ACCEPTED_TOTAL, ALPHA_SIGNALS_REJECTED_TOTAL,
                ALPHA_CAPITAL_FRACTION_GAUGE, ALPHA_WEIGHT_GAUGE,
            )
            strat_weights = self._weight_tracker.get_weights()

            # Apply governance dampening if weight velocity too high
            if self._alpha_gov_enabled:
                dampened = self._alpha_governor.get_dampened_weight(strategy_name)
                if dampened is not None:
                    strat_weights = dict(strat_weights)
                    strat_weights[strategy_name] = dampened

            scored_list = self._strategy_scorer.score_signals(
                [signal_dict], regime=regime.value, strategy_weights=strat_weights,
            )
            if scored_list:
                scored = scored_list[0]

                # score_only: return candidate without recording or executing
                if score_only:
                    best = self._strategy_scorer.select_best(scored_list)
                    if best is None:
                        return None  # below min score — not even a shadow candidate
                    return {
                        "_candidate": True,
                        "strategy": strategy_name,
                        "score": scored.score,
                        "signal_dict": signal_dict,
                        "result": result,
                        "current_atr": current_atr,
                        "risk_factor": risk_factor,
                    }

                ALPHA_SIGNALS_SCORED_TOTAL.labels(strategy=strategy_name).inc()
                ALPHA_SCORE_HISTOGRAM.labels(strategy=strategy_name).observe(scored.score)
                ALPHA_WEIGHT_GAUGE.labels(strategy=strategy_name).set(
                    strat_weights.get(strategy_name, 1.0)
                )

                # Record in governance
                if self._alpha_gov_enabled:
                    self._alpha_governor.record_scored(strategy_name, scored.score)

                best = self._strategy_scorer.select_best(scored_list)
                if best is None:
                    ALPHA_SIGNALS_REJECTED_TOTAL.labels(strategy=strategy_name).inc()
                    if self._alpha_gov_enabled:
                        self._alpha_governor.record_rejected(strategy_name, "below_min_score", scored.score)
                    logger.info(
                        f"[ALPHA_GATE] {symbol} | {strategy_name} REJECTED"
                        f" | score={scored.score:.3f}"
                    )
                    trace.add("ALPHA", ExitAction.BLOCK.value,
                              f"score={scored.score:.3f}<min", strategy=strategy_name)
                    metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                    return None
                ALPHA_SIGNALS_ACCEPTED_TOTAL.labels(strategy=strategy_name).inc()
                # Apply capital allocation to adjust risk_factor
                cap_frac = self._strategy_capital_allocator.get_fraction(strategy_name)
                ALPHA_CAPITAL_FRACTION_GAUGE.labels(strategy=strategy_name).set(cap_frac)
                risk_factor = risk_factor * cap_frac / 0.15  # normalize relative to ~15% base

                # Record acceptance in governance
                if self._alpha_gov_enabled:
                    self._alpha_governor.record_accepted(strategy_name, cap_frac, scored.score)

                logger.info(
                    f"[ALPHA] {symbol} | {strategy_name} ACCEPTED"
                    f" | score={scored.score:.3f} | cap={cap_frac:.1%}"
                    f" | adj_rf={risk_factor:.3f}"
                )
                trace.add("ALPHA", ExitAction.PASS.value,
                          f"score={scored.score:.3f} cap={cap_frac:.1%}",
                          strategy=strategy_name)

        # ── Alpha Governance Check ───────────────
        if self._alpha_gov_enabled and self._check_cycle_counter() % 10 == 0:
            try:
                from smc_futures.observability.metrics import (
                    ALPHA_GOV_ALERT_TOTAL, ALPHA_GOV_STARVATION_GAUGE,
                    ALPHA_GOV_CONCENTRATION_GAUGE, ALPHA_GOV_DEAD_STRATEGIES_GAUGE,
                    ALPHA_GOV_ACCEPT_RATE, ALPHA_GOV_ATTRIBUTION_PNL,
                    ALPHA_GOV_BREADTH, ALPHA_GOV_SCORE_SPREAD,
                    ALPHA_GOV_SHADOW_TOTAL,
                )
                gov_alerts = self._alpha_governor.check()
                for ga in gov_alerts:
                    ALPHA_GOV_ALERT_TOTAL.labels(
                        alert_type=ga.alert_type, severity=ga.severity
                    ).inc()
                snap = self._alpha_governor.snapshot()
                health = snap.get("health", {})
                ALPHA_GOV_STARVATION_GAUGE.set(health.get("strategies_starved", 0))
                ALPHA_GOV_CONCENTRATION_GAUGE.set(0)  # updated per-strategy below
                ALPHA_GOV_DEAD_STRATEGIES_GAUGE.set(health.get("strategies_dead", 0))
                ALPHA_GOV_BREADTH.set(health.get("alpha_breadth", 0))
                ALPHA_GOV_SCORE_SPREAD.set(health.get("signal_quality_spread", 0))
                for attr in snap.get("attribution", []):
                    sn = attr["strategy"]
                    ALPHA_GOV_ACCEPT_RATE.labels(strategy=sn).set(attr.get("accept_rate_pct", 0))
                    ALPHA_GOV_ATTRIBUTION_PNL.labels(strategy=sn).set(attr.get("pnl", 0))
                    ALPHA_GOV_SHADOW_TOTAL.labels(strategy=sn).set(attr.get("signals_shadow", 0))
            except Exception as e:
                logger.debug(f"[ALPHA_GOV] Governance check error: {e}")

        # ── Validate risk ────────────────────────
        validation = self.smc_validator.validate(
            symbol=symbol,
            signal_dict=signal_dict,
            current_price=candle.close,
            leverage=SMC_CONFIG["DEFAULT_LEVERAGE"],
            neutral_override=True,
        )
        if not validation["valid"]:
            trace.add("RISK", ExitAction.REJECT.value, str(validation.get("reason", "UNKNOWN")))
            metrics.safe_record(symbol, ExitAction.REJECT.value, trace)
            return None

        adjusted_qty = validation["quantity"] * risk_factor
        trace.add("RISK", ExitAction.PASS.value, f"rr={validation['rr']:.2f}",
                  qty=adjusted_qty, risk_factor=risk_factor)

        # ── Execution ────────────────────────────
        order_mgr = self.ctx.order_manager
        should_auto_execute = (
            (SMC_CONFIG["AUTO_EXECUTE_ENABLED"] or SMC_CONFIG["SHADOW_MODE"])
            and (self.live_trading or SMC_CONFIG["SHADOW_MODE"])
        )

        if should_auto_execute:
            logger.info(f"[{strategy_name.upper()}_EXEC] {symbol} | signal_id={sig_id}")

            use_multi_broker = (
                self.multi_broker_enabled
                and len(self.executors) > 1
                and hasattr(order_mgr, 'execute_multi_broker')
                and (
                    SMC_CONFIG.get("MIRROR_TRADING_ENABLED", False)
                    or SMC_CONFIG.get("KRAKEN_EXECUTION_ENABLED", False)
                )
            )

            if use_multi_broker:
                multi_result = await order_mgr.execute_multi_broker(
                    symbol=symbol,
                    signal_dict=signal_dict,
                    signal_id=str(sig_id),
                    executors=self.executors,
                    active_trade=self.active_trade,
                    account_balance=self.account_balance,
                    quantity=adjusted_qty,
                    leverage=SMC_CONFIG["DEFAULT_LEVERAGE"],
                )
                if multi_result.success:
                    first_success = next(
                        (r for r in multi_result.broker_results.values() if r.success), None
                    )
                    exec_result = {
                        "success": True,
                        "entry_order_id": first_success.order_id if first_success else None,
                        "price_executed": first_success.fill_price if first_success else None,
                        "quantity": first_success.quantity if first_success else adjusted_qty,
                        "multi_broker": True,
                    }
                else:
                    exec_result = {"success": False, "reason": "MULTI_BROKER_FAILED"}
            else:
                exec_result = await order_mgr.execute(
                    symbol=symbol,
                    signal_dict=signal_dict,
                    signal_id=str(sig_id),
                    executor=self.executor,
                    active_trade=self.active_trade,
                    account_balance=self.account_balance,
                    quantity=adjusted_qty,
                    leverage=SMC_CONFIG["DEFAULT_LEVERAGE"],
                )

            if exec_result.get("success"):
                self.active_trade = signal_dict.copy()
                self.active_trade["order_id"] = exec_result.get("entry_order_id")
                self.active_trade["filled_price"] = exec_result.get("price_executed")
                self.active_trade["qty"] = exec_result.get("quantity", adjusted_qty)
                self.last_trade_time = now
                self.executed_signals.add(sig_id)
                self.trade_log.append({
                    "timestamp": datetime.now().isoformat(),
                    "signal": signal_dict,
                    "execution": exec_result,
                    "strategy": strategy_name,
                })
                mode_label = "SHADOW" if SMC_CONFIG.get("SHADOW_MODE") else "FILLED"
                trace.add("EXEC", ExitAction.PASS.value, mode_label,
                          order_id=exec_result.get("entry_order_id"), strategy=strategy_name)
                self.signal_history.append(signal_dict)
                self.last_signal = signal_dict
                metrics.safe_record(symbol, ExitAction.PASS.value, trace)
                trace.log(logger)
                return signal_dict
            else:
                reason = exec_result.get("reason", "UNKNOWN")
                if reason == "PAPER_MODE":
                    trace.add("ORDER", ExitAction.SKIP.value, "PAPER_MODE")
                elif reason == "EXECUTION_ERROR":
                    error_msg = exec_result.get("details", {}).get("error", "Unknown")
                    trace.add("EXEC", ExitAction.ERROR.value, error_msg)
                    metrics.safe_record(symbol, ExitAction.ERROR.value, trace)
                    return None
                else:
                    trace.add("ORDER", ExitAction.BLOCK.value, reason)
                    metrics.safe_record(symbol, ExitAction.BLOCK.value, trace)
                    return None
        else:
            trace.add("ORDER", ExitAction.SKIP.value, "MANUAL_MODE")

        self.signal_history.append(signal_dict)
        self.last_signal = signal_dict
        metrics.safe_record(symbol, ExitAction.PASS.value, trace)
        trace.log(logger)
        return signal_dict

    def _check_cycle_counter(self) -> int:
        """Incrementing counter for governance check throttling."""
        if not hasattr(self, '_gov_cycle'):
            self._gov_cycle = 0
        self._gov_cycle += 1
        return self._gov_cycle

    def _parse_candle(self, c_data: Dict) -> Candle:
        return Candle(
            timestamp=c_data.get('timestamp') or c_data.get('ts'),
            open=float(c_data['open']),
            high=float(c_data['high']),
            low=float(c_data['low']),
            close=float(c_data['close']),
            volume=float(c_data.get('volume', 0))
        )
    
    # [REMOVED DUPLICATE] process_uptodate_candle (Merged into process_ltf_execution flow)
    async def _process_envelope_logic(self, candle_data: Dict = None) -> Optional[Dict]:
        """
        Full Mean Reversion Envelope strategy cycle.
        """
        if not self.executor:
            logger.warning("⚠️ No executor available — envelope strategy requires live trading")
            return None
            
        try:
            # 1. Fetch historical data for MA calculation
            df = await self._get_recent_ohlcv_dataframe()
            if df.empty:
                logger.warning(f"⚠️ No OHLCV data available for {self.symbol}")
                return {"status": "no_action", "message": f"No OHLCV data for {self.symbol}. Check network/symbol."}
                
            # 2. Calculate envelope levels
            levels = self.envelope_engine.calculate_levels(df)
            if not levels:
                logger.warning("⚠️ Could not calculate envelope levels")
                return {"status": "no_action", "message": "Failed to calculate envelope levels (need more candles)"}
            
            logger.info(f"📊 Envelope levels: MA={levels['ma_base']:.2f}, "
                       f"Low={[f'{l:.2f}' for l in levels['low']]}, "
                       f"High={[f'{h:.2f}' for h in levels['high']]}")
                
            # 3. Get account balance
            balance = await self.executor.get_account_equity()
            logger.info(f"💰 Account Balance Check: {balance:.2f} USDT")
            
            if balance <= 0:
                logger.warning(f"⚠️ Zero or negative balance: {balance}")
                return {"status": "no_action", "message": f"Balance is zero ({balance} USDT). Transfer funds to USDT-FUTURES."}
            
            leverage = 1  # Envelope strategy uses 1x by default
            buying_power = balance * leverage
            logger.info(f"💰 Balance: {balance:.2f} USDT, Buying Power: {buying_power:.2f} USDT")
            
            # 4. Get current state
            positions_raw = await self.executor.get_positions(self.symbol)
            positions = positions_raw.get("data", []) if isinstance(positions_raw, dict) else (positions_raw if isinstance(positions_raw, list) else [])
            
            open_orders = await self.executor.get_open_orders(self.symbol)
            
            # 5. Cancel ALL pending orders
            order_ids = []
            buy_orders = []
            sell_orders = []
            for order in open_orders:
                oid = order.get("orderId", order.get("id", ""))
                if oid:
                    order_ids.append(oid)
                side = order.get("side", "")
                reduce = order.get("reduceOnly", order.get("tradeSide") == "close")
                if side == "buy" and not reduce:
                    buy_orders.append(order)
                elif side == "sell" and not reduce:
                    sell_orders.append(order)
            
            if order_ids:
                logger.info(f"🗑️ Cancelling {len(order_ids)} pending orders")
                await self.executor.cancel_orders(self.symbol, order_ids)
            
            # 6. Close positions at MA base (limit order)
            for pos in positions:
                contracts = float(pos.get("total", 0) or pos.get("available", 0))
                contract_size = float(pos.get("contractSize", 1))
                hold_side = pos.get("holdSide", pos.get("posSide", ""))
                
                if contracts > 0 and hold_side:
                    close_side = "sell" if hold_side == "long" else "buy"
                    close_amount = contracts * contract_size
                    logger.info(f"📉 Close {hold_side} position: {close_amount} at MA={levels['ma_base']:.2f}")
                    
                    await self.executor.place_order(
                        symbol=self.symbol,
                        side=close_side,
                        order_type="limit",
                        quantity=close_amount,
                        price=levels["ma_base"],
                        posSide=hold_side,
                        tradeSide="close",
                        marginMode="isolated"
                    )
            
            # 7. Place grid orders
            num_tiers = len(self.envelope_engine.envelopes)
            has_positions = len([p for p in positions if float(p.get("total", 0) or p.get("available", 0)) > 0]) > 0
            
            if has_positions:
                # If we have positions, only add missing envelope tiers
                first_buy_tier = num_tiers - len(buy_orders)
                first_sell_tier = num_tiers - len(sell_orders)
            else:
                # No positions: place full grid
                first_buy_tier = 0
                first_sell_tier = 0
            
            # Place BUY orders at low envelopes (long entries)
            for i in range(first_buy_tier, num_tiers):
                price = levels["low"][i]
                amount = (buying_power / num_tiers) / price
                logger.info(f"🟢 Buy order tier {i+1}: {amount:.6f} at {price:.2f}")
                await self.executor.place_order(
                    symbol=self.symbol,
                    side="buy",
                    order_type="limit",
                    quantity=amount,
                    price=price,
                    posSide="long",
                    tradeSide="open",
                    marginMode="isolated"
                )
            
            # Place SELL orders at high envelopes (short entries)
            for i in range(first_sell_tier, num_tiers):
                price = levels["high"][i]
                amount = (buying_power / num_tiers) / price
                logger.info(f"🔴 Sell order tier {i+1}: {amount:.6f} at {price:.2f}")
                await self.executor.place_order(
                    symbol=self.symbol,
                    side="sell",
                    order_type="limit",
                    quantity=amount,
                    price=price,
                    posSide="short",
                    tradeSide="open",
                    marginMode="isolated"
                )
                
            return {
                "direction": "grid_update",
                "entry": levels["ma_base"],
                "levels": levels,
                "balance": balance,
                "grid_tiers": num_tiers,
                "timestamp": datetime.now().timestamp() * 1000
            }
            
        except Exception as e:
            logger.error(f"❌ Envelope Logic Error: {e}", exc_info=True)
            return None

    async def _get_recent_ohlcv_dataframe(self) -> pd.DataFrame:
        """Fetch OHLCV candles for MA calculation using ccxt via Bitget"""
        try:
            # Format symbol for ccxt: BTC/USDT → BTC/USDT:USDT (futures)
            ccxt_symbol = self.symbol.replace("/", "").replace("USDT", "/USDT") + ":USDT"
            if not "/" in ccxt_symbol:
                # Handle BTCUSDT → BTC/USDT:USDT
                base = ccxt_symbol.replace("USDT:USDT", "")
                ccxt_symbol = f"{base}/USDT:USDT"
            
            df = await asyncio.to_thread(fetch_ohlcv, "bitget", ccxt_symbol, "1h", None, 100)
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            logger.error(f"❌ Failed to fetch OHLCV: {e}")
            return pd.DataFrame()
    
    def get_dashboard_state(self) -> Dict:
        """Export 3-tier state for dashboard."""
        # Use LTF price as canonical current_price
        current_price = 0
        if self.ltf_structure_engine.candle_history:
            current_price = self.ltf_structure_engine.candle_history[-1].close
        elif self.mtf_structure_engine.candle_history:
            current_price = self.mtf_structure_engine.candle_history[-1].close

        return {
            "symbol": self.symbol,
            "account": self.risk_engine.get_account_state(),
            "last_signal": self.last_signal,
            "active_trade": self.active_trade,
            "structure": self.ltf_structure_engine.get_current_state(),
            "liquidity": self.ltf_liquidity_engine.get_current_state(),
            "order_blocks": self.mtf_ob_engine.get_current_state(),
            "fvgs": self.mtf_fvg_engine.get_current_state(),
            "mtf_bias": self.mtf_engine.get_current_state(),
            "envelope_info": self.envelope_engine.get_current_state(),
            "signal_count": len(self.signal_history),
            "current_price": current_price,
        }

# FastAPI Setup
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict
from fastapi.responses import JSONResponse


# Import new routes
from backend.routes import stats, trades
from backend.ws.trades_ws import router as ws_router

# ════════════════════════════════════════════════════════════════════════════
# DEPRECATED: This FastAPI app is LEGACY. Use smc_futures.api_smc_engine:app
# The 'app' name is kept for backward compatibility but should NOT be used.
# Use: python run.py (which starts api_smc_engine.py)
# ════════════════════════════════════════════════════════════════════════════
def create_legacy_app(enable_startup_hooks: Optional[bool] = None) -> FastAPI:
    """Factory for the legacy orchestrator FastAPI app (no startup hooks in test mode)."""
    from backend.auth import ApiKeyMiddleware

    use_hooks = (not is_test_mode()) if enable_startup_hooks is None else enable_startup_hooks
    new_app = FastAPI(title="SmartTrade AI Backend (LEGACY - DO NOT USE)", version="2.0-deprecated")
    new_app.include_router(trades.router)
    new_app.include_router(stats.router)
    new_app.include_router(ws_router)
    for route in app.router.routes[_LEGACY_ROUTER_ROUTE_COUNT:]:
        new_app.router.routes.append(route)
    new_app.exception_handlers.update(app.exception_handlers)
    new_app.dependency_overrides.update(app.dependency_overrides)
    new_app.router.redirect_slashes = app.router.redirect_slashes
    origins = os.getenv(
        "ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173,http://localhost:8000"
    ).split(",")
    new_app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )
    new_app.add_middleware(ApiKeyMiddleware)
    if use_hooks:
        new_app.add_event_handler("startup", startup_event)
        for shutdown_fn in app.router.on_shutdown:
            new_app.add_event_handler("shutdown", shutdown_fn)
    return new_app


app = FastAPI(title="SmartTrade AI Backend (LEGACY - DO NOT USE)", version="2.0-deprecated")

# Register new routers
app.include_router(trades.router)
app.include_router(stats.router)
app.include_router(ws_router)
_LEGACY_ROUTER_ROUTE_COUNT = len(app.router.routes)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error("❌ 422 Validation error on %s", request.url.path)
    logger.error("Request body invalid: %s", exc.errors())
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "detail": exc.errors()},
    )

_ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173,http://localhost:8000"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

from backend.auth import ApiKeyMiddleware
app.add_middleware(ApiKeyMiddleware)

# WebSocket Manager registered lazily via get_app_context() when AppContext is materialized
if _app_ctx is not None:
    _app_ctx.set_ws_manager(ws_manager)


# ──────────────────────────────────────────────────
#  Background Strategy Processor (UNIFIED)
# ──────────────────────────────────────────────────

async def run_symbol_cycle(symbol: str, orch: SMCFuturesOrchestrator, loop_count: int):
    """
    Unified Pipeline Step: Runs exactly once per loop per symbol.
    Guarantees consistent execution path for ALL tokens.
    """
    mc = ctx.market_client  # BitgetV2Client
    htf_refresh = SMC_CONFIG["HTF_REFRESH_INTERVAL"]

    # ── Stale Guard: fetch live ticker and block if data is stale ──
    ticker_stale = False
    ticker = None
    try:
        ticker = await mc.get_ticker(symbol)
        if mc.is_stale(ticker):
            logger.warning(
                "[STALE_DATA] symbol=%s | last_update=%dms | signal_blocked",
                symbol, ticker["age_ms"],
            )
            ticker_stale = True
            # WS broadcast DATA_STALE → UI goes read-only
            if orch.ctx.ws_manager:
                await orch.ctx.ws_manager.broadcast({
                    "type": "DATA_STALE",
                    "symbol": symbol,
                    "age_ms": ticker["age_ms"],
                    "threshold_ms": mc.stale_threshold_ms,
                    "message": f"{symbol} price data stale ({ticker['age_ms']}ms)",
                })
        else:
            global _last_data_fetch_ts
            _last_data_fetch_ts = datetime.now(timezone.utc)
            logger.info(
                "[TICKER_PARSED] symbol=%s price=%.4f source=%s productType=%s",
                symbol, ticker["price"], ticker["source_field"],
                ticker.get("productType", "USDT-FUTURES"),
            )
    except Exception as e:
        logger.error("[STALE_DATA] symbol=%s | error=%s | signal_blocked", symbol, e)
        ticker_stale = True

    # ── Phase 4.5: Analysis Update ────────────────
    analyzer = ctx.get_analyzer(symbol)
    if ticker and "price" in ticker:
        analyzer.update(price=ticker["price"], latency_ms=0, is_stale=ticker_stale)

    # ── Stream A: HTF Context (4H) — every N loops OR on stale data ──
    # Force HTF refresh if data is stale (beyond threshold)
    now_ms = int(time.time() * 1000)
    htf_age_ms = now_ms - orch._last_htf_update_ts if orch._last_htf_update_ts > 0 else 0
    max_htf_age_ms = htf_refresh * SMC_CONFIG["LOOP_SLEEP_SECONDS"] * 1000 * 1.5
    force_htf_refresh = htf_age_ms > max_htf_age_ms
    
    if loop_count % htf_refresh == 0 or force_htf_refresh:
        refresh_reason = "FORCED_STALE" if force_htf_refresh else "SCHEDULED"
        if force_htf_refresh:
            logger.warning(
                f"[HTF_REFRESH] {symbol} | {refresh_reason}"
                f" | htf_age_sec={htf_age_ms/1000:.0f}"
                f" | threshold_sec={max_htf_age_ms/1000:.0f}"
            )
        try:
            htf_count = len(orch.htf_structure_engine.candle_history)
            orch.ctx.metrics.update_htf_progress(symbol, htf_count, SMC_CONFIG["MIN_HTF_CANDLES"]) 

            # Paginated vs Standard Fetch
            if htf_count == 0:
                htf_candles = await fetch_htf_candles_paginated(
                    symbol=symbol, tf=SMC_CONFIG["HTF_TF"], target=SMC_CONFIG["HTF_LIMIT"]
                )
            else:
                # Use at least MIN_HTF_CANDLES (300) for updates to ensure gate passes
                htf_limit = max(SMC_CONFIG["MIN_HTF_CANDLES"], 300)
                htf_candles = await fetch_htf_candles_paginated(
                    symbol=symbol, tf=SMC_CONFIG["HTF_TF"], target=htf_limit
                )
            
            if htf_candles:
                orch.process_htf_context(symbol, htf_candles)
                orch.ctx.metrics.update_htf_progress(symbol, len(orch.htf_structure_engine.candle_history), SMC_CONFIG["MIN_HTF_CANDLES"])
        except Exception as e:
            logger.error(f"❌ [HTF] Context update failed for {symbol}: {e}")

    # ── Stream B: MTF Setup (15m) ──────────────────
    try:
        mtf_candles = await get_candles(
            symbol=symbol, tf=SMC_CONFIG["SETUP_TF"], limit=min(200, SMC_CONFIG["SETUP_LIMIT"])
        )
        if mtf_candles:
            orch.process_mtf_setup(symbol, mtf_candles)
    except Exception as e:
        logger.error(f"❌ [MTF] Setup update failed for {symbol}: {e}")

    # ── Stream C: LTF Execution (5m) ───────────────
    signal = None
    if not ticker_stale:  # STALE GUARD: no signals on stale data
        try:
            ltf_candles = await get_candles(
                symbol=symbol, tf=SMC_CONFIG["ENTRY_TF"], limit=min(200, SMC_CONFIG["ENTRY_LIMIT"])
            )
            if ltf_candles:
                # UNIFORM CALL: process_ltf_execution IS the signal generation step
                signal = await orch.process_ltf_execution(symbol, ltf_candles)
        except Exception as e:
            logger.error(f"❌ [LTF] Execution cycle failed for {symbol}: {e}")
    else:
        logger.info("[STALE] %s → LTF execution SKIPPED (DATA_STALE)", symbol)

    # ── Single-Authority SMC V3 -> MT5 Execution Router ──
    # orchestrator.py is the SOLE strategic authority. Kraken already executed inside
    # process_ltf_execution(); dispatch the same canonical SMC V3 signal to MT5 (with
    # unique signal_id deduplication and MT5's own risk/lot/max-open guards).
    if signal and SMC_CONFIG.get("AUTO_EXECUTE_ENABLED", False):
        try:
            _mbe = getattr(orch.ctx, "_multi_broker_engine", None)
            if _mbe is not None and hasattr(_mbe, "dispatch_canonical_smc_signal"):
                from smc_futures.engine.multi_broker_engine import BrokerType, Signal as MBESignal, SignalType
                _dir = str(signal.get("direction", "")).strip().lower()
                _sig_type = SignalType.LONG if _dir in ("long", "buy") else (SignalType.SHORT if _dir in ("short", "sell") else SignalType.NO_SIGNAL)
                if _sig_type != SignalType.NO_SIGNAL:
                    _canon_id = f"SMCV3_{symbol}_{_dir}_{signal.get('entry')}_{signal.get('stop_loss')}_{signal.get('timestamp')}"
                    _mbe_sig = MBESignal(
                        symbol=symbol,
                        signal_type=_sig_type,
                        entry_price=float(signal.get("entry") or 0.0),
                        stop_loss=float(signal.get("stop_loss") or 0.0) or None,
                        take_profit=float(signal.get("take_profit") or 0.0) or None,
                        leverage=int(SMC_CONFIG.get("DEFAULT_LEVERAGE", 3)),
                        reason=f"SMC_V3:{signal.get('setup', 'SMC')}({signal.get('regime', '')})",
                        confidence=float(signal.get("confidence") or 0.0),
                    )
                    await _mbe.dispatch_canonical_smc_signal(
                        _mbe_sig,
                        signal_id=_canon_id,
                        brokers=[BrokerType.MT5],
                    )
        except Exception as mt5_route_err:
            logger.error(f"❌ [SMC_ROUTER->MT5] Failed routing SMC signal for {symbol}: {mt5_route_err}")

    # ── Broadcast ──────────────────────────────────
    try:
        state = orch.get_dashboard_state()
        if orch.ctx.ws_manager:
            await orch.ctx.ws_manager.broadcast({
                "type": "update", "symbol": symbol, "state": state,
            })
            if signal:
                logger.info(f"📡 Broadcasted Signal for {symbol}: {signal['direction']}")
                await orch.ctx.ws_manager.broadcast({
                    "type": "signal", "symbol": symbol, "signal": signal,
                    "timestamp": datetime.now().isoformat(),
                })
    except Exception as e:
        logger.error(f"❌ [BROADCAST] Error: {e}")


async def strategy_loop():
    """
    3-Tier Strategy Loop (V3) - Unified entry point.
    
    Handles:
      - Daily reset
      - Bitget balance refresh
      - Kraken Futures balance refresh (if enabled)
      - Small account mode
      - Symbol processing
    """
    logger.info("🚀 Starting Background Strategy Loop (V3 — Identical Paths)...")
    loop_count = 0
    sleep_sec = SMC_CONFIG["LOOP_SLEEP_SECONDS"]
    htf_refresh = SMC_CONFIG["HTF_REFRESH_INTERVAL"]
    sam = ctx.small_account_manager

    while True:
        try:
            # 1. Global Daily Reset
            ctx.order_manager.maybe_reset_daily()

            # 2. Balance Refresh — broker-mode-aware
            #    Only refresh brokers that are relevant to execution.
            #    Only push to AccountStateService with broker= tag.
            from smc_futures.risk.account_state import KRAKEN_ONLY, BITGET_ONLY
            _broker_mode = ctx.account_state.execution_broker_mode
            futures_executor = ctx.get_executor("futures")
            current_equity = 0.0

            _bitget_should_refresh = (
                _broker_mode != KRAKEN_ONLY
                and futures_executor is not None
                and loop_count % htf_refresh == 0
            )
            if _bitget_should_refresh:
                try:
                    bal_data = await futures_executor.get_account_balance(margin_coin="USDT")
                    available = 0.0
                    if isinstance(bal_data, dict):
                        available = float(bal_data.get("available", 0) or bal_data.get("crossedMaxAvailable", 0) or 0)
                        current_equity = float(bal_data.get("equity", available) or available)
                    
                    if available > 0:
                        for orch in ctx.orchestrators.values():
                            orch.smc_validator.update_balance(available)
                            orch.risk_engine.account_balance = available
                            orch.account_balance = available
                        # ── Central AccountState (broker=bitget) ──
                        upnl = current_equity - available if current_equity > available else 0.0
                        ctx.account_state.update(available, current_equity, upnl, broker="bitget")
                        ctx.order_manager.update_equity(current_equity)
                        logger.info(f"💰 [BITGET_BALANCE] Refreshed: {available:.2f} USDT (equity={current_equity:.2f})")
                except Exception as e:
                    logger.error(f"❌ [BITGET_BALANCE] Refresh failed: {e}")
                    if _analytics_ok and _AnalyticsRec:
                        try:
                            _AnalyticsRec.record_ops_event(
                                event_type="API_ERROR", severity="WARNING",
                                source="bitget_balance", message=str(e), broker="bitget",
                            )
                        except Exception:
                            pass

                # FM-10: Reconcile Positions
                try:
                    await ctx.order_manager.reconcile_positions(futures_executor, ctx.orchestrators)
                except Exception as e:
                    logger.error(f"❌ [GUARD] Reconciliation failed: {e}")
            elif _broker_mode == KRAKEN_ONLY and loop_count % htf_refresh == 0:
                logger.debug("[BALANCE] Bitget refresh SKIPPED — mode=%s", _broker_mode)

            # 2b. Kraken Futures Balance Refresh (every >=45s so AccountState never hits STALE_BALANCE > 120s)
            kraken_executor = ctx.executors.get("kraken_futures")
            _last_k_bal_ts = getattr(ctx, "_last_kraken_balance_refresh_ts", 0.0)
            _kraken_should_refresh = (
                _broker_mode != BITGET_ONLY
                and kraken_executor is not None
                and (loop_count == 0 or (time.time() - _last_k_bal_ts) >= 45.0)
            )
            if _kraken_should_refresh:
                try:
                    kraken_balance = await kraken_executor.get_account_info()
                    ctx._last_kraken_balance_refresh_ts = time.time()
                    kraken_available = kraken_balance.get("available", 0.0)
                    kraken_total = kraken_balance.get("total", 0.0)
                    kraken_pnl = kraken_balance.get("unrealizedPL", 0.0)
                    
                    # Store in context for later use
                    ctx._kraken_balance = kraken_balance
                    
                    # Propagate Kraken balance to orchestrators for sizing
                    if kraken_available > 0:
                        for orch in ctx.orchestrators.values():
                            current_bal = getattr(orch, 'account_balance', 0.0)
                            # When Kraken is the sole active crypto broker (KRAKEN_ONLY),
                            # ALWAYS overwrite orch.account_balance with real Kraken equity
                            # (never keep stale 10000.0 fallback via max()).
                            if _broker_mode == KRAKEN_ONLY or current_bal <= 0:
                                target_balance = kraken_available
                            else:
                                target_balance = max(current_bal, kraken_available)
                            if abs(target_balance - current_bal) > 1e-6:
                                orch.smc_validator.update_balance(target_balance)
                                orch.risk_engine.account_balance = target_balance
                                orch.account_balance = target_balance
                        logger.info(
                            f"💰 [KRAKEN_BALANCE] Propagated to sizing: {kraken_available:.2f} USD (mode={_broker_mode})"
                        )
                    
                    logger.info(
                        f"💰 [KRAKEN_BALANCE] available={kraken_available:.2f} USD | total={kraken_total:.2f} | pnl={kraken_pnl:.2f}"
                    )

                    # ── Central AccountState (broker=kraken) ──
                    ctx.account_state.update(kraken_available, kraken_total, kraken_pnl, broker="kraken")
                    # Update order manager equity from Kraken if it's the primary source
                    if _broker_mode == KRAKEN_ONLY or current_equity <= 0:
                        ctx.order_manager.update_equity(kraken_total)
                        current_equity = kraken_total
                    elif kraken_total > current_equity:
                        current_equity = kraken_total
                        
                except Exception as e:
                    logger.error(f"❌ [KRAKEN_BALANCE] Refresh failed: {e}")
                    ctx._kraken_balance = None
                    if _analytics_ok and _AnalyticsRec:
                        try:
                            _AnalyticsRec.record_ops_event(
                                event_type="API_ERROR", severity="WARNING",
                                source="kraken_balance", message=str(e), broker="kraken",
                            )
                        except Exception:
                            pass

            # 3. Small Account Mode: Check symbol eligibility
            eligible_symbols = None
            if sam and sam.enabled and current_equity > 0:
                prices = {}
                for symbol in ctx.orchestrators.keys():
                    try:
                        ticker = await ctx.market_client.get_ticker(symbol)
                        if ticker.get("code") == "00000" or "price" in ticker:
                            prices[symbol] = float(ticker.get("price", 0))
                    except:
                        pass
                
                eligible, ineligible, _ = sam.get_eligible_symbols(
                    equity=current_equity,
                    prices=prices,
                    all_symbols=list(ctx.orchestrators.keys()),
                )
                eligible_symbols = set(eligible)
                
                if loop_count % htf_refresh == 0:
                    logger.info(
                        f"[SMALL_ACC] equity=${current_equity:.2f}"
                        f" | eligible={eligible} | ineligible={ineligible}"
                        f" | risk={sam.get_current_risk_pct():.2f}%"
                    )

            # 4. Process EACH Symbol Uniformly
            # Use copy of values to allow safe iteration
            active_orchestrators = list(ctx.orchestrators.items())
            
            for symbol, orch in active_orchestrators:
                # Small Account Mode: Skip ineligible symbols
                if eligible_symbols is not None and symbol not in eligible_symbols:
                    if loop_count % htf_refresh == 0:
                        logger.debug(f"[SMALL_ACC] {symbol} SKIPPED (ineligible)")
                    continue
                
                await run_symbol_cycle(symbol, orch, loop_count)
                # Inter-symbol delay
                await asyncio.sleep(2)

            # 5. Real-time Dashboard Broadcast (Phase 4.5/5)
            if loop_count % 2 == 0:  # Every 2 loops (approx every 10-20s depending on SLEEP)
                try:
                    dashboard_data = {}
                    for symbol, orch in ctx.orchestrators.items():
                        dashboard_data[symbol] = orch.get_dashboard_state()
                    
                    # Also include global system metrics
                    system_metrics = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "latency": ctx.latency_tracker.get_stats(),
                        "scale_readiness": {s: ctx.scale_gate.check_readiness(s) for s in ctx.orchestrators.keys()},
                        "active_trades_count": sum(1 for orch in ctx.orchestrators.values() if orch.active_trade),
                        "om_stats": getattr(ctx.order_manager, "get_stats", lambda: {})()
                    }
                    
                    from backend.ws.manager import ws_manager
                    await ws_manager.broadcast_metrics({
                        "bots": dashboard_data,
                        "system": system_metrics
                    })
                except Exception as e:
                    logger.warning(f"⚠️ [WS_DASHBOARD] Broadcast failed: {e}")
                    if _analytics_ok and _AnalyticsRec:
                        try:
                            _AnalyticsRec.record_ops_event(
                                event_type="WS_DISCONNECT", severity="WARNING",
                                source="ws_dashboard", message=str(e),
                            )
                        except Exception:
                            pass

            # 6. System State Snapshot for Analytics (every 5 loops ≈ 30-60s)
            if loop_count % 5 == 0 and _analytics_ok and _AnalyticsRec:
                try:
                    # Collect current system state
                    risk_eng = getattr(ctx, 'risk_engine', None) or (
                        list(ctx.orchestrators.values())[0].risk_engine if ctx.orchestrators else None
                    )
                    active_count = sum(1 for o in ctx.orchestrators.values() if o.active_trade)
                    daily_pnl_sum = sum(
                        getattr(o, 'daily_pnl', 0.0) or 0.0 for o in ctx.orchestrators.values()
                    )
                    # Phase 1.9.9: Resolve real regime (including _cached_regime) and real equity
                    # from live context without ever inventing a fictitious balance or regime.
                    regime, regime_conf, snap_equity = _resolve_snapshot_regime_and_equity(
                        ctx_obj=ctx,
                        loop_equity=current_equity,
                    )

                    _AnalyticsRec.record_system_snapshot(
                        regime=regime,
                        regime_confidence=regime_conf,
                        risk_factor=getattr(risk_eng, 'risk_factor', 1.0) if risk_eng else 1.0,
                        mode=SMC_CONFIG.get("TRADING_MODE", "LIVE").upper(),
                        kill_switch_active=getattr(risk_eng, 'kill_switch_active', False) if risk_eng else False,
                        volatility_pause_active=getattr(risk_eng, 'volatility_pause_active', False) if risk_eng else False,
                        recovery_step=getattr(risk_eng, 'recovery_step', 0) if risk_eng else 0,
                        capital_utilization=active_count / max(len(ctx.orchestrators), 1) if ctx.orchestrators else 0,
                        open_positions=active_count,
                        daily_pnl=daily_pnl_sum,
                        equity=snap_equity,
                    )
                except Exception as e:
                    logger.debug(f"[ANALYTICS] Snapshot recording failed: {e}")

            loop_count += 1
            await asyncio.sleep(sleep_sec)

        except Exception as e:
            logger.error(f"❌ Strategy Loop Crash (loop_count={loop_count}): {e}")
            if _analytics_ok and _AnalyticsRec:
                try:
                    _AnalyticsRec.record_ops_event(
                        event_type="API_ERROR", severity="CRITICAL",
                        source="strategy_loop", message=f"Loop crash: {e}",
                    )
                except Exception:
                    pass
            await asyncio.sleep(10)


# --- Helper Methods ---

def _resolve_snapshot_regime_and_equity(
    ctx_obj: Any,
    loop_equity: float = 0.0,
) -> tuple[str, Optional[float], Optional[float]]:
    """
    Phase 1.9.9: Extract real observed regime, regime_confidence, and equity for
    system_state_snapshots without ever fabricating a fictitious balance or regime.

    - Regime: checks orchestrator `_cached_regime` (actual attribute set by RegimeFilter),
      `current_regime`, `htf_regime`, and `mtf_state["regime"]`. Stays "UNKNOWN" if none observed.
    - Equity: uses `loop_equity` if finite > 0; otherwise reads the latest real exchange equity
      already cached in `ctx._kraken_balance["total"]` or `ctx.account_state`. Returns None if
      no real positive finite balance is available (never falls back to static ACCOUNT_BALANCE).
    """
    import math as _math

    def _valid_pos(val: Any) -> Optional[float]:
        if val is None or isinstance(val, bool):
            return None
        try:
            f = float(val)
        except (TypeError, ValueError):
            return None
        if not _math.isfinite(f) or f <= 0:
            return None
        return f

    # 1. Resolve real observed regime
    regime = "UNKNOWN"
    regime_conf: Optional[float] = None
    orchestrators = getattr(ctx_obj, "orchestrators", None) or {}
    for o in orchestrators.values():
        r = (
            getattr(o, "_cached_regime", None)
            or getattr(o, "current_regime", None)
            or getattr(o, "htf_regime", None)
        )
        if not r:
            mtf_st = getattr(o, "mtf_state", None)
            if isinstance(mtf_st, dict):
                r = mtf_st.get("regime")
        if r and str(r).strip().upper() != "UNKNOWN":
            regime = str(r).strip()
            raw_conf = getattr(o, "regime_confidence", None)
            if raw_conf is not None and not isinstance(raw_conf, bool):
                try:
                    cf = float(raw_conf)
                    if _math.isfinite(cf):
                        regime_conf = cf
                except (TypeError, ValueError):
                    pass
            break

    # 2. Resolve real observed equity (NEVER fall back to static ACCOUNT_BALANCE)
    snap_equity = _valid_pos(loop_equity)
    if snap_equity is None:
        k_bal = getattr(ctx_obj, "_kraken_balance", None)
        if isinstance(k_bal, dict):
            snap_equity = _valid_pos(k_bal.get("total")) or _valid_pos(k_bal.get("available"))
    if snap_equity is None:
        acc_state = getattr(ctx_obj, "account_state", None)
        if acc_state is not None:
            for attr in ("get_equity", "equity", "total_equity"):
                candidate = getattr(acc_state, attr, None)
                try:
                    val = candidate() if callable(candidate) else candidate
                except Exception:
                    val = None
                snap_equity = _valid_pos(val)
                if snap_equity is not None:
                    break

    return regime, regime_conf, snap_equity


async def get_candles(symbol: str, tf: str, limit: int = 100):
    """Fetch candles via unified BitgetV2Client (Bitget Mix V2 API)."""
    try:
        return await ctx.market_client.get_candles(symbol, tf, limit)
    except Exception as e:
        logger.error("[CANDLES] %s %s fetch error: %s", symbol, tf, e)
        return []

async def fetch_htf_candles_paginated(symbol: str, tf: str, target: int = 500) -> list:
    """
    Fetch up to `target` HTF candles using proper pagination with endTime.
    Bitget V2 API limits each call to 200 candles max.
    Uses endTime pagination to fetch older candles progressively.
    """
    all_candles = []
    pages = (target + 199) // 200
    end_time = None  # Start from most recent
    
    for page in range(pages):
        try:
            # Use BitgetV2Client's paginated method or manual pagination
            if end_time:
                batch = await ctx.market_client.get_candles_with_end_time(
                    symbol, tf, 200, end_time
                )
            else:
                batch = await get_candles(symbol, tf, 200)
            
            if not batch:
                break
            
            all_candles.extend(batch)
            
            if len(batch) < 200:
                break
            
            # Get oldest timestamp for next page
            oldest_ts = min(c.get("ts") or c.get("timestamp") or 0 for c in batch)
            if oldest_ts > 0:
                end_time = oldest_ts - 1  # Go back further
            else:
                break
            
            await asyncio.sleep(0.5)  # Rate limit
            
        except Exception as e:
            logger.warning(f"[HTF_PAGINATE] Page {page} error: {e}")
            break
    
    # Dedup and sort ascending by timestamp
    seen = set()
    unique = []
    for c in all_candles:
        ts = c.get("ts") or c.get("timestamp")
        if ts and ts not in seen:
            seen.add(ts)
            unique.append(c)
    unique.sort(key=lambda x: x.get("ts") or x.get("timestamp") or 0)
    
    # Keep only the most recent `target` candles
    if len(unique) > target:
        unique = unique[-target:]
    
    logger.info(f"[HTF_PAGINATE] {symbol} {tf} | fetched={len(unique)} | target={target}")
    return unique


# ──────────────────────────────────────────────────
#  API Endpoints Fixes
# ──────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    if is_test_mode() or os.getenv("SMARTTRADE_SKIP_APP_LIFESPAN", "") == "1":
        logger.info("[BOOT_SKIP] Skipping legacy orchestrator startup_event in isolated test mode")
        return
    init_db()
    logger.info("[BOOT] orchestrator path = %s", __file__)
    logger.info("[BOOT] ctx initialized = %s", ctx is not None)
    logger.info("📡 FastAPI Startup: Initializing Background Strategy Loop...")
    
    # [NEW] Pre-init executors if environment allows
    env_key = os.getenv("BITGET_API_KEY")
    env_secret = os.getenv("BITGET_API_SECRET")
    env_pass = os.getenv("BITGET_PASSPHRASE")
    if env_key and env_secret:
        exc = BitgetFuturesExecutor(env_key, env_secret, env_pass)
        ctx.executors["futures"] = exc # Register in context
        logger.info("[BOOT] Bitget executor registered")
    
    # [NEW] Initialize orchestrators for all symbols in config
    symbols = SMC_CONFIG.get("SYMBOLS", [])
    account_balance = float(os.getenv("ACCOUNT_BALANCE", 10000.0))
    
    for symbol in symbols:
        try:
            orch = SMCFuturesOrchestrator(
                symbol=symbol,
                context=ctx,
                account_balance=account_balance,
                api_key=env_key,
                secret_key=env_secret,
                passphrase=env_pass,
                live_trading=(SMC_CONFIG.get("TRADING_MODE", "paper") == "live"),
                strategy_mode=SMC_CONFIG.get("MODE", "smc"),
                timeframe="4h"
            )
            ctx.register_orchestrator(symbol, orch)
            logger.info(f"[BOOT] Orchestrator initialized for {symbol}")
        except Exception as e:
            logger.error(f"[BOOT] Failed to initialize orchestrator for {symbol}: {e}")
        
    asyncio.create_task(strategy_loop())

@app.get("/api/bot/analysis")
async def bot_analysis():
    results = {}
    for symbol, orch in ctx.orchestrators.items():
        results[symbol] = orch.get_dashboard_state()
    return {"bots": results, "count": len(results)}

@app.get("/api/bot/metrics")
async def get_bot_metrics():
    metrics = ctx.metrics.to_dict()
    # Enrich with context info...
    return metrics # Simplified for brevity

@app.get("/api/bot/exec-stats")
async def get_exec_stats():
    """Return execution manager statistics."""
    return ctx.order_manager.get_stats()

class KillSwitchRequest(BaseModel):
    enabled: bool
    reason: str = "manual_api"

@app.post("/api/bot/kill-switch")
async def set_kill_switch(req: KillSwitchRequest):
    """Runtime kill-switch toggle."""
    if req.enabled:
        ctx.order_manager.engage_kill_switch(req.reason)
        return {"status": "killed", "reason": req.reason}
    else:
        ctx.order_manager.disengage_kill_switch()
        return {"status": "active", "msg": "Kill switch disengaged"}

class StrategyConfig(BaseModel):
    symbol: str
    account_balance: float = 10000.0
    live_trading: bool = False
    api_key: Optional[str] = None
    secret_key: Optional[str] = None
    passphrase: Optional[str] = None
    strategy_mode: str = "smc"
    timeframe: str = "4h"

@app.post("/api/smc/init")
async def initialize_strategy(config: StrategyConfig):
    normalized_symbol = config.symbol.replace("/", "").upper()
    orch = SMCFuturesOrchestrator(
        symbol=normalized_symbol,
        context=ctx, # Inject
        **config.model_dump(exclude={"symbol"})
    )
    ctx.register_orchestrator(normalized_symbol, orch)
    
    # [Brief] Trigger initial data load (simplified)
    # This logic should be similar to "stream A/B" calls in run_symbol_cycle
    # For now we assume the strategy loop picks it up in ~10s
    return {"status": "initialized", "symbol": normalized_symbol}

def init_db():
    if is_test_mode() and DB_PATH == "data/candles.db":
        raise RuntimeError(
            "SECURITY BLOCK: orchestrator.init_db() targeting data/candles.db is forbidden in test mode."
        )
    # 1. Create directory
    db_dir = os.path.dirname(DB_PATH)
    if not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    
    # 2. Create Table
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                exchange TEXT,
                symbol TEXT,
                timeframe TEXT,
                ts INTEGER,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                PRIMARY KEY (symbol, timeframe, ts)
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Failed to initialize database: {e}")

@app.get("/api/smc/state")
async def get_state(symbol: str):
    try:
        normalized_symbol = symbol.replace("/", "").upper() # Simplified normalization
        # Lazy Initialization Fallback
        if normalized_symbol not in ctx.orchestrators:
             logger.warning(f"⚠️ Lazy Initializing Orchestrator for {normalized_symbol}")
             # We need a default way to init if not present...
             # For now, return 404 to encourage explicit init via /api/smc/init
             return JSONResponse(status_code=404, content={"error": "not_found", "message": f"Strategy for {symbol} not initialized. Use /api/smc/init first."})

        return ctx.orchestrators[normalized_symbol].get_dashboard_state()
    except Exception as e:
        logger.error(f"Error in get_state: {traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": "internal_error", "message": str(e)})

# WebSocket Endpoint moved to api_smc_engine.py / backend/ws/trades_ws.py


# ──────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────

def normalize_symbol(symbol: str) -> str:
    """Normalize BTC/USDT or BTC:USDT to BTCUSDT"""
    return symbol.replace("/", "").replace(":", "").replace("-", "").upper()

TF_MAP_V2 = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "4H": "4H",
    "6h": "6H", "12h": "12H", "1d": "1D",
}

def tf_to_ms(tf: str) -> int:
    """Convert timeframe string to milliseconds"""
    t = tf.lower()
    if t.endswith("m"):
        return int(t[:-1]) * 60_000
    if t.endswith("h"):
        return int(t[:-1]) * 3_600_000
    if t.endswith("d"):
        return int(t[:-1]) * 86_400_000
    raise ValueError(f"Unsupported tf: {tf}")


def _get_executor():
    """Resolve the Bitget executor from context."""
    exc = ctx.get_executor("futures")
    if not exc:
        env_key = os.getenv("BITGET_API_KEY", "")
        env_secret = os.getenv("BITGET_API_SECRET", "")
        env_pass = os.getenv("BITGET_PASSPHRASE", "")
        if env_key and env_secret:
            exc = BitgetFuturesExecutor(env_key, env_secret, env_pass)
            ctx.executors["futures"] = exc
    return exc


def _is_multi_broker_enabled() -> bool:
    """Check if multi-broker execution is enabled."""
    return (
        os.getenv("MULTI_BROKER_MIRROR", "0").lower() in ("1", "true", "yes") or
        os.getenv("DUAL_EXECUTION", "0").lower() in ("1", "true", "yes") or
        os.getenv("MULTI_BROKER_EXECUTION", "0").lower() in ("1", "true", "yes")
    )


def _get_multi_executor():
    """
    Get MultiOrderExecutor for parallel broker execution.
    
    Used when MULTI_BROKER_MIRROR=true or DUAL_EXECUTION=true.
    Falls back to single Bitget executor if Kraken not available.
    """
    if not _is_multi_broker_enabled():
        return _get_executor()
    
    try:
        from backend.services.multi_order_executor import get_multi_order_executor
        
        multi_exec = get_multi_order_executor()
        
        # Ensure Bitget executor is registered
        bitget_exec = _get_executor()
        if bitget_exec and not multi_exec.is_broker_enabled("bitget"):
            multi_exec.set_bitget_executor(bitget_exec)
            logger.info("[MULTI_EXEC] Bitget executor registered")
        
        # Try to register Kraken executor if available
        kraken_enabled = os.getenv("KRAKEN_ENABLED", "0").lower() in ("1", "true", "yes")
        if kraken_enabled and not multi_exec.is_broker_enabled("kraken"):
            try:
                kraken_exec = ctx.executors.get("kraken_futures")
                if not kraken_exec:
                    # Try to create Kraken executor
                    from smc_futures.engine.execution.kraken_executor import KrakenFuturesExecutor
                    kraken_key = os.getenv("KRAKEN_API_KEY", "")
                    kraken_secret = os.getenv("KRAKEN_API_SECRET", "")
                    if kraken_key and kraken_secret:
                        kraken_exec = KrakenFuturesExecutor(kraken_key, kraken_secret)
                        ctx.executors["kraken_futures"] = kraken_exec
                
                if kraken_exec:
                    multi_exec.set_kraken_executor(kraken_exec)
                    logger.info("[MULTI_EXEC] Kraken executor registered")
            except Exception as e:
                logger.warning("[MULTI_EXEC] Failed to register Kraken: %s", e)
        
        enabled_brokers = multi_exec.get_enabled_brokers()
        logger.debug("[MULTI_EXEC] Active brokers: %s", enabled_brokers)
        
        return multi_exec
        
    except ImportError as e:
        logger.warning("[MULTI_EXEC] MultiOrderExecutor not available: %s", e)
        return _get_executor()


# ──────────────────────────────────────────────────
#  GET /api/health
# ──────────────────────────────────────────────────

@app.get("/api/health")
async def api_health():
    routes = [r.path for r in app.routes]
    
    # Check multi-broker status
    multi_broker_enabled = _is_multi_broker_enabled()
    kraken_enabled = os.getenv("KRAKEN_ENABLED", "0").lower() in ("1", "true", "yes")
    
    return {
        "status": "ok",
        "service": "smc-trader",
        "version": "v3",
        "ws": "/ws",
        "routes_count": len(routes),
        "orchestrators": list(ctx.orchestrators.keys()),
        "executor_ready": ctx.get_executor("futures") is not None,
        "trading_mode": SMC_CONFIG.get("TRADING_MODE", "paper"),
        "price_source": SMC_CONFIG.get("PRICE_SOURCE", "mark"),
        "multi_broker_enabled": multi_broker_enabled,
        "kraken_enabled": kraken_enabled,
    }


# ──────────────────────────────────────────────────
#  GET /api/v1/market/ticker
# ──────────────────────────────────────────────────

@app.get("/api/v1/market/ticker")
async def get_ticker(symbol: str, productType: str = "USDT-FUTURES"):
    """Parsed ticker via BitgetV2Client (mark/last/index + stale guard)."""
    try:
        ticker = await ctx.market_client.get_ticker(symbol, productType)
        return ticker
    except Exception as e:
        logger.error(f"❌ ticker error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ──────────────────────────────────────────────────
#  GET /api/smc/market/candles  (smart cache)
# ──────────────────────────────────────────────────

@app.get("/api/smc/market/candles")
async def smc_market_candles(
    symbol: str = Query(...),
    tf: str = Query("5m"),
    limit: int = Query(200, ge=10, le=1000),
):
    """Return OHLCV candles — read-through SQLite cache + Bitget live fallback."""
    # 1. Normalize tf
    mapped_tf = TF_MAP_V2.get(tf, TF_MAP_V2.get(tf.lower(), tf))
    clean_symbol = normalize_symbol(symbol)

    # 2. Try SQLite cache first
    if os.path.exists(DB_PATH):
        try:
            def db_check():
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("SELECT MAX(ts) FROM candles WHERE symbol=? AND timeframe=?",
                            (clean_symbol, tf))
                row = cur.fetchone()
                max_ts = row[0] if row else None

                fetch_required = True
                data = None

                if max_ts:
                    now_ms = int(time.time() * 1000)
                    interval_ms = tf_to_ms(tf)
                    if (now_ms - max_ts) < (interval_ms * 2):
                        fetch_required = False

                if not fetch_required:
                    cur.execute("""
                        SELECT ts, open, high, low, close, volume
                        FROM candles WHERE symbol=? AND timeframe=?
                        ORDER BY ts DESC LIMIT ?
                    """, (clean_symbol, tf, limit))
                    rows = cur.fetchall()
                    if rows:
                        rows.reverse()
                        data = [
                            {"ts": r[0], "open": r[1], "high": r[2],
                             "low": r[3], "close": r[4], "volume": r[5]}
                            for r in rows
                        ]
                conn.close()
                return fetch_required, data

            need_fetch, cached = await asyncio.to_thread(db_check)
            if not need_fetch and cached:
                return cached
        except Exception as e:
            logger.warning(f"⚠️ SQLite cache read error: {e}")

    # 3. Live fetch from Bitget
    logger.info(f"🌐 Fetching live candles for {clean_symbol} ({tf})")
    url = f"{BITGET_BASE}/api/v2/mix/market/candles"
    params = {
        "symbol": clean_symbol,
        "productType": "usdt-futures",
        "granularity": mapped_tf,
        "limit": str(min(limit, 200)),
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)

        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Bitget error: {r.text}")

        body = r.json()
        if body.get("code") != "00000":
            raise HTTPException(status_code=502, detail=f"Bitget: {body.get('msg')}")

        raw = body.get("data", [])
        out = []
        db_rows = []
        for row in raw:
            ts = int(row[0])
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            v = float(row[5]) if len(row) > 5 else 0.0
            out.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
            db_rows.append(("bitget", clean_symbol, tf, ts, o, h, l, c, v))

        out.sort(key=lambda x: x["ts"])

        # Upsert into SQLite
        if db_rows:
            def db_upsert():
                conn = sqlite3.connect(DB_PATH)
                conn.executemany("""
                    INSERT OR REPLACE INTO candles
                    (exchange, symbol, timeframe, ts, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, db_rows)
                conn.commit()
                conn.close()
            await asyncio.to_thread(db_upsert)
            logger.info(f"💾 Cached {len(db_rows)} candles for {clean_symbol}")

        return out

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ candles fetch error: {e}")
        return []


# ──────────────────────────────────────────────────
#  GET /api/v1/positions
# ──────────────────────────────────────────────────

@app.get("/api/v1/positions")
async def get_positions(symbol: Optional[str] = None):
    """
    Get open positions via Bitget executor.
    
    Returns structured response:
    {
        "code": "00000",
        "count": N,
        "positions": [...],
        "source": "bitget_v2",
        "productType": "USDT-FUTURES",
        "marginCoin": "USDT"
    }
    """
    executor = _get_executor()
    if not executor:
        return JSONResponse(status_code=503, content={
            "error": "no_executor",
            "message": "Bitget API keys not configured",
            "count": 0,
            "positions": [],
            "source": "none"
        })
    try:
        # Use get_positions (with fallback to get_open_positions for compat)
        if hasattr(executor, "get_positions"):
            result = await executor.get_positions(symbol)
        elif hasattr(executor, "get_open_positions"):
            result = await executor.get_open_positions(symbol)
        else:
            return JSONResponse(status_code=501, content={
                "error": "not_implemented",
                "message": "Executor has no get_positions method",
                "count": 0,
                "positions": [],
                "source": "none"
            })
        
        # Ensure response has expected structure
        if isinstance(result, dict):
            # Add count if missing
            if "count" not in result:
                positions = result.get("positions", result.get("data", []))
                result["count"] = len(positions) if isinstance(positions, list) else 0
            # Add source if missing
            if "source" not in result:
                result["source"] = "bitget_v2"
            # Ensure positions key exists
            if "positions" not in result and "data" in result:
                result["positions"] = result["data"]
        
        return result
        
    except Exception as e:
        logger.error(f"❌ positions error: {traceback.format_exc()}")
        return JSONResponse(status_code=500, content={
            "error": "internal_error",
            "route": "/api/v1/positions",
            "message": str(e),
            "count": 0,
            "positions": [],
            "source": "error"
        })


# ──────────────────────────────────────────────────
#  GET /api/v1/account/futures
# ──────────────────────────────────────────────────

@app.get("/api/v1/account/futures")
async def get_futures_balance(marginCoin: str = "USDT"):
    executor = _get_executor()
    if not executor:
        return JSONResponse(status_code=503, content={"error": "no_executor",
            "message": "Bitget API keys not configured"})
    try:
        return await executor.get_account_balance(marginCoin)
    except Exception as e:
        logger.error(f"❌ balance error: {traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": "internal_error",
            "route": "/api/v1/account/futures", "message": str(e)})


# ──────────────────────────────────────────────────
#  GET /api/v1/trades/history
# ──────────────────────────────────────────────────

@app.get("/api/v1/trades/history")
async def get_trade_history(symbol: str, limit: int = 50):
    executor = _get_executor()
    if not executor:
        return JSONResponse(status_code=503, content={"error": "no_executor",
            "message": "Bitget API keys not configured"})
    try:
        return await executor.get_trade_history(symbol, limit)
    except Exception as e:
        logger.error(f"❌ trade-history error: {traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": "internal_error",
            "route": "/api/v1/trades/history", "message": str(e)})


# ──────────────────────────────────────────────────
#  POST /api/v2/order/dry-run  (risk sizing, no Bitget calls)
# ──────────────────────────────────────────────────




# DryRunOrderRequest imported from smc_futures.schemas.orders

class KillSwitchRequest(BaseModel):
    enabled: bool
    reason: str = "manual_override"
@app.post("/api/v2/order/dry-run")
async def dry_run_order(req: DryRunOrderRequest):
    """
    Pure-computation dry-run: calculate position size from risk parameters.
    ZERO Bitget API calls.  Works in any mode (shadow / paper / live).
    """
    import uuid as _uuid

    symbol = normalize_symbol(req.symbol)
    logger.info(
        "[DRY_RUN] REQUEST symbol=%s side=%s entry=%.4f sl=%.4f tp=%.4f risk=%.2f%% lev=%dx",
        symbol, req.side, req.entry_price, req.stop_loss, req.take_profit,
        req.risk_pct, req.leverage,
    )

    # ── Validate inputs ──────────────────────────
    if req.entry_price <= 0 or req.stop_loss <= 0:
        return JSONResponse(status_code=400, content={
            "ok": False, "reason": "entry_price and stop_loss must be > 0",
        })

    sl_distance = abs(req.entry_price - req.stop_loss)
    if sl_distance == 0:
        return JSONResponse(status_code=400, content={
            "ok": False, "reason": "stop_loss cannot equal entry_price",
        })

    # ── Account balance ──────────────────────────
    # Try refreshed balance from any active orchestrator, fallback to config
    account_balance = 0.0
    for orch in ctx.orchestrators.values():
        if orch.account_balance > 0:
            account_balance = orch.account_balance
            break
    if account_balance <= 0:
        account_balance = float(os.getenv("BOT_ACCOUNT_BALANCE", "10000"))

    # ── Risk sizing ──────────────────────────────
    risk_amount = account_balance * (req.risk_pct / 100.0)
    raw_size = (risk_amount * req.leverage) / sl_distance

    # ── Symbol rules (cached, no HTTP) ───────────
    qty_step = 0.001
    min_qty = 0.001
    executor = ctx.get_executor("futures")
    if executor and hasattr(executor, "_rules_cache"):
        cached = executor._rules_cache.get(symbol, {})
        qty_step = cached.get("qty_step", 0.001)
        min_qty = cached.get("min_qty", 0.001)

    # Round to step
    from smc_futures.engine.execution.bitget_executor import BitgetFuturesExecutor
    size_str = BitgetFuturesExecutor.round_step(raw_size, qty_step)
    final_size = float(size_str)

    if final_size < min_qty:
        logger.warning("[DRY_RUN] REJECT size=%.6f < min_qty=%.6f", final_size, min_qty)
        return {
            "ok": False,
            "reason": f"Computed size {final_size} below minimum {min_qty}",
            "payloadPreview": {
                "symbol": symbol,
                "side": req.side,
                "size": final_size,
                "stopLoss": req.stop_loss,
                "takeProfit": req.take_profit,
            },
        }

    # ── Build preview ────────────────────────────
    payload = {
        "symbol": symbol,
        "side": req.side,
        "posSide": req.pos_side,
        "size": final_size,
        "sizeStr": size_str,
        "stopLoss": req.stop_loss,
        "takeProfit": req.take_profit,
        "entry": req.entry_price,
        "leverage": req.leverage,
        "productType": "USDT-FUTURES",
        "marginMode": "isolated",
        "tradeSide": "open",
        "orderType": "market",
    }

    logger.info(
        "[DRY_RUN] OK symbol=%s side=%s size=%s sl=%.4f tp=%.4f balance=%.2f",
        symbol, req.side, size_str, req.stop_loss, req.take_profit, account_balance,
    )

    return {
        "ok": True,
        "orderId": f"DRYRUN-{_uuid.uuid4().hex[:12].upper()}",
        "status": "simulated",
        "payloadPreview": payload,
        "riskCalc": {
            "accountBalance": account_balance,
            "riskPct": req.risk_pct,
            "riskAmount": round(risk_amount, 2),
            "slDistance": round(sl_distance, 6),
            "rawSize": round(raw_size, 8),
            "qtyStep": qty_step,
            "minQty": min_qty,
        },
    }




# ──────────────────────────────────────────────────
#  POST /api/v1/order
# ──────────────────────────────────────────────────

# OrderRequest imported from smc_futures.schemas.orders

@app.post("/api/v1/order")
async def place_order_endpoint(req: OrderRequest):
    # 1. Global Checks
    if not SMC_CONFIG["AUTO_EXECUTE_ENABLED"] and not SMC_CONFIG["SHADOW_MODE"]:
         return JSONResponse(status_code=403, content={
            "error": "auto_execute_disabled",
            "message": "AUTO_EXECUTE is disabled in config.",
        })

    # 2. Shadow Mode -> ZERO network calls
    if SMC_CONFIG["SHADOW_MODE"]:
        import uuid
        fake_id = f"SHADOW-{uuid.uuid4().hex[:12].upper()}"
        logger.info("[DRY_RUN] WOULD_SEND symbol=%s side=%s qty=%s", req.symbol, req.side, req.quantity or req.size)
        return {
            "success": True,
            "orderId": fake_id,
            "status": "shadow",
            "message": "WOULD_SEND — no real order placed",
            "fillPrice": req.price or 0.0,
        }

    # 3. Mode check: true -> proceed to executor (multi-broker if enabled)
    executor = _get_multi_executor()  # Uses multi-broker if MULTI_BROKER_MIRROR=true
    if not executor:
        return JSONResponse(status_code=503, content={"error": "no_executor",
            "message": "No executor available (Bitget keys not configured)"})

    # Resolve aliases
    final_qty = req.quantity or req.size
    final_sl = req.stopLoss or req.stop_loss
    final_tp = req.takeProfit or req.take_profit
    final_order_type = req.orderType or req.order_type or "market"

    if not final_qty or final_qty <= 0:
        return JSONResponse(status_code=400, content={
            "error": "missing_quantity",
            "message": "quantity or size is required and must be > 0",
        })

    try:
        result = await executor.place_order(
            symbol=req.symbol, direction=req.side, order_type=final_order_type,
            quantity=str(final_qty), entry_price=req.price or 0.0,
            stop_loss=final_sl or 0.0, take_profit=final_tp or 0.0,
            productType=req.productType, posSide=req.posSide,
            marginMode=req.marginMode or "isolated", tradeSide=req.tradeSide,
        )
        return result
    except Exception as e:
        logger.error(f"💥 order error: {traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": "internal_error",
            "route": "/api/v1/order", "message": str(e)})


# ──────────────────────────────────────────────────
#  GET /api/v1/health  (UI compat alias)
# ──────────────────────────────────────────────────

@app.get("/api/v1/health")
async def api_v1_health():
    """Alias for /api/health — fixes UI 'BACKEND OFFLINE' status."""
    om = ctx.order_manager
    return {
        "status": "ok",
        "service": "smc-engine",
        "auto_execute": om.auto_execute_enabled if om else False,
        "shadow_mode": om.shadow_mode if om else False,
        "trading_mode": om.trading_mode if om else "unknown",
        "kill_switch": om.kill_switch_active if om else False,
        "ws": "/ws",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.post("/api/bot/kill-switch")
async def kill_switch_endpoint(req: KillSwitchRequest):
    om = ctx.order_manager
    if not om:
        return JSONResponse(status_code=503, content={"error": "order_manager_not_ready"})
    
    if req.enabled:
        om.engage_kill_switch(req.reason)
        msg = f"Kill-switch ENGAGED: {req.reason}"
    else:
        om.disengage_kill_switch()
        msg = "Kill-switch DISENGAGED"
    
    return {
        "ok": True, 
        "message": msg,
        "kill_switch": om.kill_switch_active,
        "reason": om.kill_switch_reason
    }


# ──────────────────────────────────────────────────
#  GET /api/status  (trading state for UI)
# ──────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    """Full trading status for UI banner / connectivity check."""
    om = ctx.order_manager
    sam = ctx.small_account_manager
    
    status_data = {
        "status": "ok",
        "service": "smc-engine",
        "mode": SMC_CONFIG.get("TRADING_MODE", "paper"),
        "auto_execute": SMC_CONFIG.get("AUTO_EXECUTE_ENABLED", False),
        "shadow_mode": SMC_CONFIG.get("SHADOW_MODE", False),
        "auto_execute_mode": SMC_CONFIG.get("AUTO_EXECUTE_MODE", "false"),
        "force_trade_enabled": SMC_CONFIG.get("FORCE_TRADE_ENABLED", False),
        "price_source": SMC_CONFIG.get("PRICE_SOURCE", "mark"),
        "stale_threshold_ms": SMC_CONFIG.get("STALE_THRESHOLD_MS", 10_000),
        "ws_connected": ctx.ws_manager is not None,
        "executor_ready": ctx.get_executor("futures") is not None,
        "symbols": list(ctx.orchestrators.keys()),
    }

    if om:
        status_data["protection_status"] = {
            "kill_switch": om.kill_switch_active,
            "kill_reason": om.kill_switch_reason,
            "daily_limit_reached": (om._trades_today >= om.max_trades_per_day),
            "trades_today": om._trades_today,
            "daily_pnl": om._daily_pnl,
        }
        status_data["config"] = {
            "max_trades_day": om.max_trades_per_day,
            "cooldown_sec": om.cooldown_sec,
            "min_rr": om.min_rr,
            "risk_pct": om.risk_per_trade_pct,
            "leverage": om.default_leverage,
        }
    else:
        status_data["protection_status"] = "offline"
        status_data["config"] = "offline"
    
    # Small Account Mode info
    status_data["small_account_mode"] = {
        "enabled": sam.enabled if sam else False,
        "current_risk_pct": sam.get_current_risk_pct() if sam else None,
        "disabled_symbols": sam.get_disabled_symbols() if sam else [],
    }

    return status_data


@app.get("/api/v1/status")
async def api_v1_status():
    """
    Extended status endpoint for small account mode.
    Includes symbol eligibility and equity thresholds.
    """
    om = ctx.order_manager
    sam = ctx.small_account_manager
    executor = ctx.get_executor("futures")
    
    # Get current equity
    equity = 0.0
    if executor:
        try:
            balance_resp = await executor.get_account_balance("USDT")
            if balance_resp.get("code") == "00000":
                data = balance_resp.get("data", [{}])
                if data:
                    equity = float(data[0].get("equity", data[0].get("available", 0)))
        except Exception as e:
            logger.warning(f"[STATUS] Failed to get equity: {e}")
    
    # Get current prices for eligibility calculation
    prices = {}
    symbols = SMC_CONFIG.get("SYMBOLS", [])
    for symbol in symbols:
        try:
            ticker = await ctx.market_client.get_ticker(symbol)
            if ticker.get("code") == "00000":
                data = ticker.get("data", [{}])
                if data:
                    prices[symbol] = float(data[0].get("lastPr", 0))
        except:
            pass
    
    # Build response
    response = {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "equity": round(equity, 2),
    }
    
    # Small account mode details
    if sam and sam.enabled:
        sam_status = sam.get_status(equity, prices, symbols)
        response["small_account"] = sam_status.to_dict()
    else:
        response["small_account"] = {"enabled": False}
    
    # Protection status
    if om:
        response["protection"] = {
            "kill_switch": om.kill_switch_active,
            "trades_today": om._trades_today,
            "max_trades_day": om.max_trades_per_day,
            "daily_pnl": round(om._daily_pnl, 2),
            "cooldown_remaining": sam.get_cooldown_remaining() if sam else 0,
        }
    
    # Active positions summary
    response["symbols"] = {
        "configured": symbols,
        "active_orchestrators": list(ctx.orchestrators.keys()),
        "prices": {s: round(p, 4) for s, p in prices.items()},
    }
    
    return response


# ──────────────────────────────────────────────────
#  POST /api/test/force-order  (shadow / paper / live)
# ──────────────────────────────────────────────────

# In-memory daily trade counter for live mode protection
_force_order_daily: Dict[str, int] = {}
_force_order_day: str = ""


# ForceOrderRequest imported from smc_futures.schemas.orders


@app.post("/api/test/force-order")
async def test_force_order(req: ForceOrderRequest):
    """
    Test endpoint for automation.
    - shadow: log WOULD_SEND, return simulated payload
    - paper:  simulate in-memory, return fake orderId
    - live:   send to Bitget (DOUBLE-LOCKED)
    """
    import uuid
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    logger.info(
        "[TEST_FORCE] request: symbol=%s side=%s type=%s size=%.4f mode=%s",
        req.symbol, req.side, req.order_type, req.size, req.mode,
    )

    payload = {
        "symbol": req.symbol,
        "side": req.side,
        "orderType": req.order_type,
        "size": req.size,
        "price": req.price,
        "productType": "USDT-FUTURES",
        "marginMode": "isolated",
        "tradeSide": "open",
    }

    # ─── SHADOW MODE ──────────────────────────
    if req.mode == "shadow":
        logger.info("[ORDER] WOULD_SEND %s %s %.4f %s", req.symbol, req.side, req.size, req.order_type)
        return {
            "status": "shadow",
            "message": "WOULD_SEND — no order placed",
            "payload": payload,
            "timestamp": now_iso,
        }

    # ─── PAPER MODE ───────────────────────────
    if req.mode == "paper":
        fake_id = f"PAPER-{uuid.uuid4().hex[:12].upper()}"
        logger.info("[ORDER] PAPER_FILL orderId=%s %s %s %.4f", fake_id, req.symbol, req.side, req.size)
        return {
            "status": "paper",
            "orderId": fake_id,
            "message": "Paper order simulated",
            "payload": payload,
            "timestamp": now_iso,
        }

    # ─── LIVE MODE (DOUBLE-LOCKED) ────────────
    if req.mode == "live":
        # Lock 1: Config variable
        if not SMC_CONFIG.get("FORCE_TRADE_ENABLED", False):
            logger.warning("[TEST_FORCE] LIVE BLOCKED — FORCE_TRADE_ENABLED=false")
            return JSONResponse(status_code=403, content={
                "error": "live_disabled",
                "message": "Set FORCE_TRADE_ENABLED=true in .env to enable live force-orders",
            })

        # Lock 2: Risk limit
        risk_pct = float(os.getenv("RISK_PCT", "0.01"))
        if risk_pct > 0.1:
            logger.warning("[TEST_FORCE] LIVE BLOCKED — RISK_PCT=%.2f > 0.1", risk_pct)
            return JSONResponse(status_code=403, content={
                "error": "risk_too_high",
                "message": f"RISK_PCT={risk_pct} exceeds max 0.1 for force-orders",
            })

        # Lock 3: Daily trade limit
        global _force_order_day, _force_order_daily
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        max_daily = int(os.getenv("MAX_TRADES_PER_DAY", "1"))
        if _force_order_day != today:
            _force_order_day = today
            _force_order_daily = {}
        daily_count = _force_order_daily.get(req.symbol, 0)
        if daily_count >= max_daily:
            logger.warning("[TEST_FORCE] LIVE BLOCKED — daily limit %d/%d for %s",
                           daily_count, max_daily, req.symbol)
            return JSONResponse(status_code=429, content={
                "error": "daily_limit",
                "message": f"Max {max_daily} live force-orders/day for {req.symbol} (used {daily_count})",
            })

        # Execute (multi-broker if enabled)
        executor = _get_multi_executor()  # Uses multi-broker if MULTI_BROKER_MIRROR=true
        if not executor:
            return JSONResponse(status_code=503, content={
                "error": "no_executor",
                "message": "No executor available (API keys not configured)",
            })

        try:
            logger.info("[ORDER] SENDING LIVE %s %s %.4f (multi_broker=%s)", 
                        req.symbol, req.side, req.size, _is_multi_broker_enabled())
            result = await executor.place_order(
                symbol=req.symbol,
                side=req.side,
                order_type=req.order_type,
                quantity=req.size,
                price=req.price,
                productType="USDT-FUTURES",
                marginMode="isolated",
                tradeSide="open",
            )
            _force_order_daily[req.symbol] = daily_count + 1
            logger.info("[EXEC] result=%s", result)
            return {
                "status": "live",
                "result": result,
                "daily_used": daily_count + 1,
                "daily_max": max_daily,
                "timestamp": now_iso,
            }
        except Exception as e:
            logger.error("[EXEC] LIVE ERROR: %s", traceback.format_exc())
            return JSONResponse(status_code=500, content={
                "error": "execution_error",
                "message": str(e),
            })

    # Unknown mode
    return JSONResponse(status_code=400, content={
        "error": "invalid_mode",
        "message": f"mode must be shadow|paper|live, got '{req.mode}'",
    })


# ──────────────────────────────────────────────────
#  POST /api/test/force-trade  (alias → force-order)
# ──────────────────────────────────────────────────

@app.post("/api/test/force-trade")
async def test_force_trade_alias(req: ForceOrderRequest):
    """Alias for /api/test/force-order — matches frontend/smarttrade_api expectations."""
    return await test_force_order(req)
