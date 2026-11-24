"""
Physical Anti-Production Database Guard for Pytest Test Suites
==============================================================

Enforces a STRICT DEFAULT-DENY ALLOWLIST policy for all SQLite database operations:
- Supports two isolated profiles via SMARTTRADE_DB_PROFILE:
    - "memory" (default): Authorizes ONLY in-memory SQLite ('sqlite://', 'sqlite:///:memory:', ':memory:').
    - "temp_file": Authorizes in-memory SQLite AND file-backed SQLite strictly inside an
      explicitly registered authorized temporary root (normalizing Windows paths, blocking
      directory traversal '..', blocking symlinks escaping the root, and always blocking
      production DB filenames).
- Never globally authorizes .db or .sqlite extensions.
"""

import sys

# Check 1: Verify sqlite3 has not been imported before this security guard
if "sqlite3" in sys.modules or "_sqlite3" in sys.modules:
    raise RuntimeError(
        "FATAL SECURITY VIOLATION: sqlite3 or _sqlite3 was imported prior to tests.db_guard! "
        "Native C references cannot be safely intercepted. Aborting execution."
    )

# Check 2 (RES-3.2): Verify sqlalchemy has not been imported before this security guard.
# Deliberately restrictive fail-closed policy: even a bare `import sqlalchemy` without an
# Engine is rejected if it precedes `tests.db_guard`, because once SQLAlchemy is imported
# earlier in the process it is impossible to guarantee that no pre-existing Engine, idle
# pooled connection, or already-checked-out connection was created beforehand (and a guard
# installed late can NEVER neutralize an already-checked-out DBAPI connection).
if "sqlalchemy" in sys.modules or "sqlalchemy.engine" in sys.modules:
    raise RuntimeError(
        "FATAL SECURITY VIOLATION: sqlalchemy was imported prior to tests.db_guard! "
        "Pre-existing Engines, pooled connections, or already-checked-out connections cannot "
        "be safely intercepted. Aborting execution."
    )

import os
import tempfile
import urllib.parse
import sqlite3
import importlib
import logging
from pathlib import Path
from typing import Optional, Set, Union

try:
    import _sqlite3
except ImportError:
    _sqlite3 = None

logger = logging.getLogger("smarttrade.test_security.db_guard")

_CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _CURRENT_DIR.parent
_PROD_DATA_DIR = (PROJECT_ROOT / "data").resolve(strict=False)

_PRODUCTION_DB_FILENAMES = frozenset({
    "trades.db",
    "candles.db",
    "balance_history.sqlite3",
    "forensic_trades_backup.db",
})


class UnauthorizedDatabaseAccessDenied(RuntimeError):
    """Fatal security exception raised when any code attempts to access an unauthorized database."""


ProductionDatabaseAccessDenied = UnauthorizedDatabaseAccessDenied

# Profile and authorized temporary database state
_DB_PROFILE = "memory"
_ISOLATED_TEMP_DIR: Optional[str] = None
_ISOLATED_TEMP_DB_PATH: Optional[str] = None
_ISOLATED_CANONICAL_DB: Optional[str] = None
_AUTHORIZED_TEMP_ROOTS: Set[Path] = set()


def get_db_profile() -> str:
    """Return active db_guard profile ('memory' or 'temp_file')."""
    return _DB_PROFILE


def set_db_profile(profile: str) -> str:
    """Switch active db_guard profile explicitly ('memory' or 'temp_file'); returns previous profile."""
    global _DB_PROFILE
    if profile not in ("memory", "temp_file"):
        raise ValueError(f"Unsupported db_guard profile: {profile!r}")
    prev = _DB_PROFILE
    _DB_PROFILE = profile
    return prev


def authorize_temp_db_root(root_path: Union[str, os.PathLike]) -> Path:
    """
    Explicitly authorize a temporary directory root for temp_file SQLite tests.
    Refuses the repository root or production data/ directory as a temp root.
    """
    resolved = Path(os.path.realpath(os.fspath(root_path))).resolve(strict=False)
    try:
        if resolved == _PROD_DATA_DIR or resolved.is_relative_to(_PROD_DATA_DIR):
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Cannot authorize production data dir as temp root: {resolved}"
            )
    except ValueError:
        pass
    if resolved == PROJECT_ROOT:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Cannot authorize repository root as temp root: {resolved}"
        )
    _AUTHORIZED_TEMP_ROOTS.add(resolved)
    return resolved


def revoke_temp_db_root(root_path: Union[str, os.PathLike]) -> None:
    """Revoke a previously authorized temporary directory root."""
    resolved = Path(os.path.realpath(os.fspath(root_path))).resolve(strict=False)
    _AUTHORIZED_TEMP_ROOTS.discard(resolved)


def get_isolated_temp_db_path() -> Optional[str]:
    """Get the active session temporary database path (':memory:' in 'memory' profile)."""
    return _ISOLATED_TEMP_DB_PATH


def get_isolated_canonical_db() -> Optional[str]:
    """Get the active session canonical database path (':memory:' in 'memory' profile)."""
    return _ISOLATED_CANONICAL_DB


# ── LEVEL 0: SQLITE AUTHORIZER (BLOCK SQLITE_ATTACH BEFORE OPENING) ───────────
def _sqlite_authorizer(action_code, param1, param2, db_name, trigger_name):
    if action_code == sqlite3.SQLITE_ATTACH:
        if param1 == "":
            return sqlite3.SQLITE_OK
        msg = f"CRITICAL SECURITY VIOLATION: ATTACH DATABASE is strictly FORBIDDEN! Target: {param1!r}"
        logger.critical(msg)
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def install_connection_authorizer(connection):
    if not hasattr(connection, "set_authorizer"):
        try:
            if hasattr(connection, "close"):
                connection.close()
        except Exception as close_err:
            logger.error(f"Failed to close connection lacking set_authorizer: {close_err}")
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Connection object {type(connection)!r} lacks mandatory 'set_authorizer'!"
        )

    try:
        connection.set_authorizer(_sqlite_authorizer)
        if hasattr(connection, "_authorizer_locked"):
            connection._authorizer_locked = True
    except Exception as e:
        try:
            if hasattr(connection, "close"):
                connection.close()
        except Exception as close_err:
            logger.error(f"Failed to close connection after authorizer failure: {close_err}")
        raise UnauthorizedDatabaseAccessDenied(
            f"FATAL SECURITY VIOLATION: Failed to install authorizer on SQLite connection: {e}"
        ) from e


def _unquote_db_str(value: str) -> str:
    """Iteratively URL-decode percent-encoded characters and reject null bytes (used for blacklist/traversal detection)."""
    current = value
    for _ in range(3):
        decoded = urllib.parse.unquote(current)
        if "\x00" in decoded:
            raise UnauthorizedDatabaseAccessDenied("Null byte in URL-decoded database target")
        if decoded == current:
            break
        current = decoded
    return current


def _coerce_db_target_to_str(
    database: Optional[object],
    caller_context: str = "",
    *,
    allow_empty: bool = False,
) -> str:
    """
    Convert a database target (`str`, `bytes`, or `os.PathLike` returning `str` or `bytes`)
    to `str` using `os.fspath()` + `os.fsdecode()`, matching CPython's `PyUnicode_FSConverter`.

    Security properties (P1-A & P2-B):
    1. Never converts `bytes` with `str(database)` (which would produce `"b'...'"` and corrupt
       the basename to `"trades.db'"`).
    2. Explicitly rejects `bytearray` and any type other than `str`, `bytes`, `os.PathLike`
       (CPython's `PyUnicode_FSConverter` in `_sqlite3` rejects `bytearray` with `TypeError`).
    3. Supports `os.PathLike` objects whose `__fspath__()` returns `str` or `bytes`.
    4. Rejects embedded NUL bytes (`\\x00`).
    5. Never silently strips leading or trailing whitespace (`decoded != decoded.strip()`
       is rejected fail-closed as ambiguous).
    """
    if database is None:
        if allow_empty:
            return ""
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Database target cannot be None! Context: {caller_context}"
        )

    if isinstance(database, bytearray) or not isinstance(database, (str, bytes, os.PathLike)):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Unsupported database target type {type(database).__name__!r} "
            f"(only str, bytes, or os.PathLike are accepted by SQLite; bytearray is forbidden)! "
            f"Context: {caller_context}"
        )

    try:
        fspath_val = os.fspath(database)
    except Exception as exc:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: os.fspath() failed on database target {database!r}: {exc} "
            f"| Context: {caller_context}"
        ) from exc

    if isinstance(fspath_val, bytearray) or not isinstance(fspath_val, (str, bytes)):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: __fspath__() returned unsupported type "
            f"{type(fspath_val).__name__!r}! Context: {caller_context}"
        )

    if isinstance(fspath_val, bytes):
        if b"\x00" in fspath_val:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Null byte in bytes database target! Context: {caller_context}"
            )
        try:
            decoded = os.fsdecode(fspath_val)
        except Exception as exc:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: os.fsdecode() failed on bytes database target: {exc} "
                f"| Context: {caller_context}"
            ) from exc
    else:
        decoded = fspath_val

    if "\x00" in decoded:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Null byte in database target! Context: {caller_context}"
        )

    if decoded == "":
        if allow_empty:
            return ""
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Empty database target! Context: {caller_context}"
        )

    if decoded != decoded.strip():
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Leading or trailing whitespace in database target "
            f"{decoded!r} is ambiguous and strictly FORBIDDEN! Context: {caller_context}"
        )

    return decoded


