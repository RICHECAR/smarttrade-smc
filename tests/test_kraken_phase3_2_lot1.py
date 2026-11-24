"""
Unit and Integration Tests for Phase 3.2 - Lot 1:
Persistence of SL/TP Intentions and Protections, Concurrency, Fail-Closed, Crash Recovery,
and Adversarial P0/P1 Safety Invariants.

Strictly non-destructive. Mocks and isolated SQLite fixtures only.
Zero network calls to Kraken.
"""

import os
import uuid
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)
from backend.services.live_trades_service import LiveTradesService, get_db_session
from backend.db.session import engine
from backend.models.live_trade import LiveTrade
from backend.models.trade_fill import TradeFill


# =====================================================================
# FIXTURES
# =====================================================================

@pytest.fixture(autouse=True)
def clean_test_trades():
    """Ensure test trades and their fills are safely cleaned up before and after each test."""
    test_symbols = ["PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD", "BTCUSDT", "ETHUSDT"]
    def _cleanup():
        with get_db_session() as db:
            trades = db.query(LiveTrade).filter(LiveTrade.symbol.in_(test_symbols)).all()
            trade_ids = [t.id for t in trades]
            if trade_ids:
                db.query(TradeFill).filter(TradeFill.trade_id.in_(trade_ids)).delete(synchronize_session=False)
                db.query(LiveTrade).filter(LiveTrade.id.in_(trade_ids)).delete(synchronize_session=False)
                db.commit()
            else:
                db.rollback()
        engine.dispose()
    _cleanup()
    yield
    _cleanup()


@pytest.fixture
def mock_kraken_client():
    """Mock client returning standard successful responses with realistic execution price."""
    client = AsyncMock()
    client._placed = False

    async def _place_mkt(**kwargs):
        client._placed = True
        return {
            "success": True,
            "code": "00000",
            "order_id": "ORD-ENTRY-100",
            "filled_size": 0.005,
            "status": "placed",
            "price": 90000.0,
            "price_executed": 90000.0,
        }
    client.place_market_order = AsyncMock(side_effect=_place_mkt)

    async def _place_order(**kwargs):
        return {
            "success": True,
            "code": "00000",
            "order_id": "ORD-GENERIC-100",
            "filled_size": 0.005,
            "status": "placed",
            "price": 90000.0,
        }
    client.place_order = AsyncMock(side_effect=_place_order)

    client.get_fills = AsyncMock(return_value=[
        {"order_id": "ORD-ENTRY-100", "symbol": "PF_XBTUSD", "size": 0.005, "price": 90000.0}
    ])

    async def _get_pos(*args, **kw):
        if not client._placed:
            return []
        return [{"symbol": "PF_XBTUSD", "side": "long", "size": 0.005}]

    client.get_positions = AsyncMock(side_effect=_get_pos)
    client.get_open_orders = AsyncMock(return_value=[])
    client.get_order_status = AsyncMock(return_value={
        "status": "untouched",
        "filled_size": 0,
    })
    return client


# =====================================================================
# 1. PERSISTENCE D'INTENTION AVANT ÉMISSION ET FAIL-CLOSED
# =====================================================================

class TestLot1_IntentionPersistenceAndFailClosed:
    """Verifies that protection intentions are persisted in SQLite before Kraken emission."""

    @pytest.mark.asyncio
    async def test_1_1_sl_intent_persisted_before_emission_and_attached_on_success(self, mock_kraken_client):
        """
        When placing an order with SL:
        1. Intention is recorded in SQLite (INTENT_RECORDED) with deterministic client_oid.
        2. REST order is sent with client_order_id.
        3. Upon success, SQLite status becomes ATTACHED with kraken order_id.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        wrapper._rest_client = mock_kraken_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        sl_placed = []
        async def _mock_place_order(**kwargs):
            if kwargs.get("order_type") == "stp":
                sl_placed.append(kwargs)
                return {
                    "success": True,
                    "code": "00000",
                    "order_id": "SL-KRAKEN-888",
                    "status": "placed",
                }
            return {
                "success": True,
                "code": "00000",
                "order_id": "ORD-ENTRY-100",
                "status": "placed",
                "price": 90000.0,
            }

        mock_kraken_client.place_order = AsyncMock(side_effect=_mock_place_order)

        res = await wrapper.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=0.005,
            order_type="market",
            stop_loss=88000.0,
            intent="open",
            client_oid="BOT_INTENT_TEST_1",
        )

        assert res["success"] is True
        assert res["sl_attached"] is True
        assert len(sl_placed) == 1
        assert sl_placed[0]["client_order_id"].startswith("BOT_SL_PF_XBTUSD_")

        # Verify SQLite state
        prots = LiveTradesService.get_protections("PF_XBTUSD", order_id=res.get("order_id"))
        assert "sl" in prots
        assert prots["sl"]["status"] == "ATTACHED"
        assert prots["sl"]["order_id"] == "SL-KRAKEN-888"
        assert prots["sl"]["client_oid"] == sl_placed[0]["client_order_id"]

    @pytest.mark.asyncio
    async def test_1_2_fail_closed_if_sqlite_intent_persistence_fails(self, mock_kraken_client):
        """
        If recording the intention in SQLite fails (DB lock, disk error):
        - The SL must NOT be sent to Kraken.
        - The position is immediately closed via emergency close (fail-closed).
        - Result is flagged as failed.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        wrapper._rest_client = mock_kraken_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        sl_attempts = []
        async def _mock_place_order(**kwargs):
            if kwargs.get("order_type") == "stp":
                sl_attempts.append(kwargs)
            return {"success": True, "code": "00000", "order_id": "ORD-EMERGENCY"}
        mock_kraken_client.place_order = AsyncMock(side_effect=_mock_place_order)

        with patch("backend.services.live_trades_service.LiveTradesService.record_protection_intent", return_value=False):
            res = await wrapper.place_order(
                symbol="BTCUSDT",
                side="buy",
                quantity=0.005,
                order_type="market",
                stop_loss=88000.0,
                intent="open",
            )

        assert len(sl_attempts) == 0, "SL must NOT be sent if SQLite intent fails"
        assert res["_sl_emergency_close"] is True
        assert res["status"] == "SL_FAILED_POSITION_EMERGENCY_CLOSED"


# =====================================================================
# 2. CONCURRENCE ET INTÉGRITÉ JSON SQLITE
# =====================================================================

class TestLot1_ConcurrencyAndJsonIntegrity:
    """Verifies SQLite raw_exchange concurrent safety and versioning."""

    @pytest.mark.asyncio
    async def test_2_1_concurrent_sl_and_tp_updates_preserve_both(self, mock_kraken_client):
        """Simultaneous updates to SL and TP on the same trade preserve both without overwriting."""
        trade = LiveTradesService.create_live_trade(
            order_id="ORD-CONCUR-1",
            symbol="PF_ETHUSD",
            side="long",
            qty=1.0,
            entry=3000.0,
            stop=2900.0,
            tp=3200.0,
            broker="kraken",
            raw_exchange={"entry_meta": "original_data"},
        )
        assert trade is not None

        async def _record_sl():
            await asyncio.sleep(0.01)
            return LiveTradesService.record_protection_intent(
                symbol="PF_ETHUSD",
                protection_type="sl",
                client_oid="BOT_SL_CONCUR_01",
                order_type="stp",
                trigger_price=2900.0,
                placed_qty=1.0,
                order_id="ORD-CONCUR-1",
                status="ATTACHED",
            )

        async def _record_tp():
            await asyncio.sleep(0.01)
            return LiveTradesService.record_protection_intent(
                symbol="PF_ETHUSD",
                protection_type="tp",
                client_oid="BOT_TP_CONCUR_01",
                order_type="take_profit",
                trigger_price=3200.0,
                placed_qty=1.0,
                order_id="ORD-CONCUR-1",
                status="ATTACHED",
            )

        res_sl, res_tp = await asyncio.gather(_record_sl(), _record_tp())
        assert res_sl is True
        assert res_tp is True

        prots = LiveTradesService.get_protections("PF_ETHUSD", order_id="ORD-CONCUR-1")
        assert "sl" in prots, "SL must exist in protections"
        assert "tp" in prots, "TP must exist in protections"
        assert prots["sl"]["client_oid"] == "BOT_SL_CONCUR_01"
        assert prots["tp"]["client_oid"] == "BOT_TP_CONCUR_01"

        saved_trade = LiveTradesService.get_trade_by_order_id("ORD-CONCUR-1", broker="kraken")
        raw = saved_trade.get_raw_exchange()
        assert raw.get("entry_meta") == "original_data"


# =====================================================================
# 3. GESTION DES RÉPONSES RÉSEAU AMBIGUËS (UNCONFIRMED)
# =====================================================================

