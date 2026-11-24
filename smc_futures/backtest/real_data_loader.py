"""
Real Data Loader — Kraken OHLC API + SQLite + Validation
════════════════════════════════════════════════════════════════════
Fetches REAL historical candle data for backtesting.

Data sources (in order of preference):
  1. Kraken public OHLC API (no auth needed for market data)
  2. Local SQLite cache (data/candles.db)

Validation:
  - Gap detection (missing candles)
  - OHLC coherence (high >= open/close, low <= open/close)
  - Timestamp monotonicity
  - Anomaly detection (extreme moves, zero volume)
  - Reject corrupted data — never interpolate

Usage:
    loader = RealDataLoader()
    result = await loader.load("BTCUSDT", "5m", days=90)
    if result.is_valid:
        candles = result.candles  # List[BTCandle]
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Tuple

import httpx

from smc_futures.backtest.engine import BTCandle

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────

KRAKEN_BASE = "https://api.kraken.com"
KRAKEN_OHLC_BATCH = 720  # Kraken returns max 720 candles per call
RATE_LIMIT_DELAY = 0.5  # seconds between API calls

TF_DURATION_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000,
    "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "12h": 43_200_000,
    "1d": 86_400_000, "1w": 604_800_000,
}

# Kraken OHLC interval in minutes
KRAKEN_TF_MAP = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240,
    "6h": 360, "12h": 720,
    "1d": 1440, "1w": 10080,
}

# Symbol mapping: unified symbol → Kraken spot pair
KRAKEN_SYMBOL_MAP = {
    "BTCUSDT": "XXBTZUSD", "ETHUSDT": "XETHZUSD",
    "XRPUSDT": "XXRPZUSD", "SOLUSDT": "SOLUSD",
    "DOGEUSDT": "XDGUSD", "ADAUSDT": "ADAUSD",
    "AVAXUSDT": "AVAXUSD", "LINKUSDT": "LINKUSD",
    "DOTUSDT": "DOTUSD", "MATICUSDT": "MATICUSD",
    "LTCUSDT": "XLTCZUSD", "UNIUSDT": "UNIUSD",
    "ATOMUSDT": "ATOMUSD", "NEARUSDT": "NEARUSD",
    "AAVEUSDT": "AAVEUSD",
}


# ─── Validation Result ───────────────────

@dataclass
class ValidationResult:
    """Results of data quality validation."""
    is_valid: bool = True
    total_candles: int = 0
    gaps_found: int = 0
    gap_details: List[str] = field(default_factory=list)
    ohlc_violations: int = 0
    ohlc_details: List[str] = field(default_factory=list)
    timestamp_issues: int = 0
    anomalies: int = 0
    anomaly_details: List[str] = field(default_factory=list)
    zero_volume_candles: int = 0
    date_range: str = ""
    coverage_pct: float = 0.0


@dataclass
class LoadResult:
    """Result from real data loading."""
    candles: List[BTCandle] = field(default_factory=list)
    source: str = ""
    symbol: str = ""
    timeframe: str = ""
    validation: ValidationResult = field(default_factory=ValidationResult)

    @property
    def is_valid(self) -> bool:
        return self.validation.is_valid and len(self.candles) > 0


# ─── Data Validator ──────────────────────

class DataValidator:
    """Validate OHLCV data quality — reject, never interpolate."""

    @staticmethod
    def validate(
        candles: List[BTCandle],
        timeframe: str,
        max_gap_ratio: float = 0.02,
        max_anomaly_ratio: float = 0.01,
    ) -> ValidationResult:
        v = ValidationResult(total_candles=len(candles))

        if not candles:
            v.is_valid = False
            return v

        tf_ms = TF_DURATION_MS.get(timeframe, 300_000)

        # Date range
        from datetime import datetime, timezone
        start_dt = datetime.fromtimestamp(candles[0].timestamp / 1000, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(candles[-1].timestamp / 1000, tz=timezone.utc)
        v.date_range = f"{start_dt:%Y-%m-%d %H:%M} → {end_dt:%Y-%m-%d %H:%M} UTC"

        # Expected candles
        span_ms = candles[-1].timestamp - candles[0].timestamp
        expected_candles = (span_ms // tf_ms) + 1
        v.coverage_pct = (len(candles) / expected_candles * 100) if expected_candles > 0 else 0

        # 1. Timestamp monotonicity + gap detection
        for i in range(1, len(candles)):
            dt = candles[i].timestamp - candles[i - 1].timestamp

            # Non-monotonic
            if dt <= 0:
                v.timestamp_issues += 1

            # Gap (> 1.5x expected interval)
            if dt > tf_ms * 1.5:
                v.gaps_found += 1
                gap_bars = dt // tf_ms - 1
                ts_str = datetime.fromtimestamp(
                    candles[i - 1].timestamp / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M")
                if len(v.gap_details) < 20:
                    v.gap_details.append(
                        f"Gap of {gap_bars} bars at {ts_str}"
                    )

        # 2. OHLC coherence
        for i, c in enumerate(candles):
            issues = []
            if c.high < c.open or c.high < c.close:
                issues.append("high < open|close")
            if c.low > c.open or c.low > c.close:
                issues.append("low > open|close")
            if c.high < c.low:
                issues.append("high < low")
            if c.close <= 0 or c.open <= 0:
                issues.append("price <= 0")
            if issues:
                v.ohlc_violations += 1
                if len(v.ohlc_details) < 10:
                    v.ohlc_details.append(f"Bar {i}: {', '.join(issues)}")

        # 3. Anomaly detection (extreme moves)
        prices = [c.close for c in candles]
        for i in range(1, len(prices)):
            if prices[i - 1] <= 0:
                continue
            ret = abs(prices[i] - prices[i - 1]) / prices[i - 1]
            # > 10% single-bar move is suspicious on 5m
            if ret > 0.10:
                v.anomalies += 1
                if len(v.anomaly_details) < 10:
                    v.anomaly_details.append(
                        f"Bar {i}: {ret:.1%} move"
                    )

        # 4. Zero volume
        v.zero_volume_candles = sum(1 for c in candles if c.volume <= 0)

        # Validity: reject if too many issues
        gap_ratio = v.gaps_found / max(1, len(candles))
        anomaly_ratio = v.anomalies / max(1, len(candles))
        ohlc_ratio = v.ohlc_violations / max(1, len(candles))

        if gap_ratio > max_gap_ratio:
            v.is_valid = False
            logger.warning(f"Data REJECTED: gap ratio {gap_ratio:.3f} > {max_gap_ratio}")
        if anomaly_ratio > max_anomaly_ratio:
            v.is_valid = False
            logger.warning(f"Data REJECTED: anomaly ratio {anomaly_ratio:.3f} > {max_anomaly_ratio}")
        if ohlc_ratio > 0.005:
            v.is_valid = False
            logger.warning(f"Data REJECTED: OHLC violation ratio {ohlc_ratio:.3f}")
        if v.timestamp_issues > 0:
            v.is_valid = False
            logger.warning(f"Data REJECTED: {v.timestamp_issues} timestamp ordering issues")
        if v.coverage_pct < 95.0:
            logger.warning(f"Data coverage {v.coverage_pct:.1f}% (< 95%) — proceed with caution")

        return v

    @staticmethod
    def format_report(v: ValidationResult) -> str:
        lines = [
            "── Data Validation ──",
            f"  Candles:        {v.total_candles}",
            f"  Date Range:     {v.date_range}",
            f"  Coverage:       {v.coverage_pct:.1f}%",
            f"  Gaps:           {v.gaps_found}",
            f"  OHLC Issues:    {v.ohlc_violations}",
            f"  Anomalies:      {v.anomalies}",
            f"  Zero Vol Bars:  {v.zero_volume_candles}",
            f"  TS Issues:      {v.timestamp_issues}",
            f"  VALID:          {'YES' if v.is_valid else 'NO — DATA REJECTED'}",
        ]
        if v.gap_details:
            lines.append(f"  Gap details (first {len(v.gap_details)}):")
            for d in v.gap_details[:5]:
                lines.append(f"    - {d}")
        if v.anomaly_details:
            lines.append(f"  Anomaly details:")
            for d in v.anomaly_details[:5]:
                lines.append(f"    - {d}")
        return "\n".join(lines)


# ─── Kraken OHLC Fetcher ─────────────────

class KrakenFetcher:
    """Fetch historical candles from Kraken public OHLC API."""

    @staticmethod
    def _resolve_pair(symbol: str) -> str:
        """Convert unified symbol to Kraken pair name."""
        return KRAKEN_SYMBOL_MAP.get(symbol, symbol)

    @staticmethod
    async def fetch(
        symbol: str,
        timeframe: str,
        days: int = 90,
        progress_callback=None,
    ) -> List[BTCandle]:
        interval = KRAKEN_TF_MAP.get(timeframe)
        if not interval:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        tf_ms = TF_DURATION_MS[timeframe]
        now_s = int(time.time())
        target_start_s = now_s - (days * 86_400)

        all_candles: Dict[int, BTCandle] = {}
        since_s = target_start_s
        total_fetched = 0
        retries = 0
        max_retries = 3

        pair = KrakenFetcher._resolve_pair(symbol)
        logger.info(f"Fetching {symbol} ({pair}) {timeframe} candles ({days} days) from Kraken...")

        async with httpx.AsyncClient(timeout=15.0) as client:
            while since_s < now_s:
                params = {
                    "pair": pair,
                    "interval": interval,
                    "since": since_s,
                }

                try:
                    resp = await client.get(
                        f"{KRAKEN_BASE}/0/public/OHLC",
                        params=params,
                    )
                    resp.raise_for_status()
                    body = resp.json()

                    errors = body.get("error", [])
                    if errors:
                        logger.error(f"Kraken API error: {errors}")
                        retries += 1
                        if retries >= max_retries:
                            break
                        await asyncio.sleep(2.0)
                        continue

                    result = body.get("result", {})
                    # Find the pair key (excludes "last")
                    pair_key = [k for k in result.keys() if k != "last"]
                    if not pair_key:
                        break
                    rows = result[pair_key[0]]
                    if not rows:
                        break

                    retries = 0
                    batch_count = 0
                    newest_ts_s = since_s

                    for r in rows:
                        ts_s = int(r[0])
                        ts_ms = ts_s * 1000
                        candle = BTCandle(
                            timestamp=ts_ms,
                            open=float(r[1]),
                            high=float(r[2]),
                            low=float(r[3]),
                            close=float(r[4]),
                            volume=float(r[6]) if len(r) > 6 else 0.0,
                        )
                        if ts_ms not in all_candles:
                            all_candles[ts_ms] = candle
                            batch_count += 1
                        newest_ts_s = max(newest_ts_s, ts_s)

                    total_fetched += batch_count

                    if progress_callback:
                        progress_callback(total_fetched, days)

                    # Kraken paginates forward via "last" timestamp
                    last_ts = result.get("last", 0)
                    if last_ts and last_ts > since_s:
                        since_s = last_ts
                    else:
                        break

                    if batch_count == 0:
                        break

                    await asyncio.sleep(RATE_LIMIT_DELAY)

                except httpx.HTTPStatusError as e:
                    logger.error(f"HTTP error: {e}")
                    retries += 1
                    if retries >= max_retries:
                        break
                    await asyncio.sleep(2.0)
                except Exception as e:
                    logger.error(f"Fetch error: {e}")
                    retries += 1
                    if retries >= max_retries:
                        break
                    await asyncio.sleep(2.0)

        # Sort by timestamp
        sorted_candles = sorted(all_candles.values(), key=lambda c: c.timestamp)
        logger.info(f"Fetched {len(sorted_candles)} candles for {symbol} ({pair}) {timeframe}")
        return sorted_candles


# ─── SQLite Cache Loader ─────────────────

class SQLiteCacheLoader:
    """Load candles from local SQLite cache (data/candles.db)."""

    @staticmethod
    def load(
        symbol: str,
        timeframe: str,
        db_path: Optional[str] = None,
    ) -> List[BTCandle]:
        if db_path is None:
            project_root = Path(__file__).resolve().parent.parent.parent
            db_path = str(project_root / "data" / "candles.db")

        if not os.path.exists(db_path):
            logger.info(f"No SQLite cache at {db_path}")
            return []

        candles = []
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # Check what tables exist
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = [row[0] for row in cursor.fetchall()]

            if "candles" in tables:
                cursor.execute(
                    "SELECT open_time, open, high, low, close, volume "
                    "FROM candles "
                    "WHERE symbol=? AND timeframe=? "
                    "ORDER BY open_time ASC",
                    (symbol, timeframe),
                )
                for row in cursor.fetchall():
                    candles.append(BTCandle(
                        timestamp=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]) if row[5] else 0.0,
                    ))
            elif "ohlcv" in tables:
                cursor.execute(
                    "SELECT timestamp, open, high, low, close, volume "
                    "FROM ohlcv "
                    "WHERE symbol=? AND timeframe=? "
                    "ORDER BY timestamp ASC",
                    (symbol, timeframe),
                )
                for row in cursor.fetchall():
                    candles.append(BTCandle(
                        timestamp=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]) if row[5] else 0.0,
                    ))

            conn.close()
            logger.info(f"Loaded {len(candles)} candles from SQLite cache")
        except Exception as e:
            logger.warning(f"SQLite load error: {e}")

        return candles


# ─── SQLite Cache Writer ─────────────────

class SQLiteCacheWriter:
    """Save fetched candles to SQLite for future use."""

    @staticmethod
    def save(
        candles: List[BTCandle],
        symbol: str,
        timeframe: str,
        db_path: Optional[str] = None,
    ) -> int:
        if not candles:
            return 0

        if db_path is None:
            project_root = Path(__file__).resolve().parent.parent.parent
            db_path = str(project_root / "data" / "candles.db")

        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                open_time INTEGER NOT NULL,
                close_time INTEGER,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL DEFAULT 0,
                PRIMARY KEY (symbol, timeframe, open_time)
            )
        """)

        tf_ms = TF_DURATION_MS.get(timeframe, 300_000)
        inserted = 0

        for c in candles:
            try:
                cursor.execute(
                    "INSERT OR REPLACE INTO candles "
                    "(symbol, timeframe, open_time, close_time, open, high, low, close, volume) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (symbol, timeframe, c.timestamp, c.timestamp + tf_ms,
                     c.open, c.high, c.low, c.close, c.volume),
                )
                inserted += 1
            except Exception:
                pass

        conn.commit()
        conn.close()
        logger.info(f"Cached {inserted} candles to SQLite")
        return inserted


