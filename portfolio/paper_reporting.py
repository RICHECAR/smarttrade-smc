"""
Paper Reporting — Monthly + cumulative observability.
════════════════════════════════════════════════════
Produces:
  - JSON / CSV exports
  - Human-readable text report
  - Current state snapshot
"""

from __future__ import annotations

import json
import csv
import io
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from portfolio.paper_state import PaperPortfolioState
from portfolio.paper_journal import PaperJournal, JournalEntry


def _rolling_sharpe(values: pd.Series, window: int = 126, rf_annual: float = 0.04) -> pd.Series:
    rets = values.pct_change().dropna()
    daily_rf = (1 + rf_annual) ** (1 / 252) - 1
    excess = rets - daily_rf
    rolling_mean = excess.rolling(window).mean()
    rolling_std = excess.rolling(window).std()
    return (rolling_mean / rolling_std.replace(0, np.nan)) * np.sqrt(252)


def _max_drawdown_current(values: pd.Series) -> float:
    peak = values.expanding().max()
    dd = (values - peak) / peak
    return float(dd.iloc[-1]) if len(dd) > 0 else 0.0


def _max_drawdown(values: pd.Series) -> float:
    peak = values.expanding().max()
    dd = (values - peak) / peak
    return float(dd.min()) if len(dd) > 0 else 0.0


def _cagr(values: pd.Series) -> float:
    if len(values) < 2:
        return 0.0
    total_ret = values.iloc[-1] / values.iloc[0]
    n_years = (values.index[-1] - values.index[0]).days / 365.25
    if n_years <= 0:
        return 0.0
    return total_ret ** (1 / n_years) - 1


def _sharpe(values: pd.Series, rf_annual: float = 0.04) -> float:
    rets = values.pct_change().dropna()
    if len(rets) < 10:
        return 0.0
    daily_rf = (1 + rf_annual) ** (1 / 252) - 1
    excess = rets - daily_rf
    std = excess.std()
    if std == 0:
        return 0.0
    return float((excess.mean() / std) * np.sqrt(252))


def _contribution_by_asset(journal_entries: List[JournalEntry]) -> Dict[str, float]:
    """Average target weight per ticker across all non-blocked rebalances."""
    counts: Dict[str, List[float]] = {}
    for e in journal_entries:
        if e.blocked:
            continue
        for t, w in e.target_weights.items():
            counts.setdefault(t, []).append(w)
    return {t: round(np.mean(ws), 4) for t, ws in sorted(counts.items(), key=lambda x: -np.mean(x[1]))}


