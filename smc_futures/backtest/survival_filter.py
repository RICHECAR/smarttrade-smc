"""
Survival Filtering — Simulate Filter Impact on Edge
════════════════════════════════════════════════════
Chantier 9: Test what happens if we filter out toxic trades.

Filters tested:
  1. Remove extreme volatility trades
  2. Remove specific exit reasons (timeouts)
  3. Remove low confidence signals
  4. Remove short holding periods (churning)
  5. Remove specific directions (long-only / short-only)
  6. Remove worst MAE/MFE ratio trades
  7. Remove specific volatility regimes
  8. Remove specific sessions (off-hours)
  9. Remove specific RR ranges (negative RR)
  10. Combined: stack best individual filters

Each filter: before/after trades, expectancy, PF, DD estimate, verdict.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable, Tuple

from smc_futures.backtest.engine import RealisticTrade


# ───────────────────────────────────────────
#  Filter Result
# ───────────────────────────────────────────

@dataclass
class FilterResult:
    """Impact of one filter on strategy performance."""
    filter_name: str = ""
    filter_desc: str = ""

    # Before
    trades_before: int = 0
    pnl_before: float = 0.0
    expectancy_before: float = 0.0
    pf_before: float = 0.0
    wr_before: float = 0.0

    # After
    trades_after: int = 0
    pnl_after: float = 0.0
    expectancy_after: float = 0.0
    pf_after: float = 0.0
    wr_after: float = 0.0

    # Delta
    trades_removed: int = 0
    pnl_delta: float = 0.0
    expectancy_delta: float = 0.0
    pf_delta: float = 0.0

    # Verdict
    verdict: str = ""               # "IMPROVES" / "DEGRADES" / "NEUTRAL"
    improvement_pct: float = 0.0    # Expectancy improvement %

    # Surviving trade indices (for stacking)
    surviving_indices: List[int] = field(default_factory=list)


@dataclass
class SurvivalResult:
    """Complete survival filtering analysis."""
    filters: List[FilterResult] = field(default_factory=list)
    best_single: Optional[FilterResult] = None
    combined: Optional[FilterResult] = None
    total_survivors: int = 0          # Filters that improve expectancy
    report: str = ""


# ═══════════════════════════════════════════
#  SURVIVAL FILTER ENGINE
# ═══════════════════════════════════════════

class SurvivalFilterEngine:
    """Test 10+ filters and their impact on strategy edge."""

    @classmethod
    def analyze(cls, trades: List[RealisticTrade]) -> SurvivalResult:
        """Run all survival filters."""
        result = SurvivalResult()
        if not trades:
            return result

        # Define all filters
        filters: List[Tuple[str, str, Callable[[RealisticTrade], bool]]] = [
            (
                "remove_extreme_vol",
                "Remove trades in extreme volatility regime",
                lambda t: t.volatility_regime != "extreme",
            ),
            (
                "remove_high_vol",
                "Remove trades in high + extreme volatility",
                lambda t: t.volatility_regime not in ("high", "extreme"),
            ),
            (
                "remove_timeouts",
                "Remove timeout/end_of_data exits",
                lambda t: t.exit_reason in ("sl", "tp"),
            ),
            (
                "remove_low_confidence",
                "Remove signals with confidence < 0.5",
                lambda t: t.confidence >= 0.5,
            ),
            (
                "long_only",
                "Keep only long trades",
                lambda t: t.direction == "long",
            ),
            (
                "short_only",
                "Keep only short trades",
                lambda t: t.direction == "short",
            ),
            (
                "remove_short_hold",
                "Remove trades held < 6 bars (30min)",
                lambda t: t.holding_bars >= 6,
            ),
            (
                "remove_poor_mae_mfe",
                "Remove trades with MFE/MAE ratio < 0.5",
                lambda t: (
                    t.mfe / abs(t.mae) >= 0.5 if t.mae != 0 and t.mfe > 0
                    else t.mfe > 0  # Keep if never went adverse
                ),
            ),
            (
                "remove_negative_rr",
                "Remove trades with RR achieved < -1",
                lambda t: t.rr_achieved > -1,
            ),
            (
                "remove_off_hours",
                "Remove off-hours trades (22-08 UTC)",
                lambda t: cls._is_active_hours(t),
            ),
        ]

        improving_filters = []
        for name, desc, predicate in filters:
            fr = cls._test_filter(trades, name, desc, predicate)
            result.filters.append(fr)
            if fr.verdict == "IMPROVES":
                improving_filters.append(fr)

        result.total_survivors = len(improving_filters)

        # Best single filter
        if improving_filters:
            result.best_single = max(improving_filters, key=lambda f: f.expectancy_after)

        # Combined: stack all improving filters
        if len(improving_filters) >= 2:
            result.combined = cls._combine_filters(trades, improving_filters)

        result.report = cls._format_report(result)
        return result

    @staticmethod
    def _is_active_hours(t: RealisticTrade) -> bool:
        """Keep trades during active hours (08-22 UTC)."""
        if t.entry_timestamp <= 0:
            return True
        import datetime
        dt = datetime.datetime.fromtimestamp(t.entry_timestamp / 1000, tz=datetime.timezone.utc)
        return 8 <= dt.hour < 22

    @classmethod
    def _test_filter(
        cls,
        trades: List[RealisticTrade],
        name: str,
        desc: str,
        predicate: Callable[[RealisticTrade], bool],
    ) -> FilterResult:
        """Test one filter's impact."""
        fr = FilterResult(filter_name=name, filter_desc=desc)

        # Before
        fr.trades_before = len(trades)
        fr.pnl_before, fr.expectancy_before, fr.pf_before, fr.wr_before = cls._quick_stats(trades)

        # Apply filter
        surviving = [(i, t) for i, t in enumerate(trades) if predicate(t)]
        filtered = [t for _, t in surviving]
        fr.surviving_indices = [i for i, _ in surviving]

        # After
        fr.trades_after = len(filtered)
        fr.trades_removed = fr.trades_before - fr.trades_after

        if filtered:
            fr.pnl_after, fr.expectancy_after, fr.pf_after, fr.wr_after = cls._quick_stats(filtered)
        else:
            fr.pnl_after = 0
            fr.expectancy_after = 0
            fr.pf_after = 0
            fr.wr_after = 0

        # Deltas
        fr.pnl_delta = fr.pnl_after - fr.pnl_before
        fr.expectancy_delta = fr.expectancy_after - fr.expectancy_before
        fr.pf_delta = fr.pf_after - fr.pf_before

        # Verdict
        if fr.trades_after < 5:
            fr.verdict = "NEUTRAL"  # Too few trades to judge
        elif fr.expectancy_after > fr.expectancy_before and fr.expectancy_after > 0:
            fr.verdict = "IMPROVES"
        elif fr.expectancy_after > fr.expectancy_before:
            fr.verdict = "IMPROVES"  # Less negative is still improvement
        elif abs(fr.expectancy_delta) < 0.01:
            fr.verdict = "NEUTRAL"
        else:
            fr.verdict = "DEGRADES"

        if fr.expectancy_before != 0:
            fr.improvement_pct = fr.expectancy_delta / abs(fr.expectancy_before) * 100

        return fr

    @classmethod
    def _combine_filters(
        cls,
        trades: List[RealisticTrade],
        improving: List[FilterResult],
    ) -> FilterResult:
        """Stack all improving filters together."""
        # Intersection of surviving indices
        if not improving:
            return FilterResult(filter_name="combined", filter_desc="No filters to combine")

        surviving_set = set(range(len(trades)))
        for f in improving:
            surviving_set &= set(f.surviving_indices)

        combined_trades = [trades[i] for i in sorted(surviving_set)]

        fr = FilterResult(
            filter_name="combined_best",
            filter_desc=f"Stack {len(improving)} best filters: " +
                        ", ".join(f.filter_name for f in improving),
        )

        fr.trades_before = len(trades)
        fr.pnl_before, fr.expectancy_before, fr.pf_before, fr.wr_before = cls._quick_stats(trades)

        fr.trades_after = len(combined_trades)
        fr.trades_removed = fr.trades_before - fr.trades_after
        fr.surviving_indices = sorted(surviving_set)

        if combined_trades:
            fr.pnl_after, fr.expectancy_after, fr.pf_after, fr.wr_after = cls._quick_stats(combined_trades)
        else:
            fr.pnl_after = 0
            fr.expectancy_after = 0
            fr.pf_after = 0
            fr.wr_after = 0

        fr.pnl_delta = fr.pnl_after - fr.pnl_before
        fr.expectancy_delta = fr.expectancy_after - fr.expectancy_before
        fr.pf_delta = fr.pf_after - fr.pf_before

        if fr.trades_after >= 5 and fr.expectancy_after > fr.expectancy_before:
            fr.verdict = "IMPROVES"
        elif fr.trades_after < 5:
            fr.verdict = "NEUTRAL"
        else:
            fr.verdict = "DEGRADES"

        if fr.expectancy_before != 0:
            fr.improvement_pct = fr.expectancy_delta / abs(fr.expectancy_before) * 100

        return fr

    @staticmethod
    def _quick_stats(
        trades: List[RealisticTrade],
    ) -> Tuple[float, float, float, float]:
        """Quick PnL, expectancy, PF, WR."""
        if not trades:
            return 0, 0, 0, 0
        pnls = [t.net_pnl for t in trades]
        total = sum(pnls)
        exp = total / len(trades)
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / len(trades) * 100

        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else (
            float("inf") if gross_win > 0 else 0
        )
        return total, exp, pf, wr

    # ── Report ──

    @classmethod
    def _format_report(cls, result: SurvivalResult) -> str:
        lines = []
        sep = "─" * 72

        lines.append(sep)
        lines.append("  SURVIVAL FILTERING — CAN FILTERS SAVE THE EDGE?")
        lines.append(sep)
        lines.append(
            f"  Filters tested: {len(result.filters)}, "
            f"Improving: {result.total_survivors}"
        )
        lines.append("")

        lines.append(
            f"  {'Filter':<25} {'Before':>7} {'After':>7} {'Exp Δ':>8} "
            f"{'PF Δ':>7} {'Verdict':>10}"
        )
        lines.append(
            f"  {'─' * 25} {'─' * 7} {'─' * 7} {'─' * 8} "
            f"{'─' * 7} {'─' * 10}"
        )
        for f in result.filters:
            v_mark = {"IMPROVES": "↑", "DEGRADES": "↓", "NEUTRAL": "─"}[f.verdict]
            lines.append(
                f"  {f.filter_name:<25} {f.trades_before:>4}→{f.trades_after:<3}"
                f" {f.expectancy_after - f.expectancy_before:>+7.2f} "
                f"→${f.expectancy_after:>+7.2f} {f.pf_delta:>+6.2f} "
                f"{v_mark} {f.verdict}"
            )
        lines.append("")

        if result.best_single:
            b = result.best_single
            lines.append(
                f"  BEST SINGLE FILTER: {b.filter_name} — "
                f"exp=${b.expectancy_after:+.2f}, PF={b.pf_after:.2f}, "
                f"removed {b.trades_removed} trades"
            )

        if result.combined:
            c = result.combined
            lines.append(
                f"  COMBINED FILTERS  : exp=${c.expectancy_after:+.2f}, "
                f"PF={c.pf_after:.2f}, {c.trades_after} trades surviving"
            )
            profitability = "PROFITABLE" if c.expectancy_after > 0 else "STILL UNPROFITABLE"
            lines.append(f"  Combined result   : {profitability}")

        if not result.best_single:
            lines.append("  NO FILTER IMPROVES EXPECTANCY — edge is fundamentally dead")

        lines.append("")
        return "\n".join(lines)
