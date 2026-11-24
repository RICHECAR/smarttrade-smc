import React, { useState, useEffect, useCallback } from 'react';
import { RefreshCw, TrendingUp, TrendingDown, Target, Award, AlertTriangle, Percent, BarChart3 } from 'lucide-react';

// ═══════════════════════════════════════════════════════════════════════════════
// TYPES
// ═══════════════════════════════════════════════════════════════════════════════

export interface PerformanceStats {
  total_pnl: number;
  win_rate: number;
  avg_rr: number;
  total_trades: number;
  max_drawdown: number;
  profit_factor: number;
  wins: number;
  losses: number;
  break_even: number;
  avg_win: number;
  avg_loss: number;
  best_trade: number;
  worst_trade: number;
  avg_hold_time_hours: number;
  // Institutional metrics
  expectancy?: number;
  sharpe_ratio?: number;
  sortino_ratio?: number;
  calmar_ratio?: number;
  avg_pnl_per_trade?: number;
  kelly_criterion?: number;
  skewness?: number;
  kurtosis?: number;
  exposure_time_pct?: number;
  current_streak?: number;
  max_win_streak?: number;
  max_loss_streak?: number;
}

export interface PerformanceResponse {
  stats: PerformanceStats;
  broker_filter: string | null;
  date_from: string | null;
  date_to: string | null;
  timestamp: string;
}

export interface BrokerPerformance {
  bitget: PerformanceStats;
  kraken: PerformanceStats;
  combined: PerformanceStats;
  date_from: string | null;
  date_to: string | null;
  timestamp: string;
}

interface Props {
  apiUrl?: string;
  dateFrom?: string;
  dateTo?: string;
  refreshInterval?: number; // ms
  onDataLoaded?: (combined: PerformanceStats) => void;
}

// ═══════════════════════════════════════════════════════════════════════════════
// UTILS
// ═══════════════════════════════════════════════════════════════════════════════

function fmt(n: number | undefined | null, decimals: number = 2): string {
  if (n === undefined || n === null) return "—";
  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: decimals,
    minimumFractionDigits: decimals
  }).format(n);
}