def generate_paper_report(
    state: PaperPortfolioState,
    journal: PaperJournal,
    block_counts: Dict[str, int] = None,
) -> str:
    """Generate comprehensive text report."""
    entries = journal.read_all()
    valuations = state.valuations

    lines = [
        "",
        "╔══════════════════════════════════════════════════════════════╗",
        "║   PAPER TRADING REPORT — PORTFOLIO ALLOCATION ENGINE       ║",
        "╠══════════════════════════════════════════════════════════════╣",
    ]

    # ── 1. Current portfolio ──────────────────────────────────────
    lines.append("║                                                              ║")
    lines.append("║  1. CURRENT PORTFOLIO                                        ║")
    lines.append(f"║     Total Value:    ${state.total_value:>12,.2f}                      ║")
    lines.append(f"║     Cash:           ${state.cash:>12,.2f}                      ║")
    lines.append(f"║     Invested:       ${state.invested_value:>12,.2f}                      ║")
    lines.append(f"║     Inception:      {state.inception_date:<20}                  ║")
    lines.append(f"║     Rebalances:     {state.rebal_count:<20}                  ║")

    # ── 2. Current weights ────────────────────────────────────────
    lines.append("║                                                              ║")
    lines.append("║  2. CURRENT WEIGHTS                                          ║")
    weights = state.weights()
    for t in sorted(weights, key=lambda x: -weights[x]):
        lines.append(f"║     {t:<8}  {weights[t]:>7.1%}  ${state.positions[t].market_value:>10,.0f}       ║")
    cash_w = state.cash / state.total_value if state.total_value > 0 else 0
    lines.append(f"║     {'CASH':<8}  {cash_w:>7.1%}  ${state.cash:>10,.0f}       ║")

    # ── 3. Performance since inception ────────────────────────────
    lines.append("║                                                              ║")
    lines.append("║  3. PERFORMANCE SINCE INCEPTION                              ║")

    if len(valuations) > 1:
        val_series = pd.Series(
            [v.total_value for v in valuations],
            index=pd.to_datetime([v.date for v in valuations]),
        )
        val_series = val_series[~val_series.index.duplicated(keep='last')]

        bench_series = pd.Series(
            [v.benchmark_value for v in valuations],
            index=pd.to_datetime([v.date for v in valuations]),
        )
        bench_series = bench_series[~bench_series.index.duplicated(keep='last')]
        if bench_series.iloc[0] > 0:
            bench_norm = bench_series / bench_series.iloc[0] * state.initial_cash
        else:
            bench_norm = bench_series

        total_ret = (state.total_value / state.initial_cash - 1)
        cagr = _cagr(val_series)
        sharpe = _sharpe(val_series)
        mdd = _max_drawdown(val_series)
        cur_dd = _max_drawdown_current(val_series)

        bench_ret = (bench_norm.iloc[-1] / bench_norm.iloc[0] - 1) if len(bench_norm) > 1 and bench_norm.iloc[0] > 0 else 0

        lines.append(f"║     Total Return:   {total_ret:>+8.2%}                               ║")
        lines.append(f"║     CAGR:           {cagr:>+8.2%}                               ║")
        lines.append(f"║     Sharpe:         {sharpe:>8.2f}                               ║")
        lines.append(f"║     Max Drawdown:   {mdd:>8.2%}                               ║")
        lines.append(f"║     Current DD:     {cur_dd:>8.2%}                               ║")
        lines.append(f"║     Bench Return:   {bench_ret:>+8.2%} (SPY)                        ║")

        # Rolling Sharpe 6M
        if len(val_series) >= 126:
            rs = _rolling_sharpe(val_series, 126)
            latest_rs = rs.dropna().iloc[-1] if len(rs.dropna()) > 0 else 0
            lines.append(f"║     Rolling Sharpe 6M: {latest_rs:>5.2f}                           ║")
    else:
        lines.append("║     (insufficient data for performance metrics)              ║")

    # ── 4. Turnover summary ───────────────────────────────────────
    lines.append("║                                                              ║")
    lines.append("║  4. TURNOVER SUMMARY                                         ║")
    non_blocked = [e for e in entries if not e.blocked]
    if non_blocked:
        turnovers = [e.turnover for e in non_blocked]
        costs = [e.cost_simulated for e in non_blocked]
        lines.append(f"║     Avg Turnover/Rebal: {np.mean(turnovers):>6.1%}                       ║")
        lines.append(f"║     Max Turnover:       {np.max(turnovers):>6.1%}                       ║")
        lines.append(f"║     Total Cost:         ${sum(costs):>8,.2f}                     ║")
        lines.append(f"║     Avg Cost/Rebal:     ${np.mean(costs):>8,.2f}                     ║")

    # ── 5. Contribution by asset ──────────────────────────────────
    lines.append("║                                                              ║")
    lines.append("║  5. AVG WEIGHT BY ASSET                                      ║")
    contribs = _contribution_by_asset(entries)
    for t, w in list(contribs.items())[:10]:
        lines.append(f"║     {t:<8}  avg {w:>6.1%}                                   ║")

    # ── 6. Safeguard blocks ───────────────────────────────────────
    lines.append("║                                                              ║")
    lines.append("║  6. SAFEGUARD / CONSTRAINT BLOCKS                            ║")
    blocked_entries = [e for e in entries if e.blocked]
    lines.append(f"║     Total blocks:     {len(blocked_entries)}                                    ║")
    if block_counts:
        for code, cnt in sorted(block_counts.items(), key=lambda x: -x[1]):
            lines.append(f"║     {code:<30} {cnt:>4}x              ║")

    lines.append("║                                                              ║")
    lines.append("╚══════════════════════════════════════════════════════════════╝")
    return "\n".join(lines)


def export_valuations_csv(state: PaperPortfolioState, path: Path) -> None:
    """Export daily valuations to CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "total_value", "cash", "invested", "benchmark_value"])
        writer.writeheader()
        for v in state.valuations:
            writer.writerow(asdict(v))


def export_journal_csv(journal: PaperJournal, path: Path) -> None:
    """Export journal to CSV (flat summary)."""
    entries = journal.read_all()
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["rebal_date", "blocked", "turnover", "cost_simulated",
              "portfolio_value_after", "benchmark_value", "safeguard_status",
              "constraint_status", "universe_valid_count"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for e in entries:
            row = {k: getattr(e, k, "") for k in fields}
            writer.writerow(row)


def export_state_json(state: PaperPortfolioState, path: Path) -> None:
    """Export current state snapshot as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "total_value": round(state.total_value, 2),
        "cash": round(state.cash, 2),
        "invested": round(state.invested_value, 2),
        "inception_date": state.inception_date,
        "rebal_count": state.rebal_count,
        "weights": {t: round(w, 4) for t, w in state.weights().items()},
        "positions": {t: p.to_dict() for t, p in state.positions.items()},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
