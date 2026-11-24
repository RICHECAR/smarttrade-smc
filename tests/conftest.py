"""
Shared test fixtures — Environment and Physical Database Safety Net
===================================================================

- Installs db_guard immediately at conftest import time (before test collection).
- Forces SMARTTRADE_TEST_MODE=1 and SMARTTRADE_DISABLE_DOTENV=1 so tests remain
  independent of production `.env` and `./data/trades.db`.
- Supports two explicit SQLite profiles via SMARTTRADE_DB_PROFILE:
    - "memory" (default): sqlite:///:memory: (zero disk SQLite files).
    - "temp_file": dedicated session temporary file outside the repo.
- Does NOT run schema migrations or create disk DB files globally during pytest_configure().
"""

import os
import sys
import pytest

# ── STEP 1: PHYSICAL ANTI-PRODUCTION DB GUARD (EARLIEST INITIALIZATION) ────────
from tests.db_guard import (
    setup_isolated_test_environment,
    verify_authorized_db,
    verify_not_production_db,
    get_db_profile,
    set_db_profile,
    authorize_temp_db_root,
    revoke_temp_db_root,
    UnauthorizedDatabaseAccessDenied,
    ProductionDatabaseAccessDenied,
)

# Force test environment BEFORE importing any backend/smc_futures module
_ISOLATED_TEST_DB = setup_isolated_test_environment()

from backend.db.session import configure_database, reset_database_configuration

# Configure lazy SQLAlchemy engine cleanly onto the isolated test target
_DEFAULT_TEST_URL = os.environ["DATABASE_URL"]
configure_database(_DEFAULT_TEST_URL)

_CRITICAL_ENV_VARS = [
    "BITGET_ENABLED", "KRAKEN_ENABLED", "TRADING_MODE", "AUTO_EXECUTE",
    "BITGET_REAL_ONLY", "BITGET_PRODUCT_TYPE", "NODE_ENV",
    "FORCE_EXECUTE", "CONFIRM_LIVE_MULTI_BROKER",
    "MIRROR_TRADING_ENABLED", "MULTI_BROKER_MODE",
    "MAX_DAILY_LOSS_PCT", "SMC_MAX_DAILY_LOSS_PCT",
    "ALLOW_MANUAL_TRADES",
    "DASHBOARD_API_KEY",
    "SMARTTRADE_TEST_MODE", "SMARTTRADE_DISABLE_DOTENV", "SMARTTRADE_SKIP_APP_LIFESPAN",
    "DATABASE_URL", "TRADES_DB_PATH", "SMARTTRADE_DB_PATH",
]


def pytest_configure(config):
    """
    Pytest configuration hook: executes before test collection.
    Verifies isolated DB environment without writing production schema or disk files in memory mode.
    """
    os.environ["SMARTTRADE_TEST_MODE"] = "1"
    os.environ["SMARTTRADE_DISABLE_DOTENV"] = "1"
    os.environ["SMARTTRADE_SKIP_APP_LIFESPAN"] = "1"

    for env_var in ("DATABASE_URL", "TRADES_DB_PATH", "SMARTTRADE_DB_PATH"):
        current_val = os.environ.get(env_var)
        if not current_val:
            if env_var == "DATABASE_URL":
                os.environ["DATABASE_URL"] = _DEFAULT_TEST_URL
            else:
                os.environ[env_var] = _ISOLATED_TEST_DB
            current_val = os.environ.get(env_var)
        verify_authorized_db(current_val, caller_context=f"pytest_configure {env_var}")

    if "backend.db.session" in sys.modules:
        import backend.db.session as sess
        engine_url = str(sess.get_engine().url)
        verify_authorized_db(engine_url, caller_context="pytest_configure backend.db.session.engine")


