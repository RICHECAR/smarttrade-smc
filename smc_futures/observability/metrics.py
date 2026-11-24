import os
from prometheus_client import Counter, Histogram, Gauge, start_http_server

# ---------------------------------------------------------------------------
# DEFINITION DES METRIQUES PROMETHEUS
# ---------------------------------------------------------------------------

# Compteurs Globaux
TRADES_TOTAL = Counter('trades_total', 'Total number of trade intents processed', ['exchange', 'symbol', 'side'])
REJECTS_TOTAL = Counter('rejects_total', 'Total number of rejected trades', ['reason'])
PARTIAL_EXEC_TOTAL = Counter('partial_exec_total', 'Total number of partial execution failures caught by OMS')
ROLLBACK_TOTAL = Counter('rollback_total', 'Total number of rollbacks executed')

# Phase 4 Asymmetric Metrics
PRIMARY_TIMEOUT_TOTAL = Counter('primary_timeout_total', 'Total number of primary maker timeouts')
LATE_FILL_TOTAL = Counter('late_fill_total', 'Fills received after successful cancellation')
ORPHAN_DETECTED_TOTAL = Counter('orphan_detected_total', 'Secondary hedge rejected leaving primary naked')
ASYMMETRIC_COMPLETED_TOTAL = Counter('asymmetric_completed_total', 'Successfully hedged asymmetric execution')
ASYMMETRIC_FAILED_SAFE_TOTAL = Counter('asymmetric_failed_safe_total', 'Asymmetric execution aborted safely without orphan')

# Latences (En secondes: on track les ms en divisant)
LATENCY_P2P = Histogram('latency_p2p_seconds', 'End-to-End latency from signal to execution')
LATENCY_OMS_DISPATCH = Histogram('latency_oms_dispatch_seconds', 'Latency of concurrent network dispatches in OMS')
SECONDARY_HEDGE_LATENCY = Histogram('secondary_hedge_latency_seconds', 'Latency between Primary Fill and Secondary Hedge Market')
HEDGE_SLIPPAGE_BPS = Histogram('hedge_slippage_bps', 'Slippage distance between Maker limit and Taker hedge execution')

# Santé WebSocket / Desyncs
WS_DESYNC_COUNT = Counter('ws_desync_count', 'Total times the WS Reconciler caught a DB vs Exchange Desync')

# Alerting Gauges (1 = En alerte, 0 = OK)
STALE_TICK_GAUGE = Gauge('stale_tick_alert', 'Alerts if consecutive stale ticks exceed threshold')
PNL_DRAWDOWN_GAUGE = Gauge('pnl_drawdown_alert', 'Alerts if daily loss exceeds limit')
EXECUTION_MODE_ACTIVE = Gauge('execution_mode_active', 'Currently active execution mode (1=active)', ['mode'])
COOLDOWN_ACTIVE = Gauge('cooldown_active', 'Is the post-incident cooldown currently active (1=yes)')
# Phase 4.5 Institutional Metrics
VOLATILITY_SIGMA = Gauge('volatility_sigma', 'Rolling standard deviation of log returns', ['symbol'])
STALE_REJECTION_RATE = Gauge('stale_rejection_rate', 'Ratio of stale rejections to total intents', ['symbol'])
LATENCY_HISTOGRAM = Histogram(
    'latency_bucket_ms', 
    'Execution latency buckets', 
    ['type'], 
    buckets=[10, 25, 50, 100, 250, 500, 1000, 2000, 5000]
)
LATENCY_P95_GAUGE = Gauge('latency_p95_ms', 'P95 execution latency in milliseconds', ['symbol'])
LATENCY_P99_GAUGE = Gauge('latency_p99_ms', 'P99 execution latency in milliseconds', ['symbol'])

# Phase 4.5 Correlation & Regression Metrics
CORRELATION_PEARSON = Gauge('correlation_pearson', 'Pearson correlation between volatility and stale rate', ['symbol'])
CORRELATION_SPEARMAN = Gauge('correlation_spearman', 'Spearman correlation between volatility and stale rate', ['symbol'])
REGRESSION_SLOPE = Gauge('regression_slope', 'Slope of stale_rate ~ sigma regression', ['symbol'])
REGRESSION_R2 = Gauge('regression_r2', 'R-squared of stale_rate ~ sigma regression', ['symbol'])
LATENCY_BURST_GAUGE = Gauge('latency_burst_active', '1 if a latency burst is detected', ['symbol'])

