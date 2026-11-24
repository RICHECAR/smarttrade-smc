"""
Stateful fake exchange helper for Kraken executor tests.

The executor now CONFIRMS the executed quantity through real fills and the live position
before placing SL/TP (it never falls back to the requested quantity). Test doubles therefore
need to expose get_fills() / get_positions() consistent with the orders they accept.

make_exchange_stateful(client) wraps an existing fake client so that:
  - accepted opening market/limit orders create a fill + a position,
  - accepted reduce-only orders reduce the position,
  - get_fills() / get_positions() reflect that state.
Nothing is mocked out of the executor itself.
"""


def _as_dict(res):
    if isinstance(res, dict):
        return res
    if hasattr(res, "to_dict"):
        return res.to_dict()
    return {"success": getattr(res, "success", False), "order_id": getattr(res, "order_id", None)}


def make_exchange_stateful(client, fill_ratio=1.0):
    state = {"fills": [], "positions": {}}

    def wrap(name):
        orig = getattr(client, name, None)
        if orig is None:
            return

        async def wrapped(*args, _orig=orig, **kw):
            res = await _orig(*args, **kw)
            d = _as_dict(res)
            if d.get("success") is True or d.get("code") == "00000":
                symbol = kw.get("symbol")
                side = kw.get("side")
                size = float(kw.get("size") or 0)
                executed = d.get("filled_size")
                executed = float(executed) if executed is not None else size * fill_ratio
                if kw.get("reduce_only"):
                    key = (symbol, "long" if side == "sell" else "short")
                    state["positions"][key] = max(0.0, state["positions"].get(key, 0.0) - executed)
                elif executed > 0:
                    key = (symbol, "long" if side == "buy" else "short")
                    state["positions"][key] = state["positions"].get(key, 0.0) + executed
                    state["fills"].append(
                        {"order_id": d.get("order_id"), "symbol": symbol, "side": side, "size": executed}
                    )
            return res

        setattr(client, name, wrapped)

    for n in ("place_market_order", "place_limit_order"):
        wrap(n)

    async def get_fills(last_fill_time=None):
        return list(state["fills"])

    async def get_positions(force_refresh=False):
        return [
            {"symbol": s, "side": side, "size": qty}
            for (s, side), qty in state["positions"].items()
            if qty > 0
        ]

    client.get_fills = get_fills
    client.get_positions = get_positions
    client._fake_state = state
    return client
