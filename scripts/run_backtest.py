"""
Backtester SMC — Replay des signaux sur données MySQL
═══════════════════════════════════════════════════════

Charge les bougies historiques depuis MySQL et les rejoue à travers
le pipeline SMC complet (Structure → Liquidity → FVG → OB → MTFBias
→ SignalBuilder) pour évaluer la qualité des signaux.

Architecture :
  MySQL (candles 4H/15m/5m) → SMC Engines → Signals → Simulation PnL

Fonctionnalités :
  ✓ Replay fidèle du pipeline SMC complet (3 timeframes)
  ✓ Simulation réaliste (slippage, fees, margin)
  ✓ Métriques : win rate, profit factor, Sharpe, max drawdown
  ✓ Export des résultats dans MySQL (backtest_runs + backtest_trades)
  ✓ Comparaison multi-paramètres (grid search)
  ✓ Export CSV des trades pour analyse externe

Usage :
    # Backtest standard (derniers 30 jours)
    python scripts/run_backtest.py --days 30

    # Backtest sur une période spécifique
    python scripts/run_backtest.py --start 2025-12-01 --end 2026-03-01

    # Backtest avec paramètres personnalisés
    python scripts/run_backtest.py --risk 0.02 --leverage 5 --min-rr 2.0

    # Grid search : tester plusieurs combinaisons
    python scripts/run_backtest.py --grid

    # Symbole unique
    python scripts/run_backtest.py --symbols BTCUSDT --days 60
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# Ajouter le répertoire racine au path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import and_
from backend.db.mysql_models import (
    Candle as CandleRow, BacktestRun, BacktestTrade,
    get_mysql_engine, get_mysql_session, MarketBase,
)

# SMC Engine imports
from smc_futures.engine.multi_broker_engine import Candle
from smc_futures.engine.strategy.structure import StructureEngine
from smc_futures.engine.strategy.liquidity import LiquidityEngine
from smc_futures.engine.strategy.fvg import FVGEngine
from smc_futures.engine.strategy.orderblocks import OrderBlockEngine
from smc_futures.engine.strategy.mtf_bias import MTFBiasEngine
from smc_futures.engine.strategy.signal_builder import SignalBuilder

logger = logging.getLogger("backtester")

# ══════════════════════════════════════════════════════════════
#  CONFIGURATION DU BACKTEST
# ══════════════════════════════════════════════════════════════

@dataclass
class BacktestConfig:
    """Paramètres du backtest."""
    # Période
    symbols: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    days: int = 30

    # Capital & risque
    initial_capital: float = 1000.0
    risk_pct: float = 0.01          # 1% par trade
    leverage: int = 10
    min_rr: float = 1.5

    # SL/TP
    sl_pct: float = 1.0             # Stop loss par défaut (% du prix)
    tp_pct: float = 2.0             # Take profit par défaut (% du prix)

    # Frais & slippage
    fee_rate: float = 0.0006        # 0.06% (taker Bitget)
    slippage_pct: float = 0.01      # 0.01% slippage moyen

    # Limites
    max_open_trades: int = 3
    max_trades_per_day: int = 10
    cooldown_bars: int = 3          # Barres d'attente après un trade

    # SMC params
    allow_countertrend: bool = True
    neutral_permissive: bool = True
    sweep_fallback_enabled: bool = True

    # HTF/MTF/LTF timeframes
    htf_tf: str = "4h"
    mtf_tf: str = "15m"
    ltf_tf: str = "5m"

    # Broker source
    broker: str = "bitget"

    # Nom du run
    name: str = ""


# ══════════════════════════════════════════════════════════════
#  CHARGEMENT DONNÉES DEPUIS MySQL
# ══════════════════════════════════════════════════════════════

def load_candles_from_mysql(
    session, broker: str, symbol: str, timeframe: str,
    start_ts: int, end_ts: int,
) -> List[Candle]:
    """Charge les bougies depuis MySQL et les convertit en Candle dataclass."""
    rows = session.query(CandleRow).filter(
        and_(
            CandleRow.broker == broker,
            CandleRow.symbol == symbol,
            CandleRow.timeframe == timeframe,
            CandleRow.open_time >= start_ts,
            CandleRow.open_time <= end_ts,
        )
    ).order_by(CandleRow.open_time).all()

    candles = [
        Candle(
            timestamp=row.open_time,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
        )
        for row in rows
    ]

    logger.info(f"[MYSQL] {symbol}/{timeframe} : {len(candles)} bougies "
                f"({datetime.utcfromtimestamp(start_ts/1000).date()} → "
                f"{datetime.utcfromtimestamp(end_ts/1000).date()})")
    return candles


# ══════════════════════════════════════════════════════════════
#  SIMULATEUR DE POSITIONS
# ══════════════════════════════════════════════════════════════

@dataclass
class SimPosition:
    """Position simulée pour le backtest."""
    symbol: str
    direction: str      # "long" ou "short"
    entry_price: float
    stop_loss: float
    take_profit: float
    qty: float
    entry_time: int     # timestamp ms
    setup_type: str = ""
    rr_ratio: float = 0.0

    # Résultat (rempli à la clôture)
    exit_price: float = 0.0
    exit_time: int = 0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    result: str = ""    # win / loss / be
    exit_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.exit_time == 0


class PositionSimulator:
    """Simule l'ouverture/fermeture de positions avec gestion du capital."""

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.capital = config.initial_capital
        self.peak_capital = config.initial_capital
        self.max_drawdown = 0.0
        self.open_positions: List[SimPosition] = []
        self.closed_positions: List[SimPosition] = []
        self.daily_trades: Dict[str, int] = {}
        self.cooldown_until: Dict[str, int] = {}   # symbol → ts

    def can_open(self, symbol: str, timestamp: int) -> bool:
        """Vérifie si on peut ouvrir une nouvelle position."""
        # Max trades ouverts
        if len(self.open_positions) >= self.config.max_open_trades:
            return False

        # Pas de double position sur le même symbole
        for pos in self.open_positions:
            if pos.symbol == symbol:
                return False

        # Cooldown
        if symbol in self.cooldown_until:
            if timestamp < self.cooldown_until[symbol]:
                return False

        # Max trades par jour
        day_key = datetime.utcfromtimestamp(timestamp / 1000).strftime("%Y-%m-%d")
        if self.daily_trades.get(day_key, 0) >= self.config.max_trades_per_day:
            return False

        # Capital minimum
        if self.capital < 10.0:
            return False

        return True

    def open_position(
        self, symbol: str, direction: str, entry_price: float,
        stop_loss: float, take_profit: float, timestamp: int,
        setup_type: str = "", rr_ratio: float = 0.0,
    ) -> Optional[SimPosition]:
        """Ouvre une position simulée."""
        if not self.can_open(symbol, timestamp):
            return None

        # Calculer la quantité basée sur le risque
        risk_amount = self.capital * self.config.risk_pct
        stop_distance = abs(entry_price - stop_loss)
        if stop_distance == 0:
            stop_distance = entry_price * (self.config.sl_pct / 100)

        qty = risk_amount / stop_distance
        notional = qty * entry_price

        # Vérifier la marge avec le levier
        margin_required = notional / self.config.leverage
        if margin_required > self.capital * 0.9:
            qty = (self.capital * 0.9 * self.config.leverage) / entry_price
            notional = qty * entry_price

        # Appliquer le slippage à l'entrée
        if direction == "long":
            entry_price *= (1 + self.config.slippage_pct / 100)
        else:
            entry_price *= (1 - self.config.slippage_pct / 100)

        pos = SimPosition(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            qty=qty,
            entry_time=timestamp,
            setup_type=setup_type,
            rr_ratio=rr_ratio,
        )

        self.open_positions.append(pos)

        # Compter le trade pour la limite journalière
        day_key = datetime.utcfromtimestamp(timestamp / 1000).strftime("%Y-%m-%d")
        self.daily_trades[day_key] = self.daily_trades.get(day_key, 0) + 1

        return pos

    def update_positions(self, candle: Candle) -> List[SimPosition]:
        """Met à jour les positions ouvertes avec une nouvelle bougie 5m."""
        closed_this_bar = []

        for pos in list(self.open_positions):
            if not pos.is_open:
                continue

            hit_sl = False
            hit_tp = False
            exit_price = 0.0

            if pos.direction == "long":
                # Stop loss touché ?
                if candle.low <= pos.stop_loss:
                    hit_sl = True
                    exit_price = pos.stop_loss * (1 - self.config.slippage_pct / 100)
                # Take profit touché ?
                elif candle.high >= pos.take_profit:
                    hit_tp = True
                    exit_price = pos.take_profit * (1 - self.config.slippage_pct / 100)
            else:  # short
                if candle.high >= pos.stop_loss:
                    hit_sl = True
                    exit_price = pos.stop_loss * (1 + self.config.slippage_pct / 100)
                elif candle.low <= pos.take_profit:
                    hit_tp = True
                    exit_price = pos.take_profit * (1 + self.config.slippage_pct / 100)

            if hit_sl or hit_tp:
                self._close_position(pos, exit_price, candle.timestamp,
                                     "SL" if hit_sl else "TP")
                closed_this_bar.append(pos)

        return closed_this_bar

    def _close_position(self, pos: SimPosition, exit_price: float,
                        timestamp: int, reason: str):
        """Ferme une position et calcule le PnL."""
        # Frais d'entrée + sortie
        fee_entry = pos.qty * pos.entry_price * self.config.fee_rate
        fee_exit = pos.qty * exit_price * self.config.fee_rate
        total_fees = fee_entry + fee_exit

        # PnL brut
        if pos.direction == "long":
            raw_pnl = (exit_price - pos.entry_price) * pos.qty
        else:
            raw_pnl = (pos.entry_price - exit_price) * pos.qty

        # PnL net (après frais)
        pos.pnl = raw_pnl - total_fees
        pos.pnl_pct = (pos.pnl / self.capital) * 100 if self.capital > 0 else 0
        pos.exit_price = exit_price
        pos.exit_time = timestamp
        pos.exit_reason = reason

        if reason == "TP":
            pos.result = "win"
        elif reason == "SL":
            pos.result = "loss"
        else:
            pos.result = "be"

        # Mettre à jour le capital
        self.capital += pos.pnl
        self.peak_capital = max(self.peak_capital, self.capital)
        drawdown = (self.peak_capital - self.capital) / self.peak_capital * 100
        self.max_drawdown = max(self.max_drawdown, drawdown)

        # Déplacer vers closed
        if pos in self.open_positions:
            self.open_positions.remove(pos)
        self.closed_positions.append(pos)

        # Cooldown
        tf_ms = 300_000  # 5m
        self.cooldown_until[pos.symbol] = timestamp + (self.config.cooldown_bars * tf_ms)

    def get_stats(self) -> Dict:
        """Calcule les statistiques du backtest."""
        trades = self.closed_positions
        if not trades:
            return {
                "total_trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "total_pnl": 0.0,
                "max_drawdown_pct": 0.0, "profit_factor": 0.0,
                "avg_rr": 0.0, "sharpe_ratio": 0.0,
                "final_capital": self.capital,
            }

        wins = [t for t in trades if t.result == "win"]
        losses = [t for t in trades if t.result == "loss"]

        total_pnl = sum(t.pnl for t in trades)
        gross_profit = sum(t.pnl for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Sharpe ratio (simplifié)
        pnls = [t.pnl_pct for t in trades]
        if len(pnls) > 1:
            import statistics
            avg_return = statistics.mean(pnls)
            std_return = statistics.stdev(pnls)
            sharpe = (avg_return / std_return) * (252 ** 0.5) if std_return > 0 else 0
        else:
            sharpe = 0.0

        avg_rr = statistics.mean([t.rr_ratio for t in trades if t.rr_ratio > 0]) \
            if any(t.rr_ratio > 0 for t in trades) else 0.0

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) * 100,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl / self.config.initial_capital * 100,
            "max_drawdown_pct": self.max_drawdown,
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "avg_rr": avg_rr,
            "final_capital": self.capital,
            "capital_growth_pct": (self.capital - self.config.initial_capital) / self.config.initial_capital * 100,
        }


