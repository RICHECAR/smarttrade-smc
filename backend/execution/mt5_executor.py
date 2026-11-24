"""
MetaTrader 5 (MT5) Executor — Async-Safe Multi-Broker Adapter
═════════════════════════════════════════════════════════════

Connects SmartTrade to a local MetaTrader 5 terminal (Windows) using the
official `MetaTrader5` Python package.

Features:
  - Thread-safe async wrapper (`asyncio.to_thread` + `threading.RLock`)
  - Automatic symbol mapping (BTCUSDT / PF_XBTUSD -> BTCUSD, plus native MT5 symbols)
  - Exact contract-size to lot conversion (`volume_step`, `volume_min`, `volume_max`)
  - Automatic filling mode selection (IOC / FOK / RETURN from `symbol_info.filling_mode`)
  - Native MT5 SL / TP attachment with digit rounding
  - Fail-closed Demo/Live safety guard (`MT5_ALLOW_LIVE=false` blocks real accounts)
  - Safe position closing via `position=ticket` (never accidentally flips)
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("smarttrade.mt5_executor")

# Canonical crypto/futures & Forex aliases -> standard MT5 symbol names
_SYMBOL_ALIASES: Dict[str, str] = {
    "BTCUSDT": "BTCUSD",
    "BTCUSD": "BTCUSD",
    "XBTUSD": "BTCUSD",
    "PI_XBTUSD": "BTCUSD",
    "PF_XBTUSD": "BTCUSD",
    "ETHUSDT": "ETHUSD",
    "ETHUSD": "ETHUSD",
    "PI_ETHUSD": "ETHUSD",
    "PF_ETHUSD": "ETHUSD",
    "SOLUSDT": "SOLUSD",
    "SOLUSD": "SOLUSD",
    "PI_SOLUSD": "SOLUSD",
    "PF_SOLUSD": "SOLUSD",
    "XRPUSDT": "XRPUSD",
    "XRPUSD": "XRPUSD",
    "PI_XRPUSD": "XRPUSD",
    "PF_XRPUSD": "XRPUSD",
    # Major Forex & Metals (PUPrime-Demo tradable suffix .s)
    "EURUSD": "EURUSD.s",
    "GBPUSD": "GBPUSD.s",
    "USDJPY": "USDJPY.s",
    "XAUUSD": "XAUUSD.s",
}

_FOREX_CURRENCIES = {"EUR", "USD", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "XAU", "XAG"}


def is_forex_or_metal_symbol(symbol: str) -> bool:
    """Return True if symbol is a Forex pair or Precious Metal (subject to Mon-Fri market hours)."""
    clean = str(symbol or "").strip().upper().split(".")[0].replace("/", "").replace("_", "")
    if len(clean) == 6 and clean[:3] in _FOREX_CURRENCIES and clean[3:] in _FOREX_CURRENCIES:
        return True
    return False


def is_mt5_forex_market_open(now_utc: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Business-day market hours guard for MT5 Forex & Metals (UTC).

    - Open: Monday 00:05 UTC -> Friday (MT5_FOREX_FRIDAY_CLOSE_UTC_HOUR:MIN, default 20:30 UTC).
    - Closed: Friday >= 20:30 UTC, all Saturday, all Sunday, Monday < 00:05 UTC.
    - Daily rollover window (20:55 -> 22:05 UTC Mon-Thu): closed when MT5_FOREX_CLOSE_DAILY_ROLLOVER=true.
    """
    dt = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    wd = dt.weekday()  # 0=Mon .. 4=Fri, 5=Sat, 6=Sun
    hhmm = dt.hour * 60 + dt.minute

    fri_close_h = int(os.getenv("MT5_FOREX_FRIDAY_CLOSE_UTC_HOUR", "20"))
    fri_close_m = int(os.getenv("MT5_FOREX_FRIDAY_CLOSE_UTC_MIN", "30"))
    fri_close_min = fri_close_h * 60 + fri_close_m

    # Weekend: Saturday or Sunday
    if wd in (5, 6):
        return False, f"WEEKEND_CLOSED ({dt.strftime('%A %H:%M UTC')})"

    # Monday before 00:05 UTC (wait for spread normalization after open)
    if wd == 0 and hhmm < 5:
        return False, f"MONDAY_PRE_OPEN ({dt.strftime('%H:%M UTC')})"

    # Friday pre-close cutoff (default 20:30 UTC = 30 min before 21:00 UTC market close)
    if wd == 4 and hhmm >= fri_close_min:
        return False, f"FRIDAY_PRE_CLOSE_CUTOFF ({dt.strftime('%H:%M UTC')} >= {fri_close_h:02d}:{fri_close_m:02d} UTC)"

    # Daily Forex rollover closure window (20:55 -> 22:05 UTC Mon-Thu)
    close_rollover = _is_truthy(os.getenv("MT5_FOREX_CLOSE_DAILY_ROLLOVER"), default=True)
    if close_rollover and (20 * 60 + 55) <= hhmm <= (22 * 60 + 5):
        return False, f"DAILY_ROLLOVER_CLOSURE ({dt.strftime('%H:%M UTC')})"

    return True, "OPEN"


