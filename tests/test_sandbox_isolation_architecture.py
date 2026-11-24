"""
Non-regression tests for SmartTrade DB/App/Test Isolation Architecture (Lots A-D + AUD-01..AUD-06).

Covers all 12 mandatory non-regression invariants:
 1. Memory SQLite URLs (`sqlite://`, `sqlite:///:memory:`) accepted in memory profile.
 2. Authorized temp-file SQLite URL accepted ONLY under explicitly authorized temp root.
 3. Production DB paths (`data/trades.db`, `data/candles.db`, `data/balance_history.sqlite3`, `data/*`) rejected.
 4. Forensic backup (`forensic_trades_backup.db`) rejected everywhere.
 5. Forbidden SQLite URLs with query strings (`?mode=ro`, `?cache=shared`) rejected.
 6. Directory traversal (`..`) and symlinks escaping authorized temp root rejected.
 7. Injected `session_factory` in `get_positions_service()` strictly validated (no global fallback).
 8. Schema initialization occurs strictly on the injected engine (never global `get_engine()`).
 9. All 10 canonical REST routers + CORS + ApiKeyMiddleware + WS routes present on `create_api_app()`.
10. No premature `build_app_context()` execution on module import (`api_smc_engine` / `orchestrator`).
11. Standalone script mode compatibility (`test_position_close_reduce.run_all_tests()`).
12. Production `startup_event` / `shutdown_event` hooks preserved on `app` and `create_api_app(enable_startup_hooks=True)`.
"""

import inspect
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, inspect as sa_inspect
from sqlalchemy.orm import sessionmaker


def test_single_declarative_base_across_all_models():
    """All ORM models must share the exact same declarative Base instance."""
    from backend.db.base import Base as CanonicalBase
    from backend.db.session import Base as SessionBase
    from backend.models.trade import Base as TradeBase
    from backend.models.live_trade import Base as LiveTradeBase
    from backend.models.persisted_position import Base as PositionBase
    from backend.models.trade_fill import Base as FillBase
    from backend.models.analytics import Base as AnalyticsBase

    assert SessionBase is CanonicalBase
    assert TradeBase is CanonicalBase
    assert LiveTradeBase is CanonicalBase
    assert PositionBase is CanonicalBase
    assert FillBase is CanonicalBase
    assert AnalyticsBase is CanonicalBase


# ─────────────────────────────────────────────────────────────────────────────
# 1. Memory SQLite URLs (AUD-01 & AUD-02)
# ─────────────────────────────────────────────────────────────────────────────

def test_01_memory_sqlite_urls_accepted_and_deceptive_or_non_uri_file_memory_rejected(monkeypatch):
    """
    - Exact non-URI memory SQLite URLs (`sqlite://`, `sqlite:///`, `sqlite:///:memory:`) accepted.
    - `sqlite:///file::memory:...` accepted ONLY when `?uri=true` is explicitly present and valid.
    - Raw `file::memory:...` accepted ONLY when `uri_mode=True` is explicitly passed.
    - `sqlite:///file::memory:` or `sqlite:///file::memory:?cache=shared` WITHOUT `uri=true`,
      or with `uri=false` / `mode=ro` / unknown query params, or deceptive prefixes
      (`:memory:evil`, `file::memory:evil`) are strictly rejected by both session.py and db_guard.py.
    """
    from backend.db.session import assert_database_url_allowed, resolve_database_url
    from tests.db_guard import set_db_profile, verify_authorized_db

    monkeypatch.setenv("SMARTTRADE_TEST_MODE", "1")
    prev_profile = set_db_profile("memory")
    try:
        # 1. Valid SQLAlchemy memory URLs
        for url in (
            "sqlite://",
            "sqlite:///",
            "sqlite:///:memory:",
            "sqlite:///:memory:?cache=shared",
            "sqlite:///file::memory:?uri=true",
            "sqlite:///file::memory:?cache=shared&uri=true",
            "sqlite:///file::memory:?mode=memory&cache=shared&uri=true",
        ):
            assert assert_database_url_allowed(url, caller_context="unit_test") is True
            assert verify_authorized_db(url, caller_context="unit_test") is True

        # 2. Valid raw sqlite3 targets (using native SQLite URI syntax with uri_mode=True)
        assert verify_authorized_db(":memory:", caller_context="unit_test") is True
        assert verify_authorized_db("file::memory:", caller_context="unit_test", uri_mode=True) is True
        assert verify_authorized_db("file::memory:?cache=shared", caller_context="unit_test", uri_mode=True) is True
        assert (
            verify_authorized_db("file::memory:?mode=memory&cache=shared", caller_context="unit_test", uri_mode=True)
            is True
        )

        # 3. Raw `file::memory:` without `uri_mode=True` (including textual `?uri=true`) MUST be rejected!
        for raw_no_uri in (
            "file::memory:",
            "file::memory:?cache=shared",
            "file::memory:?uri=true",
            "file::memory:?cache=shared&uri=true",
        ):
            with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
                verify_authorized_db(raw_no_uri, caller_context="test_01_raw_no_uri", uri_mode=False)

        # 3b. Direct DBAPI `:memory:` with ANY query string MUST be rejected (even with uri_mode=True),
        #     because SQLite treats `:memory:?...` (without `file:` prefix) as a physical filename!
        for raw_mem_with_query in (
            ":memory:?cache=shared",
            ":memory:?mode=memory&cache=shared",
            ":memory:?foo=bar",
            ":memory:?",
        ):
            for uri_flag in (False, True):
                with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
                    verify_authorized_db(
                        raw_mem_with_query,
                        caller_context="test_01_raw_mem_query",
                        uri_mode=uri_flag,
                    )

        # 4. Deceptive prefixes and non-URI or malformed `file::memory:` URLs MUST be rejected by BOTH
        rejected_urls = (
            "sqlite:///file::memory:",
            "sqlite:///file::memory:?cache=shared",
            "sqlite:///file::memory:?uri=false",
            "sqlite:///file::memory:?uri=0",
            "sqlite:///file::memory:?uri=true&mode=ro",
            "sqlite:///file::memory:?uri=true&mode=rwc",
            "sqlite:///file::memory:?uri=true&foo=bar",
            "sqlite:///:memory:?mode=ro",
            "sqlite:///:memory:?foo=bar",
            "sqlite:///:memory:evil",
            "sqlite:///:memory:evil?cache=shared",
            "sqlite:///file::memory:evil",
            "sqlite:///file::memory:evil?uri=true",
            "sqlite:///file::memory:evil?cache=shared&uri=true",
        )
        for bad_url in rejected_urls:
            with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
                verify_authorized_db(bad_url, caller_context="test_01_rejected")
            with pytest.raises(RuntimeError, match="SECURITY BLOCK"):
                assert_database_url_allowed(bad_url, caller_context="test_01_rejected")

        for bad_raw in (":memory:evil", "file::memory:evil", "file::memory:evil?cache=shared"):
            with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
                verify_authorized_db(bad_raw, caller_context="test_01_rejected_raw", uri_mode=True)
    finally:
        set_db_profile(prev_profile)

    # Unset DATABASE_URL + clear configured url cannot silently fall back to ./data/trades.db
    import backend.db.session as sess
    prev_url = sess._configured_url
    try:
        sess._configured_url = None
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="DATABASE_URL must be explicitly configured"):
            resolve_database_url()
    finally:
        sess._configured_url = prev_url


# ─────────────────────────────────────────────────────────────────────────────
# 2. Authorized temp-file SQLite URL (AUD-01)
# ─────────────────────────────────────────────────────────────────────────────

def test_02_temp_file_profile_authorizes_only_scoped_temp_root(tmp_path, monkeypatch):
    """In `temp_file` profile, only DB files inside explicitly authorized temp roots are allowed."""
    from tests.db_guard import (
        authorize_temp_db_root,
        revoke_temp_db_root,
        set_db_profile,
        verify_authorized_db,
    )

    monkeypatch.setenv("SMARTTRADE_TEST_MODE", "1")
    authorized_dir = tmp_path / "allowed_root"
    outside_dir = tmp_path / "outside_root"
    authorized_dir.mkdir()
    outside_dir.mkdir()

    allowed_db = authorized_dir / "isolated_test.db"
    outside_db = outside_dir / "unauthorized.db"

    prev_profile = set_db_profile("memory")
    try:
        # In memory profile, even a file inside tmp_path is rejected
        with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
            verify_authorized_db(f"sqlite:///{allowed_db.as_posix()}", caller_context="test_02")

        set_db_profile("temp_file")
        # Without authorized root, temp_file profile still fails closed
        with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
            verify_authorized_db(f"sqlite:///{allowed_db.as_posix()}", caller_context="test_02")

        token = authorize_temp_db_root(authorized_dir)
        try:
            assert verify_authorized_db(
                f"sqlite:///{allowed_db.as_posix()}", caller_context="test_02"
            ) is True
            # Outside authorized root must still be blocked
            with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
                verify_authorized_db(
                    f"sqlite:///{outside_db.as_posix()}", caller_context="test_02"
                )
        finally:
            revoke_temp_db_root(token)
        # Outside root must be rejected BEFORE any parent directory is created on disk
        unauth_new_parent = outside_dir / "must_not_be_created_subdir"
        unauth_db = unauth_new_parent / "unauth.db"
        from backend.db.session import create_configured_engine
        from backend.db.migrations import ensure_schema_up_to_date

        with pytest.raises(RuntimeError):
            create_configured_engine(f"sqlite:///{unauth_db.as_posix()}")
        assert not unauth_new_parent.exists(), "Directory created before authorization check!"

        with pytest.raises(RuntimeError):
            ensure_schema_up_to_date(f"sqlite:///{unauth_db.as_posix()}")
        assert not unauth_new_parent.exists(), "Migration created directory before authorization check!"
    finally:
        set_db_profile(prev_profile)


