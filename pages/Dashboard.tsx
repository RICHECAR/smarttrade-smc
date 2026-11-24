import React, { useEffect, useMemo, useState, useCallback } from "react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import { Activity, ArrowUpRight, ArrowDownRight, TrendingUp, TrendingDown, Clock, Calendar, AlertTriangle, RefreshCw, Wallet, Server, Zap, Play, Square, BarChart3, History, ShieldAlert } from "lucide-react";

// Import new components
import PerformanceAnalyticsPanel from "../components/PerformanceAnalyticsPanel";
import PositionsTable from "../components/PositionsTable";

type Summary = {
  pnl: number;
  win_rate: number;
  avg_rr: number;
  trades: number;
  max_dd: number;
  profit_factor: number;
  wins?: number;
  losses?: number;
};

type Trade = {
  id: string;
  ts: string;
  ts_open?: string;
  symbol: string;
  side: "long" | "short";
  entry?: number;
  stop?: number;
  tp?: number;
  rr: number;
  pnl: number;
  session?: string;
  preset?: string;
  result?: "win" | "loss" | "be";
  status?: "open" | "closed";
  broker?: string;
};

type HeatCell = {
  dow: number;   // 0=Sun ... 6=Sat
  hour: number;  // 0..23
  trades: number;
  pnl: number;
  win_rate: number;
};

type RRPoint = {
  ts: string;
  rr: number;
};

// Multi-broker types
type BrokerBalance = {
  available: number;
  total: number;
  currency: string;
  margin_used: number;
  unrealized_pnl: number;
};

type BrokerStatus = {
  connected: boolean;
  mode?: string;
  server?: string;
  balance?: BrokerBalance;
  error?: string;
};

type MultiBrokerStatus = {
  running: boolean;
  brokers: {
    bitget?: BrokerStatus;
    kraken?: BrokerStatus;
    mt5?: BrokerStatus;
  };
  statistics?: {
    total_signals?: number;
    total_orders?: number;
    successful_orders?: number;
    failed_orders?: number;
  };
  timestamp?: string;
};

type BrokerPosition = {
  symbol: string;
  side: string;
  size: number;
  entry_price: number;
  unrealized_pnl: number;
  leverage: number;
};

type MultiBrokerPositions = {
  bitget?: BrokerPosition[] | { error: string };
  kraken?: BrokerPosition[] | { error: string };
};

// Balance history types
type BalanceHistoryRecord = {
  id: number;
  broker: string;
  available: number;
  total: number;
  currency: string;
  margin_used: number;
  unrealized_pnl: number;
  timestamp: string;
};

type BalanceTotalStatus = "complete" | "partial" | "unavailable";

type BalanceHistoryPoint = {
  ts: number;
  bitget?: number;
  kraken?: number;
  total?: number;
  partialTotal?: number;
  totalStatus: BalanceTotalStatus;
};

type ExecutionHealth = {
  execution_mode: string;
  primary_broker: string;
  secondary_broker: string;
  cooldown_active: boolean;
  last_orphan_detected: string;
  orphan_count: number;
  late_fill_count: number;
  stale_rejection_count: number;
  kill_switch_active: boolean;
};

const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function fmt(n: number | undefined | null, decimals: number = 2): string {
  if (n === undefined || n === null) return "—";
  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: decimals,
    minimumFractionDigits: decimals
  }).format(n);
}

type DateRange = {
  from: string;
  to: string;
};

