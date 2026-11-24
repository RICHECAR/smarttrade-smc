"""
Database Session Configuration - SQLAlchemy (Lazy & Test-Isolated)

Supports SQLite (default in production) and PostgreSQL via DATABASE_URL env var.
No engine or directory is created at module import time.
"""

import logging
import os
import threading
import urllib.parse
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db.base import Base
from core.env_loader import is_test_mode, safe_load_dotenv

logger = logging.getLogger(__name__)

_DEFAULT_PROD_DB_URL = "sqlite:///./data/trades.db"
_FORBIDDEN_PROD_DB_NAMES = frozenset({
    "trades.db",
    "candles.db",
    "balance_history.sqlite3",
    "forensic_trades_backup.db",
})
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PROD_DATA_DIR = (_PROJECT_ROOT / "data").resolve(strict=False)

_lock = threading.RLock()
_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None
_configured_url: Optional[str] = None


def _parse_sqlalchemy_url(database_url: str, caller_context: str = "") -> URL:
    """Parse database_url strictly with SQLAlchemy make_url(); fail closed on ambiguity."""
    if not database_url or not isinstance(database_url, str) or not database_url.strip():
        raise RuntimeError(f"SECURITY BLOCK ({caller_context or 'session'}): Invalid or empty DATABASE_URL")
    if "\x00" in database_url:
        raise RuntimeError(f"SECURITY BLOCK ({caller_context or 'session'}): Null byte in DATABASE_URL")
    try:
        return make_url(database_url.strip())
    except (ArgumentError, ValueError, TypeError) as exc:
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): "
            f"Unparseable or ambiguous DATABASE_URL {database_url!r}: {exc}"
        ) from exc


def _unquote_strict(value: str, caller_context: str = "") -> str:
    """Iteratively URL-decode percent-encoded segments and reject embedded null bytes."""
    current = value
    for _ in range(3):
        decoded = urllib.parse.unquote(current)
        if "\x00" in decoded:
            raise RuntimeError(
                f"SECURITY BLOCK ({caller_context or 'session'}): Null byte after URL decoding"
            )
        if decoded == current:
            break
        current = decoded
    return current


def _validate_sqlite_memory_query(query_dict: dict, require_uri: bool, caller_context: str = "") -> None:
    """
    Validate query parameters on an in-memory SQLite URL.
    - For `sqlite:///:memory:` / `sqlite://`: `uri` is optional, only `cache=shared`, `mode=memory`,
      and `uri=true` are permitted if query parameters are present.
    - For `sqlite:///file::memory:...`: `uri=true` is MANDATORY (without `uri=true`, SQLAlchemy
      pysqlite resolves `os.path.abspath('file::memory:')` as a disk file in CWD!).
    """
    if require_uri:
        uri_val = str(query_dict.get("uri", "")).strip().lower()
        if uri_val != "true":
            raise RuntimeError(
                f"SECURITY BLOCK ({caller_context or 'session'}): "
                "SQLite 'file::memory:' requires explicit '?uri=true' in query string; "
                "without uri=true, SQLAlchemy/SQLite treats 'file::memory:' as a physical file path."
            )

    allowed_keys = {"uri", "cache", "mode"}
    unknown_keys = set(query_dict.keys()) - allowed_keys
    if unknown_keys:
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): "
            f"Unsupported query parameters {sorted(unknown_keys)!r} on in-memory SQLite URL."
        )
    if "uri" in query_dict and str(query_dict["uri"]).strip().lower() != "true":
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): "
            f"Invalid uri={query_dict['uri']!r} on in-memory SQLite URL (must be 'true')."
        )
    if "cache" in query_dict and str(query_dict["cache"]).strip().lower() != "shared":
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): "
            f"Invalid cache={query_dict['cache']!r} on in-memory SQLite URL (must be 'shared')."
        )
    if "mode" in query_dict and str(query_dict["mode"]).strip().lower() != "memory":
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): "
            f"Invalid mode={query_dict['mode']!r} on in-memory SQLite URL (must be 'memory')."
        )