def test_02b_non_sqlite_remote_dialects_and_unix_windows_paths(monkeypatch):
    """
    Remote dialects (PostgreSQL, MySQL, MariaDB, MSSQL, Oracle) must be blocked in test mode,
    and Unix 4-slash URLs (`sqlite:////tmp/test/smarttrade.db`) must keep their leading `/`.
    """
    from backend.db.session import assert_database_url_allowed
    from tests.db_guard import extract_sqlite_url_path, verify_authorized_db

    monkeypatch.setenv("SMARTTRADE_TEST_MODE", "1")
    remote_urls = (
        "postgresql://user:pass@localhost:5432/smarttrade",
        "postgresql+psycopg2://user:pass@127.0.0.1:5432/smarttrade",
        "mysql://user:pass@localhost:3306/smarttrade",
        "mysql+pymysql://user:pass@localhost:3306/smarttrade",
        "mariadb://user:pass@localhost:3306/smarttrade",
        "mssql+pyodbc://user:pass@localhost/smarttrade",
    )
    for r_url in remote_urls:
        with pytest.raises(RuntimeError, match="SECURITY BLOCK"):
            assert_database_url_allowed(r_url, caller_context="test_02b")
        with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
            verify_authorized_db(r_url, caller_context="test_02b")

    # Unix 4-slash absolute path must retain leading '/' (never relative to PROJECT_ROOT)
    assert extract_sqlite_url_path("sqlite:////tmp/test/smarttrade.db") == "/tmp/test/smarttrade.db"
    # Windows drive paths (3 or 4 slashes) must normalize to 'C:/tmp/test.db'
    assert extract_sqlite_url_path("sqlite:///C:/tmp/test.db") == "C:/tmp/test.db"
    assert extract_sqlite_url_path("sqlite:////C:/tmp/test.db") == "C:/tmp/test.db"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Forbidden production DB paths (AUD-01 & AUD-02)
# ─────────────────────────────────────────────────────────────────────────────

def test_03_forbidden_prod_paths_rejected(tmp_path, monkeypatch):
    """Production DB names and any path under `data/` are blocked in both session and db_guard."""
    from backend.db.session import assert_database_url_allowed
    from tests.db_guard import (
        authorize_temp_db_root,
        revoke_temp_db_root,
        set_db_profile,
        verify_authorized_db,
    )

    monkeypatch.setenv("SMARTTRADE_TEST_MODE", "1")
    forbidden_urls = [
        "sqlite:///./data/trades.db",
        "sqlite:///data/trades.db",
        "sqlite:///./data/candles.db",
        "sqlite:///./data/balance_history.sqlite3",
        "sqlite:///./data/any_other_prod.db",
    ]
    for url in forbidden_urls:
        with pytest.raises(RuntimeError, match="SECURITY BLOCK"):
            assert_database_url_allowed(url, caller_context="test_03")
        with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
            verify_authorized_db(url, caller_context="test_03")

    # Even in temp_file profile with an authorized root, prod DB filenames are still forbidden
    prev_profile = set_db_profile("temp_file")
    token = authorize_temp_db_root(tmp_path)
    try:
        for forbidden_name in ("trades.db", "candles.db", "balance_history.sqlite3"):
            bad_url = f"sqlite:///{(tmp_path / forbidden_name).as_posix()}"
            with pytest.raises(RuntimeError, match="SECURITY BLOCK"):
                assert_database_url_allowed(bad_url, caller_context="test_03")
            with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
                verify_authorized_db(bad_url, caller_context="test_03")
    finally:
        revoke_temp_db_root(token)
        set_db_profile(prev_profile)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Forbidden forensic backup (AUD-01 & AUD-02)
# ─────────────────────────────────────────────────────────────────────────────

def test_04_forensic_backup_rejected(tmp_path, monkeypatch):
    """`forensic_trades_backup.db` must be rejected everywhere (session + db_guard)."""
    from backend.db.session import assert_database_url_allowed
    from tests.db_guard import (
        authorize_temp_db_root,
        revoke_temp_db_root,
        set_db_profile,
        verify_authorized_db,
    )

    monkeypatch.setenv("SMARTTRADE_TEST_MODE", "1")
    prev_profile = set_db_profile("temp_file")
    token = authorize_temp_db_root(tmp_path)
    try:
        backup_urls = [
            "sqlite:///./data/forensic_trades_backup.db",
            "sqlite:///forensic_trades_backup.db",
            f"sqlite:///{(tmp_path / 'forensic_trades_backup.db').as_posix()}",
        ]
        for url in backup_urls:
            with pytest.raises(RuntimeError, match="SECURITY BLOCK"):
                assert_database_url_allowed(url, caller_context="test_04")
            with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
                verify_authorized_db(url, caller_context="test_04")
    finally:
        revoke_temp_db_root(token)
        set_db_profile(prev_profile)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Forbidden SQLite URL with query string (AUD-01 & AUD-02)
# ─────────────────────────────────────────────────────────────────────────────

def test_05_forbidden_sqlite_url_with_query_string_rejected(monkeypatch):
    """Query strings such as `?mode=ro` or `?uri=true` must never bypass DB guards."""
    from backend.db.session import assert_database_url_allowed
    from tests.db_guard import verify_authorized_db

    monkeypatch.setenv("SMARTTRADE_TEST_MODE", "1")
    bypass_attempts = [
        "sqlite:///./data/trades.db?mode=ro",
        "sqlite:///./data/trades.db?mode=ro&cache=shared",
        "sqlite:///./data/trades.db?mode=memory",
        "sqlite:///./data/trades%2edb?mode=ro",
        "sqlite:///file:./data/trades.db?mode=ro&uri=true",
        "sqlite:///./data/forensic_trades_backup.db?mode=ro",
        "sqlite:///./data/candles.db?uri=true",
        "file:./data/trades.db?mode=ro",
    ]
    for url in bypass_attempts:
        with pytest.raises(RuntimeError, match="SECURITY BLOCK"):
            assert_database_url_allowed(url, caller_context="test_05")
        with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
            verify_authorized_db(url, caller_context="test_05")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Directory traversal & symlink escape (AUD-01)
# ─────────────────────────────────────────────────────────────────────────────

def test_06_directory_traversal_and_symlink_escape_rejected(tmp_path, monkeypatch):
    """`..` traversal and symlinks pointing outside the authorized temp root must be blocked."""
    from tests.db_guard import (
        authorize_temp_db_root,
        revoke_temp_db_root,
        set_db_profile,
        verify_authorized_db,
    )

    monkeypatch.setenv("SMARTTRADE_TEST_MODE", "1")
    allowed_root = tmp_path / "sandbox_root"
    outside_dir = tmp_path / "escape_target"
    allowed_root.mkdir()
    outside_dir.mkdir()

    prev_profile = set_db_profile("temp_file")
    token = authorize_temp_db_root(allowed_root)
    try:
        traversal_url = f"sqlite:///{allowed_root.as_posix()}/../escape_target/evil.db"
        with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
            verify_authorized_db(traversal_url, caller_context="test_06_traversal")

        nested_traversal = f"sqlite:///{allowed_root.as_posix()}/sub/../../escape_target/evil.db"
        with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
            verify_authorized_db(nested_traversal, caller_context="test_06_nested_traversal")

        encoded_traversal = f"sqlite:///{allowed_root.as_posix()}/%2e%2e/escape_target/evil.db"
        with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
            verify_authorized_db(encoded_traversal, caller_context="test_06_encoded_traversal")

        # Symlink escape check (gracefully skipped only if OS denies symlink privilege)
        link_path = allowed_root / "escape_link.db"
        outside_file = outside_dir / "outside.db"
        outside_file.touch()
        try:
            os.symlink(outside_file, link_path)
        except OSError:
            pass
        else:
            symlink_url = f"sqlite:///{link_path.as_posix()}"
            with pytest.raises(RuntimeError, match="CRITICAL SECURITY VIOLATION"):
                verify_authorized_db(symlink_url, caller_context="test_06_symlink")
    finally:
        revoke_temp_db_root(token)
        set_db_profile(prev_profile)


# ─────────────────────────────────────────────────────────────────────────────
# 7 & 8. Injected session_factory & schema init on exact engine (AUD-06)
# ─────────────────────────────────────────────────────────────────────────────

def test_07_positions_service_injected_session_factory_validation():
    """get_positions_service(session_factory=...) refuses silent fallback and engine mismatch."""
    from backend.services.positions_service import (
        get_positions_service,
        reset_positions_service,
    )

    eng_a = create_engine("sqlite:///:memory:")
    eng_b = create_engine("sqlite:///:memory:")
    bound_factory = sessionmaker(bind=eng_a)
    unbound_factory = sessionmaker()

    try:
        # Unbound factory with initialize_schema=True and no target_engine -> ValueError
        with pytest.raises(ValueError, match="requires a bound session_factory or an explicit target_engine"):
            get_positions_service(session_factory=unbound_factory, initialize_schema=True)

        # Bound factory with conflicting target_engine -> ValueError
        with pytest.raises(ValueError, match="Conflicting engines"):
            get_positions_service(
                session_factory=bound_factory,
                target_engine=eng_b,
                initialize_schema=True,
            )

        # Unbound factory with initialize_schema=False succeeds cleanly
        svc = get_positions_service(session_factory=unbound_factory, initialize_schema=False)
        assert svc is not None
    finally:
        reset_positions_service()
        eng_a.dispose()
        eng_b.dispose()