def _coerce_uri_mode_bool(raw_uri: object, caller_context: str = "") -> bool:
    """
    Evaluate and validate the boolean `uri` parameter consistently with CPython's Argument Clinic
    `bool` converter (`PyObject_IsTrue`, i.e. `bool(raw_uri)`), while failing closed on ambiguous
    non-boolean/non-{0,1} types (P1-C).

    - `True` and `1` -> `True` (CPython `PyObject_IsTrue` == 1, activates `SQLITE_OPEN_URI`).
    - `False` and `0` -> `False` (CPython `PyObject_IsTrue` == 0, disables `SQLITE_OPEN_URI`).
    - Non-boolean/non-{0,1} values such as `uri="true"`, `uri="0"` (where Python `bool("0")` is
      `True` despite `"0"` looking falsy!), `uri=[]` (`bool([])` is `False`), or objects whose
      `__bool__()` raises an exception are rejected fail-closed with `UnauthorizedDatabaseAccessDenied`
      before any call to the underlying SQLite primitive.
    """
    try:
        cpython_truth = bool(raw_uri)
    except Exception as exc:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION ({caller_context}): Evaluating truthiness of uri parameter "
            f"raised {type(exc).__name__}: {exc}!"
        ) from exc

    if not isinstance(raw_uri, (bool, int)) or raw_uri not in (True, False, 1, 0):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION ({caller_context}): Ambiguous non-boolean uri parameter "
            f"{raw_uri!r} (type={type(raw_uri).__name__}, CPython PyObject_IsTrue={cpython_truth}) "
            "is FORBIDDEN; uri must be an explicit bool (True/False) or int (1/0)!"
        )

    return cpython_truth


def _extract_effective_uri_flag(args: tuple, kwargs: dict, caller_context: str = "") -> bool:
    """
    Extract and validate the effective `uri` flag from `sqlite3.connect(*args, **kwargs)` or
    `sqlite3.Connection.__init__(self, *args, **kwargs)` without modifying parameter order (P1-C).

    CPython 3.14 signature order (8 positional-or-keyword parameters + keyword-only `autocommit`):
      0: database
      1: timeout (5.0)
      2: detect_types (0)
      3: isolation_level ("")
      4: check_same_thread (True)
      5: factory (Connection)
      6: cached_statements (128)
      7: uri (False)
    """
    if len(args) > 8:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION ({caller_context}): Too many positional arguments "
            f"({len(args)} > 8) passed to SQLite connect!"
        )
    has_pos_uri = len(args) >= 8
    has_kw_uri = "uri" in kwargs
    if has_pos_uri and has_kw_uri:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION ({caller_context}): Parameter 'uri' passed both by "
            "position (args[7]) and by keyword ('uri')!"
        )
    if has_kw_uri:
        return _coerce_uri_mode_bool(kwargs["uri"], caller_context=caller_context)
    if has_pos_uri:
        return _coerce_uri_mode_bool(args[7], caller_context=caller_context)
    return False


def _contains_parent_traversal(raw_path: str) -> bool:
    """Detect explicit '..' directory traversal segments (both raw and URL-encoded %2e%2e)."""
    for candidate in (raw_path, _unquote_db_str(raw_path)):
        parts = candidate.replace("\\", "/").split("/")
        if any(p == ".." for p in parts):
            return True
    return False


def _contains_forbidden_prod_reference(raw_value: str) -> bool:
    """
    Check whether raw_value (either raw or URL-decoded) references a production DB filename
    or the production `data/` directory. Used as an early defense-in-depth check before any
    reject on `%xx` encoding or query strings so production DB references are always detected.
    """
    for candidate in (raw_value, _unquote_db_str(raw_value)):
        base_part = candidate.split("?", 1)[0]
        for prefix in ("sqlite:///", "sqlite://", "sqlite:", "file:///localhost/", "file://localhost/", "file:///", "file:"):
            if base_part.lower().startswith(prefix):
                base_part = base_part[len(prefix):]
                break
        norm_slashes = base_part.replace("\\", "/")
        segments = [seg.lower() for seg in norm_slashes.split("/") if seg and seg != "."]
        if any(seg in _PRODUCTION_DB_FILENAMES for seg in segments):
            return True
        if segments and segments[0] == "data":
            return True
    return False


def _validate_memory_uri_query(
    query_str: str,
    *,
    is_sa_url: bool,
    require_uri: bool,
    uri_mode: bool,
) -> None:
    """
    Strictly validate query string and URI mode on ':memory:' or 'file::memory:' targets.

    Mandatory separation between SQLAlchemy URLs and direct DBAPI calls:
    1. SQLAlchemy URL (`is_sa_url=True`, e.g. `sqlite:///file::memory:?uri=true`):
       - When `require_uri=True` (`file::memory:`), the URL query string MUST explicitly contain
         `uri=true` (which SQLAlchemy's pysqlite dialect consumes and converts into `cparams['uri'] = True`).
       - Allowed query keys on SQLAlchemy memory URLs: `{"uri", "cache", "mode"}` with
         `uri == "true"`, `cache == "shared"`, `mode == "memory"`.
    2. Direct DBAPI target (`is_sa_url=False`, e.g. `sqlite3.connect("file::memory:...", uri=True)`):
       - When `require_uri=True` (`file::memory:`), the Python parameter `uri=True` (`uri_mode is True`)
         MUST be explicitly provided.
       - Textual `?uri=true` inside a direct DBAPI target string NEVER substitutes for `uri_mode is True`
         and is strictly forbidden (`allowed_keys = {"cache", "mode"}`).
    """
    uri_eff = _coerce_uri_mode_bool(uri_mode, caller_context="_validate_memory_uri_query")
    if "#" in query_str:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: URI fragment ('#') in memory DB query string is FORBIDDEN: {query_str!r}"
        )
    if "%" in query_str:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Percent-encoded query string on memory DB target is FORBIDDEN: {query_str!r}"
        )
    try:
        parsed_qs = urllib.parse.parse_qs(query_str, keep_blank_values=True, strict_parsing=True) if query_str else {}
    except ValueError as exc:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Malformed query string on memory DB target: {query_str!r}"
        ) from exc

    if is_sa_url:
        allowed_keys = {"uri", "cache", "mode"}
    else:
        # In direct sqlite3.connect(), 'uri' is a Python keyword argument, NEVER a query parameter!
        if "uri" in parsed_qs:
            raise UnauthorizedDatabaseAccessDenied(
                "CRITICAL SECURITY VIOLATION: Textual '?uri=...' in a direct sqlite3 target is FORBIDDEN; "
                "pass the Python keyword argument uri=True to sqlite3.connect()!"
            )
        allowed_keys = {"cache", "mode"}

    unknown_keys = set(parsed_qs.keys()) - allowed_keys
    if unknown_keys:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Unsupported query parameters {sorted(unknown_keys)!r} on memory DB target!"
        )

    has_uri_true = False
    if "uri" in parsed_qs:
        vals = [v.lower() for v in parsed_qs["uri"]]
        if vals != ["true"]:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Invalid uri parameter {parsed_qs['uri']!r} (must be exact 'true')!"
            )
        has_uri_true = True

    if require_uri:
        if is_sa_url:
            if not has_uri_true:
                raise UnauthorizedDatabaseAccessDenied(
                    "CRITICAL SECURITY VIOLATION: SQLAlchemy 'sqlite:///file::memory:' requires explicit '?uri=true'!"
                )
        else:
            if not uri_eff:
                raise UnauthorizedDatabaseAccessDenied(
                    "CRITICAL SECURITY VIOLATION: Direct DBAPI 'file::memory:' requires Python parameter uri=True "
                    "(uri_mode=True); textual '?uri=true' or uri=False is FORBIDDEN!"
                )

    if "cache" in parsed_qs:
        vals = [v.lower() for v in parsed_qs["cache"]]
        if vals != ["shared"]:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Invalid cache parameter {parsed_qs['cache']!r} (must be 'shared')!"
            )

    if "mode" in parsed_qs:
        vals = [v.lower() for v in parsed_qs["mode"]]
        if vals != ["memory"]:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Invalid mode parameter {parsed_qs['mode']!r} on memory DB (must be 'memory')!"
            )


