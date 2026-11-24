import { Candle, Ticker, TradeSignal, ApiConfig, Position } from '../types';
import { TIMEFRAME_MAP } from '../constants';

/**
 * KrakenService — Real-Data Trading API client.
 * Routes strictly through the Python backend API.
 * NEVER fabricates mock candles, random prices, or simulated balances.
 */

/**
 * Normalizes symbol BTC/USDT or BTC:USDT to BTCUSDT
 */
export function normSymbol(s: string): string {
  if (!s) return "";
  return s.replace("/", "").replace(":", "").replace("-", "").toUpperCase();
}

let globalCooldownUntil = 0;

const fetchWithRetry = async (url: string, options?: RequestInit, retries: number = 1): Promise<Response> => {
  if (Date.now() < globalCooldownUntil) {
    return new Response(JSON.stringify({ code: '429', message: 'Global Cooldown' }), {
      status: 429,
      headers: { 'Content-Type': 'application/json' }
    });
  }

  const response = await fetch(url, options);

  if (response.status === 429) {
    const retryAfter = response.headers.get("Retry-After");
    const waitMs = retryAfter ? Number(retryAfter) * 1000 : 5000;
    globalCooldownUntil = Date.now() + waitMs;
    console.warn(`[KrakenService] 429 detected for ${url}. Global Cooldown active for ${waitMs}ms`);

    if (retries > 0) {
      await new Promise(resolve => setTimeout(resolve, waitMs));
      return fetchWithRetry(url, options, retries - 1);
    }
  }
  return response;
};

/**
 * Avoids floating point errors and rounds to specific step
 */
export function roundToStep(value: number, step: number): string {
  if (value === undefined || value === null || isNaN(value)) return "0";
  const stepStr = step.toString();
  const decimals = stepStr.includes('.') ? stepStr.split('.')[1].length : 0;
  const factor = Math.pow(10, decimals);
  return (Math.round(value / step) * step).toFixed(decimals);
}