def test_08_positions_service_initializes_schema_strictly_on_injected_engine():
    """Schema creation for injected session_factory must target ONLY its bound engine."""
    from backend.services.positions_service import (
        get_positions_service,
        reset_positions_service,
    )

    custom_engine = create_engine("sqlite:///:memory:")
    custom_factory = sessionmaker(bind=custom_engine)

    try:
        with patch(
            "backend.services.positions_service.get_engine",
            side_effect=AssertionError("Global get_engine() must NOT be called when session_factory is injected"),
        ):
            svc = get_positions_service(session_factory=custom_factory, initialize_schema=True)
            assert svc is not None
            tables = sa_inspect(custom_engine).get_table_names()
            assert "persisted_positions" in tables
    finally:
        reset_positions_service()
        custom_engine.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# 9. All 10 REST routers + CORS + Auth + WS present on create_api_app() (AUD-04)
# ─────────────────────────────────────────────────────────────────────────────

def _dep_tree_signature(dependant):
    """Build a recursive, deterministic signature of a FastAPI Dependant tree."""
    if dependant is None:
        return None
    return (
        getattr(dependant, "call", None),
        getattr(dependant, "name", None),
        getattr(dependant, "use_cache", True),
        tuple(_dep_tree_signature(sub) for sub in getattr(dependant, "dependencies", [])),
    )


