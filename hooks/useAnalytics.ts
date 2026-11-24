/**
 * Analytics Types — TypeScript definitions for the Analytics Pipeline
 * 
 * These types mirror the Python enums & API response shapes exactly.
 * The frontend NEVER calculates PF, DD, WR, edge — it consumes pre-computed facts.
 * 
 * Author: SMC Trading Bot Framework — Analytics Pipeline
 */

// ═══════════════════════════════════════════════════════════════════════════════
// ENUMS (mirror backend/models/analytics_enums.py)
// ═══════════════════════════════════════════════════════════════════════════════

export enum MarketRegime {
  TREND = "TREND",
  RANGE = "RANGE",
  CHOP = "CHOP",
  RECOVERY = "RECOVERY",
  UNKNOWN = "UNKNOWN",
}

export enum SystemMode {
  LIVE = "LIVE",
  SHADOW = "SHADOW",
  PAUSED = "PAUSED",
  HANDS_OFF = "HANDS_OFF",
  MONITORING = "MONITORING",
}

export enum SignalDecision {
  ACCEPT = "ACCEPT",
  REJECT = "REJECT",
}

export enum RejectReason {
  // Risk gates
  KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE",
  DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT",
  SESSION_LOSS_LIMIT = "SESSION_LOSS_LIMIT",
  CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES",
  MAX_POSITIONS_REACHED = "MAX_POSITIONS_REACHED",
  DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT",
  VOLATILITY_PAUSE = "VOLATILITY_PAUSE",
  COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE",
  // Market quality
  REGIME_BLOCKED = "REGIME_BLOCKED",
  SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE",
  LIQUIDITY_TOO_THIN = "LIQUIDITY_TOO_THIN",
  STALE_DATA = "STALE_DATA",
  FUNDING_RATE_HIGH = "FUNDING_RATE_HIGH",
  // Signal quality
  RR_TOO_LOW = "RR_TOO_LOW",
  EDGE_SCORE_LOW = "EDGE_SCORE_LOW",
  NO_MTF_ALIGNMENT = "NO_MTF_ALIGNMENT",
  NO_LIQUIDITY_SWEEP = "NO_LIQUIDITY_SWEEP",
  DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL",
  CONFLICTING_POSITION = "CONFLICTING_POSITION",
  // Execution
  BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE",
  ORDER_REJECTED = "ORDER_REJECTED",
  SIZING_BELOW_MINIMUM = "SIZING_BELOW_MINIMUM",
  HEALTH_GATE_FAIL = "HEALTH_GATE_FAIL",
  // Capital
  CAPITAL_NOT_ALLOCATED = "CAPITAL_NOT_ALLOCATED",
  SCALE_READINESS_FAIL = "SCALE_READINESS_FAIL",
  // Misc
  MANUAL_BLOCK = "MANUAL_BLOCK",
  UNKNOWN = "UNKNOWN",
}

export enum ExecutionMode {
  MAKER = "MAKER",
  TAKER = "TAKER",
  FALLBACK_TAKER = "FALLBACK_TAKER",
}

export enum BrokerName {
  KRAKEN = "kraken",
}

export enum RiskEventType {
  KILL_SWITCH_TRIGGER = "KILL_SWITCH_TRIGGER",
  KILL_SWITCH_RELEASE = "KILL_SWITCH_RELEASE",
  DRAWDOWN_EVENT = "DRAWDOWN_EVENT",
  RECOVERY_ENTER = "RECOVERY_ENTER",
  RECOVERY_STEP_CHANGE = "RECOVERY_STEP_CHANGE",
  RECOVERY_EXIT = "RECOVERY_EXIT",
  HEALTH_GATE_FAIL = "HEALTH_GATE_FAIL",
  CHAOS_SCENARIO = "CHAOS_SCENARIO",
  SLO_BREACH = "SLO_BREACH",
  VOLATILITY_PAUSE_START = "VOLATILITY_PAUSE_START",
  VOLATILITY_PAUSE_END = "VOLATILITY_PAUSE_END",
  COOLDOWN_START = "COOLDOWN_START",
  COOLDOWN_END = "COOLDOWN_END",
  MANUAL_INTERVENTION = "MANUAL_INTERVENTION",
}