# Phase 5 Adaptive Capital Metrics
CAPITAL_ALLOCATED_GAUGE = Gauge('capital_allocated_pct', 'Current capital allocation percentage', ['symbol'])
RISK_MULTIPLIER_GAUGE = Gauge('risk_multiplier', 'Dynamic risk multiplier based on execution quality', ['symbol'])
CAPITAL_REGIME_GAUGE = Gauge('capital_regime', 'Current capital regime (0=FROZEN, 1=MICRO, 2=NORMAL, 3=EXPANSION, 4=RETRACTION)', ['symbol'])
EXECUTION_QUALITY_SCORE = Gauge('execution_quality_score', 'Aggregated execution quality score (0.0-1.0)', ['symbol'])
# Convenience aliases for global (non-per-symbol) access
GATE_STATUS = Gauge('gate_status', 'Current Scale Gate status (1=GREEN, 2=AMBER, 3=RED)', ['symbol'])
SCALE_GATE_STATUS = Gauge('scale_gate_status', 'Current Scale Gate status (0=GREEN, 1=AMBER, 2=RED)', ['symbol']) # Legacy


# Global Exposure Guardian Metrics
EXPOSURE_TOTAL_USD = Gauge('exposure_total_usd', 'Total gross exposure across all positions (USD)')
EXPOSURE_NET_USD = Gauge('exposure_net_usd', 'Net directional exposure across all positions (USD)')
EXPOSURE_BY_SYMBOL_USD = Gauge('exposure_by_symbol_usd', 'Exposure per symbol (USD)', ['symbol'])
EXPOSURE_BY_BROKER_USD = Gauge('exposure_by_broker_usd', 'Exposure per broker (USD)', ['broker'])
EXPOSURE_GUARD_BLOCK_TOTAL = Counter('exposure_guard_block_total', 'Total number of blocks by global exposure guardian', ['reason'])
EXPOSURE_GUARD_THROTTLE_TOTAL = Counter('exposure_guard_throttle_total', 'Total number of throttle advisories by global exposure guardian', ['reason'])

STRESS_SIM_BLOCK_TOTAL = Counter('stress_sim_block_total', 'Total number of blocks by capital stress simulator', ['reason'])
STRESS_SIM_THROTTLE_TOTAL = Counter('stress_sim_throttle_total', 'Total number of throttle advisories from capital stress simulator', ['reason'])
STRESS_SIM_PROJECTED_LOSS_USD = Gauge('stress_sim_projected_loss_usd', 'Projected stress loss in USD', ['symbol'])
STRESS_SIM_PROJECTED_DD_PCT = Gauge('stress_sim_projected_dd_pct', 'Projected stress drawdown in pct', ['symbol'])
STRESS_SIM_PROJECTED_MARGIN_PCT = Gauge('stress_sim_projected_margin_pct', 'Projected stress margin usage in pct', ['symbol'])
STRESS_SIM_WORST_SCENARIO = Gauge('stress_sim_worst_scenario', 'Worst stress scenario name (gauge=1 for current worst)', ['symbol', 'scenario'])

# Governance hygiene metrics
GOV_INVALID_REGIME_BREAKDOWN_TOTAL = Counter(
    'gov_invalid_regime_breakdown_total',
    'Invalid regime_breakdown values detected in governance edge scores',
)

# Trade Ledger Reconciliation metrics
LEDGER_RECON_RUNS_TOTAL = Counter('ledger_recon_runs_total', 'Total number of ledger reconciliation runs')
LEDGER_RECON_ISSUES_TOTAL = Counter('ledger_recon_issues_total', 'Total number of reconciliation issues detected', ['type'])
LEDGER_RECON_FIXED_TOTAL = Counter('ledger_recon_fixed_total', 'Total number of reconciliation issues fixed', ['type'])
LEDGER_RECON_DRYRUN_TOTAL = Counter('ledger_recon_dryrun_total', 'Total number of dry-run reconciliation runs')
LEDGER_RECON_LAST_DURATION_MS = Gauge('ledger_recon_last_duration_ms', 'Duration of last reconciliation run in ms')