def _is_sqlite_memory_url_obj(url: URL, caller_context: str = "") -> bool:
    """
    Return True ONLY if parsed SQLAlchemy URL targets a validated in-memory SQLite database:
      - `sqlite://` or `sqlite:///` (url.database is None or "")
      - `sqlite:///:memory:` (url.database is literally ":memory:")
      - `sqlite:///file::memory:?uri=true` or `sqlite:///file::memory:?cache=shared&uri=true`
        (url.database is literally "file::memory:" AND query explicitly contains `uri=true`).
    Raises RuntimeError if `file::memory:` is used without `uri=true` or with invalid query params,
    or if `:memory:` has forbidden query parameters (e.g. `?mode=ro`).
    Never accepts prefixes such as `:memory:evil` or `file::memory:evil`.
    """
    if url.get_backend_name() != "sqlite":
        return False
    db = url.database
    query_dict = dict(url.query)
    if db is None or db == "":
        _validate_sqlite_memory_query(query_dict, require_uri=False, caller_context=caller_context)
        return True
    db_clean = _unquote_strict(db.strip(), caller_context=caller_context or "_is_sqlite_memory_url_obj")
    if "?" in db_clean:
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): "
            f"Embedded '?' inside SQLite database target {db!r} is forbidden."
        )
    if db_clean == ":memory:":
        _validate_sqlite_memory_query(query_dict, require_uri=False, caller_context=caller_context)
        return True
    if db_clean.lower() == "file::memory:":
        _validate_sqlite_memory_query(query_dict, require_uri=True, caller_context=caller_context)
        return True
    return False


def _is_deceptive_memory_prefix(raw_db: str) -> bool:
    """
    Return True if raw_db starts with ':memory:' or 'file::memory:' without exact match,
    or if a path with directory separators has a basename starting with ':memory:' or 'file::memory:'
    (e.g. `<cwd>/file::memory:` produced by SQLAlchemy when `uri=true` is missing).
    """
    clean = raw_db.split("?", 1)[0].strip()
    norm_slashes = clean.replace("\\", "/")
    low = norm_slashes.lower()
    if low.startswith(":memory:") and norm_slashes != ":memory:":
        return True
    if low.startswith("file::memory:") and low != "file::memory:":
        return True
    if "/" in norm_slashes:
        base_low = norm_slashes.split("/")[-1].lower()
        if base_low.startswith((":memory:", "file::memory:")):
            return True
    return False


def _is_sqlite_memory_url(database_url: str) -> bool:
    """Return True if database_url targets an in-memory SQLite database."""
    try:
        url = _parse_sqlalchemy_url(database_url, caller_context="_is_sqlite_memory_url")
    except RuntimeError:
        return False
    return _is_sqlite_memory_url_obj(url)


def _extract_sqlite_real_path(url: URL, caller_context: str = "") -> Path:
    """
    Resolve the real filesystem Path for a file-backed SQLite SQLAlchemy URL.
    Handles Windows drive prefixes, relative paths, file: URIs, and query params.
    Fails closed if normalization fails.
    """
    raw_db = url.database
    if not raw_db or not isinstance(raw_db, str):
        raise RuntimeError(f"Missing SQLite database path ({caller_context or 'session'})")

    db_str = _unquote_strict(raw_db.strip(), caller_context=caller_context)
    if _is_deceptive_memory_prefix(db_str):
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): "
            f"Deceptive ':memory:' prefix in SQLite target {raw_db!r} is forbidden."
        )
    if db_str.lower().startswith("file:"):
        parsed = urllib.parse.urlparse(db_str)
        db_str = (parsed.netloc + parsed.path) if parsed.netloc else parsed.path
        db_str = _unquote_strict(db_str, caller_context=caller_context)
        if _is_deceptive_memory_prefix(db_str):
            raise RuntimeError(
                f"SECURITY BLOCK ({caller_context or 'session'}): "
                f"Deceptive 'file::memory:' prefix in SQLite target {raw_db!r} is forbidden."
            )

    # Strip any residual query string embedded inside database path
    if "?" in db_str:
        db_str = db_str.split("?", 1)[0]
        db_str = _unquote_strict(db_str, caller_context=caller_context)

    # Normalize Windows drive letter form (/C:/path -> C:/path) while preserving Unix /tmp/path
    if len(db_str) >= 3 and db_str[0] in ("/", "\\") and db_str[2] == ":" and db_str[1].isalpha():
        db_str = db_str[1:]

    if not db_str:
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): Empty SQLite path after URI normalization"
        )

    if _is_deceptive_memory_prefix(db_str):
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): "
            f"Deceptive ':memory:' prefix in SQLite target {raw_db!r} is forbidden."
        )

    try:
        candidate = Path(db_str)
        if not candidate.is_absolute() and not db_str.startswith(("/", "\\")):
            candidate = _PROJECT_ROOT / candidate
        return Path(os.path.realpath(str(candidate))).resolve(strict=False)
    except Exception as exc:
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): "
            f"Failed to normalize SQLite path {raw_db!r}: {exc}"
        ) from exc


