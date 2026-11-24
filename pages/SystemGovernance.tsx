import React, { useEffect, useState, useCallback } from 'react';
import {
  Shield, Activity, AlertTriangle, CheckCircle, XCircle,
  RefreshCw, Zap, PieChart, TrendingUp, BarChart2, Loader2,
  ChevronDown, ChevronUp, Info, DollarSign, Target, Clock,
  AlertOctagon, Layers
} from 'lucide-react';

// ─────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────

interface RegimeData {
  current_regime: string;
  objectives: Record<string, any>;
  risk_factor: number;
  regime_caps?: Record<string, any>;
  all_objectives?: Record<string, any>;
  regime_distribution_24h?: Record<string, number>;
  system_state?: {
    regime_confidence?: number;
    mode?: string;
    kill_switch_active?: boolean;
    open_positions?: number;
    daily_pnl?: number;
    equity?: number;
    capital_utilization?: number;
    snapshot_ts?: string;
  };
  error?: string;
  timestamp: string;
}

interface HealthGate {
  name: string;
  passed: boolean;
  critical: boolean;
  message: string;
  value?: any;
}

interface HealthData {
  all_passed: boolean;
  critical_passed: boolean;
  gates: HealthGate[];
  summary: string;
  live_metrics?: {
    open_positions: number;
    risk_events_1h: number;
    data_age_seconds: number;
    kill_switch: boolean;
    free_disk_mb: number;
    process_memory_mb: number;
  };
  error?: string;
  timestamp: string;
}

interface ChaosScenario {
  name: string;
  passed: boolean;
  description?: string;
  result?: string;
}

interface ChaosData {
  scenarios?: ChaosScenario[];
  summary?: { passed: number; failed: number; total: number };
  error?: string;
  timestamp: string;
}

interface SloData {
  summary?: { compliant: number; total: number; score: number };
  slos?: Record<string, any>;
  actuals?: Record<string, number>;
  sample_sizes?: { snapshots_24h: number; trades_24h: number; signals_24h: number };
  definitions?: Record<string, any>;
  error?: string;
  timestamp: string;
}

interface EdgeData {
  per_regime?: Record<string, {
    edge_score: number;
    grade: string;
    components?: { pf_ratio: number; dd_ratio: number; wr_ratio: number };
    real_stats?: { trades: number; winrate: number; pnl: number; profit_factor: number };
    objectives: any;
  }>;
  data_source?: string;
  error?: string;
  timestamp: string;
}

interface AllocationEntry {
  symbol: string;
  direction: string;
  entry: number;
  stop: number;
  tp: number;
  qty: number;
  leverage: number;
  notional: number;
  risk_usd: number;
  broker: string;
  ts_open: string;
}

interface AllocationData {
  parameters?: {
    equity: number;
    regime: string;
    capital_utilization: number;
  };
  allocations?: AllocationEntry[];
  total_risk?: number;
  open_count?: number;
  error?: string;
  timestamp: string;
}

// ─────────────────────────────────────────────────────────
// Theme & Helpers
// ─────────────────────────────────────────────────────────

const REGIME_COLORS: Record<string, string> = {
  TREND_UP: 'text-green-400',
  TREND_DOWN: 'text-red-400',
  RANGE: 'text-yellow-400',
  VOLATILE: 'text-orange-400',
  UNKNOWN: 'text-gray-400',
};

const REGIME_BG: Record<string, string> = {
  TREND_UP: 'bg-green-500/10 border-green-500/30',
  TREND_DOWN: 'bg-red-500/10 border-red-500/30',
  RANGE: 'bg-yellow-500/10 border-yellow-500/30',
  VOLATILE: 'bg-orange-500/10 border-orange-500/30',
  UNKNOWN: 'bg-gray-500/10 border-gray-500/30',
};

const GRADE_COLORS: Record<string, string> = {
  'A+': 'text-emerald-300', A: 'text-green-400',
  B: 'text-blue-400', C: 'text-yellow-400',
  D: 'text-orange-400', F: 'text-red-400',
};

async function fetchApi<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// ─────────────────────────────────────────────────────────
// Sub-components
// ─────────────────────────────────────────────────────────

