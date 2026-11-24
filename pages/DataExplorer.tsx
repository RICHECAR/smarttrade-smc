import React, { useState, useMemo } from "react";
import {
  BarChart, Bar, LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, PieChart, Pie, Cell, AreaChart, Area, Legend,
} from "recharts";
import {
  Database, TrendingUp, Shield, Activity, BarChart3, Filter,
  AlertTriangle, CheckCircle, XCircle, RefreshCw, Calendar,
  ArrowUpRight, ArrowDownRight, Zap, Clock,
} from "lucide-react";
import {
  useAnalyticsOverview,
  useDailySeries,
  useDecisionBreakdown,
  useExecutionQuality,
  useRiskEvents,
  useRegimeHistory,
  useExecutionBlocked,
} from "../hooks/useAnalyticsData";
import {
  REGIME_COLORS,
  SEVERITY_COLORS,
  REJECT_REASON_LABELS,
  EXECUTION_BLOCK_LABELS,
  MarketRegime,
  RiskSeverity,
} from "../hooks/useAnalytics";
import type {
  RegimeAcceptance,
  SymbolDecisionStats,
  BrokerStats,
  ABTestingStats,
  SymbolBlockedStats,
  DailyBlockedStats,
} from "../hooks/useAnalytics";

// ═══════════════════════════════════════════════════════════════════════════════
// DATE RANGE SELECTOR
// ═══════════════════════════════════════════════════════════════════════════════

const presets = [
  { label: "7j", days: 7 },
  { label: "14j", days: 14 },
  { label: "30j", days: 30 },
  { label: "90j", days: 90 },
];

function formatDate(d: Date): string {
  return d.toISOString().split("T")[0];
}