export enum RiskSeverity {
  INFO = "INFO",
  WARNING = "WARNING",
  CRITICAL = "CRITICAL",
  EMERGENCY = "EMERGENCY",
}

// ── Execution Attempt enums ──────────────────────────────────────────────────

export enum ExecutionAttemptStatus {
  DISPATCH_STARTED = "DISPATCH_STARTED",
  PRE_FLIGHT_REJECT = "PRE_FLIGHT_REJECT",
  PRE_FLIGHT_PASS = "PRE_FLIGHT_PASS",
  EXECUTED = "EXECUTED",
  EXECUTION_FAILED = "EXECUTION_FAILED",
}

export enum ExecutionBlockReason {
  AUTO_EXECUTE_DISABLED = "AUTO_EXECUTE_DISABLED",
  PAPER_MODE = "PAPER_MODE",
  KILL_SWITCH = "KILL_SWITCH",
  ACTIVE_TRADE = "ACTIVE_TRADE",
  DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL",
  MAX_DAILY_LOSS = "MAX_DAILY_LOSS",
  ZERO_BALANCE = "ZERO_BALANCE",
  INVALID_TPSL = "INVALID_TPSL",
  NOTIONAL_TOO_LOW = "NOTIONAL_TOO_LOW",
  QTY_VALIDATION_FAILED = "QTY_VALIDATION_FAILED",
  MAX_TRADES_PER_DAY = "MAX_TRADES_PER_DAY",
  COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE",
  COOLDOWN_HARD_REJECT = "COOLDOWN_HARD_REJECT",
  LOW_RR = "LOW_RR",
  DISABLED_SYMBOL = "DISABLED_SYMBOL",
  GLOBAL_RISK = "GLOBAL_RISK",
  MAKER_NOT_VIABLE = "MAKER_NOT_VIABLE",
  INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE",
  MULTI_BROKER_FAILED = "MULTI_BROKER_FAILED",
  STALE_DATA = "STALE_DATA",
  EXECUTION_ERROR = "EXECUTION_ERROR",
  UNKNOWN = "UNKNOWN",
}

export enum OpsEventType {
  WS_DISCONNECT = "WS_DISCONNECT",
  WS_RECONNECT = "WS_RECONNECT",
  CLOCK_SKEW = "CLOCK_SKEW",
  LATENCY_SPIKE = "LATENCY_SPIKE",
  API_ERROR = "API_ERROR",
  RATE_LIMIT_HIT = "RATE_LIMIT_HIT",
  CONFIG_RELOAD = "CONFIG_RELOAD",
  ENGINE_START = "ENGINE_START",
  ENGINE_STOP = "ENGINE_STOP",
  HEALTH_CHECK_FAIL = "HEALTH_CHECK_FAIL",
  DB_ERROR = "DB_ERROR",
  BROKER_TIMEOUT = "BROKER_TIMEOUT",
}

// ═══════════════════════════════════════════════════════════════════════════════
// API RESPONSE TYPES
// ═══════════════════════════════════════════════════════════════════════════════

// --- /api/analytics/overview ---
export interface EquityCurvePoint {
  date: string;
  pnl: number;
  cumulative: number;
}

export interface AnalyticsOverview {
  period: { from: string; to: string };
  pnl: number;
  profit_factor: number | null;
  winrate: number | null;
  total_trades: number;
  wins: number;
  losses: number;
  max_drawdown: number | null;
  days_count: number;
  equity_curve: EquityCurvePoint[];
}

// --- /api/analytics/daily-series ---
export interface DailyAnalyticsRow {
  date: string;
  pnl: number;
  profit_factor: number | null;
  winrate: number | null;
  expectancy: number | null;
  max_drawdown: number | null;
  total_trades: number;
  wins: number;
  losses: number;
  avg_edge_score: number | null;
  capital_efficiency: number | null;
  avg_rr: number | null;
  maker_count: number;
  taker_count: number;
  fallback_count: number;
  avg_mae: number | null;
  avg_mfe: number | null;
  regime_breakdown: Record<string, { pnl: number; trades: number; wins: number; wr: number }> | null;
  broker_breakdown: Record<string, { pnl: number; trades: number }> | null;
  signals_total: number;
  signals_accepted: number;
  signals_rejected: number;
  top_reject_reasons: Record<string, number> | null;
  execution_blocked_total: number | null;
  execution_blocked_by_reason: Record<string, number> | null;
  execution_dispatch_total: number | null;
  execution_success_total: number | null;
}