export const KrakenService = {
  async getCandles(symbol: string, timeframe: string, limit: number = 200): Promise<any[]> {
    const cleanSymbol = normSymbol(symbol);
    const granularity = TIMEFRAME_MAP[timeframe] || timeframe;
    const safeLimit = Math.max(10, Math.min(1000, Math.round(limit)));

    // Use the SMC market candles endpoint (real OHLCV data only — zero mock fallback)
    const url = `/api/smc/market/candles?symbol=${cleanSymbol}&tf=${granularity}&limit=${safeLimit}`;

    try {
      const response = await fetchWithRetry(url);
      if (!response.ok) throw new Error(`Backend Error: ${response.status}`);
      const data = await response.json();
      if (!Array.isArray(data)) return [];
      return data.map((c: any) => ({
        ...c,
        time: Number(c.time ?? c.ts ?? 0),
        open: Number(c.open),
        high: Number(c.high),
        low: Number(c.low),
        close: Number(c.close),
        volume: Number(c.volume ?? 0),
        isReal: true,
      }));
    } catch (error) {
      console.error(`[KrakenService] Real candles unavailable for ${symbol}:`, error);
      return [];
    }
  },

  async getBalanceV2(_config?: ApiConfig): Promise<any> {
    try {
      // Fetch real Kraken Futures balance from backend (never dead Bitget 0.00 balance)
      const response = await fetchWithRetry('/api/v2/balance?broker=kraken');
      if (!response.ok) return null;
      const json = await response.json();
      const d = json?.data;
      if (d && typeof d === 'object') {
        return {
          code: '00000',
          broker: 'kraken',
          available: Number(d.available ?? 0),
          equity: Number(d.total ?? d.available ?? 0),
          currency: String(d.currency ?? 'USD'),
          data: [
            {
              available: Number(d.available ?? 0),
              equity: Number(d.total ?? d.available ?? 0),
              marginCoin: String(d.currency ?? 'USD'),
            },
          ],
        };
      }
      return json;
    } catch (e) {
      console.error("[KrakenService] getBalanceV2 failed:", e);
      return null;
    }
  },

  async getLeverageV2(symbol: string, config: ApiConfig): Promise<number> {
    try {
      const response = await fetchWithRetry(`/api/v2/position/leverage?symbol=${normSymbol(symbol)}`, {
        headers: {
          'X-API-KEY': config.apiKey,
          'X-API-SECRET': config.secretKey,
          'X-API-PASSPHRASE': config.passphrase
        }
      });
      if (!response.ok) return 10;
      const data = await response.json();
      return data.leverage || 10;
    } catch (e) {
      return 10;
    }
  },

  async dryRunOrder(signal: TradeSignal, config: ApiConfig): Promise<any> {
    try {
      const response = await fetchWithRetry('/api/v2/order/dry-run', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-KEY': config.apiKey,
          'X-API-SECRET': config.secretKey,
          'X-API-PASSPHRASE': config.passphrase
        },
        body: JSON.stringify({
          symbol: normSymbol(signal.symbol),
          side: signal.type === 'LONG' ? 'buy' : 'sell',
          pos_side: signal.type === 'LONG' ? 'long' : 'short',
          entry_price: signal.entryPrice,
          stop_loss: signal.stopLoss,
          take_profit: signal.tp2 || signal.tp1,
          risk_pct: 1.0,
          leverage: 10,
          product_type: 'USDT-FUTURES'
        })
      });

      const text = await response.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch (e) {
        throw new Error(`Invalid server response: ${text.slice(0, 100)}`);
      }

      if (!response.ok) {
        const errorMsg = data.reason || data.detail || (Array.isArray(data.detail) ? data.detail.map((d: any) => d.msg).join(", ") : null) || "Dry-run failed";
        throw new Error(errorMsg);
      }
      return data;
    } catch (e: any) {
      console.error("[KrakenService] dry-run failed:", e);
      throw e;
    }
  },

  /**
   * Check backend health and verify we're talking to the trading backend.
   * Returns { ok, service, version, wrongBackend } 
   */
  async checkHealth(): Promise<{ ok: boolean; service: string; version: string; wrongBackend: boolean }> {
    try {
      const response = await fetchWithRetry('/api/health');
      if (!response.ok) return { ok: false, service: '', version: '', wrongBackend: false };
      const data = await response.json();
      const service = data.service || '';
      return {
        ok: data.status === 'ok',
        service,
        version: data.version || '',
        wrongBackend: service !== '' && service !== 'smc-trader',
      };
    } catch {
      return { ok: false, service: '', version: '', wrongBackend: false };
    }
  },

  /**
   * Fetch Phase 4 Execution Health (Asymmetric Mode, Orphans, Latency)
   */
  async getExecutionHealth(): Promise<{
    execution_mode: string;
    primary_broker: string;
    secondary_broker: string;
    cooldown_active: boolean;
    last_orphan_detected: string;
    orphan_count: number;
    late_fill_count: number;
    stale_rejection_count: number;
    kill_switch_active: boolean;
  } | null> {
    try {
      const response = await fetchWithRetry('/api/health/execution');
      if (!response.ok) return null;
      return await response.json();
    } catch {
      return null;
    }
  },

  async getTicker(symbol: string): Promise<Ticker | null> {
    const cleanSymbol = normSymbol(symbol);
    const url = `/api/v1/market/ticker?symbol=${cleanSymbol}`;

    try {
      const response = await fetchWithRetry(url);
      if (!response.ok) throw new Error('Network error');
      const data = await response.json();

      // Format A: Our V2Client flat format {price, mark_price, source_field, ...}
      if (data.price !== undefined) {
        return {
          symbol: data.symbol || cleanSymbol,
          lastPr: String(data.last_price || data.price),
          markPrice: String(data.mark_price || data.price),
          indexPrice: String(data.index_price || '0'),
          change24h: '0',
          high24h: String(data.ask1 || data.price),
          low24h: String(data.bid1 || data.price),
          isReal: true,
          stale: data.stale || false,
          source: data.source_field || 'markPrice',
        };
      }

      // Format B: Raw exchange proxy format {code:"00000", data:[{lastPr:...}]}
      if ((data.code === '00000' || data.status === 'success') && data.data && data.data.length > 0) {
        return { ...data.data[0], isReal: true };
      }
      return null;
    } catch (error) {
      console.error(`[KrakenService] Real ticker unavailable for ${symbol}:`, error);
      return null;
    }
  },

  async placeOrder(signal: TradeSignal, config: ApiConfig, size?: number): Promise<any> {
    // 1. If size is missing, try to get it from dry-run
    let finalSize = size;
    let finalSL = Number(signal.stopLoss);
    let finalTP = Number(signal.tp2 || signal.tp1);

    if (!finalSize) {
      console.info("[KrakenService] Size missing, performing pre-trade dry-run...");
      try {
        const dryRun = await this.dryRunOrder(signal, config);
        if (dryRun.ok) {
          finalSize = dryRun.payloadPreview.size;
          finalSL = dryRun.payloadPreview.stopLoss;
          finalTP = dryRun.payloadPreview.takeProfit;
      console.info(`[KrakenService] Dry-run success: Size=${finalSize}, SL=${finalSL}, TP=${finalTP}`);
        } else {
          throw new Error(`Risk Validation Failed: ${dryRun.reason}`);
        }
      } catch (e: any) {
        throw new Error(`Dry-run error: ${e.message}`);
      }
    }

    const body = {
      symbol: normSymbol(signal.symbol),
      productType: 'USDT-FUTURES',
      marginCoin: 'USDT',
      marginMode: 'isolated',
      side: signal.type === 'LONG' ? 'buy' : 'sell',
      posSide: signal.type === 'LONG' ? 'long' : 'short',
      orderType: 'market',
      tradeSide: 'open',
      size: finalSize,
      quantity: finalSize,
      stopLoss: finalSL,
      takeProfit: finalTP,
    };

    // 2. Call our secure backend (/api/v1/order)
    try {
      const response = await fetchWithRetry('/api/v1/order', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-KEY': config.apiKey,
          'X-API-SECRET': config.secretKey,
          'X-API-PASSPHRASE': config.passphrase
        },
        body: JSON.stringify(body)
      });

      const data = await response.json();

      if (!response.ok) {
        // Handle 422 Unprocessable Entity (FastAPI validation)
        if (response.status === 422) {
          console.error("ORDER 422/ERROR BODY:", data);
          const detailStr = data?.detail?.map?.((d: any) => `${d.loc?.join(".")}: ${d.msg}`).join(" | ")
            || JSON.stringify(data?.detail || data);
          throw new Error(`Validation Error: ${detailStr}`);
        }

        // Handle explicit risk validation errors
        if (data.error === 'RISK_VALIDATION_FAILED') {
          const detail = data.details;
          throw new Error(`Risk Error: Margin needed (${detail.marginRequired.toFixed(2)}) > Available (${detail.availableBalance.toFixed(2)})`);
        }
        throw new Error(data.message || data.error || `API Error ${response.status}`);
      }

      if (data.code !== '00000' && data.status !== 'success' && data.code !== '200') {
        throw new Error(data.msg || data.message || 'Order rejected by exchange');
      }
      return data;
    } catch (error: any) {
      console.error("[KrakenService] Order failed:", error);
      throw error;
    }
  },

  async getPositions(config: ApiConfig): Promise<Position[]> {
    try {
      const response = await fetchWithRetry('/api/v1/positions', {
        method: 'GET',
        headers: {
          'X-API-KEY': config.apiKey,
          'X-API-SECRET': config.secretKey,
          'X-API-PASSPHRASE': config.passphrase
        }
      });

      if (!response.ok) return [];

      const data = await response.json();
      if ((data.code === '00000' || data.status === 'success') && data.data) {
        return data.data;
      }
      return [];
    } catch (e) {
      return [];
    }
  },

  /**
   * Manual Trade Execution API - Pre-flight check
   * Validates if a signal can be executed before sending the order
   */
  async manualPreflight(signal: TradeSignal, config: ApiConfig): Promise<{
    can_execute: boolean;
    reason: string;
    computed_qty?: number;
    min_qty?: number;
    min_notional?: number;
    notional?: number;
    risk_amount?: number;
    risk_deviation_pct?: number;
    account_balance?: number;
    has_open_position?: boolean;
    is_locked?: boolean;
  }> {
    try {
      const response = await fetchWithRetry('/api/manual/preflight', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          symbol: normSymbol(signal.symbol),
          side: signal.type === 'LONG' ? 'long' : 'short',
          entry_price: signal.entryPrice,
          stop_loss: signal.stopLoss,
          take_profit: signal.tp2 || signal.tp1 || 0,
          leverage: 10,
          risk_pct: 1.0,
        }),
      });

      const data = await response.json();
      return data;
    } catch (e: any) {
      console.error('[KrakenService] Manual preflight failed:', e);
      return {
        can_execute: false,
        reason: e.message || 'PREFLIGHT_ERROR',
      };
    }
  },

  /**
   * Manual Trade Execution API - Execute trade
   * Executes a trade through the same pipeline as the bot
   */
  async manualExecute(signal: TradeSignal, config: ApiConfig): Promise<{
    success: boolean;
    reason: string;
    order_id?: string;
    symbol?: string;
    side?: string;
    qty?: number;
    entry_price?: number;
    stop_loss?: number;
    take_profit?: number;
    trade_id?: string;
  }> {
    try {
      const response = await fetchWithRetry('/api/manual/execute', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          symbol: normSymbol(signal.symbol),
          side: signal.type === 'LONG' ? 'long' : 'short',
          entry_price: signal.entryPrice,
          stop_loss: signal.stopLoss,
          take_profit: signal.tp2 || signal.tp1 || 0,
          leverage: 10,
          risk_pct: 1.0,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        return {
          success: false,
          reason: data.reason || data.detail || `HTTP ${response.status}`,
        };
      }

      return data;
    } catch (e: any) {
      console.error('[KrakenService] Manual execute failed:', e);
      return {
        success: false,
        reason: e.message || 'EXECUTION_ERROR',
      };
    }
  },

  /**
   * Get manual execution status
   */
  async getManualStatus(): Promise<{
    enabled: boolean;
    executor_ready: boolean;
    locked_symbols: string[];
  }> {
    try {
      const response = await fetchWithRetry('/api/manual/status');
      if (!response.ok) {
        return { enabled: false, executor_ready: false, locked_symbols: [] };
      }
      return await response.json();
    } catch (e) {
      return { enabled: false, executor_ready: false, locked_symbols: [] };
    }
  },
};
