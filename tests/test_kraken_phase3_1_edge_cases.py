"""
Unit and Integration Tests for Phase 3.1 Edge Cases - Kraken Futures.
Strictly non-destructive. Production code is NOT modified.
Covers the 4 scenarios defined in Phase 3.1:
  - Scenario 1: Reconnection after network loss
  - Scenario 2: Deferred re-inspection of UNCONFIRMED SL/TP orders
  - Scenario 3: Orphan conditional orders without SQLite tracker
  - Scenario 4: Unprotected residual position after discrepancy and purge
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)


# =====================================================================
# SCENARIO 1 — RECONNEXION APRÈS COUPURE RÉSEAU
# =====================================================================

class TestScenario1_NetworkLossAndReconnection:
    """
    Test resilience during and after network disruption.
    Ensures fail-closed behavior, zero blind orders, and state integrity.
    """

    @pytest.mark.asyncio
    async def test_1_1_network_loss_during_integrity_check_maintains_local_state(self):
        """
        When network fails during check_position_integrity:
        - Returns (False, 'POSITION_FETCH_ERROR: ...')
        - Local tracker state is preserved (not corrupted)
        - Symbol is not unquarantined
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(side_effect=ConnectionResetError("Network unreachable"))
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        tracker = KrakenTradeTracker(
            order_id="TR-1",
            client_oid="BOT_test_1",
            symbol="PF_XBTUSD",
            side="long",
            filled_qty=0.01,
            expected_exposure=0.01,
            status="OPEN",
        )
        wrapper._active_trackers["PF_XBTUSD"] = tracker

        ok, err, live_pos = await wrapper.check_position_integrity("PF_XBTUSD")
        assert ok is False
        assert "POSITION_FETCH_ERROR" in str(err)
        assert tracker.status == "OPEN"
        assert tracker.expected_exposure == 0.01
        # No orders sent
        mock_client.place_order.assert_not_called()
        mock_client.place_market_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_1_2_network_loss_blocks_new_orders_fail_closed(self):
        """
        During network loss, place_order must fail-closed with 0 REST orders transmitted.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(side_effect=TimeoutError("REST gateway timed out"))
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        # Open order attempt
        res_open = await wrapper.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=0.01,
            order_type="market",
            intent="open",
        )
        assert res_open["success"] is False
        assert res_open["code"] == "POSITION_CHECK_FAILED"
        mock_client.place_market_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_1_3_reconnection_allows_fresh_check_without_corruption(self):
        """
        When connectivity is restored:
        - Fresh position check succeeds
        - State accurately reflects verified Kraken position
        - No orders are sent simply due to reconnection
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        tracker = KrakenTradeTracker(
            order_id="TR-2",
            client_oid="BOT_test_2",
            symbol="PF_XBTUSD",
            side="long",
            filled_qty=0.01,
            expected_exposure=0.01,
            status="OPEN",
        )
        wrapper._active_trackers["PF_XBTUSD"] = tracker

        # Phase 1: Network down
        mock_client.get_positions = AsyncMock(side_effect=OSError("Network down"))
        ok_down, err_down, _ = await wrapper.check_position_integrity("PF_XBTUSD")
        assert ok_down is False

        # Phase 2: Network restored, position unchanged on Kraken
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        ok_up, err_up, live_pos = await wrapper.check_position_integrity("PF_XBTUSD")
        assert ok_up is True
        assert err_up is None
        assert live_pos == 0.01
        assert tracker.status == "OPEN"
        mock_client.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_1_4_no_automatic_order_or_exposure_on_reconnection(self):
        """
        Reconnection must NEVER automatically trigger opening or closing orders.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_ETHUSD", "side": "long", "size": 0.5}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        # Simulate reconnect and startup reconciliation
        with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]), \
             patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]):
            await wrapper.reconcile_on_startup()
        mock_client.place_order.assert_not_called()
        mock_client.place_market_order.assert_not_called()


# =====================================================================
# SCENARIO 2 — RÉINSPECTION DIFFÉRÉE DES ORDRES UNCONFIRMED
# =====================================================================

class TestScenario2_DeferredUnconfirmedOrdersReinspection:
    """
    Test handling of CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED state
    and verification of order state upon deferred manual/programmed inspection.
    """

    @pytest.mark.asyncio
    async def test_2_1_unconfirmed_order_sets_critical_flag_and_notes(self):
        """
        When cancel or open_orders verification times out:
        - tracker.sl_tp_state == 'CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED'
        - reconciliation_notes contains UNCONFIRMED_PROTECTIONS
        - No blind close order is sent
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        # Order is active in carnet
        mock_client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "SL-TIMEOUT-99", "symbol": "PF_XBTUSD", "size": 0.01}
        ])
        # Cancel call itself fails with TimeoutError
        mock_client.cancel_order = AsyncMock(side_effect=TimeoutError("Kraken REST timeout"))
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        tracker = KrakenTradeTracker(
            order_id="TR-SL-FAIL",
            client_oid="BOT_sl_fail",
            symbol="PF_XBTUSD",
            side="long",
            filled_qty=0.01,
            expected_exposure=0.01,
            sl_order_id="SL-TIMEOUT-99",
            status="MANUAL_RECONCILIATION_REQUIRED",
        )
        wrapper._active_trackers["PF_XBTUSD"] = tracker

        report = await wrapper.purge_orphan_protections("PF_XBTUSD")
        assert "SL-TIMEOUT-99" in report["unconfirmed"]
        assert tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"
        assert "UNCONFIRMED_PROTECTIONS" in tracker.reconciliation_notes
        mock_client.place_market_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_2_2_reinspection_confirms_cancellation_when_order_left_carnet(self):
        """
        Upon subsequent inspection when network is available:
        If order is no longer in get_open_orders and fill is 0,
        it can be confirmed cancelled.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        # Order is now absent from open orders
        mock_client.get_open_orders = AsyncMock(return_value=[])
        # Cancel returns success (or order already gone)
        mock_client.cancel_order = AsyncMock(return_value={"result": "success"})
        mock_client.get_order_status = AsyncMock(return_value={"status": "cancelled", "filled_size": 0})
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        tracker = KrakenTradeTracker(
            order_id="TR-REINSPECT",
            client_oid="BOT_reinspect",
            symbol="PF_SOLUSD",
            side="long",
            filled_qty=1.0,
            expected_exposure=1.0,
            sl_order_id="SL-DEFERRED-1",
            status="MANUAL_RECONCILIATION_REQUIRED",
            sl_tp_state="CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED",
        )
        wrapper._active_trackers["PF_SOLUSD"] = tracker

        # Re-run purge/inspection
        report = await wrapper.purge_orphan_protections("PF_SOLUSD")
        assert "SL-DEFERRED-1" in report["cancelled"]
        assert "SL-DEFERRED-1" not in report["unconfirmed"]
        assert tracker.sl_tp_state == "PROTECTIONS_PURGED"

    @pytest.mark.asyncio
    async def test_2_3_reinspection_detects_order_still_active_in_carnet(self):
        """
        If order is STILL active in carnet during reinspection,
        it must remain flagged UNCONFIRMED with zero assumption of cancellation.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        # Open orders still shows the SL
        mock_client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "SL-STILL-ACTIVE", "symbol": "PF_XRPUSD", "side": "sell", "size": 20}
        ])
        mock_client.cancel_order = AsyncMock(side_effect=Exception("API rejection"))
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        tracker = KrakenTradeTracker(
            order_id="TR-STILL-ACTIVE",
            client_oid="BOT_still_active",
            symbol="PF_XRPUSD",
            side="long",
            filled_qty=20.0,
            expected_exposure=20.0,
            sl_order_id="SL-STILL-ACTIVE",
            status="MANUAL_RECONCILIATION_REQUIRED",
        )
        wrapper._active_trackers["PF_XRPUSD"] = tracker

        report = await wrapper.purge_orphan_protections("PF_XRPUSD")
        assert "SL-STILL-ACTIVE" in report["unconfirmed"]
        assert tracker.sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"

    @pytest.mark.asyncio
    async def test_2_4_gap_characterization_absence_of_periodic_background_worker(self):
        """
        GAP CHARACTERIZATION:
        Verify that no automatic background polling worker currently exists
        in KrakenFuturesExecutorWrapper to automatically poll UNCONFIRMED orders
        without manual/external invocation.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        # In current Phase 3 production code, there is no background task attribute for unconfirmed order retry
        has_periodic_worker = (
            hasattr(wrapper, "_orphan_retry_worker")
            or hasattr(wrapper, "_unconfirmed_orders_poller")
            or hasattr(wrapper, "start_orphan_poller")
        )
        # This test documents the GAP: no automatic worker exists yet.
        assert not has_periodic_worker, (
            "GAP DETECTED: Automatic periodic background poller for UNCONFIRMED orders is absent."
        )


# =====================================================================
# SCENARIO 3 — ORDRES CONDITIONNELS ORPHELINS SANS TRACKER SQLITE
# =====================================================================

class TestScenario3_OrphanConditionalOrdersWithoutDbTracker:
    """
    Test detection and isolation of resting conditional orders on Kraken
    that have no corresponding tracker in SQLite.
    """

    @pytest.mark.asyncio
    async def test_3_1_reconcile_startup_with_db_trade_detects_unknown_conditional_order(self):
        """
        When SQLite has a trade for PF_XBTUSD, but Kraken shows an unrecognized
        open order alongside it:
        - reconcile_on_startup flags the symbol as ambiguous / quarantined
        - Does NOT blindly cancel or execute anything
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.005}
        ])
        # Open orders contains recognized trade_order_id AND an unknown orphan conditional order
        mock_client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "ORD-KNOWN-1", "cliOrdId": "BOT_known", "symbol": "PF_XBTUSD"},
            {"order_id": "ORD-UNKNOWN-ORPHAN", "cliOrdId": "MANUAL_X", "symbol": "PF_XBTUSD"},
        ])
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        fake_db_trade = MagicMock()
        fake_db_trade.symbol = "PF_XBTUSD"
        fake_db_trade.quantity = 0.005
        fake_db_trade.order_id = "ORD-KNOWN-1"
        fake_db_trade.client_order_id = "BOT_known"
        fake_db_trade.side = "long"

        with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[fake_db_trade]), \
             patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]):
            report = await wrapper.reconcile_on_startup()

        assert "PF_XBTUSD" in report["quarantined_symbols"]
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        # Verify no blind cancellations occurred
        mock_client.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_3_2_preflight_gate_blocks_entry_if_orphan_conditional_order_exists(self):
        """
        Even if DB has no trade, if Kraken has an orphan conditional order,
        the Pre-Flight Open Orders Gate must block any new entry.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        # No position
        mock_client.get_positions = AsyncMock(return_value=[])
        # Orphan resting stop order on Kraken
        mock_client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "ORPHAN-STP-1", "symbol": "PF_XBTUSD", "orderType": "stp", "size": 0.005}
        ])
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        res = await wrapper.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=0.005,
            order_type="market",
            intent="open",
        )
        assert res["success"] is False
        assert res["code"] == "ENTRY_BLOCKED_EXTERNAL_OPEN_ORDERS"
        # Zero REST placement orders
        mock_client.place_market_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_3_3_gap_characterization_startup_reconcile_ignores_symbols_without_db_trade(self):
        """
        GAP CHARACTERIZATION:
        In current reconcile_on_startup(), if SQLite has NO open trades for a symbol,
        reconcile_on_startup() iterates only over trades_by_sym.keys() and does not
        proactively query /openorders for unreferenced symbols at boot time.
        (They are blocked at place_order time via preflight gate, but not pre-quarantined at boot).
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[])
        mock_client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "ORPHAN-RESTING-SL", "symbol": "PF_ETHUSD", "size": 0.1}
        ])
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        # DB has NO open trades at all
        with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]), \
             patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]):
            report = await wrapper.reconcile_on_startup()

        # Documenting the GAP: PF_ETHUSD was not in DB, so reconcile_on_startup didn't add it to quarantined_symbols at boot
        gap_present = "PF_ETHUSD" not in report["quarantined_symbols"]
        assert gap_present, (
            "GAP DETECTED: reconcile_on_startup() only checks symbols with existing DB records. "
            "Orphan orders on symbols absent from DB are caught at preflight entry gate, not at boot."
        )

    @pytest.mark.asyncio
    async def test_3_4_no_blind_close_or_cancel_of_orphan_orders_without_tracker(self):
        """
        When orphan conditional orders exist, the bot must NEVER send blind
        cancel_order or place_market_order for orders it does not own.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        # Attempt to close without tracker
        res = await wrapper.close_position(symbol="PF_ETHUSD")
        assert res["success"] is False
        assert res["code"] in ("CLOSE_BLOCKED_NO_TRACKER", "CLOSE_BLOCKED_MANUAL_RECONCILIATION_REQUIRED")
        mock_client.cancel_order.assert_not_called()
        mock_client.place_market_order.assert_not_called()


# =====================================================================
# SCENARIO 4 — EXPOSITION RÉSIDUELLE NON PROTÉGÉE
# =====================================================================

class TestScenario4_UnprotectedResidualPosition:
    """
    Test scenario where external manual reduction occurred (20 XRP -> 10 XRP),
    bot purged the 20 XRP SL to prevent orphan fill, and 10 XRP remains unprotected.
    """

    @pytest.mark.asyncio
    async def test_4_1_discrepancy_detected_and_tracker_quarantined(self):
        """
        When live position is 10 XRP but tracker expected 20 XRP:
        - check_position_integrity detects discrepancy
        - Symbol is added to _reconciliation_required_symbols
        - Tracker status is MANUAL_RECONCILIATION_REQUIRED
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XRPUSD", "side": "long", "size": 10.0}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        tracker = KrakenTradeTracker(
            order_id="TR-XRP-20",
            client_oid="BOT_xrp_20",
            symbol="PF_XRPUSD",
            side="long",
            filled_qty=20.0,
            expected_exposure=20.0,
            sl_order_id="SL-XRP-20",
            status="OPEN",
        )
        wrapper._active_trackers["PF_XRPUSD"] = tracker

        ok, err, live_pos = await wrapper.check_position_integrity("PF_XRPUSD")
        assert ok is False
        assert err == "EXTERNAL_INTERFERENCE_DETECTED"
        assert live_pos == 10.0
        assert "PF_XRPUSD" in wrapper._reconciliation_required_symbols
        assert tracker.status == "MANUAL_RECONCILIATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_4_2_sl_purged_leaving_residual_position_unprotected(self):
        """
        Upon discrepancy, purge_orphan_protections cancels the 20 XRP SL.
        This leaves the residual 10 XRP in carnet without SL protection.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XRPUSD", "side": "long", "size": 10.0}
        ])
        # Open orders initially has the SL-XRP-20, cancel removes it
        calls = 0
        async def _mock_open_orders(*args, **kw):
            nonlocal calls
            calls += 1
            if calls <= 1:
                return [{"order_id": "SL-XRP-20", "symbol": "PF_XRPUSD", "size": 20.0}]
            return []
        mock_client.get_open_orders = AsyncMock(side_effect=_mock_open_orders)
        mock_client.cancel_order = AsyncMock(return_value={"result": "success"})
        mock_client.get_order_status = AsyncMock(return_value={"status": "cancelled", "filled_size": 0})
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        tracker = KrakenTradeTracker(
            order_id="TR-XRP-20",
            client_oid="BOT_xrp_20",
            symbol="PF_XRPUSD",
            side="long",
            filled_qty=20.0,
            expected_exposure=20.0,
            sl_order_id="SL-XRP-20",
            status="OPEN",
        )
        wrapper._active_trackers["PF_XRPUSD"] = tracker

        # Trigger integrity check which triggers purge
        await wrapper.check_position_integrity("PF_XRPUSD")

        # SL was cancelled
        assert tracker.sl_tp_state == "PROTECTIONS_PURGED"
        mock_client.cancel_order.assert_awaited_once_with("SL-XRP-20")

    @pytest.mark.asyncio
    async def test_4_3_no_blind_close_and_no_unvalidated_sl_replacement(self):
        """
        Strict fail-closed rule:
        The bot must NEVER automatically close the remaining 10 XRP
        nor automatically replace a 10 XRP SL without manual validation.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        mock_client = AsyncMock()
        mock_client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XRPUSD", "side": "long", "size": 10.0}
        ])
        mock_client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = mock_client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        tracker = KrakenTradeTracker(
            order_id="TR-XRP-20",
            client_oid="BOT_xrp_20",
            symbol="PF_XRPUSD",
            side="long",
            filled_qty=20.0,
            expected_exposure=20.0,
            status="MANUAL_RECONCILIATION_REQUIRED",
        )
        wrapper._active_trackers["PF_XRPUSD"] = tracker
        wrapper._reconciliation_required_symbols.add("PF_XRPUSD")

        # Attempt to close or reduce
        res = await wrapper.place_order(
            symbol="XRPUSDT",
            side="sell",
            quantity=10.0,
            order_type="market",
            reduce_only=True,
            intent="close",
        )
        assert res["success"] is False
        assert res["code"] == "CLOSE_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"

        # ZERO market orders, ZERO replacement SL orders
        mock_client.place_market_order.assert_not_called()
        mock_client.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_4_4_gap_characterization_alert_string_critical_residual_position_unprotected(self):
        """
        GAP CHARACTERIZATION:
        In current Phase 3 code, purge_orphan_protections logs:
        'WARNING: Remaining exposure is now UNPROTECTED and requires manual intervention.'
        but does NOT emit a structured event/alert token 'CRITICAL_RESIDUAL_POSITION_UNPROTECTED'
        nor a dashboard push event.
        """
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="s")
        # Check if a dedicated method or event emitter for CRITICAL_RESIDUAL_POSITION_UNPROTECTED exists
        has_residual_alert_emitter = (
            hasattr(wrapper, "emit_residual_unprotected_alert")
            or hasattr(wrapper, "notify_residual_unprotected")
        )
        # Document the GAP
        assert not has_residual_alert_emitter, (
            "GAP DETECTED: Structured notification system / UI dashboard emitter "
            "for CRITICAL_RESIDUAL_POSITION_UNPROTECTED is absent in current backend."
        )