def _validate_file_uri_query(
    query_str: str,
    *,
    is_sa_url: bool,
    require_uri_true: bool,
) -> None:
    """
    Validate query parameters on a file-backed SQLAlchemy SQLite URL or a native SQLite `file:` URI (P1-B).
    - Strictly FORBIDS `mode=memory` on any non-`file::memory:` target (e.g. `file:./data/trades.db?mode=memory`
      causes SQLite to open an in-memory DB named after a file path, creating a severe semantic divergence).
    - Strictly FORBIDS textual `?uri=...` on direct DBAPI `file:` URIs (`is_sa_url=False`).
    - When `require_uri_true=True` (SQLAlchemy `sqlite:///file:...`), requires explicit `uri=true`.
    - Rejects percent-encoding or unknown query parameters.
    """
    if "#" in query_str:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: URI fragment ('#') in file DB query string is FORBIDDEN: {query_str!r}"
        )
    if "%" in query_str:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Percent-encoded query string on file DB target is FORBIDDEN: {query_str!r}"
        )
    try:
        parsed_qs = urllib.parse.parse_qs(query_str, keep_blank_values=True, strict_parsing=True) if query_str else {}
    except ValueError as exc:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Malformed query string on file DB target: {query_str!r}"
        ) from exc

    if is_sa_url:
        allowed_keys = {"uri", "mode", "cache"}
    else:
        if "uri" in parsed_qs:
            raise UnauthorizedDatabaseAccessDenied(
                "CRITICAL SECURITY VIOLATION: Textual '?uri=...' in a direct sqlite3 file: URI is FORBIDDEN!"
            )
        allowed_keys = {"mode", "cache"}

    unknown_keys = set(parsed_qs.keys()) - allowed_keys
    if unknown_keys:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Unsupported query parameters {sorted(unknown_keys)!r} on file DB target!"
        )

    has_uri_true = False
    if "uri" in parsed_qs:
        vals = [v.lower() for v in parsed_qs["uri"]]
        if vals != ["true"]:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Invalid uri parameter {parsed_qs['uri']!r} on file DB target!"
            )
        has_uri_true = True

    if require_uri_true and not has_uri_true:
        raise UnauthorizedDatabaseAccessDenied(
            "CRITICAL SECURITY VIOLATION: SQLAlchemy 'sqlite:///file:...' target requires explicit '?uri=true'!"
        )

    if "mode" in parsed_qs:
        vals = [v.lower() for v in parsed_qs["mode"]]
        if "memory" in vals:
            raise UnauthorizedDatabaseAccessDenied(
                "CRITICAL SECURITY VIOLATION: 'mode=memory' is strictly FORBIDDEN on file-path targets "
                "(only exact 'file::memory:' may use mode=memory)!"
            )
        if len(vals) != 1 or vals[0] not in ("ro", "rw", "rwc"):
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Invalid mode parameter {parsed_qs['mode']!r} on file DB target!"
            )

    if "cache" in parsed_qs:
        vals = [v.lower() for v in parsed_qs["cache"]]
        if len(vals) != 1 or vals[0] not in ("shared", "private"):
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Invalid cache parameter {parsed_qs['cache']!r} on file DB target!"
            )


def _is_deceptive_memory_token(raw_value: str) -> bool:
    """
    Return True if raw_value starts with ':memory:' or 'file::memory:' without exact equality,
    or if a path with directory separators has a basename starting with ':memory:' or 'file::memory:'
    (e.g. `<cwd>\\file::memory:` produced by SQLAlchemy pysqlite when `uri=true` is omitted).
    """
    for candidate in (raw_value.split("?", 1)[0], _unquote_db_str(raw_value.split("?", 1)[0])):
        clean = candidate
        for prefix in ("sqlite:///", "sqlite://", "sqlite:"):
            if clean.lower().startswith(prefix):
                clean = clean[len(prefix):]
                break
        norm_slashes = clean.replace("\\", "/")
        low = norm_slashes.lower()
        if low.startswith(":memory:") and norm_slashes != ":memory:":
            return True
        if low.startswith("file::memory:") and norm_slashes != "file::memory:":
            return True
        if "/" in norm_slashes:
            base_low = norm_slashes.split("/")[-1].lower()
            if base_low.startswith((":memory:", "file::memory:")):
                return True
    return False


def _extract_native_sqlite_file_uri_path(raw_file_uri_no_query: str) -> str:
    """
    Extract the filesystem path from a native SQLite `file:...` URI (without query string)
    following SQLite's `sqlite3ParseUri` rules (`src/main.c`):
    - Must begin with exact lowercase `"file:"`.
    - Authority, if present (`file://...`), must be empty (`file:///...`) or `"localhost"`
      (`file://localhost/...`). Any other authority is rejected by SQLite and by this guard.
    - Never decodes `%xx` to make an encoded path look like an authorized temporary root:
      any `%` in a file URI path is rejected fail-closed.
    - Strips leading `/` before a Windows drive letter (`/C:/...` -> `C:/...`), matching
      SQLite's DOS drive letter handling in `sqlite3ParseUri`.
    """
    if not raw_file_uri_no_query.startswith("file:"):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Native SQLite file URI must start with lowercase 'file:': "
            f"{raw_file_uri_no_query!r}"
        )
    if "#" in raw_file_uri_no_query:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: URI fragment ('#') in SQLite file: URI is FORBIDDEN: "
            f"{raw_file_uri_no_query!r}"
        )
    if "%" in raw_file_uri_no_query:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Percent-encoded characters in SQLite file: URI are FORBIDDEN: "
            f"{raw_file_uri_no_query!r}"
        )

    after_file = raw_file_uri_no_query[5:]
    if not after_file or after_file != after_file.strip():
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Empty or whitespace-padded path in SQLite file: URI: "
            f"{raw_file_uri_no_query!r}"
        )

    if after_file.lower() == ":memory:":
        return "file::memory:"

    if _is_deceptive_memory_token(after_file):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Ambiguous file: URI memory target {raw_file_uri_no_query!r} is FORBIDDEN!"
        )

    if after_file.startswith("//"):
        slash_idx = after_file.find("/", 2)
        if slash_idx == -1:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Invalid authority without path in file: URI: "
                f"{raw_file_uri_no_query!r}"
            )
        authority = after_file[2:slash_idx]
        if authority not in ("", "localhost"):
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Invalid authority {authority!r} in SQLite file: URI "
                f"(only empty authority or 'localhost' is valid in SQLite): {raw_file_uri_no_query!r}"
            )
        uri_path = after_file[slash_idx:]
    else:
        uri_path = after_file

    if _is_deceptive_memory_token(uri_path) or uri_path.lower() == ":memory:":
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Ambiguous file: URI memory target {raw_file_uri_no_query!r} is FORBIDDEN!"
        )

    # SQLite sqlite3ParseUri DOS drive letter rule: /C:/path -> C:/path (leaves Unix /tmp/path intact)
    if len(uri_path) >= 3 and uri_path[0] == "/" and uri_path[1].isalpha() and uri_path[2] == ":":
        uri_path = uri_path[1:]

    # RES-2.1 (Option A): A native SQLite `file:` URI pointing to a filesystem path MUST be an
    # unambiguous absolute path (rejects `file:relative.db`, `file:./relative.db`, `file:../...`,
    # Windows drive-relative `file:C:foo.db`, and Windows root-relative `file:/foo.db`).
    if not _is_unambiguous_absolute_os_path(uri_path):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Relative or incomplete path {uri_path!r} in SQLite file: URI "
            f"{raw_file_uri_no_query!r} is FORBIDDEN (absolute path required; prevents os.getcwd() TOCTOU)!"
        )

    return uri_path