def assert_database_url_allowed(database_url: str, caller_context: str = "") -> bool:
    """
    Validate database URL using SQLAlchemy make_url() before engine construction or connection.
    In test mode:
      - Rejects all non-SQLite dialects (PostgreSQL, MySQL, MariaDB, MSSQL, Oracle, etc.) by default.
      - Allows in-memory SQLite URLs strictly (sqlite://, sqlite:///:memory:, sqlite:///file::memory:).
      - Rejects deceptive prefixes such as :memory:evil or file::memory:evil.
      - Normalizes real filesystem paths and strips query params (?mode=ro cannot bypass).
      - Strictly forbids production DBs (trades.db, candles.db, balance_history.sqlite3,
        forensic_trades_backup.db) and any path inside the repository data/ directory.
      - Delegates to tests.db_guard.verify_authorized_db() when loaded so active temp_file / memory
        profile is enforced before any directory creation or engine construction.
    """
    url = _parse_sqlalchemy_url(database_url, caller_context=caller_context)

    if not is_test_mode():
        return True

    backend_name = url.get_backend_name()
    if backend_name != "sqlite":
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): "
            f"Non-SQLite dialect {backend_name!r} is forbidden in test mode (url={database_url!r})."
        )

    if _is_sqlite_memory_url_obj(url, caller_context=caller_context):
        return True

    real_path = _extract_sqlite_real_path(url, caller_context=caller_context)
    base_name = real_path.name.lower()

    if base_name.startswith((":memory:", "file::memory:")):
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): "
            f"Filesystem path with memory-like basename {base_name!r} is forbidden "
            f"(missing '?uri=true' or deceptive memory filename: url={database_url!r})."
        )

    if base_name in _FORBIDDEN_PROD_DB_NAMES:
        raise RuntimeError(
            f"SECURITY BLOCK ({caller_context or 'session'}): "
            f"Forbidden production/backup database {base_name!r} in test mode (url={database_url!r}, resolved={real_path})."
        )

    try:
        if real_path.is_relative_to(_PROD_DATA_DIR):
            raise RuntimeError(
                f"SECURITY BLOCK ({caller_context or 'session'}): "
                f"SQLite path inside production data/ directory is forbidden in test mode ({real_path})."
            )
    except ValueError:
        pass

    # Defense-in-depth: reject query parameter values referencing forbidden DB names
    for qk, qv in url.query.items():
        qv_str = _unquote_strict(str(qv), caller_context=caller_context).replace("\\", "/").lower()
        for forbidden in _FORBIDDEN_PROD_DB_NAMES:
            if forbidden in qv_str:
                raise RuntimeError(
                    f"SECURITY BLOCK ({caller_context or 'session'}): "
                    f"Query parameter {qk!r} references forbidden DB {forbidden!r}."
                )

    # Enforce active db_guard profile (memory vs temp_file authorized root) before any FS effect
    import sys as _sys
    db_guard_mod = _sys.modules.get("tests.db_guard")
    if db_guard_mod is not None and hasattr(db_guard_mod, "verify_authorized_db"):
        db_guard_mod.verify_authorized_db(database_url, caller_context=caller_context or "session")

    return True


def resolve_database_url(explicit_url: Optional[str] = None) -> str:
    """
    Resolve effective DATABASE_URL lazily.
    In test mode, never falls back implicitly to ./data/trades.db.
    """
    if explicit_url:
        assert_database_url_allowed(explicit_url, caller_context="resolve_database_url(explicit)")
        return explicit_url

    if _configured_url:
        assert_database_url_allowed(_configured_url, caller_context="resolve_database_url(configured)")
        return _configured_url

    safe_load_dotenv()
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        assert_database_url_allowed(env_url, caller_context="resolve_database_url(env)")
        return env_url

    if is_test_mode():
        raise RuntimeError(
            "SMARTTRADE_TEST_MODE active: DATABASE_URL must be explicitly configured "
            "(fallback to sqlite:///./data/trades.db is forbidden)."
        )

    return _DEFAULT_PROD_DB_URL


