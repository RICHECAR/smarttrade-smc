"""
Centralized, test-safe environment (.env) loader.

Separates two orthogonal concerns:
1. `is_test_mode()`: True ONLY when `SMARTTRADE_TEST_MODE` is truthy or `APP_ENV=test`.
   Controls test-only security guards (DB allowlist, startup hook suppression).
   Never activated merely by `SMARTTRADE_DISABLE_DOTENV=1`, so production deployments
   that inject secrets via OS environment variables and disable `.env` files keep
   100% of their production runtime behavior.
2. `is_dotenv_disabled()`: True when `is_test_mode()` is True OR when
   `SMARTTRADE_DISABLE_DOTENV` is truthy. Used exclusively by `safe_load_dotenv()`
   to skip reading physical `.env` files from disk.
"""

import os

_TRUTHY = frozenset({"1", "true", "yes"})


def is_test_mode() -> bool:
    """Return True strictly when running under isolated test mode."""
    return (
        os.getenv("SMARTTRADE_TEST_MODE", "").strip().lower() in _TRUTHY
        or os.getenv("APP_ENV", "").strip().lower() == "test"
    )


def is_dotenv_disabled() -> bool:
    """Return True when physical `.env` file loading should be skipped."""
    return (
        is_test_mode()
        or os.getenv("SMARTTRADE_DISABLE_DOTENV", "").strip().lower() in _TRUTHY
    )


def safe_load_dotenv(*args, **kwargs) -> bool:
    """Load `.env` only when dotenv loading is not disabled."""
    if is_dotenv_disabled():
        return False
    from dotenv import load_dotenv
    return bool(load_dotenv(*args, **kwargs))