def extract_sqlite_url_path(
    database: object,
    *,
    allow_sa_url: bool = True,
    uri_mode: bool = False,
) -> str:
    """
    Strict path extractor that distinguishes:
      1. SQLAlchemy URL (`sqlite://`, `sqlite:///...`),
      2. Direct DBAPI path (`is_sa_url=False`, `uri_mode=False`),
      3. Native SQLite URI `file:...` (`is_sa_url=False`, `uri_mode=True`).

    Security invariants (P1-A, P1-B, P2-B):
      - Never silently strips leading/trailing whitespace.
      - Never automatically URL-decodes `%xx` to make an encoded target appear authorized.
      - For direct DBAPI with `uri_mode=False`, never strips `file:` and never truncates `?query`
        (rejects any `file:` prefix, `?` query string, or `%` encoding fail-closed).
      - Preserves Unix 4-slash absolute paths (`sqlite:////tmp/test/smarttrade.db` -> `/tmp/test/smarttrade.db`),
        Windows drive paths (`sqlite:///C:/tmp/test.db` -> `C:/tmp/test.db`), and relative paths.
      - Never transforms `file::memory:` into `:memory:`.
    """
    db_str = _coerce_db_target_to_str(database, caller_context="extract_sqlite_url_path")
    uri_eff = _coerce_uri_mode_bool(uri_mode, caller_context="extract_sqlite_url_path")

    # Always check traversal and deceptive memory tokens first (including via URL-unquoting)
    raw_base, raw_query = (db_str.split("?", 1) + [""])[:2]
    if raw_base != raw_base.strip():
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Whitespace adjacent to '?' in database target is FORBIDDEN: {db_str!r}"
        )
    if "?" in _unquote_db_str(raw_base):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Encoded '?' in database path is FORBIDDEN: {db_str!r}"
        )
    if _is_deceptive_memory_token(db_str):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Deceptive ':memory:' prefix is FORBIDDEN: {db_str!r}"
        )
    if _contains_parent_traversal(raw_base):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Directory traversal ('..') forbidden in DB path: {db_str!r}"
        )

    low_str = db_str.lower()
    is_sa_url = low_str.startswith(("sqlite:///", "sqlite://", "sqlite:"))

    # ── Category 1: SQLAlchemy URL ───────────────────────────────────────────
    if is_sa_url:
        if not allow_sa_url:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: SQLAlchemy URL {db_str!r} passed directly to raw DBAPI connect!"
            )
        if "?" in db_str and raw_query == "":
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Trailing '?' with empty query string is FORBIDDEN: {db_str!r}"
            )
        if "%" in raw_base:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Percent-encoded path in SQLAlchemy URL is FORBIDDEN: {db_str!r}"
            )

        if raw_base.lower().startswith("sqlite:///"):
            after_scheme = raw_base[len("sqlite:///"):]
        elif raw_base.lower().startswith("sqlite://"):
            after_scheme = raw_base[len("sqlite://"):]
        else:
            after_scheme = raw_base[len("sqlite:"):]

        if after_scheme in ("", ":memory:"):
            return ":memory:"
        if after_scheme.lower() == "file::memory:":
            if after_scheme != "file::memory:":
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: Case-variant 'file::memory:' is FORBIDDEN: {db_str!r}"
                )
            return "file::memory:"

        if after_scheme.lower().startswith("file:"):
            if not after_scheme.startswith("file:"):
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: Case-variant 'file:' prefix is FORBIDDEN: {db_str!r}"
                )
            if "#" in db_str:
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: URI fragment ('#') in SQLAlchemy SQLite file: URI is FORBIDDEN: {db_str!r}"
                )
            _validate_file_uri_query(raw_query, is_sa_url=True, require_uri_true=True)
            return _extract_native_sqlite_file_uri_path(after_scheme)

        if raw_query:
            _validate_file_uri_query(raw_query, is_sa_url=True, require_uri_true=False)

        # Windows drive letter handling on SQLAlchemy URLs: /C:/path or \C:\path -> C:/path
        if (
            len(after_scheme) >= 3
            and after_scheme[0] in ("/", "\\")
            and after_scheme[1].isalpha()
            and after_scheme[2] == ":"
        ):
            after_scheme = after_scheme[1:]
        return after_scheme

    # ── Category 2: Direct DBAPI target with uri_mode=False ──────────────────
    if not uri_eff:
        if db_str == ":memory:":
            return ":memory:"
        if low_str.startswith(":memory:"):
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Direct DBAPI ':memory:' target must be exact ':memory:' "
                f"without any query string (got {db_str!r})!"
            )
        if low_str.startswith("file:"):
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Direct DBAPI target starting with 'file:' ({db_str!r}) "
                "is FORBIDDEN when uri=False (SQLite would open a literal file named 'file:...')!"
            )
        if "?" in db_str:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Query string in direct DBAPI target ({db_str!r}) "
                "is FORBIDDEN when uri=False (SQLite does not parse query strings when uri=False)!"
            )
        if "%" in db_str:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Percent-encoded sequence in direct DBAPI target ({db_str!r}) "
                "is FORBIDDEN when uri=False (SQLite does not decode '%xx' when uri=False)!"
            )
        return db_str

    # ── Category 3: Direct DBAPI target with uri_mode=True ───────────────────
    if not low_str.startswith("file:"):
        # Without 'file:' prefix, SQLite does NOT treat the target as a URI even when uri=True!
        if db_str == ":memory:":
            return ":memory:"
        if low_str.startswith(":memory:"):
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Direct DBAPI ':memory:' target must be exact ':memory:' "
                f"without any query string even when uri=True (got {db_str!r})!"
            )
        if "?" in db_str:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Query string in direct DBAPI target without 'file:' prefix "
                f"({db_str!r}) is FORBIDDEN even when uri=True (SQLite only parses URI queries on 'file:' URIs)!"
            )
        if "%" in db_str:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Percent-encoded sequence in direct DBAPI target without 'file:' "
                f"prefix ({db_str!r}) is FORBIDDEN!"
            )
        return db_str

    # Subcase 3b: Native SQLite `file:...` URI with `uri_mode=True`
    if not db_str.startswith("file:"):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Case-variant 'file:' scheme ({db_str!r}) is FORBIDDEN!"
        )
    if "#" in db_str:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: URI fragment ('#') in native SQLite file: URI is FORBIDDEN: {db_str!r}"
        )
    if "?" in db_str and raw_query == "":
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Trailing '?' with empty query string is FORBIDDEN: {db_str!r}"
        )
    if raw_base.lower() == "file::memory:":
        if raw_base != "file::memory:":
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Case-variant 'file::memory:' is FORBIDDEN: {db_str!r}"
            )
        return "file::memory:"

    _validate_file_uri_query(raw_query, is_sa_url=False, require_uri_true=False)
    return _extract_native_sqlite_file_uri_path(raw_base)


def normalize_db_target(database: Optional[object], uri_mode: bool = False) -> str:
    """
    Normalize any database reference (path, SQLAlchemy URL, file URI, PathLike, bytes)
    to canonical filesystem path (via pathlib + os.path.realpath) or ':memory:'.
    - Converts bytes and PathLike consistently with CPython's `PyUnicode_FSConverter` (`os.fsdecode(os.fspath(...))`).
    - Rejects `bytearray`, leading/trailing whitespace, `%xx` path obfuscation, and `?query` on non-URI paths.
    - Recognizes ':memory:' by exact equality with validated query params.
    - Never automatically assimilates 'file::memory:' to ':memory:': requires explicit,
      validated URI mode (`?uri=true` on SQLAlchemy URLs or `uri_mode=True` on sqlite3.connect).
    """
    if database is None:
        return ""

    db_str = _coerce_db_target_to_str(database, caller_context="normalize_db_target", allow_empty=True)
    if not db_str:
        return ""

    uri_eff = _coerce_uri_mode_bool(uri_mode, caller_context="normalize_db_target")

    # Reject deceptive ':memory:evil' / 'file::memory:evil' / '<dir>/file::memory:' tokens immediately
    if _is_deceptive_memory_token(db_str):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Deceptive ':memory:' prefix is FORBIDDEN: {db_str!r}"
        )

    raw_base, raw_query = (db_str.split("?", 1) + [""])[:2]
    if raw_base != raw_base.strip():
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Whitespace adjacent to '?' in database target is FORBIDDEN: {db_str!r}"
        )
    if "?" in _unquote_db_str(raw_base):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Encoded '?' in database path is FORBIDDEN: {db_str!r}"
        )

    low_base = raw_base.lower()
    is_sa_url = low_base.startswith(("sqlite:///", "sqlite://", "sqlite:"))

    # 1. Direct DBAPI ':memory:' target (not a SQLAlchemy sqlite:// URL):
    #    SQLite only recognizes ':memory:' as in-memory when the string is literally ':memory:'
    #    with ZERO query string (':memory:?cache=shared' is treated by SQLite as a physical file
    #    on disk, even when uri=True, because SQLite URI syntax requires the 'file:' prefix!).
    if not is_sa_url and (low_base == ":memory:" or db_str.lower().startswith(":memory:")):
        if db_str != ":memory:":
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Direct DBAPI ':memory:' target must be exact ':memory:' "
                f"without any query string (got {db_str!r}; SQLite treats ':memory:?...' as a physical file)!"
            )
        return ":memory:"

    # 2. SQLAlchemy non-URI in-memory SQLite URLs (sqlite://, sqlite:///, sqlite:///:memory:[?cache=shared])
    if is_sa_url and low_base in (
        "sqlite://",
        "sqlite:///",
        "sqlite:///:memory:",
    ):
        if low_base == "sqlite:///:memory:" and not raw_base.endswith(":memory:"):
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Case-variant ':memory:' is FORBIDDEN: {db_str!r}"
            )
        if "?" in db_str and raw_query == "":
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Trailing '?' with empty query string is FORBIDDEN: {db_str!r}"
            )
        _validate_memory_uri_query(
            raw_query,
            is_sa_url=True,
            require_uri=False,
            uri_mode=False,
        )
        return ":memory:"

    # 3. 'file::memory:' or file targets
    extracted = extract_sqlite_url_path(db_str, allow_sa_url=True, uri_mode=uri_eff)
    if extracted == "file::memory:":
        if "?" in db_str and raw_query == "":
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Trailing '?' with empty query string is FORBIDDEN: {db_str!r}"
            )
        # Explicit validation required BEFORE returning ':memory:'
        _validate_memory_uri_query(
            raw_query,
            is_sa_url=is_sa_url,
            require_uri=True,
            uri_mode=uri_eff,
        )
        return ":memory:"

    if not extracted:
        return ""

    return _resolve_canonical_os_path(extracted)


def _is_within_authorized_temp_roots(real_path: Path) -> bool:
    """Verify real_path (after symlink resolution) resides strictly inside an authorized temp root."""
    norm_target = Path(os.path.normcase(str(real_path)))
    for root in _AUTHORIZED_TEMP_ROOTS:
        norm_root = Path(os.path.normcase(str(root)))
        try:
            if norm_target != norm_root and norm_target.is_relative_to(norm_root):
                return True
        except ValueError:
            continue
    return False


