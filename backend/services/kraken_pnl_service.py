"""
Kraken PnL Service — Production-Grade V3.1
═══════════════════════════════════════════

WHY THIS EXISTS:
  - Kraken ClosedOrders only returns closed/cancelled orders — NOT live PnL
  - The /openpositions endpoint's "pnl" field is unreliable/absent
  - We must compute PnL ourselves from position data + market price

Architecture (strict separation of concerns):
  - FeeConfig: configurable fee model (default 0.04%)
  - KrakenPositionService: fetch open positions via /openpositions
  - KrakenPriceService: fetch mark prices via /tickers (spread-based adaptive cache)
  - KrakenPnLCalculator: pure math — PnL, fees, liquidation risk (no I/O)
  - KrakenRiskTracker: equity curve, peak equity, drawdown, daily PnL
  - KrakenPnLService: orchestrates everything, provides unified API

PnL Formulas (Kraken Futures — size in base asset):
  Long:   pnl = (mark_price - entry_price) * size
  Short:  pnl = (entry_price - mark_price) * size
  net_pnl = pnl + unrealized_funding - estimated_fees

NOTE: On Kraken Futures, leverage does NOT multiply PnL; it determines
margin requirements only.  The "size" field from /openpositions already
represents the full position size in base asset.  portfolioValue from
/accounts already includes unrealized PnL — we use balanceValue (pure
collateral) for equity calculations.

Endpoints used:
  GET /derivatives/api/v3/openpositions  (authenticated)
  GET /derivatives/api/v3/tickers        (public, no auth — mark prices)
  GET /derivatives/api/v3/accounts       (authenticated — balance for equity)
  GET /derivatives/api/v3/fills          (authenticated — realized PnL)
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Any, Dict, List, Optional, Protocol

from backend.kraken_futures_rest_v2 import (
    KrakenFuturesRESTClientV2,
    get_kraken_futures_client,
)

logger = logging.getLogger("smarttrade.kraken_pnl")

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

# Kraken Futures fee tiers — reference values
KRAKEN_TAKER_FEE_PCT = 0.05   # 0.05% taker fee
KRAKEN_MAKER_FEE_PCT = 0.02   # 0.02% maker fee (limit orders)

# Adaptive cache thresholds (spread-based volatility)
CACHE_TTL_NORMAL = 5.0                  # seconds — normal market
CACHE_TTL_VOLATILE = 1.0                # seconds — high volatility
SPREAD_VOLATILITY_THRESHOLD_PCT = 0.10  # bid-ask spread > 0.10% → volatile


# ══════════════════════════════════════════════════════════════════════════════
# FEE CONFIG
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FeeConfig:
    """Configurable fee model for PnL estimation.

    Attributes:
        default_pct: Fee percentage applied per leg (default 0.04%).
                     Total round-trip cost = 2 × default_pct.
        maker_pct:   Maker fee override (used only when explicitly requested).
        taker_pct:   Taker fee override.
    """
    default_pct: float = 0.04   # 0.04% per leg — user-configurable
    maker_pct: float = KRAKEN_MAKER_FEE_PCT
    taker_pct: float = KRAKEN_TAKER_FEE_PCT


# ══════════════════════════════════════════════════════════════════════════════
# PRICE SOURCE PROTOCOL (for multi-exchange extensibility)
# ══════════════════════════════════════════════════════════════════════════════

class PriceSource(Protocol):
    """Abstraction for price providers (REST, WebSocket, etc.)."""

    async def get_mark_price(self, symbol: str) -> float: ...

    async def get_mark_prices(self, symbols: List[str]) -> Dict[str, float]: ...

    async def get_ticker(self, symbol: str) -> Dict[str, Any]: ...


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PositionPnL:
    """Single position with computed PnL and risk indicators."""
    symbol: str
    side: str                       # "long" or "short"
    size: float                     # position size in base asset
    entry_price: float
    mark_price: float               # from /tickers markPrice
    liquidation_price: float
    notional: float                 # entry_price * size
    unrealized_pnl: float           # (mark - entry) * size
    unrealized_funding: float       # funding rate income/cost from API
    estimated_fees: float           # round-trip fee estimate
    net_pnl: float                  # unrealized_pnl + unrealized_funding - estimated_fees
    pnl_pct: float                  # % relative to notional
    liquidation_risk_pct: float     # distance to liquidation as % of mark
    max_leverage: Optional[float]   # maxFixedLeverage from API (isolated only)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "size": round(self.size, 6),
            "entry_price": round(self.entry_price, 2),
            "mark_price": round(self.mark_price, 2),
            "liquidation_price": round(self.liquidation_price, 2),
            "notional": round(self.notional, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "unrealized_funding": round(self.unrealized_funding, 4),
            "estimated_fees": round(self.estimated_fees, 4),
            "net_pnl": round(self.net_pnl, 4),
            "pnl_pct": round(self.pnl_pct, 4),
            "liquidation_risk_pct": round(self.liquidation_risk_pct, 2),
            "max_leverage": round(self.max_leverage, 1) if self.max_leverage else None,
        }


@dataclass
class RiskMetrics:
    """Account-level risk metrics."""
    balance: float              # from /accounts
    equity: float               # balance + total_unrealized_pnl
    peak_equity: float          # highest equity ever observed
    drawdown: float             # peak_equity - equity
    drawdown_pct: float         # drawdown / peak_equity * 100
    margin_used: float          # from /accounts
    margin_ratio_pct: float     # margin_used / equity * 100
    daily_pnl: float            # PnL since start-of-day
    timestamp: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "peak_equity": round(self.peak_equity, 2),
            "drawdown": round(self.drawdown, 2),
            "drawdown_pct": round(self.drawdown_pct, 4),
            "margin_used": round(self.margin_used, 2),
            "margin_ratio_pct": round(self.margin_ratio_pct, 4),
            "daily_pnl": round(self.daily_pnl, 4),
            "timestamp": self.timestamp,
        }


@dataclass
class PnLSummary:
    """Aggregated unrealized PnL across all positions + risk metrics."""
    total_unrealized_pnl: float
    total_estimated_fees: float
    total_net_pnl: float
    positions: List[PositionPnL]
    risk: Optional[RiskMetrics] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_unrealized_pnl": round(self.total_unrealized_pnl, 4),
            "total_estimated_fees": round(self.total_estimated_fees, 4),
            "total_net_pnl": round(self.total_net_pnl, 4),
            "position_count": len(self.positions),
            "positions": [p.to_dict() for p in self.positions],
            "risk": self.risk.to_dict() if self.risk else None,
            "timestamp": self.timestamp,
        }


@dataclass
class RealizedTrade:
    """A single realized trade from fills."""
    symbol: str
    side: str
    size: float
    price: float
    pnl: float
    fee: float
    net_pnl: float              # pnl - fee
    fill_time: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "size": round(self.size, 6),
            "price": round(self.price, 2),
            "pnl": round(self.pnl, 4),
            "fee": round(self.fee, 4),
            "net_pnl": round(self.net_pnl, 4),
            "fill_time": self.fill_time,
        }


@dataclass
class RealizedPnLSummary:
    """Aggregated realized PnL."""
    total_pnl: float
    total_fees: float
    net_pnl: float
    trade_count: int
    winning_trades: int
    losing_trades: int
    trades: List[RealizedTrade]

    @property
    def win_rate(self) -> float:
        if self.trade_count <= 0:
            return 0.0
        return self.winning_trades / self.trade_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_pnl": round(self.total_pnl, 4),
            "total_fees": round(self.total_fees, 4),
            "net_pnl": round(self.net_pnl, 4),
            "trade_count": self.trade_count,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(self.win_rate, 4),
            "trades": [t.to_dict() for t in self.trades],
        }


@dataclass
class FullPnLReport:
    """Unified output combining unrealized + realized + risk.

    Matches the requested JSON shape:
        {
            "unrealized": { "total": ..., "positions": [...] },
            "realized": float,
            "total_pnl": float,
            "risk": { "equity": ..., "peak_equity": ..., "drawdown": ... }
        }
    """
    unrealized_summary: PnLSummary
    realized_summary: RealizedPnLSummary
    risk: Optional[RiskMetrics]

    @property
    def total_pnl(self) -> float:
        return self.unrealized_summary.total_net_pnl + self.realized_summary.net_pnl

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unrealized": {
                "total": round(self.unrealized_summary.total_unrealized_pnl, 4),
                "total_fees": round(self.unrealized_summary.total_estimated_fees, 4),
                "net": round(self.unrealized_summary.total_net_pnl, 4),
                "position_count": len(self.unrealized_summary.positions),
                "positions": [p.to_dict() for p in self.unrealized_summary.positions],
            },
            "realized": round(self.realized_summary.net_pnl, 4),
            "realized_details": {
                "total_pnl": round(self.realized_summary.total_pnl, 4),
                "total_fees": round(self.realized_summary.total_fees, 4),
                "net_pnl": round(self.realized_summary.net_pnl, 4),
                "trade_count": self.realized_summary.trade_count,
                "winning_trades": self.realized_summary.winning_trades,
                "losing_trades": self.realized_summary.losing_trades,
                "win_rate": round(self.realized_summary.win_rate, 4),
            },
            "total_pnl": round(self.total_pnl, 4),
            "risk": self.risk.to_dict() if self.risk else None,
            "timestamp": time.time(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# POSITION SERVICE
# ══════════════════════════════════════════════════════════════════════════════

class KrakenPositionService:
    """Fetches open positions from Kraken Futures /openpositions endpoint."""

    def __init__(self, client: KrakenFuturesRESTClientV2) -> None:
        self._client = client

    async def get_open_positions(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Fetch all open positions as normalised dicts."""
        positions = await self._client.get_positions(force_refresh)
        return [
            {
                "symbol": p.symbol,
                "side": p.side,
                "size": p.size,
                "entry_price": p.entry_price,
                "unrealized_funding": getattr(p, 'unrealized_pnl', 0.0),
                "liquidation_price": p.liquidation_price,
                "max_leverage": p.leverage if p.leverage != 1.0 else None,
            }
            for p in positions
        ]