def test_09_create_api_app_full_route_dependency_and_lifecycle_parity():
    """
    create_api_app() and create_legacy_app() must match production apps across:
    - exact route list, methods, endpoints, and recursive dependency graphs
    - middlewares (cls + kwargs)
    - exception_handlers, dependency_overrides, app.state, and FastAPI config parameters
    - zero duplicate WebSocket routes
    """
    from collections import Counter
    from smc_futures.api_smc_engine import _API_ROUTERS, app as prod_app, create_api_app
    from smc_futures.orchestrator import app as legacy_prod_app, create_legacy_app

    factory_app = create_api_app(enable_startup_hooks=False)

    assert len(_API_ROUTERS) == 10
    assert len(factory_app.routes) == len(prod_app.routes)

    doc_paths = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    for r_prod, r_fac in zip(prod_app.routes, factory_app.routes):
        assert type(r_prod) is type(r_fac)
        assert getattr(r_prod, "path", None) == getattr(r_fac, "path", None)
        assert getattr(r_prod, "methods", None) == getattr(r_fac, "methods", None)
        if getattr(r_prod, "path", None) in doc_paths:
            assert getattr(r_prod.endpoint, "__name__", None) == getattr(r_fac.endpoint, "__name__", None)
        else:
            assert getattr(r_prod, "endpoint", None) is getattr(r_fac, "endpoint", None)
        assert getattr(r_prod, "response_model", None) == getattr(r_fac, "response_model", None)
        assert getattr(r_prod, "status_code", None) == getattr(r_fac, "status_code", None)
        assert getattr(r_prod, "tags", None) == getattr(r_fac, "tags", None)
        assert _dep_tree_signature(getattr(r_prod, "dependant", None)) == _dep_tree_signature(
            getattr(r_fac, "dependant", None)
        )

    # FastAPI configuration & handlers parity
    for attr in ("title", "version", "description", "openapi_url", "docs_url", "redoc_url"):
        assert getattr(factory_app, attr) == getattr(prod_app, attr)
    assert factory_app.router.redirect_slashes == prod_app.router.redirect_slashes
    assert dict(factory_app.exception_handlers) == dict(prod_app.exception_handlers)
    assert dict(factory_app.dependency_overrides) == dict(prod_app.dependency_overrides)
    assert vars(factory_app.state) == vars(prod_app.state)
    assert [(m.cls, m.kwargs) for m in factory_app.user_middleware] == [
        (m.cls, m.kwargs) for m in prod_app.user_middleware
    ]

    # WebSocket endpoints present and never duplicated
    ws_routes = [r for r in factory_app.routes if "WebSocket" in type(r).__name__]
    ws_path_counts = Counter(r.path for r in ws_routes)
    assert ws_path_counts == {"/ws": 1, "/ws/trades": 1, "/ws/{path:path}": 1}

    # Legacy orchestrator app parity
    legacy_fac = create_legacy_app(enable_startup_hooks=False)
    assert len(legacy_fac.routes) == len(legacy_prod_app.routes)
    assert dict(legacy_fac.exception_handlers) == dict(legacy_prod_app.exception_handlers)
    assert dict(legacy_fac.dependency_overrides) == dict(legacy_prod_app.dependency_overrides)
    assert [(m.cls, m.kwargs) for m in legacy_fac.user_middleware] == [
        (m.cls, m.kwargs) for m in legacy_prod_app.user_middleware
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 10. No premature build_app_context() on module import (AUD-04)
# ─────────────────────────────────────────────────────────────────────────────

def test_10_no_premature_build_app_context_on_import():
    """Importing api_smc_engine and orchestrator must NOT eagerly call build_app_context()."""
    import smc_futures.api_smc_engine as api_mod
    import smc_futures.orchestrator as orch_mod

    assert isinstance(api_mod.ctx, api_mod._LazyAppContextProxy)
    assert isinstance(orch_mod.ctx, orch_mod._LazyAppContextProxy)
    # Static check: no module-level `ctx = build_app_context()` statement in either file
    api_src = Path(api_mod.__file__).read_text(encoding="utf-8")
    orch_src = Path(orch_mod.__file__).read_text(encoding="utf-8")
    assert "\nctx = build_app_context()" not in api_src
    assert "\nctx = build_app_context()" not in orch_src


# ─────────────────────────────────────────────────────────────────────────────
# 11. Standalone script mode compatibility (AUD-05)
# ─────────────────────────────────────────────────────────────────────────────

def test_11_position_close_reduce_script_mode_compatible():
    """run_all_tests() in test_position_close_reduce.py must wire isolated in-memory DB args."""
    import tests.test_position_close_reduce as mod

    src = inspect.getsource(mod.run_all_tests)
    assert "sqlite:///:memory:" in src
    assert "configure_database" in src
    assert "test_database_init(db_ctx)" in src
    assert "test_add_fill_flow(db_ctx)" in src
    assert "reset_database_configuration()" in src


# ─────────────────────────────────────────────────────────────────────────────
# 12. Production startup/shutdown hooks preserved (AUD-04)
# ─────────────────────────────────────────────────────────────────────────────

def test_12_production_startup_and_shutdown_hooks_preserved():
    """Production `app` and `create_api_app(enable_startup_hooks=True)` preserve startup/shutdown."""
    from smc_futures.api_smc_engine import (
        app as prod_app,
        create_api_app,
        shutdown_event,
        startup_event,
    )

    assert startup_event in prod_app.router.on_startup
    assert shutdown_event in prod_app.router.on_shutdown

    prod_like_app = create_api_app(enable_startup_hooks=True)
    assert startup_event in prod_like_app.router.on_startup
    assert shutdown_event in prod_like_app.router.on_shutdown

    isolated_app = create_api_app(enable_startup_hooks=False)
    assert len(isolated_app.router.on_startup) == 0
    assert len(isolated_app.router.on_shutdown) == 0

    ws_only_app = create_api_app(enable_startup_hooks=False, include_ws_only=True)
    assert len(ws_only_app.router.on_startup) == 0
    assert len(ws_only_app.router.on_shutdown) == 0
    ws_paths = {getattr(r, "path", None) for r in ws_only_app.routes}
    assert "/ws" in ws_paths
    assert "/ws/trades" in ws_paths


# ─────────────────────────────────────────────────────────────────────────────
# Additional core invariants (migrations, balance_history, orchestrator, auth, schemas)
# ─────────────────────────────────────────────────────────────────────────────

def test_migrations_reject_prod_db_and_remote_dialects_in_test_mode_and_skip_memory(monkeypatch):
    """Migrations must skip :memory: safely and block trades.db & remote dialects in test mode."""
    from backend.db.migrations import (
        CURRENT_SCHEMA_VERSION,
        ensure_all_columns_exist,
        ensure_schema_up_to_date,
        get_db_path_from_url,
        get_live_trades_schema_info,
    )

    monkeypatch.setenv("SMARTTRADE_TEST_MODE", "1")
    assert ensure_schema_up_to_date("sqlite:///:memory:") == (
        CURRENT_SCHEMA_VERSION,
        CURRENT_SCHEMA_VERSION,
    )
    assert ensure_all_columns_exist("sqlite:///:memory:") is False

    with pytest.raises(RuntimeError, match="SECURITY BLOCK"):
        ensure_schema_up_to_date("sqlite:///./data/trades.db")

    for remote_url in (
        "postgresql://user:pass@localhost:5432/smarttrade",
        "mysql+pymysql://user:pass@localhost:3306/smarttrade",
    ):
        with pytest.raises(RuntimeError, match="SECURITY BLOCK"):
            ensure_schema_up_to_date(remote_url)
        with pytest.raises(RuntimeError, match="SECURITY BLOCK"):
            ensure_all_columns_exist(remote_url)
        with pytest.raises(RuntimeError, match="SECURITY BLOCK"):
            get_db_path_from_url(remote_url)
        with pytest.raises(RuntimeError, match="SECURITY BLOCK"):
            get_live_trades_schema_info(remote_url)


def test_dotenv_disable_orthogonal_to_is_test_mode(monkeypatch):
    """SMARTTRADE_DISABLE_DOTENV=1 must disable .env reading without activating is_test_mode()."""
    from core.env_loader import is_dotenv_disabled, is_test_mode, safe_load_dotenv

    monkeypatch.setenv("SMARTTRADE_TEST_MODE", "0")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SMARTTRADE_DISABLE_DOTENV", "1")

    assert is_test_mode() is False
    assert is_dotenv_disabled() is True
    assert safe_load_dotenv() is False


def test_balance_history_service_blocks_default_prod_path_in_test_mode(monkeypatch):
    """BalanceHistoryService must refuse fallback to data/balance_history.sqlite3 in test mode."""
    from backend.services.balance_history_service import (
        BalanceHistoryService,
        reset_balance_history_service,
    )

    monkeypatch.setenv("SMARTTRADE_TEST_MODE", "1")
    monkeypatch.delenv("BALANCE_HISTORY_DB_PATH", raising=False)
    reset_balance_history_service()
    with pytest.raises(RuntimeError, match="requires an explicit db_path"):
        BalanceHistoryService()

    svc = BalanceHistoryService(db_path=":memory:")
    assert svc._db_path == ":memory:"
    reset_balance_history_service()


def test_orchestrator_init_db_blocked_in_test_mode(monkeypatch):
    """orchestrator.init_db() targeting data/candles.db must fail closed in test mode."""
    from smc_futures.orchestrator import init_db

    monkeypatch.setenv("SMARTTRADE_TEST_MODE", "1")
    with pytest.raises(RuntimeError, match="data/candles.db is forbidden in test mode"):
        init_db()


def test_auth_middleware_reads_api_key_dynamically_without_reload(monkeypatch):
    """ApiKeyMiddleware must reflect env changes dynamically without importlib.reload."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from backend.auth import ApiKeyMiddleware

    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware)

    @app.post("/mutate")
    async def mutate():
        return {"ok": True}

    client = TestClient(app)

    monkeypatch.setenv("DASHBOARD_API_KEY", "secret-abc")
    assert client.post("/mutate").status_code == 401
    assert client.post("/mutate", headers={"X-API-Key": "secret-abc"}).status_code == 200

    monkeypatch.setenv("DASHBOARD_API_KEY", "")
    assert client.post("/mutate").status_code == 200


def test_schemas_importable_without_app_side_effects():
    """Pydantic schemas must be importable directly from smc_futures.schemas.orders."""
    from smc_futures.schemas.orders import (
        DryRunOrderRequest,
        ForceOrderRequest,
        OrderRequest,
    )

    req = OrderRequest(symbol="BTCUSDT", side="buy", quantity=0.01)
    assert req.symbol == "BTCUSDT"
    assert DryRunOrderRequest is not None
    assert ForceOrderRequest is not None


# ─────────────────────────────────────────────────────────────────────────────
# 13. Contradictory Audit P1-A / P1-B / P1-C / P2-A / P2-B Non-Regression
# ─────────────────────────────────────────────────────────────────────────────

def test_13_p1a_bytes_and_pathlike_coercion_and_bytearray_rejection(tmp_path):
    """
    P1-A:
    - Never converts `bytes` via `str(database)` (`"b'data/trades.db'"`).
    - Decodes `bytes` and `os.PathLike` (returning `str` or `bytes`) via `os.fsdecode(os.fspath(...))`.
    - Explicitly rejects `bytearray` before connection (matching CPython `PyUnicode_FSConverter`).
    - Production database names (`b"data/trades.db"`, `Path("data/trades.db")`, `PathLike(b"...")`)
      are always detected as forbidden production databases in both `memory` and `temp_file` profiles.
    - `b":memory:"` normalizes cleanly to `":memory:"`.
    """
    from tests.db_guard import (
        UnauthorizedDatabaseAccessDenied,
        _assert_raw_dbapi_target_coherent,
        authorize_temp_db_root,
        normalize_db_target,
        revoke_temp_db_root,
        set_db_profile,
        verify_authorized_db,
    )

    class BytesPathLike(os.PathLike):
        def __init__(self, raw: bytes):
            self._raw = raw

        def __fspath__(self) -> bytes:
            return self._raw

    class StrPathLike(os.PathLike):
        def __init__(self, raw: str):
            self._raw = raw

        def __fspath__(self) -> str:
            return self._raw

    prev_profile = set_db_profile("memory")
    try:
        # 1. b":memory:" and PathLike(":memory:") accepted as in-memory
        assert normalize_db_target(b":memory:") == ":memory:"
        assert verify_authorized_db(b":memory:", caller_context="test_13_p1a") is True
        _assert_raw_dbapi_target_coherent(b":memory:", uri_mode=False, caller_context="test_13_p1a")

        assert normalize_db_target(BytesPathLike(b":memory:")) == ":memory:"
        assert verify_authorized_db(BytesPathLike(b":memory:"), caller_context="test_13_p1a") is True
        assert normalize_db_target(StrPathLike(":memory:")) == ":memory:"
        assert verify_authorized_db(StrPathLike(":memory:"), caller_context="test_13_p1a") is True

        # 2. bytearray is rejected by CPython PyUnicode_FSConverter and must be rejected by db_guard
        for bad_ba in (bytearray(b":memory:"), bytearray(b"data/trades.db")):
            with pytest.raises(UnauthorizedDatabaseAccessDenied, match="bytearray is forbidden"):
                normalize_db_target(bad_ba)
            with pytest.raises(UnauthorizedDatabaseAccessDenied, match="bytearray is forbidden"):
                verify_authorized_db(bad_ba, caller_context="test_13_p1a")
            with pytest.raises(UnauthorizedDatabaseAccessDenied, match="bytearray is forbidden"):
                _assert_raw_dbapi_target_coherent(bad_ba, uri_mode=False, caller_context="test_13_p1a")

        # 3. Production DB targets in bytes, Path, and PathLike(bytes/str) must be detected as production DBs
        set_db_profile("temp_file")
        token = authorize_temp_db_root(tmp_path)
        try:
            rel_prod_targets = (
                b"data/trades.db",
                Path("data/trades.db"),
                BytesPathLike(b"data/trades.db"),
                StrPathLike("data/trades.db"),
            )
            for target in rel_prod_targets:
                with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Relative or incomplete"):
                    normalize_db_target(target)
                with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Production database"):
                    verify_authorized_db(target, caller_context="test_13_p1a_prod_rel")

            abs_prod_targets = (
                os.fsencode(str(tmp_path / "trades.db")),
                tmp_path / "trades.db",
                BytesPathLike(os.fsencode(str(tmp_path / "trades.db"))),
                StrPathLike(str(tmp_path / "trades.db")),
            )
            for target in abs_prod_targets:
                # Must NEVER contain literal "b'" from str(bytes_obj)
                norm = normalize_db_target(target)
                assert "b'" not in norm
                assert Path(norm).name.lower() == "trades.db"
                with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Production database"):
                    verify_authorized_db(target, caller_context="test_13_p1a_prod_abs")

            # Authorized temp file in bytes / Path / PathLike(bytes/str) inside tmp_path
            safe_file = tmp_path / "safe_p1a.db"
            for ok_target in (
                str(safe_file),
                os.fsencode(str(safe_file)),
                safe_file,
                BytesPathLike(os.fsencode(str(safe_file))),
                StrPathLike(str(safe_file)),
            ):
                assert verify_authorized_db(ok_target, caller_context="test_13_p1a_safe") is True
                _assert_raw_dbapi_target_coherent(ok_target, uri_mode=False, caller_context="test_13_p1a_safe")
        finally:
            revoke_temp_db_root(token)
    finally:
        set_db_profile(prev_profile)


def test_14_p1b_temp_file_path_coherence_and_uri_distinction(tmp_path):
    """
    P1-B:
    - Strictly distinguishes SQLAlchemy URL, direct DBAPI path (`uri=False`), and native SQLite URI (`uri=True`).
    - When `uri=False`, never strips `file:`, never truncates `?query`, and never decodes `%xx`.
    - When `uri=True`, enforces SQLite native `file:` URI parsing rules and rejects `mode=memory` on file paths.
    - Guarantees raw DBAPI coherence for file targets (`norm != ":memory:"`) as well as `:memory:`.
    """
    from tests.db_guard import (
        UnauthorizedDatabaseAccessDenied,
        _assert_raw_dbapi_target_coherent,
        authorize_temp_db_root,
        revoke_temp_db_root,
        set_db_profile,
        verify_authorized_db,
    )

    auth_root = tmp_path / "auth_root"
    space_dir = auth_root / "dir with space"
    auth_root.mkdir()
    space_dir.mkdir()
    safe_db = auth_root / "isolated.db"
    space_db = space_dir / "space_test.db"

    prev_profile = set_db_profile("temp_file")
    token = authorize_temp_db_root(auth_root)
    try:
        # 1. Mandatory negative cases from P1-B specification (under both uri=False and uri=True)
        mandatory_rejected = (
            "file:./data/trades.db?mode=ro",
            "file:./data/trades.db?mode=memory",
            "file:./data%2ftrades.db",
            "./data/trades.db?mode=ro",
            "./%2e/data/trades.db",
            "./%2e%2e/data/trades.db",
            f"{auth_root.as_posix()}/%2e%2e/outside.db",
            f"{auth_root.as_posix()}/%2e/isolated.db",
            f"{auth_root.as_posix()}%2fisolated.db",
            f"file:///{safe_db.as_posix()}?mode=memory",
            f"{safe_db.as_posix()}?mode=ro",
            f"{space_dir.as_posix().replace(' ', '%20')}/space_test.db",
            f"file:///{space_dir.as_posix().replace(' ', '%20')}/space_test.db",
        )
        for bad_target in mandatory_rejected:
            for uri_flag in (False, True):
                with pytest.raises(UnauthorizedDatabaseAccessDenied):
                    verify_authorized_db(bad_target, caller_context="test_14_p1b", uri_mode=uri_flag)
                with pytest.raises(UnauthorizedDatabaseAccessDenied):
                    _assert_raw_dbapi_target_coherent(bad_target, uri_mode=uri_flag, caller_context="test_14_p1b")

        # 2. A native `file:` URI to an authorized temp file is REJECTED when uri=False,
        #    and ACCEPTED when uri=True (with valid non-memory query or no query)
        native_file_uri = f"file:///{safe_db.as_posix()}"
        native_file_uri_ro = f"file:///{safe_db.as_posix()}?mode=ro"
        with pytest.raises(UnauthorizedDatabaseAccessDenied):
            verify_authorized_db(native_file_uri, caller_context="test_14_p1b_uri_false", uri_mode=False)
        with pytest.raises(UnauthorizedDatabaseAccessDenied):
            _assert_raw_dbapi_target_coherent(native_file_uri, uri_mode=False, caller_context="test_14_p1b_uri_false")

        assert verify_authorized_db(native_file_uri, caller_context="test_14_p1b_uri_true", uri_mode=True) is True
        _assert_raw_dbapi_target_coherent(native_file_uri, uri_mode=True, caller_context="test_14_p1b_uri_true")
        assert verify_authorized_db(native_file_uri_ro, caller_context="test_14_p1b_uri_true_ro", uri_mode=True) is True
        _assert_raw_dbapi_target_coherent(native_file_uri_ro, uri_mode=True, caller_context="test_14_p1b_uri_true_ro")

        # 3. A SQLAlchemy URL (`sqlite:///...`) is accepted by verify_authorized_db for engine URLs
        #    but REJECTED by _assert_raw_dbapi_target_coherent if passed directly to sqlite3.connect!
        sa_url = f"sqlite:///{safe_db.as_posix()}"
        assert verify_authorized_db(sa_url, caller_context="test_14_p1b_sa") is True
        with pytest.raises(UnauthorizedDatabaseAccessDenied, match="SQLAlchemy URL"):
            _assert_raw_dbapi_target_coherent(sa_url, uri_mode=False, caller_context="test_14_p1b_raw_sa")

        # 4. Windows absolute paths and paths with literal internal spaces inside auth_root succeed
        for valid_direct in (str(safe_db), safe_db.as_posix(), str(space_db), space_db.as_posix()):
            assert verify_authorized_db(valid_direct, caller_context="test_14_p1b_direct", uri_mode=False) is True
            _assert_raw_dbapi_target_coherent(valid_direct, uri_mode=False, caller_context="test_14_p1b_direct")
    finally:
        revoke_temp_db_root(token)
        set_db_profile(prev_profile)


def test_15_p1c_uri_boolean_semantics_positional_and_keyword():
    """
    P1-C:
    - `uri=True` and `uri=1` evaluate to `True` (matching CPython `PyObject_IsTrue`).
    - `uri=False` and `uri=0` evaluate to `False` (matching CPython `PyObject_IsTrue`).
    - Ambiguous non-boolean values (`uri="true"`, `uri="0"`, `uri=[]`, or objects raising in `__bool__`)
      are rejected fail-closed before calling the underlying SQLite connect primitive.
    - Handles both keyword `uri=...` and positional `args[7]`, and rejects simultaneous positional + keyword.
    """
    from tests.db_guard import (
        GuardedSqlite3Connection,
        UnauthorizedDatabaseAccessDenied,
        _extract_effective_uri_flag,
        set_db_profile,
        verify_authorized_db,
    )

    class ExplodingBool:
        def __bool__(self):
            raise RuntimeError("boom in __bool__")

    prev_profile = set_db_profile("memory")
    try:
        # 1. Explicit boolean and integer 0/1 (keyword and positional args[7])
        assert _extract_effective_uri_flag((":memory:",), {"uri": True}, "test_15") is True
        assert _extract_effective_uri_flag((":memory:",), {"uri": 1}, "test_15") is True
        assert _extract_effective_uri_flag((":memory:",), {"uri": False}, "test_15") is False
        assert _extract_effective_uri_flag((":memory:",), {"uri": 0}, "test_15") is False

        # Positional args[7] (8th positional parameter)
        pos_prefix = ("file::memory:", 5.0, 0, "", True, GuardedSqlite3Connection, 128)
        assert _extract_effective_uri_flag((*pos_prefix, True), {}, "test_15_pos") is True
        assert _extract_effective_uri_flag((*pos_prefix, 1), {}, "test_15_pos") is True
        assert _extract_effective_uri_flag((*pos_prefix, False), {}, "test_15_pos") is False
        assert _extract_effective_uri_flag((*pos_prefix, 0), {}, "test_15_pos") is False

        # Simultaneous positional args[7] + keyword 'uri' -> rejected
        with pytest.raises(UnauthorizedDatabaseAccessDenied, match="both by position"):
            _extract_effective_uri_flag((*pos_prefix, True), {"uri": True}, "test_15_dup")

        # 2. verify_authorized_db with uri_mode=True/1 vs False/0 on file::memory:
        assert verify_authorized_db("file::memory:", caller_context="test_15", uri_mode=True) is True
        assert verify_authorized_db("file::memory:", caller_context="test_15", uri_mode=1) is True
        with pytest.raises(UnauthorizedDatabaseAccessDenied):
            verify_authorized_db("file::memory:", caller_context="test_15", uri_mode=False)
        with pytest.raises(UnauthorizedDatabaseAccessDenied):
            verify_authorized_db("file::memory:", caller_context="test_15", uri_mode=0)

        # 3. Non-boolean ambiguous values ("true", "0", [], ExplodingBool) rejected fail-closed
        for bad_uri in ("true", "0", [], ExplodingBool()):
            with pytest.raises(UnauthorizedDatabaseAccessDenied):
                _extract_effective_uri_flag(("file::memory:",), {"uri": bad_uri}, "test_15_bad_kw")
            with pytest.raises(UnauthorizedDatabaseAccessDenied):
                _extract_effective_uri_flag((*pos_prefix, bad_uri), {}, "test_15_bad_pos")
            with pytest.raises(UnauthorizedDatabaseAccessDenied):
                verify_authorized_db("file::memory:", caller_context="test_15_bad_verify", uri_mode=bad_uri)
    finally:
        set_db_profile(prev_profile)


def test_16_p2a_sqlalchemy_do_connect_cparams_validation():
    """
    P2-A:
    - `_validate_sqlalchemy_do_connect` validates only target keys (`database`, `db`, `filename`)
      in `cparams` as database paths.
    - Non-target options (`isolation_level="IMMEDIATE"`, `isolation_level=""`, `timeout=10`, `uri=True`)
      are NOT misinterpreted as database file paths.
    - Unknown `cparams` keys are rejected fail-closed.
    - Zero SQLAlchemy `Engine` creation or SQLite connection needed.
    """
    from types import SimpleNamespace
    from tests.db_guard import (
        UnauthorizedDatabaseAccessDenied,
        _validate_sqlalchemy_do_connect,
        set_db_profile,
    )

    prev_profile = set_db_profile("memory")
    try:
        mem_dialect = SimpleNamespace(name="sqlite", _smarttrade_engine_url="sqlite:///:memory:")
        uri_mem_dialect = SimpleNamespace(
            name="sqlite",
            _smarttrade_engine_url="sqlite:///file::memory:?cache=shared&uri=true",
        )

        # 1. Required connect_args examples from P2-A specification
        _validate_sqlalchemy_do_connect(
            mem_dialect, None, [":memory:"], {"isolation_level": "IMMEDIATE"}
        )
        _validate_sqlalchemy_do_connect(
            mem_dialect, None, [":memory:"], {"isolation_level": ""}
        )
        _validate_sqlalchemy_do_connect(
            mem_dialect, None, [":memory:"], {"timeout": 10}
        )
        _validate_sqlalchemy_do_connect(
            uri_mem_dialect, None, ["file::memory:?cache=shared"], {"uri": True}
        )

        # 2. Target-carrying cparams keys (`database`, `db`, `filename`) ARE still validated
        with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Production database"):
            _validate_sqlalchemy_do_connect(
                mem_dialect, None, [":memory:"], {"database": "data/trades.db"}
            )

        # 3. Unknown connect_args keys are rejected fail-closed
        with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Unknown SQLite connect_args"):
            _validate_sqlalchemy_do_connect(
                mem_dialect, None, [":memory:"], {"unknown_param": "data/trades.db"}
            )

        # 4. Incoherent file::memory: without uri=True in cparams is still rejected
        with pytest.raises(UnauthorizedDatabaseAccessDenied, match="did not receive uri=True"):
            _validate_sqlalchemy_do_connect(
                uri_mem_dialect, None, ["file::memory:?cache=shared"], {"uri": False}
            )
    finally:
        set_db_profile(prev_profile)


def test_17_p2b_no_silent_whitespace_strip():
    """
    P2-B:
    - Leading or trailing whitespace in a database target (e.g. `" :memory: "`, `":memory: "`,
      `" file::memory: "`, `" data/trades.db "`) is NEVER silently stripped; it is rejected
      immediately as ambiguous across `normalize_db_target`, `verify_authorized_db`, and
      `_assert_raw_dbapi_target_coherent`.
    """
    from tests.db_guard import (
        UnauthorizedDatabaseAccessDenied,
        _assert_raw_dbapi_target_coherent,
        normalize_db_target,
        set_db_profile,
        verify_authorized_db,
    )

    prev_profile = set_db_profile("memory")
    try:
        padded_targets = (
            " :memory:",
            ":memory: ",
            " :memory: ",
            "\t:memory:",
            ":memory:\n",
            " file::memory:",
            "file::memory: ",
            " sqlite:///:memory: ",
            " data/trades.db ",
            b" :memory: ",
        )
        for padded in padded_targets:
            with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Leading or trailing whitespace"):
                normalize_db_target(padded, uri_mode=True)
            with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Leading or trailing whitespace"):
                verify_authorized_db(padded, caller_context="test_17_p2b", uri_mode=True)
            with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Leading or trailing whitespace"):
                _assert_raw_dbapi_target_coherent(padded, uri_mode=True, caller_context="test_17_p2b")
    finally:
        set_db_profile(prev_profile)


# ─────────────────────────────────────────────────────────────────────────────
# 14. Targeted Fix RES-1 (URI `#` fragment) & RES-2 (Relative path vs os.getcwd)
# ─────────────────────────────────────────────────────────────────────────────

def test_18_res1_file_uri_fragment_hash_strictly_rejected(tmp_path):
    """
    RES-1:
    - Any `#` fragment in a SQLite `file:` URI (in path or query string) MUST be rejected
      before path normalization across `normalize_db_target`, `verify_authorized_db`,
      `_extract_native_sqlite_file_uri_path`, and `_assert_raw_dbapi_target_coherent`.
    - Fragments are NEVER stripped to make a target appear valid.
    - Zero SQLite connection opened.
    """
    from tests.db_guard import (
        UnauthorizedDatabaseAccessDenied,
        _assert_raw_dbapi_target_coherent,
        _extract_native_sqlite_file_uri_path,
        authorize_temp_db_root,
        normalize_db_target,
        revoke_temp_db_root,
        set_db_profile,
        verify_authorized_db,
    )

    auth_root = tmp_path / "res1_auth_root"
    auth_root.mkdir()

    prev_profile = set_db_profile("temp_file")
    token = authorize_temp_db_root(auth_root)
    try:
        fragment_cases = (
            "file:./isolated.db#fragment",
            "file:./data/trades.db#fragment",
            "file:///C:/temp/isolated.db#fragment",
            "file:///C:/temp/trades.db#fragment",
            f"file:///{(auth_root / 'isolated.db').as_posix()}#fragment",
            f"file:///{(auth_root / 'trades.db').as_posix()}#fragment",
            f"file:///{(auth_root / 'isolated.db').as_posix()}?mode=ro#fragment",
            "file::memory:?cache=shared#fragment",
            "file::memory:#fragment",
        )
        for target in fragment_cases:
            for uri_flag in (False, True):
                with pytest.raises(UnauthorizedDatabaseAccessDenied):
                    normalize_db_target(target, uri_mode=uri_flag)
                with pytest.raises(UnauthorizedDatabaseAccessDenied):
                    verify_authorized_db(target, caller_context="test_18_res1", uri_mode=uri_flag)
                with pytest.raises(UnauthorizedDatabaseAccessDenied):
                    _assert_raw_dbapi_target_coherent(target, uri_mode=uri_flag, caller_context="test_18_res1")

        # Direct call to _extract_native_sqlite_file_uri_path rejects '#' before path extraction
        for uri_no_q in (
            "file:./isolated.db#fragment",
            "file:./data/trades.db#fragment",
            f"file:///{(auth_root / 'trades.db').as_posix()}#fragment",
        ):
            with pytest.raises(UnauthorizedDatabaseAccessDenied, match="URI fragment"):
                _extract_native_sqlite_file_uri_path(uri_no_q)
    finally:
        revoke_temp_db_root(token)
        set_db_profile(prev_profile)


def test_19_res2_1_option_a_reject_relative_dbapi_paths(tmp_path):
    """
    RES-2.1 (Option A — Elimination of os.getcwd() TOCTOU):
    Covers all 13 mandatory static test cases without opening any SQLite connection or creating any Engine:
     1. Simple relative DBAPI path -> REJECTED.
     2. Relative DBAPI path with `.` -> REJECTED.
     3. Relative DBAPI path with `..` -> REJECTED.
     4. Relative `file:` URI -> REJECTED.
     5. Relative `file:` URI with `mode=ro` -> REJECTED.
     6. Relative `file:` URI with `cache=shared` -> REJECTED.
     7. Windows drive-relative path (`C:foo.db`) -> REJECTED.
     8. Windows root-relative path (`\\foo.db`, `/foo.db`) -> REJECTED.
     9. Authorized absolute path -> ACCEPTED (unchanged).
    10. `:memory:` -> ACCEPTED (unchanged).
    11. Authorized memory URI (`file::memory:...`) -> ACCEPTED (unchanged).
    12. SQLAlchemy `uri=true` with relative URI -> REJECTED.
    13. SQLAlchemy without `uri=true`, coherent absolute DBAPI path -> ACCEPTED (unchanged);
        divergent DBAPI path -> REJECTED.
    """
    from types import SimpleNamespace
    from tests.db_guard import (
        UnauthorizedDatabaseAccessDenied,
        _assert_raw_dbapi_target_coherent,
        _resolve_canonical_os_path,
        _validate_sqlalchemy_do_connect,
        authorize_temp_db_root,
        normalize_db_target,
        revoke_temp_db_root,
        set_db_profile,
        verify_authorized_db,
    )

    auth_root = tmp_path / "res2_1_auth_root"
    auth_root.mkdir()
    abs_db = auth_root / "isolated.db"
    other_abs_db = auth_root / "other.db"

    prev_profile = set_db_profile("temp_file")
    token = authorize_temp_db_root(auth_root)
    try:
        # Ensure validation never relies on os.getcwd() to authorize relative paths
        with patch("os.getcwd", side_effect=AssertionError("os.getcwd() must NEVER be called to authorize paths!")):
            # 1-3. Relative DBAPI paths (simple, '.', '..') -> REJECTED
            for rel_target in ("relative.db", "sub/relative.db", "./relative.db", "./sub/relative.db", "../relative.db"):
                for uri_flag in (False, True):
                    with pytest.raises(UnauthorizedDatabaseAccessDenied):
                        normalize_db_target(rel_target, uri_mode=uri_flag)
                    with pytest.raises(UnauthorizedDatabaseAccessDenied):
                        verify_authorized_db(rel_target, caller_context="test_19_rel_dbapi", uri_mode=uri_flag)
                    with pytest.raises(UnauthorizedDatabaseAccessDenied):
                        _assert_raw_dbapi_target_coherent(rel_target, uri_mode=uri_flag, caller_context="test_19_rel_dbapi")

            # 4-6. Relative native `file:` URIs (plain, mode=ro, cache=shared, uri=true) -> REJECTED
            for rel_uri in (
                "file:relative.db",
                "file:./relative.db",
                "file:../relative.db",
                "file:./relative.db?mode=ro",
                "file:./relative.db?cache=shared",
                "file:./relative.db?uri=true",
            ):
                for uri_flag in (False, True):
                    with pytest.raises(UnauthorizedDatabaseAccessDenied):
                        normalize_db_target(rel_uri, uri_mode=uri_flag)
                    with pytest.raises(UnauthorizedDatabaseAccessDenied):
                        verify_authorized_db(rel_uri, caller_context="test_19_rel_uri", uri_mode=uri_flag)
                    with pytest.raises(UnauthorizedDatabaseAccessDenied):
                        _assert_raw_dbapi_target_coherent(rel_uri, uri_mode=uri_flag, caller_context="test_19_rel_uri")

            # 7. Windows drive-relative path (`C:foo.db`) -> REJECTED
            for drive_rel in ("C:foo.db", "C:sub\\foo.db"):
                with pytest.raises(UnauthorizedDatabaseAccessDenied):
                    _resolve_canonical_os_path(drive_rel)
                with pytest.raises(UnauthorizedDatabaseAccessDenied):
                    verify_authorized_db(drive_rel, caller_context="test_19_drive_rel")
                with pytest.raises(UnauthorizedDatabaseAccessDenied):
                    _assert_raw_dbapi_target_coherent(drive_rel, uri_mode=False, caller_context="test_19_drive_rel")

            # 8. Windows root-relative paths (`\foo.db`, `/foo.db`) -> REJECTED on Windows (`nt`)
            if os.name == "nt":
                for root_rel in ("\\foo.db", "/foo.db", "file:/foo.db"):
                    for uri_flag in (False, True):
                        with pytest.raises(UnauthorizedDatabaseAccessDenied):
                            verify_authorized_db(root_rel, caller_context="test_19_root_rel", uri_mode=uri_flag)
                        with pytest.raises(UnauthorizedDatabaseAccessDenied):
                            _assert_raw_dbapi_target_coherent(root_rel, uri_mode=uri_flag, caller_context="test_19_root_rel")

            # 12. SQLAlchemy `uri=true` with relative URI -> REJECTED (without os.getcwd)
            rel_uri_dialect = SimpleNamespace(
                name="sqlite",
                _smarttrade_engine_url="sqlite:///file:./relative.db?uri=true",
            )
            with pytest.raises(UnauthorizedDatabaseAccessDenied):
                _validate_sqlalchemy_do_connect(rel_uri_dialect, None, ["file:./relative.db"], {"uri": True})

        # 9. Authorized absolute paths (direct & native file:/// URI with uri=True) -> ACCEPTED
        for abs_target in (str(abs_db), abs_db.as_posix()):
            assert verify_authorized_db(abs_target, caller_context="test_19_abs_ok", uri_mode=False) is True
            _assert_raw_dbapi_target_coherent(abs_target, uri_mode=False, caller_context="test_19_abs_ok")
        abs_file_uri = f"file:///{abs_db.as_posix()}"
        assert verify_authorized_db(abs_file_uri, caller_context="test_19_abs_uri_ok", uri_mode=True) is True
        _assert_raw_dbapi_target_coherent(abs_file_uri, uri_mode=True, caller_context="test_19_abs_uri_ok")

        # 10. `:memory:` -> ACCEPTED
        assert verify_authorized_db(":memory:", caller_context="test_19_mem_ok") is True
        _assert_raw_dbapi_target_coherent(":memory:", uri_mode=False, caller_context="test_19_mem_ok")

        # 11. Authorized memory URIs -> ACCEPTED
        for mem_uri in ("file::memory:", "file::memory:?cache=shared", "file::memory:?mode=memory&cache=shared"):
            assert verify_authorized_db(mem_uri, caller_context="test_19_mem_uri_ok", uri_mode=True) is True
            _assert_raw_dbapi_target_coherent(mem_uri, uri_mode=True, caller_context="test_19_mem_uri_ok")
        assert verify_authorized_db("sqlite:///file::memory:?uri=true", caller_context="test_19_sa_mem_uri") is True

        # 13. SQLAlchemy without `uri=true`, coherent absolute DBAPI path -> ACCEPTED; relative cargs[0] -> REJECTED
        rel_sa_dialect = SimpleNamespace(
            name="sqlite",
            _smarttrade_engine_url="sqlite:///./isolated.db",
        )
        _validate_sqlalchemy_do_connect(rel_sa_dialect, None, [str(abs_db)], {})
        # Relative DBAPI cargs[0] without dialect absolutization -> REJECTED
        with pytest.raises(UnauthorizedDatabaseAccessDenied, match="non-absolute DBAPI target"):
            _validate_sqlalchemy_do_connect(rel_sa_dialect, None, ["./isolated.db"], {})
    finally:
        revoke_temp_db_root(token)
        set_db_profile(prev_profile)


def test_20_res2_2_approach_b_sqlalchemy_relative_url_validation(tmp_path):
    """
    RES-2.2 (Approach B — Authoritative absolute `cargs[0]` + independent checks on `eng_url`):
    Covers all 11 mandatory static cases without creating any Engine or opening any SQLite connection:
     1. Relative URL + authorized absolute `cargs[0]` -> ACCEPTED.
     2. Relative URL + relative `cargs[0]` -> REJECTED.
     3. Relative URL + empty/missing `cargs[0]` (`""`, `None`, `[]`) -> REJECTED.
     4. Relative URL + absolute `cargs[0]` outside authorized root -> REJECTED.
     5. Relative URL + `cargs[0]` pointing to a production DB -> REJECTED.
     6. Two distinct authorized roots containing the same filename -> both ACCEPTED.
     7. Relative URL traversing a symlink/NTFS junction (simulated via `os.path.realpath` mock) -> ACCEPTED.
     8. Relative URL with Windows 8.3 short name (simulated via `os.path.realpath` mock) -> ACCEPTED.
     9. Relative URL with `uri=true` -> REJECTED.
    10. Absolute URL incoherent with `cargs[0]` -> REJECTED.
    11. `:memory:` and `file::memory:` preserved -> ACCEPTED.
    """
    from types import SimpleNamespace
    from tests.db_guard import (
        UnauthorizedDatabaseAccessDenied,
        _validate_sqlalchemy_do_connect,
        authorize_temp_db_root,
        revoke_temp_db_root,
        set_db_profile,
    )

    auth_a = tmp_path / "auth_a"
    auth_b = tmp_path / "auth_b"
    outside_dir = tmp_path / "outside_dir"
    auth_a.mkdir()
    auth_b.mkdir()
    outside_dir.mkdir()

    prev_profile = set_db_profile("temp_file")
    tok_a = authorize_temp_db_root(auth_a)
    tok_b = authorize_temp_db_root(auth_b)
    try:
        rel_dialect = SimpleNamespace(name="sqlite", _smarttrade_engine_url="sqlite:///./app.db")

        # 1. Relative URL + authorized absolute cargs[0] -> ACCEPTED
        _validate_sqlalchemy_do_connect(rel_dialect, None, [str(auth_a / "app.db")], {})

        # 2. Relative URL + relative cargs[0] -> REJECTED
        for bad_rel in ("./app.db", "app.db", "sub/app.db"):
            with pytest.raises(UnauthorizedDatabaseAccessDenied):
                _validate_sqlalchemy_do_connect(rel_dialect, None, [bad_rel], {})

        # 3. Relative URL + empty/missing cargs[0] -> REJECTED
        for empty_cargs in ([], [""], [None]):
            with pytest.raises(UnauthorizedDatabaseAccessDenied):
                _validate_sqlalchemy_do_connect(rel_dialect, None, empty_cargs, {})

        # 4. Relative URL + absolute cargs[0] outside authorized root -> REJECTED
        with pytest.raises(UnauthorizedDatabaseAccessDenied):
            _validate_sqlalchemy_do_connect(rel_dialect, None, [str(outside_dir / "app.db")], {})

        # 5. Relative URL + cargs[0] pointing to a production DB -> REJECTED
        with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Production database"):
            _validate_sqlalchemy_do_connect(rel_dialect, None, [str(auth_a / "trades.db")], {})

        # 6. Two distinct authorized roots with the same filename -> both ACCEPTED
        same_dialect = SimpleNamespace(name="sqlite", _smarttrade_engine_url="sqlite:///./same.db")
        _validate_sqlalchemy_do_connect(same_dialect, None, [str(auth_a / "same.db")], {})
        _validate_sqlalchemy_do_connect(same_dialect, None, [str(auth_b / "same.db")], {})

        # 7. Relative URL traversing a symlink / NTFS junction (simulated without creating links) -> ACCEPTED
        link_dialect = SimpleNamespace(name="sqlite", _smarttrade_engine_url="sqlite:///./link_sub/foo.db")
        cargs_via_link = str(auth_a / "link_sub" / "foo.db")
        resolved_real = str(auth_a / "real_sub" / "foo.db")
        orig_realpath = os.path.realpath

        def _mock_realpath_symlink(p, *, strict=False):
            if str(p).lower().endswith("link_sub\\foo.db") or str(p).lower().endswith("link_sub/foo.db"):
                return resolved_real
            return orig_realpath(p, strict=strict)

        with patch("os.path.realpath", side_effect=_mock_realpath_symlink):
            _validate_sqlalchemy_do_connect(link_dialect, None, [cargs_via_link], {})

        # 8. Relative URL with Windows 8.3 short name (simulated without creating files) -> ACCEPTED
        sfn_dialect = SimpleNamespace(name="sqlite", _smarttrade_engine_url="sqlite:///./MYFOLD~1/foo.db")
        cargs_via_sfn = str(auth_a / "MYFOLD~1" / "foo.db")
        resolved_long = str(auth_a / "myfolderlongname" / "foo.db")

        def _mock_realpath_sfn(p, *, strict=False):
            if "myfold~1" in str(p).lower():
                return resolved_long
            return orig_realpath(p, strict=strict)

        with patch("os.path.realpath", side_effect=_mock_realpath_sfn):
            _validate_sqlalchemy_do_connect(sfn_dialect, None, [cargs_via_sfn], {})

        # 9. Relative URL with uri=true -> REJECTED
        rel_uri_dialect = SimpleNamespace(name="sqlite", _smarttrade_engine_url="sqlite:///file:./app.db?uri=true")
        with pytest.raises(UnauthorizedDatabaseAccessDenied):
            _validate_sqlalchemy_do_connect(rel_uri_dialect, None, ["file:./app.db"], {"uri": True})
        with pytest.raises(UnauthorizedDatabaseAccessDenied):
            _validate_sqlalchemy_do_connect(rel_dialect, None, [str(auth_a / "app.db")], {"uri": True})

        # 10. Absolute URL incoherent with cargs[0] -> REJECTED
        abs_dialect = SimpleNamespace(
            name="sqlite",
            _smarttrade_engine_url=f"sqlite:///{(auth_a / 'expected.db').as_posix()}",
        )
        with pytest.raises(UnauthorizedDatabaseAccessDenied, match="diverges from transmitted DBAPI"):
            _validate_sqlalchemy_do_connect(abs_dialect, None, [str(auth_a / "different.db")], {})

        # 11. `:memory:` and `file::memory:` preserved -> ACCEPTED
        mem_dialect = SimpleNamespace(name="sqlite", _smarttrade_engine_url="sqlite:///:memory:")
        uri_mem_dialect = SimpleNamespace(
            name="sqlite",
            _smarttrade_engine_url="sqlite:///file::memory:?cache=shared&uri=true",
        )
        _validate_sqlalchemy_do_connect(mem_dialect, None, [":memory:"], {})
        _validate_sqlalchemy_do_connect(uri_mem_dialect, None, ["file::memory:?cache=shared"], {"uri": True})
    finally:
        revoke_temp_db_root(tok_b)
        revoke_temp_db_root(tok_a)
        set_db_profile(prev_profile)


def test_21_res2_3_sqlalchemy_url_vs_cparams_uri_coherence(tmp_path):
    """
    RES-2.3 (P2 URI coherence between SQLAlchemy URL `?uri=...` and `cparams['uri']`):
    - URL `?uri=true` + `cparams['uri']=False` (or omitted) -> REJECTED (both relative and absolute URLs).
    - URL `?uri=true` + `cparams['uri']=True` -> follows existing policy (relative rejected, absolute accepted).
    - URL without `uri` + `cparams['uri']=False` -> preserved.
    - URL without `uri` + `cparams['uri']=True` -> preserved (relative rejected, absolute without `file:` accepted).
    - URL with duplicated `uri` parameter -> REJECTED.
    - `file:` and `file::memory:` URLs -> zero regression.
    Zero Engine created, zero SQLite connection opened.
    """
    from types import SimpleNamespace
    from tests.db_guard import (
        UnauthorizedDatabaseAccessDenied,
        _validate_sqlalchemy_do_connect,
        authorize_temp_db_root,
        revoke_temp_db_root,
        set_db_profile,
    )

    auth_root = tmp_path / "res2_3_auth"
    auth_root.mkdir()
    abs_db = auth_root / "app.db"

    prev_profile = set_db_profile("temp_file")
    token = authorize_temp_db_root(auth_root)
    try:
        # 1. URL ?uri=true + cparams['uri']=False (or absent) -> REJECTED (relative and absolute)
        rel_uri_true = SimpleNamespace(name="sqlite", _smarttrade_engine_url="sqlite:///./app.db?uri=true")
        abs_uri_true = SimpleNamespace(name="sqlite", _smarttrade_engine_url=f"sqlite:///{abs_db.as_posix()}?uri=true")
        for d in (rel_uri_true, abs_uri_true):
            for bad_cparams in ({"uri": False}, {}):
                with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Contradictory uri mode"):
                    _validate_sqlalchemy_do_connect(d, None, [str(abs_db)], bad_cparams)

        # 2. URL ?uri=true + cparams['uri']=True -> relative REJECTED, absolute ACCEPTED
        with pytest.raises(UnauthorizedDatabaseAccessDenied):
            _validate_sqlalchemy_do_connect(rel_uri_true, None, [str(abs_db)], {"uri": True})
        _validate_sqlalchemy_do_connect(abs_uri_true, None, [str(abs_db)], {"uri": True})

        # 3. URL without uri + cparams['uri']=False -> preserved (both relative & absolute ACCEPTED)
        rel_no_uri = SimpleNamespace(name="sqlite", _smarttrade_engine_url="sqlite:///./app.db")
        abs_no_uri = SimpleNamespace(name="sqlite", _smarttrade_engine_url=f"sqlite:///{abs_db.as_posix()}")
        _validate_sqlalchemy_do_connect(rel_no_uri, None, [str(abs_db)], {"uri": False})
        _validate_sqlalchemy_do_connect(abs_no_uri, None, [str(abs_db)], {"uri": False})

        # 4. URL without uri + cparams['uri']=True -> relative REJECTED, absolute without file: ACCEPTED
        with pytest.raises(UnauthorizedDatabaseAccessDenied):
            _validate_sqlalchemy_do_connect(rel_no_uri, None, [str(abs_db)], {"uri": True})
        _validate_sqlalchemy_do_connect(abs_no_uri, None, [str(abs_db)], {"uri": True})

        # 5. URL with duplicated uri parameter -> REJECTED
        for dup_url in (
            f"sqlite:///{abs_db.as_posix()}?uri=true&uri=false",
            f"sqlite:///{abs_db.as_posix()}?uri=true&uri=true",
            "sqlite:///./app.db?uri=true&uri=false",
        ):
            dup_dialect = SimpleNamespace(name="sqlite", _smarttrade_engine_url=dup_url)
            with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Duplicated 'uri' parameter"):
                _validate_sqlalchemy_do_connect(dup_dialect, None, [str(abs_db)], {"uri": True})

        # 6. file: and file::memory: -> zero regression
        abs_file_uri = SimpleNamespace(
            name="sqlite",
            _smarttrade_engine_url=f"sqlite:///file:///{abs_db.as_posix()}?uri=true",
        )
        _validate_sqlalchemy_do_connect(abs_file_uri, None, [f"file:///{abs_db.as_posix()}"], {"uri": True})
        mem_uri = SimpleNamespace(
            name="sqlite",
            _smarttrade_engine_url="sqlite:///file::memory:?cache=shared&uri=true",
        )
        _validate_sqlalchemy_do_connect(mem_uri, None, ["file::memory:?cache=shared"], {"uri": True})
    finally:
        revoke_temp_db_root(token)
        set_db_profile(prev_profile)


# ─────────────────────────────────────────────────────────────────────────────
# 15. RES-3.2: SQLAlchemy Bypass Hardening (Import Order, Checkout, creator=, Provenance)
# ─────────────────────────────────────────────────────────────────────────────

def test_22_res3_2_sqlalchemy_bypass_hardening_static():
    """
    RES-3.2 (purely static validator tests — zero Engine created, zero SQLite connection opened):
     1. Reused idle pre-existing unguarded connection -> REJECTED at Pool.checkout.
     2. Already-checked-out pre-existing connection -> cannot be neutralized after the fact;
        prevented structurally by top-level `sqlalchemy` import-order check in `tests/db_guard.py`.
     3. Checkout of unguarded connection or GuardedSqlite3Connection without `_authorizer_locked=True` -> REJECTED.
     4. Checkout of properly guarded connection (`GuardedSqlite3Connection` + `_authorizer_locked=True`) -> ACCEPTED.
     5. `creator=` via `sqlalchemy.create_engine` & `sqlalchemy.engine.create_engine` -> REJECTED.
     6. `create_engine` alias identity verified (`sqlalchemy.create_engine is sqlalchemy.engine.create_engine`).
     7. Direct `Engine(...)` construction (`_validate_direct_engine_init`) rejects non-Pool & non-SQLite/empty dialect.
     8. Empty or None dialect name in `_validate_sqlalchemy_do_connect` -> REJECTED.
     9. `eng_url is None` in `_validate_sqlalchemy_do_connect` -> REJECTED.
    10. Custom non-Pool `poolclass` / `pool` -> REJECTED.
    11. Valid `Pool` subclass (`issubclass(CustomPool, Pool)`) -> ACCEPTED and covered by `Pool.checkout`.
    """
    from types import SimpleNamespace
    import sqlalchemy
    import sqlalchemy.engine as sa_engine
    from sqlalchemy.engine import create as sa_create
    from sqlalchemy.pool import Pool
    from tests.db_guard import (
        GuardedSqlite3Connection,
        UnauthorizedDatabaseAccessDenied,
        _validate_create_engine_kwargs,
        _validate_direct_engine_init,
        _validate_pool_checkout,
        _validate_sqlalchemy_do_connect,
    )

    # 1 & 3. Idle pre-existing / unguarded connection at Pool.checkout -> REJECTED
    fake_raw_conn = SimpleNamespace(_authorizer_locked=True)
    with pytest.raises(UnauthorizedDatabaseAccessDenied, match="NOT a GuardedSqlite3Connection"):
        _validate_pool_checkout(fake_raw_conn, None, None)

    # GuardedSqlite3Connection instance created via __new__ (no SQLite connection opened) without lock -> REJECTED
    unlocked_conn = GuardedSqlite3Connection.__new__(GuardedSqlite3Connection)
    unlocked_conn._authorizer_locked = False
    with pytest.raises(UnauthorizedDatabaseAccessDenied, match="_authorizer_locked is True"):
        _validate_pool_checkout(unlocked_conn, None, None)

    # 4. GuardedSqlite3Connection with _authorizer_locked=True -> ACCEPTED
    locked_conn = GuardedSqlite3Connection.__new__(GuardedSqlite3Connection)
    locked_conn._authorizer_locked = True
    _validate_pool_checkout(locked_conn, None, None)

    # 2. Import-order guard present in tests/db_guard.py source (covers already-checked-out risk)
    guard_src = Path("tests/db_guard.py").read_text(encoding="utf-8")
    assert 'if "sqlalchemy" in sys.modules or "sqlalchemy.engine" in sys.modules:' in guard_src

    # 5 & 6. creator= rejected; all 3 create_engine entry points reference the same guarded function
    assert sqlalchemy.create_engine is sa_engine.create_engine
    assert sqlalchemy.create_engine is sa_create.create_engine
    with pytest.raises(UnauthorizedDatabaseAccessDenied, match="creator=.*FORBIDDEN"):
        _validate_create_engine_kwargs(("sqlite:///:memory:",), {"creator": lambda: None}, pool_base_cls=Pool)

    # 7. Direct Engine.__init__ validation rejects non-Pool and empty/non-SQLite dialect
    class NotAPool:
        pass

    class ValidPoolSubclass(Pool):
        pass

    sqlite_dialect = SimpleNamespace(name="sqlite")
    empty_dialect = SimpleNamespace(name="")
    pg_dialect = SimpleNamespace(name="postgresql")
    valid_pool_stub = ValidPoolSubclass.__new__(ValidPoolSubclass)

    with pytest.raises(UnauthorizedDatabaseAccessDenied, match="NOT an instance of Pool"):
        _validate_direct_engine_init(NotAPool(), sqlite_dialect, "sqlite:///:memory:", pool_base_cls=Pool)
    with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Non-SQLite or unnamed dialect"):
        _validate_direct_engine_init(valid_pool_stub, empty_dialect, "sqlite:///:memory:", pool_base_cls=Pool)
    with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Non-SQLite or unnamed dialect"):
        _validate_direct_engine_init(valid_pool_stub, pg_dialect, "postgresql://u:p@h/db", pool_base_cls=Pool)
    _validate_direct_engine_init(valid_pool_stub, sqlite_dialect, "sqlite:///:memory:", pool_base_cls=Pool)

    # 8. Empty or None dialect name in _validate_sqlalchemy_do_connect -> REJECTED
    for bad_dialect in (
        SimpleNamespace(name="", _smarttrade_engine_url="sqlite:///:memory:"),
        SimpleNamespace(name=None, _smarttrade_engine_url="sqlite:///:memory:"),
        SimpleNamespace(_smarttrade_engine_url="sqlite:///:memory:"),
    ):
        with pytest.raises(UnauthorizedDatabaseAccessDenied, match="Non-SQLite or unnamed SQLAlchemy dialect"):
            _validate_sqlalchemy_do_connect(bad_dialect, None, [":memory:"], {})

    # 9. eng_url is None in _validate_sqlalchemy_do_connect -> REJECTED
    no_url_dialect = SimpleNamespace(name="sqlite")
    with pytest.raises(UnauthorizedDatabaseAccessDenied, match="no verifiable URL provenance"):
        _validate_sqlalchemy_do_connect(no_url_dialect, None, [":memory:"], {})

    # 10 & 11. Custom non-Pool rejected; valid Pool subclass accepted
    with pytest.raises(UnauthorizedDatabaseAccessDenied, match="NOT a subclass of Pool"):
        _validate_create_engine_kwargs(("sqlite:///:memory:",), {"poolclass": NotAPool}, pool_base_cls=Pool)
    with pytest.raises(UnauthorizedDatabaseAccessDenied, match="NOT an instance of Pool"):
        _validate_create_engine_kwargs(("sqlite:///:memory:",), {"pool": NotAPool()}, pool_base_cls=Pool)
    _validate_create_engine_kwargs(("sqlite:///:memory:",), {"poolclass": ValidPoolSubclass}, pool_base_cls=Pool)
    _validate_create_engine_kwargs(("sqlite:///:memory:",), {"pool": valid_pool_stub}, pool_base_cls=Pool)
