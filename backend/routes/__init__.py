# Backend routes module (lazy re-exports to prevent eager DB/session imports)
__all__ = [
    "trades_router",
    "stats_router",
    "live_trades_router",
    "governance_router",
    "trades",
    "stats",
]


def __getattr__(name: str):
    if name == "trades_router":
        from backend.routes.trades import router
        return router
    if name == "stats_router":
        from backend.routes.stats import router
        return router
    if name == "live_trades_router":
        from backend.routes.live_trades import router
        return router
    if name == "governance_router":
        from backend.routes.governance import router
        return router
    if name == "trades":
        import backend.routes.trades as mod
        return mod
    if name == "stats":
        import backend.routes.stats as mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