ASYMMETRIC_COST_ESTIMATE_USD = Counter('asymmetric_cost_estimate_usd', 'Estimated USD saved vs strict mirror or spent on hedge')

# ── Phase 6: Performance Governance Metrics ──────────────────────────────────
# Rejection dashboard
REJECTION_TOTAL = Counter('p6_rejection_total', 'Total pre-flight rejections', ['filter', 'symbol'])
REJECTION_HARD = Counter('p6_rejection_hard_total', 'Hard rejections (never bypassed)')
REJECTION_SOFT = Counter('p6_rejection_soft_total', 'Soft rejections (bypassed by FORCE_EXECUTE)')
SIGNAL_EVALUATED_TOTAL = Counter('p6_signal_evaluated_total', 'Total signals entering pipeline')

# Execution quality
SLIPPAGE_BPS_HISTOGRAM = Histogram(
    'p6_slippage_bps', 'Fill slippage in basis points',
    buckets=[1, 5, 10, 20, 30, 50, 60, 80, 100, 200]
)
FILL_DELAY_HISTOGRAM = Histogram(
    'p6_fill_delay_ms', 'Time from signal to fill in milliseconds',
    buckets=[50, 100, 250, 500, 1000, 2000, 5000, 10000]
)
EMERGENCY_CLOSE_TOTAL = Counter('p6_emergency_close_total', 'Emergency close attempts', ['outcome'])
RECONCILER_ORPHANS_TOTAL = Counter('p6_reconciler_orphans_total', 'Orphan positions detected by reconciler')
RECONCILER_GHOSTS_TOTAL = Counter('p6_reconciler_ghosts_total', 'Ghost positions cleaned by reconciler')
RECONCILER_FLATTENED_TOTAL = Counter('p6_reconciler_flattened_total', 'Orphans auto-flattened by reconciler')

# Phase 6.5: Position Drift Metrics
DRIFT_DETECTED_TOTAL = Counter('p6_position_drift_detected_total', 'Position drift events detected', ['severity'])
DRIFT_SIZE = Histogram(
    'p6_position_drift_size', 'Absolute drift size between exchange and internal',
    buckets=[0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0]
)
DRIFT_AUTO_ADJUST_TOTAL = Counter('p6_position_drift_auto_adjust_total', 'Drift auto-corrections applied')

# Strategy health
STRATEGY_TRADES_TOTAL = Counter('p6_strategy_trades_total', 'Trades opened', ['strategy', 'regime'])
STRATEGY_PNL_GAUGE = Gauge('p6_strategy_pnl', 'Daily PnL by strategy', ['strategy'])

# Shadow / protection cost metrics
BLOCKED_TRADES_TOTAL = Counter('p6_blocked_trades_total', 'Signals blocked by filters in shadow mode')
BLOCKED_TRADES_BY_FILTER = Counter('p6_blocked_trades_by_filter', 'Blocked signals per filter', ['filter'])
SHADOW_PROFIT_MISSED = Counter('p6_shadow_profit_missed', 'Hypothetical profit missed due to filter blocks', ['filter', 'outcome'])

# ── Phase 7: Adaptive Risk Engine Metrics ────────────────────────────────────
ADAPTIVE_RISK_MULTIPLIER = Gauge('p7_adaptive_risk_multiplier', 'Current adaptive risk multiplier')
ADAPTIVE_RISK_MODE_TOTAL = Counter('p7_adaptive_risk_mode_total', 'Adaptive risk decisions by mode', ['mode'])
ADAPTIVE_RISK_BLOCK_TOTAL = Counter('p7_adaptive_risk_block_total', 'Blocked trades by adaptive risk', ['reason'])
ADAPTIVE_LEVERAGE = Gauge('p7_adaptive_leverage', 'Effective leverage after adaptive adjustment')
ADAPTIVE_EFFECTIVE_RISK_PCT = Gauge('p7_adaptive_effective_risk_pct', 'Effective risk pct after adaptive adjustment')