def verify_authorized_db(
    database: Optional[object],
    caller_context: str = "",
    uri_mode: bool = False,
) -> bool:
    """
    STRICT DEFAULT-DENY ALLOWLIST VALIDATOR.
    - Converts `str`, `bytes`, and `os.PathLike` (returning `str` or `bytes`) via `_coerce_db_target_to_str`
      (matching CPython `PyUnicode_FSConverter` without ever calling `str(bytes_obj)` or `.strip()`).
    - Rejects `bytearray` and any non-`(str, bytes, os.PathLike)` target before connection.
    - Accepts ':memory:', 'sqlite://', and 'sqlite:///:memory:' in both profiles.
    - Accepts 'file::memory:' ONLY when URI mode is explicitly enabled and validated
      ('?uri=true' on SQLAlchemy URLs or uri_mode=True/1 on sqlite3.connect).
    - Rejects directory traversal ('..'), percent-encoded path tricks ('%2e', '%2f'), symlinks escaping
      authorized roots, production DB filenames, and any path inside the repository data/ directory.
    - In 'temp_file' profile, accepts file-backed SQLite ONLY under an explicitly authorized
      temporary root (or _ISOLATED_CANONICAL_DB). Never globally allows .db/.sqlite extensions.
    """
    raw_str = _coerce_db_target_to_str(database, caller_context=caller_context)
    uri_eff = _coerce_uri_mode_bool(uri_mode, caller_context=caller_context)

    # Reject non-SQLite database URL schemes (PostgreSQL, MySQL, MariaDB, MSSQL, Oracle, etc.)
    if "://" in raw_str:
        scheme = raw_str.split("://", 1)[0].lower()
        base_scheme = scheme.split("+", 1)[0]
        if base_scheme != "sqlite" and not (base_scheme == "file" and uri_eff and raw_str.startswith("file:")):
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Non-SQLite database scheme {scheme!r} is strictly FORBIDDEN "
                f"in test mode! Target: {database!r} | Context: {caller_context or 'Unknown'}"
            )

    # Early defense-in-depth check: detect production DB filenames and `data/` references
    # even if disguised with `%2f`, `%2e`, `file:`, or query strings
    if _contains_forbidden_prod_reference(raw_str):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Production database or production data/ directory is strictly FORBIDDEN!\n"
            f"  Target: {database!r}\n"
            f"  Decoded string: {raw_str!r}\n"
            f"  Caller context: {caller_context or 'Unknown'}"
        )

    # Reject explicit directory traversal segments before checking allowlist
    raw_no_query = raw_str.split("?", 1)[0]
    if _contains_parent_traversal(raw_no_query):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Directory traversal ('..') forbidden in DB path: {database!r} "
            f"(context={caller_context or 'Unknown'})"
        )

    norm_target = normalize_db_target(raw_str, uri_mode=uri_eff)
    if not norm_target:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Unnormalizable database target {database!r}! Context: {caller_context}"
        )

    if norm_target == ":memory:":
        return True

    real_path = Path(norm_target)
    base_name = real_path.name.lower()

    if base_name.startswith((":memory:", "file::memory:")):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Filesystem path with memory-like basename {base_name!r} "
            f"is strictly FORBIDDEN (missing '?uri=true' or deceptive memory filename)!\n"
            f"  Target: {database!r}\n"
            f"  Resolved canonical path: {norm_target!r}\n"
            f"  Caller context: {caller_context or 'Unknown'}"
        )

    if base_name in _PRODUCTION_DB_FILENAMES:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Production database {base_name!r} is strictly FORBIDDEN!\n"
            f"  Target: {database!r}\n"
            f"  Resolved canonical path: {norm_target!r}\n"
            f"  Caller context: {caller_context or 'Unknown'}"
        )

    try:
        if Path(os.path.normcase(str(real_path))).is_relative_to(Path(os.path.normcase(str(_PROD_DATA_DIR)))):
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Path inside production data/ directory is FORBIDDEN: {norm_target!r}"
            )
    except ValueError:
        pass

    if _DB_PROFILE == "temp_file":
        if _ISOLATED_CANONICAL_DB and _ISOLATED_CANONICAL_DB != ":memory:" and norm_target == _ISOLATED_CANONICAL_DB:
            return True
        if _is_within_authorized_temp_roots(real_path):
            return True

    msg = (
        f"CRITICAL SECURITY VIOLATION: Access to database is FORBIDDEN under DEFAULT-DENY ({_DB_PROFILE}) policy!\n"
        f"  Target: {database!r}\n"
        f"  Resolved canonical path: {norm_target!r}\n"
        f"  Authorized roots: {[str(r) for r in sorted(_AUTHORIZED_TEMP_ROOTS)]}\n"
        f"  Caller context: {caller_context or 'Unknown'}"
    )
    logger.critical(msg)
    raise UnauthorizedDatabaseAccessDenied(msg)


verify_not_production_db = verify_authorized_db


# ── LEVEL 1: PHYSICAL INTERCEPTION OF SQLITE3.CONNECT & CONNECTION ─────────────
_ORIGINAL_SQLITE3_CONNECT = sqlite3.connect
_ORIGINAL_SQLITE3_CONNECTION = sqlite3.Connection
_ORIGINAL_IMPORTLIB_RELOAD = importlib.reload
_SQLITE3_GUARD_INSTALLED = False


def _is_unambiguous_absolute_os_path(raw_os_path: str) -> bool:
    """
    Return True iff `raw_os_path` is a complete, unambiguous absolute OS path that does not
    depend on `os.getcwd()` or per-drive current directories (RES-2.1, Option A).
    - On Windows (`os.name == "nt"`): requires BOTH a drive/UNC share and a root (`Path.is_absolute()`),
      rejecting relative paths (`foo.db`, `./foo.db`), drive-relative paths (`C:foo.db`), and
      root-relative paths (`\\foo.db`, `/foo.db`).
    - On POSIX: requires a leading `/` (`Path.is_absolute()`).
    """
    candidate = Path(raw_os_path)
    if candidate.drive and not candidate.root:
        return False
    return candidate.is_absolute()


def _resolve_canonical_os_path(raw_os_path: str) -> str:
    """
    Resolve an unambiguous absolute OS filesystem path to its canonical form (RES-2.1, Option A).
    - Rejects relative paths (`foo.db`, `./foo.db`, `../foo.db`), Windows drive-relative paths
      (`C:foo.db`), and Windows root-relative paths (`\\foo.db`, `/foo.db`) BEFORE any SQLite call.
    - Never calls `os.getcwd()`, `os.chdir()`, or `PROJECT_ROOT` fallback to authorize a relative path.
    """
    if not _is_unambiguous_absolute_os_path(raw_os_path):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Relative or incomplete OS database path {raw_os_path!r} "
            "is FORBIDDEN (complete absolute path required; prevents os.getcwd() TOCTOU)!"
        )
    real_path = Path(os.path.realpath(raw_os_path)).resolve(strict=False)
    return os.path.normcase(str(real_path))