# ══════════════════════════════════════════════════════════════
#  BACKTESTER PRINCIPAL
# ══════════════════════════════════════════════════════════════

class SMCBacktester:
    """
    Backtester SMC complet : replay les 3 timeframes et génère des signaux.
    """

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.simulator = PositionSimulator(config)
        self.signals_generated = 0
        self.signals_rejected = 0

    def _create_smc_engines(self):
        """Crée les instances des moteurs SMC."""
        return {
            "structure_htf": StructureEngine(lookback_period=50, min_swing_bars=2),
            "structure_mtf": StructureEngine(lookback_period=50, min_swing_bars=2),
            "structure_ltf": StructureEngine(lookback_period=50, min_swing_bars=2),
            "liquidity": LiquidityEngine(lookback_period=50),
            "fvg": FVGEngine(lookback_period=100),
            "orderblocks": OrderBlockEngine(lookback_period=100),
            "mtf_bias": MTFBiasEngine(
                allow_countertrend=self.config.allow_countertrend,
            ),
            "signal_builder": SignalBuilder(
                min_rr_ratio=self.config.min_rr,
                neutral_permissive=self.config.neutral_permissive,
                allow_countertrend=self.config.allow_countertrend,
                sweep_fallback_enabled=self.config.sweep_fallback_enabled,
                # Backtest mode: enable soft bypasses for realistic signal generation
                allow_no_sweep_with_ob=True,
                allow_no_sweep_with_fvg=True,
                fallback_allow_no_fvg=True,
                no_sweep_require_fresh_ob=True,
                no_sweep_block_strong_htf=False,
                # LTF trigger relaxation
                allow_no_ltf_trigger_with_zone=True,
                # Entry zone relaxation
                entry_fallback_mode=True,
                entry_zone_nonfresh=True,
            ),
        }

    def run(self, session) -> Dict:
        """
        Exécute le backtest complet.
        
        Retourne les statistiques et les trades.
        """
        config = self.config

        # Calculer les bornes temporelles
        if config.end_date:
            end_ts = int(config.end_date.timestamp() * 1000)
        else:
            end_ts = int(time.time() * 1000)

        if config.start_date:
            start_ts = int(config.start_date.timestamp() * 1000)
        else:
            start_ts = end_ts - (config.days * 86_400_000)

        # Charger les données avec un buffer pour le warmup (300 bougies HTF)
        warmup_ms = 300 * 14_400_000  # 300 bougies × 4h = ~50 jours
        data_start_ts = start_ts - warmup_ms

        print()
        print("=" * 58)
        print("   BACKTESTER SMC - Replay sur donnees historiques")
        print("=" * 58)
        print(f"  Symboles    : {', '.join(config.symbols)}")
        print(f"  Période     : {datetime.utcfromtimestamp(start_ts/1000).date()} → "
              f"{datetime.utcfromtimestamp(end_ts/1000).date()}")
        print(f"  Capital     : ${config.initial_capital:,.2f}")
        print(f"  Risque      : {config.risk_pct*100:.1f}% | Levier {config.leverage}x")
        print(f"  Min RR      : {config.min_rr}")
        print(f"  Fees        : {config.fee_rate*100:.3f}% + {config.slippage_pct}% slippage")
        print()

        all_results = {}

        for symbol in config.symbols:
            print(f"  [+] {symbol} - chargement des donnees...", end="", flush=True)

            # Charger les 3 timeframes depuis MySQL
            htf_candles = load_candles_from_mysql(
                session, config.broker, symbol, config.htf_tf,
                data_start_ts, end_ts,
            )
            mtf_candles = load_candles_from_mysql(
                session, config.broker, symbol, config.mtf_tf,
                data_start_ts, end_ts,
            )
            ltf_candles = load_candles_from_mysql(
                session, config.broker, symbol, config.ltf_tf,
                data_start_ts, end_ts,
            )

            if not ltf_candles:
                print(f" ❌ Pas de données LTF ({config.ltf_tf})")
                continue

            print(f" HTF={len(htf_candles)} MTF={len(mtf_candles)} LTF={len(ltf_candles)}")

            # Créer les moteurs SMC (1 set par symbole)
            engines = self._create_smc_engines()

            # ─────────────────────────────────────────
            # Phase 1 : Warmup HTF (alimenter le bias)
            # ─────────────────────────────────────────
            warmup_htf = [c for c in htf_candles if c.timestamp < start_ts]
            for c in warmup_htf:
                engines["structure_htf"].add_candle(c)

            # Warmup MTF
            warmup_mtf = [c for c in mtf_candles if c.timestamp < start_ts]
            for c in warmup_mtf:
                engines["structure_mtf"].add_candle(c)
                engines["liquidity"].add_candle(c)
                engines["fvg"].add_candle(c)
                engines["orderblocks"].add_candle(c)

            # Warmup LTF
            warmup_ltf = [c for c in ltf_candles if c.timestamp < start_ts]
            for c in warmup_ltf:
                engines["structure_ltf"].add_candle(c)

            # Mettre à jour le bias après warmup
            htf_state = engines["structure_htf"].get_current_state()
            engines["mtf_bias"].update_htf_context(htf_state)
            mtf_struct = engines["structure_mtf"].get_current_state()
            ob_state = engines["orderblocks"].get_current_state()
            fvg_state = engines["fvg"].get_current_state()
            engines["mtf_bias"].update_mtf_setup(mtf_struct, ob_state, fvg_state)
            ltf_state = engines["structure_ltf"].get_current_state()
            engines["mtf_bias"].update_ltf_data(ltf_state)

            print(f"  -> Warmup termine ({len(warmup_htf)} HTF + {len(warmup_mtf)} MTF + {len(warmup_ltf)} LTF)")

            # ─────────────────────────────────────────
            # Phase 2 : Replay bar par bar (5m)
            # ─────────────────────────────────────────
            test_ltf = [c for c in ltf_candles if c.timestamp >= start_ts]
            test_mtf = [c for c in mtf_candles if c.timestamp >= start_ts]
            test_htf = [c for c in htf_candles if c.timestamp >= start_ts]

            # Index pour avancer les TF supérieurs
            mtf_idx = 0
            htf_idx = 0

            signal_count = 0
            bar_count = 0

            for ltf_candle in test_ltf:
                bar_count += 1
                ts = ltf_candle.timestamp

                # Mettre à jour les positions ouvertes
                closed = self.simulator.update_positions(ltf_candle)

                # Avancer le HTF si de nouvelles bougies 4h sont disponibles
                while htf_idx < len(test_htf) and test_htf[htf_idx].timestamp <= ts:
                    engines["structure_htf"].add_candle(test_htf[htf_idx])
                    htf_state = engines["structure_htf"].get_current_state()
                    engines["mtf_bias"].update_htf_context(htf_state)
                    htf_idx += 1

                # Avancer le MTF
                while mtf_idx < len(test_mtf) and test_mtf[mtf_idx].timestamp <= ts:
                    mtf_c = test_mtf[mtf_idx]
                    engines["structure_mtf"].add_candle(mtf_c)
                    engines["liquidity"].add_candle(mtf_c)
                    engines["fvg"].add_candle(mtf_c)
                    engines["orderblocks"].add_candle(mtf_c)

                    mtf_struct = engines["structure_mtf"].get_current_state()
                    ob_state = engines["orderblocks"].get_current_state()
                    fvg_state = engines["fvg"].get_current_state()
                    engines["mtf_bias"].update_mtf_setup(mtf_struct, ob_state, fvg_state)
                    mtf_idx += 1

                # Mettre à jour le LTF
                engines["structure_ltf"].add_candle(ltf_candle)
                ltf_state = engines["structure_ltf"].get_current_state()
                engines["mtf_bias"].update_ltf_data(ltf_state)

                # ─── Générer un signal ───
                try:
                    ltf_trigger = engines["mtf_bias"].check_ltf_trigger(symbol)
                    mtf_state = engines["mtf_bias"].get_current_state()
                    struct_state = engines["structure_mtf"].get_current_state()
                    liq_state = engines["liquidity"].get_current_state()
                    ob_state_now = engines["orderblocks"].get_current_state()
                    fvg_state_now = engines["fvg"].get_current_state()

                    result = engines["signal_builder"].build_signal(
                        symbol=symbol,
                        structure_state=struct_state,
                        liquidity_state=liq_state,
                        ob_state=ob_state_now,
                        fvg_state=fvg_state_now,
                        mtf_state=mtf_state,
                        current_price=ltf_candle.close,
                        timestamp=ts,
                        ltf_trigger=ltf_trigger,
                    )

                    if result and hasattr(result, 'signal') and result.signal:
                        sig = result.signal
                        self.signals_generated += 1

                        # Extraire SL/TP
                        entry = getattr(sig, 'entry_price', ltf_candle.close) or ltf_candle.close
                        sl = getattr(sig, 'stop_loss', 0) or 0
                        tp = getattr(sig, 'take_profit', 0) or 0
                        direction = getattr(sig, 'direction', 'long')
                        setup = getattr(sig, 'setup_type', 'smc')
                        rr = getattr(sig, 'rr_ratio', 0) or 0

                        # Auto SL/TP si manquants
                        if sl <= 0:
                            if direction == "long":
                                sl = entry * (1 - config.sl_pct / 100)
                            else:
                                sl = entry * (1 + config.sl_pct / 100)
                        if tp <= 0:
                            if direction == "long":
                                tp = entry * (1 + config.tp_pct / 100)
                            else:
                                tp = entry * (1 - config.tp_pct / 100)

                        # RR check
                        stop_dist = abs(entry - sl)
                        tp_dist = abs(tp - entry)
                        if stop_dist > 0:
                            rr = tp_dist / stop_dist

                        if rr < config.min_rr:
                            self.signals_rejected += 1
                            continue

                        # Ouvrir la position
                        pos = self.simulator.open_position(
                            symbol=symbol,
                            direction=direction,
                            entry_price=entry,
                            stop_loss=sl,
                            take_profit=tp,
                            timestamp=ts,
                            setup_type=str(setup),
                            rr_ratio=rr,
                        )
                        if pos:
                            signal_count += 1

                except Exception as e:
                    # Log signal errors for debugging
                    logger.warning(f"Signal error: {type(e).__name__}: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

                # Progress
                if bar_count % 1000 == 0:
                    pct = bar_count / len(test_ltf) * 100
                    print(f"\r  → {symbol} : {pct:5.1f}% | "
                          f"{signal_count} signaux | "
                          f"capital ${self.simulator.capital:,.2f}", end="", flush=True)

            # Fermer les positions encore ouvertes (à la dernière bougie)
            for pos in list(self.simulator.open_positions):
                if pos.symbol == symbol:
                    last_price = test_ltf[-1].close if test_ltf else 0
                    self.simulator._close_position(
                        pos, last_price, test_ltf[-1].timestamp if test_ltf else 0,
                        "END_OF_DATA"
                    )

            print(f"\r  ✅ {symbol} : {bar_count} barres | "
                  f"{signal_count} signaux | "
                  f"capital ${self.simulator.capital:,.2f}        ")

        # ─────────────────────────────────────────
        #  Résultats
        # ─────────────────────────────────────────
        stats = self.simulator.get_stats()
        stats["signals_generated"] = self.signals_generated
        stats["signals_rejected"] = self.signals_rejected

        return stats

    def save_results(self, session, stats: Dict) -> int:
        """Sauvegarde les résultats dans MySQL."""
        config = self.config

        if config.end_date:
            end_date = config.end_date
        else:
            end_date = datetime.utcnow()

        if config.start_date:
            start_date = config.start_date
        else:
            start_date = end_date - timedelta(days=config.days)

        run = BacktestRun(
            name=config.name or f"SMC_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            strategy="smc",
            symbols=",".join(config.symbols),
            timeframe=config.mtf_tf,
            start_date=start_date,
            end_date=end_date,
            initial_capital=config.initial_capital,
            risk_pct=config.risk_pct,
            leverage=config.leverage,
            total_trades=stats.get("total_trades", 0),
            wins=stats.get("wins", 0),
            losses=stats.get("losses", 0),
            win_rate=stats.get("win_rate", 0),
            total_pnl=stats.get("total_pnl", 0),
            max_drawdown_pct=stats.get("max_drawdown_pct", 0),
            sharpe_ratio=stats.get("sharpe_ratio"),
            profit_factor=stats.get("profit_factor"),
            avg_rr=stats.get("avg_rr"),
            params_json=json.dumps({
                "sl_pct": config.sl_pct,
                "tp_pct": config.tp_pct,
                "fee_rate": config.fee_rate,
                "slippage_pct": config.slippage_pct,
                "min_rr": config.min_rr,
                "allow_countertrend": config.allow_countertrend,
            }),
        )
        session.add(run)
        session.flush()
        run_id = run.id

        # Sauvegarder chaque trade
        for pos in self.simulator.closed_positions:
            trade = BacktestTrade(
                run_id=run_id,
                symbol=pos.symbol,
                direction=pos.direction,
                entry_price=pos.entry_price,
                exit_price=pos.exit_price,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
                qty=pos.qty,
                rr_ratio=pos.rr_ratio,
                pnl=pos.pnl,
                pnl_pct=pos.pnl_pct,
                result=pos.result,
                setup_type=pos.setup_type,
                entry_time=pos.entry_time,
                exit_time=pos.exit_time,
                duration_min=int((pos.exit_time - pos.entry_time) / 60_000) if pos.exit_time else 0,
                entry_reason=pos.setup_type,
                exit_reason=pos.exit_reason,
            )
            session.add(trade)

        session.commit()
        return run_id

    def export_trades_csv(self, output_file: str = "data/backtest_trades.csv"):
        """Exporte les trades en CSV."""
        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)

        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "symbol", "direction", "setup_type",
                "entry_price", "exit_price", "stop_loss", "take_profit",
                "qty", "rr_ratio", "pnl", "pnl_pct", "result",
                "entry_time", "exit_time", "exit_reason",
                "duration_min",
            ])
            for pos in self.simulator.closed_positions:
                entry_dt = datetime.utcfromtimestamp(pos.entry_time / 1000).isoformat()
                exit_dt = datetime.utcfromtimestamp(pos.exit_time / 1000).isoformat() if pos.exit_time else ""
                duration = int((pos.exit_time - pos.entry_time) / 60_000) if pos.exit_time else 0
                writer.writerow([
                    pos.symbol, pos.direction, pos.setup_type,
                    f"{pos.entry_price:.8f}", f"{pos.exit_price:.8f}",
                    f"{pos.stop_loss:.8f}", f"{pos.take_profit:.8f}",
                    f"{pos.qty:.8f}", f"{pos.rr_ratio:.2f}",
                    f"{pos.pnl:.4f}", f"{pos.pnl_pct:.2f}", pos.result,
                    entry_dt, exit_dt, pos.exit_reason,
                    duration,
                ])

        print(f"  Trades exportes -> {output_file}")


