import { Candle, MarketStructure, Pivot, OrderBlock, Trend, TradeSignal } from '../types';

/**
 * Finds Pivot Highs and Lows based on neighbors.
 * @param candles OHLCV Data
 * @param len Left and Right lookback length
 */
const findPivots = (candles: Candle[], len: number): Pivot[] => {
  const pivots: Pivot[] = [];
  
  // We need at least len * 2 + 1 candles
  for (let i = len; i < candles.length - len; i++) {
    const current = candles[i];
    
    // Check High
    let isHigh = true;
    for (let k = 1; k <= len; k++) {
      if (candles[i - k].high > current.high || candles[i + k].high > current.high) {
        isHigh = false;
        break;
      }
    }

    // Check Low
    let isLow = true;
    for (let k = 1; k <= len; k++) {
      if (candles[i - k].low < current.low || candles[i + k].low < current.low) {
        isLow = false;
        break;
      }
    }

    if (isHigh) {
      pivots.push({ index: i, price: current.high, type: 'HIGH', time: current.time, broken: false });
    }
    if (isLow) {
      pivots.push({ index: i, price: current.low, type: 'LOW', time: current.time, broken: false });
    }
  }
  return pivots;
};

/**
 * Identifies Order Blocks based on BOS sequence.
 * Simplified Definition:
 * Bullish OB: The last bearish candle before a move that broke a Swing High.
 * Bearish OB: The last bullish candle before a move that broke a Swing Low.
 */
const findOrderBlocks = (candles: Candle[], pivots: Pivot[]): OrderBlock[] => {
  const obs: OrderBlock[] = [];
  
  // Naive approach: Look for strong moves originating from a pivot area
  // Ideally, this tracks BOS events. Here we iterate pivots and look for breaks.
  
  // Only considering unmitigated for simplicity in this demo
  const recentPivots = pivots.slice(-10); 
  
  // Placeholder logic for visual demonstration
  // Real implementation requires tracking BOS events sequentially
  
  // Find Bullish OBs
  // Logic: Find a pivot low that was followed by a break of a recent pivot high
  // For demo, we will just take the lowest down-candle before a strong rally 
  
  // Let's implement a simplified detection:
  // 1. Detect Impulse Move (3 consecutive green candles with increasing volume or large body)
  // 2. The candle before that sequence is potential OB
  
  for(let i = 20; i < candles.length - 5; i++) {
     const c1 = candles[i];
     const c2 = candles[i+1];
     const c3 = candles[i+2];

     // Bullish OB Criteria: Red candle followed by strong Green expansion
     if(c1.close < c1.open && c2.close > c2.open && c3.close > c3.open) {
        // Check if c2+c3 moves significantly (e.g., > 1% move)
        const move = (c3.close - c1.low) / c1.low;
        if(move > 0.005) {
            obs.push({
                type: 'BULLISH',
                top: c1.high,
                bottom: c1.low,
                startIndex: i,
                startTime: c1.time,
                mitigated: false
            });
        }
     }

     // Bearish OB Criteria
     if(c1.close > c1.open && c2.close < c2.open && c3.close < c3.open) {
         const move = (c1.high - c3.close) / c1.high;
         if(move > 0.005) {
             obs.push({
                 type: 'BEARISH',
                 top: c1.high,
                 bottom: c1.low,
                 startIndex: i,
                 startTime: c1.time,
                 mitigated: false
             });
         }
     }
  }

  // Filter nearby/overlapping OBs, keep recent
  return obs.filter(ob => (candles.length - ob.startIndex) < 100); 
};

/**
 * Main Analysis Function
 */
export const analyzeMarket = (candles: Candle[], config: { pivotLen: number }): MarketStructure => {
  if (candles.length < 50) {
    return {
      trend: Trend.NEUTRAL,
      lastBosIndex: null,
      lastChochIndex: null,
      pivots: [],
      orderBlocks: [],
      lastSweep: null
    };
  }

  const pivots = findPivots(candles, config.pivotLen);
  const orderBlocks = findOrderBlocks(candles, pivots);

  // Determine Trend based on last two high/low pivots
  const highs = pivots.filter(p => p.type === 'HIGH');
  const lows = pivots.filter(p => p.type === 'LOW');

  let trend = Trend.NEUTRAL;
  if (highs.length >= 2 && lows.length >= 2) {
    const lastHigh = highs[highs.length - 1];
    const prevHigh = highs[highs.length - 2];
    const lastLow = lows[lows.length - 1];
    const prevLow = lows[lows.length - 2];

    if (lastHigh.price > prevHigh.price && lastLow.price > prevLow.price) {
      trend = Trend.BULLISH;
    } else if (lastHigh.price < prevHigh.price && lastLow.price < prevLow.price) {
      trend = Trend.BEARISH;
    }
  }

  // Detect Sweep (Liquidity Grab)
  // Logic: Price wicks below a recent low pivot but closes above it (Bullish Sweep)
  let lastSweep = null;
  const recentLow = lows[lows.length - 1];
  const recentHigh = highs[highs.length - 1];
  const lastCandle = candles[candles.length - 1];

  if (recentLow && lastCandle.low < recentLow.price && lastCandle.close > recentLow.price) {
      lastSweep = { type: 'LOW', time: lastCandle.time, price: recentLow.price };
  } else if (recentHigh && lastCandle.high > recentHigh.price && lastCandle.close < recentHigh.price) {
      // @ts-ignore
      lastSweep = { type: 'HIGH', time: lastCandle.time, price: recentHigh.price };
  }

  return {
    trend,
    pivots,
    orderBlocks,
    lastBosIndex: null, // Simplified for this implementation
    lastChochIndex: null,
    lastSweep
  };
};

