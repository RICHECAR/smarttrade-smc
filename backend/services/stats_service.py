"""
Stats Service - Analytics and statistics for trade performance

Provides:
- Summary statistics (PnL, win rate, avg RR, max DD, profit factor)
- RR time series for charting
- Heatmap data (trades by day/hour)

Author: SMC Trading Bot Framework
"""

import logging
from datetime import date
from collections import defaultdict
from typing import List, Dict, Any

from backend.services.live_trades_service import LiveTradesService
from backend.models.trade import TradeSummary, RRPoint, HeatCell, TradeOut
from backend.models.live_trade import LiveTrade

logger = logging.getLogger(__name__)


class StatsService:
    """
    Service class for trade statistics and analytics.
    All methods are static for easy use.
    """
    
    @staticmethod
    def summary(from_date: date, to_date: date) -> TradeSummary:
        """
        Calculate summary statistics for trades in date range.
        
        Args:
            from_date: Start date (inclusive)
            to_date: End date (inclusive)
        
        Returns:
            TradeSummary with PnL, win rate, avg RR, max DD, profit factor
        """
        trades = LiveTradesService.get_trades_by_date_range(
            from_date=from_date,
            to_date=to_date,
            status="CLOSED"
        )
        
        if not trades:
            return TradeSummary(
                pnl=0.0,
                win_rate=0.0,
                avg_rr=0.0,
                trades=0,
                max_dd=0.0,
                profit_factor=0.0,
                wins=0,
                losses=0,
            )
        
        # Calculate metrics
        # Calculate metrics (exclude UNKNOWN PnL)
        known_trades = [t for t in trades if t.pnl is not None]
        total_pnl = sum(t.pnl for t in known_trades)
        
        # Win/loss classification
        wins = [t for t in known_trades if t.result == "win" or (t.result is None and t.pnl > 0)]
        losses = [t for t in known_trades if t.result == "loss" or (t.result is None and t.pnl < 0)]
        breakevens = [t for t in known_trades if t.result == "be" or (t.result is None and t.pnl == 0)]
        
        # Win rate (excluding breakevens from calculation)
        decisive_trades = len(wins) + len(losses)
        win_rate = len(wins) / decisive_trades if decisive_trades > 0 else 0.0
        
        # Average RR
        avg_rr = sum(t.rr for t in known_trades) / len(known_trades) if known_trades else 0.0
        
        # Profit factor = gross profit / gross loss
        gross_profit = sum(t.pnl for t in wins if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in losses if t.pnl < 0))
        
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        else:
            profit_factor = gross_profit if gross_profit > 0 else 0.0
        
        # Max drawdown (simple cumulative PnL based)
        sorted_trades = sorted(known_trades, key=lambda x: x.ts_open)
        cum_pnl = 0.0
        peak = 0.0
        max_dd = 0.0
        
        for t in sorted_trades:
            cum_pnl += t.pnl
            peak = max(peak, cum_pnl)
            dd = peak - cum_pnl
            max_dd = max(max_dd, dd)
        
        logger.debug(
            f"[STATS] Summary: PnL={total_pnl:.2f}, WinRate={win_rate:.2%}, "
            f"AvgRR={avg_rr:.2f}, MaxDD={max_dd:.2f}, PF={profit_factor:.2f}"
        )
        
        return TradeSummary(
            pnl=round(total_pnl, 2),
            win_rate=round(win_rate, 4),
            avg_rr=round(avg_rr, 2),
            trades=len(trades),
            max_dd=round(max_dd, 2),
            profit_factor=round(profit_factor, 2),
            wins=len(wins),
            losses=len(losses),
        )
    
    @staticmethod
    def rr_series(from_date: date, to_date: date) -> List[RRPoint]:
        """
        Get RR values over time for charting.
        
        Args:
            from_date: Start date
            to_date: End date
        
        Returns:
            List of RRPoint with ts and rr values
        """
        trades = LiveTradesService.get_trades_by_date_range(
            from_date=from_date,
            to_date=to_date,
            status="CLOSED"
        )
        known_trades = [t for t in trades if t.pnl is not None]
        sorted_trades = sorted(known_trades, key=lambda x: x.ts_open)
        
        return [
            RRPoint(ts=t.ts_open.isoformat(), rr=round(t.rr, 2))
            for t in sorted_trades
        ]
    
    @staticmethod
    def pnl_series(from_date: date, to_date: date) -> List[Dict[str, Any]]:
        """
        Get cumulative PnL over time for equity curve charting.
        
        Returns list of {ts, pnl, cum_pnl} points.
        """
        trades = LiveTradesService.get_trades_by_date_range(
            from_date=from_date,
            to_date=to_date,
            status="CLOSED"
        )
        known_trades = [t for t in trades if t.pnl is not None]
        sorted_trades = sorted(known_trades, key=lambda x: x.ts_open)
        
        cum_pnl = 0.0
        series = []
        
        for t in sorted_trades:
            cum_pnl += t.pnl
            series.append({
                "ts": t.ts_open.isoformat(),
                "pnl": round(t.pnl, 2),
                "cum_pnl": round(cum_pnl, 2),
            })
        
        return series
    
    @staticmethod
    def heatmap(from_date: date, to_date: date) -> List[HeatCell]:
        """
        Generate heatmap data by day of week and hour.
        
        Useful for identifying best trading times.
        
        Args:
            from_date: Start date
            to_date: End date
        
        Returns:
            List of HeatCell with dow, hour, trades, pnl, win_rate
        """
        trades = LiveTradesService.get_trades_by_date_range(
            from_date=from_date,
            to_date=to_date,
            status="CLOSED"
        )
        known_trades = [t for t in trades if t.pnl is not None]
        
        # Grid: [dow][hour] -> {trades, pnl, wins}
        grid: Dict[tuple, Dict[str, Any]] = defaultdict(
            lambda: {"trades": 0, "pnl": 0.0, "wins": 0}
        )
        
        for t in known_trades:
            dt = t.ts_open
            dow = int(dt.strftime("%w"))  # 0=Sun, 6=Sat
            hour = dt.hour
            key = (dow, hour)
            
            grid[key]["trades"] += 1
            grid[key]["pnl"] += float(t.pnl)
            
            if t.pnl > 0 or t.result == "win":
                grid[key]["wins"] += 1
        
        # Convert to HeatCell list
        cells = []
        for (dow, hour), data in grid.items():
            win_rate = data["wins"] / data["trades"] if data["trades"] > 0 else 0.0
            cells.append(HeatCell(
                dow=dow,
                hour=hour,
                trades=data["trades"],
                pnl=round(data["pnl"], 2),
                win_rate=round(win_rate, 4),
            ))
        
        logger.debug(f"[STATS] Heatmap: {len(cells)} cells with data")
        return cells
    
    @staticmethod
    def session_breakdown(from_date: date, to_date: date) -> Dict[str, TradeSummary]:
        """
        Get summary statistics broken down by trading session.
        
        Returns:
            Dict with session name -> TradeSummary
        """
        trades = LiveTradesService.get_trades_by_date_range(
            from_date=from_date,
            to_date=to_date,
            status="CLOSED"
        )
        known_trades = [t for t in trades if t.pnl is not None]
        
        # Group by session
        by_session: Dict[str, List[LiveTrade]] = defaultdict(list)
        for t in known_trades:
            session = t.session or "Unknown"
            by_session[session].append(t)
        
        results = {}
        for session, session_trades in by_session.items():
            # Calculate summary for this session
            total_pnl = sum(t.pnl for t in session_trades)
            wins = len([t for t in session_trades if t.pnl > 0])
            losses = len([t for t in session_trades if t.pnl < 0])
            
            win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.0
            avg_rr = sum(t.rr for t in session_trades) / len(session_trades) if session_trades else 0.0
            
            results[session] = TradeSummary(
                pnl=round(total_pnl, 2),
                win_rate=round(win_rate, 4),
                avg_rr=round(avg_rr, 2),
                trades=len(session_trades),
                max_dd=0.0,  # Simplified
                profit_factor=0.0,  # Simplified
                wins=wins,
                losses=losses,
            )
        
        return results
    
    @staticmethod
    def preset_breakdown(from_date: date, to_date: date) -> Dict[str, TradeSummary]:
        """
        Get summary statistics broken down by preset.
        
        Returns:
            Dict with preset name -> TradeSummary
        """
        trades = LiveTradesService.get_trades_by_date_range(
            from_date=from_date,
            to_date=to_date,
            status="CLOSED"
        )
        known_trades = [t for t in trades if t.pnl is not None]
        
        # Group by preset
        by_preset: Dict[str, List[LiveTrade]] = defaultdict(list)
        for t in known_trades:
            preset = t.preset or "Default"
            by_preset[preset].append(t)
        
        results = {}
        for preset, preset_trades in by_preset.items():
            total_pnl = sum(t.pnl for t in preset_trades)
            wins = len([t for t in preset_trades if t.pnl > 0])
            losses = len([t for t in preset_trades if t.pnl < 0])
            
            win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.0
            avg_rr = sum(t.rr for t in preset_trades) / len(preset_trades) if preset_trades else 0.0
            
            results[preset] = TradeSummary(
                pnl=round(total_pnl, 2),
                win_rate=round(win_rate, 4),
                avg_rr=round(avg_rr, 2),
                trades=len(preset_trades),
                max_dd=0.0,
                profit_factor=0.0,
                wins=wins,
                losses=losses,
            )
        
        return results