class TestLot1_AmbiguousNetworkResponses:
    """Verifies that network exceptions flag status as UNCONFIRMED without duplicate orders."""

    @pytest.mark.asyncio
    async def test_3_1_timeout_on_sl_placement_sets_status_unconfirmed(self, mock_kraken_client):
        """When SL submission raises TimeoutError, protection status is set to UNCONFIRMED with client_oid saved."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        wrapper._rest_client = mock_kraken_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        async def _mock_place_order(**kwargs):
            if kwargs.get("order_type") == "stp":
                raise TimeoutError("Kraken gateway timeout on sendorder")
            return {
                "success": True,
                "code": "00000",
                "order_id": "ORD-ENTRY-TIMEOUT",
                "status": "placed",
                "price": 90000.0,
            }

        mock_kraken_client.place_order = AsyncMock(side_effect=_mock_place_order)

        res = await wrapper.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=0.005,
            order_type="market",
            stop_loss=87000.0,
            intent="open",
            client_oid="BOT_TIMEOUT_TEST",
        )

        assert res.get("sl_attached") is False
        prots = LiveTradesService.get_protections("PF_XBTUSD", order_id=res.get("order_id"))
        assert "sl" in prots
        assert prots["sl"]["status"] == "UNCONFIRMED"
        assert "timeout" in prots["sl"].get("error_message", "").lower()
        assert prots["sl"]["client_oid"].startswith("BOT_SL_PF_XBTUSD_")


# =====================================================================
# 4. RÉCONCILIATION AU DÉMARRAGE ET LES 12 TESTS OBLIGATOIRES P0/P1
# =====================================================================

class TestLot1_MandatoryAdversarialP0P1Tests:
    """The 12 mandatory adversarial safety tests required by the audit."""

    @pytest.mark.asyncio
    async def test_recovered_sl_with_reduce_only_false_rejected_and_quarantined(self):
        """
        Test 1: If recovered candidate SL has reduceOnly == False,
        it must be rejected, not attached, and symbol quarantined.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[{
            "order_id": "SL-BAD-REDUCEONLY",
            "cliOrdId": "BOT_SL_PF_XBTUSD_REDUCE_FALSE",
            "symbol": "PF_XBTUSD",
            "orderType": "stp",
            "side": "sell",
            "reduceOnly": False,  # VIOLATION: reduceOnly is False!
            "size": 0.01,
            "stopPrice": 88000.0,
            "status": "placed",
        }])
        wrapper._rest_client = mock_client
        wrapper._connected = True

        trade = LiveTradesService.create_live_trade(
            order_id="ORD-TEST-1",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": None,
                        "client_oid": "BOT_SL_PF_XBTUSD_REDUCE_FALSE",
                        "status": "UNCONFIRMED",
                    }
                }
            }
        )
        assert trade is not None

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        assert "PF_XBTUSD" in report["ambiguous"]
        assert "PF_XBTUSD" not in report["healthy"]

        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker is not None
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert tracker.sl_order_id is None

        # Verify SQLite trade marked QUARANTINED
        saved = LiveTradesService.get_trade_by_order_id("ORD-TEST-1", broker="kraken")
        assert saved.status == "QUARANTINED"
        assert "reduceOnly" in saved.error_message

    @pytest.mark.asyncio
    async def test_recovered_sl_with_wrong_side_rejected_and_quarantined(self):
        """
        Test 2: For a long trade, candidate SL with side 'buy' instead of 'sell'
        must be rejected and quarantined.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[{
            "order_id": "SL-WRONG-SIDE",
            "cliOrdId": "BOT_SL_PF_XBTUSD_WRONG_SIDE",
            "symbol": "PF_XBTUSD",
            "orderType": "stp",
            "side": "buy",  # VIOLATION: long position protection must be sell!
            "reduceOnly": True,
            "size": 0.01,
            "stopPrice": 88000.0,
            "status": "placed",
        }])
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-TEST-2",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": None,
                        "client_oid": "BOT_SL_PF_XBTUSD_WRONG_SIDE",
                        "status": "UNCONFIRMED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        saved = LiveTradesService.get_trade_by_order_id("ORD-TEST-2", broker="kraken")
        assert saved.status == "QUARANTINED"
        assert "Side mismatch" in saved.error_message

    @pytest.mark.asyncio
    async def test_recovered_sl_with_wrong_type_rejected_and_quarantined(self):
        """
        Test 3: Candidate SL with orderType 'lmt' instead of 'stp'/'stop_loss'
        must be rejected and quarantined.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[{
            "order_id": "SL-WRONG-TYPE",
            "cliOrdId": "BOT_SL_PF_XBTUSD_WRONG_TYPE",
            "symbol": "PF_XBTUSD",
            "orderType": "lmt",  # VIOLATION: limit order instead of stp!
            "side": "sell",
            "reduceOnly": True,
            "size": 0.01,
            "stopPrice": 88000.0,
            "status": "placed",
        }])
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-TEST-3",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": None,
                        "client_oid": "BOT_SL_PF_XBTUSD_WRONG_TYPE",
                        "status": "UNCONFIRMED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        saved = LiveTradesService.get_trade_by_order_id("ORD-TEST-3", broker="kraken")
        assert saved.status == "QUARANTINED"
        assert "Invalid orderType" in saved.error_message

    @pytest.mark.asyncio
    async def test_recovered_sl_with_insufficient_qty_rejected_and_quarantined(self):
        """
        Test 4: Candidate SL with size 0.005 covering only half of 0.01 position
        must be rejected and quarantined.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[{
            "order_id": "SL-INSUFFICIENT-QTY",
            "cliOrdId": "BOT_SL_PF_XBTUSD_HALF_QTY",
            "symbol": "PF_XBTUSD",
            "orderType": "stp",
            "side": "sell",
            "reduceOnly": True,
            "size": 0.005,  # VIOLATION: only 0.005 for position of 0.01!
            "stopPrice": 88000.0,
            "status": "placed",
        }])
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-TEST-4",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": None,
                        "client_oid": "BOT_SL_PF_XBTUSD_HALF_QTY",
                        "status": "UNCONFIRMED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        saved = LiveTradesService.get_trade_by_order_id("ORD-TEST-4", broker="kraken")
        assert saved.status == "QUARANTINED"
        assert "Insufficient active remaining qty" in saved.error_message

    @pytest.mark.asyncio
    async def test_recovered_sl_with_incoherent_trigger_price_rejected_and_quarantined(self):
        """
        Test 5: For a long trade with entry=90000.0, an SL trigger price of 92000.0 (above entry)
        must be rejected as incoherent and quarantined.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[{
            "order_id": "SL-INCOHERENT-PRICE",
            "cliOrdId": "BOT_SL_PF_XBTUSD_INCOHERENT",
            "symbol": "PF_XBTUSD",
            "orderType": "stp",
            "side": "sell",
            "reduceOnly": True,
            "size": 0.01,
            "stopPrice": 92000.0,  # VIOLATION: trigger 92000 > entry 90000 for Long SL!
            "status": "placed",
        }])
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-TEST-5",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": None,
                        "client_oid": "BOT_SL_PF_XBTUSD_INCOHERENT",
                        "status": "UNCONFIRMED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        saved = LiveTradesService.get_trade_by_order_id("ORD-TEST-5", broker="kraken")
        assert saved.status == "QUARANTINED"
        assert "Incoherent SL trigger" in saved.error_message

    @pytest.mark.asyncio
    async def test_sl_persist_fail_and_emergency_close_fail_quarantines_symbol(self, mock_kraken_client):
        """
        Test 6: When SL placement fails AND emergency market close also fails:
        - Symbol must be added to _reconciliation_required_symbols.
        - Tracker status becomes MANUAL_RECONCILIATION_REQUIRED.
        - Result returns status MANUAL_RECONCILIATION_REQUIRED with critical naked position error.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        wrapper._rest_client = mock_kraken_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        # SL fails on rest place_order
        async def _mock_place_order(**kwargs):
            return {"success": False, "error": "Kraken SL rejected"}
        mock_kraken_client.place_order = AsyncMock(side_effect=_mock_place_order)

        # Entry succeeds, but emergency close (reduce_only=True) fails!
        async def _mock_place_mkt(**kwargs):
            if kwargs.get("reduce_only") is True:
                return {"success": False, "error": "InsufficientLiquidity", "code": "ERROR"}
            mock_kraken_client._placed = True
            return {
                "success": True,
                "code": "00000",
                "order_id": "ORD-ENTRY-NAKED-1",
                "status": "placed",
                "filled_size": 0.005,
                "price": 90000.0,
                "price_executed": 90000.0,
            }
        mock_kraken_client.place_market_order = AsyncMock(side_effect=_mock_place_mkt)
        mock_kraken_client.get_fills = AsyncMock(return_value=[
            {"order_id": "ORD-ENTRY-NAKED-1", "symbol": "PF_XBTUSD", "size": 0.005, "price": 90000.0}
        ])

        res = await wrapper.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=0.005,
            order_type="market",
            stop_loss=88000.0,
            intent="open",
        )

        assert res["success"] is False
        assert res["status"] == "MANUAL_RECONCILIATION_REQUIRED"
        assert "CRITICAL_NAKED_POSITION" in res["error"]
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols

        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker is not None
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_quarantine_persisted_in_sqlite_on_naked_position(self, mock_kraken_client):
        """
        Test 7: Verify that the trade record in SQLite DB is updated to status QUARANTINED
        when a naked position occurs (emergency close fails).
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        wrapper._rest_client = mock_kraken_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        async def _mock_place_order(**kwargs):
            return {"success": False, "error": "Exchange SL error"}
        mock_kraken_client.place_order = AsyncMock(side_effect=_mock_place_order)

        async def _mock_place_mkt(**kwargs):
            if kwargs.get("reduce_only") is True:
                return {"success": False, "error": "Market halted", "code": "HALTED"}
            mock_kraken_client._placed = True
            return {
                "success": True,
                "code": "00000",
                "order_id": "ORD-ENTRY-NAKED-SQLITE",
                "status": "placed",
                "filled_size": 0.005,
                "price": 90000.0,
                "price_executed": 90000.0,
            }
        mock_kraken_client.place_market_order = AsyncMock(side_effect=_mock_place_mkt)
        mock_kraken_client.get_fills = AsyncMock(return_value=[
            {"order_id": "ORD-ENTRY-NAKED-SQLITE", "symbol": "PF_XBTUSD", "size": 0.005, "price": 90000.0}
        ])

        res = await wrapper.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=0.005,
            order_type="market",
            stop_loss=88000.0,
            intent="open",
        )

        assert res["status"] == "MANUAL_RECONCILIATION_REQUIRED"
        saved = LiveTradesService.get_trade_by_order_id("ORD-ENTRY-NAKED-SQLITE", broker="kraken")
        assert saved is not None
        assert saved.status == "QUARANTINED"
        assert "CRITICAL_NAKED_POSITION" in saved.error_message

    @pytest.mark.asyncio
    async def test_new_entries_blocked_on_quarantined_symbol_after_incident(self, mock_kraken_client):
        """
        Test 8: After a symbol is locked in quarantine due to an incident,
        any subsequent attempt to open a new position on that symbol is immediately blocked.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        wrapper._rest_client = mock_kraken_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        # Lock symbol in quarantine
        wrapper._reconciliation_required_symbols.add("PF_XBTUSD")

        # Attempt to open new order on quarantined symbol
        res = await wrapper.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=0.005,
            order_type="market",
            intent="open",
        )

        assert res["success"] is False
        assert res["code"] == "ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"
        assert "MANUAL_RECONCILIATION_REQUIRED" in res["error"]

    @pytest.mark.asyncio
    async def test_client_oid_uniqueness_between_successive_cycles(self, mock_kraken_client):
        """
        Test 9: Successive trade cycles on the same symbol generate strictly distinct client_oids
        for SL and TP with UUID tokens, length under 100 characters.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        wrapper._rest_client = mock_kraken_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        placed_client_oids = []
        async def _mock_place_order(**kwargs):
            if "client_order_id" in kwargs:
                placed_client_oids.append(kwargs["client_order_id"])
            return {
                "success": True,
                "code": "00000",
                "order_id": f"ORD-CYCLE-{len(placed_client_oids)}",
                "status": "placed",
                "price": 90000.0,
            }
        mock_kraken_client.place_order = AsyncMock(side_effect=_mock_place_order)

        # Cycle 1
        res1 = await wrapper.place_order(
            symbol="BTCUSDT", side="buy", quantity=0.005, order_type="market",
            stop_loss=88000.0, take_profit=95000.0, intent="open",
        )
        assert res1["success"] is True

        # Clear active tracker for cycle 2
        wrapper._active_trackers.clear()
        mock_kraken_client._placed = False

        # Cycle 2
        res2 = await wrapper.place_order(
            symbol="BTCUSDT", side="buy", quantity=0.005, order_type="market",
            stop_loss=88000.0, take_profit=95000.0, intent="open",
        )
        assert res2["success"] is True

        assert len(placed_client_oids) >= 4  # SL1, TP1, SL2, TP2
        # All client order IDs must be unique
        assert len(set(placed_client_oids)) == len(placed_client_oids)
        # All client order IDs must be strictly shorter than 100 chars
        for cid in placed_client_oids:
            assert len(cid) < 100
            assert cid.startswith("BOT_SL_PF_XBTUSD_") or cid.startswith("BOT_TP_PF_XBTUSD_")

    @pytest.mark.asyncio
    async def test_two_trades_on_same_symbol_strict_association_no_mismatch(self):
        """
        Test 10: With multiple trades in SQLite on PF_XBTUSD,
        record_protection_intent and update_protection_status update ONLY the targeted trade
        and NEVER fall back or cross-wire protections.
        """
        trade1 = LiveTradesService.create_live_trade(
            order_id="ORD-FIRST-TRADE",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=95000.0,
            broker="kraken",
            raw_exchange={"protections": {"version": 1, "sl": {"order_id": "SL-1", "status": "ATTACHED"}}}
        )
        trade2 = LiveTradesService.create_live_trade(
            order_id="ORD-SECOND-TRADE",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.02,
            entry=91000.0,
            stop=89000.0,
            tp=96000.0,
            broker="kraken",
            raw_exchange={"protections": {"version": 1, "sl": {"order_id": "SL-2", "status": "INTENT_RECORDED"}}}
        )
        assert trade1 is not None and trade2 is not None

        # Update SL on trade2 strictly by order_id
        ok = LiveTradesService.update_protection_status(
            symbol="PF_XBTUSD",
            protection_type="sl",
            status="ATTACHED",
            order_id="ORD-SECOND-TRADE",
            protection_order_id="SL-2-CONFIRMED",
            broker="kraken",
        )
        assert ok is True

        # Verify trade1 was NOT touched
        p1 = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-FIRST-TRADE")
        assert p1["sl"]["order_id"] == "SL-1"
        assert p1["sl"]["status"] == "ATTACHED"

        # Verify trade2 has the new protection order_id
        p2 = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-SECOND-TRADE")
        assert p2["sl"]["order_id"] == "SL-2-CONFIRMED"
        assert p2["sl"]["status"] == "ATTACHED"

        # Negative test: update with unknown order_id must fail without affecting any trade
        ok_fail = LiveTradesService.update_protection_status(
            symbol="PF_XBTUSD",
            protection_type="sl",
            status="TRIGGERED",
            order_id="NON_EXISTENT_ORDER_ID",
            broker="kraken",
        )
        assert ok_fail is False

    @pytest.mark.asyncio
    async def test_concurrent_processes_raw_exchange_no_lost_update(self):
        """
        Test 11: Rapid interleaved writes on protections simulate multi-worker concurrency.
        BEGIN IMMEDIATE locking ensures neither SL nor TP is lost.
        """
        trade = LiveTradesService.create_live_trade(
            order_id="ORD-CONCUR-MULTI-1",
            symbol="PF_SOLUSD",
            side="long",
            qty=10.0,
            entry=200.0,
            stop=190.0,
            tp=220.0,
            broker="kraken",
            raw_exchange={"base": "data"},
        )
        assert trade is not None

        async def _write_sl(i):
            await asyncio.sleep(0.005 * i)
            return LiveTradesService.record_protection_intent(
                symbol="PF_SOLUSD",
                protection_type="sl",
                client_oid=f"BOT_SL_SOL_{i}",
                order_type="stp",
                trigger_price=190.0,
                placed_qty=10.0,
                order_id="ORD-CONCUR-MULTI-1",
                status="ATTACHED",
            )

        async def _write_tp(i):
            await asyncio.sleep(0.005 * i)
            return LiveTradesService.record_protection_intent(
                symbol="PF_SOLUSD",
                protection_type="tp",
                client_oid=f"BOT_TP_SOL_{i}",
                order_type="take_profit",
                trigger_price=220.0,
                placed_qty=10.0,
                order_id="ORD-CONCUR-MULTI-1",
                status="ATTACHED",
            )

        tasks = [_write_sl(1), _write_tp(1), _write_sl(2), _write_tp(2)]
        results = await asyncio.gather(*tasks)
        assert all(results)

        prots = LiveTradesService.get_protections("PF_SOLUSD", order_id="ORD-CONCUR-MULTI-1")
        assert "sl" in prots
        assert "tp" in prots
        assert prots["sl"]["client_oid"] == "BOT_SL_SOL_2"
        assert prots["tp"]["client_oid"] == "BOT_TP_SOL_2"

    @pytest.mark.asyncio
    async def test_crash_after_sl_executed_reconciles_without_false_protection(self):
        """
        Test 12: If an SL was executed (or closed/cancelled) while bot was down,
        startup reconciliation does not falsely attach an inactive order,
        detects discrepancy, and flags the symbol.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        # Open orders has the SL order with status 'filled' (terminal!)
        mock_client.get_open_orders = AsyncMock(return_value=[{
            "order_id": "SL-ALREADY-FILLED",
            "cliOrdId": "BOT_SL_PF_XBTUSD_EXECUTED",
            "symbol": "PF_XBTUSD",
            "orderType": "stp",
            "side": "sell",
            "reduceOnly": True,
            "size": 0.01,
            "stopPrice": 88000.0,
            "status": "filled",  # TERMINAL!
        }])
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-CRASH-FILLED-SL",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": None,
                        "client_oid": "BOT_SL_PF_XBTUSD_EXECUTED",
                        "status": "UNCONFIRMED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker is not None
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert tracker.sl_order_id is None


# =====================================================================
# 5. TESTS CONTRADICTOIRES P0 REWORK & 10 CAS LIMITES OBLIGATOIRES
# =====================================================================

class TestLot1_P0ReworkAndMandatoryEdgeCases:
    """
    10 edge-case tests verifying the P0 fix for reconcile_on_startup,
    fail-closed protection recovery, multi-protocol client_oid handling,
    and strict symbol locking.
    """

    @pytest.mark.asyncio
    async def test_sl_attached_in_sqlite_missing_on_kraken_quarantined(self):
        """
        1. Trade in SQLite with sl_status='ATTACHED', sl_order_id='SL-ABSENT-100'.
        Kraken returns NO open orders.
        Result: detected as MISSING_ON_EXCHANGE, symbol quarantined, trade in SQLite quarantined.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[])  # Absent on Kraken!
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-MISSING-SL-100",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": "SL-ABSENT-100",
                        "client_oid": "BOT_SL_PF_XBTUSD_ABSENT",
                        "status": "ATTACHED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker is not None
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"

        # Check SQLite updated
        prots = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-MISSING-SL-100")
        assert prots["sl"]["status"] == "MISSING_ON_EXCHANGE"

        trade = LiveTradesService.get_trade_by_order_id("ORD-MISSING-SL-100", broker="kraken")
        assert trade.status == "QUARANTINED"

    @pytest.mark.asyncio
    async def test_tp_attached_in_sqlite_missing_on_kraken_quarantined(self):
        """
        2. Trade in SQLite with tp_status='ATTACHED', tp_order_id='TP-ABSENT-100'.
        Kraken returns valid SL, but NO TP order.
        Result: tp_status becomes MISSING_ON_EXCHANGE, symbol in quarantined_symbols.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[
            {
                "order_id": "SL-VALID-100",
                "cliOrdId": "BOT_SL_PF_XBTUSD_VALID",
                "symbol": "PF_XBTUSD",
                "orderType": "stp",
                "side": "sell",
                "reduceOnly": True,
                "size": 0.01,
                "stopPrice": 88000.0,
                "status": "placed",
            }
        ])
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-MISSING-TP-100",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=95000.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": "SL-VALID-100",
                        "client_oid": "BOT_SL_PF_XBTUSD_VALID",
                        "status": "ATTACHED",
                    },
                    "tp": {
                        "order_id": "TP-ABSENT-100",
                        "client_oid": "BOT_TP_PF_XBTUSD_ABSENT",
                        "status": "ATTACHED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols

        prots = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-MISSING-TP-100")
        assert prots["tp"]["status"] == "MISSING_ON_EXCHANGE"

    @pytest.mark.asyncio
    async def test_sl_present_and_valid_on_kraken_confirmed_attached(self):
        """
        3. Trade in SQLite with sl_status='ATTACHED', sl_order_id='SL-VALID-100'.
        Kraken returns valid active SL order matching all criteria.
        Result: trade in report['healthy'], tracker status='OPEN', sl_tp_state='SL_ATTACHED'.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[
            {
                "order_id": "SL-VALID-100",
                "cliOrdId": "BOT_SL_PF_XBTUSD_OK",
                "symbol": "PF_XBTUSD",
                "orderType": "stp",
                "side": "sell",
                "reduceOnly": True,
                "size": 0.01,
                "stopPrice": 88000.0,
                "status": "placed",
            }
        ])
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-HEALTHY-SL",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": "SL-VALID-100",
                        "client_oid": "BOT_SL_PF_XBTUSD_OK",
                        "status": "ATTACHED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["healthy"]
        assert "PF_XBTUSD" not in report["quarantined_symbols"]
        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker is not None
        assert tracker.status == "OPEN"
        assert tracker.sl_order_id == "SL-VALID-100"
        assert tracker.sl_tp_state == "SL_ATTACHED"

    @pytest.mark.asyncio
    async def test_sl_cancelled_during_bot_downtime_detected_and_quarantined(self):
        """
        4. Trade has SL in SQLite, but was cancelled manually or by exchange during downtime.
        Kraken returns empty open orders.
        Result: detected as missing, quarantined, tracker MANUAL_RECONCILIATION_REQUIRED.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[])  # Was cancelled!
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-CANCELLED-DOWNTIME",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": "SL-CANCELLED-1",
                        "client_oid": "BOT_SL_PF_XBTUSD_CANCELLED",
                        "status": "ATTACHED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_sl_partially_filled_with_sufficient_coverage_accepted(self):
        """
        5. Position remaining is 0.005. SL order has size=0.01, filled_size=0.005, status='partiallyFilled'.
        remaining_order_qty = 0.005 >= trade_qty (0.005).
        Result: validated as healthy coverage, attached.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.005}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[
            {
                "order_id": "SL-PARTIAL-SUFFICIENT",
                "cliOrdId": "BOT_SL_PF_XBTUSD_PART1",
                "symbol": "PF_XBTUSD",
                "orderType": "stp",
                "side": "sell",
                "reduceOnly": True,
                "size": 0.01,
                "filled_size": 0.005,
                "status": "partiallyFilled",
                "stopPrice": 88000.0,
            }
        ])
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-PARTIAL-SUFF",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.005,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": "SL-PARTIAL-SUFFICIENT",
                        "client_oid": "BOT_SL_PF_XBTUSD_PART1",
                        "status": "ATTACHED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["healthy"]
        assert "PF_XBTUSD" not in report["quarantined_symbols"]

    @pytest.mark.asyncio
    async def test_sl_partially_filled_with_insufficient_coverage_quarantined(self):
        """
        6. Position is 0.01. SL order has size=0.01, filled_size=0.006, status='partiallyFilled'.
        remaining_order_qty = 0.004 < trade_qty (0.01).
        Result: validation fails (insufficient active remaining qty), quarantined.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[
            {
                "order_id": "SL-PARTIAL-INSUFFICIENT",
                "cliOrdId": "BOT_SL_PF_XBTUSD_PART2",
                "symbol": "PF_XBTUSD",
                "orderType": "stp",
                "side": "sell",
                "reduceOnly": True,
                "size": 0.01,
                "filled_size": 0.006,
                "status": "partiallyFilled",
                "stopPrice": 88000.0,
            }
        ])
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-PARTIAL-INSUFF",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": "SL-PARTIAL-INSUFFICIENT",
                        "client_oid": "BOT_SL_PF_XBTUSD_PART2",
                        "status": "ATTACHED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_order_recovered_solely_via_cl_ord_id_websocket_compat(self):
        """
        7. Order in Kraken returns only cl_ord_id (WebSocket format) instead of cliOrdId.
        _extract_order_client_oid() matches it successfully, and protection is attached.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[
            {
                "order_id": "SL-WS-12345",
                "cl_ord_id": "BOT_SL_PF_XBTUSD_WS_01",  # WS v2 field name
                "symbol": "PF_XBTUSD",
                "orderType": "stp",
                "side": "sell",
                "reduceOnly": True,
                "size": 0.01,
                "stopPrice": 88000.0,
                "status": "placed",
            }
        ])
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-WS-COMPAT",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": None,  # Lost order_id
                        "client_oid": "BOT_SL_PF_XBTUSD_WS_01",
                        "status": "UNCONFIRMED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" in report["healthy"]
        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker.sl_order_id == "SL-WS-12345"
        assert tracker.status == "OPEN"

    @pytest.mark.asyncio
    async def test_restart_with_symbol_already_in_quarantine_preserves_lock(self):
        """
        8. SQLite trade has status='QUARANTINED'.
        reconcile_on_startup() restores it directly into _reconciliation_required_symbols.
        Symbol remains quarantined without attempting blind recovery.
        """
        trade = LiveTradesService.create_live_trade(
            order_id="ORD-ALREADY-QUARANTINED",
            symbol="PF_ETHUSD",
            side="long",
            qty=1.0,
            entry=3000.0,
            stop=2900.0,
            tp=0.0,
            broker="kraken",
        )
        LiveTradesService.mark_trade_quarantined(
            symbol="PF_ETHUSD",
            order_id="ORD-ALREADY-QUARANTINED",
            reason="Prior incident quarantine",
        )

        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[])
        mock_client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = mock_client
        wrapper._connected = True

        report = await wrapper.reconcile_on_startup()
        assert "PF_ETHUSD" in report["quarantined_symbols"]
        assert "PF_ETHUSD" in wrapper._reconciliation_required_symbols

    @pytest.mark.asyncio
    async def test_quarantined_symbol_strictly_blocks_new_entries(self):
        """
        9. Symbol in _reconciliation_required_symbols.
        place_order(symbol='BTCUSDT', intent='open') immediately returns
        code='ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED' with 0 network calls.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False
        wrapper._reconciliation_required_symbols.add("PF_XBTUSD")

        res = await wrapper.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=0.01,
            order_type="market",
            stop_loss=88000.0,
            intent="open",
        )
        assert res["success"] is False
        assert res["code"] == "ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"
        assert mock_client.place_market_order.call_count == 0
        assert mock_client.place_order.call_count == 0

    @pytest.mark.asyncio
    async def test_sqlite_persistence_failure_during_quarantine_preserves_memory_lock(self):
        """
        10. If SQLite persistence raises an exception while marking trade quarantined,
        in-memory _reconciliation_required_symbols still locks the symbol and tracker
        remains MANUAL_RECONCILIATION_REQUIRED.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_SOLUSD", "side": "long", "size": 10.0}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[])  # SL is missing!
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-SQLITE-FAIL-QUARANTINE",
            symbol="PF_SOLUSD",
            side="long",
            qty=10.0,
            entry=200.0,
            stop=190.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": "SL-FAIL-SQLITE",
                        "status": "ATTACHED",
                    }
                }
            }
        )

        with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined", side_effect=RuntimeError("Disk full")):
            report = await wrapper.reconcile_on_startup()

        # In-memory safety lock must be absolute even if DB write fails
        assert "PF_SOLUSD" in wrapper._reconciliation_required_symbols
        assert "PF_SOLUSD" in report["quarantined_symbols"]
        tracker = wrapper._active_trackers.get("PF_SOLUSD")
        assert tracker is not None
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_reconciliation_network_error_timeout_quarantined_with_explicit_status(self):
        """
        11. P1 Verification: When open orders retrieval fails with a TimeoutError:
        - Must NOT assert MISSING_ON_EXCHANGE (not a confirmed carnet).
        - Must set sl_status = 'RECONCILIATION_NETWORK_ERROR'.
        - Must lock symbol in _reconciliation_required_symbols fail-closed.
        - Must mark trade QUARANTINED in SQLite.
        - Must set tracker status to MANUAL_RECONCILIATION_REQUIRED.
        - Trade must NEVER be declared healthy.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(side_effect=TimeoutError("Connection timed out to futures.kraken.com"))
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-NET-TIMEOUT-1",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": "SL-NET-1",
                        "status": "ATTACHED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" not in report["healthy"]
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols

        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker is not None
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert "RECONCILIATION_NETWORK_ERROR" in tracker.reconciliation_notes

        prots = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-NET-TIMEOUT-1")
        assert prots["sl"]["status"] == "RECONCILIATION_NETWORK_ERROR"

        trade = LiveTradesService.get_trade_by_order_id("ORD-NET-TIMEOUT-1", broker="kraken")
        assert trade.status == "QUARANTINED"

    @pytest.mark.asyncio
    async def test_reconciliation_auth_error_quarantined_with_explicit_status(self):
        """
        12. P1 Verification: When open orders retrieval fails with an authentication error (401 / Invalid Key):
        - Must set sl_status = 'RECONCILIATION_AUTH_ERROR'.
        - Must lock symbol in _reconciliation_required_symbols.
        - Must set tracker status to MANUAL_RECONCILIATION_REQUIRED.
        - Trade must NEVER be declared healthy.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(side_effect=RuntimeError("401 Unauthorized: Invalid API key or signature"))
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-AUTH-ERR-1",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": "SL-AUTH-1",
                        "status": "ATTACHED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" not in report["healthy"]
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols

        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert "RECONCILIATION_AUTH_ERROR" in tracker.reconciliation_notes

        prots = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-AUTH-ERR-1")
        assert prots["sl"]["status"] == "RECONCILIATION_AUTH_ERROR"

    @pytest.mark.asyncio
    async def test_reconciliation_api_error_502_quarantined_with_explicit_status(self):
        """
        13. P1 Verification: When open orders retrieval fails with an HTTP 502 Bad Gateway / API error:
        - Must set sl_status = 'RECONCILIATION_API_ERROR'.
        - Must lock symbol in _reconciliation_required_symbols.
        - Must mark trade QUARANTINED in SQLite.
        - Must set tracker status to MANUAL_RECONCILIATION_REQUIRED.
        - Trade must NEVER be declared healthy.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(side_effect=RuntimeError("502 Bad Gateway: Kraken backend service unavailable"))
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-API-ERR-1",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {
                        "order_id": "SL-API-1",
                        "status": "ATTACHED",
                    }
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" not in report["healthy"]
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols

        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert "RECONCILIATION_API_ERROR" in tracker.reconciliation_notes

        prots = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-API-ERR-1")
        assert prots["sl"]["status"] == "RECONCILIATION_API_ERROR"

        trade = LiveTradesService.get_trade_by_order_id("ORD-API-ERR-1", broker="kraken")
        assert trade.status == "QUARANTINED"

    @pytest.mark.asyncio
    async def test_get_open_orders_missing_method_raises_when_raise_errors_true(self):
        """
        14. Point 1: When REST client does NOT implement get_open_orders:
        - get_open_orders(raise_errors=True) MUST raise AttributeError.
        - get_open_orders(raise_errors=False) returns [].
        - In reconcile_on_startup(): symbol is quarantined, status is RECONCILIATION_API_ERROR,
          and protection is NEVER classified as MISSING_ON_EXCHANGE.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        class ClientWithoutGetOpenOrders:
            async def get_positions(self):
                return [{"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}]

        wrapper._rest_client = ClientWithoutGetOpenOrders()
        wrapper._connected = True

        # Unit verification
        assert await wrapper.get_open_orders("PF_XBTUSD", raise_errors=False) == []
        with pytest.raises(AttributeError, match="Kraken client does not implement get_open_orders"):
            await wrapper.get_open_orders("PF_XBTUSD", raise_errors=True)

        # Reconciliation verification
        LiveTradesService.create_live_trade(
            order_id="ORD-NO-METHOD-1",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {"order_id": "SL-NM-1", "status": "ATTACHED"}
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" not in report["healthy"]
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert "RECONCILIATION_API_ERROR" in tracker.reconciliation_notes

        prots = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-NO-METHOD-1")
        assert prots["sl"]["status"] == "RECONCILIATION_API_ERROR"
        assert prots["sl"]["status"] != "MISSING_ON_EXCHANGE"

    @pytest.mark.asyncio
    async def test_get_open_orders_malformed_and_unexpected_payload_raises_when_raise_errors_true(self):
        """
        15. Point 2: When API returns a malformed or non-list payload:
        - get_open_orders(raise_errors=True) MUST raise TypeError.
        - get_open_orders(raise_errors=False) returns [].
        - In reconcile_on_startup(): symbol is quarantined, status is RECONCILIATION_API_ERROR,
          never converted silently to empty carnet.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        wrapper._rest_client = mock_client

        # Test various non-list responses
        for bad_payload in [None, "invalid_string_payload", 12345, {"result": "ok", "orders": "not_a_list"}]:
            mock_client.get_open_orders = AsyncMock(return_value=bad_payload)
            assert await wrapper.get_open_orders("PF_XBTUSD", raise_errors=False) == []
            with pytest.raises(TypeError, match="Unexpected Kraken get_open_orders response"):
                await wrapper.get_open_orders("PF_XBTUSD", raise_errors=True)

        # Reconciliation test with malformed string response
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(return_value="malformed_non_list_response")
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-MALFORMED-1",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {"order_id": "SL-MAL-1", "status": "ATTACHED"}
                }
            }
        )

        report = await wrapper.reconcile_on_startup()
        assert "PF_XBTUSD" not in report["healthy"]
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert "RECONCILIATION_API_ERROR" in tracker.reconciliation_notes

        prots = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-MALFORMED-1")
        assert prots["sl"]["status"] == "RECONCILIATION_API_ERROR"
        assert prots["sl"]["status"] != "MISSING_ON_EXCHANGE"

    @pytest.mark.asyncio
    async def test_tp_reconciliation_sqlite_failure_logs_critical_and_preserves_memory_lock(self, caplog):
        """
        16. Point 4: In the TP error branch of reconcile_on_startup():
        - If LiveTradesService.update_protection_status raises an exception (SQLite failure),
        - The exception is caught, logged at CRITICAL level,
        - The memory lock (self._reconciliation_required_symbols) MUST remain set,
        - The trade tracker status MUST remain MANUAL_RECONCILIATION_REQUIRED,
        - The trade must be in report['ambiguous'] and report['quarantined_symbols'],
        - The trade is NEVER declared healthy.
        """
        import logging
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(side_effect=RuntimeError("connection reset by peer"))
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-TP-SQLITE-ERR",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=95000.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {"order_id": "SL-TP-FAIL-1", "status": "ATTACHED"},
                    "tp": {"order_id": "TP-TP-FAIL-1", "status": "ATTACHED"},
                }
            }
        )

        orig_update = LiveTradesService.update_protection_status
        def mock_update(*args, **kwargs):
            if kwargs.get("protection_type") == "tp":
                raise RuntimeError("Simulated SQLite write crash on TP quarantine status")
            return orig_update(*args, **kwargs)

        with caplog.at_level(logging.CRITICAL):
            with patch.object(LiveTradesService, "update_protection_status", side_effect=mock_update):
                report = await wrapper.reconcile_on_startup()

        # Check CRITICAL log
        assert any(
            "Failed to persist TP quarantine status" in record.message
            and record.levelno == logging.CRITICAL
            for record in caplog.records
        ), "CRITICAL log for failed TP quarantine persistence was not emitted"

        # Check memory lock preservation
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        assert "PF_XBTUSD" not in report["healthy"]
        assert "PF_XBTUSD" in report["ambiguous"]
        assert "PF_XBTUSD" in report["quarantined_symbols"]

        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker is not None
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert "RECONCILIATION_NETWORK_ERROR" in tracker.reconciliation_notes

    @pytest.mark.asyncio
    async def test_no_false_missing_on_exchange_on_transport_or_reconciliation_error(self):
        """
        17. Verification: Any uncertainty, network timeout, or transport failure
        must NEVER cause active SL/TP protections to be falsely classified as MISSING_ON_EXCHANGE.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        mock_client.get_open_orders = AsyncMock(side_effect=TimeoutError("Request timed out after 10000ms"))
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-NO-FALSE-MISSING",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=95000.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {"order_id": "SL-PEND-1", "status": "ATTACHED"},
                    "tp": {"order_id": "TP-PEND-1", "status": "ATTACHED"},
                }
            }
        )

        report = await wrapper.reconcile_on_startup()

        prots = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-NO-FALSE-MISSING")
        assert prots["sl"]["status"] == "RECONCILIATION_NETWORK_ERROR"
        assert prots["sl"]["status"] != "MISSING_ON_EXCHANGE"
        assert prots["tp"]["status"] == "RECONCILIATION_NETWORK_ERROR"
        assert prots["tp"]["status"] != "MISSING_ON_EXCHANGE"

        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        assert "PF_XBTUSD" not in report["healthy"]


