import React, { useState, useEffect, useCallback } from 'react';
import { RefreshCw, History, Filter, ChevronLeft, ChevronRight, ArrowUpRight, ArrowDownRight, Clock, CheckCircle, XCircle, MinusCircle, AlertTriangle } from 'lucide-react';

// ═══════════════════════════════════════════════════════════════════════════════
// TYPES
// ═══════════════════════════════════════════════════════════════════════════════

export interface Position {
  id: string;
  order_id: string | null;
  broker: string;
  symbol: string;
  side: 'long' | 'short';
  size: number;
  leverage: number;
  notional: number | null;
  entry_price: number;
  entry_time: string;
  stop_loss: number | null;
  take_profit: number | null;
  exit_price: number | null;
  exit_time: string | null;
  pnl: number;
  pnl_percent: number;
  fee: number;
  rr: number;
  rr_target: number;
  status: 'OPEN' | 'CLOSED' | 'CANCELLED' | 'FAILED';
  result: 'win' | 'loss' | 'be' | null;
  error_message?: string | null;
  close_reason: string | null;
  session_name: string | null;
  preset: string | null;
}

export interface PositionsResponse {
  positions: Position[];
  total: number;
  open_count: number;
  closed_count: number;
  page: number;
  page_size: number;
  filters: {
    broker: string | null;
    status: string | null;
    symbol: string | null;
    from: string | null;
    to: string | null;
    include_archived?: boolean;
  };
  timestamp: string;
}

interface Props {
  apiUrl?: string;
  dateFrom?: string;
  dateTo?: string;
  refreshInterval?: number;
  pageSize?: number;
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

function formatDateTime(isoString: string | null): string {
  if (!isoString) return "—";
  try {
    return new Date(isoString).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return isoString;
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// COMPONENT
// ═══════════════════════════════════════════════════════════════════════════════

export default function PositionsTable({
  apiUrl = '',
  dateFrom,
  dateTo,
  refreshInterval = 30000,
  pageSize = 20,
}: Props) {
  const [positions, setPositions] = useState<Position[]>([]);
  const [total, setTotal] = useState(0);
  const [openCount, setOpenCount] = useState(0);
  const [closedCount, setClosedCount] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [brokerFilter, setBrokerFilter] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [includeArchived, setIncludeArchived] = useState<boolean>(false);

  // Calculate total pages
  const totalPages = Math.ceil(total / pageSize);

  // Fetch positions
  const loadPositions = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams();
      params.append('page', String(page));
      params.append('page_size', String(pageSize));

      if (brokerFilter) params.append('broker', brokerFilter);
      if (statusFilter) params.append('status', statusFilter);
      if (dateFrom) params.append('from', dateFrom);
      if (dateTo) params.append('to', dateTo);
      if (includeArchived) params.append('include_archived', 'true');

      const res = await fetch(`${apiUrl}/api/positions?${params}`);

      if (!res.ok) {
        throw new Error(`API error: ${res.status}`);
      }

      const data: PositionsResponse = await res.json();
      let mergedPositions: Position[] = data.positions || [];
      let mergedTotal = data.total || 0;
      let mergedOpen = data.open_count || 0;

      // Also include live open MetaTrader 5 (MT5) positions when filter allows MT5 & OPEN
      if (
        page === 1 &&
        (brokerFilter === '' || brokerFilter === 'mt5') &&
        (statusFilter === '' || statusFilter === 'OPEN')
      ) {
        try {
          const mt5Res = await fetch(`${apiUrl}/api/mt5/positions`);
          if (mt5Res.ok) {
            const mt5Data = await mt5Res.json();
            const mt5Rows: Position[] = (mt5Data?.positions || []).map((p: any) => ({
              id: `mt5-${p.ticket ?? p.order_id}`,
              order_id: String(p.ticket ?? p.order_id ?? ''),
              broker: 'mt5',
              symbol: String(p.symbol ?? ''),
              side: p.side === 'short' ? 'short' : 'long',
              size: Number(p.volume ?? p.size ?? 0),
              leverage: 0,
              notional: p.entry_price && p.volume ? Number(p.entry_price) * Number(p.volume) : null,
              entry_price: Number(p.entry_price ?? 0),
              entry_time: new Date().toISOString(),
              stop_loss: p.stop_loss != null ? Number(p.stop_loss) : null,
              take_profit: p.take_profit != null ? Number(p.take_profit) : null,
              exit_price: p.mark_price != null ? Number(p.mark_price) : null,
              exit_time: null,
              pnl: Number(p.unrealized_pnl ?? 0),
              pnl_percent: 0,
              fee: 0,
              rr: 0,
              rr_target: 0,
              status: 'OPEN',
              result: null,
              close_reason: null,
              session_name: null,
              preset: p.comment || 'MT5',
            }));
            if (mt5Rows.length > 0) {
              mergedPositions = brokerFilter === 'mt5' ? mt5Rows : [...mt5Rows, ...mergedPositions];
              mergedTotal = brokerFilter === 'mt5' ? mt5Rows.length : mergedTotal + mt5Rows.length;
              mergedOpen = brokerFilter === 'mt5' ? mt5Rows.length : mergedOpen + mt5Rows.length;
            }
          }
        } catch {
          // MT5 endpoint optional; never break Kraken/DB positions table
        }
      }

      setPositions(mergedPositions);
      setTotal(mergedTotal);
      setOpenCount(mergedOpen);
      setClosedCount(data.closed_count);
    } catch (err) {
      console.error('[PositionsTable] Load error:', err);
      setError(err instanceof Error ? err.message : 'Failed to load positions');
    } finally {
      setLoading(false);
    }
  }, [apiUrl, page, pageSize, brokerFilter, statusFilter, dateFrom, dateTo, includeArchived]);

