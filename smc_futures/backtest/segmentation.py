"""
Multi-Dimensional Segmentation Analysis
════════════════════════════════════════
Chantier 9: Break performance across 10 dimensions, verdict per segment.

Dimensions:
  1. setup_type          — Strategy pattern name
  2. direction           — long / short
  3. volatility_regime   — low / normal / high / extreme
  4. exit_reason         — sl / tp / timeout / end_of_data
  5. session             — Hour-of-day bucket (Asia/Europe/US)
  6. day_of_week         — Mon-Sun
  7. holding_bucket      — short / medium / long / timeout
  8. volatility_bucket   — ATR quantile (low / med / high / extreme)
  9. rr_bucket           — Risk-reward achieved bucket
  10. mae_mfe_bucket     — MAE/MFE efficiency bucket

Per segment: trades, expectancy, PF, WR, avg_slippage_bps, total_pnl, verdict.
Verdict per segment: KEEP / REVIEW / KILL
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable, Tuple

from smc_futures.backtest.engine import RealisticTrade


# ───────────────────────────────────────────
#  Segment Stats
# ───────────────────────────────────────────

@dataclass
class SegmentStats:
    """Performance metrics for one segment slice."""
    dimension: str = ""
    segment_name: str = ""
    trades: int = 0

    # PnL
    total_pnl: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0

    # Costs
    avg_entry_slip_bps: float = 0.0
    avg_exit_slip_bps: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0

    # Risk
    max_loss: float = 0.0
    avg_mae: float = 0.0
    avg_mfe: float = 0.0
    edge_ratio: float = 0.0        # MFE / |MAE|

    # Averages
    avg_holding_bars: float = 0.0
    avg_rr: float = 0.0

    # Verdict
    verdict: str = "KILL"           # KEEP / REVIEW / KILL


@dataclass
class SegmentationResult:
    """Complete segmentation across all dimensions."""
    dimensions: Dict[str, List[SegmentStats]] = field(default_factory=dict)
    profitable_segments: int = 0
    total_segments: int = 0
    best_segment: Optional[SegmentStats] = None
    worst_segment: Optional[SegmentStats] = None
    report: str = ""


# ═══════════════════════════════════════════
#  SEGMENTATION ENGINE
# ═══════════════════════════════════════════

class SegmentationEngine:
    """Segment trades across 10 dimensions and verdict each slice."""

    # ── Holding bucket boundaries (in bars, assuming 5min) ──
    HOLD_SHORT_MAX = 12      # < 1h
    HOLD_MEDIUM_MAX = 72     # < 6h
    HOLD_LONG_MAX = 288      # < 24h

    # ── Session hours (UTC) ──
    SESSION_MAP = {
        "asia":   range(0, 8),    # 00-08 UTC
        "europe": range(8, 15),   # 08-15 UTC
        "us":     range(15, 22),  # 15-22 UTC
        "off":    range(22, 24),  # 22-24 UTC (+ 0 but handled in asia)
    }

    @classmethod
    def analyze(cls, trades: List[RealisticTrade]) -> SegmentationResult:
        """Run full 10-dimension segmentation."""
        result = SegmentationResult()
        if not trades:
            return result

        dimensions: Dict[str, Callable[[RealisticTrade], str]] = {
            "setup_type":        lambda t: t.setup_type or "unknown",
            "direction":         lambda t: t.direction,
            "volatility_regime": lambda t: t.volatility_regime or "unknown",
            "exit_reason":       lambda t: t.exit_reason or "unknown",
            "session":           cls._get_session,
            "day_of_week":       cls._get_day_of_week,
            "holding_bucket":    cls._get_holding_bucket,
            "volatility_bucket": lambda t: cls._get_atr_bucket(t, trades),
            "rr_bucket":         cls._get_rr_bucket,
            "mae_mfe_bucket":    cls._get_mae_mfe_bucket,
        }

        all_segments = []
        for dim_name, key_fn in dimensions.items():
            groups: Dict[str, List[RealisticTrade]] = {}
            for t in trades:
                key = key_fn(t)
                groups.setdefault(key, []).append(t)

            segments = []
            for seg_name, seg_trades in sorted(groups.items()):
                stats = cls._compute_stats(dim_name, seg_name, seg_trades)
                stats.verdict = cls._verdict(stats)
                segments.append(stats)

            result.dimensions[dim_name] = segments
            all_segments.extend(segments)

        # ── Summary ──
        result.total_segments = len(all_segments)
        result.profitable_segments = sum(
            1 for s in all_segments if s.verdict == "KEEP"
        )

        keep_segments = [s for s in all_segments if s.verdict == "KEEP"]
        kill_segments = [s for s in all_segments if s.verdict == "KILL"]

        if keep_segments:
            result.best_segment = max(keep_segments, key=lambda s: s.expectancy)
        if kill_segments:
            result.worst_segment = min(kill_segments, key=lambda s: s.total_pnl)

        result.report = cls._format_report(result)
        return result

    # ── Bucket Functions ──

    @staticmethod
    def _get_session(t: RealisticTrade) -> str:
        if t.entry_timestamp <= 0:
            return "unknown"
        dt = datetime.datetime.fromtimestamp(t.entry_timestamp / 1000, tz=datetime.timezone.utc)
        hour = dt.hour
        if hour < 8:
            return "asia"
        elif hour < 15:
            return "europe"
        elif hour < 22:
            return "us"
        else:
            return "off"

    @staticmethod
    def _get_day_of_week(t: RealisticTrade) -> str:
        if t.entry_timestamp <= 0:
            return "unknown"
        dt = datetime.datetime.fromtimestamp(t.entry_timestamp / 1000, tz=datetime.timezone.utc)
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.weekday()]

    @classmethod
    def _get_holding_bucket(cls, t: RealisticTrade) -> str:
        h = t.holding_bars
        if h <= cls.HOLD_SHORT_MAX:
            return "short (<1h)"
        elif h <= cls.HOLD_MEDIUM_MAX:
            return "medium (1-6h)"
        elif h <= cls.HOLD_LONG_MAX:
            return "long (6-24h)"
        else:
            return "timeout (>24h)"

    @staticmethod
    def _get_atr_bucket(t: RealisticTrade, all_trades: List[RealisticTrade]) -> str:
        if t.atr_at_entry == 0:
            return "unknown"
        atrs = sorted(tr.atr_at_entry for tr in all_trades if tr.atr_at_entry > 0)
        if not atrs:
            return "unknown"
        n = len(atrs)
        p25, p50, p75 = atrs[n // 4], atrs[n // 2], atrs[3 * n // 4]
        if t.atr_at_entry <= p25:
            return "Q1_low"
        elif t.atr_at_entry <= p50:
            return "Q2_med"
        elif t.atr_at_entry <= p75:
            return "Q3_high"
        else:
            return "Q4_extreme"

    @staticmethod
    def _get_rr_bucket(t: RealisticTrade) -> str:
        rr = t.rr_achieved
        if rr <= -2:
            return "rr<=-2"
        elif rr <= -1:
            return "-2<rr<=-1"
        elif rr <= 0:
            return "-1<rr<=0"
        elif rr <= 1:
            return "0<rr<=1"
        elif rr <= 2:
            return "1<rr<=2"
        else:
            return "rr>2"

    @staticmethod
    def _get_mae_mfe_bucket(t: RealisticTrade) -> str:
        if t.mfe == 0 and t.mae == 0:
            return "flat"
        mae_abs = abs(t.mae)
        if t.mfe > 0 and mae_abs > 0:
            ratio = t.mfe / mae_abs
            if ratio > 3:
                return "excellent (MFE/MAE>3)"
            elif ratio > 1.5:
                return "good (1.5-3)"
            elif ratio > 0.7:
                return "mediocre (0.7-1.5)"
            else:
                return "poor (<0.7)"
        elif t.mfe <= 0:
            return "never_green"
        else:
            return "no_adverse"

    # ── Stats Computation ──

    @staticmethod
    def _compute_stats(
        dim: str, name: str, trades: List[RealisticTrade],
    ) -> SegmentStats:
        s = SegmentStats(dimension=dim, segment_name=name, trades=len(trades))
        if not trades:
            return s

        pnls = [t.net_pnl for t in trades]
        s.total_pnl = sum(pnls)
        s.expectancy = s.total_pnl / len(trades)

        wins = sum(1 for p in pnls if p > 0)
        s.win_rate = wins / len(trades) * 100

        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        s.profit_factor = gross_win / gross_loss if gross_loss > 0 else (
            float("inf") if gross_win > 0 else 0
        )

        s.avg_entry_slip_bps = sum(t.entry_slippage_bps for t in trades) / len(trades)
        s.avg_exit_slip_bps = sum(t.exit_slippage_bps for t in trades) / len(trades)
        s.total_fees = sum(t.total_fees for t in trades)

        # Slippage cost estimate
        slip_cost = sum(abs(t.actual_entry - t.intended_entry) * t.qty for t in trades if t.intended_entry)
        s.total_slippage = slip_cost

        s.max_loss = min(pnls) if pnls else 0

        maes = [t.mae for t in trades if t.mae != 0]
        mfes = [t.mfe for t in trades if t.mfe > 0]
        s.avg_mae = sum(maes) / len(maes) if maes else 0
        s.avg_mfe = sum(mfes) / len(mfes) if mfes else 0
        s.edge_ratio = abs(s.avg_mfe / s.avg_mae) if s.avg_mae != 0 else 0

        s.avg_holding_bars = sum(t.holding_bars for t in trades) / len(trades)
        rrs = [t.rr_achieved for t in trades if t.rr_achieved != 0]
        s.avg_rr = sum(rrs) / len(rrs) if rrs else 0

        return s

    # ── Verdict Logic ──

    @staticmethod
    def _verdict(s: SegmentStats) -> str:
        """
        KEEP:   expectancy > 0, PF > 1.1, WR in [25%, 80%], >= 5 trades
        REVIEW: expectancy > 0 but some warning, or too few trades
        KILL:   everything else
        """
        if s.trades < 3:
            return "REVIEW"  # Too few to judge

        if s.expectancy > 0 and s.profit_factor > 1.1 and 25 <= s.win_rate <= 80:
            if s.trades >= 10:
                return "KEEP"
            else:
                return "REVIEW"  # Positive but low sample

        if s.expectancy > 0 and s.profit_factor > 1.0:
            return "REVIEW"  # Marginal

        return "KILL"

    # ── Report Formatting ──

    @classmethod
    def _format_report(cls, result: SegmentationResult) -> str:
        lines = []
        sep = "─" * 72

        lines.append(sep)
        lines.append("  SEGMENTATION ANALYSIS — 10 DIMENSIONS")
        lines.append(sep)
        lines.append(
            f"  Total segments: {result.total_segments}, "
            f"Profitable (KEEP): {result.profitable_segments}, "
            f"Survival rate: {result.profitable_segments / result.total_segments * 100:.1f}%"
            if result.total_segments else "  No segments"
        )
        lines.append("")

        for dim_name, segments in result.dimensions.items():
            lines.append(f"  ┌─ {dim_name.upper()} ─────────────────────────────────────")
            lines.append(
                f"  │ {'Segment':<22} {'#':>4} {'PnL':>10} {'Exp':>8} "
                f"{'PF':>6} {'WR':>6} {'Verdict':>8}"
            )
            lines.append(
                f"  │ {'─' * 22} {'─' * 4} {'─' * 10} {'─' * 8} "
                f"{'─' * 6} {'─' * 6} {'─' * 8}"
            )
            for s in segments:
                pf_str = f"{s.profit_factor:.2f}" if s.profit_factor < 100 else "∞"
                verdict_mark = {"KEEP": "✓", "REVIEW": "?", "KILL": "✗"}[s.verdict]
                lines.append(
                    f"  │ {s.segment_name:<22} {s.trades:>4} {s.total_pnl:>+10.2f} "
                    f"{s.expectancy:>+8.2f} {pf_str:>6} {s.win_rate:>5.1f}% "
                    f"{verdict_mark} {s.verdict}"
                )
            lines.append(f"  └{'─' * 71}")
            lines.append("")

        if result.best_segment:
            b = result.best_segment
            lines.append(
                f"  BEST : [{b.dimension}] {b.segment_name} — "
                f"exp=${b.expectancy:+.2f}, PF={b.profit_factor:.2f}, {b.trades}t"
            )
        if result.worst_segment:
            w = result.worst_segment
            lines.append(
                f"  WORST: [{w.dimension}] {w.segment_name} — "
                f"PnL=${w.total_pnl:+.2f}, {w.trades}t"
            )
        lines.append("")

        return "\n".join(lines)