# =====================================================================
# LOT 1.2: ADAPTER ERROR PROPAGATION (R1) & filledSize (R2)
# =====================================================================

class TestLot1_2_KrakenAdapterErrorPropagationAndFilledSize:
    """
    Mandatory tests for Phase 3.2 - Lot 1.2:
    - R1: Error propagation in KrakenFuturesClient.get_open_orders and wrapper transmission
    - R2: Strict handling of filledSize and remaining quantity coverage
    """

    @pytest.mark.asyncio
    async def test_1_kraken_client_empty_orders_returns_valid_empty_list(self):
        """1. Real successful Kraken response with openOrders: [] returns a valid empty list."""
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key="mock_key", api_secret="bW9ja19zZWNyZXQ=")
        client._request = AsyncMock(return_value={"result": "success", "openOrders": []})

        # Both legacy mode and strict mode return [] on legitimate empty carnet
        assert await client.get_open_orders("PF_XBTUSD", raise_errors=False) == []
        assert await client.get_open_orders("PF_XBTUSD", raise_errors=True) == []

    @pytest.mark.asyncio
    async def test_2_kraken_client_timeout_raises_in_strict_mode(self):
        """2. Timeout REST: raises ValueError in strict mode, returns [] in legacy mode."""
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key="mock_key", api_secret="bW9ja19zZWNyZXQ=")
        client._request = AsyncMock(side_effect=ValueError("Request timeout: /openorders"))

        # Legacy mode returns []
        assert await client.get_open_orders("PF_XBTUSD", raise_errors=False) == []

        # Strict mode propagates exception
        with pytest.raises(ValueError, match="Request timeout: /openorders"):
            await client.get_open_orders("PF_XBTUSD", raise_errors=True)

    @pytest.mark.asyncio
    async def test_3_kraken_client_auth_error_raises_in_strict_mode(self):
        """3. Auth error: raises in strict mode, returns [] in legacy mode."""
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key="mock_key", api_secret="bW9ja19zZWNyZXQ=")
        client._request = AsyncMock(side_effect=ValueError("Kraken authentication failed: Invalid API key"))

        assert await client.get_open_orders("PF_XBTUSD", raise_errors=False) == []

        with pytest.raises(ValueError, match="Kraken authentication failed"):
            await client.get_open_orders("PF_XBTUSD", raise_errors=True)

    @pytest.mark.asyncio
    async def test_4_kraken_client_http_502_raises_in_strict_mode(self):
        """4. HTTP 502 error: raises in strict mode, returns [] in legacy mode."""
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key="mock_key", api_secret="bW9ja19zZWNyZXQ=")
        client._request = AsyncMock(side_effect=ValueError("HTTP error 502"))

        assert await client.get_open_orders("PF_XBTUSD", raise_errors=False) == []

        with pytest.raises(ValueError, match="HTTP error 502"):
            await client.get_open_orders("PF_XBTUSD", raise_errors=True)

    @pytest.mark.asyncio
    async def test_5_kraken_client_legacy_default_signature_and_behavior(self):
        """5. Backward compatibility: calling without raise_errors uses default False."""
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key="mock_key", api_secret="bW9ja19zZWNyZXQ=")
        client._request = AsyncMock(side_effect=ValueError("Some network glitch"))

        # Default argument call
        result = await client.get_open_orders("PF_XBTUSD")
        assert result == []

    @pytest.mark.asyncio
    async def test_6_unfilled_order_with_filled_size_zero_validated_attached(self):
        """6. Unfilled order with filledSize = 0 is validated as ATTACHED."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-UNFILLED-1",
            "cliOrdId": "BOT_SL_1",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": 0.0,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_7_partially_filled_order_with_sufficient_remaining_qty_accepted(self):
        """7. Partially filled order with sufficient remaining quantity is accepted."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-PARTIAL-OK",
            "cliOrdId": "BOT_SL_2",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.02,
            "filledSize": 0.01,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "partiallyFilled",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_8_partially_filled_order_with_insufficient_remaining_qty_quarantined(self):
        """8. Partially filled order with insufficient remaining quantity is rejected."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-PARTIAL-FAIL",
            "cliOrdId": "BOT_SL_3",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": 0.005,  # remaining = 0.005 < 0.01
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "partiallyFilled",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Insufficient active remaining qty" in reason
        assert "0.005" in reason

    @pytest.mark.asyncio
    async def test_9_wrapper_transmits_strict_mode_to_rest_client(self):
        """9. Verify wrapper transmits raise_errors=True to the underlying REST client."""
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        client = KrakenFuturesClient(api_key="mock_key", api_secret="bW9ja19zZWNyZXQ=")
        client._request = AsyncMock(side_effect=ValueError("Request timeout: /openorders"))
        wrapper._rest_client = client

        # When wrapper calls with raise_errors=True, client propagates exception
        with pytest.raises(ValueError, match="Request timeout"):
            await wrapper.get_open_orders("PF_XBTUSD", raise_errors=True)

        # When wrapper calls with raise_errors=False, returns []
        assert await wrapper.get_open_orders("PF_XBTUSD", raise_errors=False) == []

    @pytest.mark.asyncio
    async def test_10_rest_transport_error_through_adapter_never_becomes_missing_on_exchange(self):
        """10. Adapter REST error propagates to reconcile_on_startup and NEVER becomes MISSING_ON_EXCHANGE."""
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        client = KrakenFuturesClient(api_key="mock_key", api_secret="bW9ja19zZWNyZXQ=")
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        client._request = AsyncMock(side_effect=ValueError("Network error: connection reset by peer"))
        wrapper._rest_client = client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-LOT12-NET-ERR",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={
                "protections": {
                    "version": 1,
                    "sl": {"order_id": "SL-LOT12-1", "status": "ATTACHED"}
                }
            }
        )

        report = await wrapper.reconcile_on_startup()

        # Protection must NOT be declared MISSING_ON_EXCHANGE
        prots = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-LOT12-NET-ERR")
        assert prots["sl"]["status"] == "RECONCILIATION_NETWORK_ERROR"
        assert prots["sl"]["status"] != "MISSING_ON_EXCHANGE"

        # Symbol locked in memory quarantine
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        assert "PF_XBTUSD" not in report["healthy"]

        tracker = wrapper._active_trackers.get("PF_XBTUSD")
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"
        assert "RECONCILIATION_NETWORK_ERROR" in tracker.reconciliation_notes


# =====================================================================
# LOT 1.3: NUMERIC VALIDATION (D1), STRICT MODE HARDENING (D2) & FIELD PRIORITY (R3)
# =====================================================================

class TestLot1_3_NumericValidationAndStrictModeHardenings:
    """
    Mandatory tests for Phase 3.2 - Lot 1.3:
    - D1: Numeric validation, finite bounds, non-aborting multi-trade reconciliation
    - D2: Strict mode preservation on signature inspection failures / unsupporting proxies
    - R3: Coalesce field priority, 0.0 retention, alias resolution
    """

    # ── D1: VALIDATION NUMÉRIQUE ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_d1_1_filled_size_invalid_string_rejected(self):
        """D1.1: filledSize = 'invalid' is rejected without crashing."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-NUM-1",
            "cliOrdId": "BOT_SL_1",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": "invalid",
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Non-numeric" in reason

    @pytest.mark.asyncio
    async def test_d1_2_filled_size_dict_rejected(self):
        """D1.2: filledSize = {} is rejected without crashing."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-NUM-2",
            "cliOrdId": "BOT_SL_2",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": {},
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Non-numeric" in reason

    @pytest.mark.asyncio
    async def test_d1_3_filled_size_none_falls_back_to_zero(self):
        """D1.3: filledSize = None with no other alias falls back to 0.0 and accepts."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-NUM-3",
            "cliOrdId": "BOT_SL_3",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": None,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_d1_4_size_non_numeric_rejected(self):
        """D1.4: size = 'N/A' is rejected without crashing."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-NUM-4",
            "cliOrdId": "BOT_SL_4",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": "N/A",
            "filledSize": 0.0,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Non-numeric" in reason

    @pytest.mark.asyncio
    async def test_d1_5_filled_size_nan_rejected(self):
        """D1.5: filledSize = NaN is rejected."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-NUM-5",
            "cliOrdId": "BOT_SL_5",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": float("nan"),
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Non-finite" in reason

    @pytest.mark.asyncio
    async def test_d1_6_filled_size_inf_rejected(self):
        """D1.6: filledSize = Infinity is rejected."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-NUM-6",
            "cliOrdId": "BOT_SL_6",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": float("inf"),
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Non-finite" in reason

    @pytest.mark.asyncio
    async def test_d1_7_filled_size_negative_rejected(self):
        """D1.7: filledSize < 0 is rejected."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-NUM-7",
            "cliOrdId": "BOT_SL_7",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": -0.005,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Order filled_size cannot be negative" in reason

    @pytest.mark.asyncio
    async def test_d1_8_filled_size_greater_than_size_rejected(self):
        """D1.8: filledSize > size is rejected."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-NUM-8",
            "cliOrdId": "BOT_SL_8",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": 0.015,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Order filled_size exceeds total order size" in reason

    @pytest.mark.asyncio
    async def test_d1_9_malformed_order_does_not_abort_reconciliation_for_other_trades(self):
        """D1.9: A malformed order on one trade quarantines that trade without aborting reconciliation for other trades."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01},
            {"symbol": "PF_ETHUSD", "side": "long", "size": 0.5},
        ])
        # Two open orders returned: XBT has malformed non-numeric size, ETH is valid
        async def mock_open_orders(symbol=None, raise_errors=False):
            orders = [
                {
                    "order_id": "SL-BAD-XBT",
                    "cliOrdId": "BOT_SL_XBT",
                    "symbol": "pf_xbtusd",
                    "orderType": "stp",
                    "side": "sell",
                    "size": "malformed_string_size",
                    "filledSize": 0.0,
                    "stopPrice": 88000.0,
                    "reduceOnly": True,
                    "status": "untouched",
                },
                {
                    "order_id": "SL-GOOD-ETH",
                    "cliOrdId": "BOT_SL_ETH",
                    "symbol": "pf_ethusd",
                    "orderType": "stp",
                    "side": "sell",
                    "size": 0.5,
                    "filledSize": 0.0,
                    "stopPrice": 2800.0,
                    "reduceOnly": True,
                    "status": "untouched",
                }
            ]
            if symbol:
                s_up = symbol.upper()
                return [o for o in orders if o["symbol"].upper() == s_up]
            return orders

        mock_client.get_open_orders = AsyncMock(side_effect=mock_open_orders)
        wrapper._rest_client = mock_client
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-MULTI-BAD-1",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={"protections": {"version": 1, "sl": {"order_id": "SL-BAD-XBT", "status": "ATTACHED"}}}
        )
        LiveTradesService.create_live_trade(
            order_id="ORD-MULTI-GOOD-2",
            symbol="PF_ETHUSD",
            side="long",
            qty=0.5,
            entry=3000.0,
            stop=2800.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={"protections": {"version": 1, "sl": {"order_id": "SL-GOOD-ETH", "status": "ATTACHED"}}}
        )

        report = await wrapper.reconcile_on_startup()

        # Trade 1 (XBT) must be quarantined due to malformed size
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        assert "PF_XBTUSD" not in report["healthy"]

        # Trade 2 (ETH) must be processed successfully and declared HEALTHY
        assert "PF_ETHUSD" in report["healthy"]
        assert "PF_ETHUSD" not in wrapper._reconciliation_required_symbols
        tracker_eth = wrapper._active_trackers.get("PF_ETHUSD")
        assert tracker_eth is not None
        assert tracker_eth.status == "OPEN"
        assert tracker_eth.sl_tp_state == "SL_ATTACHED"

    # ── D2: SÉCURISATION DU MODE STRICT ───────────────────────────────

    @pytest.mark.asyncio
    async def test_d2_1_inspect_signature_value_error_raises_in_strict_mode(self):
        """D2.1: If inspect.signature raises ValueError, get_open_orders(raise_errors=True) raises TypeError."""
        import inspect
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = MagicMock()
        mock_client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = mock_client

        with patch("inspect.signature", side_effect=ValueError("Cannot inspect signature")):
            with pytest.raises(TypeError, match="Cannot inspect Kraken client get_open_orders signature in strict mode"):
                await wrapper.get_open_orders("PF_XBTUSD", raise_errors=True)

    @pytest.mark.asyncio
    async def test_d2_2_inspect_signature_type_error_raises_in_strict_mode(self):
        """D2.2: If inspect.signature raises TypeError, get_open_orders(raise_errors=True) raises TypeError."""
        import inspect
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = MagicMock()
        mock_client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = mock_client

        with patch("inspect.signature", side_effect=TypeError("Callable has no inspectable signature")):
            with pytest.raises(TypeError, match="Cannot inspect Kraken client get_open_orders signature in strict mode"):
                await wrapper.get_open_orders("PF_XBTUSD", raise_errors=True)

    @pytest.mark.asyncio
    async def test_d2_3_decorated_method_with_functools_wraps_supported(self):
        """D2.3: Method decorated with functools.wraps is inspected correctly and preserves strict mode."""
        import functools
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")

        class DecoratedClient:
            def _decorator(self, func):
                @functools.wraps(func)
                async def inner(*args, **kwargs):
                    return await func(*args, **kwargs)
                return inner

            def __init__(self):
                async def _get_open_orders(symbol=None, raise_errors=False):
                    if raise_errors:
                        raise ValueError("Simulated strict error")
                    return []
                self.get_open_orders = self._decorator(_get_open_orders)

        wrapper._rest_client = DecoratedClient()

        with pytest.raises(ValueError, match="Simulated strict error"):
            await wrapper.get_open_orders("PF_XBTUSD", raise_errors=True)

    @pytest.mark.asyncio
    async def test_d2_4_decorated_method_without_functools_wraps_raises_in_strict_mode(self):
        """D2.4: Method decorated without wraps and without kwargs raises TypeError in strict mode."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")

        class BareDecoratedClient:
            def __init__(self):
                # Bare decorator with fixed parameters lacking raise_errors and **kwargs
                def bare_wrapper(symbol=None):
                    async def inner():
                        return []
                    return inner()
                self.get_open_orders = bare_wrapper

        wrapper._rest_client = BareDecoratedClient()

        with pytest.raises(TypeError, match="does not support strict mode"):
            await wrapper.get_open_orders("PF_XBTUSD", raise_errors=True)

    @pytest.mark.asyncio
    async def test_d2_5_proxy_without_raise_errors_support_raises_in_strict_mode(self):
        """D2.5: Proxy lacking raise_errors parameter raises TypeError in strict mode."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")

        class LegacyProxyClient:
            async def get_open_orders(self, symbol=None):
                return []

        wrapper._rest_client = LegacyProxyClient()

        with pytest.raises(TypeError, match="does not support strict mode"):
            await wrapper.get_open_orders("PF_XBTUSD", raise_errors=True)

    @pytest.mark.asyncio
    async def test_d2_6_proxy_without_raise_errors_quarantines_symbol_in_reconcile(self):
        """D2.6: When client proxy lacks raise_errors, reconcile_on_startup quarantines the trade and symbol."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")

        class LegacyProxyClient:
            async def get_positions(self):
                return [{"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}]
            async def get_open_orders(self, symbol=None):
                return []

        wrapper._rest_client = LegacyProxyClient()
        wrapper._connected = True

        LiveTradesService.create_live_trade(
            order_id="ORD-LEGACY-PROXY-1",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={"protections": {"version": 1, "sl": {"order_id": "SL-1", "status": "ATTACHED"}}}
        )

        report = await wrapper.reconcile_on_startup()

        # Must NOT be healthy, must be quarantined with RECONCILIATION_API_ERROR, never MISSING_ON_EXCHANGE
        assert "PF_XBTUSD" not in report["healthy"]
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols

        prots = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-LEGACY-PROXY-1")
        assert prots["sl"]["status"] == "RECONCILIATION_API_ERROR"
        assert prots["sl"]["status"] != "MISSING_ON_EXCHANGE"

    @pytest.mark.asyncio
    async def test_d2_7_legacy_non_strict_behavior_preserved_when_raise_errors_false(self):
        """D2.7: In non-strict mode (raise_errors=False), legacy proxy is called without error and returns []."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")

        class LegacyProxyClient:
            async def get_open_orders(self, symbol=None):
                return [{"order_id": "LEGACY-ORD-1"}]

        wrapper._rest_client = LegacyProxyClient()

        orders = await wrapper.get_open_orders("PF_XBTUSD", raise_errors=False)
        assert len(orders) == 1
        assert orders[0]["order_id"] == "LEGACY-ORD-1"

    # ── R3: PRIORITÉ DES CHAMPS ET COALESCE (AMENDÉ PAR LOT 1.3-B) ──

    @pytest.mark.asyncio
    async def test_r3_1_filled_size_zero_with_cum_qty_nonzero_rejected(self):
        """R3.1: filledSize = 0.0 with cum_qty = 0.005 is rejected as contradictory (C1)."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-R3-1",
            "cliOrdId": "BOT_SL_R3_1",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": 0.0,
            "cum_qty": 0.005,  # contradictory with filledSize=0.0
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Contradictory filled quantity fields" in reason

    @pytest.mark.asyncio
    async def test_r3_2_filled_size_divergent_from_cum_qty_rejected(self):
        """R3.2: filledSize = 0.001 and cum_qty = 0.005 diverge -> rejected as contradictory (C1)."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-R3-2",
            "cliOrdId": "BOT_SL_R3_2",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.011,
            "filledSize": 0.001,
            "cum_qty": 0.005,  # contradictory with filledSize=0.001
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "partiallyFilled",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Contradictory filled quantity fields" in reason

    @pytest.mark.asyncio
    async def test_r3_3_filled_size_none_with_valid_alias(self):
        """R3.3: filledSize = None falls back to next present alias (filled_size = 0.005)."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-R3-3",
            "cliOrdId": "BOT_SL_R3_3",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.015,
            "filledSize": None,
            "filled_size": 0.005,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "partiallyFilled",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        # Remaining qty is 0.015 - 0.005 = 0.010 == trade_qty -> ACCEPTED
        assert is_valid is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_r3_4_absence_of_all_aliases(self):
        """R3.4: Absence of any filled alias safely defaults to 0.0."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-R3-4",
            "cliOrdId": "BOT_SL_R3_4",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        # Remaining qty is 0.01 - 0.0 = 0.01 -> ACCEPTED
        assert is_valid is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_r3_5_remaining_quantity_calculation_strict(self):
        """R3.5: Remaining quantity is strictly size - filled_size, rejected if insufficient."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-R3-5",
            "cliOrdId": "BOT_SL_R3_5",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.015,
            "filledSize": 0.005,  # remaining = 0.010
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "partiallyFilled",
        }
        # Required 0.010 -> ACCEPTED
        is_valid, _ = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.010,
            entry_price=90000.0,
        )
        assert is_valid is True

        # Required 0.011 -> REJECTED (insufficient coverage)
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.011,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Insufficient active remaining qty" in reason


class TestLot1_3B_QuantityAliasContradictions:
    """
    Mandatory tests for Phase 3.2 - Lot 1.3-B:
    - C1 (P1): Executed quantity alias contradictions & invalid official field rejection
    - C3 (P2): Total size alias contradictions & invalid official field rejection
    - End-to-end multi-trade reconciliation quarantine & non-abortion
    """

    # ── C1: QUANTITÉS EXÉCUTÉES ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_c1_1_filled_size_and_cum_qty_contradictory_rejected(self):
        """C1.1: filledSize=0.001 and cum_qty=0.005 diverge -> rejected with Contradictory filled quantity fields."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-C1-1",
            "cliOrdId": "BOT_SL_1",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.015,
            "filledSize": 0.001,
            "cum_qty": 0.005,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "partiallyFilled",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Contradictory filled quantity fields" in reason
        assert "'filledSize'=0.001" in reason
        assert "'cum_qty'=0.005" in reason

    @pytest.mark.asyncio
    async def test_c1_2_filled_size_and_cum_qty_equal_accepted(self):
        """C1.2: filledSize=0.005 and cum_qty=0.005 are equal -> accepted."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-C1-2",
            "cliOrdId": "BOT_SL_2",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.015,
            "filledSize": 0.005,
            "cum_qty": 0.005,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "partiallyFilled",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.010,
            entry_price=90000.0,
        )
        assert is_valid is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_c1_3_filled_size_zero_with_cum_qty_nonzero_rejected(self):
        """C1.3: filledSize=0.0 and cum_qty=0.005 diverge -> rejected with Contradictory filled quantity fields."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-C1-3",
            "cliOrdId": "BOT_SL_3",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": 0.0,
            "cum_qty": 0.005,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Contradictory filled quantity fields" in reason

    @pytest.mark.asyncio
    async def test_c1_4_filled_size_invalid_with_valid_alias_rejected(self):
        """C1.4: filledSize='invalid' with cum_qty=0.005 -> rejected immediately without falling back to cum_qty."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-C1-4",
            "cliOrdId": "BOT_SL_4",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": "corrupted_val",
            "cum_qty": 0.005,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Non-numeric filled quantity in field 'filledSize'" in reason

    @pytest.mark.asyncio
    async def test_c1_5_filled_size_negative_nan_inf_rejected(self):
        """C1.5: filledSize negative, NaN, or Inf are rejected."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        base = {
            "order_id": "SL-C1-5",
            "cliOrdId": "BOT_SL_5",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        for bad_val, expected_err in [
            (-0.001, "Order filled_size cannot be negative"),
            (float("nan"), "Non-finite order filled_size"),
            (float("inf"), "Non-finite order filled_size"),
        ]:
            order = dict(base, filledSize=bad_val)
            is_valid, reason = wrapper._validate_recovered_protection(
                order=order,
                expected_symbol="PF_XBTUSD",
                protection_type="sl",
                trade_side="long",
                trade_qty=0.01,
                entry_price=90000.0,
            )
            assert is_valid is False, f"Failed for {bad_val}"
            assert expected_err in reason

    @pytest.mark.asyncio
    async def test_c1_6_no_filled_aliases_defaults_to_zero(self):
        """C1.6: When all filled aliases are absent or None, filled_size safely defaults to 0.0."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-C1-6",
            "cliOrdId": "BOT_SL_6",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "filledSize": None,
            "filled_size": None,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is True
        assert reason is None

    # ── C3: QUANTITÉS TOTALES ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_c3_1_size_and_amount_contradictory_rejected(self):
        """C3.1: size=0.02 and amount=0.01 diverge -> rejected with Contradictory total size fields."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-C3-1",
            "cliOrdId": "BOT_SL_C3_1",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.02,
            "amount": 0.01,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Contradictory total size fields" in reason
        assert "'size'=0.02" in reason
        assert "'amount'=0.01" in reason

    @pytest.mark.asyncio
    async def test_c3_2_size_and_amount_equal_accepted(self):
        """C3.2: size=0.01 and amount=0.01 are equal -> accepted."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-C3-2",
            "cliOrdId": "BOT_SL_C3_2",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": 0.01,
            "amount": 0.01,
            "order_qty": 0.01,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_c3_3_size_invalid_with_valid_amount_rejected(self):
        """C3.3: size='bad' and amount=0.01 -> rejected immediately without falling back to amount."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        order = {
            "order_id": "SL-C3-3",
            "cliOrdId": "BOT_SL_C3_3",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "size": "bad",
            "amount": 0.01,
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        is_valid, reason = wrapper._validate_recovered_protection(
            order=order,
            expected_symbol="PF_XBTUSD",
            protection_type="sl",
            trade_side="long",
            trade_qty=0.01,
            entry_price=90000.0,
        )
        assert is_valid is False
        assert "Non-numeric total size in field 'size'" in reason

    @pytest.mark.asyncio
    async def test_c3_4_size_negative_zero_nan_inf_rejected(self):
        """C3.4: size negative, zero, NaN, or Inf are rejected."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        base = {
            "order_id": "SL-C3-4",
            "cliOrdId": "BOT_SL_C3_4",
            "symbol": "pf_xbtusd",
            "orderType": "stp",
            "side": "sell",
            "stopPrice": 88000.0,
            "reduceOnly": True,
            "status": "untouched",
        }
        for bad_val, expected_err in [
            (-0.01, "Order size must be strictly positive"),
            (0.0, "Order size must be strictly positive"),
            (float("nan"), "Non-finite order size"),
            (float("inf"), "Non-finite order size"),
        ]:
            order = dict(base, size=bad_val)
            is_valid, reason = wrapper._validate_recovered_protection(
                order=order,
                expected_symbol="PF_XBTUSD",
                protection_type="sl",
                trade_side="long",
                trade_qty=0.01,
                entry_price=90000.0,
            )
            assert is_valid is False, f"Failed for {bad_val}"
            assert expected_err in reason

    # ── RÉCONCILIATION DE BOUT EN BOUT ───────────────────────────────

    @pytest.mark.asyncio
    async def test_reconciliation_quarantines_contradictory_order_and_locks_symbol(self):
        """
        An order with contradictory quantity aliases is quarantined with RECONCILIATION_API_ERROR,
        locks the symbol in _reconciliation_required_symbols, and allows other clean trades to reconcile.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        wrapper._connected = True

        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01},
            {"symbol": "PF_ETHUSD", "side": "long", "size": 0.5},
        ])

        async def mock_open_orders(symbol=None, raise_errors=False):
            orders = [
                # Contradictory filled quantities: filledSize=0.001 vs cum_qty=0.005
                {
                    "order_id": "SL-XBT-CONTRA",
                    "cliOrdId": "BOT_SL_XBT",
                    "symbol": "pf_xbtusd",
                    "orderType": "stp",
                    "side": "sell",
                    "size": 0.01,
                    "filledSize": 0.001,
                    "cum_qty": 0.005,
                    "stopPrice": 88000.0,
                    "reduceOnly": True,
                    "status": "partiallyFilled",
                },
                # Clean order for ETH
                {
                    "order_id": "SL-ETH-CLEAN",
                    "cliOrdId": "BOT_SL_ETH",
                    "symbol": "pf_ethusd",
                    "orderType": "stp",
                    "side": "sell",
                    "size": 0.5,
                    "filledSize": 0.0,
                    "cum_qty": 0.0,
                    "stopPrice": 2800.0,
                    "reduceOnly": True,
                    "status": "untouched",
                }
            ]
            if symbol:
                s_up = symbol.upper()
                return [o for o in orders if o["symbol"].upper() == s_up]
            return orders

        mock_client.get_open_orders = AsyncMock(side_effect=mock_open_orders)
        wrapper._rest_client = mock_client

        LiveTradesService.create_live_trade(
            order_id="ORD-XBT-1",
            symbol="PF_XBTUSD",
            side="long",
            qty=0.01,
            entry=90000.0,
            stop=88000.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={"protections": {"version": 1, "sl": {"order_id": "SL-XBT-CONTRA", "status": "ATTACHED"}}}
        )

        LiveTradesService.create_live_trade(
            order_id="ORD-ETH-1",
            symbol="PF_ETHUSD",
            side="long",
            qty=0.5,
            entry=3000.0,
            stop=2800.0,
            tp=0.0,
            broker="kraken",
            raw_exchange={"protections": {"version": 1, "sl": {"order_id": "SL-ETH-CLEAN", "status": "ATTACHED"}}}
        )

        report = await wrapper.reconcile_on_startup()

        # 1. Contradictory order must NOT be declared healthy
        assert "PF_XBTUSD" not in report["healthy"]
        # 2. Symbol must be quarantined in _reconciliation_required_symbols
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        # 3. Trade protections must be marked with INVALID_ON_EXCHANGE, never HEALTHY or ATTACHED
        xbt_prots = LiveTradesService.get_protections("PF_XBTUSD", order_id="ORD-XBT-1")
        assert xbt_prots["sl"]["status"] == "INVALID_ON_EXCHANGE"

        # 4. Clean trade ETH must reconcile successfully and not be aborted by XBT
        assert "PF_ETHUSD" in report["healthy"]
        eth_prots = LiveTradesService.get_protections("PF_ETHUSD", order_id="ORD-ETH-1")
        assert eth_prots["sl"]["status"] == "ATTACHED"