  // Initial load and refresh
  useEffect(() => {
    loadPositions();

    if (refreshInterval > 0) {
      const interval = setInterval(loadPositions, refreshInterval);
      return () => clearInterval(interval);
    }
  }, [loadPositions, refreshInterval]);

  // Reset page when filters change
  useEffect(() => {
    setPage(1);
  }, [brokerFilter, statusFilter, includeArchived]);

  return (
    <div className="bg-kraken-card border border-kraken-border rounded-xl shadow-lg overflow-hidden">
      {/* Header */}
      <div className="p-5 border-b border-kraken-border">
        <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
          <h3 className="text-white font-bold flex items-center gap-2">
            <History size={18} className="text-kraken-cyan" />
            Position History
            {loading && <RefreshCw size={14} className="animate-spin text-gray-500 ml-2" />}
          </h3>

          <div className="flex flex-wrap items-center gap-3">
            {/* Stats */}
            <div className="flex items-center gap-2 text-xs">
              <span className="px-2 py-1 bg-green-500/20 text-green-400 rounded">
                {openCount} Open
              </span>
              <span className="px-2 py-1 bg-gray-500/20 text-gray-400 rounded">
                {closedCount} Closed
              </span>
            </div>

            {/* Filters */}
            <div className="flex items-center gap-2">
              <Filter size={14} className="text-gray-400" />

              {/* Broker Filter */}
              <select
                value={brokerFilter}
                onChange={(e) => setBrokerFilter(e.target.value)}
                className="bg-[#1B2129] border border-kraken-border rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-kraken-cyan"
              >
                <option value="">All Brokers</option>
                <option value="kraken">Kraken</option>
                <option value="mt5">MetaTrader 5 (MT5)</option>
              </select>

              {/* Status Filter */}
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="bg-[#1B2129] border border-kraken-border rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-kraken-cyan"
              >
                <option value="">All Status</option>
                <option value="OPEN">Open</option>
                <option value="CLOSED">Closed</option>
                <option value="FAILED">Failed</option>
              </select>

              {/* Include Archived Toggle */}
              <label className="flex items-center gap-2 cursor-pointer text-xs text-gray-400 hover:text-white transition-colors">
                <input
                  type="checkbox"
                  checked={includeArchived}
                  onChange={(e) => setIncludeArchived(e.target.checked)}
                  className="rounded border-gray-600 bg-[#1B2129] text-kraken-cyan focus:ring-kraken-cyan focus:ring-offset-gray-900"
                />
                Audit Mode (Show Archived)
              </label>
            </div>

            {/* Refresh Button */}
            <button
              onClick={loadPositions}
              disabled={loading}
              className="p-2 bg-kraken-cyan/10 hover:bg-kraken-cyan/20 text-kraken-cyan border border-kraken-cyan/50 rounded-lg transition-all disabled:opacity-50"
            >
              <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
            </button>
          </div>
        </div>
      </div>

      {/* Error State */}
      {error && (
        <div className="p-4 bg-red-500/10 border-b border-red-500/20 text-red-400 text-sm">
          {error}
        </div>
      )}

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="bg-[#151A1F] text-xs uppercase text-gray-500 font-medium">
            <tr>
              {['Broker', 'Symbol', 'Side', 'Entry', 'Exit', 'Size', 'PnL', 'R:R', 'Status', 'Time'].map(h => (
                <th key={h} className="px-4 py-3 whitespace-nowrap">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-kraken-border">
            {positions.length === 0 ? (
              <tr>
                <td colSpan={10} className="px-4 py-12 text-center text-gray-500">
                  {loading ? 'Loading positions...' : 'No positions found'}
                </td>
              </tr>
            ) : (
              positions.map((pos) => (
                <tr key={pos.id} className="hover:bg-white/5 transition-colors">
                  {/* Broker */}
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase ${pos.broker === 'kraken'
                      ? 'bg-purple-500/20 text-purple-400'
                      : 'bg-yellow-500/20 text-yellow-400'
                      }`}>
                      {pos.broker}
                    </span>
                  </td>

                  {/* Symbol */}
                  <td className="px-4 py-3 text-white font-bold">{pos.symbol}</td>

                  {/* Side */}
                  <td className="px-4 py-3">
                    <span className={`flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-bold uppercase w-fit ${pos.side === 'long'
                      ? 'bg-green-500/20 text-green-400'
                      : 'bg-red-500/20 text-red-400'
                      }`}>
                      {pos.side === 'long' ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}
                      {pos.side}
                    </span>
                  </td>

                  {/* Entry Price */}
                  <td className="px-4 py-3 font-mono text-white">{fmt(pos.entry_price, 4)}</td>

                  {/* Exit Price */}
                  <td className="px-4 py-3 font-mono text-gray-400">
                    {pos.exit_price ? fmt(pos.exit_price, 4) : '—'}
                  </td>

                  {/* Size */}
                  <td className="px-4 py-3 font-mono text-gray-400">{fmt(pos.size, 4)}</td>

                  {/* PnL */}
                  <td className={`px-4 py-3 font-mono font-bold ${pos.pnl > 0 ? 'text-green-400' : pos.pnl < 0 ? 'text-red-400' : 'text-gray-400'
                    }`}>
                    {pos.pnl > 0 ? '+' : ''}{fmt(pos.pnl)}
                    {pos.pnl_percent !== 0 && (
                      <span className="text-xs opacity-60 ml-1">
                        ({pos.pnl_percent > 0 ? '+' : ''}{fmt(pos.pnl_percent, 1)}%)
                      </span>
                    )}
                  </td>

                  {/* R:R */}
                  <td className="px-4 py-3 font-mono text-white">
                    {pos.rr ? fmt(pos.rr, 2) : '—'}
                  </td>

                  {/* Status & Result */}
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1 group relative">
                      {pos.status === 'FAILED' ? (
                        <div className="flex items-center gap-1 text-red-500 cursor-help">
                          <AlertTriangle size={12} />
                          <span className="text-xs font-bold">Failed</span>
                          {pos.error_message && (
                            <div className="absolute left-0 bottom-full mb-1 hidden group-hover:block w-48 bg-gray-900 border border-red-500/50 text-gray-300 text-[10px] p-2 rounded shadow-xl z-10 break-words">
                              {pos.error_message}
                            </div>
                          )}
                        </div>
                      ) : pos.status === 'OPEN' ? (
                        <span className="flex items-center gap-1 text-blue-400">
                          <Clock size={12} />
                          <span className="text-xs">Open</span>
                        </span>
                      ) : pos.result === 'win' ? (
                        <span className="flex items-center gap-1 text-green-400">
                          <CheckCircle size={12} />
                          <span className="text-xs">Win</span>
                        </span>
                      ) : pos.result === 'loss' ? (
                        <span className="flex items-center gap-1 text-red-400">
                          <XCircle size={12} />
                          <span className="text-xs">Loss</span>
                        </span>
                      ) : (
                        <span className="flex items-center gap-1 text-yellow-400">
                          <MinusCircle size={12} />
                          <span className="text-xs">BE</span>
                        </span>
                      )}
                    </div>
                  </td>

                  {/* Time */}
                  <td className="px-4 py-3 text-gray-400 text-xs whitespace-nowrap">
                    <div>{formatDateTime(pos.entry_time)}</div>
                    {pos.exit_time && (
                      <div className="text-gray-500">→ {formatDateTime(pos.exit_time)}</div>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="p-4 border-t border-kraken-border flex items-center justify-between">
          <div className="text-xs text-gray-500">
            Showing {((page - 1) * pageSize) + 1} - {Math.min(page * pageSize, total)} of {total}
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={page === 1 || loading}
              className="p-2 bg-[#1B2129] border border-kraken-border rounded hover:bg-kraken-cyan/10 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft size={16} className="text-gray-400" />
            </button>

            <span className="px-3 py-1 text-sm text-white">
              {page} / {totalPages}
            </span>

            <button
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              disabled={page === totalPages || loading}
              className="p-2 bg-[#1B2129] border border-kraken-border rounded hover:bg-kraken-cyan/10 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronRight size={16} className="text-gray-400" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
