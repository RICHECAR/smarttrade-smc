"""
Stats API Routes - Analytics and performance statistics

SOURCE OF TRUTH: live_trades table (LiveTrade model)
All metrics are computed from DB only — no runtime state, no cache, no WebSocket.

Endpoints:
- GET /api/stats/summary - Summary statistics (PnL, win rate, etc.)
- GET /api/stats/rr-series - R:R values over time for charting
- GET /api/stats/heatmap - Trade activity heatmap by day/hour
- GET /api/stats/equity-curve - Cumulative PnL equity curve

Author: SMC Trading Bot Framework
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import date, datetime, timezone
from typing import Dict, Any, List
from collections import defaultdict
import logging

from backend.models.live_trade import LiveTrade
from backend.services.live_trades_service import get_db_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/stats", tags=["stats"])


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _to_utc(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware (UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ═══════════════════════════════════════════════════════════════════════════════
# /api/stats/summary
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/summary")
def summary(
    from_: date = Query(..., alias="from", description="Start date"),
    to: date = Query(..., description="End date"),
) -> Dict[str, Any]:
    """
    Calculate summary statistics from live_trades (DB source of truth).

    Rules:
      - Only status='CLOSED' AND pnl IS NOT NULL
      - Date filter on ts_close (not ts_open)
      - FAILED/SKIPPED/PENDING/ARCHIVED excluded
    """
    try:
        start_dt = _to_utc(datetime.combine(from_, datetime.min.time()))
        end_dt = _to_utc(datetime.combine(to, datetime.max.time()))

        with get_db_session() as db:
            # Fetch closed trades by ts_close (the correct date for realized PnL)
            closed_with_pnl = (
                db.query(LiveTrade)
                .filter(
                    LiveTrade.status == "CLOSED",
                    LiveTrade.pnl != None,
                    LiveTrade.ts_close >= start_dt,
                    LiveTrade.ts_close <= end_dt,
                )
                .all()
            )

            # Counts for debugging
            closed_unknown = (
                db.query(LiveTrade)
                .filter(
                    LiveTrade.status == "CLOSED",
                    LiveTrade.pnl == None,
                    LiveTrade.ts_close >= start_dt,
                    LiveTrade.ts_close <= end_dt,
                )
                .count()
            )

            total_in_db = (
                db.query(LiveTrade)
                .filter(
                    LiveTrade.ts_open >= start_dt,
                    LiveTrade.ts_open <= end_dt,
                )
                .count()
            )

            failed_count = (
                db.query(LiveTrade)
                .filter(
                    LiveTrade.status.in_(["FAILED", "SKIPPED", "ARCHIVED"]),
                    LiveTrade.ts_open >= start_dt,
                    LiveTrade.ts_open <= end_dt,
                )
                .count()
            )

            logger.info(
                "[STATS] total=%d | closed_with_pnl=%d | from=%s | to=%s",
                total_in_db, len(closed_with_pnl), from_, to,
            )

            if not closed_with_pnl:
                empty = _compute_metrics([])
                empty.update({
                    "closed_with_pnl": 0, "closed_unknown": closed_unknown,
                    "failed_trades": failed_count, "total_in_db": total_in_db,
                    "by_broker": {},
                    "reason": "no_closed_trades_with_pnl",
                })
                return empty

            # Use unified metrics calculator
            metrics = _compute_metrics(closed_with_pnl)

            # ── Per-broker breakdown ─────────────────────────────────
            brokers_seen = set(t.broker or "kraken" for t in closed_with_pnl)
            by_broker = {}
            for bk in brokers_seen:
                bk_trades = [t for t in closed_with_pnl if (t.broker or "kraken") == bk]
                bk_m = _compute_metrics(bk_trades)
                by_broker[bk] = {
                    "trades": bk_m["total_trades"],
                    "pnl": bk_m["total_pnl"],
                    "win_rate": bk_m["win_rate"],
                    "wins": bk_m["wins"],
                    "losses": bk_m["losses"],
                }

            # Expunge to avoid detached instance errors
            for t in closed_with_pnl:
                db.expunge(t)

        # Map _compute_metrics keys to legacy /summary keys for backward compat
        result = {
            "pnl": metrics["total_pnl"],
            "win_rate": metrics["win_rate"],
            "avg_rr": metrics["avg_rr"],
            "trades": metrics["total_trades"],
            "max_dd": metrics["max_drawdown"],
            "profit_factor": metrics["profit_factor"],
            "wins": metrics["wins"],
            "losses": metrics["losses"],
            "break_even": metrics["break_even"],
            "avg_win": metrics["avg_win"],
            "avg_loss": metrics["avg_loss"],
            "best_trade": metrics["best_trade"],
            "worst_trade": metrics["worst_trade"],
            "avg_hold_time_hours": metrics["avg_hold_time_hours"],
            "expectancy": metrics["expectancy"],
            "sharpe_ratio": metrics["sharpe_ratio"],
            "calmar_ratio": metrics["calmar_ratio"],
            "closed_with_pnl": len(closed_with_pnl),
            "closed_unknown": closed_unknown,
            "failed_trades": failed_count,
            "total_in_db": total_in_db,
            "by_broker": by_broker,
        }

        logger.info(
            "[STATS] Summary: PnL=%s, WR=%s, Trades=%s, ByBroker=%s",
            result["pnl"], f"{result['win_rate']:.2%}", result["trades"],
            {k: v["pnl"] for k, v in by_broker.items()},
        )
        return result

    except Exception as e:
        logger.error(f"[STATS] Error calculating summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# /api/stats/rr-series
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/rr-series")
def rr_series(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
) -> List[Dict[str, Any]]:
    """
    Get R:R values over time for charting.
    Source: live_trades WHERE status=CLOSED AND pnl IS NOT NULL.
    """
    try:
        start_dt = _to_utc(datetime.combine(from_, datetime.min.time()))
        end_dt = _to_utc(datetime.combine(to, datetime.max.time()))

        with get_db_session() as db:
            trades = (
                db.query(LiveTrade)
                .filter(
                    LiveTrade.ts_close >= start_dt,
                    LiveTrade.ts_close <= end_dt,
                    LiveTrade.status == "CLOSED",
                    LiveTrade.pnl != None,
                )
                .order_by(LiveTrade.ts_close.asc())
                .all()
            )

            cum_pnl = 0.0
            series = []
            for t in trades:
                cum_pnl += t.pnl
                series.append({
                    "ts": (t.ts_close or t.ts_open).isoformat() if (t.ts_close or t.ts_open) else None,
                    "rr": round(t.rr or 0, 2),
                    "pnl": round(t.pnl, 2),
                    "cum_pnl": round(cum_pnl, 2),
                    "symbol": t.symbol,
                    "side": t.side,
                    "result": t.result,
                    "broker": t.broker or "kraken",
                })

            for t in trades:
                db.expunge(t)

        logger.info(f"[STATS] RR series: {len(series)} points")
        return series

    except Exception as e:
        logger.error(f"[STATS] Error getting RR series: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# /api/stats/heatmap
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/heatmap")
def heatmap(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
) -> List[Dict[str, Any]]:
    """
    Get trade activity heatmap data by day of week and hour.
    Source: live_trades WHERE status=CLOSED AND pnl IS NOT NULL.
    """
    try:
        start_dt = _to_utc(datetime.combine(from_, datetime.min.time()))
        end_dt = _to_utc(datetime.combine(to, datetime.max.time()))

        with get_db_session() as db:
            trades = (
                db.query(LiveTrade)
                .filter(
                    LiveTrade.ts_close >= start_dt,
                    LiveTrade.ts_close <= end_dt,
                    LiveTrade.status == "CLOSED",
                    LiveTrade.pnl != None,
                )
                .all()
            )

            cells = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})

            for t in trades:
                if getattr(t, "status", None) != "CLOSED" or getattr(t, "pnl", None) is None:
                    continue
                    
                ts = t.ts_close or t.ts_open
                if ts:
                    dow = (ts.weekday() + 1) % 7  # 0=Sunday
                    hour = ts.hour
                    key = (dow, hour)
                    cells[key]["trades"] += 1
                    cells[key]["pnl"] += t.pnl
                    if t.result == "win" or (t.result is None and t.pnl > 0):
                        cells[key]["wins"] += 1

            for t in trades:
                db.expunge(t)

        result = []
        for (dow, hour), data in cells.items():
            wr = data["wins"] / data["trades"] if data["trades"] > 0 else 0.0
            result.append({
                "dow": dow, "hour": hour,
                "trades": data["trades"],
                "pnl": round(data["pnl"], 2),
                "win_rate": round(wr, 4),
            })
        result.sort(key=lambda x: (x["dow"], x["hour"]))

        logger.info(f"[STATS] Heatmap: {len(result)} cells")
        return result

    except Exception as e:
        logger.error(f"[STATS] Error getting heatmap: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# /api/stats/equity-curve
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/equity-curve")
def equity_curve(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
) -> List[Dict[str, Any]]:
    """
    Get equity curve data for charting.
    Source: live_trades WHERE status=CLOSED AND pnl IS NOT NULL.
    Ordered by ts_close (actual close time).
    """
    try:
        start_dt = _to_utc(datetime.combine(from_, datetime.min.time()))
        end_dt = _to_utc(datetime.combine(to, datetime.max.time()))

        with get_db_session() as db:
            trades = (
                db.query(LiveTrade)
                .filter(
                    LiveTrade.ts_close >= start_dt,
                    LiveTrade.ts_close <= end_dt,
                    LiveTrade.status == "CLOSED",
                    LiveTrade.pnl != None,
                )
                .order_by(LiveTrade.ts_close.asc())
                .all()
            )

            equity = 0.0
            peak = 0.0
            max_dd = 0.0
            curve = [{"ts": start_dt.isoformat(), "equity": 0.0, "peak": 0.0,
                       "drawdown": 0.0, "drawdown_pct": 0.0}]

            for t in trades:
                equity += t.pnl
                equity_r = round(equity, 2)
                peak = max(peak, equity_r)
                dd = round(peak - equity_r, 2)
                dd_pct = round((dd / peak) * 100, 2) if peak > 0 else 0.0
                max_dd = max(max_dd, dd)
                ts = t.ts_close or t.ts_open
                curve.append({
                    "ts": ts.isoformat() if ts else None,
                    "equity": equity_r,
                    "peak": round(peak, 2),
                    "drawdown": dd,
                    "drawdown_pct": dd_pct,
                })

            for t in trades:
                db.expunge(t)

        return curve

    except Exception as e:
        logger.error(f"[STATS] Error getting equity curve: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER — Calculate institutional-grade metrics from a list of closed trades
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_metrics(trades: list) -> Dict[str, Any]:
    """
    Compute comprehensive institutional-grade metrics from CLOSED LiveTrade objects.
    All trades MUST have pnl != None.
    Returns a dict matching PerformanceStats interface expected by frontend.
    """
    import math

    _EMPTY = {
        "total_pnl": 0.0, "win_rate": 0.0, "avg_rr": 0.0,
        "total_trades": 0, "max_drawdown": 0.0, "profit_factor": 0.0,
        "wins": 0, "losses": 0, "break_even": 0,
        "avg_win": 0.0, "avg_loss": 0.0,
        "best_trade": 0.0, "worst_trade": 0.0,
        "avg_hold_time_hours": 0.0,
        # Institutional
        "expectancy": 0.0, "sharpe_ratio": 0.0, "sortino_ratio": 0.0,
        "calmar_ratio": 0.0, "avg_pnl_per_trade": 0.0,
        "kelly_criterion": 0.0, "skewness": 0.0, "kurtosis": 0.0,
        "exposure_time_pct": 0.0,
        "current_streak": 0, "max_win_streak": 0, "max_loss_streak": 0,
    }

    if not trades:
        return _EMPTY

    pnls = [t.pnl for t in trades]
    total_pnl = sum(pnls)
    n = len(pnls)

    # ── Win / Loss / Break-even classification ──
    # Unified: use result field first, fallback to pnl sign.
    # Break-even threshold: |pnl| <= 0.01 USDT
    wins = [t for t in trades if t.result == "win" or (t.result is None and t.pnl > 0.01)]
    losses = [t for t in trades if t.result == "loss" or (t.result is None and t.pnl < -0.01)]
    breakeven = [t for t in trades if abs(t.pnl) <= 0.01 and t.result not in ("win", "loss")]

    decisive = len(wins) + len(losses)
    win_rate = len(wins) / decisive if decisive > 0 else 0.0

    rr_trades = [t for t in trades if t.rr is not None]
    avg_rr = sum(t.rr for t in rr_trades) / len(rr_trades) if rr_trades else 0.0

    gross_profit = sum(t.pnl for t in wins if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in losses if t.pnl < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
        gross_profit if gross_profit > 0 else 0.0
    )

    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0

    best_trade = max(pnls)
    worst_trade = min(pnls)

    # ── Time-sorted for drawdown & streaks ──
    sorted_trades = sorted(
        trades,
        key=lambda x: (x.ts_close or x.ts_open or datetime.min.replace(tzinfo=timezone.utc)),
    )

    # ── Max drawdown ──
    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted_trades:
        cum_pnl += t.pnl
        peak = max(peak, cum_pnl)
        dd = peak - cum_pnl
        max_dd = max(max_dd, dd)

    # ── Hold time + exposure time ──
    hold_times_hrs = []
    total_hold_seconds = 0.0
    for t in trades:
        if t.ts_close and t.ts_open:
            delta_s = (t.ts_close - t.ts_open).total_seconds()
            hold_times_hrs.append(delta_s / 3600)
            total_hold_seconds += delta_s
    avg_hold = sum(hold_times_hrs) / len(hold_times_hrs) if hold_times_hrs else 0.0

    # Exposure time: fraction of calendar period actually in a position
    if sorted_trades and sorted_trades[0].ts_open and sorted_trades[-1].ts_close:
        calendar_seconds = (sorted_trades[-1].ts_close - sorted_trades[0].ts_open).total_seconds()
        exposure_time_pct = (total_hold_seconds / calendar_seconds * 100) if calendar_seconds > 0 else 0.0
    else:
        exposure_time_pct = 0.0

    # ── Institutional metrics ──
    mean_pnl = total_pnl / n
    avg_pnl_per_trade = mean_pnl

    # Expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
    loss_rate = len(losses) / decisive if decisive > 0 else 0.0
    expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)

    # Sharpe ratio = mean(PnL) / stdev(PnL)
    if n > 1:
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (n - 1)
        std_pnl = math.sqrt(variance) if variance > 0 else 0.0
        sharpe_ratio = mean_pnl / std_pnl if std_pnl > 0 else 0.0
    else:
        std_pnl = 0.0
        sharpe_ratio = 0.0

    # Sortino ratio = mean(PnL) / downside_deviation
    # Downside deviation: stdev of negative returns only (target=0)
    downside_sq = [p ** 2 for p in pnls if p < 0]
    if downside_sq:
        downside_dev = math.sqrt(sum(downside_sq) / len(downside_sq))
        sortino_ratio = mean_pnl / downside_dev if downside_dev > 0 else 0.0
    else:
        sortino_ratio = mean_pnl if mean_pnl > 0 else 0.0

    # Calmar ratio = total PnL / max drawdown
    calmar_ratio = total_pnl / max_dd if max_dd > 0 else (total_pnl if total_pnl > 0 else 0.0)

    # Kelly criterion = W - (1-W)/R  where W=win_rate, R=avg_win/avg_loss
    if avg_loss > 0 and decisive > 0:
        payoff_ratio = avg_win / avg_loss
        kelly = win_rate - (1 - win_rate) / payoff_ratio if payoff_ratio > 0 else 0.0
    else:
        kelly = 0.0

    # Skewness (Fisher-Pearson, sample)
    if n > 2 and std_pnl > 0:
        m3 = sum((p - mean_pnl) ** 3 for p in pnls) / n
        skewness = m3 / (std_pnl ** 3) * (n * (n - 1)) ** 0.5 / (n - 2) if std_pnl > 0 else 0.0
    else:
        skewness = 0.0

    # Excess kurtosis (Fisher)
    if n > 3 and std_pnl > 0:
        m4 = sum((p - mean_pnl) ** 4 for p in pnls) / n
        raw_kurt = m4 / (std_pnl ** 4) if std_pnl > 0 else 0.0
        # Adjust for sample: ((n-1)/((n-2)(n-3))) * ((n+1)*raw_kurt - 3*(n-1)) + 3 - 3
        kurtosis = ((n - 1) / ((n - 2) * (n - 3))) * ((n + 1) * raw_kurt - 3 * (n - 1))
    else:
        kurtosis = 0.0

    # ── Streaks ──
    max_win_streak = 0
    max_loss_streak = 0
    win_streak = 0
    loss_streak = 0
    for t in sorted_trades:
        if t.pnl > 0:
            win_streak += 1
            loss_streak = 0
            max_win_streak = max(max_win_streak, win_streak)
        elif t.pnl < 0:
            loss_streak += 1
            win_streak = 0
            max_loss_streak = max(max_loss_streak, loss_streak)
        else:
            win_streak = 0
            loss_streak = 0

    # Current streak: positive for wins, negative for losses
    current_streak = 0
    if sorted_trades:
        last_dir = None
        for t in reversed(sorted_trades):
            if t.pnl > 0:
                if last_dir is None:
                    last_dir = "win"
                if last_dir == "win":
                    current_streak += 1
                else:
                    break
            elif t.pnl < 0:
                if last_dir is None:
                    last_dir = "loss"
                if last_dir == "loss":
                    current_streak -= 1
                else:
                    break
            else:
                break

    return {
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 4),
        "avg_rr": round(avg_rr, 2),
        "total_trades": n,
        "max_drawdown": round(max_dd, 2),
        "profit_factor": round(profit_factor, 2),
        "wins": len(wins),
        "losses": len(losses),
        "break_even": len(breakeven),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "best_trade": round(best_trade, 2),
        "worst_trade": round(worst_trade, 2),
        "avg_hold_time_hours": round(avg_hold, 2),
        # Institutional
        "expectancy": round(expectancy, 2),
        "sharpe_ratio": round(sharpe_ratio, 2),
        "sortino_ratio": round(sortino_ratio, 2),
        "calmar_ratio": round(calmar_ratio, 2),
        "avg_pnl_per_trade": round(avg_pnl_per_trade, 2),
        "kelly_criterion": round(kelly, 4),
        "skewness": round(skewness, 2),
        "kurtosis": round(kurtosis, 2),
        "exposure_time_pct": round(exposure_time_pct, 2),
        "current_streak": current_streak,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# /api/stats/brokers  — UNIFIED source of truth for PerformanceAnalyticsPanel
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/brokers")
def stats_by_broker(
    from_: date = Query(None, alias="from"),
    to: date = Query(None),
) -> Dict[str, Any]:
    """
    Get performance stats grouped by broker from live_trades (source of truth).
    Returns same shape as /api/positions/stats/brokers for frontend compatibility.
    """
    try:
        with get_db_session() as db:
            q = db.query(LiveTrade).filter(
                LiveTrade.status == "CLOSED",
                LiveTrade.pnl != None,
            )
            if from_:
                start_dt = _to_utc(datetime.combine(from_, datetime.min.time()))
                q = q.filter(LiveTrade.ts_close >= start_dt)
            if to:
                end_dt = _to_utc(datetime.combine(to, datetime.max.time()))
                q = q.filter(LiveTrade.ts_close <= end_dt)

            all_closed = q.all()

            bitget_trades = [t for t in all_closed if (t.broker or "kraken") == "bitget"]
            kraken_trades = [t for t in all_closed if (t.broker or "kraken") == "kraken"]

            result = {
                "bitget": _compute_metrics(bitget_trades),
                "kraken": _compute_metrics(kraken_trades),
                "combined": _compute_metrics(all_closed),
                "date_from": str(from_) if from_ else None,
                "date_to": str(to) if to else None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "live_trades",
            }

            for t in all_closed:
                db.expunge(t)

        logger.info(
            "[STATS] Brokers: combined=%d trades, pnl=%s",
            result["combined"]["total_trades"], result["combined"]["total_pnl"],
        )
        return result

    except Exception as e:
        logger.error(f"[STATS] Error in stats_by_broker: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