# ══════════════════════════════════════════════════════════════
#  GRID SEARCH
# ══════════════════════════════════════════════════════════════

def run_grid_search(base_config: BacktestConfig, session):
    """
    Teste plusieurs combinaisons de paramètres.
    """
    import itertools

    risk_values = [0.005, 0.01, 0.02]
    rr_values = [1.0, 1.5, 2.0, 2.5]
    sl_values = [0.5, 1.0, 1.5]
    tp_values = [1.5, 2.0, 3.0]

    combos = list(itertools.product(risk_values, rr_values, sl_values, tp_values))

    print(f"\n  🔍 GRID SEARCH — {len(combos)} combinaisons")
    print(f"  Risk: {risk_values}")
    print(f"  RR  : {rr_values}")
    print(f"  SL% : {sl_values}")
    print(f"  TP% : {tp_values}")
    print()

    results = []

    for i, (risk, rr, sl, tp) in enumerate(combos):
        cfg = BacktestConfig(
            symbols=base_config.symbols,
            start_date=base_config.start_date,
            end_date=base_config.end_date,
            days=base_config.days,
            initial_capital=base_config.initial_capital,
            leverage=base_config.leverage,
            broker=base_config.broker,
            risk_pct=risk,
            min_rr=rr,
            sl_pct=sl,
            tp_pct=tp,
            name=f"grid_{i+1}_r{risk}_rr{rr}_sl{sl}_tp{tp}",
        )

        bt = SMCBacktester(cfg)
        stats = bt.run(session)
        stats["params"] = {"risk": risk, "rr": rr, "sl": sl, "tp": tp}
        results.append(stats)

        bt.save_results(session, stats)

        pf = stats.get("profit_factor", 0)
        wr = stats.get("win_rate", 0)
        pnl = stats.get("total_pnl", 0)
        print(f"  [{i+1}/{len(combos)}] risk={risk} rr={rr} sl={sl} tp={tp} → "
              f"WR={wr:.0f}% PF={pf:.2f} PnL=${pnl:+.2f}")

    # Trier par profit factor
    results.sort(key=lambda r: r.get("profit_factor", 0), reverse=True)

    print()
    print("=" * 70)
    print("  TOP 5 MEILLEURES COMBINAISONS :")
    print("=" * 70)
    for i, r in enumerate(results[:5]):
        p = r["params"]
        print(f"  #{i+1} risk={p['risk']} rr={p['rr']} sl={p['sl']} tp={p['tp']}")
        print(f"       WR={r['win_rate']:.0f}% | PF={r['profit_factor']:.2f} | "
              f"PnL=${r['total_pnl']:+.2f} | DD={r['max_drawdown_pct']:.1f}% | "
              f"Sharpe={r.get('sharpe_ratio', 0):.2f}")


