"""
MT5 Forex & Metals SMC Auto-Trading Loop (Business Days Only)
═════════════════════════════════════════════════════════════

Dedicated multi-timeframe SMC analysis & execution loop for MetaTrader 5
Forex/Metals symbols (EURUSD.s, GBPUSD.s, USDJPY.s, XAUUSD.s).

Features:
  - Uses real MT5 OHLCV candles (4h HTF bias, 15m MTF sweep/OB/FVG, 5m LTF trigger)
  - Strict business-day guard via `is_mt5_forex_market_open()`
  - Automatic pre-close / weekend / rollover flatten (never leaves Forex open during closure)
  - Broadcasts accepted signals to dashboard WebSocket even when AUTO_EXECUTE is paused
  - Independent MT5 execution respecting MT5_MAX_LOT_SIZE, MT5_MAX_OPEN_PER_SYMBOL,
    MT5_ALLOW_LIVE=false, signal_id deduplication, and 300s per-symbol cooldown
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.execution.mt5_executor import (
    MT5Executor,
    is_mt5_execution_enabled,
    is_mt5_forex_market_open,
)

logger = logging.getLogger("smarttrade.mt5_forex_smc")


def _ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def _atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        pc = float(candles[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent) if recent else 0.0


def evaluate_forex_smc_signal(
    symbol: str,
    htf_candles: List[Dict[str, Any]],
    mtf_candles: List[Dict[str, Any]],
    ltf_candles: List[Dict[str, Any]],
    min_rr: float = 1.5,
) -> Optional[Dict[str, Any]]:
    """
    Deterministic multi-timeframe SMC evaluation on MT5 Forex/Metal candles:
      1. HTF (4h): directional bias via swing structure + EMA20/EMA50 alignment.
      2. MTF (15m): liquidity sweep of recent swing low/high OR active FVG in HTF bias.
      3. LTF (5m): rejection / engulfing trigger confirming entry in bias direction.
      4. Risk/Reward strictly >= min_rr (default 1.5, target 2.0R).
    """
    if len(htf_candles) < 50 or len(mtf_candles) < 30 or len(ltf_candles) < 15:
        return None

    # 1. HTF (4h) Bias
    htf_closes = [float(c["close"]) for c in htf_candles]
    htf_ema20 = _ema(htf_closes[-60:], 20)
    htf_ema50 = _ema(htf_closes[-80:], 50)
    htf_last = htf_closes[-1]
    if htf_ema20 > htf_ema50 and htf_last >= htf_ema50:
        bias = "long"
    elif htf_ema20 < htf_ema50 and htf_last <= htf_ema50:
        bias = "short"
    else:
        return None

    # 2. MTF (15m) Liquidity Sweep or FVG alignment
    mtf_window = mtf_candles[-25:-1]
    mtf_last = mtf_candles[-1]
    mtf_prev = mtf_candles[-2]
    mtf_atr = _atr(mtf_candles, 14)
    if not math.isfinite(mtf_atr) or mtf_atr <= 0:
        return None

    prior_low = min(float(c["low"]) for c in mtf_window[:-2])
    prior_high = max(float(c["high"]) for c in mtf_window[:-2])

    setup_type: Optional[str] = None
    if bias == "long":
        swept_low = (
            min(float(mtf_last["low"]), float(mtf_prev["low"])) < prior_low
            and float(mtf_last["close"]) > prior_low
        )
        # Bullish FVG: candle[i-2].high < candle[i].low and price retesting near gap
        fvg_bull = any(
            float(mtf_candles[i - 2]["high"]) < float(mtf_candles[i]["low"])
            and float(mtf_last["low"]) <= float(mtf_candles[i]["low"]) + 0.5 * mtf_atr
            and float(mtf_last["close"]) >= float(mtf_candles[i - 2]["high"])
            for i in range(len(mtf_candles) - 8, len(mtf_candles) - 1)
        )
        if swept_low:
            setup_type = "SMC_SWEEP_LONG"
        elif fvg_bull:
            setup_type = "SMC_FVG_LONG"
    else:
        swept_high = (
            max(float(mtf_last["high"]), float(mtf_prev["high"])) > prior_high
            and float(mtf_last["close"]) < prior_high
        )
        fvg_bear = any(
            float(mtf_candles[i - 2]["low"]) > float(mtf_candles[i]["high"])
            and float(mtf_last["high"]) >= float(mtf_candles[i]["high"]) - 0.5 * mtf_atr
            and float(mtf_last["close"]) <= float(mtf_candles[i - 2]["low"])
            for i in range(len(mtf_candles) - 8, len(mtf_candles) - 1)
        )
        if swept_high:
            setup_type = "SMC_SWEEP_SHORT"
        elif fvg_bear:
            setup_type = "SMC_FVG_SHORT"

    if not setup_type:
        return None

    # 3. LTF (5m) Entry Trigger (bullish/bearish momentum confirmation)
    ltf_last = ltf_candles[-1]
    ltf_prev = ltf_candles[-2]
    l_open, l_close = float(ltf_last["open"]), float(ltf_last["close"])
    p_high, p_low = float(ltf_prev["high"]), float(ltf_prev["low"])

    if bias == "long" and not (l_close > l_open and l_close > p_high):
        return None
    if bias == "short" and not (l_close < l_open and l_close < p_low):
        return None

    # 4. SL / TP calculation (strictly >= min_rr, target 2.0R)
    entry = l_close
    sl_buf = max(mtf_atr * 1.2, abs(entry) * 0.0008)
    if bias == "long":
        swing_sl = min(float(ltf_last["low"]), float(ltf_prev["low"])) - (mtf_atr * 0.25)
        stop_loss = min(swing_sl, entry - sl_buf * 0.5)
        risk_dist = entry - stop_loss
        if risk_dist <= 0:
            return None
        take_profit = entry + risk_dist * max(2.0, min_rr)
    else:
        swing_sl = max(float(ltf_last["high"]), float(ltf_prev["high"])) + (mtf_atr * 0.25)
        stop_loss = max(swing_sl, entry + sl_buf * 0.5)
        risk_dist = stop_loss - entry
        if risk_dist <= 0:
            return None
        take_profit = entry - risk_dist * max(2.0, min_rr)

    rr = abs(take_profit - entry) / risk_dist
    if rr < min_rr:
        return None

    return {
        "symbol": symbol,
        "direction": bias,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "rr_ratio": round(rr, 2),
        "setup": setup_type,
        "regime": "TREND",
        "confidence": 0.78 if "SWEEP" in setup_type else 0.70,
        "timestamp": int(ltf_last["timestamp"]),
        "broker": "mt5",
    }


async def run_mt5_forex_smc_loop(
    mt5_executor: MT5Executor,
    ws_manager: Optional[Any] = None,
) -> None:
    """Background loop scanning MT5 Forex/Metal symbols with SMC on business days."""
    raw_syms = os.getenv("MT5_FOREX_SYMBOLS", "EURUSD.s,GBPUSD.s,USDJPY.s,XAUUSD.s")
    symbols = [s.strip() for s in raw_syms.split(",") if s.strip()]
    interval_sec = max(15, int(os.getenv("MT5_FOREX_LOOP_INTERVAL_SEC", "60")))
    cooldown_sec = float(os.getenv("TRADE_COOLDOWN_SEC", "300"))
    min_rr = float(os.getenv("MIN_RR", "1.5"))

    seen_signal_ids: set = set()
    last_order_ts: Dict[str, float] = {}

    logger.info(
        "[MT5_FOREX_SMC] Started business-day SMC loop | symbols=%s | interval=%ds | min_rr=%.2f",
        symbols, interval_sec, min_rr,
    )

    while True:
        try:
            if not getattr(mt5_executor, "is_connected", False):
                await asyncio.sleep(interval_sec)
                continue

            mkt_open, mkt_reason = is_mt5_forex_market_open()
            if not mkt_open:
                # Enforce flatten on every cycle during closure
                await mt5_executor.enforce_market_close_flatten()
                await asyncio.sleep(interval_sec)
                continue

            import smc_futures.smc_config as smc_cfg
            auto_exec = str(smc_cfg.SMC_CONFIG.get("AUTO_EXECUTE_MODE", "false")).strip().lower() in ("1", "true", "yes")

            for sym in symbols:
                try:
                    htf = await mt5_executor.get_candles(sym, "4h", 100)
                    mtf = await mt5_executor.get_candles(sym, "15m", 80)
                    ltf = await mt5_executor.get_candles(sym, "5m", 40)

                    sig = evaluate_forex_smc_signal(sym, htf, mtf, ltf, min_rr=min_rr)
                    if not sig:
                        continue

                    sig_id = f"MT5FX_{sym}_{sig['direction']}_{sig['timestamp']}"
                    if sig_id in seen_signal_ids:
                        continue
                    seen_signal_ids.add(sig_id)
                    if len(seen_signal_ids) > 2000:
                        seen_signal_ids.clear()

                    logger.info(
                        "[MT5_FOREX_SMC] SIGNAL | %s %s @ %.5f | SL=%.5f | TP=%.5f | RR=%.2f | %s",
                        sym, sig["direction"].upper(), sig["entry"], sig["stop_loss"],
                        sig["take_profit"], sig["rr_ratio"], sig["setup"],
                    )

                    # Always broadcast to dashboard UI (even if Start/auto-exec is paused)
                    if ws_manager is not None:
                        try:
                            await ws_manager.broadcast({
                                "type": "signal",
                                "symbol": sym,
                                "signal": sig,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })
                        except Exception:
                            pass

                    if not (auto_exec and is_mt5_execution_enabled()):
                        continue

                    now_ts = time.time()
                    if cooldown_sec > 0 and (now_ts - last_order_ts.get(sym, 0.0)) < cooldown_sec:
                        continue

                    # Compute conservative lot size capped by MT5_MAX_LOT_SIZE
                    max_lot = float(os.getenv("MT5_MAX_LOT_SIZE", "0.10"))
                    lot_size = min(max_lot, max(0.01, round(max_lot, 2)))
                    side = "buy" if sig["direction"] == "long" else "sell"
                    res = await mt5_executor.place_order(
                        symbol=sym,
                        side=side,
                        quantity=lot_size,
                        order_type="market",
                        stop_loss=sig["stop_loss"],
                        take_profit=sig["take_profit"],
                        quantity_in_lots=True,
                        comment=f"SMC-FX-{sig['direction'][:1].upper()}",
                    )
                    # Reflect MT5 Forex executions in MultiBrokerEngine dashboard counters
                    try:
                        from backend.routes.multi_broker import _engine as _mbe_stats
                        if _mbe_stats is not None and hasattr(_mbe_stats, "_stats"):
                            _mbe_stats._stats["orders_sent"] = _mbe_stats._stats.get("orders_sent", 0) + 1
                            if res.get("success"):
                                _mbe_stats._stats["orders_filled"] = _mbe_stats._stats.get("orders_filled", 0) + 1
                            else:
                                _mbe_stats._stats["orders_failed"] = _mbe_stats._stats.get("orders_failed", 0) + 1
                    except Exception:
                        pass
                    if res.get("success"):
                        last_order_ts[sym] = time.time()
                        logger.info(
                            "[MT5_FOREX_SMC] EXECUTED | %s %s %.2f lot | order_id=%s",
                            sym, side.upper(), lot_size, res.get("order_id"),
                        )
                    else:
                        logger.warning(
                            "[MT5_FOREX_SMC] REJECTED | %s | %s (%s)",
                            sym, res.get("error"), res.get("code"),
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as sym_err:
                    logger.error("[MT5_FOREX_SMC] Error scanning %s: %s", sym, sym_err)

                await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info("[MT5_FOREX_SMC] Loop cancelled cleanly")
            raise
        except Exception as loop_err:
            logger.error("[MT5_FOREX_SMC] Loop exception: %s", loop_err)

        await asyncio.sleep(interval_sec)