@pytest.fixture(autouse=True)
def _protect_env_vars():
    """Snapshot and restore critical env vars and test DB configuration around every test."""
    saved = {k: os.environ.get(k) for k in _CRITICAL_ENV_VARS}
    had_smc_config = "smc_futures.smc_config" in sys.modules
    yield

    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            if k in ("DATABASE_URL", "TRADES_DB_PATH", "SMARTTRADE_DB_PATH"):
                verify_authorized_db(v, caller_context=f"_protect_env_vars restore {k}")
            os.environ[k] = v

    os.environ["SMARTTRADE_TEST_MODE"] = "1"
    os.environ["SMARTTRADE_DISABLE_DOTENV"] = "1"
    os.environ["SMARTTRADE_SKIP_APP_LIFESPAN"] = "1"

    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = _DEFAULT_TEST_URL
    if not os.environ.get("TRADES_DB_PATH"):
        os.environ["TRADES_DB_PATH"] = _ISOLATED_TEST_DB
    if not os.environ.get("SMARTTRADE_DB_PATH"):
        os.environ["SMARTTRADE_DB_PATH"] = _ISOLATED_TEST_DB

    for env_var in ("DATABASE_URL", "TRADES_DB_PATH", "SMARTTRADE_DB_PATH"):
        verify_authorized_db(os.environ.get(env_var, ""), caller_context=f"_protect_env_vars post-check {env_var}")

    # Restore default isolated SQLAlchemy engine and clear any test-level DI overrides
    configure_database(_DEFAULT_TEST_URL)

    if "backend.auth" in sys.modules:
        import backend.auth as auth_mod
        auth_mod._API_KEY = None

    if "backend.services.live_trades_service" in sys.modules:
        from backend.services.live_trades_service import set_live_trades_session_factory
        set_live_trades_session_factory(None)

    if "backend.services.positions_service" in sys.modules:
        from backend.services.positions_service import reset_positions_service
        reset_positions_service()

    if "backend.services.analytics_service" in sys.modules:
        from backend.services.analytics_service import set_analytics_session_factory
        set_analytics_session_factory(None)

    if had_smc_config and "smc_futures.smc_config" not in sys.modules:
        try:
            import importlib
            import smc_futures.smc_config
            importlib.reload(smc_futures.smc_config)
        except Exception:
            pass


@pytest.fixture
def memory_db_engine():
    """Explicit in-memory SQLAlchemy engine profile (zero disk access)."""
    from backend.db.base import Base
    eng = configure_database("sqlite:///:memory:")
    yield eng
    Base.metadata.drop_all(bind=eng)
    reset_database_configuration()
    configure_database(_DEFAULT_TEST_URL)


@pytest.fixture
def memory_live_trades_db(memory_db_engine):
    """Explicit in-memory live_trades + trade_fills database fixture."""
    from sqlalchemy.orm import sessionmaker
    from backend.services.live_trades_service import (
        init_live_trades_schema,
        set_live_trades_session_factory,
    )

    Session = sessionmaker(autocommit=False, autoflush=False, bind=memory_db_engine)
    init_live_trades_schema(target_engine=memory_db_engine)
    set_live_trades_session_factory(Session)
    try:
        yield memory_db_engine, Session
    finally:
        set_live_trades_session_factory(None)


@pytest.fixture
def memory_positions_db(memory_db_engine):
    """Explicit in-memory persisted_positions database fixture."""
    from sqlalchemy.orm import sessionmaker
    from backend.services.positions_service import (
        init_persisted_positions_db,
        set_positions_session_factory,
        reset_positions_service,
    )

    Session = sessionmaker(autocommit=False, autoflush=False, bind=memory_db_engine)
    init_persisted_positions_db(target_engine=memory_db_engine)
    set_positions_session_factory(Session)
    try:
        yield memory_db_engine, Session
    finally:
        reset_positions_service()


@pytest.fixture
def temp_file_db_profile(tmp_path, monkeypatch):
    """
    Explicit isolated file-backed SQLite profile for migration/WAL tests.
    Registers tmp_path explicitly with db_guard under the 'temp_file' profile
    and restores the previous profile and engine cleanly on teardown.
    """
    db_file = tmp_path / "isolated_file_test.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    prev_profile = set_db_profile("temp_file")
    authorize_temp_db_root(tmp_path)
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("TRADES_DB_PATH", str(db_file))
    monkeypatch.setenv("SMARTTRADE_DB_PATH", str(db_file))
    eng = configure_database(db_url)
    try:
        yield db_url, db_file, eng
    finally:
        reset_database_configuration()
        configure_database(_DEFAULT_TEST_URL)
        revoke_temp_db_root(tmp_path)
        set_db_profile(prev_profile)
