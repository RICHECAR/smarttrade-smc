"""
MySQL Market Data Models — Schéma complet pour backtesting
══════════════════════════════════════════════════════════════

Tables :
  ┌─────────────────────┐
  │ candles              │ ← OHLCV toutes paires, tous TF
  │ funding_rates        │ ← Taux de financement futures
  │ tickers_snapshots    │ ← Snapshots prix/volume en continu
  │ signals_history      │ ← Tous les signaux SMC générés
  │ backtest_trades      │ ← Résultats de backtesting
  │ backtest_runs        │ ← Sessions de backtest
  │ collection_status    │ ← Suivi de l'avancement de collecte
  └─────────────────────┘

Usage:
    from backend.db.mysql_models import (
        MarketBase, Candle, FundingRate, TickerSnapshot,
        SignalHistory, BacktestTrade, BacktestRun, CollectionStatus,
    )
    MarketBase.metadata.create_all(bind=mysql_engine)
"""

import datetime
from sqlalchemy import (
    Column, Integer, BigInteger, Float, String, DateTime,
    Boolean, Text, Index, UniqueConstraint, Enum as SAEnum,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker
import enum
import os
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════
#  MySQL Engine
# ══════════════════════════════════════════════════════════════

MarketBase = declarative_base()

def get_mysql_url() -> str:
    """Construit l'URL MySQL depuis les variables d'environnement."""
    url = os.getenv("MARKET_DATA_DB_URL", "")
    if url:
        return url
    host = os.getenv("MYSQL_HOST", "localhost")
    port = os.getenv("MYSQL_PORT", "3306")
    user = os.getenv("MYSQL_USER", "root")
    password = os.getenv("MYSQL_PASSWORD", "root")
    database = os.getenv("MYSQL_DATABASE", "smc_market_data")
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"


def get_mysql_engine(echo: bool = False):
    """Crée un engine SQLAlchemy pour MySQL."""
    return create_engine(
        get_mysql_url(),
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=3600,
        echo=echo,
    )


def get_mysql_session(engine=None):
    """Crée une session MySQL."""
    if engine is None:
        engine = get_mysql_engine()
    Session = sessionmaker(bind=engine)
    return Session()


# ══════════════════════════════════════════════════════════════
#  CANDLES — Données OHLCV brutes
# ══════════════════════════════════════════════════════════════

class Candle(MarketBase):
    """
    Bougie OHLCV stockée en MySQL.
    Clé unique : (broker, symbol, timeframe, open_time)
    """
    __tablename__ = "candles"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    broker = Column(String(20), nullable=False, default="kraken")
    symbol = Column(String(30), nullable=False, index=True)        # BTCUSDT
    timeframe = Column(String(10), nullable=False, index=True)     # 1m, 5m, 15m...
    open_time = Column(BigInteger, nullable=False, index=True)     # timestamp ms
    close_time = Column(BigInteger, nullable=True)                 # timestamp ms
    open = Column(Float(precision=53), nullable=False)
    high = Column(Float(precision=53), nullable=False)
    low = Column(Float(precision=53), nullable=False)
    close = Column(Float(precision=53), nullable=False)
    volume = Column(Float(precision=53), nullable=False, default=0.0)
    quote_volume = Column(Float(precision=53), nullable=True)      # volume en USDT
    trades_count = Column(Integer, nullable=True)                  # nb de trades
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("broker", "symbol", "timeframe", "open_time",
                         name="uq_candle"),
        Index("ix_candle_lookup", "broker", "symbol", "timeframe", "open_time"),
        Index("ix_candle_time_range", "symbol", "timeframe", "open_time"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return (f"<Candle {self.broker}:{self.symbol} {self.timeframe} "
                f"@ {self.open_time} O={self.open} H={self.high} "
                f"L={self.low} C={self.close}>")


# ══════════════════════════════════════════════════════════════
#  FUNDING RATES — Taux de financement futures
# ══════════════════════════════════════════════════════════════

class FundingRate(MarketBase):
    """Historique des taux de financement."""
    __tablename__ = "funding_rates"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    broker = Column(String(20), nullable=False, default="kraken")
    symbol = Column(String(30), nullable=False, index=True)
    timestamp = Column(BigInteger, nullable=False)          # ms
    funding_rate = Column(Float(precision=53), nullable=False)
    mark_price = Column(Float(precision=53), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("broker", "symbol", "timestamp", name="uq_funding"),
        Index("ix_funding_lookup", "broker", "symbol", "timestamp"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


# ══════════════════════════════════════════════════════════════
#  TICKER SNAPSHOTS — Prix temps réel
# ══════════════════════════════════════════════════════════════

class TickerSnapshot(MarketBase):
    """Snapshot périodique des tickers pour reconstruire les spreads."""
    __tablename__ = "ticker_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    broker = Column(String(20), nullable=False, default="kraken")
    symbol = Column(String(30), nullable=False, index=True)
    timestamp = Column(BigInteger, nullable=False)
    bid = Column(Float(precision=53), nullable=True)
    ask = Column(Float(precision=53), nullable=True)
    last = Column(Float(precision=53), nullable=False)
    volume_24h = Column(Float(precision=53), nullable=True)
    high_24h = Column(Float(precision=53), nullable=True)
    low_24h = Column(Float(precision=53), nullable=True)
    open_interest = Column(Float(precision=53), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_ticker_lookup", "broker", "symbol", "timestamp"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


# ══════════════════════════════════════════════════════════════
#  SIGNALS HISTORY — Archive des signaux SMC
# ══════════════════════════════════════════════════════════════

class SignalDirection(str, enum.Enum):
    LONG = "long"
    SHORT = "short"


class SignalHistory(MarketBase):
    """
    Archive complète de chaque signal SMC émis (exécuté ou non).
    Permet de backtester les mêmes signaux sur des données historiques.
    """
    __tablename__ = "signals_history"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    signal_id = Column(String(64), nullable=True, unique=True)
    broker = Column(String(20), nullable=False, default="kraken")
    symbol = Column(String(30), nullable=False, index=True)
    direction = Column(String(10), nullable=False)          # long / short
    timeframe = Column(String(10), nullable=True)            # tf du setup
    entry_price = Column(Float(precision=53), nullable=False)
    stop_loss = Column(Float(precision=53), nullable=True)
    take_profit = Column(Float(precision=53), nullable=True)
    rr_ratio = Column(Float, nullable=True)
    setup_type = Column(String(50), nullable=True)           # sweep, bos_fvg, asia...
    htf_bias = Column(String(20), nullable=True)             # bullish / bearish / neutral
    session = Column(String(20), nullable=True)              # London / NY / Asia
    confidence = Column(Float, nullable=True)
    executed = Column(Boolean, default=False)
    rejected_reason = Column(Text, nullable=True)
    timestamp = Column(BigInteger, nullable=False)           # quand le signal a été généré
    candle_data = Column(Text, nullable=True)                # JSON des candles au moment du signal
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_signal_lookup", "broker", "symbol", "timestamp"),
        Index("ix_signal_setup", "setup_type", "direction"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


# ══════════════════════════════════════════════════════════════
#  BACKTEST RESULTS — Résultats de backtesting
# ══════════════════════════════════════════════════════════════

class BacktestRun(MarketBase):
    """Session de backtest avec paramètres."""
    __tablename__ = "backtest_runs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=True)
    strategy = Column(String(50), nullable=False, default="smc")
    symbols = Column(String(500), nullable=False)             # CSV
    timeframe = Column(String(10), nullable=False)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    initial_capital = Column(Float, default=1000.0)
    risk_pct = Column(Float, default=0.01)
    leverage = Column(Integer, default=3)
    # Résultats globaux
    total_trades = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    win_rate = Column(Float, default=0.0)
    total_pnl = Column(Float, default=0.0)
    max_drawdown_pct = Column(Float, default=0.0)
    sharpe_ratio = Column(Float, nullable=True)
    profit_factor = Column(Float, nullable=True)
    avg_rr = Column(Float, nullable=True)
    params_json = Column(Text, nullable=True)                 # JSON complet des params
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class BacktestTrade(MarketBase):
    """Trade individuel d'un backtest."""
    __tablename__ = "backtest_trades"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(BigInteger, nullable=False, index=True)
    symbol = Column(String(30), nullable=False)
    direction = Column(String(10), nullable=False)
    entry_price = Column(Float(precision=53), nullable=False)
    exit_price = Column(Float(precision=53), nullable=True)
    stop_loss = Column(Float(precision=53), nullable=True)
    take_profit = Column(Float(precision=53), nullable=True)
    qty = Column(Float, nullable=True)
    rr_ratio = Column(Float, nullable=True)
    pnl = Column(Float, default=0.0)
    pnl_pct = Column(Float, default=0.0)
    result = Column(String(10), nullable=True)                # win / loss / be
    setup_type = Column(String(50), nullable=True)
    entry_time = Column(BigInteger, nullable=False)
    exit_time = Column(BigInteger, nullable=True)
    duration_min = Column(Integer, nullable=True)
    entry_reason = Column(Text, nullable=True)
    exit_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_bt_trade_run", "run_id", "symbol"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


# ══════════════════════════════════════════════════════════════
#  COLLECTION STATUS — Suivi de la collecte
# ══════════════════════════════════════════════════════════════

class CollectionStatus(MarketBase):
    """Suivi de l'avancement de la collecte par symbole/timeframe."""
    __tablename__ = "collection_status"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    broker = Column(String(20), nullable=False, default="kraken")
    symbol = Column(String(30), nullable=False)
    timeframe = Column(String(10), nullable=False)
    oldest_ts = Column(BigInteger, nullable=True)            # plus ancienne bougie
    newest_ts = Column(BigInteger, nullable=True)            # plus récente bougie
    total_candles = Column(Integer, default=0)
    last_fetch_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="pending")           # pending / collecting / done / error
    error_msg = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("broker", "symbol", "timeframe", name="uq_collection_status"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