def _assert_raw_dbapi_target_coherent(target: Optional[object], uri_mode: bool, caller_context: str) -> None:
    """
    Verify that the exact raw target handed to CPython sqlite3.connect / sqlite3.Connection
    never diverges from what SQLite's C engine will actually open (for BOTH `:memory:` AND
    file targets in `temp_file` profile — P1-A, P1-B, P1-C, P2-B).

    1. Converts `target` via `_coerce_db_target_to_str` (`os.fsdecode(os.fspath(target))`),
       preserving complete parity with CPython's `PyUnicode_FSConverter`.
    2. Forbids any SQLAlchemy URL (`sqlite:...`) passed directly to raw DBAPI `sqlite3.connect`.
    3. If `norm == ":memory:"`, the raw decoded string MUST be either:
       - exact `":memory:"` (8 chars, no query string), OR
       - `uri_eff is True` and `raw_s` has base `"file::memory:"` with validated query string.
    4. If `norm != ":memory:"` (file target in `temp_file` profile):
       - When `uri_eff is False`: `raw_s` must not start with `"file:"`, must not contain `"?"`
         or `"%"`, and `_resolve_canonical_os_path(raw_s)` must equal `norm`.
       - When `uri_eff is True`: if `raw_s` starts with `"file:"`, its SQLite URI query and
         extracted file path must resolve to `norm`; otherwise `raw_s` must not contain `"?"`
         or `"%"` and `_resolve_canonical_os_path(raw_s)` must equal `norm`.
    """
    raw_s = _coerce_db_target_to_str(target, caller_context=caller_context)
    uri_eff = _coerce_uri_mode_bool(uri_mode, caller_context=caller_context)

    if raw_s.lower().startswith(("sqlite:///", "sqlite://", "sqlite:")):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION ({caller_context}): SQLAlchemy URL {target!r} cannot be passed "
            "directly to raw DBAPI sqlite3.connect!"
        )

    if _contains_forbidden_prod_reference(raw_s):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION ({caller_context}): Production database or data/ reference in raw "
            f"DBAPI target {target!r} is strictly FORBIDDEN!"
        )

    if _contains_parent_traversal(raw_s.split("?", 1)[0]):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION ({caller_context}): Directory traversal ('..') in raw "
            f"DBAPI target {target!r} is strictly FORBIDDEN!"
        )

    if raw_s.lower().startswith("file:") and "#" in raw_s:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION ({caller_context}): URI fragment ('#') in raw DBAPI file: URI "
            f"{target!r} is strictly FORBIDDEN!"
        )

    norm = normalize_db_target(raw_s, uri_mode=uri_eff)

    if norm == ":memory:":
        if raw_s == ":memory:":
            return
        if uri_eff:
            base_part, q_part = (raw_s.split("?", 1) + [""])[:2]
            if base_part == "file::memory:" and not ("?" in raw_s and q_part == ""):
                _validate_memory_uri_query(q_part, is_sa_url=False, require_uri=True, uri_mode=True)
                return
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION ({caller_context}): Raw DBAPI target {target!r} (uri={uri_eff}) "
            "diverges from normalized ':memory:' and would be opened by SQLite as a physical file!"
        )

    # File target coherence check (`norm != ":memory:"`, e.g. in `temp_file` profile)
    if "%" in raw_s:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION ({caller_context}): Percent-encoded file target {target!r} "
            "is FORBIDDEN!"
        )

    if not uri_eff:
        if raw_s.lower().startswith("file:") or "?" in raw_s:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION ({caller_context}): Raw DBAPI file target {target!r} "
                "with 'file:' prefix or '?' query string is FORBIDDEN when uri=False!"
            )
        effective_path = _resolve_canonical_os_path(raw_s)
    else:
        if raw_s.lower().startswith("file:"):
            if not raw_s.startswith("file:"):
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION ({caller_context}): Case-variant 'file:' scheme in {target!r}!"
                )
            base_part, q_part = (raw_s.split("?", 1) + [""])[:2]
            if "?" in raw_s and q_part == "":
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION ({caller_context}): Trailing '?' in file: URI {target!r}!"
                )
            _validate_file_uri_query(q_part, is_sa_url=False, require_uri_true=False)
            uri_file_path = _extract_native_sqlite_file_uri_path(base_part)
            effective_path = _resolve_canonical_os_path(uri_file_path)
        else:
            if "?" in raw_s:
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION ({caller_context}): Query string on non-'file:' DBAPI target "
                    f"{target!r} is FORBIDDEN even when uri=True!"
                )
            effective_path = _resolve_canonical_os_path(raw_s)

    if effective_path != norm:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION ({caller_context}): Raw DBAPI target {target!r} (effective={effective_path!r}) "
            f"diverges from normalized target {norm!r}!"
        )


def _extract_dbapi_target_arg(args: tuple, kwargs: dict, caller_context: str) -> object:
    """Extract the `database` argument from positional `args[0]` or keyword `kwargs['database']`."""
    if args and "database" in kwargs:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION ({caller_context}): Parameter 'database' passed both by "
            "position (args[0]) and by keyword ('database')!"
        )
    if args:
        return args[0]
    return kwargs.get("database")


def _guarded_sqlite3_connect(*args, **kwargs):
    target = _extract_dbapi_target_arg(args, kwargs, caller_context="sqlite3.connect")
    uri_flag = _extract_effective_uri_flag(args, kwargs, caller_context="sqlite3.connect")
    verify_authorized_db(target, caller_context="sqlite3.connect", uri_mode=uri_flag)
    _assert_raw_dbapi_target_coherent(target, uri_mode=uri_flag, caller_context="sqlite3.connect")
    kwargs.setdefault("factory", GuardedSqlite3Connection)
    conn = _ORIGINAL_SQLITE3_CONNECT(*args, **kwargs)
    if not isinstance(conn, GuardedSqlite3Connection):
        install_connection_authorizer(conn)
    return conn


class GuardedSqlite3Connection(_ORIGINAL_SQLITE3_CONNECTION):
    def __init__(self, *args, **kwargs):
        target = _extract_dbapi_target_arg(args, kwargs, caller_context="sqlite3.Connection")
        uri_flag = _extract_effective_uri_flag(args, kwargs, caller_context="sqlite3.Connection")
        verify_authorized_db(target, caller_context="sqlite3.Connection", uri_mode=uri_flag)
        _assert_raw_dbapi_target_coherent(target, uri_mode=uri_flag, caller_context="sqlite3.Connection")
        super().__init__(*args, **kwargs)
        self._authorizer_locked = False
        install_connection_authorizer(self)

    def set_authorizer(self, authorizer_cb):
        if getattr(self, "_authorizer_locked", False):
            if authorizer_cb is not _sqlite_authorizer:
                raise UnauthorizedDatabaseAccessDenied(
                    "CRITICAL SECURITY VIOLATION: Modifying locked SQLite security authorizer is FORBIDDEN!"
                )
        return super().set_authorizer(authorizer_cb)


def _guarded_importlib_reload(module):
    mod_name = getattr(module, "__name__", "")
    if mod_name in ("sqlite3", "_sqlite3", "sqlite3.dbapi2") or mod_name.startswith("sqlite3."):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Reloading {mod_name} during tests is strictly FORBIDDEN!"
        )
    return _ORIGINAL_IMPORTLIB_RELOAD(module)


def install_sqlite3_guard():
    global _SQLITE3_GUARD_INSTALLED
    if not _SQLITE3_GUARD_INSTALLED:
        sqlite3.connect = _guarded_sqlite3_connect
        sqlite3.Connection = GuardedSqlite3Connection
        if hasattr(sqlite3, "dbapi2"):
            sqlite3.dbapi2.connect = _guarded_sqlite3_connect
            sqlite3.dbapi2.Connection = GuardedSqlite3Connection

        if _sqlite3 is not None:
            _sqlite3.connect = _guarded_sqlite3_connect
            _sqlite3.Connection = GuardedSqlite3Connection

        importlib.reload = _guarded_importlib_reload
        _SQLITE3_GUARD_INSTALLED = True


# ── LEVEL 2: PHYSICAL INTERCEPTION OF SQLALCHEMY CONNECTIONS ──────────────────
_SQLALCHEMY_GUARD_INSTALLED = False

# Target-carrying keys in SQLAlchemy `cparams` (`connect_args`)
_TARGET_SQLITE_CPARAM_KEYS = frozenset({"database", "db", "filename"})

# Known non-target SQLite DBAPI `connect()` keyword options that must NOT be interpreted as file paths (P2-A).
# Any unknown key outside `_TARGET_SQLITE_CPARAM_KEYS | _KNOWN_SQLITE_NON_TARGET_CPARAM_KEYS` is rejected
# fail-closed so an unrecognized parameter can never smuggle an unvalidated database target.
_KNOWN_SQLITE_NON_TARGET_CPARAM_KEYS = frozenset({
    "timeout",
    "detect_types",
    "isolation_level",
    "check_same_thread",
    "factory",
    "cached_statements",
    "uri",
    "autocommit",
})


def _extract_sa_url_uri_query_flag(eng_url_str: str) -> Optional[bool]:
    """
    Extract the explicit `uri` query parameter from a SQLAlchemy SQLite URL, if present (RES-2.3).
    - Returns `None` if no `uri` query parameter is present in the URL.
    - Returns `True` for exact `uri=true` (single occurrence) and `False` for `uri=false` (single occurrence).
    - Rejects duplicated `uri` parameters (e.g. `?uri=true&uri=false`, `?uri=true&uri=true`) or
      malformed/unsupported values fail-closed.
    """
    if "?" not in eng_url_str:
        return None
    raw_query = eng_url_str.split("?", 1)[1]
    if not raw_query:
        return None
    if "#" in raw_query or "%" in raw_query:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Forbidden character ('#' or '%') in SQLAlchemy URL query: {eng_url_str!r}"
        )
    try:
        parsed_qs = urllib.parse.parse_qs(raw_query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Malformed query string in SQLAlchemy URL: {eng_url_str!r}"
        ) from exc
    if "uri" not in parsed_qs:
        return None
    vals = [v.lower() for v in parsed_qs["uri"]]
    if len(vals) != 1:
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Duplicated 'uri' parameter {parsed_qs['uri']!r} in SQLAlchemy URL "
            f"{eng_url_str!r} is FORBIDDEN!"
        )
    if vals[0] == "true":
        return True
    if vals[0] == "false":
        return False
    raise UnauthorizedDatabaseAccessDenied(
        f"CRITICAL SECURITY VIOLATION: Invalid 'uri' query value {parsed_qs['uri']!r} in SQLAlchemy URL "
        f"{eng_url_str!r} (expected 'true' or 'false')!"
    )