function DateRangeBar({
  from, to, onChange,
}: {
  from: string; to: string;
  onChange: (f: string, t: string) => void;
}) {
  return (
    <div className="flex items-center gap-2 text-xs flex-wrap">
      <Calendar size={14} className="text-gray-400" />
      {presets.map((p) => (
        <button
          key={p.label}
          onClick={() => {
            const d = new Date(); d.setDate(d.getDate() - p.days);
            onChange(formatDate(d), formatDate(new Date()));
          }}
          className={`px-2 py-1 rounded ${
            from === formatDate((() => { const d = new Date(); d.setDate(d.getDate() - p.days); return d; })())
              ? "bg-blue-600 text-white"
              : "bg-gray-700 text-gray-300 hover:bg-gray-600"
          }`}
        >
          {p.label}
        </button>
      ))}
      <input
        type="date" value={from}
        onChange={(e) => onChange(e.target.value, to)}
        className="bg-gray-800 border border-gray-600 rounded px-2 py-1 text-gray-300"
      />
      <span className="text-gray-500">→</span>
      <input
        type="date" value={to}
        onChange={(e) => onChange(from, e.target.value)}
        className="bg-gray-800 border border-gray-600 rounded px-2 py-1 text-gray-300"
      />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// KPI CARD
// ═══════════════════════════════════════════════════════════════════════════════

function KpiCard({
  label, value, subtitle, icon: Icon, color = "text-white",
}: {
  label: string; value: string; subtitle?: string;
  icon: React.ElementType; color?: string;
}) {
  return (
    <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-gray-400">{label}</span>
        <Icon size={14} className="text-gray-500" />
      </div>
      <div className={`text-xl font-bold ${color}`}>{value}</div>
      {subtitle && <div className="text-xs text-gray-500 mt-1">{subtitle}</div>}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// OVERVIEW SECTION
// ═══════════════════════════════════════════════════════════════════════════════

function OverviewSection({ from, to }: { from: string; to: string }) {
  const { data, loading } = useAnalyticsOverview(from, to);

  if (loading) return <LoadingRow />;
  if (!data) return <NoDataRow label="Aucune donnée pour cette période" />;

  const pnlColor = (data.pnl ?? 0) >= 0 ? "text-green-400" : "text-red-400";
  const PnlIcon = (data.pnl ?? 0) >= 0 ? ArrowUpRight : ArrowDownRight;

  return (
    <div>
      {/* KPI Grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 mb-4">
        <KpiCard label="PnL Total" value={`$${(data.pnl ?? 0).toFixed(2)}`} icon={PnlIcon} color={pnlColor} />
        <KpiCard label="Profit Factor" value={data.profit_factor?.toFixed(2) ?? "—"} icon={TrendingUp}
          color={data.profit_factor && data.profit_factor >= 1.5 ? "text-green-400" : "text-yellow-400"} />
        <KpiCard label="Win Rate" value={data.winrate != null ? `${(data.winrate * 100).toFixed(1)}%` : "—"} icon={CheckCircle}
          color={data.winrate && data.winrate >= 0.55 ? "text-green-400" : "text-yellow-400"} />
        <KpiCard label="Trades" value={String(data.total_trades ?? 0)} icon={Activity}
          subtitle={`${data.wins ?? 0}W / ${data.losses ?? 0}L`} />
        <KpiCard label="Max DD" value={data.max_drawdown != null ? `$${data.max_drawdown.toFixed(2)}` : "—"} icon={AlertTriangle}
          color="text-red-400" />
        <KpiCard label="Jours" value={String(data.days_count ?? 0)} icon={Calendar} />
      </div>

      {/* Equity Curve */}
      {data.equity_curve && data.equity_curve.length > 0 && (
        <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
          <h3 className="text-sm font-semibold text-gray-300 mb-3">Courbe d'Equity (PnL cumulé)</h3>
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={data.equity_curve}>
              <defs>
                <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="date" tick={{ fontSize: 10, fill: "#9ca3af" }} />
              <YAxis tick={{ fontSize: 10, fill: "#9ca3af" }} />
              <Tooltip contentStyle={{ backgroundColor: "#1f2937", border: "1px solid #374151", fontSize: 12 }} />
              <Area type="monotone" dataKey="cumulative" stroke="#3b82f6" fill="url(#eqGrad)" strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// DAILY PNL BAR CHART
// ═══════════════════════════════════════════════════════════════════════════════

function DailyPnlSection({ from, to }: { from: string; to: string }) {
  const { data, loading } = useDailySeries(from, to);

  if (loading) return <LoadingRow />;
  if (!data.length) return <NoDataRow label="Aucune analytics journalière" />;

  return (
    <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
      <h3 className="text-sm font-semibold text-gray-300 mb-3">PnL Journalier</h3>
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
          <XAxis dataKey="date" tick={{ fontSize: 10, fill: "#9ca3af" }} />
          <YAxis tick={{ fontSize: 10, fill: "#9ca3af" }} />
          <Tooltip contentStyle={{ backgroundColor: "#1f2937", border: "1px solid #374151", fontSize: 12 }} />
          <Bar dataKey="pnl" fill="#3b82f6"
            shape={(props: any) => {
              const { x, y, width, height, pnl } = props;
              const color = pnl >= 0 ? "#22c55e" : "#ef4444";
              return <rect x={x} y={y} width={width} height={Math.abs(height)} fill={color} rx={2} />;
            }}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// DECISION BREAKDOWN
// ═══════════════════════════════════════════════════════════════════════════════

const PIE_COLORS = ["#22c55e", "#ef4444", "#f59e0b", "#3b82f6", "#8b5cf6", "#ec4899", "#06b6d4", "#84cc16"];

function DecisionSection({ from, to }: { from: string; to: string }) {
  const { data, loading } = useDecisionBreakdown(from, to);

  if (loading) return <LoadingRow />;
  if (!data || data.total_signals === 0) return <NoDataRow label="Aucune décision enregistrée" />;

  const pieData = Object.entries(data.reject_reasons).slice(0, 8).map(([key, val]) => ({
    name: REJECT_REASON_LABELS[key] || key,
    value: val,
  }));

  const regimeData = Object.entries(data.by_regime).map(([regime, stats]) => {
    const s = stats as RegimeAcceptance;
    return {
      regime,
      total: s.total,
      accepted: s.accepted,
      rate: s.rate,
      rate_pct: (s.rate * 100).toFixed(1),
    };
  });

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      {/* Accept / Reject summary */}
      <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">Signaux: Accept vs Reject</h3>
        <div className="flex items-center gap-6 mb-4">
          <div className="flex items-center gap-2">
            <CheckCircle size={16} className="text-green-400" />
            <span className="text-green-400 font-bold text-lg">{data.accepted}</span>
            <span className="text-xs text-gray-400">acceptés</span>
          </div>
          <div className="flex items-center gap-2">
            <XCircle size={16} className="text-red-400" />
            <span className="text-red-400 font-bold text-lg">{data.rejected}</span>
            <span className="text-xs text-gray-400">rejetés</span>
          </div>
          <div className="text-xs text-gray-500">
            Taux: {((data.acceptance_rate ?? 0) * 100).toFixed(1)}%
          </div>
        </div>

        {/* Rejection reasons pie */}
        {pieData.length > 0 && (
          <div>
            <h4 className="text-xs text-gray-400 mb-2">Raisons de rejet</h4>
            <ResponsiveContainer width="100%" height={180}>
              <PieChart>
                <Pie data={pieData} dataKey="value" nameKey="name" cx="50%" cy="50%"
                  outerRadius={65} innerRadius={30} paddingAngle={2}>
                  {pieData.map((_, i) => (
                    <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip contentStyle={{ backgroundColor: "#1f2937", border: "1px solid #374151", fontSize: 11 }} />
                <Legend wrapperStyle={{ fontSize: 10 }} />
              </PieChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      {/* Per-regime acceptance */}
      <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">Taux d'acceptation par Régime</h3>
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={regimeData} layout="vertical">
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis type="number" domain={[0, 100]} tick={{ fontSize: 10, fill: "#9ca3af" }} unit="%" />
            <YAxis type="category" dataKey="regime" tick={{ fontSize: 10, fill: "#9ca3af" }} width={80} />
            <Tooltip contentStyle={{ backgroundColor: "#1f2937", border: "1px solid #374151", fontSize: 12 }}
              formatter={(v: number) => `${v}%`} />
            <Bar dataKey="rate_pct" fill="#3b82f6" radius={[0, 4, 4, 0]}
              label={{ position: "right", fontSize: 10, fill: "#9ca3af" }} />
          </BarChart>
        </ResponsiveContainer>

        {/* Symbol table */}
        <h4 className="text-xs text-gray-400 mt-4 mb-2">Par Symbole</h4>
        <div className="max-h-32 overflow-y-auto">
          <table className="w-full text-xs">
            <thead><tr className="text-gray-500 border-b border-gray-700">
              <th className="text-left py-1">Symbol</th>
              <th className="text-right">Total</th>
              <th className="text-right">Accept</th>
              <th className="text-right">Reject</th>
            </tr></thead>
            <tbody>
              {Object.entries(data.by_symbol).map(([sym, raw]) => {
                const s = raw as SymbolDecisionStats;
                return (
                <tr key={sym} className="border-b border-gray-700/50 text-gray-300">
                  <td className="py-1 font-mono">{sym}</td>
                  <td className="text-right">{s.total}</td>
                  <td className="text-right text-green-400">{s.accepted}</td>
                  <td className="text-right text-red-400">{s.rejected}</td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// EXECUTION QUALITY
// ═══════════════════════════════════════════════════════════════════════════════

function ExecutionSection({ from, to }: { from: string; to: string }) {
  const { data, loading } = useExecutionQuality(from, to);

  if (loading) return <LoadingRow />;
  if (!data || data.total_trades === 0) return <NoDataRow label="Aucun trade analytics" />;

  const modeData = Object.entries(data.execution_modes).map(([k, v]) => ({ name: k, value: v }));
  const brokerData = Object.entries(data.broker_stats || {}).map(([k, raw]) => {
    const v = raw as BrokerStats;
    return { broker: k, trades: v.trades ?? 0, pnl: v.pnl ?? 0, avg_fees: v.avg_fees ?? 0, fees_total: v.fees_total ?? 0 };
  });

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
      {/* Execution modes */}
      <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">Modes d'Exécution</h3>
        <ResponsiveContainer width="100%" height={160}>
          <PieChart>
            <Pie data={modeData} dataKey="value" nameKey="name" cx="50%" cy="50%"
              outerRadius={55} innerRadius={25}>
              {modeData.map((_, i) => (
                <Cell key={i} fill={["#22c55e", "#f59e0b", "#ef4444"][i] || PIE_COLORS[i]} />
              ))}
            </Pie>
            <Tooltip contentStyle={{ backgroundColor: "#1f2937", border: "1px solid #374151", fontSize: 11 }} />
            <Legend wrapperStyle={{ fontSize: 10 }} />
          </PieChart>
        </ResponsiveContainer>
      </div>

      {/* MAE/MFE */}
      <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">MAE / MFE</h3>
        <div className="space-y-3 text-xs">
          <div>
            <div className="text-gray-400 mb-1">MAE (Max Adverse Excursion)</div>
            <div className="flex gap-4">
              <span className="text-red-400">Avg: {data.mae.avg?.toFixed(4) ?? "—"}</span>
              <span className="text-gray-500">Min: {data.mae.min?.toFixed(4) ?? "—"}</span>
              <span className="text-gray-500">Max: {data.mae.max?.toFixed(4) ?? "—"}</span>
            </div>
          </div>
          <div>
            <div className="text-gray-400 mb-1">MFE (Max Favorable Excursion)</div>
            <div className="flex gap-4">
              <span className="text-green-400">Avg: {data.mfe.avg?.toFixed(4) ?? "—"}</span>
              <span className="text-gray-500">Min: {data.mfe.min?.toFixed(4) ?? "—"}</span>
              <span className="text-gray-500">Max: {data.mfe.max?.toFixed(4) ?? "—"}</span>
            </div>
          </div>
          <div>
            <div className="text-gray-400 mb-1">Durée</div>
            <div className="flex gap-4">
              <span className="text-blue-400">Avg: {data.duration.avg_sec != null ? `${Math.round(data.duration.avg_sec / 60)}min` : "—"}</span>
              <span className="text-gray-500">Min: {data.duration.min_sec != null ? `${Math.round(data.duration.min_sec / 60)}min` : "—"}</span>
              <span className="text-gray-500">Max: {data.duration.max_sec != null ? `${Math.round(data.duration.max_sec / 60)}min` : "—"}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Brokers */}
      <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">Performance par Broker</h3>
        <div className="space-y-2">
          {brokerData.map((b) => (
            <div key={b.broker} className="flex items-center justify-between text-xs border-b border-gray-700 pb-2">
              <span className="font-mono text-gray-300 uppercase">{b.broker}</span>
              <div className="flex gap-3">
                <span className="text-gray-400">{b.trades} trades</span>
                <span className={b.pnl >= 0 ? "text-green-400" : "text-red-400"}>
                  ${b.pnl.toFixed(2)}
                </span>
                <span className="text-gray-500">Fees: ${b.fees_total.toFixed(2)}</span>
              </div>
            </div>
          ))}
        </div>

        {/* A/B Testing */}
        {Object.keys(data.ab_testing).length > 0 && (
          <div className="mt-4">
            <h4 className="text-xs text-gray-400 mb-2">A/B Testing</h4>
            {Object.entries(data.ab_testing).map(([ver, raw]) => {
              const stats = raw as ABTestingStats;
              return (
              <div key={ver} className="flex items-center justify-between text-xs mb-1">
                <span className="font-mono text-gray-300">Version {ver}</span>
                <div className="flex gap-3">
                  <span className="text-gray-400">{stats.trades}t</span>
                  <span className={stats.pnl >= 0 ? "text-green-400" : "text-red-400"}>
                    ${stats.pnl.toFixed(2)}
                  </span>
                  <span className="text-blue-400">WR: {(stats.wr * 100).toFixed(0)}%</span>
                </div>
              </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// REGIME TIMELINE
// ═══════════════════════════════════════════════════════════════════════════════

function RegimeTimeline({ from, to }: { from: string; to: string }) {
  const { data, loading } = useRegimeHistory(from, to, 200);

  if (loading) return <LoadingRow />;
  if (!data.length) return <NoDataRow label="Aucun snapshot système" />;

  const chartData = data.map((s) => ({
    ts: new Date(s.ts).toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }),
    risk_factor: s.risk_factor,
    capital_utilization: s.capital_utilization ? s.capital_utilization * 100 : null,
    regime_num: Object.values(MarketRegime).indexOf(s.regime),
    regime: s.regime,
  }));

  return (
    <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
      <h3 className="text-sm font-semibold text-gray-300 mb-3">Timeline Régime & Risque</h3>
      <ResponsiveContainer width="100%" height={180}>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
          <XAxis dataKey="ts" tick={{ fontSize: 9, fill: "#9ca3af" }} interval="preserveStartEnd" />
          <YAxis tick={{ fontSize: 10, fill: "#9ca3af" }} />
          <Tooltip contentStyle={{ backgroundColor: "#1f2937", border: "1px solid #374151", fontSize: 11 }} />
          <Legend wrapperStyle={{ fontSize: 10 }} />
          <Line type="stepAfter" dataKey="risk_factor" stroke="#f59e0b" dot={false} strokeWidth={2} name="Risk Factor" />
          <Line type="monotone" dataKey="capital_utilization" stroke="#3b82f6" dot={false} strokeWidth={1.5} name="Cap. Util. %" />
        </LineChart>
      </ResponsiveContainer>

      {/* Regime badges (last 20) */}
      <div className="flex flex-wrap gap-1 mt-3">
        {data.slice(-20).map((s) => (
          <span key={s.id} className="text-[10px] px-2 py-0.5 rounded-full font-mono"
            style={{ backgroundColor: REGIME_COLORS[s.regime as MarketRegime] + "30", color: REGIME_COLORS[s.regime as MarketRegime] || "#9ca3af" }}>
            {s.regime}
          </span>
        ))}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// RISK EVENTS LOG
// ═══════════════════════════════════════════════════════════════════════════════

function RiskEventsLog({ from, to }: { from: string; to: string }) {
  const { data, loading } = useRiskEvents(from, to, 50);

  if (loading) return <LoadingRow />;
  if (!data.length) return <NoDataRow label="Aucun événement de risque" />;

  return (
    <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
      <h3 className="text-sm font-semibold text-gray-300 mb-3">Événements de Risque</h3>
      <div className="max-h-64 overflow-y-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-500 border-b border-gray-700">
              <th className="text-left py-1">Timestamp</th>
              <th className="text-left">Type</th>
              <th className="text-left">Sévérité</th>
              <th className="text-left">Symbol</th>
              <th className="text-left">Détail</th>
            </tr>
          </thead>
          <tbody>
            {data.map((e) => (
              <tr key={e.id} className="border-b border-gray-700/50">
                <td className="py-1.5 text-gray-400 font-mono">
                  {new Date(e.ts).toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                </td>
                <td className="text-gray-300">{e.event_type.replace(/_/g, " ")}</td>
                <td>
                  <span className="px-1.5 py-0.5 rounded text-[10px] font-bold"
                    style={{ backgroundColor: SEVERITY_COLORS[e.severity as RiskSeverity] + "30", color: SEVERITY_COLORS[e.severity as RiskSeverity] || "#9ca3af" }}>
                    {e.severity}
                  </span>
                </td>
                <td className="text-gray-400 font-mono">{e.symbol ?? "—"}</td>
                <td className="text-gray-500 max-w-48 truncate">
                  {e.detail ? JSON.stringify(e.detail).slice(0, 60) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// UTILITY COMPONENTS
// ═══════════════════════════════════════════════════════════════════════════════

function LoadingRow() {
  return (
    <div className="flex items-center gap-2 text-gray-400 text-sm py-8 justify-center">
      <RefreshCw size={14} className="animate-spin" /> Chargement...
    </div>
  );
}

function NoDataRow({ label }: { label: string }) {
  return (
    <div className="text-center text-gray-500 text-sm py-8 bg-gray-800/50 rounded-lg border border-gray-700/50">
      <Database size={20} className="mx-auto mb-2 opacity-40" />
      {label}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// EXECUTION BLOCKED SECTION (new — top priority)
// ═══════════════════════════════════════════════════════════════════════════════

const BLOCK_COLORS = [
  "#ef4444", "#f59e0b", "#3b82f6", "#8b5cf6", "#ec4899",
  "#10b981", "#f97316", "#6366f1", "#14b8a6", "#e11d48",
];

function ExecutionBlockedSection({ from, to }: { from: string; to: string }) {
  const { data, loading } = useExecutionBlocked(from, to);

  if (loading) return <div className="text-gray-500 text-sm py-4">Chargement...</div>;
  if (!data || data.total_attempts === 0) return <NoDataRow label="Aucune tentative d'exécution sur cette période" />;

  // Prepare pie data for reasons
  const reasonData = Object.entries(data.by_reason).map(([key, count]) => ({
    name: EXECUTION_BLOCK_LABELS[key] || key,
    value: count,
    key,
  }));

  // Prepare bar data for daily timeline
  const timelineData = Object.entries(data.daily_timeline as Record<string, DailyBlockedStats>)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, stats]) => ({
      date: day.slice(5), // "MM-DD"
      blocked: stats.blocked,
      passed: stats.passed,
      total: stats.total,
      rate: stats.total > 0 ? Math.round((stats.blocked / stats.total) * 100) : 0,
    }));

  // Per-symbol data
  const symbolData = Object.entries(data.by_symbol as Record<string, SymbolBlockedStats>)
    .sort(([, a], [, b]) => b.blocked - a.blocked)
    .slice(0, 10);

  return (
    <div className="space-y-4">
      {/* KPIs */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="bg-gray-800/50 rounded-lg border border-gray-700/50 p-3">
          <div className="text-xs text-gray-400 mb-1">Tentatives</div>
          <div className="text-xl font-bold text-white">{data.total_attempts}</div>
        </div>
        <div className="bg-gray-800/50 rounded-lg border border-red-900/30 p-3">
          <div className="text-xs text-red-400 mb-1 flex items-center gap-1"><XCircle size={12} />Bloqués</div>
          <div className="text-xl font-bold text-red-400">{data.blocked}</div>
        </div>
        <div className="bg-gray-800/50 rounded-lg border border-green-900/30 p-3">
          <div className="text-xs text-green-400 mb-1 flex items-center gap-1"><CheckCircle size={12} />Passés</div>
          <div className="text-xl font-bold text-green-400">{data.passed}</div>
        </div>
        <div className="bg-gray-800/50 rounded-lg border border-gray-700/50 p-3">
          <div className="text-xs text-gray-400 mb-1">Taux de blocage</div>
          <div className={`text-xl font-bold ${data.block_rate > 0.5 ? "text-red-400" : data.block_rate > 0.2 ? "text-yellow-400" : "text-green-400"}`}>
            {(data.block_rate * 100).toFixed(1)}%
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Reasons Pie */}
        <div className="bg-gray-800/50 rounded-lg border border-gray-700/50 p-4">
          <h3 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <AlertTriangle size={14} className="text-yellow-400" />
            Raisons de blocage
          </h3>
          {reasonData.length > 0 ? (
            <div className="flex items-center gap-4">
              <ResponsiveContainer width="50%" height={200}>
                <PieChart>
                  <Pie data={reasonData} dataKey="value" nameKey="name" cx="50%" cy="50%"
                    outerRadius={80} innerRadius={40} paddingAngle={2}>
                    {reasonData.map((_, i) => (
                      <Cell key={i} fill={BLOCK_COLORS[i % BLOCK_COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip contentStyle={{ background: "#1f2937", border: "1px solid #374151", borderRadius: 8, fontSize: 12 }} />
                </PieChart>
              </ResponsiveContainer>
              <div className="flex-1 space-y-1 text-xs">
                {reasonData.map((r, i) => (
                  <div key={r.key} className="flex items-center justify-between">
                    <div className="flex items-center gap-1.5">
                      <div className="w-2.5 h-2.5 rounded-sm" style={{ background: BLOCK_COLORS[i % BLOCK_COLORS.length] }} />
                      <span className="text-gray-300">{r.name}</span>
                    </div>
                    <span className="text-gray-400 font-mono">{r.value}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="text-gray-500 text-xs py-4 text-center">Aucun blocage</div>
          )}
        </div>

        {/* Per-symbol breakdown */}
        <div className="bg-gray-800/50 rounded-lg border border-gray-700/50 p-4">
          <h3 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <BarChart3 size={14} className="text-blue-400" />
            Blocages par symbole
          </h3>
          {symbolData.length > 0 ? (
            <div className="space-y-2">
              {symbolData.map(([sym, stats]) => {
                const s = stats as SymbolBlockedStats;
                const pct = s.total > 0 ? (s.blocked / s.total) * 100 : 0;
                return (
                  <div key={sym} className="flex items-center gap-2 text-xs">
                    <span className="text-gray-300 font-mono w-24 truncate">{sym}</span>
                    <div className="flex-1 h-3 bg-gray-700 rounded overflow-hidden">
                      <div className="h-full bg-red-500/80 rounded" style={{ width: `${pct}%` }} />
                    </div>
                    <span className="text-gray-400 w-16 text-right">{s.blocked}/{s.total}</span>
                    <span className={`w-12 text-right font-mono ${pct > 50 ? "text-red-400" : "text-gray-400"}`}>
                      {pct.toFixed(0)}%
                    </span>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="text-gray-500 text-xs py-4 text-center">Pas de données</div>
          )}
        </div>
      </div>

      {/* Daily timeline */}
      {timelineData.length > 0 && (
        <div className="bg-gray-800/50 rounded-lg border border-gray-700/50 p-4">
          <h3 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <Clock size={14} className="text-gray-400" />
            Timeline journalière
          </h3>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={timelineData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="date" stroke="#6b7280" tick={{ fontSize: 10 }} />
              <YAxis stroke="#6b7280" tick={{ fontSize: 10 }} />
              <Tooltip contentStyle={{ background: "#1f2937", border: "1px solid #374151", borderRadius: 8, fontSize: 12 }} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Bar dataKey="blocked" name="Bloqués" fill="#ef4444" stackId="a" radius={[0, 0, 0, 0]} />
              <Bar dataKey="passed" name="Passés" fill="#22c55e" stackId="a" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Recent blocked events table */}
      {data.recent_blocked.length > 0 && (
        <div className="bg-gray-800/50 rounded-lg border border-gray-700/50 p-4">
          <h3 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <XCircle size={14} className="text-red-400" />
            Derniers blocages
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-400 border-b border-gray-700">
                  <th className="py-1.5 px-2 text-left">Heure</th>
                  <th className="py-1.5 px-2 text-left">Symbole</th>
                  <th className="py-1.5 px-2 text-left">Raison</th>
                  <th className="py-1.5 px-2 text-left">Side</th>
                  <th className="py-1.5 px-2 text-right">Entry</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_blocked.slice(0, 20).map((evt) => (
                  <tr key={evt.id} className="border-b border-gray-700/50 hover:bg-gray-700/30">
                    <td className="py-1.5 px-2 text-gray-400 font-mono">
                      {evt.ts ? new Date(evt.ts).toLocaleString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit", day: "2-digit", month: "2-digit" }) : "—"}
                    </td>
                    <td className="py-1.5 px-2 text-white font-mono">{evt.symbol}</td>
                    <td className="py-1.5 px-2">
                      <span className="px-1.5 py-0.5 rounded bg-red-900/30 text-red-300 text-[10px]">
                        {EXECUTION_BLOCK_LABELS[evt.block_reason || ""] || evt.block_reason || "—"}
                      </span>
                    </td>
                    <td className="py-1.5 px-2 text-gray-300">{evt.side || "—"}</td>
                    <td className="py-1.5 px-2 text-right text-gray-400 font-mono">
                      {evt.entry_price ? evt.entry_price.toFixed(2) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// MAIN PAGE
// ═══════════════════════════════════════════════════════════════════════════════

type Tab = "overview" | "decisions" | "execution" | "blocked" | "regime" | "risk";

const DataExplorer: React.FC = () => {
  const [activeTab, setActiveTab] = useState<Tab>("overview");
  const d30 = new Date(); d30.setDate(d30.getDate() - 30);
  const [from, setFrom] = useState(formatDate(d30));
  const [to, setTo] = useState(formatDate(new Date()));

  const handleDateChange = (f: string, t: string) => { setFrom(f); setTo(t); };

  const tabs: { key: Tab; label: string; icon: React.ElementType }[] = [
    { key: "overview", label: "Vue Globale", icon: BarChart3 },
    { key: "decisions", label: "Décisions", icon: Filter },
    { key: "execution", label: "Exécution", icon: Zap },
    { key: "blocked", label: "Bloqués", icon: XCircle },
    { key: "regime", label: "Régime", icon: Activity },
    { key: "risk", label: "Risque", icon: Shield },
  ];

  return (
    <div className="p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <Database size={20} className="text-blue-400" />
          <h1 className="text-lg font-bold text-white">Data Explorer</h1>
          <span className="text-xs text-gray-500 bg-gray-800 px-2 py-0.5 rounded">Analytics Pipeline</span>
        </div>
        <DateRangeBar from={from} to={to} onChange={handleDateChange} />
      </div>

      {/* Tab bar */}
      <div className="flex gap-1 bg-gray-800/50 rounded-lg p-1">
        {tabs.map(({ key, label, icon: Icon }) => (
          <button key={key} onClick={() => setActiveTab(key)}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium transition-colors ${
              activeTab === key
                ? "bg-blue-600 text-white"
                : "text-gray-400 hover:text-gray-200 hover:bg-gray-700/50"
            }`}>
            <Icon size={13} />
            {label}
          </button>
        ))}
      </div>

      {/* Content */}
      {activeTab === "overview" && (
        <div className="space-y-4">
          <OverviewSection from={from} to={to} />
          <DailyPnlSection from={from} to={to} />
        </div>
      )}
      {activeTab === "decisions" && <DecisionSection from={from} to={to} />}
      {activeTab === "execution" && <ExecutionSection from={from} to={to} />}
      {activeTab === "blocked" && <ExecutionBlockedSection from={from} to={to} />}
      {activeTab === "regime" && <RegimeTimeline from={from} to={to} />}
      {activeTab === "risk" && <RiskEventsLog from={from} to={to} />}
    </div>
  );
};

export default DataExplorer;
