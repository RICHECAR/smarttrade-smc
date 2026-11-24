# Database module (lazy re-exports to prevent import-time engine creation)
from backend.db.base import Base

__all__ = [
    "Base",
    "engine",
    "SessionLocal",
    "get_db",
    "configure_database",
    "get_engine",
    "get_session_factory",
]


def __getattr__(name: str):
    if name in ("engine", "SessionLocal", "get_db", "configure_database", "get_engine", "get_session_factory"):
        import backend.db.session as _sess
        return getattr(_sess, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