def _import_mt5():
    """Import MetaTrader5 lazily so non-MT5 environments can still import this module."""
    try:
        import MetaTrader5 as mt5  # type: ignore
        return mt5
    except ImportError as exc:
        raise RuntimeError(
            "Le package Python 'MetaTrader5' n'est pas installé. "
            "Installez-le avec: pip install MetaTrader5"
        ) from exc


def _is_truthy(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def is_mt5_enabled() -> bool:
    """Return True if MT5 broker integration is enabled via MT5_ENABLED."""
    return _is_truthy(os.getenv("MT5_ENABLED"), default=False)


def is_mt5_execution_enabled() -> bool:
    """Return True if order execution on MT5 is enabled via MT5_EXECUTION_ENABLED."""
    return _is_truthy(os.getenv("MT5_EXECUTION_ENABLED"), default=False)


class MT5Executor:
    """
    Async-safe MetaTrader 5 executor compatible with SmartTrade's
    MultiBrokerEngine and BrokerRouter interfaces.
    """

    def __init__(
        self,
        login: Optional[int] = None,
        password: Optional[str] = None,
        server: Optional[str] = None,
        terminal_path: Optional[str] = None,
        magic_number: Optional[int] = None,
        deviation: Optional[int] = None,
        symbol_suffix: Optional[str] = None,
        allow_live: Optional[bool] = None,
        max_open_per_symbol: Optional[int] = None,
        max_lot_size: Optional[float] = None,
        timeout_ms: int = 30000,
    ) -> None:
        raw_login = login if login is not None else os.getenv("MT5_LOGIN", "")
        self.login: Optional[int] = int(str(raw_login).strip()) if str(raw_login).strip() else None
        self.password: str = password if password is not None else os.getenv("MT5_PASSWORD", "")
        self.server: str = (server if server is not None else os.getenv("MT5_SERVER", "")).strip()
        self.terminal_path: str = (
            terminal_path
            if terminal_path is not None
            else os.getenv("MT5_TERMINAL_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")
        ).strip()
        self.magic_number: int = (
            int(magic_number)
            if magic_number is not None
            else int(os.getenv("MT5_MAGIC_NUMBER", "20261122"))
        )
        self.deviation: int = (
            int(deviation)
            if deviation is not None
            else int(os.getenv("MT5_DEVIATION_POINTS", "30"))
        )
        self.symbol_suffix: str = (
            symbol_suffix
            if symbol_suffix is not None
            else os.getenv("MT5_SYMBOL_SUFFIX", "")
        ).strip()
        self.allow_live: bool = (
            bool(allow_live)
            if allow_live is not None
            else _is_truthy(os.getenv("MT5_ALLOW_LIVE"), default=False)
        )
        self.max_open_per_symbol: int = max(
            1,
            int(max_open_per_symbol if max_open_per_symbol is not None else os.getenv("MT5_MAX_OPEN_PER_SYMBOL", "1")),
        )
        self.max_lot_size: float = float(
            max_lot_size if max_lot_size is not None else os.getenv("MT5_MAX_LOT_SIZE", "0.10")
        )
        self.timeout_ms: int = int(timeout_ms)

        self._lock = threading.RLock()
        self._connected: bool = False
        self._account_trade_mode: Optional[int] = None
        self._last_error: Optional[str] = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    # ── Symbol & Timeframe Helpers ──────────────────────────────────────────

    def normalize_symbol(self, symbol: str) -> str:
        """Map SmartTrade/Bitget/Kraken symbols to MT5 broker symbol names."""
        raw = str(symbol or "").strip()
        if not raw:
            return ""
        upper = raw.upper()
        mapped = _SYMBOL_ALIASES.get(upper, raw)
        if self.symbol_suffix and not mapped.endswith(self.symbol_suffix):
            return f"{mapped}{self.symbol_suffix}"
        return mapped

    def _resolve_mt5_symbol_sync(self, mt5: Any, symbol: str) -> Tuple[str, Any]:
        """Resolve and select a tradable symbol in MT5 Market Watch (prefers trade_mode != 0)."""
        candidates: List[str] = []
        norm = self.normalize_symbol(symbol)
        if norm:
            candidates.append(norm)
        raw = str(symbol or "").strip()
        if raw and raw not in candidates:
            candidates.append(raw)
        if norm and not norm.endswith(".s"):
            candidates.append(f"{norm}.s")

        fallback: Tuple[str, Any] = (norm, None)
        for cand in candidates:
            info = mt5.symbol_info(cand)
            if info is not None:
                if not info.visible:
                    mt5.symbol_select(cand, True)
                    info = mt5.symbol_info(cand)
                if info is not None and int(getattr(info, "trade_mode", 4)) != 0:
                    return cand, info
                if fallback[1] is None:
                    fallback = (cand, info)
        return fallback

    @staticmethod
    def _map_timeframe(mt5: Any, timeframe: str) -> Any:
        tf = str(timeframe or "15m").strip().lower()
        mapping = {
            "1m": mt5.TIMEFRAME_M1,
            "3m": mt5.TIMEFRAME_M3,
            "5m": mt5.TIMEFRAME_M5,
            "15m": mt5.TIMEFRAME_M15,
            "30m": mt5.TIMEFRAME_M30,
            "1h": mt5.TIMEFRAME_H1,
            "2h": mt5.TIMEFRAME_H2,
            "4h": mt5.TIMEFRAME_H4,
            "6h": mt5.TIMEFRAME_H6,
            "12h": mt5.TIMEFRAME_H12,
            "1d": mt5.TIMEFRAME_D1,
            "1w": mt5.TIMEFRAME_W1,
        }
        return mapping.get(tf, mt5.TIMEFRAME_M15)

    @staticmethod
    def _select_filling_mode(mt5: Any, symbol_info: Any) -> int:
        """Pick an order filling mode supported by the MT5 symbol."""
        fm = int(getattr(symbol_info, "filling_mode", 0) or 0)
        # Bitmask: 1 = SYMBOL_FILLING_FOK, 2 = SYMBOL_FILLING_IOC
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    @staticmethod
    def _round_volume(lots: float, vol_min: float, vol_max: float, vol_step: float) -> float:
        """Floor lot size to exact broker volume_step without exceeding vol_max."""
        if not math.isfinite(lots) or lots <= 0 or vol_step <= 0:
            return 0.0
        capped = min(lots, vol_max)
        steps = math.floor((capped + 1e-12) / vol_step)
        rounded = round(steps * vol_step, 8)
        if rounded < vol_min:
            return 0.0
        return rounded

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def _connect_sync(self) -> bool:
        mt5 = _import_mt5()
        with self._lock:
            init_kwargs: Dict[str, Any] = {"timeout": self.timeout_ms}
            if self.terminal_path and os.path.exists(self.terminal_path):
                init_kwargs["path"] = self.terminal_path
            if self.login is not None:
                init_kwargs["login"] = self.login
            if self.password:
                init_kwargs["password"] = self.password
            if self.server:
                init_kwargs["server"] = self.server

            ok = mt5.initialize(**init_kwargs)
            if not ok:
                err = mt5.last_error()
                self._connected = False
                self._last_error = f"mt5.initialize failed: {err}"
                logger.error("[MT5] Connection failed: %s", self._last_error)
                return False

            acc = mt5.account_info()
            if acc is None:
                err = mt5.last_error()
                self._connected = False
                self._last_error = f"mt5.account_info returned None: {err}"
                logger.error("[MT5] %s", self._last_error)
                return False

            self._account_trade_mode = int(getattr(acc, "trade_mode", 0))
            self._connected = True
            self._last_error = None
            mode_label = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(self._account_trade_mode, "UNKNOWN")
            logger.info(
                "[MT5] Connected | login=%s | server=%s | mode=%s | balance=%.2f %s | leverage=1:%s",
                acc.login,
                acc.server,
                mode_label,
                acc.balance,
                acc.currency,
                acc.leverage,
            )
            return True

    async def connect(self) -> bool:
        """Initialize and authenticate the MT5 terminal session."""
        return await asyncio.to_thread(self._connect_sync)

    def _disconnect_sync(self) -> None:
        with self._lock:
            try:
                mt5 = _import_mt5()
                mt5.shutdown()
            except Exception as exc:
                logger.warning("[MT5] Shutdown warning: %s", exc)
            finally:
                self._connected = False

    async def disconnect(self) -> None:
        """Cleanly shut down the MT5 terminal Python bridge."""
        await asyncio.to_thread(self._disconnect_sync)
        logger.info("[MT5] Disconnected")

    async def _ensure_connected(self) -> bool:
        if self._connected:
            return True
        return await self.connect()

    # ── Account & Market Data ───────────────────────────────────────────────

    def _get_account_info_sync(self) -> Dict[str, Any]:
        mt5 = _import_mt5()
        with self._lock:
            if not self._connected and not self._connect_sync():
                raise RuntimeError(self._last_error or "MT5 not connected")
            acc = mt5.account_info()
            ti = mt5.terminal_info()
            if acc is None:
                raise RuntimeError(f"MT5 account_info unavailable: {mt5.last_error()}")
            trade_mode = int(getattr(acc, "trade_mode", 0))
            return {
                "broker": "mt5",
                "login": acc.login,
                "server": acc.server,
                "trade_mode": {0: "demo", 1: "contest", 2: "real"}.get(trade_mode, "unknown"),
                "currency": acc.currency,
                "available": float(acc.margin_free),
                "total": float(acc.equity),
                "balance": float(acc.balance),
                "equity": float(acc.equity),
                "margin_used": float(acc.margin),
                "unrealizedPL": float(acc.profit),
                "unrealized_pnl": float(acc.profit),
                "leverage": int(acc.leverage),
                "trade_allowed": bool(acc.trade_allowed and (ti.trade_allowed if ti else True)),
                "connected": bool(ti.connected if ti else True),
            }

    async def get_account_info(self) -> Dict[str, Any]:
        """Return unified account balance & status dict."""
        return await asyncio.to_thread(self._get_account_info_sync)

    async def get_balance(self) -> Dict[str, Any]:
        """Alias for get_account_info()."""
        return await self.get_account_info()

    def _get_positions_sync(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        mt5 = _import_mt5()
        with self._lock:
            if not self._connected and not self._connect_sync():
                return []
            if symbol:
                mt5_sym, _ = self._resolve_mt5_symbol_sync(mt5, symbol)
                raw = mt5.positions_get(symbol=mt5_sym)
            else:
                raw = mt5.positions_get()
            if not raw:
                return []
            positions: List[Dict[str, Any]] = []
            for p in raw:
                side = "long" if p.type == mt5.POSITION_TYPE_BUY else "short"
                positions.append(
                    {
                        "ticket": int(p.ticket),
                        "order_id": str(p.ticket),
                        "symbol": p.symbol,
                        "side": side,
                        "size": float(p.volume),
                        "volume": float(p.volume),
                        "entry_price": float(p.price_open),
                        "mark_price": float(p.price_current),
                        "stop_loss": float(p.sl) if p.sl else None,
                        "take_profit": float(p.tp) if p.tp else None,
                        "unrealized_pnl": float(p.profit),
                        "swap": float(p.swap),
                        "magic": int(p.magic),
                        "comment": str(p.comment or ""),
                        "broker": "mt5",
                    }
                )
            return positions

    async def get_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return currently open MT5 positions."""
        return await asyncio.to_thread(self._get_positions_sync, symbol)

    def _get_ticker_sync(self, symbol: str) -> Optional[Dict[str, Any]]:
        mt5 = _import_mt5()
        with self._lock:
            if not self._connected and not self._connect_sync():
                return None
            mt5_sym, info = self._resolve_mt5_symbol_sync(mt5, symbol)
            if info is None:
                return None
            tick = mt5.symbol_info_tick(mt5_sym)
            if tick is None:
                return None
            return {
                "symbol": mt5_sym,
                "bid": float(tick.bid),
                "ask": float(tick.ask),
                "last": float(tick.last or tick.bid or tick.ask),
                "spread": round(float(tick.ask - tick.bid), int(info.digits)),
                "timestamp": int(tick.time_msc),
            }

    async def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return latest bid/ask tick for symbol."""
        return await asyncio.to_thread(self._get_ticker_sync, symbol)

    def _get_candles_sync(self, symbol: str, timeframe: str = "15m", limit: int = 100) -> List[Dict[str, Any]]:
        mt5 = _import_mt5()
        with self._lock:
            if not self._connected and not self._connect_sync():
                return []
            mt5_sym, info = self._resolve_mt5_symbol_sync(mt5, symbol)
            if info is None:
                logger.warning("[MT5] Symbol not found for candles: %s (resolved=%s)", symbol, mt5_sym)
                return []
            tf = self._map_timeframe(mt5, timeframe)
            rates = mt5.copy_rates_from_pos(mt5_sym, tf, 0, int(limit))
            if rates is None or len(rates) == 0:
                return []
            candles: List[Dict[str, Any]] = []
            for r in rates:
                candles.append(
                    {
                        "timestamp": int(r["time"]) * 1000,
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": float(r["tick_volume"]),
                    }
                )
            return candles

    async def get_candles(self, symbol: str, timeframe: str = "15m", limit: int = 100) -> List[Dict[str, Any]]:
        """Fetch OHLCV candles from MT5 terminal."""
        return await asyncio.to_thread(self._get_candles_sync, symbol, timeframe, limit)

    # ── Order Execution ─────────────────────────────────────────────────────

    def _place_order_sync(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        reduce_only: bool = False,
        quantity_in_lots: bool = False,
        comment: str = "SmartTrade-SMC",
    ) -> Dict[str, Any]:
        mt5 = _import_mt5()
        with self._lock:
            if not is_mt5_execution_enabled():
                return {
                    "success": False,
                    "broker": "mt5",
                    "code": "MT5_EXECUTION_DISABLED",
                    "error": "MT5_EXECUTION_ENABLED is false — refusing order",
                }

            if not self._connected and not self._connect_sync():
                return {
                    "success": False,
                    "broker": "mt5",
                    "code": "MT5_NOT_CONNECTED",
                    "error": self._last_error or "MT5 terminal not connected",
                }

            acc = mt5.account_info()
            if acc is None:
                return {"success": False, "broker": "mt5", "code": "MT5_ACCOUNT_UNAVAILABLE", "error": "account_info is None"}

            # Fail-closed guard: block REAL accounts unless MT5_ALLOW_LIVE=true
            if int(getattr(acc, "trade_mode", 2)) == 2 and not self.allow_live:
                return {
                    "success": False,
                    "broker": "mt5",
                    "code": "MT5_LIVE_NOT_ALLOWED",
                    "error": "Connected MT5 account is REAL but MT5_ALLOW_LIVE=false",
                }

            mt5_sym, info = self._resolve_mt5_symbol_sync(mt5, symbol)
            if info is None:
                return {
                    "success": False,
                    "broker": "mt5",
                    "code": "MT5_SYMBOL_NOT_FOUND",
                    "error": f"Symbol '{symbol}' (resolved '{mt5_sym}') not found on MT5 server",
                }

            tick = mt5.symbol_info_tick(mt5_sym)
            if tick is None or (tick.bid <= 0 and tick.ask <= 0):
                return {
                    "success": False,
                    "broker": "mt5",
                    "code": "MT5_NO_QUOTE",
                    "error": f"No live quote available for {mt5_sym}",
                }

            side_norm = str(side or "").strip().lower()
            is_buy = side_norm in ("buy", "long")
            if not is_buy and side_norm not in ("sell", "short"):
                return {
                    "success": False,
                    "broker": "mt5",
                    "code": "INVALID_SIDE",
                    "error": f"Invalid side: {side!r}",
                }

            contract_size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
            raw_lots = float(quantity) if quantity_in_lots else (float(quantity) / contract_size)
            lots = self._round_volume(raw_lots, float(info.volume_min), float(info.volume_max), float(info.volume_step))
            if lots <= 0:
                return {
                    "success": False,
                    "broker": "mt5",
                    "code": "BELOW_MIN_LOT",
                    "error": (
                        f"Computed lot size {raw_lots:.6f} is below broker minimum {info.volume_min} "
                        f"for {mt5_sym} (contract_size={contract_size})"
                    ),
                }

            digits = int(info.digits)
            filling = self._select_filling_mode(mt5, info)

            # Handle reduce_only by closing matching open position(s) by ticket (never blocked by market hours)
            if reduce_only:
                return self._close_position_sync(mt5, mt5_sym, lots=lots, close_buy_order=is_buy, comment=comment)

            # Fail-closed Forex/Metals business-day market-hours guard (NEVER open during closure/weekend/rollover)
            if is_forex_or_metal_symbol(mt5_sym):
                mkt_open, mkt_reason = is_mt5_forex_market_open()
                if not mkt_open:
                    logger.warning("[MT5_FOREX_GUARD] BLOCKED OPEN on %s: %s", mt5_sym, mkt_reason)
                    return {
                        "success": False,
                        "broker": "mt5",
                        "code": "MT5_FOREX_MARKET_CLOSED",
                        "error": f"Forex/Metal market closed for {mt5_sym}: {mkt_reason}",
                    }

            if int(getattr(info, "trade_mode", 4)) == 0:
                return {
                    "success": False,
                    "broker": "mt5",
                    "code": "MT5_SYMBOL_TRADE_DISABLED",
                    "error": f"Symbol {mt5_sym} has trade_mode=DISABLED on MT5 server",
                }

            # Fail-closed position stacking guard: never exceed MT5_MAX_OPEN_PER_SYMBOL
            existing_pos = mt5.positions_get(symbol=mt5_sym) or ()
            if len(existing_pos) >= self.max_open_per_symbol:
                logger.warning(
                    "[MT5_GUARD] BLOCKED OPEN on %s: %d open position(s) >= max_open_per_symbol=%d",
                    mt5_sym, len(existing_pos), self.max_open_per_symbol,
                )
                return {
                    "success": False,
                    "broker": "mt5",
                    "code": "MT5_MAX_OPEN_POSITIONS_REACHED",
                    "error": f"Already {len(existing_pos)} open position(s) on {mt5_sym} (limit={self.max_open_per_symbol})",
                }

            # Cap lot size to MT5_MAX_LOT_SIZE safety ceiling
            if self.max_lot_size > 0 and lots > self.max_lot_size:
                capped_lots = self._round_volume(self.max_lot_size, float(info.volume_min), float(info.volume_max), float(info.volume_step))
                logger.warning("[MT5_GUARD] Capping lot size on %s: %.4f -> %.4f (MT5_MAX_LOT_SIZE)", mt5_sym, lots, capped_lots)
                lots = capped_lots

            exec_price = round(float(tick.ask if is_buy else tick.bid), digits)
            if order_type.lower() == "limit" and price is not None:
                mt5_type = mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT
                action = mt5.TRADE_ACTION_PENDING
                exec_price = round(float(price), digits)
            else:
                mt5_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
                action = mt5.TRADE_ACTION_DEAL

            request: Dict[str, Any] = {
                "action": action,
                "symbol": mt5_sym,
                "volume": lots,
                "type": mt5_type,
                "price": exec_price,
                "deviation": self.deviation,
                "magic": self.magic_number,
                "comment": str(comment)[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }
            # Respect broker minimum stops_level (+ spread buffer) to avoid retcode=10016 (Invalid stops)
            point = float(getattr(info, "point", 0.0) or 0.0)
            stops_level_pts = int(getattr(info, "trade_stops_level", 0) or 0)
            spread_px = abs(float(tick.ask) - float(tick.bid)) if (tick.ask > 0 and tick.bid > 0) else 0.0
            min_stop_dist = max(stops_level_pts * point, spread_px * 1.5)

            if stop_loss is not None and math.isfinite(float(stop_loss)) and float(stop_loss) > 0:
                sl_val = float(stop_loss)
                if min_stop_dist > 0:
                    if is_buy and sl_val > (exec_price - min_stop_dist):
                        sl_val = exec_price - min_stop_dist * 1.1
                    elif not is_buy and sl_val < (exec_price + min_stop_dist):
                        sl_val = exec_price + min_stop_dist * 1.1
                request["sl"] = round(sl_val, digits)
            if take_profit is not None and math.isfinite(float(take_profit)) and float(take_profit) > 0:
                tp_val = float(take_profit)
                if min_stop_dist > 0:
                    if is_buy and tp_val < (exec_price + min_stop_dist):
                        tp_val = exec_price + min_stop_dist * 1.1
                    elif not is_buy and tp_val > (exec_price - min_stop_dist):
                        tp_val = exec_price - min_stop_dist * 1.1
                request["tp"] = round(tp_val, digits)

            res = mt5.order_send(request)
            if res is None:
                err = mt5.last_error()
                return {
                    "success": False,
                    "broker": "mt5",
                    "code": "MT5_ORDER_SEND_NONE",
                    "error": f"order_send returned None: {err}",
                }

            retcode = int(getattr(res, "retcode", -1))
            ok_codes = {
                getattr(mt5, "TRADE_RETCODE_DONE", 10009),
                getattr(mt5, "TRADE_RETCODE_PLACED", 10008),
                getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010),
            }
            if retcode not in ok_codes:
                return {
                    "success": False,
                    "broker": "mt5",
                    "code": f"MT5_RETCODE_{retcode}",
                    "error": f"MT5 order rejected (retcode={retcode}): {getattr(res, 'comment', '')}",
                }

            filled_price = float(getattr(res, "price", 0.0) or exec_price)
            filled_vol = float(getattr(res, "volume", 0.0) or lots)
            order_id = str(getattr(res, "order", "") or getattr(res, "deal", ""))
            logger.info(
                "[MT5_ORDER_OK] %s %s %s lots=%s price=%s sl=%s tp=%s order_id=%s",
                mt5_sym,
                "BUY" if is_buy else "SELL",
                order_type.upper(),
                filled_vol,
                filled_price,
                request.get("sl"),
                request.get("tp"),
                order_id,
            )
            return {
                "success": True,
                "code": "00000",
                "broker": "mt5",
                "symbol": mt5_sym,
                "order_id": order_id,
                "deal_id": str(getattr(res, "deal", "")),
                "price": filled_price,
                "price_executed": filled_price,
                "filled_qty": filled_vol,
                "volume": filled_vol,
                "sl_attached": "sl" in request,
                "tp_attached": "tp" in request,
                "retcode": retcode,
                "comment": str(getattr(res, "comment", "")),
            }

    def _close_position_sync(
        self,
        mt5: Any,
        mt5_sym: str,
        lots: Optional[float] = None,
        close_buy_order: Optional[bool] = None,
        ticket: Optional[int] = None,
        comment: str = "SmartTrade-Close",
    ) -> Dict[str, Any]:
        positions = mt5.positions_get(ticket=ticket) if ticket else mt5.positions_get(symbol=mt5_sym)
        if not positions:
            return {
                "success": False,
                "broker": "mt5",
                "code": "MT5_NO_OPEN_POSITION",
                "error": f"No open MT5 position found for {mt5_sym}",
            }

        target = None
        for p in positions:
            if close_buy_order is None:
                target = p
                break
            # Closing a SHORT position requires a BUY order; closing a LONG requires a SELL order
            if close_buy_order and p.type == mt5.POSITION_TYPE_SELL:
                target = p
                break
            if (not close_buy_order) and p.type == mt5.POSITION_TYPE_BUY:
                target = p
                break
        if target is None:
            return {
                "success": False,
                "broker": "mt5",
                "code": "MT5_NO_MATCHING_SIDE_POSITION",
                "error": f"No matching position side to reduce/close on {mt5_sym}",
            }

        info = mt5.symbol_info(target.symbol)
        tick = mt5.symbol_info_tick(target.symbol)
        if info is None or tick is None:
            return {"success": False, "broker": "mt5", "code": "MT5_NO_QUOTE", "error": f"No quote for {target.symbol}"}

        is_close_buy = target.type == mt5.POSITION_TYPE_SELL
        close_vol = float(target.volume) if lots is None else min(float(lots), float(target.volume))
        close_vol = self._round_volume(close_vol, float(info.volume_min), float(info.volume_max), float(info.volume_step))
        if close_vol <= 0:
            return {"success": False, "broker": "mt5", "code": "BELOW_MIN_LOT", "error": "Close volume below min lot"}

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": target.symbol,
            "volume": close_vol,
            "type": mt5.ORDER_TYPE_BUY if is_close_buy else mt5.ORDER_TYPE_SELL,
            "position": int(target.ticket),
            "price": round(float(tick.ask if is_close_buy else tick.bid), int(info.digits)),
            "deviation": self.deviation,
            "magic": self.magic_number,
            "comment": str(comment)[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._select_filling_mode(mt5, info),
        }
        res = mt5.order_send(req)
        if res is None:
            return {"success": False, "broker": "mt5", "code": "MT5_CLOSE_NONE", "error": str(mt5.last_error())}
        retcode = int(getattr(res, "retcode", -1))
        if retcode not in (
            getattr(mt5, "TRADE_RETCODE_DONE", 10009),
            getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010),
        ):
            return {
                "success": False,
                "broker": "mt5",
                "code": f"MT5_RETCODE_{retcode}",
                "error": f"Close rejected (retcode={retcode}): {getattr(res, 'comment', '')}",
            }
        return {
            "success": True,
            "code": "00000",
            "broker": "mt5",
            "symbol": target.symbol,
            "order_id": str(getattr(res, "order", "")),
            "price": float(getattr(res, "price", 0.0) or req["price"]),
            "filled_qty": float(getattr(res, "volume", 0.0) or close_vol),
            "closed_ticket": int(target.ticket),
        }

    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        reduce_only: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Place a market, limit, or reduce-only closing order on MT5."""
        return await asyncio.to_thread(
            self._place_order_sync,
            symbol,
            side,
            quantity,
            order_type,
            price,
            stop_loss,
            take_profit,
            reduce_only,
            bool(kwargs.get("quantity_in_lots", False)),
            str(kwargs.get("comment", "SmartTrade-SMC")),
        )

    async def close_position(
        self,
        symbol: str,
        ticket: Optional[int] = None,
        volume: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Close an open MT5 position (full or partial)."""
        def _run():
            mt5 = _import_mt5()
            with self._lock:
                if not self._connected and not self._connect_sync():
                    return {"success": False, "broker": "mt5", "error": self._last_error or "Not connected"}
                mt5_sym, _ = self._resolve_mt5_symbol_sync(mt5, symbol)
                return self._close_position_sync(mt5, mt5_sym, lots=volume, ticket=ticket)

        return await asyncio.to_thread(_run)

    async def enforce_market_close_flatten(
        self,
        now_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Pre-Close / Closure Auto-Flatten Guard:
        NEVER leave any MT5 Forex/Metal position open during market closures
        (Friday >= 20:30 UTC, weekend, or daily rollover).
        If MT5_CLOSE_ALL_ON_CLOSURE=true, also flattens crypto positions on MT5 during closure.
        """
        mkt_open, mkt_reason = is_mt5_forex_market_open(now_utc)
        if mkt_open:
            return {"action": "none", "market_open": True, "closed": [], "failed": []}

        close_all_assets = _is_truthy(os.getenv("MT5_CLOSE_ALL_ON_CLOSURE"), default=False)
        positions = await self.get_positions()
        closed: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []

        for p in positions:
            sym = str(p.get("symbol") or "")
            if not close_all_assets and not is_forex_or_metal_symbol(sym):
                continue
            ticket = p.get("ticket")
            logger.warning(
                "[MT5_CLOSE_GUARD] Auto-closing open position before/during closure (%s): %s ticket=%s vol=%s",
                mkt_reason, sym, ticket, p.get("volume"),
            )
            res = await self.close_position(symbol=sym, ticket=ticket)
            if res.get("success"):
                closed.append({"symbol": sym, "ticket": ticket, "order_id": res.get("order_id")})
            else:
                failed.append({"symbol": sym, "ticket": ticket, "error": res.get("error")})
                logger.critical(
                    "[MT5_CLOSE_GUARD] FAILED to auto-close %s ticket=%s: %s",
                    sym, ticket, res.get("error"),
                )

        return {
            "action": "flatten" if (closed or failed) else "checked_flat",
            "market_open": False,
            "reason": mkt_reason,
            "closed": closed,
            "failed": failed,
        }
