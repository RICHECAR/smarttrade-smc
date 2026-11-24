"""
Run Chantier 14 — Structural Arbitrage Engine
══════════════════════════════════════════════
4 Axes:
  1. Funding Rate Arbitrage
  2. Perp vs Spot Basis
  3. Cross-Exchange Arbitrage
  4. Pairs Trading (BTC vs ETH)

Rule: "Ne pas prédire le marché. Exploiter ses incohérences."
"""

from __future__ import annotations

import asyncio
import logging
import sys
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict

# Setup path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smc_futures.backtest.arb_engine import (
    ArbBar, ArbTrade, ArbConfig, ArbState, ArbMetrics, compute_arb_metrics,
)
from smc_futures.backtest.arb_funding import (
    FundingArbConfig, simulate_funding_rates, build_arb_bars_funding, run_funding_arb,
)
from smc_futures.backtest.arb_basis import (
    BasisArbConfig, simulate_spot_prices, build_arb_bars_basis, run_basis_arb,
)
from smc_futures.backtest.arb_cross_exchange import (
    CrossExConfig, simulate_exchange_b_prices, build_arb_bars_cross_ex, run_cross_exchange_arb,
)
from smc_futures.backtest.arb_pairs import (
    PairsConfig, build_arb_bars_pairs, run_pairs_trading,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ───────────────────────────────────────────
#  Data Loading
# ───────────────────────────────────────────

async def load_candles(symbol: str, timeframe: str, days: int):
    """Load real candle data from Bitget via RealDataLoader."""
    try:
        from smc_futures.backtest.real_data_loader import RealDataLoader
        loader = RealDataLoader()
        result = await loader.load(symbol=symbol, timeframe=timeframe, days=days, force_api=True)
        if result.candles and len(result.candles) > 0:
            logger.info(
                f"  {symbol} {timeframe} {days}d: {len(result.candles)} bars "
                f"[{result.source}, coverage={result.validation.coverage_pct:.0f}%]"
            )
            return result.candles
        else:
            logger.warning(f"  {symbol}: No data returned")
            return None
    except Exception as e:
        logger.error(f"  {symbol} load failed: {e}")
        return None


# ───────────────────────────────────────────
#  Result Container
# ───────────────────────────────────────────

@dataclass
class ArbResult:
    """Standardized result for comparison table."""
    axe: int
    name: str
    symbol: str
    trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_pnl: float = 0.0
    carry_pnl: float = 0.0
    price_pnl: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    max_dd_pct: float = 0.0
    sharpe: float = 0.0
    avg_holding: float = 0.0
    avg_trade_pnl: float = 0.0
    cost_ratio: float = 0.0
    annualized_ret: float = 0.0
    verdict: str = "NO EDGE"


def extract_result(
    axe: int,
    name: str,
    symbol: str,
    state: ArbState,
    config: ArbConfig,
    total_bars: int,
) -> ArbResult:
    """Extract standardized result from arb state."""
    m = compute_arb_metrics(state, config, total_bars)

    # Verdict logic
    verdict = "NO EDGE"
    if m.total_trades >= 8:
        if m.profit_factor >= 1.5 and m.total_net_pnl > 0 and m.cost_ratio < 0.7:
            verdict = "EDGE FOUND"
        elif m.profit_factor >= 1.1 and m.total_net_pnl > 0:
            verdict = "EDGE WEAK"
    elif m.total_trades >= 3:
        if m.profit_factor >= 2.0 and m.total_net_pnl > 0:
            verdict = "EDGE WEAK (low N)"

    return ArbResult(
        axe=axe,
        name=name,
        symbol=symbol,
        trades=m.total_trades,
        win_rate=m.win_rate,
        profit_factor=m.profit_factor,
        total_pnl=m.total_net_pnl,
        carry_pnl=m.total_carry_pnl,
        price_pnl=m.total_price_pnl,
        total_fees=m.total_fees,
        total_slippage=m.total_slippage,
        max_dd_pct=m.max_drawdown_pct,
        sharpe=m.sharpe_ratio,
        avg_holding=m.avg_holding_bars,
        avg_trade_pnl=m.avg_trade_pnl,
        cost_ratio=m.cost_ratio,
        annualized_ret=m.annualized_return_pct,
        verdict=verdict,
    )


# ───────────────────────────────────────────
#  Table Formatting
# ───────────────────────────────────────────

def print_comparison_table(results: List[ArbResult]):
    """Print formatted comparison table."""
    header = (
        f"{'Axe':>3} {'Alpha':<25} {'Sym':<8} {'#Tr':>4} "
        f"{'WR%':>6} {'PF':>6} {'Exp$':>8} {'PnL$':>9} "
        f"{'Carry$':>8} {'Fees$':>7} {'Slip$':>7} "
        f"{'DD%':>6} {'Sharpe':>7} {'Hold':>5} {'CostR':>6} {'Verdict':<18}"
    )
    sep = "=" * len(header)

    print(f"\n{sep}")
    print("  CHANTIER 14 — STRUCTURAL ARBITRAGE ENGINE")
    print(f"  Rule: Ne pas predire le marche. Exploiter ses incoherences.")
    print(f"{sep}")
    print(header)
    print("-" * len(header))

    for r in results:
        pf_str = f"{r.profit_factor:.2f}" if r.profit_factor < 100 else "INF"
        print(
            f"{r.axe:>3} {r.name:<25} {r.symbol:<8} {r.trades:>4} "
            f"{r.win_rate:>5.1f}% {pf_str:>6} {r.avg_trade_pnl:>8.2f} {r.total_pnl:>9.2f} "
            f"{r.carry_pnl:>8.2f} {r.total_fees:>7.2f} {r.total_slippage:>7.2f} "
            f"{r.max_dd_pct:>5.1f}% {r.sharpe:>7.2f} {r.avg_holding:>5.1f} {r.cost_ratio:>6.2f} {r.verdict:<18}"
        )

    print(sep)


def print_diagnostics(results: List[ArbResult]):
    """Print diagnostic summary."""
    print("\n--- DIAGNOSTICS ---")

    if not results:
        print("  No results to analyze.")
        return

    # Best by PF (min 5 trades)
    viable = [r for r in results if r.trades >= 5]
    if viable:
        best_pf = max(viable, key=lambda r: r.profit_factor)
        print(f"  Best PF (N>=5): {best_pf.name} [{best_pf.symbol}] "
              f"PF={best_pf.profit_factor:.2f}, N={best_pf.trades}, PnL=${best_pf.total_pnl:.2f}")
    else:
        print("  No strategy with >= 5 trades")

    # Best by total PnL
    best_pnl = max(results, key=lambda r: r.total_pnl)
    print(f"  Best PnL: {best_pnl.name} [{best_pnl.symbol}] "
          f"PnL=${best_pnl.total_pnl:.2f}, PF={best_pnl.profit_factor:.2f}")

    # Carry vs Price decomposition
    total_carry = sum(r.carry_pnl for r in results)
    total_price = sum(r.price_pnl for r in results)
    total_costs = sum(r.total_fees + r.total_slippage for r in results)
    print(f"  Aggregate Carry PnL: ${total_carry:.2f}")
    print(f"  Aggregate Price PnL: ${total_price:.2f}")
    print(f"  Aggregate Costs:     ${total_costs:.2f}")
    print(f"  Net (all strategies): ${sum(r.total_pnl for r in results):.2f}")

    # Per-axe summary
    for axe_num in sorted(set(r.axe for r in results)):
        axe_results = [r for r in results if r.axe == axe_num]
        axe_name = axe_results[0].name.split("_")[0] if axe_results else f"Axe {axe_num}"
        best = max(axe_results, key=lambda r: r.total_pnl)
        print(f"  Axe {axe_num}: best={best.name} PnL=${best.total_pnl:.2f} "
              f"PF={best.profit_factor:.2f} N={best.trades}")

    # Trade breakdown
    for r in results:
        if r.trades > 0:
            print(f"    {r.name:<25} {r.symbol}: "
                  f"Carry=${r.carry_pnl:+.2f}, Price=${r.price_pnl:+.2f}, "
                  f"Costs=${-(r.total_fees+r.total_slippage):.2f}")


def print_verdict(results: List[ArbResult]):
    """Print final verdict."""
    edge_found = [r for r in results if r.verdict == "EDGE FOUND"]
    edge_weak = [r for r in results if "EDGE WEAK" in r.verdict]

    print("\n" + "=" * 60)
    if edge_found:
        print("  VERDICT: EDGE FOUND")
        for r in edge_found:
            print(f"    -> {r.name} [{r.symbol}]: PF={r.profit_factor:.2f}, "
                  f"N={r.trades}, PnL=${r.total_pnl:.2f}")
    elif edge_weak:
        print("  VERDICT: EDGE WEAK")
        for r in edge_weak:
            print(f"    -> {r.name} [{r.symbol}]: PF={r.profit_factor:.2f}, "
                  f"N={r.trades}, PnL=${r.total_pnl:.2f}")
    else:
        print("  VERDICT: NO EDGE")
        print("  Aucune strategie d'arbitrage structurel ne montre un edge exploitable.")
        # Cost analysis
        all_costs = sum(r.total_fees + r.total_slippage for r in results)
        all_gross = sum(max(r.carry_pnl + r.price_pnl, 0) for r in results)
        if all_gross > 0:
            print(f"  Ratio couts/revenus bruts: {all_costs/all_gross*100:.1f}%")
    print("=" * 60)


# ───────────────────────────────────────────
#  Main
# ───────────────────────────────────────────

async def main():
    """Run all 4 arbitrage axes."""
    start_time = time.time()
    results: List[ArbResult] = []

    DAYS = 180
    TIMEFRAME = "4h"
    SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    CAPITAL = 10_000.0

    arb_config = ArbConfig(
        initial_capital=CAPITAL,
        maker_fee_rate=0.0002,    # 2 bps
        taker_fee_rate=0.0006,    # 6 bps
        entry_slippage_bps=0.5,   # Limit entry
        exit_slippage_bps=1.0,    # May need taker exit
        two_legged=True,
        bar_duration_ms=14_400_000,  # 4H
    )

    print("=" * 60)
    print("  CHANTIER 14 — STRUCTURAL ARBITRAGE ENGINE")
    print(f"  Capital: ${CAPITAL:,.0f} | Period: {DAYS}d | TF: {TIMEFRAME}")
    print(f"  Fees: maker={arb_config.maker_fee_rate*10000:.1f}bps, taker={arb_config.taker_fee_rate*10000:.1f}bps")
    print("=" * 60)

    # ─── Load Data ───
    print("\n[1/5] Loading data...")
    candle_cache: Dict[str, list] = {}
    for sym in SYMBOLS:
        candles = await load_candles(sym, TIMEFRAME, DAYS)
        if candles and len(candles) >= 50:
            candle_cache[sym] = candles
        else:
            logger.warning(f"  Skipping {sym}: insufficient data")

    if not candle_cache:
        print("FATAL: No data loaded. Aborting.")
        return

    # ─────────────────────────────────────────
    #  AXE 1 — FUNDING RATE ARBITRAGE
    # ─────────────────────────────────────────
    print("\n[2/5] AXE 1: Funding Rate Arbitrage...")

    funding_configs = {
        "BTCUSDT": {"mean": 0.03, "std": 0.63, "entry": 1.0},    # Conservative
        "ETHUSDT": {"mean": 0.15, "std": 0.54, "entry": 0.8},
        "SOLUSDT": {"mean": -0.11, "std": 0.70, "entry": 0.9},
    }

    for sym, candles in candle_cache.items():
        fc = funding_configs.get(sym, {"mean": 0.0, "std": 0.6, "entry": 1.0})

        for seed_offset, label in [(42, "base"), (123, "alt_seed")]:
            funding_rates = simulate_funding_rates(
                len(candles), mean=fc["mean"], std=fc["std"], seed=seed_offset,
            )
            arb_bars = build_arb_bars_funding(candles, funding_rates)

            cfg = FundingArbConfig(
                entry_threshold_bps=fc["entry"],
                exit_threshold_bps=0.3,
                max_holding_bars=18,
                stop_loss_bps=50.0,
                position_pct=0.5,
                leverage=3,
            )

            state = run_funding_arb(arb_bars, cfg, arb_config)
            name = f"Funding_{sym[:3]}_{label}"
            r = extract_result(1, name, sym, state, arb_config, len(arb_bars))
            results.append(r)
            logger.info(f"  {name}: {r.trades} trades, PF={r.profit_factor:.2f}, PnL=${r.total_pnl:.2f}")

    # ─────────────────────────────────────────
    #  AXE 2 — PERP vs SPOT BASIS
    # ─────────────────────────────────────────
    print("\n[3/5] AXE 2: Perp vs Spot Basis Arbitrage...")

    basis_configs = {
        "BTCUSDT": {"mean": -2.0, "std": 6.0},
        "ETHUSDT": {"mean": -1.0, "std": 8.0},
        "SOLUSDT": {"mean": -3.0, "std": 10.0},
    }

    for sym, candles in candle_cache.items():
        bc = basis_configs.get(sym, {"mean": -2.0, "std": 6.0})

        for seed_offset, label in [(43, "base"), (200, "alt_seed")]:
            spot_prices = simulate_spot_prices(
                candles, basis_mean_bps=bc["mean"], basis_std_bps=bc["std"], seed=seed_offset,
            )
            arb_bars = build_arb_bars_basis(candles, spot_prices)

            cfg = BasisArbConfig(
                entry_threshold_bps=8.0,
                exit_threshold_bps=2.0,
                max_holding_bars=24,
                stop_loss_bps=60.0,
                position_pct=0.4,
                leverage=2,
            )

            state = run_basis_arb(arb_bars, cfg, arb_config)
            name = f"Basis_{sym[:3]}_{label}"
            r = extract_result(2, name, sym, state, arb_config, len(arb_bars))
            results.append(r)
            logger.info(f"  {name}: {r.trades} trades, PF={r.profit_factor:.2f}, PnL=${r.total_pnl:.2f}")

    # ─────────────────────────────────────────
    #  AXE 3 — CROSS-EXCHANGE ARBITRAGE
    # ─────────────────────────────────────────
    print("\n[4/5] AXE 3: Cross-Exchange Arbitrage (SYNTHETIC)...")

    for sym, candles in candle_cache.items():
        for seed_offset, label in [(44, "base"), (300, "alt_seed")]:
            ex_b_prices = simulate_exchange_b_prices(
                candles, spread_std_bps=4.0, seed=seed_offset,
            )
            arb_bars = build_arb_bars_cross_ex(candles, ex_b_prices)

            cfg = CrossExConfig(
                entry_threshold_bps=6.0,
                exit_threshold_bps=1.5,
                execution_delay_bars=1,
                max_holding_bars=12,
                stop_loss_bps=40.0,
                position_pct=0.3,
                leverage=2,
            )

            state = run_cross_exchange_arb(arb_bars, cfg, arb_config)
            name = f"CrossEx_{sym[:3]}_{label}"
            r = extract_result(3, name, sym, state, arb_config, len(arb_bars))
            results.append(r)
            logger.info(f"  {name}: {r.trades} trades, PF={r.profit_factor:.2f}, PnL=${r.total_pnl:.2f}")

    # ─────────────────────────────────────────
    #  AXE 4 — PAIRS TRADING (BTC vs ETH)
    # ─────────────────────────────────────────
    print("\n[5/5] AXE 4: Pairs Trading BTC vs ETH...")

    if "BTCUSDT" in candle_cache and "ETHUSDT" in candle_cache:
        btc_candles = candle_cache["BTCUSDT"]
        eth_candles = candle_cache["ETHUSDT"]

        # Align by minimum length
        arb_bars = build_arb_bars_pairs(btc_candles, eth_candles)

        if len(arb_bars) >= 100:
            # Config variations
            pairs_variations = [
                ("Pairs_z2.0", PairsConfig(entry_z=2.0, exit_z=0.5, beta_window=60, zscore_window=30)),
                ("Pairs_z1.5", PairsConfig(entry_z=1.5, exit_z=0.3, beta_window=60, zscore_window=30)),
                ("Pairs_z2.5", PairsConfig(entry_z=2.5, exit_z=0.8, beta_window=90, zscore_window=45)),
            ]

            for name, cfg in pairs_variations:
                state = run_pairs_trading(arb_bars, cfg, arb_config)
                r = extract_result(4, name, "BTC/ETH", state, arb_config, len(arb_bars))
                results.append(r)
                logger.info(f"  {name}: {r.trades} trades, PF={r.profit_factor:.2f}, PnL=${r.total_pnl:.2f}")
        else:
            logger.warning(f"  Pairs: insufficient aligned bars ({len(arb_bars)})")
    else:
        logger.warning("  Pairs: need both BTCUSDT and ETHUSDT data")

    # ─── Results ───
    print_comparison_table(results)
    print_diagnostics(results)
    print_verdict(results)

    elapsed = time.time() - start_time
    print(f"\nCompleted in {elapsed:.1f}s")

    # Save report
    report_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "reports", f"chantier14_arb_{DAYS}d.txt"
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"CHANTIER 14 — STRUCTURAL ARBITRAGE ENGINE — {DAYS}d\n")
        f.write(f"Capital: ${CAPITAL:,.0f} | TF: {TIMEFRAME}\n")
        f.write(f"Strategies tested: {len(results)}\n\n")
        for r in results:
            f.write(
                f"[Axe {r.axe}] {r.name:<25} {r.symbol:<8} "
                f"N={r.trades:>3}, WR={r.win_rate:>5.1f}%, PF={r.profit_factor:>5.2f}, "
                f"PnL=${r.total_pnl:>9.2f}, Carry=${r.carry_pnl:>8.2f}, "
                f"Fees=${r.total_fees:>7.2f}, DD={r.max_dd_pct:>5.1f}% "
                f"-> {r.verdict}\n"
            )
        f.write(f"\nElapsed: {elapsed:.1f}s\n")
    print(f"Report saved: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