# ══════════════════════════════════════════════════════════════════════════════
# PRICE SERVICE (spread-based adaptive cache TTL)
# ══════════════════════════════════════════════════════════════════════════════

class KrakenPriceService:
    """
    Fetches mark prices from Kraken Futures /tickers endpoint.

    Volatility detection uses **bid-ask spread** as proxy:
      - spread_pct = (ask - bid) / midpoint * 100
      - If any PF_ symbol spread > SPREAD_VOLATILITY_THRESHOLD_PCT → 1s TTL
      - Otherwise → 5s TTL

    Supports injection of an optional WebSocket price source that
    overrides REST when available.
    """

    def __init__(
        self,
        client: KrakenFuturesRESTClientV2,
        ws_source: Optional[Any] = None,
    ) -> None:
        self._client = client
        self._ws_source = ws_source
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_time: float = 0
        self._current_ttl: float = CACHE_TTL_NORMAL
        self._volatile: bool = False

    def set_ws_source(self, ws_source: Any) -> None:
        """Hot-swap a WebSocket price source at runtime."""
        self._ws_source = ws_source
        logger.info("[PRICE] WebSocket source attached")

    async def get_mark_price(self, symbol: str) -> float:
        """Get mark price for a Kraken Futures symbol.

        Priority: WebSocket mark_price → REST mark_price → REST last.
        Raises ValueError if no valid price found.
        """
        # Try WS first
        if self._ws_source is not None:
            try:
                ws_price = await self._ws_source.get_mark_price(symbol)
                if ws_price and ws_price > 0:
                    return ws_price
            except Exception:
                pass  # fall through to REST

        ticker = await self._get_ticker(symbol)
        price = ticker.get("mark_price", 0)
        if price <= 0:
            price = ticker.get("last", 0)
        if price <= 0:
            raise ValueError(f"No valid mark price for {symbol}")
        return price

    async def get_mark_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Get mark prices for multiple symbols in one API call."""
        await self._refresh_cache()
        prices: Dict[str, float] = {}
        for sym in symbols:
            key = sym.upper()
            # Try WS first per symbol
            if self._ws_source is not None:
                try:
                    ws_price = await self._ws_source.get_mark_price(key)
                    if ws_price and ws_price > 0:
                        prices[sym] = ws_price
                        continue
                except Exception:
                    pass
            if key in self._cache:
                t = self._cache[key]
                price = t.get("mark_price", 0)
                if price <= 0:
                    price = t.get("last", 0)
                if price > 0:
                    prices[sym] = price
                else:
                    logger.warning("[PRICE] Invalid price for %s", sym)
            else:
                logger.warning("[PRICE] No ticker for %s", sym)
        return prices

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Public accessor for raw ticker dict."""
        return await self._get_ticker(symbol)

    @property
    def is_volatile(self) -> bool:
        return self._volatile

    @property
    def current_ttl(self) -> float:
        return self._current_ttl

    async def _get_ticker(self, symbol: str) -> Dict[str, Any]:
        await self._refresh_cache()
        key = symbol.upper()
        if key in self._cache:
            return self._cache[key]
        raise ValueError(f"Ticker not found for {symbol}")

    async def _refresh_cache(self) -> None:
        elapsed = time.time() - self._cache_time
        if elapsed < self._current_ttl:
            return

        self._cache = await self._client.get_tickers()
        self._cache_time = time.time()

        # Detect volatility via bid-ask spread
        self._volatile = False
        for sym, t in self._cache.items():
            if not sym.startswith("PF_"):
                continue
            bid = t.get("bid", 0)
            ask = t.get("ask", 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                spread_pct = (ask - bid) / mid * 100
                if spread_pct >= SPREAD_VOLATILITY_THRESHOLD_PCT:
                    self._volatile = True
                    break

        self._current_ttl = CACHE_TTL_VOLATILE if self._volatile else CACHE_TTL_NORMAL
        logger.debug(
            "[PRICE] Refreshed %d tickers | volatile=%s | ttl=%.1fs",
            len(self._cache), self._volatile, self._current_ttl,
        )


# ══════════════════════════════════════════════════════════════════════════════
# PNL CALCULATOR (pure math — no I/O)
# ══════════════════════════════════════════════════════════════════════════════

class KrakenPnLCalculator:
    """Pure computation of PnL. All methods are static. No API calls, no state."""

    @staticmethod
    def compute_unrealized_pnl(
        side: str,
        entry_price: float,
        current_price: float,
        size: float,
    ) -> float:
        """Compute unrealized PnL.

        On Kraken Futures, size is in base asset (e.g. BTC).
        Leverage does NOT multiply PnL — it only affects margin requirements.

        Long:   (mark - entry) * size
        Short:  (entry - mark) * size
        """
        if side.lower() in ("long", "buy"):
            return (current_price - entry_price) * size
        else:
            return (entry_price - current_price) * size

    @staticmethod
    def estimate_fees(
        entry_price: float,
        current_price: float,
        size: float,
        fee_pct: float = 0.04,
    ) -> float:
        """Estimate round-trip trading fees (entry + exit).

        fee = (entry_notional + exit_notional) * fee_pct / 100
        """
        entry_notional = entry_price * size
        exit_notional = current_price * size
        return (entry_notional + exit_notional) * fee_pct / 100

    @staticmethod
    def compute_pnl_pct(
        unrealized_pnl: float,
        entry_price: float,
        size: float,
    ) -> float:
        """PnL as percentage of notional value.

        notional = entry_price * size
        pnl_pct = unrealized_pnl / notional * 100
        """
        notional = entry_price * size
        if notional <= 0:
            return 0.0
        return (unrealized_pnl / notional) * 100

    @staticmethod
    def liquidation_risk_pct(
        mark_price: float,
        liquidation_price: float,
    ) -> float:
        """Distance to liquidation as percentage of mark price.

        Lower value = higher risk.  Returns 100.0 if liquidation_price
        is zero or invalid (no risk data).
        """
        if liquidation_price <= 0 or mark_price <= 0:
            return 100.0
        return abs(mark_price - liquidation_price) / mark_price * 100


# ══════════════════════════════════════════════════════════════════════════════
# RISK TRACKER (equity curve + drawdown + daily PnL)
# ══════════════════════════════════════════════════════════════════════════════

class KrakenRiskTracker:
    """Tracks equity curve, peak equity, drawdown, and daily PnL."""

    def __init__(self) -> None:
        self._peak_equity: float = 0.0
        self._equity_history: List[Dict[str, Any]] = []
        self._max_history: int = 1000
        # Daily PnL tracking
        self._day_start_equity: float = 0.0
        self._day_tag: Optional[str] = None  # e.g. "2026-03-31"

    def update(self, equity: float) -> None:
        """Record new equity observation and maintain daily baseline."""
        now = time.time()
        today = date.today().isoformat()

        # Reset daily baseline on new day
        if self._day_tag != today:
            self._day_start_equity = equity
            self._day_tag = today
            logger.debug("[RISK] New day %s — baseline equity=%.2f", today, equity)

        if equity > self._peak_equity:
            self._peak_equity = equity

        self._equity_history.append({
            "equity": equity,
            "peak": self._peak_equity,
            "ts": now,
        })
        if len(self._equity_history) > self._max_history:
            self._equity_history = self._equity_history[-self._max_history:]

    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    def drawdown(self, current_equity: float) -> float:
        """Absolute drawdown from peak."""
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, self._peak_equity - current_equity)

    def drawdown_pct(self, current_equity: float) -> float:
        """Drawdown as percentage of peak equity."""
        if self._peak_equity <= 0:
            return 0.0
        return self.drawdown(current_equity) / self._peak_equity * 100

    def daily_pnl(self, current_equity: float) -> float:
        """PnL since start of current day."""
        if self._day_start_equity <= 0:
            return 0.0
        return current_equity - self._day_start_equity

    def reset(self) -> None:
        """Full reset (e.g. on account change)."""
        self._peak_equity = 0.0
        self._equity_history.clear()
        self._day_start_equity = 0.0
        self._day_tag = None


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PNL SERVICE
# ══════════════════════════════════════════════════════════════════════════════