function fmtPercent(n: number | undefined | null): string {
  if (n === undefined || n === null) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

// ═══════════════════════════════════════════════════════════════════════════════
// COMPONENTS
// ═══════════════════════════════════════════════════════════════════════════════

function StatCard({ 
  title, 
  value, 
  suffix = '', 
  color = 'text-white',
  icon: Icon,
  subtext
}: {
  title: string;
  value: string;
  suffix?: string;
  color?: string;
  icon?: React.ElementType;
  subtext?: string;
}) {
  return (
    <div className="bg-[#1B2129] rounded-lg p-4 border border-kraken-border hover:border-kraken-cyan/30 transition-colors">
      <div className="flex items-start justify-between">
        <div>
          <div className="text-xs text-gray-400 uppercase tracking-wider mb-1">{title}</div>
          <div className={`text-xl font-bold ${color}`}>
            {value}
            {suffix && <span className="text-sm opacity-50 ml-1">{suffix}</span>}
          </div>
          {subtext && <div className="text-xs text-gray-500 mt-1">{subtext}</div>}
        </div>
        {Icon && (
          <div className="p-2 bg-kraken-cyan/10 rounded-lg">
            <Icon size={18} className="text-kraken-cyan" />
          </div>
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// MAIN COMPONENT
// ═══════════════════════════════════════════════════════════════════════════════

export default function PerformanceAnalyticsPanel({ 
  apiUrl = '',
  dateFrom,
  dateTo,
  refreshInterval = 30000,
  onDataLoaded
}: Props) {
  const [stats, setStats] = useState<BrokerPerformance | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedBroker, setSelectedBroker] = useState<'combined' | 'kraken'>('kraken');

  // Fetch performance data
  const loadPerformance = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams();
      if (dateFrom) params.append('from', dateFrom);
      if (dateTo) params.append('to', dateTo);

      const res = await fetch(`${apiUrl}/api/stats/brokers?${params}`);
      
      if (!res.ok) {
        throw new Error(`API error: ${res.status}`);
      }

      const data = await res.json();
      setStats(data);
      if (onDataLoaded && data.combined) {
        onDataLoaded(data.combined);
      }
    } catch (err) {
      console.error('[PerformancePanel] Load error:', err);
      setError(err instanceof Error ? err.message : 'Failed to load performance data');
    } finally {
      setLoading(false);
    }
  }, [apiUrl, dateFrom, dateTo]);

  // Initial load and interval refresh
  useEffect(() => {
    loadPerformance();
    
    if (refreshInterval > 0) {
      const interval = setInterval(loadPerformance, refreshInterval);
      return () => clearInterval(interval);
    }
  }, [loadPerformance, refreshInterval]);

  // Get current stats based on selected broker
  const currentStats: PerformanceStats | null = stats 
    ? stats[selectedBroker] 
    : null;

  return (
    <div className="bg-kraken-card border border-kraken-border rounded-xl p-5 shadow-lg">
      {/* Header */}
      <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 mb-5">
        <h3 className="text-white font-bold flex items-center gap-2">
          <BarChart3 size={18} className="text-kraken-cyan" />
          Performance Analytics
          {loading && <RefreshCw size={14} className="animate-spin text-gray-500 ml-2" />}
        </h3>

        <div className="flex items-center gap-3">
          {/* Broker Filter — Kraken primary */}
          <div className="flex rounded-lg border border-kraken-border overflow-hidden">
            {(['kraken', 'combined'] as const).map((broker) => (
              <button
                key={broker}
                onClick={() => setSelectedBroker(broker)}
                className={`px-3 py-1.5 text-xs font-bold uppercase transition-colors ${
                  selectedBroker === broker
                    ? 'bg-kraken-cyan text-black'
                    : 'bg-transparent text-gray-400 hover:text-white'
                }`}
              >
                {broker}
              </button>
            ))}
          </div>

          {/* Refresh Button */}
          <button
            onClick={loadPerformance}
            disabled={loading}
            className="p-2 bg-kraken-cyan/10 hover:bg-kraken-cyan/20 text-kraken-cyan border border-kraken-cyan/50 rounded-lg transition-all disabled:opacity-50"
          >
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {/* Error State */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/20 rounded-lg p-3 flex items-center gap-2 text-red-400 text-sm mb-4">
          <AlertTriangle size={16} />
          {error}
        </div>
      )}

      {/* Stats Grid */}
      {currentStats && (
        <>
          {/* Primary Stats Row */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 mb-4">
            <StatCard
              title="Total PnL"
              value={fmt(currentStats.total_pnl)}
              suffix="USDT"
              color={currentStats.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'}
              icon={currentStats.total_pnl >= 0 ? TrendingUp : TrendingDown}
            />
            <StatCard
              title="Win Rate"
              value={fmtPercent(currentStats.win_rate)}
              color={currentStats.win_rate >= 0.5 ? 'text-green-400' : 'text-yellow-400'}
              icon={Percent}
            />
            <StatCard
              title="Avg R:R"
              value={fmt(currentStats.avg_rr)}
              icon={Target}
            />
            <StatCard
              title="Total Trades"
              value={String(currentStats.total_trades)}
              subtext={`${currentStats.wins}W / ${currentStats.losses}L`}
            />
            <StatCard
              title="Max Drawdown"
              value={fmt(currentStats.max_drawdown)}
              suffix="USDT"
              color="text-red-400"
              icon={TrendingDown}
            />
            <StatCard
              title="Profit Factor"
              value={fmt(currentStats.profit_factor)}
              color={currentStats.profit_factor >= 1.5 ? 'text-green-400' : 'text-yellow-400'}
              icon={Award}
            />
          </div>

          {/* Secondary Stats Row */}
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-3">
            <StatCard
              title="Avg Win"
              value={fmt(currentStats.avg_win)}
              suffix="USDT"
              color="text-green-400"
            />
            <StatCard
              title="Avg Loss"
              value={fmt(currentStats.avg_loss)}
              suffix="USDT"
              color="text-red-400"
            />
            <StatCard
              title="Best Trade"
              value={fmt(currentStats.best_trade)}
              suffix="USDT"
              color="text-green-400"
            />
            <StatCard
              title="Worst Trade"
              value={fmt(currentStats.worst_trade)}
              suffix="USDT"
              color="text-red-400"
            />
            <StatCard
              title="Avg Hold Time"
              value={fmt(currentStats.avg_hold_time_hours, 1)}
              suffix="hrs"
            />
          </div>

          {/* Institutional Metrics Row 1 */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 mb-4">
            <StatCard
              title="Expectancy"
              value={fmt(currentStats.expectancy)}
              suffix="USDT"
              color={(currentStats.expectancy ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}
              icon={Target}
            />
            <StatCard
              title="Sharpe Ratio"
              value={fmt(currentStats.sharpe_ratio)}
              color={(currentStats.sharpe_ratio ?? 0) >= 1 ? 'text-green-400' : 'text-yellow-400'}
            />
            <StatCard
              title="Sortino Ratio"
              value={fmt(currentStats.sortino_ratio)}
              color={(currentStats.sortino_ratio ?? 0) >= 1 ? 'text-green-400' : 'text-yellow-400'}
            />
            <StatCard
              title="Calmar Ratio"
              value={fmt(currentStats.calmar_ratio)}
              color={(currentStats.calmar_ratio ?? 0) >= 2 ? 'text-green-400' : 'text-yellow-400'}
            />
            <StatCard
              title="Kelly Criterion"
              value={fmt((currentStats.kelly_criterion ?? 0) * 100, 1)}
              suffix="%"
              color={(currentStats.kelly_criterion ?? 0) > 0 ? 'text-green-400' : 'text-red-400'}
              subtext="optimal size"
            />
            <StatCard
              title="Avg PnL/Trade"
              value={fmt(currentStats.avg_pnl_per_trade)}
              suffix="USDT"
              color={(currentStats.avg_pnl_per_trade ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}
            />
          </div>

          {/* Institutional Metrics Row 2 */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 mb-4">
            <StatCard
              title="Skewness"
              value={fmt(currentStats.skewness)}
              color={(currentStats.skewness ?? 0) > 0 ? 'text-green-400' : 'text-yellow-400'}
              subtext={(currentStats.skewness ?? 0) > 0 ? 'positive tail' : 'negative tail'}
            />
            <StatCard
              title="Kurtosis"
              value={fmt(currentStats.kurtosis)}
              subtext={(currentStats.kurtosis ?? 0) > 0 ? 'fat tails' : 'thin tails'}
            />
            <StatCard
              title="Exposure Time"
              value={fmt(currentStats.exposure_time_pct, 1)}
              suffix="%"
              subtext="time in market"
            />
            <StatCard
              title="Current Streak"
              value={String(currentStats.current_streak ?? 0)}
              color={(currentStats.current_streak ?? 0) > 0 ? 'text-green-400' : (currentStats.current_streak ?? 0) < 0 ? 'text-red-400' : 'text-white'}
              subtext={(currentStats.current_streak ?? 0) > 0 ? 'wins' : (currentStats.current_streak ?? 0) < 0 ? 'losses' : 'neutral'}
            />
            <StatCard
              title="Best/Worst Streak"
              value={`${currentStats.max_win_streak ?? 0}W / ${currentStats.max_loss_streak ?? 0}L`}
              subtext="consecutive"
            />
          </div>

          {/* Win/Loss Bar */}
          <div className="mt-4 pt-4 border-t border-kraken-border">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs text-gray-400">Win/Loss Distribution</span>
              <span className="text-xs text-gray-500">
                {currentStats.wins + currentStats.losses + currentStats.break_even} trades
              </span>
            </div>
            <div className="h-4 bg-[#1B2129] rounded-full overflow-hidden flex">
              {currentStats.wins > 0 && (
                <div 
                  className="bg-green-500 h-full transition-all"
                  style={{ 
                    width: `${(currentStats.wins / (currentStats.wins + currentStats.losses + currentStats.break_even)) * 100}%` 
                  }}
                />
              )}
              {currentStats.break_even > 0 && (
                <div 
                  className="bg-yellow-500 h-full transition-all"
                  style={{ 
                    width: `${(currentStats.break_even / (currentStats.wins + currentStats.losses + currentStats.break_even)) * 100}%` 
                  }}
                />
              )}
              {currentStats.losses > 0 && (
                <div 
                  className="bg-red-500 h-full transition-all"
                  style={{ 
                    width: `${(currentStats.losses / (currentStats.wins + currentStats.losses + currentStats.break_even)) * 100}%` 
                  }}
                />
              )}
            </div>
            <div className="flex justify-between mt-2 text-xs">
              <span className="text-green-400">{currentStats.wins} Wins</span>
              <span className="text-yellow-400">{currentStats.break_even} BE</span>
              <span className="text-red-400">{currentStats.losses} Losses</span>
            </div>
          </div>
        </>
      )}

      {/* Empty State */}
      {!loading && !error && !currentStats && (
        <div className="text-center py-8 text-gray-500">
          <BarChart3 size={48} className="mx-auto mb-3 opacity-20" />
          <p>No performance data available</p>
          <p className="text-sm">Close some positions to see analytics</p>
        </div>
      )}
    </div>
  );
}