def _validate_sqlalchemy_do_connect(dialect, conn_rec, cargs, cparams) -> None:
    """
    Validate a SQLAlchemy `Engine.do_connect` event before any DBAPI connection is opened (P2-A):
    1. Rejects non-SQLite dialects fail-closed.
    2. Extracts and validates `uri` from `cparams` using CPython truthiness semantics (`_coerce_uri_mode_bool`).
    3. Validates `eng_url` (`dialect._smarttrade_engine_url` or `dialect.url`) and enforces strict
       coherence with `cargs` and `uri_flag` for both `:memory:` and `file::memory:`.
    4. Validates the actual database target in `cargs` (including raw DBAPI coherence).
    5. In `cparams`, validates ONLY target-representing keys (`database`, `db`, `filename`) as
       database paths; never treats non-target options (`isolation_level`, `timeout`, etc.) as
       file paths, and rejects any unknown parameter key fail-closed.
    """
    dialect_name = getattr(dialect, "name", "")
    dialect_name_low = dialect_name.lower() if isinstance(dialect_name, str) else ""
    if dialect_name_low != "sqlite":
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION: Non-SQLite or unnamed SQLAlchemy dialect {dialect_name!r} "
            "is strictly FORBIDDEN in test mode (blocked at Engine.do_connect before connection)!"
        )

    uri_flag = (
        _coerce_uri_mode_bool(cparams["uri"], caller_context="SQLAlchemy Engine.do_connect cparams[uri]")
        if (cparams and "uri" in cparams)
        else False
    )

    eng_url = getattr(dialect, "_smarttrade_engine_url", None) or getattr(dialect, "url", None)
    if eng_url is None:
        raise UnauthorizedDatabaseAccessDenied(
            "CRITICAL SECURITY VIOLATION: SQLAlchemy Engine.do_connect has no verifiable URL provenance "
            "(eng_url is None; pre-existing Engine or unpatched dialect is FORBIDDEN)!"
        )
    if eng_url is not None:
        eng_url_str = str(eng_url)
        if "://" in eng_url_str:
            eng_scheme = eng_url_str.split("://", 1)[0].lower().split("+", 1)[0]
            if eng_scheme != "sqlite":
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: Non-SQLite SQLAlchemy URL scheme {eng_scheme!r} "
                    f"is strictly FORBIDDEN! URL: {eng_url_str!r}"
                )
        if _contains_forbidden_prod_reference(eng_url_str):
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Production database or data/ reference in SQLAlchemy URL "
                f"{eng_url_str!r} is strictly FORBIDDEN!"
            )
        if _contains_parent_traversal(eng_url_str.split("?", 1)[0]):
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Directory traversal ('..') in SQLAlchemy URL "
                f"{eng_url_str!r} is strictly FORBIDDEN!"
            )

        url_uri_flag = _extract_sa_url_uri_query_flag(eng_url_str)
        eng_extracted = extract_sqlite_url_path(eng_url_str, allow_sa_url=True, uri_mode=False)
        if "file::memory:" in eng_url_str.lower():
            verify_authorized_db(eng_url_str, caller_context="SQLAlchemy Engine.do_connect dialect.url")
            if not uri_flag:
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: SQLAlchemy URL {eng_url_str!r} targets 'file::memory:' "
                    f"but driver cparams did not receive uri=True (cparams={cparams!r})!"
                )
            if not cargs or not _coerce_db_target_to_str(
                cargs[0], caller_context="SQLAlchemy Engine.do_connect cargs[0]"
            ).startswith("file::memory:"):
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: SQLAlchemy URL {eng_url_str!r} produced unexpected "
                    f"DBAPI cargs={cargs!r}!"
                )
        elif eng_extracted == ":memory:":
            verify_authorized_db(eng_url_str, caller_context="SQLAlchemy Engine.do_connect dialect.url")
            # Standard SQLAlchemy in-memory URL (sqlite://, sqlite:///:memory:[?cache=shared])
            # MUST be translated by the dialect into exact cargs=[':memory:'] with no query string!
            if list(cargs) != [":memory:"]:
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: SQLAlchemy memory URL {eng_url_str!r} did not "
                    f"translate to exact cargs=[':memory:'] (got cargs={cargs!r})!"
                )
        elif url_uri_flag is not None and url_uri_flag != uri_flag:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Contradictory uri mode between SQLAlchemy URL "
                f"{eng_url_str!r} (url uri={url_uri_flag}) and driver cparams['uri']={uri_flag}!"
            )
        elif _is_unambiguous_absolute_os_path(eng_extracted):
            verify_authorized_db(eng_url_str, caller_context="SQLAlchemy Engine.do_connect dialect.url")
            eng_norm = normalize_db_target(eng_url_str, uri_mode=False)
            if cargs:
                cargs_norm = normalize_db_target(cargs[0], uri_mode=uri_flag)
                if cargs_norm != eng_norm:
                    raise UnauthorizedDatabaseAccessDenied(
                        f"CRITICAL SECURITY VIOLATION: SQLAlchemy URL {eng_url_str!r} (resolved={eng_norm!r}) "
                        f"diverges from transmitted DBAPI cargs[0]={cargs[0]!r} (resolved={cargs_norm!r})!"
                    )
        else:
            # RES-2.2 (Approach B): Relative SQLAlchemy file URL without `uri=true`:
            # - Forbidden if `uri_flag` is True (dialect does not absolutize URI paths).
            # - `eng_url` has already passed all CWD-independent checks above (SQLite scheme,
            #   no prod reference, no `..`, no `%xx`, no `#` fragment, valid query string).
            # - Treat `cargs[0]` (already absolutized by SQLAlchemy's dialect at create_engine time)
            #   as the authoritative effective DBAPI target: require it to be present, non-empty,
            #   an unambiguous absolute OS path, authorized, and DBAPI-coherent.
            if uri_flag:
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: Relative SQLAlchemy URL {eng_url_str!r} with uri=True "
                    "is FORBIDDEN (absolute path required)!"
                )
            if "#" in eng_url_str:
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: URI fragment ('#') in SQLAlchemy URL {eng_url_str!r} is FORBIDDEN!"
                )
            if not cargs:
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: Relative SQLAlchemy URL {eng_url_str!r} has no DBAPI cargs!"
                )
            cargs0_str = _coerce_db_target_to_str(
                cargs[0], caller_context="SQLAlchemy Engine.do_connect cargs[0]"
            )
            if not _is_unambiguous_absolute_os_path(cargs0_str):
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: Relative SQLAlchemy URL {eng_url_str!r} transmitted "
                    f"non-absolute DBAPI target cargs[0]={cargs[0]!r} (complete absolute path required)!"
                )
            verify_authorized_db(
                cargs[0],
                caller_context="SQLAlchemy Engine.do_connect cargs[0]",
                uri_mode=False,
            )
            _assert_raw_dbapi_target_coherent(
                cargs[0],
                uri_mode=False,
                caller_context="SQLAlchemy Engine.do_connect cargs[0]",
            )

    has_target = False
    if cargs:
        for idx, arg in enumerate(cargs):
            ctx = f"SQLAlchemy Engine.do_connect cargs[{idx}]"
            verify_authorized_db(arg, caller_context=ctx, uri_mode=uri_flag)
            _assert_raw_dbapi_target_coherent(arg, uri_mode=uri_flag, caller_context=ctx)
            has_target = True

    if cparams:
        unknown_cparams = set(cparams.keys()) - (_TARGET_SQLITE_CPARAM_KEYS | _KNOWN_SQLITE_NON_TARGET_CPARAM_KEYS)
        if unknown_cparams:
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: Unknown SQLite connect_args parameter(s) "
                f"{sorted(unknown_cparams)!r} in SQLAlchemy Engine.do_connect are FORBIDDEN!"
            )
        for k in _TARGET_SQLITE_CPARAM_KEYS:
            if k in cparams:
                ctx = f"SQLAlchemy Engine.do_connect cparams[{k}]"
                verify_authorized_db(cparams[k], caller_context=ctx, uri_mode=uri_flag)
                _assert_raw_dbapi_target_coherent(cparams[k], uri_mode=uri_flag, caller_context=ctx)
                has_target = True

    if not has_target:
        raise UnauthorizedDatabaseAccessDenied(
            "CRITICAL SECURITY VIOLATION: SQLAlchemy Engine.do_connect received no database target "
            f"in cargs={cargs!r} or cparams={cparams!r}!"
        )


def _validate_pool_checkout(dbapi_connection, connection_record, connection_proxy) -> None:
    """
    Validate every DBAPI connection on `Pool.checkout` (including reused idle pooled
    connections across `Pool` and all its subclasses: QueuePool, StaticPool, NullPool,
    SingletonThreadPool, and custom `Pool` subclasses — RES-3.2).
    - Requires `isinstance(dbapi_connection, GuardedSqlite3Connection)`.
    - Requires `getattr(dbapi_connection, "_authorizer_locked", False) is True`.
    - Note: a `Pool.checkout` listener NEVER intercepts a connection that was already
      checked out before `install_sqlalchemy_guard()` was called.
    """
    if not isinstance(dbapi_connection, GuardedSqlite3Connection):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION (SQLAlchemy Pool.checkout): Connection object "
            f"{type(dbapi_connection)!r} is NOT a GuardedSqlite3Connection (unverified provenance)!"
        )
    if getattr(dbapi_connection, "_authorizer_locked", False) is not True:
        raise UnauthorizedDatabaseAccessDenied(
            "CRITICAL SECURITY VIOLATION (SQLAlchemy Pool.checkout): GuardedSqlite3Connection "
            "does NOT have `_authorizer_locked is True`!"
        )


