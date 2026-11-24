"""Phase 1.9.10-R1 adversarial tests for P0-1, P1-1, P1-2, P1-3, P2-1, P2-2 fixes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)
from tests.test_phase1_9_10_post_fill_protections import _make_tracker, _make_wrapper


# ── P1-1: Fill deduplication ─────────────────────────────────────────────────

def test_p1_1_duplicate_fill_ids_are_deduplicated():
    fills = [
        {"fill_id": "F1", "order_id": "O1", "size": 0.1, "price": 100.0},
        {"fill_id": "F1", "order_id": "O1", "size": 0.1, "price": 100.0},
        {"fill_id": "F2", "order_id": "O1", "size": 0.1, "price": 200.0},
    ]
    ok, qty, vwap, err = KrakenFuturesExecutorWrapper._compute_strict_fills_vwap(fills)
    assert ok is True and err is None
    assert qty == pytest.approx(0.2)
    assert vwap == pytest.approx(150.0)


# ── P2-1 & P2-2: Fee precedence max(env, kwarg) & default 100 bps slippage cap ─

def test_p2_1_kwarg_cannot_lower_env_taker_fee(monkeypatch):
    monkeypatch.setenv("KRAKEN_TAKER_FEE_BPS", "5.0")
    monkeypatch.delenv("KRAKEN_MAX_SLIPPAGE_BPS", raising=False)
    w, _ = _make_wrapper(monkeypatch, taker_fee_bps="5.0")
    ok, fee, slip, err, code = w._resolve_fee_and_slippage_bps({"taker_fee_bps": 0.0})
    assert ok is True and err is None and code is None
    assert fee == pytest.approx(5.0)

    ok2, fee2, _, _, _ = w._resolve_fee_and_slippage_bps({"taker_fee_bps": 8.0})
    assert ok2 is True and fee2 == pytest.approx(8.0)


def test_p2_2_default_max_slippage_cap_100_bps(monkeypatch):
    monkeypatch.delenv("KRAKEN_MAX_SLIPPAGE_BPS", raising=False)
    w, _ = _make_wrapper(monkeypatch, taker_fee_bps="5.0")
    ok, _, _, _, code = w._resolve_fee_and_slippage_bps({"slippage_buffer_bps": 500.0})
    assert ok is False and code == "SLIPPAGE_EXCEEDS_LIMIT"


# ── P0-1: UNCONFIRMED_REAL_VWAP performs verified emergency close (never naked) ─

@pytest.mark.asyncio
async def test_p0_1_unconfirmed_vwap_emergency_closes_and_quarantines(monkeypatch):
    w, rest = _make_wrapper(monkeypatch, max_risk_usd="4.50", taker_fee_bps="5.0")
    # Entry filled (qty=0.05) but fills API returned no fills -> vwap_verified=False
    w._confirm_executed_qty = AsyncMock(
        return_value={
            "state": "CONFIRMED",
            "qty": 0.05,
            "source": "fills+position",
            "fills_qty": 0.05,
            "position_qty": 0.05,
            "matched_fills": [],
            "vwap": None,
            "vwap_verified": False,
            "vwap_error": "No confirmed fills available",
        }
    )
    pos_seq = [0.05, 0.0]  # live exposure before close -> confirmed flat after close
    w._fetch_position_qty_strict = AsyncMock(side_effect=lambda *a, **kw: pos_seq.pop(0))

    sent_calls = []

    async def _place_rest(**kw):
        sent_calls.append(kw)
        return {"success": True, "code": "00000", "order_id": f"ORD_{len(sent_calls)}", "filled_size": 0.05}

    w._place_order_rest = _place_rest  # type: ignore[method-assign]

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=None), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined"), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade"):
        res = await w.place_order(
            symbol="PF_ETHUSD",
            side="buy",
            quantity=0.05,
            order_type="market",
            entry_price=3000.0,
            stop_loss=2970.0,
            reduce_only=False,
            intent="open",
        )
    assert res["success"] is False
    assert res["code"] == "UNCONFIRMED_REAL_VWAP"
    assert res["emergency_close_attempted"] is True
    assert res["emergency_close_confirmed"] is True
    assert res["live_exposure"] == pytest.approx(0.0)
    assert w._global_entry_lock is True
    assert "PF_ETHUSD" in w._reconciliation_required_symbols
    # Second REST order must be the reduce_only=True emergency close on the opposite side
    assert len(sent_calls) == 2
    assert sent_calls[1]["reduce_only"] is True
    assert sent_calls[1]["side"] == "sell"
    assert sent_calls[1]["quantity"] == pytest.approx(0.05)


# ── P1-3: Partial reduction replaces SL FIRST, then cancels old SL ───────────

@pytest.mark.asyncio
async def test_p1_3_partial_reduction_replaces_sl_then_cancels_old(monkeypatch):
    w, rest = _make_wrapper(monkeypatch)
    tracker = _make_tracker(qty=0.10, vwap=3060.0, sl=2970.0)
    tracker.sl_order_id = "SL_OLD"
    pos_calls = [0.10, 0.048]
    call_order = []

    w._fetch_position_qty_strict = AsyncMock(side_effect=lambda *a, **kw: pos_calls.pop(0))
    w._place_order_rest = AsyncMock(return_value={"success": True, "code": "00000", "order_id": "RED_1"})

    async def _place_sl(**kw):
        call_order.append(("place_new_sl", kw["size"], kw["stop_price"]))
        return {"success": True, "order_id": "SL_NEW", "verified": True}

    async def _cancel(**kw):
        call_order.append(("cancel_old_sl", kw["order_id"]))
        return {"success": True, "code": "00000"}

    w._place_stop_order_rest = _place_sl  # type: ignore[method-assign]
    w.cancel_order = _cancel  # type: ignore[method-assign]

    ok, new_qty, out = await w._enforce_post_fill_risk_cap(
        kraken_symbol="PF_ETHUSD", position_side="long", close_side="sell",
        protect_qty=0.10, real_vwap=3060.0, confirmed_sl=2970.0,
        contract_multiplier=1.0, max_risk_cap=4.50, taker_fee_bps=5.0, slippage_bps=0.0,
        tracker=tracker, result={"success": True},
    )
    assert ok is True
    assert new_qty == pytest.approx(0.048)
    assert out["sl_resized"] is True
    assert out["old_sl_cancelled"] is True
    assert tracker.sl_order_id == "SL_NEW"
    # Critical invariant: NEW SL placed & verified BEFORE OLD SL cancelled
    assert call_order == [
        ("place_new_sl", pytest.approx(0.048), pytest.approx(2970.0)),
        ("cancel_old_sl", "SL_OLD"),
    ]


@pytest.mark.asyncio
async def test_p1_3_replacement_sl_failure_escalates_to_emergency_close_and_cancels_orphan(monkeypatch):
    w, rest = _make_wrapper(monkeypatch)
    tracker = _make_tracker(qty=0.10, vwap=3060.0, sl=2970.0)
    tracker.sl_order_id = "SL_OLD"
    pos_calls = [0.10, 0.048, 0.0]  # pre-red -> post-red -> post-emergency-close flat
    w._fetch_position_qty_strict = AsyncMock(side_effect=lambda *a, **kw: pos_calls.pop(0))
    w._place_order_rest = AsyncMock(return_value={"success": True, "code": "00000", "order_id": "ORD_X"})
    w._place_stop_order_rest = AsyncMock(return_value={"success": False, "error": "SL rejected"})
    w.cancel_order = AsyncMock(return_value={"success": True, "code": "00000"})

    ok, new_qty, out = await w._enforce_post_fill_risk_cap(
        kraken_symbol="PF_ETHUSD", position_side="long", close_side="sell",
        protect_qty=0.10, real_vwap=3060.0, confirmed_sl=2970.0,
        contract_multiplier=1.0, max_risk_cap=4.50, taker_fee_bps=5.0, slippage_bps=0.0,
        tracker=tracker, result={"success": True},
    )
    assert ok is False and new_qty == 0.0
    assert out["code"] == "POST_FILL_RISK_EMERGENCY_CLOSED"
    assert out["sl_resized"] is False
    assert out["orphan_sl_cancelled"] is True
    w.cancel_order.assert_awaited_once_with(symbol="PF_ETHUSD", order_id="SL_OLD")


# ── P1-2: SQLite close / quarantine sync in _enforce_post_fill_risk_cap ──────

@pytest.mark.asyncio
async def test_p1_2_emergency_close_persists_sqlite_and_quarantines_on_db_desync(monkeypatch):
    w, _ = _make_wrapper(monkeypatch)
    tracker = _make_tracker(qty=0.10, vwap=3060.0, sl=2970.0)
    tracker.sl_order_id = "SL_OLD"
    pos_calls = [0.10, 0.0]
    w._fetch_position_qty_strict = AsyncMock(side_effect=lambda *a, **kw: pos_calls.pop(0))
    w._place_order_rest = AsyncMock(return_value={"success": True, "code": "00000", "order_id": "EM_1"})
    w.cancel_order = AsyncMock(return_value={"success": True, "code": "00000"})

    # 1. DB close succeeds
    with patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()) as m_close:
        ok, qty, out = await w._enforce_post_fill_risk_cap(
            kraken_symbol="PF_ETHUSD", position_side="long", close_side="sell",
            protect_qty=0.10, real_vwap=3060.0, confirmed_sl=3070.0,  # SL crossed -> immediate emergency close
            contract_multiplier=1.0, max_risk_cap=4.50, taker_fee_bps=5.0, slippage_bps=0.0,
            tracker=tracker, result={"success": True}, created_trade_id="TRADE_123",
        )
    assert ok is False and qty == 0.0
    assert out["code"] == "POST_FILL_RISK_EMERGENCY_CLOSED"
    m_close.assert_called_once()

    # 2. DB close fails -> quarantined as POST_FILL_RISK_DB_DESYNC
    w2, _ = _make_wrapper(monkeypatch)
    tracker2 = _make_tracker(qty=0.10, vwap=3060.0, sl=2970.0)
    pos_calls2 = [0.10, 0.0]
    w2._fetch_position_qty_strict = AsyncMock(side_effect=lambda *a, **kw: pos_calls2.pop(0))
    w2._place_order_rest = AsyncMock(return_value={"success": True, "code": "00000", "order_id": "EM_2"})
    with patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=None), \
         patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined") as m_q:
        ok2, _, out2 = await w2._enforce_post_fill_risk_cap(
            kraken_symbol="PF_ETHUSD", position_side="long", close_side="sell",
            protect_qty=0.10, real_vwap=3060.0, confirmed_sl=3070.0,
            contract_multiplier=1.0, max_risk_cap=4.50, taker_fee_bps=5.0, slippage_bps=0.0,
            tracker=tracker2, result={"success": True}, created_trade_id="TRADE_456",
        )
    assert ok2 is False
    assert out2["code"] == "POST_FILL_RISK_DB_DESYNC"
    assert w2._global_entry_lock is True
    m_q.assert_called_once()
