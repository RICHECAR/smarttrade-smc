"""
Realistic Backtest Engine — Zero Look-Ahead Bias
═════════════════════════════════════════════════
Signal generation strictly uses CLOSED bars only.
Execution happens at NEXT bar open + slippage/spread.

Key invariants:
  1. Signal at bar[i] uses candles[0..i-1] (all closed)
  2. Entry executes at bar[i+1].open + slippage
  3. SL/TP checked on each subsequent bar with realistic fills
  4. Fees deducted from PnL (entry + exit)
  5. Funding charged proportional to holding time
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable, Any, Tuple

from smc_futures.backtest.market_model import MarketModel, FillResult

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────
#  Configuration
# ───────────────────────────────────────────

@dataclass
class RealisticBacktestConfig:
    """Backtest configuration with realistic defaults."""
    # Capital
    initial_capital: float = 10_000.0
    risk_per_trade: float = 0.01      # 1% of capital per trade

    # Leverage & margin
    leverage: int = 10
    margin_usage_limit: float = 0.9   # Max 90% of capital as margin

    # Position limits
    max_open_positions: int = 3
    max_trades_per_day: int = 10
    cooldown_bars: int = 3            # Post-close cooldown

    # Market model
    seed: int = 42                    # Reproducible randomness

    # Timeframe in milliseconds (for cooldown calc)
    bar_duration_ms: int = 300_000    # 5 min default

    # Funding
    charge_funding: bool = True

    # ATR period for volatility classification
    atr_period: int = 14


# ───────────────────────────────────────────
#  Candle
# ───────────────────────────────────────────

@dataclass
class BTCandle:
    """Backtest candle (OHLCV)."""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# ───────────────────────────────────────────
#  Trade Record
# ───────────────────────────────────────────

@dataclass
class RealisticTrade:
    """Complete trade record with realistic execution details."""
    # Signal
    signal_bar_idx: int
    signal_price: float             # Close of the bar that generated the signal
    direction: str                  # "long" or "short"
    setup_type: str = ""
    confidence: float = 0.0

    # Entry
    entry_bar_idx: int = 0
    intended_entry: float = 0.0     # Signal's entry_price
    actual_entry: float = 0.0       # After slippage
    entry_slippage_bps: float = 0.0
    entry_fee: float = 0.0
    entry_timestamp: int = 0
    entry_latency_ms: float = 0.0

    # Stop Loss & Take Profit
    stop_loss: float = 0.0
    take_profit: float = 0.0

    # Exit
    exit_bar_idx: int = 0
    intended_exit: float = 0.0      # SL or TP level
    actual_exit: float = 0.0        # After slippage
    exit_slippage_bps: float = 0.0
    exit_fee: float = 0.0
    exit_timestamp: int = 0
    exit_reason: str = ""           # "sl", "tp", "timeout", "invalidation"

    # Size
    qty: float = 0.0
    notional_usd: float = 0.0

    # PnL
    raw_pnl: float = 0.0           # Before fees/funding
    total_fees: float = 0.0
    funding_cost: float = 0.0
    net_pnl: float = 0.0           # After everything
    net_pnl_pct: float = 0.0       # As % of capital at entry
    rr_achieved: float = 0.0

    # Risk metrics
    mae: float = 0.0               # Max adverse excursion (worst unrealized loss)
    mfe: float = 0.0               # Max favorable excursion (best unrealized profit)
    holding_bars: int = 0

    # Volatility context
    atr_at_entry: float = 0.0
    volatility_regime: str = "normal"

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0


# ───────────────────────────────────────────
#  Engine State
# ───────────────────────────────────────────

@dataclass
class EngineState:
    """Running state of the backtest engine."""
    capital: float = 0.0
    peak_capital: float = 0.0
    equity_curve: List[float] = field(default_factory=list)
    trades: List[RealisticTrade] = field(default_factory=list)
    open_positions: List[RealisticTrade] = field(default_factory=list)
    daily_trade_count: Dict[str, int] = field(default_factory=dict)
    cooldown_until: Dict[str, int] = field(default_factory=dict)
    bar_index: int = 0


# ───────────────────────────────────────────
#  Signal Protocol
# ───────────────────────────────────────────

@dataclass
class BacktestSignal:
    """
    Signal emitted by the strategy.
    Must be generated from CLOSED bars only.
    """
    symbol: str
    direction: str               # "long" or "short"
    entry_price: float           # Intended entry
    stop_loss: float
    take_profit: float
    setup_type: str = ""
    confidence: float = 0.5
    risk_reward: float = 1.0
    timestamp: int = 0


# ═══════════════════════════════════════════
#  REALISTIC BACKTEST ENGINE
# ═══════════════════════════════════════════

class RealisticBacktestEngine:
    """
    Backtest engine with zero look-ahead bias and realistic execution.

    Invariants:
    - Signal generation receives ONLY closed bars (bar[0..i-1] for bar i)
    - Entry at bar[i+1].open with market model slippage
    - SL/TP checks use bar HIGH/LOW with slippage on fill
    - Fees and funding deducted from PnL
    - Equity curve tracks mark-to-market after every bar
    """

    def __init__(
        self,
        config: RealisticBacktestConfig,
        market_models: Optional[Dict[str, MarketModel]] = None,
    ):
        self.config = config
        self._market_models = market_models or {}
        self._state = EngineState(
            capital=config.initial_capital,
            peak_capital=config.initial_capital,
        )

    def _get_market_model(self, symbol: str) -> MarketModel:
        if symbol not in self._market_models:
            self._market_models[symbol] = MarketModel.from_symbol(
                symbol, seed=self.config.seed
            )
        return self._market_models[symbol]

    # ── Utility: compute ATR ──────────────────

    @staticmethod
    def compute_atr(candles: List[BTCandle], period: int = 14) -> float:
        """True Range ATR on closed candles."""
        if len(candles) < 2:
            return candles[-1].high - candles[-1].low if candles else 0.0
        trs = []
        for i in range(1, len(candles)):
            c = candles[i]
            prev_close = candles[i - 1].close
            tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
            trs.append(tr)
        window = trs[-period:]
        return sum(window) / len(window) if window else 0.0

    @staticmethod
    def _day_key(timestamp_ms: int) -> str:
        """Convert timestamp to YYYY-MM-DD for daily trade counting."""
        import datetime
        dt = datetime.datetime.fromtimestamp(timestamp_ms / 1000, tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%d")

    # ── Main: run backtest ────────────────────

    def run(
        self,
        symbol: str,
        candles: List[BTCandle],
        signal_fn: Callable[[List[BTCandle]], Optional[BacktestSignal]],
        max_holding_bars: int = 0,
    ) -> EngineState:
        """
        Run backtest on a single symbol.

        Args:
            symbol:  Trading pair (e.g. "BTCUSDT")
            candles: Full OHLCV history, chronologically sorted
            signal_fn: Strategy function. Receives ONLY closed bars [0..i-1].
                       Returns BacktestSignal or None.
            max_holding_bars: Force close after N bars (0 = unlimited)

        Returns:
            EngineState with trades, equity curve, final capital.
        """
        state = self._state
        model = self._get_market_model(symbol)
        pending_signal: Optional[BacktestSignal] = None
        atr_history: List[float] = []

        if len(candles) < 3:
            logger.warning(f"[BT] {symbol}: Not enough candles ({len(candles)})")
            return state

        for i in range(1, len(candles)):
            bar = candles[i]
            closed_bars = candles[:i]  # STRICTLY closed bars — no look-ahead
            state.bar_index = i

            # ── 1. Compute ATR on closed bars ────────
            atr = self.compute_atr(closed_bars, self.config.atr_period)
            atr_history.append(atr)
            vol_regime = MarketModel.classify_volatility(
                atr, closed_bars[-1].close, atr_history
            )

            # ── 2. Update open positions (MAE/MFE) ──
            self._update_open_positions(bar, atr, model, vol_regime, state)

            # ── 3. Check SL/TP exits ─────────────────
            self._check_exits(bar, atr, model, vol_regime, state)

            # ── 4. Check timeout exits ───────────────
            if max_holding_bars > 0:
                self._check_timeout_exits(bar, i, max_holding_bars, atr, model, vol_regime, state)

            # ── 5. Execute pending signal at this bar's OPEN ──
            if pending_signal is not None:
                self._execute_entry(
                    pending_signal, bar, i, atr, model, vol_regime, state
                )
                pending_signal = None

            # ── 6. Generate NEW signal from closed bars ──
            #       (no position on this symbol yet)
            has_position = any(
                t.direction and t.exit_reason == ""
                for t in state.open_positions
            )
            if not has_position:
                day_key = self._day_key(bar.timestamp)
                daily_count = state.daily_trade_count.get(day_key, 0)
                cooldown_ts = state.cooldown_until.get(symbol, 0)

                if (daily_count < self.config.max_trades_per_day
                        and bar.timestamp >= cooldown_ts
                        and len(state.open_positions) < self.config.max_open_positions):
                    sig = signal_fn(closed_bars)
                    if sig is not None:
                        pending_signal = sig  # Will execute at NEXT bar open

            # ── 7. Update equity curve ───────────────
            mark_to_market = state.capital
            for pos in state.open_positions:
                if pos.direction == "long":
                    unrealized = (bar.close - pos.actual_entry) * pos.qty
                else:
                    unrealized = (pos.actual_entry - bar.close) * pos.qty
                mark_to_market += unrealized
            state.equity_curve.append(mark_to_market)
            state.peak_capital = max(state.peak_capital, mark_to_market)

            # ── 8. Charge funding every 8 hours ──────
            if self.config.charge_funding:
                self._charge_funding(bar, state, model)

        # Force close any remaining positions at last bar
        if state.open_positions:
            last_bar = candles[-1]
            for pos in list(state.open_positions):
                self._force_close(
                    pos, last_bar.close, last_bar.timestamp, len(candles) - 1,
                    "end_of_data", atr, model, vol_regime, state
                )

        return state

    # ── Entry execution ───────────────────────

    def _execute_entry(
        self,
        signal: BacktestSignal,
        bar: BTCandle,
        bar_idx: int,
        atr: float,
        model: MarketModel,
        vol_regime: str,
        state: EngineState,
    ):
        """Execute entry at bar's OPEN with market model slippage."""
        entry_price = bar.open  # Execute at the bar OPEN, not signal's intended price
        volume_5m = bar.volume * entry_price if bar.volume else 0

        # Normalize ATR to execution timeframe (~5m) for slippage calculation.
        # A 4H bar has ~48x the range of a 5m bar due to random walk scaling,
        # but order-book depth at execution doesn't scale with chart timeframe.
        bar_ratio = self.config.bar_duration_ms / 300_000  # 4H=48, 1D=288
        exec_atr = atr / math.sqrt(bar_ratio) if bar_ratio > 1 else atr

        fill = model.simulate_fill(
            side="buy" if signal.direction == "long" else "sell",
            price=entry_price,
            qty=0,  # Will size after checking
            atr=exec_atr,
            volume_5m=volume_5m,
            volatility_regime=vol_regime,
        )

        if fill.rejected:
            logger.debug(f"[BT] Entry REJECTED: {fill.reject_reason}")
            return

        # Position sizing: risk-based
        actual_entry = fill.fill_price
        stop_distance = abs(actual_entry - signal.stop_loss)
        if stop_distance <= 0:
            logger.debug("[BT] Entry REJECTED: SL == entry")
            return

        risk_amount = state.capital * self.config.risk_per_trade
        qty = risk_amount / stop_distance
        notional = qty * actual_entry

        # Margin check
        margin_required = notional / self.config.leverage
        if margin_required > state.capital * self.config.margin_usage_limit:
            qty = (state.capital * self.config.margin_usage_limit * self.config.leverage) / actual_entry
            notional = qty * actual_entry

        if qty <= 0 or notional < 1.0:
            return

        # Re-simulate with actual qty for fees
        fill = model.simulate_fill(
            side="buy" if signal.direction == "long" else "sell",
            price=entry_price,
            qty=qty,
            atr=exec_atr,
            volume_5m=volume_5m,
            volatility_regime=vol_regime,
        )
        if fill.rejected:
            return

        actual_entry = fill.fill_price
        fee_usd = notional * fill.fee_rate

        # Partial fill
        qty *= fill.partial_fill_ratio
        notional = qty * actual_entry

        # Create trade record
        trade = RealisticTrade(
            signal_bar_idx=bar_idx - 1,
            signal_price=signal.entry_price,
            direction=signal.direction,
            setup_type=signal.setup_type,
            confidence=signal.confidence,
            entry_bar_idx=bar_idx,
            intended_entry=signal.entry_price,
            actual_entry=actual_entry,
            entry_slippage_bps=fill.slippage_bps,
            entry_fee=fee_usd,
            entry_timestamp=bar.timestamp,
            entry_latency_ms=fill.latency_ms,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            qty=qty,
            notional_usd=notional,
            atr_at_entry=atr,
            volatility_regime=vol_regime,
        )

        state.open_positions.append(trade)
        day_key = self._day_key(bar.timestamp)
        state.daily_trade_count[day_key] = state.daily_trade_count.get(day_key, 0) + 1

        logger.debug(
            f"[BT] ENTRY {signal.direction} @ {actual_entry:.2f} "
            f"(intended {signal.entry_price:.2f}, slip={fill.slippage_bps:.1f}bps)"
        )

    # ── Update open positions (MAE/MFE) ──────

    def _update_open_positions(
        self, bar: BTCandle, atr: float, model: MarketModel,
        vol_regime: str, state: EngineState,
    ):
        """Track MAE/MFE for each open position."""
        for pos in state.open_positions:
            if pos.direction == "long":
                unrealized_best = bar.high - pos.actual_entry
                unrealized_worst = bar.low - pos.actual_entry
                pos.mfe = max(pos.mfe, unrealized_best * pos.qty)
                pos.mae = min(pos.mae, unrealized_worst * pos.qty)
            else:
                unrealized_best = pos.actual_entry - bar.low
                unrealized_worst = pos.actual_entry - bar.high
                pos.mfe = max(pos.mfe, unrealized_best * pos.qty)
                pos.mae = min(pos.mae, unrealized_worst * pos.qty)

    # ── SL/TP exits ──────────────────────────

    def _check_exits(
        self, bar: BTCandle, atr: float, model: MarketModel,
        vol_regime: str, state: EngineState,
    ):
        """Check SL/TP with realistic fill simulation."""
        for pos in list(state.open_positions):
            hit_sl = False
            hit_tp = False

            if pos.direction == "long":
                hit_sl = bar.low <= pos.stop_loss
                hit_tp = bar.high >= pos.take_profit
            else:
                hit_sl = bar.high >= pos.stop_loss
                hit_tp = bar.low <= pos.take_profit

            # If BOTH hit in same bar, determine by proximity to open
            if hit_sl and hit_tp:
                if pos.direction == "long":
                    sl_dist = abs(bar.open - pos.stop_loss)
                    tp_dist = abs(bar.open - pos.take_profit)
                else:
                    sl_dist = abs(bar.open - pos.stop_loss)
                    tp_dist = abs(bar.open - pos.take_profit)
                # Pessimistic: SL hit first if equidistant
                if sl_dist <= tp_dist:
                    hit_tp = False
                else:
                    hit_sl = False

            bar_idx = state.bar_index
            volume_5m = bar.volume * bar.close if bar.volume else 0

            # Normalize ATR to execution timeframe for slippage
            bar_ratio = self.config.bar_duration_ms / 300_000
            exec_atr = atr / math.sqrt(bar_ratio) if bar_ratio > 1 else atr

            if hit_sl:
                fill = model.simulate_sl_fill(
                    side=pos.direction,
                    sl_price=pos.stop_loss,
                    qty=pos.qty,
                    atr=exec_atr,
                    volume_5m=volume_5m,
                    volatility_regime=vol_regime,
                )
                self._close_trade(pos, fill.fill_price, bar.timestamp, bar_idx,
                                  "sl", fill, state)
            elif hit_tp:
                fill = model.simulate_tp_fill(
                    side=pos.direction,
                    tp_price=pos.take_profit,
                    qty=pos.qty,
                    atr=exec_atr,
                    volume_5m=volume_5m,
                    volatility_regime=vol_regime,
                )
                self._close_trade(pos, fill.fill_price, bar.timestamp, bar_idx,
                                  "tp", fill, state)

    # ── Timeout exits ────────────────────────

    def _check_timeout_exits(
        self, bar: BTCandle, bar_idx: int, max_bars: int,
        atr: float, model: MarketModel, vol_regime: str, state: EngineState,
    ):
        """Force close positions held too long."""
        for pos in list(state.open_positions):
            bars_held = bar_idx - pos.entry_bar_idx
            if bars_held >= max_bars:
                self._force_close(
                    pos, bar.close, bar.timestamp, bar_idx,
                    "timeout", atr, model, vol_regime, state
                )

    def _force_close(
        self, pos: RealisticTrade, price: float, timestamp: int,
        bar_idx: int, reason: str, atr: float, model: MarketModel,
        vol_regime: str, state: EngineState,
    ):
        """Force close at market price."""
        # Normalize ATR to execution timeframe for slippage
        bar_ratio = self.config.bar_duration_ms / 300_000
        exec_atr = atr / math.sqrt(bar_ratio) if bar_ratio > 1 else atr
        fill = model.simulate_fill(
            side="sell" if pos.direction == "long" else "buy",
            price=price,
            qty=pos.qty,
            atr=exec_atr,
            volatility_regime=vol_regime,
        )
        self._close_trade(pos, fill.fill_price, timestamp, bar_idx, reason, fill, state)

    # ── Close trade ──────────────────────────

    def _close_trade(
        self, trade: RealisticTrade, exit_price: float,
        timestamp: int, bar_idx: int, reason: str,
        fill: FillResult, state: EngineState,
    ):
        """Close a trade and compute net PnL."""
        trade.intended_exit = trade.stop_loss if reason == "sl" else trade.take_profit
        trade.actual_exit = exit_price
        trade.exit_slippage_bps = fill.slippage_bps
        trade.exit_fee = trade.notional_usd * fill.fee_rate
        trade.exit_timestamp = timestamp
        trade.exit_bar_idx = bar_idx
        trade.exit_reason = reason
        trade.holding_bars = bar_idx - trade.entry_bar_idx

        # PnL
        if trade.direction == "long":
            trade.raw_pnl = (exit_price - trade.actual_entry) * trade.qty
        else:
            trade.raw_pnl = (trade.actual_entry - exit_price) * trade.qty

        trade.total_fees = trade.entry_fee + trade.exit_fee
        trade.net_pnl = trade.raw_pnl - trade.total_fees - trade.funding_cost
        trade.net_pnl_pct = (trade.net_pnl / state.capital * 100) if state.capital > 0 else 0

        # RR achieved
        risk = abs(trade.actual_entry - trade.stop_loss)
        if risk > 0:
            if trade.direction == "long":
                trade.rr_achieved = (exit_price - trade.actual_entry) / risk
            else:
                trade.rr_achieved = (trade.actual_entry - exit_price) / risk

        # Update capital
        state.capital += trade.net_pnl

        # Move from open to closed
        if trade in state.open_positions:
            state.open_positions.remove(trade)
        state.trades.append(trade)

        # Cooldown
        state.cooldown_until["_last"] = (
            timestamp + self.config.cooldown_bars * self.config.bar_duration_ms
        )

        logger.debug(
            f"[BT] EXIT {reason} @ {exit_price:.2f} "
            f"(PnL: ${trade.net_pnl:.2f}, fees: ${trade.total_fees:.2f})"
        )

    # ── Funding charges ──────────────────────

    def _charge_funding(self, bar: BTCandle, state: EngineState, model: MarketModel):
        """Charge funding proportional to bar duration for open positions."""
        bar_hours = self.config.bar_duration_ms / 3_600_000
        for pos in state.open_positions:
            cost = model.compute_funding_cost(pos.notional_usd, bar_hours)
            pos.funding_cost += cost
            # Don't deduct from capital yet — deducted at close

    # ── Results access ───────────────────────

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def trades(self) -> List[RealisticTrade]:
        return self._state.trades

    @property
    def final_capital(self) -> float:
        return self._state.capital

    @property
    def equity_curve(self) -> List[float]:
        return self._state.equity_curve