// --- /api/analytics/weekly-series ---
export interface WeeklyAnalyticsRow {
  week_key: string;
  week_start: string;
  pnl: number;
  profit_factor: number | null;
  winrate: number | null;
  expectancy: number | null;
  max_drawdown: number | null;
  total_trades: number;
  wins: number;
  losses: number;
  avg_edge_score: number | null;
  capital_efficiency: number | null;
  regime_breakdown: Record<string, { pnl: number; trades: number; wins: number }> | null;
  broker_breakdown: Record<string, { pnl: number; trades: number }> | null;
}

// --- /api/analytics/regime-history ---
export interface SystemStateSnapshot {
  id: string;
  ts: string;
  regime: MarketRegime;
  regime_confidence: number | null;
  risk_factor: number;
  adaptive_weight: number | null;
  capital_utilization: number | null;
  mode: SystemMode;
  kill_switch_active: boolean;
  volatility_pause_active: boolean;
  recovery_step: number;
  open_positions: number;
  daily_pnl: number;
  equity: number | null;
}

// --- /api/analytics/decision-breakdown ---
export interface RegimeAcceptance {
  total: number;
  accepted: number;
  rate: number;
}

export interface SymbolDecisionStats {
  total: number;
  accepted: number;
  rejected: number;
}

export interface DecisionBreakdown {
  total_signals: number;
  accepted: number;
  rejected: number;
  acceptance_rate: number;
  reject_reasons: Record<string, number>;
  by_symbol: Record<string, SymbolDecisionStats>;
  by_regime: Record<string, RegimeAcceptance>;
}

// --- /api/analytics/execution-quality ---
export interface MinMaxAvg {
  avg: number | null;
  min: number | null;
  max: number | null;
}

export interface BrokerStats {
  trades: number;
  pnl: number;
  avg_fees: number;
  fees_total: number;
}

export interface ABTestingStats {
  trades: number;
  pnl: number;
  wins: number;
  wr: number;
}

export interface ExecutionQuality {
  total_trades: number;
  execution_modes: Record<string, number>;
  broker_stats: Record<string, BrokerStats>;
  mae: MinMaxAvg;
  mfe: MinMaxAvg;
  duration: { avg_sec: number | null; min_sec: number | null; max_sec: number | null };
  ab_testing: Record<string, ABTestingStats>;
}

// --- /api/analytics/risk-events ---
export interface RiskEventRecord {
  id: string;
  ts: string;
  event_type: RiskEventType;
  severity: RiskSeverity;
  detail: Record<string, unknown> | null;
  symbol: string | null;
  trade_id: string | null;
}

// --- /api/analytics/execution-blocked ---
export interface ExecutionAttemptRecord {
  id: string;
  ts: string;
  symbol: string;
  signal_id: string | null;
  side: string | null;
  broker: string | null;
  is_multi_broker: boolean;
  status: ExecutionAttemptStatus;
  block_reason: string | null;
  block_details: Record<string, unknown> | null;
  entry_price: number | null;
  quantity: number | null;
  account_balance: number | null;
  regime: string | null;
  order_id: string | null;
  fill_price: number | null;
  latency_ms: number | null;
}

export interface SymbolBlockedStats {
  blocked: number;
  passed: number;
  total: number;
}

export interface DailyBlockedStats {
  blocked: number;
  passed: number;
  total: number;
}

export interface ExecutionBlockedData {
  total_attempts: number;
  blocked: number;
  passed: number;
  dispatched: number;
  block_rate: number;
  by_reason: Record<string, number>;
  by_symbol: Record<string, SymbolBlockedStats>;
  daily_timeline: Record<string, DailyBlockedStats>;
  recent_blocked: ExecutionAttemptRecord[];
}

// --- /api/analytics/ops-events ---
export interface OpsEventRecord {
  id: string;
  ts: string;
  event_type: OpsEventType;
  severity: string;
  source: string | null;
  message: string | null;
  detail: Record<string, unknown> | null;
  symbol: string | null;
  broker: string | null;
}