# ======================================================================
#  AFFICHAGE DES RESULTATS
# ======================================================================

def print_results(stats: Dict, config: BacktestConfig):
    """Affiche les résultats du backtest."""
    print()
    print("=" * 58)
    print("   RESULTATS DU BACKTEST")
    print("=" * 58)
    print()

    total = stats.get("total_trades", 0)
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    wr = stats.get("win_rate", 0)
    pnl = stats.get("total_pnl", 0)
    pnl_pct = stats.get("total_pnl_pct", 0)
    dd = stats.get("max_drawdown_pct", 0)
    pf = stats.get("profit_factor", 0)
    sharpe = stats.get("sharpe_ratio", 0)
    final = stats.get("final_capital", config.initial_capital)
    growth = stats.get("capital_growth_pct", 0)

    print(f"  Trades totaux     : {total}")
    print(f"  Victoires         : {wins} ({wr:.1f}%)")
    print(f"  Defaites          : {losses}")
    print(f"  Signaux generes   : {stats.get('signals_generated', 0)}")
    print(f"  Signaux rejetes   : {stats.get('signals_rejected', 0)}")
    print()
    print(f"  PnL total         : ${pnl:+,.2f} ({pnl_pct:+.1f}%)")
    print(f"  Capital final     : ${final:,.2f} ({growth:+.1f}%)")
    print(f"  Max drawdown      : {dd:.1f}%")
    print(f"  Profit factor     : {pf:.2f}")
    print(f"  Sharpe ratio      : {sharpe:.2f}")
    print(f"  RR moyen          : {stats.get('avg_rr', 0):.2f}")
    print()

    # Verdict
    if pf >= 2.0 and wr >= 50:
        print("  [OK] EXCELLENT - Strategie performante")
    elif pf >= 1.5 and wr >= 45:
        print("  [~~] BON - Strategie acceptable, optimisable")
    elif pf >= 1.0:
        print("  [--] MOYEN - Strategie tout juste rentable")
    else:
        print("  [!!] MAUVAIS - Strategie perdante, ajustements necessaires")

    print()
    print("═" * 58)


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="Backtester SMC — Replay sur données MySQL",
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--start", type=str, help="Date début YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="Date fin YYYY-MM-DD")
    parser.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--risk", type=float, default=0.01)
    parser.add_argument("--leverage", type=int, default=10)
    parser.add_argument("--min-rr", type=float, default=1.5)
    parser.add_argument("--sl", type=float, default=1.0, help="SL par défaut (%%)")
    parser.add_argument("--tp", type=float, default=2.0, help="TP par défaut (%%)")
    parser.add_argument("--broker", type=str, default="bitget")
    parser.add_argument("--name", type=str, default="", help="Nom du run")
    parser.add_argument("--grid", action="store_true", help="Grid search")
    parser.add_argument("--export-csv", action="store_true", help="Exporter en CSV")
    parser.add_argument("--no-save", action="store_true", help="Ne pas sauvegarder dans MySQL")
    return parser.parse_args()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    args = parse_args()

    config = BacktestConfig(
        symbols=args.symbols.split(","),
        days=args.days,
        initial_capital=args.capital,
        risk_pct=args.risk,
        leverage=args.leverage,
        min_rr=args.min_rr,
        sl_pct=args.sl,
        tp_pct=args.tp,
        broker=args.broker,
        name=args.name,
    )

    if args.start:
        config.start_date = datetime.strptime(args.start, "%Y-%m-%d")
    if args.end:
        config.end_date = datetime.strptime(args.end, "%Y-%m-%d")

    # Connexion MySQL
    engine = get_mysql_engine()
    MarketBase.metadata.create_all(bind=engine)
    session = get_mysql_session(engine)

    try:
        if args.grid:
            run_grid_search(config, session)
        else:
            bt = SMCBacktester(config)
            stats = bt.run(session)
            print_results(stats, config)

            if not args.no_save:
                run_id = bt.save_results(session, stats)
                print(f"  Resultats sauvegardes dans MySQL (run_id={run_id})")

            if args.export_csv:
                bt.export_trades_csv()
    finally:
        session.close()


if __name__ == "__main__":
    main()