def _validate_create_engine_kwargs(args: tuple, kwargs: dict, *, pool_base_cls=None) -> None:
    """
    Validate `create_engine(*args, **kwargs)` arguments before any Engine or Pool is built (RES-3.2):
    - Strictly FORBIDS `creator=` (which bypasses `Engine.do_connect` in SQLAlchemy).
    - Requires any explicit `poolclass` to be a subclass of `sqlalchemy.pool.Pool`, and any
      explicit `pool` instance to be an instance of `sqlalchemy.pool.Pool` (so `Pool.connect`
      and `Pool.checkout` class listeners remain active).
    """
    if "creator" in kwargs and kwargs["creator"] is not None:
        raise UnauthorizedDatabaseAccessDenied(
            "CRITICAL SECURITY VIOLATION: create_engine(..., creator=...) is strictly FORBIDDEN "
            "in test mode (bypasses Engine.do_connect)!"
        )
    if pool_base_cls is not None:
        if "poolclass" in kwargs and kwargs["poolclass"] is not None:
            poolclass = kwargs["poolclass"]
            if not (isinstance(poolclass, type) and issubclass(poolclass, pool_base_cls)):
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: Custom poolclass {poolclass!r} is NOT a subclass "
                    f"of {pool_base_cls.__name__} and is FORBIDDEN!"
                )
        if "pool" in kwargs and kwargs["pool"] is not None:
            pool_obj = kwargs["pool"]
            if not isinstance(pool_obj, pool_base_cls):
                raise UnauthorizedDatabaseAccessDenied(
                    f"CRITICAL SECURITY VIOLATION: Custom pool {type(pool_obj)!r} is NOT an instance "
                    f"of {pool_base_cls.__name__} and is FORBIDDEN!"
                )


def _validate_direct_engine_init(pool, dialect, url, *, pool_base_cls=None) -> None:
    """
    Validate direct `Engine(pool, dialect, url, ...)` construction (RES-3.2):
    - Requires `pool` to be an instance of `sqlalchemy.pool.Pool` (when `pool_base_cls` is supplied).
    - Requires `dialect.name` to be exact `"sqlite"` (rejecting empty or non-SQLite dialects).
    - Does NOT fabricate `_smarttrade_engine_url` so provenance must still come from a patched dialect.
    """
    if pool_base_cls is not None and not isinstance(pool, pool_base_cls):
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION (Engine.__init__): pool {type(pool)!r} is NOT an instance "
            f"of {pool_base_cls.__name__}!"
        )
    dialect_name = getattr(dialect, "name", "")
    dialect_name_low = dialect_name.lower() if isinstance(dialect_name, str) else ""
    if dialect_name_low != "sqlite":
        raise UnauthorizedDatabaseAccessDenied(
            f"CRITICAL SECURITY VIOLATION (Engine.__init__): Non-SQLite or unnamed dialect "
            f"{dialect_name!r} is strictly FORBIDDEN!"
        )


def install_sqlalchemy_guard():
    global _SQLALCHEMY_GUARD_INSTALLED
    if _SQLALCHEMY_GUARD_INSTALLED:
        return

    try:
        import sqlalchemy
        import sqlalchemy.engine as sa_engine
        from sqlalchemy.engine import create as sa_create
        from sqlalchemy.engine import Engine
        from sqlalchemy.pool import Pool
        from sqlalchemy import event
    except ImportError as e:
        raise RuntimeError(f"FATAL SECURITY FAILURE: SQLAlchemy failed to import: {e}") from e

    orig_create_engine = sa_create.create_engine

    def _guarded_create_engine(*args, **kwargs):
        _validate_create_engine_kwargs(args, kwargs, pool_base_cls=Pool)
        return orig_create_engine(*args, **kwargs)

    sqlalchemy.create_engine = _guarded_create_engine
    sa_engine.create_engine = _guarded_create_engine
    sa_create.create_engine = _guarded_create_engine

    orig_engine_init = Engine.__init__

    def _guarded_engine_init(self, pool, dialect, url, *args, **kwargs):
        _validate_direct_engine_init(pool, dialect, url, pool_base_cls=Pool)
        return orig_engine_init(self, pool, dialect, url, *args, **kwargs)

    Engine.__init__ = _guarded_engine_init

    from sqlalchemy.dialects.sqlite.pysqlite import SQLiteDialect_pysqlite
    orig_create_connect_args = SQLiteDialect_pysqlite.create_connect_args

    def _guarded_create_connect_args(self, url):
        url_str = url.render_as_string(hide_password=False) if hasattr(url, "render_as_string") else str(url)
        try:
            setattr(self, "_smarttrade_engine_url", url_str)
        except Exception:
            pass
        return orig_create_connect_args(self, url)

    SQLiteDialect_pysqlite.create_connect_args = _guarded_create_connect_args

    @event.listens_for(Engine, "do_connect")
    def _guarded_sqlalchemy_do_connect(dialect, conn_rec, cargs, cparams):
        _validate_sqlalchemy_do_connect(dialect, conn_rec, cargs, cparams)

    @event.listens_for(Pool, "checkout")
    def _guarded_pool_checkout(dbapi_connection, connection_record, connection_proxy):
        _validate_pool_checkout(dbapi_connection, connection_record, connection_proxy)

    @event.listens_for(Pool, "connect")
    def _guarded_pool_connect(dbapi_connection, connection_record):
        install_connection_authorizer(dbapi_connection)
        if hasattr(dbapi_connection, "cursor"):
            try:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA database_list")
                rows = cursor.fetchall()
                for seq, name, file_path in rows:
                    if file_path:
                        verify_authorized_db(file_path, caller_context=f"SQLAlchemy Pool.connect ({name})")
            except Exception as e:
                try:
                    dbapi_connection.close()
                except Exception:
                    pass
                if isinstance(e, UnauthorizedDatabaseAccessDenied):
                    raise
                raise UnauthorizedDatabaseAccessDenied(
                    f"FATAL SECURITY VIOLATION: Failed to verify DB path via PRAGMA database_list: {e}"
                ) from e
        else:
            try:
                if hasattr(dbapi_connection, "close"):
                    dbapi_connection.close()
            except Exception:
                pass
            raise UnauthorizedDatabaseAccessDenied(
                f"CRITICAL SECURITY VIOLATION: DBAPI connection {type(dbapi_connection)!r} has no cursor() method!"
            )

    _SQLALCHEMY_GUARD_INSTALLED = True


# ── LEVEL 3: DETERMINISTIC TEST ENVIRONMENT INITIALIZATION ────────────────────
def setup_isolated_test_environment(profile: Optional[str] = None) -> str:
    """
    Configure isolated test database environment before any backend module is loaded.

    Profiles:
      - 'memory' (default): Pure in-memory SQLite ('sqlite:///:memory:'), zero disk files created.
      - 'temp_file': Dedicated temporary file DB inside tempfile.mkdtemp(prefix='pytest_safe_db_').
    """
    global _DB_PROFILE, _ISOLATED_TEMP_DIR, _ISOLATED_TEMP_DB_PATH, _ISOLATED_CANONICAL_DB

    chosen = (profile or os.getenv("SMARTTRADE_DB_PROFILE") or "memory").strip().lower()
    if chosen not in ("memory", "temp_file"):
        raise ValueError(f"Unsupported SMARTTRADE_DB_PROFILE={chosen!r}; expected 'memory' or 'temp_file'.")

    _DB_PROFILE = chosen
    os.environ["SMARTTRADE_TEST_MODE"] = "1"
    os.environ["SMARTTRADE_DISABLE_DOTENV"] = "1"
    os.environ["SMARTTRADE_SKIP_APP_LIFESPAN"] = "1"

    install_sqlite3_guard()
    install_sqlalchemy_guard()

    if chosen == "memory":
        _ISOLATED_TEMP_DIR = None
        _ISOLATED_TEMP_DB_PATH = ":memory:"
        _ISOLATED_CANONICAL_DB = ":memory:"
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
        os.environ["TRADES_DB_PATH"] = ":memory:"
        os.environ["SMARTTRADE_DB_PATH"] = ":memory:"
        return ":memory:"

    if _ISOLATED_TEMP_DB_PATH and _ISOLATED_TEMP_DB_PATH != ":memory:" and os.path.exists(_ISOLATED_TEMP_DB_PATH):
        authorize_temp_db_root(os.path.dirname(_ISOLATED_TEMP_DB_PATH))
        return _ISOLATED_TEMP_DB_PATH

    _ISOLATED_TEMP_DIR = tempfile.mkdtemp(prefix="pytest_safe_db_")
    authorize_temp_db_root(_ISOLATED_TEMP_DIR)
    _ISOLATED_TEMP_DB_PATH = os.path.normpath(os.path.join(_ISOLATED_TEMP_DIR, "test_isolated.db"))
    _ISOLATED_CANONICAL_DB = os.path.normcase(os.path.realpath(_ISOLATED_TEMP_DB_PATH))

    test_db_uri = f"sqlite:///{_ISOLATED_TEMP_DB_PATH.replace(os.sep, '/')}"
    os.environ["DATABASE_URL"] = test_db_uri
    os.environ["TRADES_DB_PATH"] = _ISOLATED_TEMP_DB_PATH
    os.environ["SMARTTRADE_DB_PATH"] = _ISOLATED_TEMP_DB_PATH
    return _ISOLATED_TEMP_DB_PATH