export const generateSignal = (
  symbol: string, 
  htf: MarketStructure, 
  ltf: MarketStructure, 
  currentPrice: number
): TradeSignal | null => {
  
  // Strategy: HTF Trend + LTF Sweep/OB
  
  // LONG SETUP
  if (htf.trend === Trend.BULLISH || htf.trend === Trend.NEUTRAL) {
    // Check LTF for Bullish OB or Liquidity Sweep of Lows
    const bullishOB = ltf.orderBlocks.find(ob => ob.type === 'BULLISH' && !ob.mitigated && currentPrice > ob.bottom && currentPrice < ob.top * 1.01);
    
    if (bullishOB || (ltf.lastSweep && ltf.lastSweep.type === 'LOW')) {
       const stopLoss = bullishOB ? bullishOB.bottom * 0.998 : (ltf.lastSweep?.price || currentPrice) * 0.995;
       const risk = currentPrice - stopLoss;
       
       return {
         symbol,
         timestamp: Date.now(),
         type: 'LONG',
         entryPrice: currentPrice,
         stopLoss: parseFloat(stopLoss.toFixed(4)),
         tp1: parseFloat((currentPrice + risk * 2).toFixed(4)),
         tp2: parseFloat((currentPrice + risk * 4).toFixed(4)),
         confidence: bullishOB ? 'HIGH' : 'MEDIUM',
         reason: [
           `HTF Trend: ${htf.trend}`,
           bullishOB ? 'Retesting Bullish Order Block' : 'Liquidity Sweep of Lows',
         ]
       };
    }
  }

  // SHORT SETUP
  if (htf.trend === Trend.BEARISH || htf.trend === Trend.NEUTRAL) {
    const bearishOB = ltf.orderBlocks.find(ob => ob.type === 'BEARISH' && !ob.mitigated && currentPrice < ob.top && currentPrice > ob.bottom * 0.99);

    if (bearishOB || (ltf.lastSweep && ltf.lastSweep.type === 'HIGH')) {
        const stopLoss = bearishOB ? bearishOB.top * 1.002 : (ltf.lastSweep?.price || currentPrice) * 1.005;
        const risk = stopLoss - currentPrice;

        return {
          symbol,
          timestamp: Date.now(),
          type: 'SHORT',
          entryPrice: currentPrice,
          stopLoss: parseFloat(stopLoss.toFixed(4)),
          tp1: parseFloat((currentPrice - risk * 2).toFixed(4)),
          tp2: parseFloat((currentPrice - risk * 4).toFixed(4)),
          confidence: bearishOB ? 'HIGH' : 'MEDIUM',
          reason: [
            `HTF Trend: ${htf.trend}`,
            bearishOB ? 'Retesting Bearish Order Block' : 'Liquidity Sweep of Highs'
          ]
        };
    }
  }

  return null;
};

/**
 * Generates a human-readable analysis commentary based on SMC logic.
 */
export const getSmcCommentary = (htf: MarketStructure, ltf: MarketStructure, currentPrice: number): string => {
    // 1. Analyze HTF Trend
    const trendIcon = htf.trend === Trend.BULLISH ? "🚀" : htf.trend === Trend.BEARISH ? "📉" : "⚖️";
    let commentary = `${trendIcon} HTF Trend is ${htf.trend}. `;

    // 2. Analyze LTF Context
    const bullishOB = ltf.orderBlocks.find(ob => ob.type === 'BULLISH' && !ob.mitigated && currentPrice > ob.bottom && currentPrice < ob.top * 1.05);
    const bearishOB = ltf.orderBlocks.find(ob => ob.type === 'BEARISH' && !ob.mitigated && currentPrice < ob.top && currentPrice > ob.bottom * 0.95);
    
    // Check for Sweeps (Stop Hunts)
    if (ltf.lastSweep) {
        if (ltf.lastSweep.type === 'LOW' && (htf.trend === Trend.BULLISH || htf.trend === Trend.NEUTRAL)) {
            return commentary + "Liquidity Sweep detected at recent Lows (Sell-side Liquidity). This is a strong potential reversal signal for a Long entry.";
        }
        if (ltf.lastSweep.type === 'HIGH' && (htf.trend === Trend.BEARISH || htf.trend === Trend.NEUTRAL)) {
            return commentary + "Liquidity Sweep detected at recent Highs (Buy-side Liquidity). Smart money may be accumulating shorts here.";
        }
    }

    // Check Order Blocks
    if (bullishOB) {
        return commentary + `Price is retesting a Bullish Order Block (Demand Zone) around ${bullishOB.top.toFixed(2)}. Look for lower timeframe confirmation to buy.`;
    }
    if (bearishOB) {
        return commentary + `Price is entering a Bearish Order Block (Supply Zone) near ${bearishOB.bottom.toFixed(2)}. Watch for rejection candles to sell.`;
    }

    // General Advice based on Trend
    if (htf.trend === Trend.BULLISH) {
        return commentary + "Market structure is making Higher Highs. Avoid counter-trend shorts. Wait for a pullback to an Order Block or a Pivot Low sweep to join the trend.";
    } else if (htf.trend === Trend.BEARISH) {
        return commentary + "Market structure is making Lower Lows. Rally is for selling. Wait for price to tap into premium supply zones.";
    } else {
        return commentary + "Market is ranging without clear direction. Price is trapped between equilibrium. Wait for a clear Break of Structure (BOS) before committing to a bias.";
    }
};