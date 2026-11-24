"""
Paper Replay — Sequential historical paper trading validation.
══════════════════════════════════════════════════════════════
Runs the paper engine month-by-month over recent history.
No lookahead. State carried forward. Full journal + reporting.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from portfolio.universe import DEFAULT_UNIVERSE
from portfolio.data_loader import fetch_prices
from portfolio.paper_state import PaperPortfolioState
from portfolio.paper_journal import PaperJournal
from portfolio.paper_engine import PaperEngine
from portfolio.paper_constraints import PaperConstraints
from portfolio.paper_broker import PaperBroker
from portfolio.paper_reporting import (
    generate_paper_report,
    export_valuations_csv,
    export_journal_csv,
    export_state_json,
)


OUTPUT_DIR = Path(__file__).parent / "data" / "paper_replay"


def main():
    print("=" * 60)
    print("  PAPER REPLAY — SEQUENTIAL HISTORICAL VALIDATION")
    print("=" * 60)
    print()

    # ── 1. Configuration ──────────────────────────────────────
    initial_cash = 100_000.0
    replay_years = 3      # last 3 years
    benchmark = "SPY"
    warmup = 252           # 1-year warmup for features

    constraints = PaperConstraints(
        rebal_freq="M",
        strategy="score_top5_monthly",
        max_vol_weight=0.15,
        max_turnover_per_rebal=0.50,
        min_universe_tickers=10,
        max_data_age_days=5,
        top_n=5,
        max_single_weight=0.30,
        cost_bps=5.0,
    )

    # ── 2. Download data ──────────────────────────────────────
    print(f"Universe: {len(DEFAULT_UNIVERSE)} tickers")
    print(f"Period: 5y (warmup={warmup}d, replay≈{replay_years}y)")
    print()

    t0 = time.time()
    prices = fetch_prices(DEFAULT_UNIVERSE, period="5y", cache=True)
    print(f"Data: {prices.shape[0]} days × {prices.shape[1]} tickers ({time.time()-t0:.1f}s)")
    print(f"Range: {prices.index[0].date()} → {prices.index[-1].date()}")
    print()

    # Determine replay window
    total_days = len(prices)
    replay_start_idx = max(warmup, total_days - replay_years * 252)
    replay_start = prices.index[replay_start_idx]
    replay_end = prices.index[-1]
    print(f"Replay: {replay_start.date()} → {replay_end.date()}")
    print()

    # ── 3. Initialize paper state ─────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    state_path = OUTPUT_DIR / "paper_state.json"
    journal_path = OUTPUT_DIR / "paper_journal.jsonl"

    state = PaperPortfolioState(initial_cash=initial_cash, state_path=state_path)
    journal = PaperJournal(path=journal_path)
    journal.clear()  # fresh replay
    broker = PaperBroker(cost_bps=constraints.cost_bps)

    engine = PaperEngine(
        state=state,
        journal=journal,
        constraints=constraints,
        broker=broker,
    )

    # ── 4. Replay month by month ──────────────────────────────
    print("Replaying month by month...")
    t0 = time.time()

    results = engine.replay(
        prices=prices,
        start_date=replay_start.strftime("%Y-%m-%d"),
        end_date=replay_end.strftime("%Y-%m-%d"),
        benchmark_ticker=benchmark,
        warmup=warmup,
    )

    elapsed = time.time() - t0
    print(f"Replay complete: {len(results)} months ({elapsed:.1f}s)")
    print()

    # ── 5. Summary ────────────────────────────────────────────
    passed = sum(1 for r in results if r.passed)
    blocked = sum(1 for r in results if r.blocked)
    total = len(results)

    print(f"Rebalances: {total}")
    print(f"  Passed:  {passed}")
    print(f"  Blocked: {blocked}")
    print()

    avg_turnover = np.mean([r.turnover for r in results if r.passed]) if passed else 0
    total_cost = sum(r.cost for r in results)
    print(f"Avg turnover/rebal: {avg_turnover:.1%}")
    print(f"Total sim cost:     ${total_cost:,.2f}")
    print()

    print(f"Final value:  ${state.total_value:,.2f}")
    print(f"Total return: {state.total_value / initial_cash - 1:+.2%}")
    print()

    # ── 6. Generate report ────────────────────────────────────
    report = generate_paper_report(state, journal, engine.block_counts)
    print(report)

    # ── 7. Export files ───────────────────────────────────────
    export_valuations_csv(state, OUTPUT_DIR / "valuations.csv")
    export_journal_csv(journal, OUTPUT_DIR / "journal.csv")
    export_state_json(state, OUTPUT_DIR / "current_state.json")
    state.save()

    print()
    print(f"Exports saved to: {OUTPUT_DIR}")
    print(f"  - paper_state.json")
    print(f"  - paper_journal.jsonl")
    print(f"  - valuations.csv")
    print(f"  - journal.csv")
    print(f"  - current_state.json")

    # ── 8. Verdict ────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  PAPER REPLAY VERDICT")
    print("=" * 60)

    issues = []
    if blocked > total * 0.20:
        issues.append(f"Too many blocks: {blocked}/{total}")
    if passed == 0:
        issues.append("Zero successful rebalances")
    if avg_turnover > 0.50:
        issues.append(f"Avg turnover {avg_turnover:.1%} > 50% cap")
    if state.total_value < initial_cash * 0.5:
        issues.append(f"Catastrophic loss: ${state.total_value:,.0f}")

    # Check journal was written correctly
    journal_count = journal.count()
    if journal_count != total:
        issues.append(f"Journal mismatch: {journal_count} entries vs {total} rebalances")

    if not issues:
        sharpe = 0.0
        if len(state.valuations) > 10:
            vals = pd.Series(
                [v.total_value for v in state.valuations],
                index=pd.to_datetime([v.date for v in state.valuations]),
            )
            vals = vals[~vals.index.duplicated(keep='last')]
            rets = vals.pct_change().dropna()
            if len(rets) > 0 and rets.std() > 0:
                sharpe = float((rets.mean() / rets.std()) * np.sqrt(252))

        print()
        print("  ██████████████████████████████████████████")
        print("  ██       PAPER READY                   ██")
        print("  ██████████████████████████████████████████")
        print(f"  Replay Sharpe: {sharpe:.2f}")
        print(f"  Blocks: {blocked}/{total}")
        print(f"  Journal: {journal_count} entries")
        return 0
    else:
        print()
        print("  ██████████████████████████████████████████")
        print("  ██       NOT READY                     ██")
        print("  ██████████████████████████████████████████")
        for i in issues:
            print(f"  ✗ {i}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