// ═══════════════════════════════════════════════════════════════════════════════
// DISPLAY HELPERS
// ═══════════════════════════════════════════════════════════════════════════════

/** Color mapping for regime badges */
export const REGIME_COLORS: Record<MarketRegime, string> = {
  [MarketRegime.TREND]: "#22c55e",
  [MarketRegime.RANGE]: "#3b82f6",
  [MarketRegime.CHOP]: "#f59e0b",
  [MarketRegime.RECOVERY]: "#ef4444",
  [MarketRegime.UNKNOWN]: "#6b7280",
};

/** Color mapping for severity badges */
export const SEVERITY_COLORS: Record<RiskSeverity, string> = {
  [RiskSeverity.INFO]: "#6b7280",
  [RiskSeverity.WARNING]: "#f59e0b",
  [RiskSeverity.CRITICAL]: "#ef4444",
  [RiskSeverity.EMERGENCY]: "#dc2626",
};

/** Human-readable labels for reject reasons */
export const REJECT_REASON_LABELS: Record<string, string> = {
  KILL_SWITCH_ACTIVE: "Kill Switch",
  DAILY_LOSS_LIMIT: "Daily Loss Limit",
  SESSION_LOSS_LIMIT: "Session Loss",
  CONSECUTIVE_LOSSES: "Consec. Losses",
  MAX_POSITIONS_REACHED: "Max Positions",
  DRAWDOWN_LIMIT: "Drawdown Limit",
  VOLATILITY_PAUSE: "Volatility Pause",
  COOLDOWN_ACTIVE: "Cooldown",
  REGIME_BLOCKED: "Regime Blocked",
  SPREAD_TOO_WIDE: "Spread Wide",
  LIQUIDITY_TOO_THIN: "Low Liquidity",
  STALE_DATA: "Stale Data",
  FUNDING_RATE_HIGH: "High Funding",
  RR_TOO_LOW: "Low R:R",
  EDGE_SCORE_LOW: "Low Edge",
  NO_MTF_ALIGNMENT: "No MTF Align",
  NO_LIQUIDITY_SWEEP: "No Sweep",
  DUPLICATE_SIGNAL: "Duplicate",
  CONFLICTING_POSITION: "Conflict",
  BROKER_UNAVAILABLE: "Broker Down",
  ORDER_REJECTED: "Order Rejected",
  SIZING_BELOW_MINIMUM: "Size Too Small",
  HEALTH_GATE_FAIL: "Health Fail",
  CAPITAL_NOT_ALLOCATED: "No Capital",
  SCALE_READINESS_FAIL: "Scale Fail",
  MANUAL_BLOCK: "Manual Block",
  UNKNOWN: "Unknown",
};

/** Human-readable labels for execution block reasons */
export const EXECUTION_BLOCK_LABELS: Record<string, string> = {
  AUTO_EXECUTE_DISABLED: "Auto-Exec Off",
  PAPER_MODE: "Paper Mode",
  KILL_SWITCH: "Kill Switch",
  ACTIVE_TRADE: "Active Trade",
  DUPLICATE_SIGNAL: "Duplicate",
  MAX_DAILY_LOSS: "Daily Loss Limit",
  ZERO_BALANCE: "Zero Balance",
  INVALID_TPSL: "Invalid TP/SL",
  NOTIONAL_TOO_LOW: "Notional Low",
  QTY_VALIDATION_FAILED: "Qty Failed",
  MAX_TRADES_PER_DAY: "Max Trades/Day",
  COOLDOWN_ACTIVE: "Cooldown",
  COOLDOWN_HARD_REJECT: "Hard Cooldown",
  LOW_RR: "Low R:R",
  DISABLED_SYMBOL: "Disabled Symbol",
  GLOBAL_RISK: "Global Risk",
  MAKER_NOT_VIABLE: "Maker Not Viable",
  INSUFFICIENT_BALANCE: "Low Balance",
  MULTI_BROKER_FAILED: "Multi-Broker Fail",
  STALE_DATA: "Stale Data",
  EXECUTION_ERROR: "Exec Error",
  UNKNOWN: "Unknown",
};