def create_configured_engine(database_url: str) -> Engine:
    """Create a SQLAlchemy Engine for database_url with safe SQLite hooks."""
    assert_database_url_allowed(database_url, caller_context="create_configured_engine")
    url = _parse_sqlalchemy_url(database_url, caller_context="create_configured_engine")

    is_sqlite = url.get_backend_name() == "sqlite"
    is_memory = _is_sqlite_memory_url_obj(url)

    engine_kwargs = {
        "pool_pre_ping": True,
        "echo": False,
    }

    if is_sqlite:
        engine_kwargs["connect_args"] = {"check_same_thread": False}
        if is_memory:
            engine_kwargs["poolclass"] = StaticPool
        else:
            real_path = _extract_sqlite_real_path(url, caller_context="create_configured_engine")
            if not real_path.parent.exists():
                real_path.parent.mkdir(parents=True, exist_ok=True)
                logger.info(f"[DB] Created directory: {real_path.parent}")

    eng = create_engine(database_url, **engine_kwargs)

    if is_sqlite:
        @event.listens_for(eng, "do_connect")
        def _verify_before_connect(dialect, conn_rec, cargs, cparams):
            assert_database_url_allowed(database_url, caller_context="SQLAlchemy do_connect")

        if is_memory:
            @event.listens_for(eng, "connect")
            def _set_sqlite_memory_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        else:
            @event.listens_for(eng, "connect")
            def _set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=FULL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    return eng


def configure_database(database_url: Optional[str] = None) -> Engine:
    """Explicitly configure (or reconfigure) the global SQLAlchemy engine and session factory."""
    global _engine, _session_factory, _configured_url
    with _lock:
        url = resolve_database_url(database_url)
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception as e:
                logger.warning(f"[DB] Error disposing previous engine: {e}")
        _configured_url = url
        _engine = create_configured_engine(url)
        _session_factory = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=_engine)
        return _engine


def reset_database_configuration() -> None:
    """Dispose current engine and clear cached DB configuration (for isolated tests)."""
    global _engine, _session_factory, _configured_url
    with _lock:
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:
                pass
        _engine = None
        _session_factory = None
        _configured_url = None


def get_engine() -> Engine:
    """Return active Engine, initializing lazily from environment if needed."""
    with _lock:
        if _engine is None:
            configure_database()
        else:
            assert_database_url_allowed(_configured_url or "", caller_context="get_engine")
        return _engine


def get_session_factory() -> sessionmaker:
    """Return active sessionmaker, initializing lazily if needed."""
    with _lock:
        if _session_factory is None:
            configure_database()
        else:
            assert_database_url_allowed(_configured_url or "", caller_context="get_session_factory")
        return _session_factory


def get_db_url() -> str:
    """Return the active or resolved database URL."""
    return resolve_database_url()


def get_db():
    """FastAPI dependency yielding a session from the active session factory."""
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()


def init_db(target_engine: Optional[Engine] = None):
    """Initialize core trade and analytics tables explicitly (separate from migrations)."""
    from backend.models.trade import Trade  # noqa: F401
    from backend.models.analytics import (  # noqa: F401
        SystemStateSnapshot, SignalDecisionRecord,
        TradeAnalytics, RiskEvent, DailyAnalytics, WeeklyAnalytics,
        ExecutionAttempt, OpsEvent,
    )

    eng = target_engine or get_engine()
    Base.metadata.create_all(bind=eng)
    logger.info(f"[DB] Initialized database schema on engine={eng.url}")


def __getattr__(name: str):
    """
    Controlled backwards compatibility for legacy callers importing
    `engine`, `SessionLocal`, or `DATABASE_URL` from `backend.db.session`.
    """
    if name == "engine":
        return get_engine()
    if name == "SessionLocal":
        return get_session_factory()
    if name == "DATABASE_URL":
        return get_db_url()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