export default function Dashboard() {
  // API base: use relative paths so Vite proxy (dev) and same-origin (prod) work.
  // Override via VITE_API_URL only for cross-origin deployments.
  const API = (import.meta as any).env?.VITE_API_URL ?? "";
  // WS: derive from current page location so it works everywhere
  const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const WS_BASE = (import.meta as any).env?.VITE_WS_URL
    ? ((import.meta as any).env.VITE_WS_URL as string).replace(/\/ws\/?$/, "")
    : `${wsProtocol}//${window.location.host}`;

  // State
  const [summary, setSummary] = useState<Summary | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [rrSeries, setRrSeries] = useState<RRPoint[]>([]);
  const [heat, setHeat] = useState<HeatCell[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(false);

  // Multi-broker state
  const [multiBrokerStatus, setMultiBrokerStatus] = useState<MultiBrokerStatus | null>(null);
  const [multiBrokerPositions, setMultiBrokerPositions] = useState<MultiBrokerPositions | null>(null);
  const [multiBrokerLoading, setMultiBrokerLoading] = useState(false);
  const [tradingLoopRunning, setTradingLoopRunning] = useState(false);

  // Balance history state
  const [balanceHistory, setBalanceHistory] = useState<BalanceHistoryPoint[]>([]);
  const [executionHealth, setExecutionHealth] = useState<ExecutionHealth | null>(null);

  // Consistency guard: cross-check /summary vs /brokers PnL
  const [pnlMismatch, setPnlMismatch] = useState<string | null>(null);

  // Date range (default: last 30 days)
  const [range, setRange] = useState<DateRange>(() => {
    const to = new Date();
    const from = new Date();
    from.setDate(to.getDate() - 30);
    return {
      from: from.toISOString().slice(0, 10),
      to: to.toISOString().slice(0, 10),
    };
  });

  // Fetch all data
  async function loadAll() {
    setLoading(true);
    setError(null);

    const qs = `from=${range.from}&to=${range.to}`;

    try {
      const [s, t, r, h] = await Promise.all([
        fetch(`${API}/api/stats/summary?${qs}`).then(res => {
          if (!res.ok) throw new Error("API not ready");
          return res.json();
        }),
        fetch(`${API}/api/live/trades?${qs}`).then(res => {
          if (!res.ok) throw new Error("API not ready");
          return res.json();
        }),
        fetch(`${API}/api/stats/rr-series?${qs}`).then(res => {
          if (!res.ok) throw new Error(`RR: ${res.status}`);
          return res.json();
        }),
        fetch(`${API}/api/stats/heatmap?${qs}`).then(res => {
          if (!res.ok) throw new Error(`Heatmap: ${res.status}`);
          return res.json();
        }),
      ]);

      setSummary(s);
      setTrades(t);
      setRrSeries(r);
      setHeat(h);
    } catch (err) {
      console.error("[Dashboard] Load error:", err);
      setError(err instanceof Error ? err.message : "Failed to load data");
    } finally {
      setLoading(false);
    }
  }

  // Load multi-broker status and positions
  const loadMultiBrokerData = useCallback(async () => {
    setMultiBrokerLoading(true);

    try {
      const [statusRes, positionsRes, historyRes, healthRes] = await Promise.all([
        fetch(`${API}/api/multi/status`).catch(() => null),
        fetch(`${API}/api/multi/positions`).catch(() => null),
        fetch(`${API}/api/multi/balance/history?days=7&limit=200`).catch(() => null),
        fetch(`${API}/api/health/execution`).catch(() => null),
      ]);

      if (healthRes?.ok) {
        setExecutionHealth(await healthRes.json());
      }

      if (statusRes?.ok) {
        const status = await statusRes.json();
        setMultiBrokerStatus(status);
        setTradingLoopRunning(status.running ?? false);
      }

      if (positionsRes?.ok) {
        const positions = await positionsRes.json();
        setMultiBrokerPositions(positions.positions ?? null);
      }

      // Process balance history for chart (keep numeric UTC epoch ms; format only at render time)
      if (historyRes?.ok) {
        const data = await historyRes.json();
        const records: BalanceHistoryRecord[] = data.history ?? [];

        type InternalBucket = {
          ts: number;
          bitget?: number;
          bitgetTs?: number;
          kraken?: number;
          krakenTs?: number;
        };

        // Group supported broker records into 5s buckets, keeping the most recent valid
        // measurement per broker regardless of input ordering.
        const grouped = new Map<number, InternalBucket>();

        for (const r of records) {
          if (r.broker !== "bitget" && r.broker !== "kraken") continue;
          if (!r.timestamp) continue;
          if (typeof r.total !== "number" || !Number.isFinite(r.total)) continue;

          let ms = Date.parse(r.timestamp);
          if (Number.isNaN(ms)) {
            const safeTimestamp = r.timestamp.replace(/\.\d+/, '');
            ms = Date.parse(safeTimestamp);
          }
          if (!Number.isFinite(ms)) continue;

          const bucketMs = Math.floor(ms / 5000) * 5000;
          let bucket = grouped.get(bucketMs);
          if (!bucket) {
            bucket = { ts: bucketMs };
            grouped.set(bucketMs, bucket);
          }

          if (r.broker === "bitget") {
            if (bucket.bitgetTs === undefined || ms >= bucket.bitgetTs) {
              bucket.bitget = r.total;
              bucket.bitgetTs = ms;
            }
          } else {
            if (bucket.krakenTs === undefined || ms >= bucket.krakenTs) {
              bucket.kraken = r.total;
              bucket.krakenTs = ms;
            }
          }
        }

        // Determine availability strictly per bucket (never substitute 0 for a missing broker):
        // - Both Bitget and Kraken present -> complete total = kraken + bitget.
        // - Only one broker present -> display individual broker balance and mark global total as partial.
        const chartData: BalanceHistoryPoint[] = Array.from(grouped.values())
          .sort((a, b) => a.ts - b.ts)
          .map((b) => {
            const hasK = b.kraken !== undefined;
            const hasB = b.bitget !== undefined;

            if (!hasK && !hasB) {
              return { ts: b.ts, totalStatus: "unavailable" as BalanceTotalStatus };
            }

            if (hasK && hasB) {
              return {
                ts: b.ts,
                kraken: b.kraken,
                bitget: b.bitget,
                total: (b.kraken as number) + (b.bitget as number),
                totalStatus: "complete" as BalanceTotalStatus,
              };
            }

            return {
              ts: b.ts,
              kraken: b.kraken,
              bitget: b.bitget,
              total: undefined,
              partialTotal: hasK ? b.kraken : b.bitget,
              totalStatus: "partial" as BalanceTotalStatus,
            };
          });

        setBalanceHistory(chartData);
      }
    } catch (err) {
      console.error("[Dashboard] Multi-broker load error:", err);
    } finally {
      setMultiBrokerLoading(false);
    }
  }, []);

  // Start/Stop trading loop
  const toggleTradingLoop = async () => {
    const endpoint = tradingLoopRunning ? "/api/multi/stop" : "/api/multi/start";
    try {
      const res = await fetch(`${API}${endpoint}`, { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        setTradingLoopRunning(data.status === "started" || data.status === "already_running");
        // Refresh status after toggle
        setTimeout(loadMultiBrokerData, 500);
      }
    } catch (err) {
      console.error("[Dashboard] Toggle trading loop error:", err);
    }
  };

  // Load data on mount and when range changes
  useEffect(() => {
    loadAll();
    loadMultiBrokerData();
  }, [range.from, range.to, loadMultiBrokerData]);

  // WebSocket connection for real-time updates (Robust & StrictMode safe)
  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
    let reconnectDelay = 1000; // Start at 1s, exponential backoff up to 30s

    const connect = () => {
      if (closed) return;

      // Close existing if any
      if (ws) {
        ws.close();
      }

      const wsUrl = `${WS_BASE}/ws/trades`;
      console.log("[WS] Connecting to:", wsUrl);
      ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        if (closed) {
          ws?.close();
          return;
        }
        console.log("[WS] Connected to:", wsUrl);
        setWsConnected(true);
        reconnectDelay = 1000; // Reset backoff on successful connection
      };

      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          if (msg?.type === "trade_opened" || msg?.type === "trade_closed") {
            // Debounce or just load
            loadAll();
          }
        } catch {
          // ignore non-json
        }
      };

      ws.onclose = () => {
        console.log("[WS] Disconnected from:", wsUrl);
        setWsConnected(false);
        if (!closed) {
          reconnectTimeout = setTimeout(connect, reconnectDelay);
          reconnectDelay = Math.min(reconnectDelay * 2, 30000); // Exponential backoff, max 30s
        }
      };

      ws.onerror = (err) => {
        console.error("[WS] Error:", wsUrl, err);
        if (!closed) ws?.close();
      };
    };

    connect();

    return () => {
      closed = true;
      if (ws) ws.close();
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
    };
  }, [WS_BASE, range.from, range.to]);

  // Auto-refresh multi-broker data every 10 seconds
  useEffect(() => {
    const interval = setInterval(() => {
      loadMultiBrokerData();
    }, 10000);
    return () => clearInterval(interval);
  }, [loadMultiBrokerData]);

  // Build heatmap matrix (7 days x 24 hours)
  const heatMatrix = useMemo(() => {
    const m: HeatCell[][] = Array.from({ length: 7 }, (_, dow) =>
      Array.from({ length: 24 }, (_, hour) => ({
        dow, hour, trades: 0, pnl: 0, win_rate: 0
      }))
    );
    for (const c of heat) {
      if (c.dow >= 0 && c.dow < 7 && c.hour >= 0 && c.hour < 24) {
        m[c.dow][c.hour] = c;
      }
    }
    return m;
  }, [heat]);

  return (
    <div className="min-h-screen bg-kraken-dark text-gray-300 font-sans p-4 lg:p-6">
      <div className="max-w-[1600px] mx-auto space-y-6">

        {/* Header */}
        <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
          <div>
            <h1 className="text-2xl font-bold text-white flex items-center gap-2">
              <Activity className="text-kraken-cyan" size={28} />
              Performance Analytics
            </h1>
            <p className="text-sm text-gray-500 mt-1">Real-time trading statistics and metrics</p>
          </div>

          <div className="flex items-center gap-3">
            <div className={`flex items-center gap-2 px-3 py-1.5 rounded-full border ${wsConnected
              ? 'bg-green-500/10 border-green-500/20 text-green-500'
              : 'bg-red-500/10 border-red-500/20 text-red-500'
              }`}>
              <div className={`w-2 h-2 rounded-full ${wsConnected ? 'bg-green-500 animate-pulse' : 'bg-red-500'}`} />
              <span className="text-xs font-bold uppercase tracking-wider">
                {wsConnected ? "System Live" : "Disconnected"}
              </span>
            </div>
          </div>
        </div>

        {/* Controls */}
        <div className="bg-kraken-card border border-kraken-border rounded-xl p-4 flex flex-wrap items-end gap-4 shadow-lg">
          <div className="space-y-1.5">
            <label className="text-xs text-gray-400 font-medium flex items-center gap-1">
              <Calendar size={12} /> From
            </label>
            <input
              type="date"
              value={range.from}
              onChange={(e) => setRange(r => ({ ...r, from: e.target.value }))}
              className="bg-[#1B2129] border border-kraken-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-kraken-cyan transition-colors dark-calendar"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-xs text-gray-400 font-medium flex items-center gap-1">
              <Calendar size={12} /> To
            </label>
            <input
              type="date"
              value={range.to}
              onChange={(e) => setRange(r => ({ ...r, to: e.target.value }))}
              className="bg-[#1B2129] border border-kraken-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-kraken-cyan transition-colors dark-calendar"
            />
          </div>
          <button
            onClick={loadAll}
            disabled={loading}
            className="px-4 py-2 bg-kraken-cyan/10 hover:bg-kraken-cyan/20 text-kraken-cyan border border-kraken-cyan/50 rounded-lg text-sm font-bold transition-all flex items-center gap-2 disabled:opacity-50 disabled:cursor-wait"
          >
            <RefreshCw size={16} className={loading ? "animate-spin" : ""} />
            {loading ? "Syncing..." : "Refresh Data"}
          </button>
        </div>

        {/* Error State */}
        {error && (
          <div className="bg-red-500/10 border border-red-500/20 rounded-xl p-4 flex items-center gap-3 text-red-400 animate-in fade-in slide-in-from-top-2">
            <AlertTriangle size={20} />
            <span className="font-medium">{error}</span>
          </div>
        )}

        {/* PnL Consistency Warning */}
        {pnlMismatch && (
          <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-xl p-4 flex items-center gap-3 text-yellow-400 animate-in fade-in slide-in-from-top-2">
            <ShieldAlert size={20} />
            <span className="font-medium text-sm">{pnlMismatch}</span>
          </div>
        )}

        {/* KPI Grid */}
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
          <KpiCard
            title="Total PnL"
            value={fmt(summary?.pnl)}
            suffix="$"
            color={summary && summary.pnl >= 0 ? "text-kraken-green" : "text-kraken-red"}
            icon={summary && summary.pnl >= 0 ? TrendingUp : TrendingDown}
          />
          <KpiCard title="Win Rate" value={summary ? fmt(summary.win_rate * 100, 1) : "—"} suffix="%" />
          <KpiCard title="Avg R:R" value={fmt(summary?.avg_rr)} />
          <KpiCard title="Total Trades" value={summary ? String(summary.trades) : "—"} />
          <KpiCard title="Max Drawdown" value={fmt(summary?.max_dd)} suffix="$" color="text-kraken-red" />
          <KpiCard title="Profit Factor" value={fmt(summary?.profit_factor)} />
        </div>

        {/* Phase 4 Execution Health & Hardening */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4 animate-in fade-in slide-in-from-bottom-2 duration-500">
          <div className={`bg-kraken-card border ${executionHealth?.cooldown_active ? 'border-yellow-500/50' : 'border-kraken-border'} rounded-xl p-4 flex items-center gap-4 shadow-lg group hover:border-kraken-cyan transition-all`}>
            <div className={`p-3 rounded-lg ${executionHealth?.cooldown_active ? 'bg-yellow-500/10 text-yellow-500' : 'bg-kraken-cyan/10 text-kraken-cyan'} group-hover:scale-110 transition-transform`}>
              <Zap size={24} />
            </div>
            <div>
              <p className="text-xs text-gray-400 font-medium uppercase tracking-wider">Execution Mode</p>
              <h4 className="text-lg font-bold text-white capitalize">{executionHealth?.execution_mode || 'Checking...'}</h4>
              <p className="text-[10px] text-gray-500 flex items-center gap-1 mt-0.5">
                {executionHealth?.primary_broker && <span className="text-kraken-green">Maker: {executionHealth.primary_broker}</span>}
                {executionHealth?.secondary_broker && <span className="text-gray-600">|</span>}
                {executionHealth?.secondary_broker && <span className="text-kraken-red">Taker: {executionHealth.secondary_broker}</span>}
              </p>
            </div>
          </div>

          <div className="bg-kraken-card border border-kraken-border rounded-xl p-4 flex items-center gap-4 shadow-lg group hover:border-kraken-cyan transition-all">
            <div className={`p-3 rounded-lg ${executionHealth?.orphan_count && executionHealth.orphan_count > 0 ? 'bg-red-500/10 text-red-500' : 'bg-green-500/10 text-green-500'} group-hover:scale-110 transition-transform`}>
              <AlertTriangle size={24} />
            </div>
            <div>
              <p className="text-xs text-gray-400 font-medium uppercase tracking-wider">Orphans Detected</p>
              <h4 className={`text-lg font-bold ${executionHealth?.orphan_count && executionHealth.orphan_count > 0 ? 'text-red-500' : 'text-white'}`}>
                {executionHealth?.orphan_count ?? 0}
              </h4>
              <p className="text-[10px] text-gray-500 mt-0.5">Last: {executionHealth?.last_orphan_detected || 'None'}</p>
            </div>
          </div>

          <div className="bg-kraken-card border border-kraken-border rounded-xl p-4 flex items-center gap-4 shadow-lg group hover:border-kraken-cyan transition-all">
            <div className={`p-3 rounded-lg ${executionHealth?.stale_rejection_count && executionHealth.stale_rejection_count > 0 ? 'bg-yellow-500/10 text-yellow-500' : 'bg-blue-500/10 text-blue-500'} group-hover:scale-110 transition-transform`}>
              <ShieldAlert size={24} />
            </div>
            <div>
              <p className="text-xs text-gray-400 font-medium uppercase tracking-wider">Stale Rejection</p>
              <h4 className="text-lg font-bold text-white">{executionHealth?.stale_rejection_count ?? 0}</h4>
              <p className="text-[10px] text-gray-500 mt-0.5">Latency guard blocks</p>
            </div>
          </div>

          <div className="bg-kraken-card border border-kraken-border rounded-xl p-4 flex items-center gap-4 shadow-lg group hover:border-kraken-cyan transition-all">
            <div className={`p-3 rounded-lg ${executionHealth?.late_fill_count && executionHealth.late_fill_count > 0 ? 'bg-orange-500/10 text-orange-500' : 'bg-blue-500/10 text-blue-500'} group-hover:scale-110 transition-transform`}>
              <Clock size={24} />
            </div>
            <div>
              <p className="text-xs text-gray-400 font-medium uppercase tracking-wider">Late Fills</p>
              <h4 className="text-lg font-bold text-white">{executionHealth?.late_fill_count ?? 0}</h4>
              <p className="text-[10px] text-gray-500 mt-0.5">Latency edge case monitoring</p>
            </div>
          </div>

          <div className="bg-kraken-card border border-kraken-border rounded-xl p-4 flex items-center gap-4 shadow-lg group hover:border-kraken-cyan transition-all">
            <div className={`p-3 rounded-lg ${executionHealth?.kill_switch_active ? 'bg-red-500 text-white animate-pulse' : 'bg-green-500/10 text-green-500'} group-hover:scale-110 transition-transform`}>
              <Server size={24} />
            </div>
            <div>
              <p className="text-xs text-gray-400 font-medium uppercase tracking-wider">Hard Lock Status</p>
              <h4 className={`text-lg font-bold ${executionHealth?.kill_switch_active ? 'text-red-500' : 'text-kraken-green'}`}>
                {executionHealth?.kill_switch_active ? 'ENGAGED' : 'READY'}
              </h4>
              <p className="text-[10px] text-gray-500 mt-0.5">
                {executionHealth?.cooldown_active ? 'Cooldown Active (Sec only)' : 'All systems normal'}
              </p>
            </div>
          </div>
        </div>

        {/* Multi-Broker Status Panel */}
        <div className="bg-kraken-card border border-kraken-border rounded-xl p-5 shadow-lg">
          <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 mb-5">
            <h3 className="text-white font-bold flex items-center gap-2">
              <Server size={18} className="text-kraken-cyan" />
              Multi-Broker Status
              {multiBrokerLoading && <RefreshCw size={14} className="animate-spin text-gray-500 ml-2" />}
            </h3>
            <div className="flex items-center gap-3">
              <button
                onClick={toggleTradingLoop}
                className={`px-4 py-2 rounded-lg text-sm font-bold transition-all flex items-center gap-2 ${tradingLoopRunning
                  ? 'bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/50'
                  : 'bg-green-500/20 hover:bg-green-500/30 text-green-400 border border-green-500/50'
                  }`}
              >
                {tradingLoopRunning ? <Square size={14} /> : <Play size={14} />}
                {tradingLoopRunning ? "Stop Trading" : "Start Trading"}
              </button>
              <button
                onClick={loadMultiBrokerData}
                disabled={multiBrokerLoading}
                className="px-3 py-2 bg-kraken-cyan/10 hover:bg-kraken-cyan/20 text-kraken-cyan border border-kraken-cyan/50 rounded-lg text-sm transition-all disabled:opacity-50"
              >
                <RefreshCw size={14} className={multiBrokerLoading ? "animate-spin" : ""} />
              </button>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* Kraken Status (Primary) */}
            <div className="bg-[#1B2129] rounded-lg p-4 border border-kraken-border">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <div className={`w-2.5 h-2.5 rounded-full ${multiBrokerStatus?.brokers?.kraken?.connected ? 'bg-green-500 animate-pulse' : 'bg-red-500'
                    }`} />
                  <span className="text-white font-bold">Kraken Futures</span>
                  {multiBrokerStatus?.brokers?.kraken?.mode === 'read_only' && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-yellow-500/20 text-yellow-300 font-semibold uppercase">
                      Read-Only
                    </span>
                  )}
                </div>
                <span className={`text-xs px-2 py-0.5 rounded ${multiBrokerStatus?.brokers?.kraken?.connected
                  ? 'bg-green-500/20 text-green-400'
                  : 'bg-red-500/20 text-red-400'
                  }`}>
                  {multiBrokerStatus?.brokers?.kraken?.connected ? 'Connected' : 'Disconnected'}
                </span>
              </div>
              {multiBrokerStatus?.brokers?.kraken?.balance && (
                <div className="space-y-2 text-sm">
                  <div className="flex justify-between">
                    <span className="text-gray-400">Available</span>
                    <span className="text-white font-mono">
                      {fmt(multiBrokerStatus.brokers.kraken.balance.available)} {multiBrokerStatus.brokers.kraken.balance.currency ?? 'USD'}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-400">Total Equity</span>
                    <span className="text-white font-mono">
                      {fmt(multiBrokerStatus.brokers.kraken.balance.total)} {multiBrokerStatus.brokers.kraken.balance.currency ?? 'USD'}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-400">Unrealized PnL</span>
                    <span className={`font-mono ${(multiBrokerStatus.brokers.kraken.balance.unrealized_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'
                      }`}>
                      {fmt(multiBrokerStatus.brokers.kraken.balance.unrealized_pnl)}
                    </span>
                  </div>
                </div>
              )}
              {multiBrokerStatus?.brokers?.kraken?.error && (
                <div className="text-red-400 text-xs mt-2">{multiBrokerStatus.brokers.kraken.error}</div>
              )}
              {!multiBrokerStatus?.brokers?.kraken && (
                <div className="text-gray-500 text-sm">Kraken not configured</div>
              )}
            </div>

            {/* MetaTrader 5 (MT5) Status */}
            <div className="bg-[#1B2129] rounded-lg p-4 border border-kraken-border">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <div className={`w-2.5 h-2.5 rounded-full ${multiBrokerStatus?.brokers?.mt5?.connected ? 'bg-green-500 animate-pulse' : 'bg-red-500'
                    }`} />
                  <span className="text-white font-bold">MetaTrader 5 (MT5)</span>
                  {multiBrokerStatus?.brokers?.mt5?.mode && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-cyan-500/20 text-cyan-300 font-semibold uppercase">
                      {multiBrokerStatus.brokers.mt5.mode}
                    </span>
                  )}
                </div>
                <span className={`text-xs px-2 py-0.5 rounded ${multiBrokerStatus?.brokers?.mt5?.connected
                  ? 'bg-green-500/20 text-green-400'
                  : 'bg-red-500/20 text-red-400'
                  }`}>
                  {multiBrokerStatus?.brokers?.mt5?.connected ? 'Connected' : 'Disconnected'}
                </span>
              </div>
              {multiBrokerStatus?.brokers?.mt5?.balance && (
                <div className="space-y-2 text-sm">
                  <div className="flex justify-between">
                    <span className="text-gray-400">Available</span>
                    <span className="text-white font-mono">
                      {fmt(multiBrokerStatus.brokers.mt5.balance.available)} {multiBrokerStatus.brokers.mt5.balance.currency ?? 'USD'}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-400">Total Equity</span>
                    <span className="text-white font-mono">
                      {fmt(multiBrokerStatus.brokers.mt5.balance.total)} {multiBrokerStatus.brokers.mt5.balance.currency ?? 'USD'}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-400">Unrealized PnL</span>
                    <span className={`font-mono ${(multiBrokerStatus.brokers.mt5.balance.unrealized_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'
                      }`}>
                      {fmt(multiBrokerStatus.brokers.mt5.balance.unrealized_pnl)}
                    </span>
                  </div>
                </div>
              )}
              {multiBrokerStatus?.brokers?.mt5?.error && (
                <div className="text-red-400 text-xs mt-2">{multiBrokerStatus.brokers.mt5.error}</div>
              )}
              {!multiBrokerStatus?.brokers?.mt5 && (
                <div className="text-gray-500 text-sm">MT5 not configured</div>
              )}
            </div>
          </div>

          {/* Trading Statistics */}
          {multiBrokerStatus?.statistics && (
            <div className="mt-4 pt-4 border-t border-kraken-border grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="text-center">
                <div className="text-2xl font-bold text-white">{multiBrokerStatus.statistics.total_signals ?? 0}</div>
                <div className="text-xs text-gray-500 uppercase tracking-wider">Signals</div>
              </div>
              <div className="text-center">
                <div className="text-2xl font-bold text-white">{multiBrokerStatus.statistics.total_orders ?? 0}</div>
                <div className="text-xs text-gray-500 uppercase tracking-wider">Orders</div>
              </div>
              <div className="text-center">
                <div className="text-2xl font-bold text-green-400">{multiBrokerStatus.statistics.successful_orders ?? 0}</div>
                <div className="text-xs text-gray-500 uppercase tracking-wider">Successful</div>
              </div>
              <div className="text-center">
                <div className="text-2xl font-bold text-red-400">{multiBrokerStatus.statistics.failed_orders ?? 0}</div>
                <div className="text-xs text-gray-500 uppercase tracking-wider">Failed</div>
              </div>
            </div>
          )}

          {/* Balance History Chart */}
          {balanceHistory.length > 0 && (
            <div className="mt-4 pt-4 border-t border-kraken-border">
              <h4 className="text-sm font-bold text-gray-400 mb-3 flex items-center gap-2">
                <TrendingUp size={14} className="text-kraken-cyan" />
                Balance History (7 days)
              </h4>
              <div className="h-48">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={balanceHistory}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#2D3748" />
                    <XAxis
                      dataKey="ts"
                      type="number"
                      scale="time"
                      domain={["dataMin", "dataMax"]}
                      tick={{ fill: "#718096", fontSize: 10 }}
                      tickFormatter={(value) => {
                        const n = Number(value);
                        return Number.isFinite(n) ? new Date(n).toLocaleDateString() : "";
                      }}
                    />
                    <YAxis tick={{ fill: "#718096", fontSize: 10 }} />
                    <Tooltip
                      content={({ active, payload }) => {
                        if (!active || !payload || payload.length === 0) return null;
                        const point = payload[0]?.payload as BalanceHistoryPoint | undefined;
                        if (!point || !Number.isFinite(point.ts)) return null;
                        return (
                          <div className="bg-[#1A202C] border border-[#2D3748] rounded-lg px-3 py-2 text-xs space-y-1 shadow-lg">
                            <div className="text-white font-semibold border-b border-[#2D3748] pb-1">
                              {new Date(point.ts).toLocaleString()}
                            </div>
                            <div className="flex justify-between gap-4">
                              <span className="text-[#5865F2]">Kraken</span>
                              <span className="text-white font-mono">
                                {point.kraken !== undefined ? fmt(point.kraken, 4) : "Indisponible"}
                              </span>
                            </div>
                            <div className="flex justify-between gap-4">
                              <span className="text-[#10B981]">Bitget</span>
                              <span className="text-white font-mono">
                                {point.bitget !== undefined ? fmt(point.bitget, 4) : "Indisponible"}
                              </span>
                            </div>
                            <div className="flex justify-between gap-4 pt-1 border-t border-[#2D3748]">
                              <span className="text-[#FFE55C]">
                                {point.totalStatus === "complete"
                                  ? "Total (complet)"
                                  : point.totalStatus === "partial"
                                  ? "Total (incomplet)"
                                  : "Total (indisponible)"}
                              </span>
                              <span className="text-white font-mono">
                                {point.totalStatus === "complete" && point.total !== undefined
                                  ? fmt(point.total, 4)
                                  : point.totalStatus === "partial" && point.partialTotal !== undefined
                                  ? `Partiel : ${fmt(point.partialTotal, 4)}`
                                  : "—"}
                              </span>
                            </div>
                          </div>
                        );
                      }}
                    />
                    <Line
                      type="monotone"
                      dataKey="kraken"
                      name="Kraken"
                      stroke="#5865F2"
                      strokeWidth={2}
                      dot={false}
                      connectNulls={false}
                    />
                    <Line
                      type="monotone"
                      dataKey="bitget"
                      name="Bitget"
                      stroke="#10B981"
                      strokeWidth={2}
                      dot={false}
                      connectNulls={false}
                    />
                    <Line
                      type="monotone"
                      dataKey="total"
                      name="Total (Complet)"
                      stroke="#FFE55C"
                      strokeWidth={2}
                      dot={false}
                      strokeDasharray="5 5"
                      connectNulls={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}
        </div>

        {/* Kraken Positions */}
        {multiBrokerPositions && (Object.keys(multiBrokerPositions).length > 0) && (
          <div className="bg-kraken-card border border-kraken-border rounded-xl shadow-lg overflow-hidden">
            <div className="p-5 border-b border-kraken-border">
              <h3 className="text-white font-bold flex items-center gap-2">
                <Wallet size={18} className="text-kraken-cyan" />
                Open Positions — Kraken
              </h3>
            </div>
            <div className="p-4">
                {Array.isArray(multiBrokerPositions.kraken) && multiBrokerPositions.kraken.length > 0 ? (
                  <div className="space-y-2">
                    {multiBrokerPositions.kraken.map((pos, i) => (
                      <div key={i} className="bg-[#1B2129] rounded-lg p-3 text-sm">
                        <div className="flex justify-between items-center mb-1">
                          <span className="text-white font-bold">{pos.symbol}</span>
                          <span className={`px-2 py-0.5 rounded text-xs font-bold ${pos.side?.toLowerCase() === 'long' ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'
                            }`}>
                            {pos.side}
                          </span>
                        </div>
                        <div className="flex justify-between text-gray-400 text-xs">
                          <span>Size: {fmt(pos.size, 4)}</span>
                          <span className={pos.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                            PnL: {fmt(pos.unrealized_pnl)}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-gray-500 text-sm">No open positions</div>
                )}
            </div>
          </div>
        )}

        {/* ══════════════════════════════════════════════════════════════ */}
        {/* PERSISTED POSITIONS PERFORMANCE ANALYTICS */}
        {/* ══════════════════════════════════════════════════════════════ */}

        <PerformanceAnalyticsPanel
          apiUrl={API}
          dateFrom={range.from}
          dateTo={range.to}
          refreshInterval={30000}
          onDataLoaded={(combined) => {
            if (summary && Math.abs(summary.pnl - combined.total_pnl) > 0.1) {
              setPnlMismatch(
                `PnL mismatch: Summary=${summary.pnl.toFixed(2)} vs Brokers=${combined.total_pnl.toFixed(2)}`
              );
            } else {
              setPnlMismatch(null);
            }
          }}
        />

        {/* ══════════════════════════════════════════════════════════════ */}
        {/* PERSISTED POSITIONS TABLE */}
        {/* ══════════════════════════════════════════════════════════════ */}

        <PositionsTable
          apiUrl={API}
          dateFrom={range.from}
          dateTo={range.to}
          refreshInterval={30000}
          pageSize={20}
        />

        {/* Charts & Heatmap */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

          {/* R:R Series Chart */}
          <div className="lg:col-span-2 bg-kraken-card border border-kraken-border rounded-xl p-5 shadow-lg flex flex-col h-[400px]">
            <h3 className="text-white font-bold flex items-center gap-2 mb-6">
              <TrendingUp size={18} className="text-kraken-cyan" />
              Risk:Reward Performance
            </h3>
            <div className="flex-grow w-full min-h-0">
              {rrSeries.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={rrSeries}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#2B3139" vertical={false} />
                    <XAxis dataKey="ts" hide />
                    <YAxis
                      tick={{ fontSize: 11, fill: '#6B7280' }}
                      axisLine={false}
                      tickLine={false}
                      domain={['auto', 'auto']}
                    />
                    <Tooltip
                      contentStyle={{ backgroundColor: '#1B2129', borderColor: '#2B3139', color: '#fff' }}
                      formatter={(value: number) => [fmt(value), "R:R"]}
                      labelFormatter={(label) => new Date(label).toLocaleString()}
                    />
                    <Line
                      type="monotone"
                      dataKey="rr"
                      stroke="#00F0FF"
                      strokeWidth={2}
                      dot={false}
                      activeDot={{ r: 4, fill: '#00F0FF' }}
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <div className="h-full flex flex-col items-center justify-center text-gray-500 gap-2">
                  <Activity size={32} className="opacity-20" />
                  <p className="text-sm">No data available for this period</p>
                </div>
              )}
            </div>
          </div>

          {/* Heatmap */}
          <div className="bg-kraken-card border border-kraken-border rounded-xl p-5 shadow-lg flex flex-col h-[400px]">
            <h3 className="text-white font-bold flex items-center gap-2 mb-4">
              <Clock size={18} className="text-kraken-cyan" />
              Trading Heatmap
            </h3>
            <div className="flex-1 overflow-auto custom-scrollbar">
              <table className="w-full text-center border-collapse">
                <thead>
                  <tr>
                    <th className="p-1 text-[10px] text-gray-500 font-medium border-b border-kraken-border bg-kraken-card sticky top-0 z-10">Day</th>
                    {Array.from({ length: 24 }, (_, h) => (
                      <th key={h} className="p-1 text-[9px] text-gray-600 font-normal border-b border-kraken-border min-w-[20px] bg-kraken-card sticky top-0 z-10">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {heatMatrix.map((row, dow) => (
                    <tr key={dow} className="group hover:bg-white/5">
                      <td className="text-[10px] text-gray-400 font-medium p-1 border-r border-kraken-border bg-kraken-card sticky left-0 z-10">{DOW[dow]}</td>
                      {row.map((c) => (
                        <td
                          key={c.hour}
                          title={`Trades: ${c.trades}\nPnL: ${fmt(c.pnl)}$\nWin: ${fmt(c.win_rate * 100, 0)}%`}
                          className="p-0.5"
                        >
                          <div
                            className="w-full h-6 rounded-sm flex items-center justify-center text-[9px] transition-all hover:scale-110 cursor-help"
                            style={{
                              backgroundColor: cellBg(c),
                              color: c.trades > 0 ? 'white' : 'transparent'
                            }}
                          >
                            {c.trades > 0 && c.trades}
                          </div>
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-3 flex justify-between items-center text-[10px] text-gray-500">
              <span>Activity Intensity</span>
              <div className="flex gap-1">
                <div className="w-3 h-3 bg-red-500/50 rounded-sm"></div>
                <div className="w-3 h-3 bg-green-500/50 rounded-sm"></div>
              </div>
            </div>
          </div>
        </div>

        {/* Recent Trades Table */}
        <div className="bg-kraken-card border border-kraken-border rounded-xl shadow-lg flex flex-col overflow-hidden">
          <div className="p-5 border-b border-kraken-border">
            <h3 className="text-white font-bold flex items-center gap-2">
              <Activity size={18} className="text-kraken-cyan" />
              Recent Trades ({trades.length})
            </h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="bg-[#151A1F] text-xs uppercase text-gray-500 font-medium">
                <tr>
                  {["Time", "Symbol", "Side", "Preset", "Session", "R:R", "PnL", "Result"].map(h => (
                    <th key={h} className="px-6 py-3">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-kraken-border">
                {trades.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="px-6 py-12 text-center text-gray-500">
                      No trades found for the selected date range.
                    </td>
                  </tr>
                ) : (
                  trades.slice(0, 50).map(t => (
                    <tr key={t.id} className="hover:bg-white/5 transition-colors group">
                      <td className="px-6 py-3 text-gray-400 text-xs font-mono">{new Date(t.ts_open || t.ts).toLocaleString()}</td>
                      <td className="px-6 py-3 text-white font-bold">{t.symbol}</td>
                      <td className="px-6 py-3">
                        <span className={`px-2 py-1 rounded text-[10px] font-bold uppercase ${t.side === "long" ? "bg-kraken-green/20 text-kraken-green" : "bg-kraken-red/20 text-kraken-red"
                          }`}>
                          {t.side}
                        </span>
                      </td>
                      <td className="px-6 py-3 text-gray-400 text-xs">{t.preset ?? "—"}</td>
                      <td className="px-6 py-3 text-gray-400 text-xs capitalize">{t.session ?? "—"}</td>
                      <td className="px-6 py-3 font-mono text-white">{fmt(t.rr)}</td>
                      <td className={`px-6 py-3 font-mono font-bold ${t.pnl >= 0 ? "text-kraken-green" : "text-kraken-red"
                        }`}>
                        {t.pnl >= 0 ? '+' : ''}{fmt(t.pnl)}$
                      </td>
                      <td className="px-6 py-3">
                        {t.result && (
                          <span className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase border ${t.result === "win" ? "border-green-500/30 text-green-400" :
                            t.result === "loss" ? "border-red-500/30 text-red-400" : "border-yellow-500/30 text-yellow-400"
                            }`}>
                            {t.result}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>

      </div>
    </div>
  );
}

// KPI Card Component
function KpiCard({ title, value, suffix, color, icon: Icon }: any) {
  return (
    <div className="bg-kraken-card border border-kraken-border rounded-xl p-4 shadow-sm hover:shadow-md transition-shadow relative overflow-hidden group">
      <div className="absolute right-0 top-0 p-3 opacity-5 group-hover:opacity-10 transition-opacity">
        {Icon && <Icon size={48} />}
      </div>
      <div className="text-xs text-gray-400 font-medium uppercase tracking-wider mb-1">{title}</div>
      <div className={`text-2xl font-bold ${color || "text-white"}`}>
        {value}
        {suffix && <span className="text-sm opacity-50 ml-1 decoration-slice">{suffix}</span>}
      </div>
    </div>
  );
}

// Heatmap cell background color based on PnL
function cellBg(c: HeatCell): string {
  if (!c.trades) return "rgba(255,255,255,0.03)";

  const cap = 50;
  const v = Math.max(-cap, Math.min(cap, c.pnl)) / cap; // -1 to 1

  if (v >= 0) {
    const intensity = 0.2 + 0.6 * v;
    return `rgba(34, 197, 94, ${intensity})`; // Green
  } else {
    const intensity = 0.2 + 0.6 * (-v);
    return `rgba(239, 68, 68, ${intensity})`; // Red
  }
}
