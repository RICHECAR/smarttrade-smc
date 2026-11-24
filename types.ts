export enum Trend {
  BULLISH = 'BULLISH',
  BEARISH = 'BEARISH',
  NEUTRAL = 'NEUTRAL',
}

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Pivot {
  index: number;
  price: number;
  type: 'HIGH' | 'LOW';
  time: number;
  broken: boolean; // if price has crossed this pivot
}

export interface OrderBlock {
  type: 'BULLISH' | 'BEARISH';
  top: number;
  bottom: number;
  startIndex: number;
  startTime: number;
  mitigated: boolean;
}

export interface MarketStructure {
  trend: Trend;
  lastBosIndex: number | null;
  lastChochIndex: number | null;
  pivots: Pivot[];
  orderBlocks: OrderBlock[];
  lastSweep: { type: 'HIGH' | 'LOW'; time: number; price: number } | null;
}

export interface SmcConfig {
  pivotLen: number; // Left/Right lookback for pivot detection
  obLookback: number; // Candles to look back for OB
  riskPerTrade: number; // %
  symbols: string[];
  timeframes: {
    htf: string;
    ltf: string;
  };
}

export interface TradeSignal {
  symbol: string;
  timestamp: number;
  type: 'LONG' | 'SHORT';
  entryPrice: number;
  stopLoss: number;
  tp1: number;
  tp2: number;
  confidence: 'HIGH' | 'MEDIUM' | 'LOW';
  reason: string[];
}

// Ticker API Types
export interface Ticker {
  symbol: string;
  lastPr: string;
  change24h: string;
  high24h: string;
  low24h: string;
  isReal?: boolean;
  markPrice?: string;
  indexPrice?: string;
  stale?: boolean;
  source?: string;
}

export interface Position {
  symbol: string;
  holdSide: string; // 'long' or 'short'
  marginCoin: string;
  available: string; // size available to close
  total: string; // total size
  openPriceAvg: string;
  unrealizedPL: string;
  leverage: number;
  achievedProfits: string;
}

export interface ApiConfig {
  apiKey: string;
  secretKey: string;
  passphrase: string;
  liveMode: boolean;
}