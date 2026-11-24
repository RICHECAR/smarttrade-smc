import { SmcConfig } from './types';

export const DEFAULT_CONFIG: SmcConfig = {
  pivotLen: 5,
  obLookback: 50,
  riskPerTrade: 1.0,
  symbols: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'SUIUSDT'],
  timeframes: {
    htf: '4H', // 4 Hour
    ltf: '15m', // 15 Minute
  }
};

// Point to our secure backend proxy
export const API_BASE_URL = '/api/v1';

// Mapping app timeframes to API granularity strings
export const TIMEFRAME_MAP: Record<string, string> = {
  '1m': '1m',
  '5m': '5m',
  '15m': '15m',
  '1H': '1H',
  '4H': '4H',
  '1D': '1D',
};