# ═══════════════════════════════════════════
#  REAL DATA LOADER — Main Class
# ═══════════════════════════════════════════

class RealDataLoader:
    """
    Unified real data loader with validation.

    Priority:
      1. SQLite cache (fast, already validated)
      2. Kraken OHLC API (fetches + caches for next time)
    """

    def __init__(self, cache_db_path: Optional[str] = None):
        self.cache_db_path = cache_db_path

    async def load(
        self,
        symbol: str = "BTCUSDT",
        timeframe: str = "5m",
        days: int = 90,
        use_cache: bool = True,
        force_api: bool = False,
    ) -> LoadResult:
        result = LoadResult(
            source="none",
            symbol=symbol,
            timeframe=timeframe,
        )

        # Try cache first
        if use_cache and not force_api:
            cached = SQLiteCacheLoader.load(
                symbol, timeframe, db_path=self.cache_db_path
            )
            if cached:
                # Check if cache covers the requested period
                now_ms = int(time.time() * 1000)
                target_start = now_ms - (days * 86_400_000)
                if cached[0].timestamp <= target_start + 86_400_000:  # Allow 1 day tolerance
                    result.candles = cached
                    result.source = "sqlite_cache"
                    result.validation = DataValidator.validate(cached, timeframe)
                    if result.is_valid:
                        logger.info(
                            f"Using cached data: {len(cached)} candles, "
                            f"valid={result.is_valid}"
                        )
                        return result
                    else:
                        logger.warning("Cached data failed validation, fetching from API")

        # Fetch from Kraken OHLC API
        try:
            candles = await KrakenFetcher.fetch(
                symbol, timeframe, days,
                progress_callback=self._log_progress,
            )
        except Exception as e:
            logger.error(f"Kraken API fetch failed: {e}")
            # Fall back to cache even if it doesn't cover full period
            if use_cache:
                cached = SQLiteCacheLoader.load(
                    symbol, timeframe, db_path=self.cache_db_path
                )
                if cached:
                    result.candles = cached
                    result.source = "sqlite_cache_fallback"
                    result.validation = DataValidator.validate(cached, timeframe)
                    return result
            return result

        if not candles:
            logger.error(f"No data fetched for {symbol} {timeframe}")
            return result

        # Validate
        result.candles = candles
        result.source = "kraken_api"
        result.validation = DataValidator.validate(candles, timeframe)

        # Cache for next time
        if result.is_valid:
            SQLiteCacheWriter.save(
                candles, symbol, timeframe, db_path=self.cache_db_path
            )

        return result

    def load_sync(
        self,
        symbol: str = "BTCUSDT",
        timeframe: str = "5m",
        days: int = 90,
        **kwargs,
    ) -> LoadResult:
        """Synchronous wrapper for load()."""
        return asyncio.run(self.load(symbol, timeframe, days, **kwargs))

    @staticmethod
    def _log_progress(fetched: int, days: int):
        expected = days * 288  # 5m bars per day
        pct = min(100, fetched / expected * 100)
        if fetched % 2000 == 0:
            logger.info(f"  ... {fetched} candles fetched (~{pct:.0f}%)")