# ── Phase 8: Multi-Alpha Engine Metrics ──────────────────────────────────────
ALPHA_SCORE_HISTOGRAM = Histogram(
    'p8_alpha_signal_score', 'Composite signal score from StrategyScorer',
    ['strategy'],
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)
ALPHA_SIGNALS_SCORED_TOTAL = Counter('p8_alpha_signals_scored_total', 'Total signals scored', ['strategy'])
ALPHA_SIGNALS_ACCEPTED_TOTAL = Counter('p8_alpha_signals_accepted_total', 'Signals accepted after scoring', ['strategy'])
ALPHA_SIGNALS_REJECTED_TOTAL = Counter('p8_alpha_signals_rejected_total', 'Signals below min_score', ['strategy'])
ALPHA_CONFLICT_TOTAL = Counter('p8_alpha_conflict_total', 'Long vs short conflicts detected')
ALPHA_CORRELATION_DEDUP_TOTAL = Counter('p8_alpha_correlation_dedup_total', 'Correlated signals merged')
ALPHA_CAPITAL_FRACTION_GAUGE = Gauge('p8_alpha_capital_fraction', 'Capital fraction per strategy', ['strategy'])
ALPHA_WEIGHT_GAUGE = Gauge('p8_alpha_weight', 'Adaptive weight per strategy', ['strategy'])

# ── Phase 9: Alpha Governance Metrics ────────────────────────────────────────
ALPHA_GOV_ALERT_TOTAL = Counter('p9_alpha_gov_alert_total', 'Governance alerts fired', ['alert_type', 'severity'])
ALPHA_GOV_STARVATION_GAUGE = Gauge('p9_alpha_gov_starvation', 'Strategies currently starved')
ALPHA_GOV_CONCENTRATION_GAUGE = Gauge('p9_alpha_gov_concentration', 'Strategies exceeding concentration limit')
ALPHA_GOV_DEAD_STRATEGIES_GAUGE = Gauge('p9_alpha_gov_dead_strategies', 'Strategies with no recent selection')
ALPHA_GOV_WEIGHT_VELOCITY = Histogram(
    'p9_alpha_gov_weight_velocity', 'Weight change magnitude per cycle',
    ['strategy'],
    buckets=[0.01, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0],
)
ALPHA_GOV_DAMPENED_TOTAL = Counter('p9_alpha_gov_dampened_total', 'Weight dampening events', ['strategy'])
ALPHA_GOV_DOUBLE_PENALTY_TOTAL = Counter('p9_alpha_gov_double_penalty_total', 'Double penalization detections', ['strategy'])
ALPHA_GOV_CONFLICT_COST_TOTAL = Counter('p9_alpha_gov_conflict_cost_total', 'Conflict cost events')
ALPHA_GOV_ATTRIBUTION_PNL = Gauge('p9_alpha_gov_attribution_pnl', 'Attributed PnL per strategy', ['strategy'])
ALPHA_GOV_ACCEPT_RATE = Gauge('p9_alpha_gov_accept_rate', 'Accept rate per strategy', ['strategy'])
ALPHA_GOV_BREADTH = Gauge('p9_alpha_gov_breadth', 'Alpha breadth: fraction of strategies with >0 accepted trades')
ALPHA_GOV_SCORE_SPREAD = Gauge('p9_alpha_score_spread', 'Signal Quality Spread: avg accepted score - avg rejected score')
ALPHA_GOV_SHADOW_TOTAL = Gauge('p9_alpha_gov_shadow_total', 'Shadow signals (valid but outscored) per strategy', ['strategy'])

def start_metrics_server(port: int = 8000):
    """
    Démarre le serveur d'exposition Prometheus sur un Thread asynchrone / background.
    """
    try:
        start_http_server(port)
        print(f"✅ Prometheus Metrics Exporter running on port {port}")
    except Exception as e:
        print(f"❌ Failed to start metrics server: {e}")
