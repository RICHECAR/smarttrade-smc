"""
Phase 1.9.10 — Tests for Lot 2 (Strict VWAP), Lots 3+4 (Post-Fill Risk Enforcement &
Verified Reduction/Emergency Close), and Lot 5 (Strict Fees & Slippage).

100% offline, simulated Kraken REST/WS, zero real network calls, zero .env/SQLite mutations.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def _isolate_live_trades_db():
    """Never allow unit tests to write synthetic rows into production data/trades.db."""
    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=None), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=None), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):
        yield

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)
from backend.market_data.kraken_futures_book_feed import KrakenFuturesBookFeed


class _FakeRest:
    def __init__(self, pos_sequence: Optional[List[Any]] = None) -> None:
        self.sent_orders: List[Dict[str, Any]] = []
        self._pos_seq = list(pos_sequence if pos_sequence is not None else [0.0])

    def next_pos(self) -> Any:
        if len(self._pos_seq) > 1:
            return self._pos_seq.pop(0)
        return self._pos_seq[0]

    async def get_positions(self, **kwargs):
        p = self.next_pos()
        if isinstance(p, Exception):
            raise p
        if p is None:
            return []
        if p > 0:
            return [{"symbol": "PF_ETHUSD", "side": "long", "size": p}]
        return []

    async def get_open_orders(self, **kwargs):
        return []


def _make_wrapper(
    monkeypatch,
    *,
    max_risk_usd: str = "4.50",
    taker_fee_bps: Optional[str] = "5.0",
    slippage_bps: Optional[str] = "0.0",
    pos_seq: Optional[List[Any]] = None,
) -> tuple[KrakenFuturesExecutorWrapper, _FakeRest]:
    monkeypatch.setenv("KRAKEN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_MAX_TRADE_RISK_USD", max_risk_usd)
    if taker_fee_bps is not None:
        monkeypatch.setenv("KRAKEN_TAKER_FEE_BPS", taker_fee_bps)
    else:
        monkeypatch.delenv("KRAKEN_TAKER_FEE_BPS", raising=False)
    if slippage_bps is not None:
        monkeypatch.setenv("KRAKEN_RISK_SLIPPAGE_BPS", slippage_bps)
    else:
        monkeypatch.delenv("KRAKEN_RISK_SLIPPAGE_BPS", raising=False)
    monkeypatch.delenv("KRAKEN_MAX_SLIPPAGE_BPS", raising=False)

    rest = _FakeRest(pos_sequence=pos_seq)
    w = KrakenFuturesExecutorWrapper(api_key="t", api_secret="t", single_entry_mode=False)
    w._rest_client = rest
    w._global_entry_lock = False
    w._global_entry_lock_reason = None
    w._startup_reconciliation_completed = True

    feed = KrakenFuturesBookFeed(symbols=["PF_ETHUSD"])
    feed.on_connected()
    feed.process_message({
        "feed": "book_snapshot", "product_id": "PF_ETHUSD", "seq": 1, "timestamp": 1700000000000,
        "bids": [{"price": 2999.5, "qty": 10.0}], "asks": [{"price": 3000.0, "qty": 10.0}],
    })
    w.set_book_feed(feed)
    return w, rest


# ══════════════════════════════════════════════════════════════════════════════
# LOT 2: STRICT VWAP
# ══════════════════════════════════════════════════════════════════════════════


def test_lot2_1_multiple_fills_weighted_vwap_exact():
    fills = [
        {"order_id": "O1", "size": 0.02, "price": 3000.0},
        {"order_id": "O1", "size": 0.03, "price": 3010.0},
    ]
    ok, qty, vwap, err = KrakenFuturesExecutorWrapper._compute_strict_fills_vwap(fills)
    assert ok is True and err is None
    assert qty == pytest.approx(0.05)
    assert vwap == pytest.approx((0.02 * 3000.0 + 0.03 * 3010.0) / 0.05)


@pytest.mark.parametrize("bad_fills", [
    [],
    ["not_a_dict"],
    [{"size": 0.05, "price": 0.0}],
    [{"size": 0.05, "price": -100.0}],
    [{"size": 0.05, "price": float("nan")}],
    [{"size": 0.05, "price": float("inf")}],
    [{"size": 0.05, "price": True}],
    [{"size": 0.05, "price": None}],
    [{"size": 0.0, "price": 3000.0}],
    [{"size": "bad", "price": 3000.0}],
])
def test_lot2_2_absent_zero_malformed_fills_rejected_strictly(bad_fills):
    ok, qty, vwap, err = KrakenFuturesExecutorWrapper._compute_strict_fills_vwap(bad_fills)
    assert ok is False and qty is None and vwap is None and err is not None


@pytest.mark.asyncio
async def test_lot2_3_confirm_executed_qty_partial_fill_and_inconsistent_position(monkeypatch):
    w, _ = _make_wrapper(monkeypatch)

    async def _fills():
        return [{"order_id": "O1", "size": 0.05, "price": 3005.0}]

    async def _pos(sym, side):
        return 0.03  # smaller than fills_qty -> capped to min(0.05, 0.03)

    w._fetch_fills_strict = _fills  # type: ignore[method-assign]
    w._fetch_position_qty_strict = _pos  # type: ignore[method-assign]

    conf = await w._confirm_executed_qty("PF_ETHUSD", "long", "O1", 0.05, client_oid="C1")
    assert conf["state"] == "CONFIRMED"
    assert conf["qty"] == pytest.approx(0.03)
    assert conf["vwap"] == pytest.approx(3005.0)
    assert conf["vwap_verified"] is True


@pytest.mark.asyncio
async def test_lot2_4_missing_real_vwap_quarantines_and_never_falls_back_to_entry_price(monkeypatch):
    w, rest = _make_wrapper(monkeypatch)

    async def _fake_rest(**kw):
        rest.sent_orders.append(kw)
        return {"success": True, "code": "00000", "order_id": "O_NO_VWAP", "filled_size": 0.05}

    async def _fake_conf(*a, **kw):
        # Confirmed qty from response_filled, but NO fills -> vwap is None & vwap_verified=False!
        return {
            "state": "CONFIRMED", "qty": 0.05, "source": "fills+position",
            "fills_qty": 0.05, "position_qty": 0.05, "matched_fills": [],
            "vwap": None, "vwap_verified": False, "vwap_error": "No fills",
        }

    async def _fake_pos(sym, side):
        return 0.05

    w._place_order_rest = _fake_rest  # type: ignore[method-assign]
    w._confirm_executed_qty = _fake_conf  # type: ignore[method-assign]
    w._fetch_position_qty_strict = _fake_pos  # type: ignore[method-assign]

    res = await w.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.05, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, reduce_only=False, intent="open",
    )
    assert res["success"] is False
    assert res["code"] == "UNCONFIRMED_REAL_VWAP"
    assert w._global_entry_lock is True
    assert "PF_ETHUSD" in w._reconciliation_required_symbols

    # Subsequent OPEN is blocked after lock/quarantine
    res2 = await w.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.05, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, reduce_only=False, intent="open",
    )
    assert res2["success"] is False
    assert res2["code"] == "GLOBAL_ENTRY_LOCK_ACTIVE"


# ══════════════════════════════════════════════════════════════════════════════
# LOTS 3 + 4: POST-FILL REAL RISK ENFORCEMENT & VERIFIED REDUCTION / EM-CLOSE
# ══════════════════════════════════════════════════════════════════════════════


def _make_tracker(qty: float = 0.10, vwap: float = 3060.0, sl: float = 2970.0) -> KrakenTradeTracker:
    return KrakenTradeTracker(
        order_id="O_PF", client_oid="C_PF", symbol="PF_ETHUSD", side="long",
        filled_qty=qty, expected_exposure=qty, entry_price=vwap, status="OPEN",
        sl_price=sl,
    )


@pytest.mark.asyncio
async def test_lot34_1_post_fill_within_cap_passes_cleanly(monkeypatch):
    w, _ = _make_wrapper(monkeypatch, max_risk_usd="4.50", taker_fee_bps="5.0")
    tracker = _make_tracker(qty=0.10, vwap=3000.0, sl=2970.0)
    # dist=30 -> 0.10 * 30 = $3.00; fee = 0.10 * 3000 * 0.001 = $0.30; total = $3.30 <= $4.50
    res: Dict[str, Any] = {"success": True}
    ok, new_qty, out = await w._enforce_post_fill_risk_cap(
        kraken_symbol="PF_ETHUSD", position_side="long", close_side="sell",
        protect_qty=0.10, real_vwap=3000.0, confirmed_sl=2970.0,
        contract_multiplier=1.0, max_risk_cap=4.50, taker_fee_bps=5.0, slippage_bps=0.0,
        tracker=tracker, result=res,
    )
    assert ok is True and new_qty == pytest.approx(0.10)
    assert out["post_fill_risk_ok"] is True
    assert out["post_fill_risk_usd"] == pytest.approx(3.30, rel=1e-4)


@pytest.mark.asyncio
async def test_lot34_2_unconfirmed_sl_quarantines(monkeypatch):
    w, _ = _make_wrapper(monkeypatch)
    tracker = _make_tracker()
    ok, _, out = await w._enforce_post_fill_risk_cap(
        kraken_symbol="PF_ETHUSD", position_side="long", close_side="sell",
        protect_qty=0.10, real_vwap=3000.0, confirmed_sl=None,
        contract_multiplier=1.0, max_risk_cap=4.50, taker_fee_bps=5.0, slippage_bps=0.0,
        tracker=tracker, result={},
    )
    assert ok is False and out["code"] == "POST_FILL_SL_UNCONFIRMED"
    assert w._global_entry_lock is True and "PF_ETHUSD" in w._reconciliation_required_symbols


@pytest.mark.asyncio
async def test_lot34_3_breach_triggers_verified_partial_reduction(monkeypatch):
    # Real VWAP slipped to 3060 -> dist=90 -> per_unit_risk = 90 + 3060*0.001 = 93.06
    # 0.10 * 93.06 = $9.306 > $4.50 -> max_safe_qty = floor(4.50/93.06, 3) = 0.048, reduce_qty = 0.052
    w, rest = _make_wrapper(monkeypatch)
    tracker = _make_tracker(qty=0.10, vwap=3060.0, sl=2970.0)
    pos_calls = [0.10, 0.048]

    async def _pos(sym, side):
        return pos_calls.pop(0)

    async def _send(**kw):
        rest.sent_orders.append(kw)
        assert kw["reduce_only"] is True and kw["side"] == "sell"
        return {"success": True, "code": "00000", "order_id": "RED_1"}

    async def _place_sl(**kw):
        return {"success": True, "order_id": "SL_REPLACED", "verified": True}

    async def _cancel_sl(**kw):
        return {"success": True, "code": "00000"}

    w._fetch_position_qty_strict = _pos  # type: ignore[method-assign]
    w._place_order_rest = _send  # type: ignore[method-assign]
    w._place_stop_order_rest = _place_sl  # type: ignore[method-assign]
    w.cancel_order = _cancel_sl  # type: ignore[method-assign]

    ok, new_qty, out = await w._enforce_post_fill_risk_cap(
        kraken_symbol="PF_ETHUSD", position_side="long", close_side="sell",
        protect_qty=0.10, real_vwap=3060.0, confirmed_sl=2970.0,
        contract_multiplier=1.0, max_risk_cap=4.50, taker_fee_bps=5.0, slippage_bps=0.0,
        tracker=tracker, result={"success": True},
    )
    assert ok is True
    assert new_qty == pytest.approx(0.048)
    assert out["post_fill_reduced"] is True
    assert out["post_fill_risk_ok"] is True
    assert out["post_fill_risk_usd"] <= 4.50 + 1e-6
    assert len(rest.sent_orders) == 1
    assert rest.sent_orders[0]["reduce_only"] is True


@pytest.mark.asyncio
async def test_lot34_4_failed_reduction_escalates_to_verified_emergency_close(monkeypatch):
    w, rest = _make_wrapper(monkeypatch)
    tracker = _make_tracker(qty=0.10, vwap=3060.0, sl=2970.0)
    pos_calls = [0.10, 0.0]  # before reduction -> after emergency close flat

    async def _pos(sym, side):
        return pos_calls.pop(0)

    call_idx = 0

    async def _send(**kw):
        nonlocal call_idx
        call_idx += 1
        rest.sent_orders.append(kw)
        if call_idx == 1:
            return {"success": False, "code": "ORDER_REJECTED"}  # partial reduction fails
        return {"success": True, "code": "00000", "order_id": "EM_1"}  # emergency close succeeds

    async def _place_sl(**kw):
        return {"success": True, "order_id": "SL_REPLACED", "verified": True}

    async def _cancel_sl(**kw):
        return {"success": True, "code": "00000"}

    w._fetch_position_qty_strict = _pos  # type: ignore[method-assign]
    w._place_order_rest = _send  # type: ignore[method-assign]
    w._place_stop_order_rest = _place_sl  # type: ignore[method-assign]
    w.cancel_order = _cancel_sl  # type: ignore[method-assign]

    ok, new_qty, out = await w._enforce_post_fill_risk_cap(
        kraken_symbol="PF_ETHUSD", position_side="long", close_side="sell",
        protect_qty=0.10, real_vwap=3060.0, confirmed_sl=2970.0,
        contract_multiplier=1.0, max_risk_cap=4.50, taker_fee_bps=5.0, slippage_bps=0.0,
        tracker=tracker, result={"success": True},
    )
    assert ok is False and new_qty == 0.0
    assert out["code"] == "POST_FILL_RISK_EMERGENCY_CLOSED"
    assert out["emergency_close_confirmed"] is True
    assert tracker.status == "CLOSED"
    assert len(rest.sent_orders) == 2


@pytest.mark.asyncio
async def test_lot34_5_unconfirmed_emergency_close_or_residual_position_quarantines(monkeypatch):
    w, rest = _make_wrapper(monkeypatch)
    tracker = _make_tracker(qty=0.005, vwap=4000.0, sl=2970.0)
    # per_unit_risk > 1030 -> max_safe_qty = 0.004 >= min_sz, force residual after close
    pos_calls = [0.005, 0.005, 0.002]  # residual 0.002 remains even though API said success!

    async def _pos(sym, side):
        return pos_calls.pop(0)

    async def _send(**kw):
        rest.sent_orders.append(kw)
        return {"success": True, "code": "00000"}

    async def _place_sl(**kw):
        return {"success": True, "order_id": "SL_REPLACED", "verified": True}

    async def _cancel_sl(**kw):
        return {"success": True, "code": "00000"}

    w._fetch_position_qty_strict = _pos  # type: ignore[method-assign]
    w._place_order_rest = _send  # type: ignore[method-assign]
    w._place_stop_order_rest = _place_sl  # type: ignore[method-assign]
    w.cancel_order = _cancel_sl  # type: ignore[method-assign]

    ok, _, out = await w._enforce_post_fill_risk_cap(
        kraken_symbol="PF_ETHUSD", position_side="long", close_side="sell",
        protect_qty=0.005, real_vwap=4000.0, confirmed_sl=2970.0,
        contract_multiplier=1.0, max_risk_cap=4.50, taker_fee_bps=5.0, slippage_bps=0.0,
        tracker=tracker, result={"success": True},
    )
    assert ok is False
    assert out["code"] == "POST_FILL_RISK_UNCONFIRMED_QUARANTINED"
    assert out["status"] == "MANUAL_RECONCILIATION_REQUIRED"
    assert w._global_entry_lock is True
    assert "PF_ETHUSD" in w._reconciliation_required_symbols


@pytest.mark.asyncio
async def test_lot34_6_api_error_during_position_check_quarantines(monkeypatch):
    w, _ = _make_wrapper(monkeypatch)
    tracker = _make_tracker(qty=0.10, vwap=3060.0, sl=2970.0)

    async def _pos_err(sym, side):
        raise RuntimeError("Kraken REST 503")

    w._fetch_position_qty_strict = _pos_err  # type: ignore[method-assign]

    ok, _, out = await w._enforce_post_fill_risk_cap(
        kraken_symbol="PF_ETHUSD", position_side="long", close_side="sell",
        protect_qty=0.10, real_vwap=3060.0, confirmed_sl=2970.0,
        contract_multiplier=1.0, max_risk_cap=4.50, taker_fee_bps=5.0, slippage_bps=0.0,
        tracker=tracker, result={"success": True},
    )
    assert ok is False
    assert out["code"] == "POST_FILL_RISK_POSITION_UNVERIFIED"
    assert w._global_entry_lock is True


# ══════════════════════════════════════════════════════════════════════════════
# LOT 5: FEES & SLIPPAGE + CLOSE/REDUCE NON-REGRESSION
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lot5_1_missing_fee_rate_refuses_open_fail_closed(monkeypatch):
    w, rest = _make_wrapper(monkeypatch, taker_fee_bps=None)
    res = await w.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.05, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, reduce_only=False, intent="open",
    )
    assert res["success"] is False
    assert res["code"] == "MISSING_FEE_RATE"
    assert len(rest.sent_orders) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_fee", ["", "-1", "nan", "inf", "abc"])
async def test_lot5_2_invalid_fee_rate_refused(monkeypatch, bad_fee):
    w, rest = _make_wrapper(monkeypatch, taker_fee_bps=bad_fee)
    res = await w.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.05, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, reduce_only=False, intent="open",
    )
    assert res["success"] is False
    assert res["code"] == "INVALID_FEE_RATE"
    assert len(rest.sent_orders) == 0


@pytest.mark.asyncio
async def test_lot5_3_excessive_slippage_and_fee_pushes_over_4_50_cap(monkeypatch):
    # 1. Excessive slippage buffer > KRAKEN_MAX_SLIPPAGE_BPS -> SLIPPAGE_EXCEEDS_LIMIT
    w1, r1 = _make_wrapper(monkeypatch, taker_fee_bps="5.0", slippage_bps="150.0")
    monkeypatch.setenv("KRAKEN_MAX_SLIPPAGE_BPS", "50.0")
    res_slip = await w1.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.05, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, reduce_only=False, intent="open",
    )
    assert res_slip["success"] is False and res_slip["code"] == "SLIPPAGE_EXCEEDS_LIMIT"
    assert len(r1.sent_orders) == 0

    # 2. Distance risk = 0.14 * 30 = $4.20 <= $4.50, BUT round-trip fee (10 bps) + slip (10 bps) on $420 notional = $0.84 -> $5.04 > $4.50!
    w2, r2 = _make_wrapper(monkeypatch, max_risk_usd="4.50", taker_fee_bps="5.0", slippage_bps="10.0")
    res_cap = await w2.place_order(
        symbol="PF_ETHUSD", side="buy", quantity=0.14, order_type="market",
        entry_price=3000.0, stop_loss=2970.0, reduce_only=False, intent="open",
    )
    assert res_cap["success"] is False and res_cap["code"] == "RISK_EXCEEDS_BUDGET"
    assert len(r2.sent_orders) == 0


@pytest.mark.asyncio
async def test_lot5_4_close_and_reduce_remain_operational_without_fee_rate(monkeypatch):
    w, rest = _make_wrapper(monkeypatch, taker_fee_bps=None, pos_seq=[0.05, 0.05])
    monkeypatch.delenv("KRAKEN_MAX_TRADE_RISK_USD", raising=False)
    w._active_trackers["PF_ETHUSD"] = _make_tracker(qty=0.05, vwap=3000.0, sl=2970.0)

    async def _send(**kw):
        rest.sent_orders.append(kw)
        return {"success": False, "code": "UNIT_TEST_REACHED_REST_SEND", "error": "ok"}

    w._place_order_rest = _send  # type: ignore[method-assign]

    r_red = await w.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.02, order_type="market",
        reduce_only=True, intent="reduce",
    )
    r_cls = await w.place_order(
        symbol="PF_ETHUSD", side="sell", quantity=0.05, order_type="market",
        reduce_only=True, intent="close",
    )
    assert r_red["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert r_cls["code"] == "UNIT_TEST_REACHED_REST_SEND"
    assert len(rest.sent_orders) == 2