class KrakenPnLService:
    """
    Production-grade Kraken Futures PnL service.

    Orchestrates:
      1. Position fetch (openpositions)
      2. Mark price fetch (tickers — spread-based adaptive cache)
      3. Leveraged PnL + configurable fee estimation (pure math)
      4. Liquidation risk indicator per position
      5. Balance fetch (accounts — for equity/risk)
      6. Risk tracking (drawdown, peak equity, daily PnL)
      7. Unified report: unrealized + realized + risk in one call

    Usage:
        service = KrakenPnLService()
        report = await service.get_full_pnl()
        print(report.to_dict())
    """

    def __init__(
        self,
        client: Optional[KrakenFuturesRESTClientV2] = None,
        fee_config: Optional[FeeConfig] = None,
    ) -> None:
        self._client = client or get_kraken_futures_client()
        self._fee = fee_config or FeeConfig()
        self._positions = KrakenPositionService(self._client)
        self._prices = KrakenPriceService(self._client)
        self._calc = KrakenPnLCalculator()
        self._risk = KrakenRiskTracker()

    @property
    def price_service(self) -> KrakenPriceService:
        """Expose price service for WebSocket injection."""
        return self._prices

    @property
    def fee_config(self) -> FeeConfig:
        return self._fee

    @fee_config.setter
    def fee_config(self, config: FeeConfig) -> None:
        self._fee = config
        logger.info("[PNL] Fee config updated: %.4f%%", config.default_pct)

    # ──────────────────────────────────────────────────────────────
    #  UNREALIZED PNL
    # ──────────────────────────────────────────────────────────────

    async def get_unrealized_pnl(self, force_refresh: bool = False) -> PnLSummary:
        """Compute unrealized PnL for all open positions.

        Steps:
            1. Fetch open positions via /openpositions
            2. Fetch mark prices via /tickers (spread-based adaptive cache)
            3. Compute leveraged PnL + estimated fees + liquidation risk
            4. Fetch balance → compute equity & risk metrics
            5. Update drawdown/daily tracker
        """
        # 1. Positions
        raw_positions = await self._positions.get_open_positions(force_refresh)

        if not raw_positions:
            logger.info("[PNL] No open positions")
            risk = await self._build_risk_metrics(0.0)
            return PnLSummary(
                total_unrealized_pnl=0.0,
                total_estimated_fees=0.0,
                total_net_pnl=0.0,
                positions=[],
                risk=risk,
            )

        # 2. Mark prices (single API call)
        symbols = list({p["symbol"] for p in raw_positions})
        prices = await self._prices.get_mark_prices(symbols)

        # 3. PnL per position
        position_pnls: List[PositionPnL] = []
        total_pnl = 0.0
        total_fees = 0.0
        total_funding = 0.0
        fee_pct = self._fee.default_pct

        for pos in raw_positions:
            symbol = pos["symbol"]
            mark = prices.get(symbol, 0)

            if mark <= 0:
                logger.warning("[PNL] No mark price for %s, skipping", symbol)
                continue

            entry = pos["entry_price"]
            size = pos["size"]
            liq = pos.get("liquidation_price", 0.0)
            funding = pos.get("unrealized_funding", 0.0)
            max_lev = pos.get("max_leverage")

            pnl = self._calc.compute_unrealized_pnl(
                side=pos["side"], entry_price=entry,
                current_price=mark, size=size,
            )
            fees = self._calc.estimate_fees(
                entry_price=entry, current_price=mark,
                size=size, fee_pct=fee_pct,
            )
            pnl_pct = self._calc.compute_pnl_pct(
                unrealized_pnl=pnl, entry_price=entry,
                size=size,
            )
            liq_risk = self._calc.liquidation_risk_pct(mark, liq)

            net = pnl + funding - fees

            pos_pnl = PositionPnL(
                symbol=symbol,
                side=pos["side"],
                size=size,
                entry_price=entry,
                mark_price=mark,
                liquidation_price=liq,
                notional=entry * size,
                unrealized_pnl=pnl,
                unrealized_funding=funding,
                estimated_fees=fees,
                net_pnl=net,
                pnl_pct=pnl_pct,
                liquidation_risk_pct=liq_risk,
                max_leverage=max_lev,
            )
            position_pnls.append(pos_pnl)
            total_pnl += pnl
            total_fees += fees
            total_funding += funding

            logger.info(
                "[PNL] %s %s | entry=%.2f mark=%.2f | "
                "pnl=$%.4f funding=$%.4f fees=$%.4f net=$%.4f (%.2f%%) | liq_risk=%.1f%%",
                symbol, pos["side"], entry, mark,
                pnl, funding, fees, net, pnl_pct, liq_risk,
            )

        # 4. Risk metrics
        total_net = total_pnl + total_funding - total_fees
        risk = await self._build_risk_metrics(total_pnl)

        summary = PnLSummary(
            total_unrealized_pnl=total_pnl,
            total_estimated_fees=total_fees,
            total_net_pnl=total_net,
            positions=position_pnls,
            risk=risk,
        )

        logger.info(
            "[PNL] Summary: %d positions | pnl=$%.4f | fees=$%.4f | net=$%.4f",
            len(position_pnls), total_pnl, total_fees, total_net,
        )
        return summary

    # ──────────────────────────────────────────────────────────────
    #  REALIZED PNL
    # ──────────────────────────────────────────────────────────────

    async def get_realized_pnl(
        self,
        symbol: Optional[str] = None,
        since_ms: Optional[int] = None,
    ) -> RealizedPnLSummary:
        """Compute realized PnL from fills history.

        Each fill's net_pnl = fill.pnl - fill.fee (actual fees subtracted).
        """
        result = await self._client._request("GET", "/fills")
        fills = result.get("fills", [])

        trades: List[RealizedTrade] = []
        total_pnl = 0.0
        total_fees = 0.0
        wins = 0
        losses = 0

        for fill in fills:
            fill_sym = fill.get("symbol", "")

            if symbol and fill_sym.upper() != symbol.upper():
                continue

            if since_ms:
                fill_time_str = fill.get("fillTime", fill.get("time", ""))
                try:
                    dt = datetime.fromisoformat(
                        fill_time_str.replace("Z", "+00:00")
                    )
                    if int(dt.timestamp() * 1000) < since_ms:
                        continue
                except (ValueError, AttributeError):
                    pass

            pnl = float(fill.get("pnl", 0) or 0)
            fee = float(fill.get("fee", 0) or 0)
            net = pnl - fee

            trade = RealizedTrade(
                symbol=fill_sym,
                side=fill.get("side", ""),
                size=float(fill.get("size", 0) or 0),
                price=float(fill.get("price", 0) or 0),
                pnl=pnl,
                fee=fee,
                net_pnl=net,
                fill_time=fill.get("fillTime", fill.get("time", "")),
            )
            trades.append(trade)
            total_pnl += pnl
            total_fees += fee

            if net > 0:
                wins += 1
            elif net < 0:
                losses += 1

        net_total = total_pnl - total_fees

        summary = RealizedPnLSummary(
            total_pnl=total_pnl,
            total_fees=total_fees,
            net_pnl=net_total,
            trade_count=len(trades),
            winning_trades=wins,
            losing_trades=losses,
            trades=trades,
        )

        logger.info(
            "[PNL] Realized: %d fills | pnl=$%.4f | fees=$%.4f | net=$%.4f | W/L=%d/%d",
            summary.trade_count, total_pnl, total_fees, net_total, wins, losses,
        )
        return summary

    # ──────────────────────────────────────────────────────────────
    #  UNIFIED REPORT
    # ──────────────────────────────────────────────────────────────

    async def get_full_pnl(
        self,
        force_refresh: bool = False,
        realized_symbol: Optional[str] = None,
        realized_since_ms: Optional[int] = None,
    ) -> FullPnLReport:
        """Return a single unified PnL report (unrealized + realized + risk).

        This is the recommended endpoint for dashboards.
        """
        unrealized = await self.get_unrealized_pnl(force_refresh)
        realized = await self.get_realized_pnl(
            symbol=realized_symbol, since_ms=realized_since_ms,
        )

        report = FullPnLReport(
            unrealized_summary=unrealized,
            realized_summary=realized,
            risk=unrealized.risk,
        )

        logger.info(
            "[PNL] Full report: unrealized=$%.4f | realized=$%.4f | total=$%.4f",
            unrealized.total_net_pnl, realized.net_pnl, report.total_pnl,
        )
        return report

    # ──────────────────────────────────────────────────────────────
    #  RISK METRICS (internal)
    # ──────────────────────────────────────────────────────────────

    async def _build_risk_metrics(self, total_unrealized_pnl: float) -> Optional[RiskMetrics]:
        """Build risk metrics from account balance + unrealized PnL.

        IMPORTANT: Kraken's portfolioValue already includes unrealized PnL.
        We use balanceValue (pure collateral) for the balance field, and
        portfolioValue directly as equity to avoid double-counting.
        """
        try:
            balance_obj = await self._client.get_balance()

            # portfolioValue = balanceValue + unrealized PnL (from Kraken docs)
            # Use portfolioValue directly as equity — do NOT add unrealized PnL again
            equity = balance_obj.total  # portfolioValue IS equity
            bal = balance_obj.balance_value if balance_obj.balance_value > 0 else (equity - total_unrealized_pnl)
            margin = balance_obj.margin_used
            maint_margin = balance_obj.maintenance_margin

            self._risk.update(equity)

            dd = self._risk.drawdown(equity)
            dd_pct = self._risk.drawdown_pct(equity)
            margin_ratio = (margin / equity * 100) if equity > 0 else 0.0
            daily = self._risk.daily_pnl(equity)

            return RiskMetrics(
                balance=bal,
                equity=equity,
                peak_equity=self._risk.peak_equity,
                drawdown=dd,
                drawdown_pct=dd_pct,
                margin_used=margin,
                margin_ratio_pct=margin_ratio,
                daily_pnl=daily,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            logger.warning("[PNL] Risk metrics unavailable: %s", e)
            return None


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_pnl_service: Optional[KrakenPnLService] = None


def get_kraken_pnl_service(
    fee_config: Optional[FeeConfig] = None,
) -> KrakenPnLService:
    """Get or create singleton KrakenPnLService."""
    global _pnl_service
    if _pnl_service is None:
        _pnl_service = KrakenPnLService(fee_config=fee_config)
    elif fee_config is not None:
        _pnl_service.fee_config = fee_config
    return _pnl_service