const Card: React.FC<{
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  collapsible?: boolean;
  expanded?: boolean;
  onToggle?: () => void;
  badge?: React.ReactNode;
}> = ({ title, icon, children, className = '', collapsible, expanded, onToggle, badge }) => (
  <div className={`bg-[#1a1a2e] border border-[#2a2a4a] rounded-xl p-5 relative ${className}`}>
    <div className="flex items-center justify-between mb-4">
      <div className="flex items-center gap-2">
        {icon}
        <h3 className="text-white font-semibold text-sm uppercase tracking-wide">{title}</h3>
        {badge}
      </div>
      {collapsible && onToggle && (
        <button onClick={onToggle} className="text-gray-500 hover:text-white transition-colors">
          {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
        </button>
      )}
    </div>
    {children}
  </div>
);

const StatusBadge: React.FC<{ ok: boolean; label: string }> = ({ ok, label }) => (
  <span className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium ${ok ? 'bg-green-500/15 text-green-400 border border-green-500/20' : 'bg-red-500/15 text-red-400 border border-red-500/20'}`}>
    {ok ? <CheckCircle size={12} /> : <XCircle size={12} />}
    {label}
  </span>
);

const MetricBox: React.FC<{ label: string; value: string | number; sub?: string; color?: string }> = ({
  label, value, sub, color = 'text-white',
}) => (
  <div className="bg-[#0f0f1e] rounded-lg p-3 border border-[#2a2a4a]/50">
    <div className="text-gray-500 text-[10px] uppercase tracking-wider font-medium">{label}</div>
    <div className={`${color} font-bold text-lg mt-0.5`}>{value}</div>
    {sub && <div className="text-gray-600 text-[10px] mt-0.5">{sub}</div>}
  </div>
);

const ProgressBar: React.FC<{ value: number; max?: number; color?: string }> = ({
  value, max = 100, color = 'bg-cyan-500',
}) => {
  const pct = Math.min(100, Math.max(0, (value / max) * 100));
  return (
    <div className="w-full h-1.5 bg-[#0f0f1e] rounded-full overflow-hidden">
      <div className={`h-full rounded-full transition-all duration-500 ${color}`} style={{ width: `${pct}%` }} />
    </div>
  );
};

// ─────────────────────────────────────────────────────────
// Main Component
// ─────────────────────────────────────────────────────────

const SystemGovernance: React.FC = () => {
  const [regime, setRegime] = useState<RegimeData | null>(null);
  const [health, setHealth] = useState<HealthData | null>(null);
  const [chaos, setChaos] = useState<ChaosData | null>(null);
  const [slo, setSlo] = useState<SloData | null>(null);
  const [edge, setEdge] = useState<EdgeData | null>(null);
  const [allocation, setAllocation] = useState<AllocationData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedSections, setExpandedSections] = useState<Record<string, boolean>>({
    regime: true, health: true, slo: true, chaos: false, edge: true, allocation: true,
  });

  const toggleSection = (key: string) =>
    setExpandedSections(prev => ({ ...prev, [key]: !prev[key] }));

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [r, h, s, c, e, a] = await Promise.all([
        fetchApi<RegimeData>('/api/governance/regime'),
        fetchApi<HealthData>('/api/governance/health'),
        fetchApi<SloData>('/api/governance/slo'),
        fetchApi<ChaosData>('/api/governance/chaos'),
        fetchApi<EdgeData>('/api/governance/edge'),
        fetchApi<AllocationData>('/api/governance/allocation'),
      ]);
      setRegime(r);
      setHealth(h);
      setSlo(s);
      setChaos(c);
      setEdge(e);
      setAllocation(a);
    } catch (err: any) {
      setError(err.message || 'Erreur de chargement');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
    const interval = setInterval(fetchAll, 30_000);
    return () => clearInterval(interval);
  }, [fetchAll]);

  if (loading && !regime) {
    return (
      <div className="flex items-center justify-center h-full min-h-[400px]">
        <Loader2 className="animate-spin text-cyan-400" size={32} />
        <span className="ml-3 text-gray-400">Chargement de la gouvernance…</span>
      </div>
    );
  }

  const pnlColor = (v: number | undefined | null) => !v ? 'text-gray-400' : v > 0 ? 'text-green-400' : v < 0 ? 'text-red-400' : 'text-gray-400';
  const fmtUsd = (v: number | undefined | null) => v != null ? `$${v.toLocaleString('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—';

  return (
    <div className="p-4 lg:p-6 space-y-4 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Shield className="text-cyan-400" size={24} />
          <h2 className="text-xl font-bold text-white">Gouvernance Système</h2>
          {regime?.system_state?.mode && (
            <span className={`text-[10px] px-2 py-0.5 rounded-full font-bold uppercase tracking-wider ${
              regime.system_state.mode === 'LIVE' ? 'bg-green-500/20 text-green-400 border border-green-500/30' :
              'bg-yellow-500/20 text-yellow-400 border border-yellow-500/30'
            }`}>{regime.system_state.mode}</span>
          )}
        </div>
        <button
          onClick={fetchAll}
          disabled={loading}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[#1a1a2e] border border-[#2a2a4a] text-gray-300 hover:text-white hover:border-cyan-500/50 transition-colors text-sm disabled:opacity-50"
        >
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          Actualiser
        </button>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 text-red-400 text-sm flex items-center gap-2">
          <AlertTriangle size={16} /> {error}
        </div>
      )}

      {/* ── QUICK OVERVIEW STRIP ─────────────────────────── */}
      {regime?.system_state && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <MetricBox
            label="Équité"
            value={fmtUsd(regime.system_state.equity)}
            color="text-cyan-400"
          />
          <MetricBox
            label="PnL Journalier"
            value={fmtUsd(regime.system_state.daily_pnl)}
            color={pnlColor(regime.system_state.daily_pnl)}
          />
          <MetricBox
            label="Positions Ouvertes"
            value={regime.system_state.open_positions ?? 0}
            color="text-white"
          />
          <MetricBox
            label="Utilisation Capital"
            value={`${((regime.system_state.capital_utilization ?? 0) * 100).toFixed(1)}%`}
            color="text-purple-400"
          />
          <MetricBox
            label="Kill Switch"
            value={regime.system_state.kill_switch_active ? 'ACTIF' : 'Inactif'}
            color={regime.system_state.kill_switch_active ? 'text-red-400' : 'text-green-400'}
          />
        </div>
      )}

      {/* ── REGIME ───────────────────────────────────────── */}
      <Card
        title="Régime de Marché"
        icon={<TrendingUp className="text-cyan-400" size={18} />}
        collapsible expanded={expandedSections.regime} onToggle={() => toggleSection('regime')}
      >
        {regime && (
          <div className="space-y-4">
            <div className="flex items-center gap-4 flex-wrap">
              <div className={`px-5 py-3 rounded-xl border text-lg font-bold ${REGIME_BG[regime.current_regime] || REGIME_BG.UNKNOWN}`}>
                <span className={REGIME_COLORS[regime.current_regime] || REGIME_COLORS.UNKNOWN}>
                  {regime.current_regime}
                </span>
              </div>
              <div className="space-y-1">
                <div className="text-gray-400 text-sm">
                  Facteur de risque : <span className="text-white font-semibold">{regime.risk_factor?.toFixed(2) ?? '—'}</span>
                </div>
                {regime.system_state?.regime_confidence != null && (
                  <div className="text-gray-400 text-sm">
                    Confiance : <span className="text-white font-semibold">{(regime.system_state.regime_confidence * 100).toFixed(0)}%</span>
                  </div>
                )}
              </div>
            </div>

            {/* Regime distribution 24h */}
            {expandedSections.regime && regime.regime_distribution_24h && Object.keys(regime.regime_distribution_24h).length > 0 && (
              <div>
                <div className="text-gray-500 text-xs uppercase tracking-wider mb-2">Distribution 24h</div>
                <div className="flex gap-1 h-3 rounded-full overflow-hidden bg-[#0f0f1e]">
                  {(() => {
                    const dist = regime.regime_distribution_24h!;
                    const vals = Object.values(dist) as number[];
                    const total = vals.reduce((a, b) => a + b, 0);
                    return Object.entries(dist).map(([r, count]: [string, number]) => (
                      <div
                        key={r}
                        className={`h-full transition-all ${
                          r === 'TREND_UP' ? 'bg-green-500' :
                          r === 'TREND_DOWN' ? 'bg-red-500' :
                          r === 'VOLATILE' ? 'bg-orange-500' :
                          r === 'RANGE' ? 'bg-yellow-500' : 'bg-gray-500'
                        }`}
                        style={{ width: `${(count / total) * 100}%` }}
                        title={`${r}: ${count} snapshots (${((count / total) * 100).toFixed(0)}%)`}
                      />
                    ));
                  })()}
                </div>
                <div className="flex gap-3 mt-1.5 flex-wrap">
                  {Object.entries(regime.regime_distribution_24h!).map(([r, count]) => (
                    <span key={r} className={`text-[10px] ${REGIME_COLORS[r] || 'text-gray-400'}`}>
                      {r}: {count}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {expandedSections.regime && regime.objectives && Object.keys(regime.objectives).length > 0 && (
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {Object.entries(regime.objectives).map(([key, val]) => (
                  <MetricBox
                    key={key}
                    label={key.replace(/_/g, ' ')}
                    value={typeof val === 'number' ? val.toFixed(2) : String(val)}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </Card>

      {/* ── HEALTH GATES ─────────────────────────────────── */}
      <Card
        title="Portes de Santé"
        icon={<Activity className={health?.all_passed ? 'text-green-400' : 'text-red-400'} size={18} />}
        collapsible expanded={expandedSections.health} onToggle={() => toggleSection('health')}
        badge={health && (
          <StatusBadge ok={health.all_passed} label={health.all_passed ? 'OK' : 'KO'} />
        )}
      >
        {health && (
          <div className="space-y-3">
            <div className="flex items-center gap-3 flex-wrap">
              <StatusBadge ok={health.critical_passed} label={health.critical_passed ? 'Critique OK' : 'Critique KO'} />
              {typeof health.summary === 'string' && (
                <span className="text-gray-400 text-xs">{health.summary}</span>
              )}
            </div>

            {/* Live metrics strip */}
            {health.live_metrics && (
              <div className="grid grid-cols-3 md:grid-cols-6 gap-2">
                <div className="bg-[#0f0f1e] rounded-lg p-2 text-center">
                  <div className="text-[9px] text-gray-600 uppercase">Positions</div>
                  <div className="text-white font-bold text-sm">{health.live_metrics.open_positions}</div>
                </div>
                <div className="bg-[#0f0f1e] rounded-lg p-2 text-center">
                  <div className="text-[9px] text-gray-600 uppercase">Risques 1h</div>
                  <div className={`font-bold text-sm ${health.live_metrics.risk_events_1h > 0 ? 'text-orange-400' : 'text-green-400'}`}>
                    {health.live_metrics.risk_events_1h}
                  </div>
                </div>
                <div className="bg-[#0f0f1e] rounded-lg p-2 text-center">
                  <div className="text-[9px] text-gray-600 uppercase">Âge données</div>
                  <div className={`font-bold text-sm ${health.live_metrics.data_age_seconds > 120 ? 'text-red-400' : 'text-green-400'}`}>
                    {health.live_metrics.data_age_seconds < 60
                      ? `${health.live_metrics.data_age_seconds.toFixed(0)}s`
                      : `${(health.live_metrics.data_age_seconds / 60).toFixed(1)}m`}
                  </div>
                </div>
                <div className="bg-[#0f0f1e] rounded-lg p-2 text-center">
                  <div className="text-[9px] text-gray-600 uppercase">Kill Switch</div>
                  <div className={`font-bold text-sm ${health.live_metrics.kill_switch ? 'text-red-400' : 'text-green-400'}`}>
                    {health.live_metrics.kill_switch ? 'ACTIF' : 'OFF'}
                  </div>
                </div>
                <div className="bg-[#0f0f1e] rounded-lg p-2 text-center">
                  <div className="text-[9px] text-gray-600 uppercase">Disque</div>
                  <div className="text-white font-bold text-sm">{(health.live_metrics.free_disk_mb / 1024).toFixed(1)} GB</div>
                </div>
                <div className="bg-[#0f0f1e] rounded-lg p-2 text-center">
                  <div className="text-[9px] text-gray-600 uppercase">Mémoire</div>
                  <div className="text-white font-bold text-sm">{health.live_metrics.process_memory_mb.toFixed(0)} MB</div>
                </div>
              </div>
            )}

            {expandedSections.health && health.gates && (
              <div className="space-y-1.5 mt-2">
                {health.gates.map((gate, i) => (
                  <div key={i} className={`flex items-center justify-between p-2.5 rounded-lg ${gate.passed ? 'bg-green-500/5' : 'bg-red-500/10'} border ${gate.passed ? 'border-green-500/10' : 'border-red-500/20'}`}>
                    <div className="flex items-center gap-2">
                      {gate.passed ? <CheckCircle size={14} className="text-green-400" /> : <XCircle size={14} className="text-red-400" />}
                      <span className="text-sm text-gray-200">{gate.name || gate.message}</span>
                      {gate.critical && <span className="text-[10px] text-yellow-500 border border-yellow-600/30 px-1.5 py-0.5 rounded-full uppercase font-bold">Critique</span>}
                    </div>
                    {gate.value !== undefined && <span className="text-xs text-gray-500 font-mono">{String(gate.value)}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </Card>

      {/* ── SLO COMPLIANCE ───────────────────────────────── */}
      <Card
        title="Conformité SLO"
        icon={<BarChart2 className="text-blue-400" size={18} />}
        collapsible expanded={expandedSections.slo} onToggle={() => toggleSection('slo')}
        badge={slo?.summary && (
          <StatusBadge
            ok={slo.summary.compliant === slo.summary.total}
            label={`${slo.summary.compliant ?? 0}/${slo.summary.total ?? 0}`}
          />
        )}
      >
        {slo && (
          <div className="space-y-3">
            {slo.summary && (
              <div className="flex items-center gap-4 flex-wrap">
                <div className="text-gray-400 text-sm">
                  Score : <span className={`font-bold text-lg ${
                    (slo.summary.score ?? 0) >= 80 ? 'text-green-400' :
                    (slo.summary.score ?? 0) >= 50 ? 'text-yellow-400' : 'text-red-400'
                  }`}>{(slo.summary.score ?? 0).toFixed(1)}%</span>
                </div>
                <ProgressBar
                  value={slo.summary.score ?? 0}
                  color={(slo.summary.score ?? 0) >= 80 ? 'bg-green-500' : (slo.summary.score ?? 0) >= 50 ? 'bg-yellow-500' : 'bg-red-500'}
                />
              </div>
            )}

            {/* Real SLO actuals */}
            {slo.actuals && expandedSections.slo && (
              <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
                <MetricBox
                  label="Disponibilité"
                  value={`${slo.actuals.availability?.toFixed(2) ?? '—'}%`}
                  color={(slo.actuals.availability ?? 0) >= 99.5 ? 'text-green-400' : 'text-red-400'}
                />
                <MetricBox
                  label="Fraîcheur données"
                  value={`${slo.actuals.data_freshness?.toFixed(0) ?? '—'}s`}
                  color={(slo.actuals.data_freshness ?? 999) <= 30 ? 'text-green-400' : 'text-orange-400'}
                />
                <MetricBox
                  label="Taux d'erreur"
                  value={`${slo.actuals.error_rate?.toFixed(2) ?? '—'}%`}
                  color={(slo.actuals.error_rate ?? 100) <= 1 ? 'text-green-400' : 'text-red-400'}
                />
                <MetricBox
                  label="Conformité risque"
                  value={`${slo.actuals.risk_compliance?.toFixed(0) ?? '—'}%`}
                  color="text-green-400"
                />
                <MetricBox
                  label="Échantillons 24h"
                  value={slo.sample_sizes?.snapshots_24h ?? 0}
                  sub={`${slo.sample_sizes?.trades_24h ?? 0} trades, ${slo.sample_sizes?.signals_24h ?? 0} signaux`}
                  color="text-gray-300"
                />
              </div>
            )}

            {expandedSections.slo && slo.definitions && (
              <div className="space-y-1.5 mt-2">
                {Object.entries(slo.definitions).map(([key, def]: [string, any]) => {
                  const actual = slo.actuals?.[key];
                  const target = typeof def === 'object' ? def.target : def;
                  return (
                    <div key={key} className="flex items-center justify-between p-2.5 rounded-lg bg-[#0f0f1e] border border-[#2a2a4a]">
                      <div className="flex items-center gap-2">
                        <Target size={12} className="text-blue-400" />
                        <span className="text-sm text-gray-200">{key.replace(/_/g, ' ')}</span>
                      </div>
                      <div className="flex items-center gap-3">
                        {actual != null && (
                          <span className="text-xs text-white font-mono">actuel: {typeof actual === 'number' ? actual.toFixed(2) : String(actual)}</span>
                        )}
                        <span className="text-xs text-gray-500 font-mono">
                          cible: {typeof target === 'number' ? target : JSON.stringify(target)}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </Card>

      {/* ── EDGE SCORES ──────────────────────────────────── */}
      <Card
        title="Scores Edge par Régime"
        icon={<PieChart className="text-purple-400" size={18} />}
        collapsible expanded={expandedSections.edge} onToggle={() => toggleSection('edge')}
        badge={edge?.data_source && (
          <span className={`text-[10px] px-2 py-0.5 rounded-full ${
            edge.data_source === 'db' ? 'bg-green-500/15 text-green-400 border border-green-500/20' : 'bg-gray-500/15 text-gray-400 border border-gray-500/20'
          }`}>{edge.data_source === 'db' ? 'Données réelles' : 'Valeurs par défaut'}</span>
        )}
      >
        {edge?.per_regime && (
          <div className={expandedSections.edge ? '' : 'max-h-16 overflow-hidden'}>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
              {Object.entries(edge.per_regime).map(([r, data]: [string, any]) => (
                <div key={r} className={`rounded-xl border p-4 ${REGIME_BG[r] || REGIME_BG.UNKNOWN}`}>
                  <div className="flex items-center justify-between mb-2">
                    <span className={`text-xs font-bold uppercase ${REGIME_COLORS[r] || REGIME_COLORS.UNKNOWN}`}>{r}</span>
                    <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${GRADE_COLORS[data.grade] || 'text-gray-400'} bg-black/20`}>
                      {data.grade}
                    </span>
                  </div>
                  <div className="text-white text-2xl font-bold mb-2">{data.edge_score}</div>
                  <ProgressBar
                    value={data.edge_score}
                    color={data.edge_score >= 70 ? 'bg-green-500' : data.edge_score >= 40 ? 'bg-yellow-500' : 'bg-red-500'}
                  />
                  {data.real_stats && expandedSections.edge && (
                    <div className="mt-3 space-y-1 text-[11px]">
                      <div className="flex justify-between">
                        <span className="text-gray-500">Trades</span>
                        <span className="text-gray-300">{data.real_stats.trades}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-500">Winrate</span>
                        <span className={data.real_stats.winrate >= 50 ? 'text-green-400' : 'text-red-400'}>
                          {data.real_stats.winrate.toFixed(1)}%
                        </span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-500">PnL</span>
                        <span className={pnlColor(data.real_stats.pnl)}>{fmtUsd(data.real_stats.pnl)}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-500">PF</span>
                        <span className="text-gray-300">{data.real_stats.profit_factor?.toFixed(2) ?? '—'}</span>
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>

      {/* ── CAPITAL ALLOCATION ───────────────────────────── */}
      <Card
        title="Allocation de Capital"
        icon={<DollarSign className="text-cyan-400" size={18} />}
        collapsible expanded={expandedSections.allocation} onToggle={() => toggleSection('allocation')}
        badge={allocation?.open_count != null && (
          <span className="text-[10px] px-2 py-0.5 rounded-full bg-cyan-500/15 text-cyan-400 border border-cyan-500/20">
            {allocation.open_count} position{allocation.open_count !== 1 ? 's' : ''}
          </span>
        )}
      >
        {allocation && (
          <div className="space-y-3">
            {allocation.parameters && (
              <div className="flex items-center gap-4 text-sm text-gray-400 flex-wrap">
                <span>Équité : <span className="text-cyan-400 font-bold">{fmtUsd(allocation.parameters.equity)}</span></span>
                {allocation.parameters.capital_utilization != null && (
                  <span>Utilisation : <span className="text-white font-medium">{allocation.parameters.capital_utilization}%</span></span>
                )}
                <span>Régime : <span className={`font-medium ${REGIME_COLORS[allocation.parameters.regime] || 'text-white'}`}>{allocation.parameters.regime}</span></span>
              </div>
            )}

            {expandedSections.allocation && allocation.allocations && allocation.allocations.length > 0 ? (
              <div className="space-y-2 mt-2">
                {allocation.allocations.map((alloc, i) => (
                  <div key={i} className="flex items-center justify-between p-3 rounded-lg bg-[#0f0f1e] border border-[#2a2a4a] hover:border-[#3a3a6a] transition-colors">
                    <div className="flex items-center gap-3">
                      <span className="text-sm text-white font-bold">{alloc.symbol}</span>
                      <span className={`text-[10px] px-2 py-0.5 rounded-full font-bold ${alloc.direction === 'LONG' ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'}`}>
                        {alloc.direction}
                      </span>
                      {alloc.broker && (
                        <span className="text-[10px] text-gray-500 border border-gray-700 px-1.5 py-0.5 rounded">{alloc.broker}</span>
                      )}
                    </div>
                    <div className="flex items-center gap-4 text-xs">
                      <span className="text-gray-400">×{alloc.leverage}</span>
                      <span className="text-gray-400">Notionnel: <span className="text-white">{fmtUsd(alloc.notional)}</span></span>
                      <span className="text-gray-400">Risque: <span className="text-orange-400 font-medium">{fmtUsd(alloc.risk_usd)}</span></span>
                      {alloc.ts_open && (
                        <span className="text-gray-600">{new Date(alloc.ts_open).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ) : expandedSections.allocation ? (
              <div className="text-center py-6 text-gray-600 text-sm">
                <Layers size={24} className="mx-auto mb-2 opacity-50" />
                Aucune position ouverte
              </div>
            ) : null}

            {allocation.total_risk != null && allocation.total_risk > 0 && (
              <div className="flex items-center justify-between text-xs text-gray-500 pt-2 border-t border-[#2a2a4a]">
                <span>Risque total</span>
                <span className="text-orange-400 font-bold">{fmtUsd(allocation.total_risk)}</span>
              </div>
            )}
          </div>
        )}
      </Card>

      {/* ── CHAOS SIMULATION ─────────────────────────────── */}
      <Card
        title="Simulation Chaos"
        icon={<Zap className="text-orange-400" size={18} />}
        collapsible expanded={expandedSections.chaos} onToggle={() => toggleSection('chaos')}
        badge={chaos?.summary && (
          <StatusBadge ok={chaos.summary.failed === 0} label={`${chaos.summary.passed}/${chaos.summary.total}`} />
        )}
      >
        {chaos && (
          <div className="space-y-3">
            {chaos.summary && (
              <div className="flex items-center gap-3">
                {chaos.summary.failed > 0 && (
                  <span className="text-red-400 text-xs font-medium">{chaos.summary.failed} échec(s)</span>
                )}
              </div>
            )}
            {expandedSections.chaos && chaos.scenarios && (
              <div className="space-y-1.5 mt-2">
                {chaos.scenarios.map((sc, i) => (
                  <div key={i} className={`flex items-center justify-between p-2.5 rounded-lg ${sc.passed ? 'bg-green-500/5 border-green-500/10' : 'bg-red-500/10 border-red-500/20'} border`}>
                    <div className="flex items-center gap-2">
                      {sc.passed ? <CheckCircle size={14} className="text-green-400" /> : <XCircle size={14} className="text-red-400" />}
                      <span className="text-sm text-gray-200">{sc.name}</span>
                    </div>
                    {sc.description && <span className="text-xs text-gray-500">{sc.description}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </Card>

      {/* Footer */}
      <div className="text-center text-xs text-gray-600 pt-2 flex items-center justify-center gap-2">
        <Clock size={10} />
        Dernière mise à jour : {regime?.timestamp ? new Date(regime.timestamp).toLocaleTimeString('fr-FR') : '—'}
        {' | '}Auto-refresh : 30s
        {regime?.system_state?.snapshot_ts && (
          <> | Snapshot DB : {new Date(regime.system_state.snapshot_ts).toLocaleTimeString('fr-FR')}</>
        )}
      </div>
    </div>
  );
};

export default SystemGovernance;
