"""
P1/P2 Reliability Tests for Kraken Futures Executor:
1-6: Deduplication P1 in _resolve_live_position_propagation (fill_id, duplicates, missing fill_id, partial fills, threshold tolerance).
7-8: SQLite persistence verification post-acceptance (fail-closed, quarantine, global entry lock, no duplicate orders).
9-10: Post-SL verification with actual Kraken UUIDs (immediate execution confirmation vs unproven closure fail-closed).

Uses strictly temporary databases and mocked API responses.
"""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)


class TestKrakenP1P2Reliability(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_db = os.path.join(self.tmp_dir.name, "test_p1_p2.db")
        os.environ["SMARTTRADE_DB_PATH"] = self.tmp_db

        self.executor = KrakenFuturesExecutorWrapper()
        self.executor._rest_client = MagicMock()
        self.executor._startup_reconciliation_completed = True
        self.executor._global_entry_lock = False
        self.executor._global_entry_lock_reason = None
        self.executor._single_entry_mode = False
        self.executor._check_preflight_position_gate = AsyncMock(return_value=(True, None, 0.0))
        self.executor._check_preflight_open_orders_gate = AsyncMock(return_value=(True, None))
        self.executor._reconciliation_required_symbols.clear()
        self.executor._active_trackers.clear()

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────────
    # ÉTAPE 1 : DÉDUPLICATION P1 DANS _resolve_live_position_propagation
    # ──────────────────────────────────────────────────────────────────────────

    async def test_1_deux_fills_distincts_meme_ordre_cumul_exact(self):
        """Test 1 : Deux fills distincts pour le même ordre (fill_id différents) doivent être cumulés."""
        entry_oid = "33fa91aa-0000-4000-8000-000000000001"
        closing_oid = "33fa91aa-0000-4000-8000-000000000002"
        now_iso = datetime.now(timezone.utc).isoformat()

        fills = [
            {
                "fill_id": "FID-1",
                "order_id": closing_oid,
                "symbol": "PF_XBTUSD",
                "side": "sell",
                "size": 0.4,
                "price": 60000.0,
                "fillTime": now_iso,
            },
            {
                "fill_id": "FID-2",
                "order_id": closing_oid,
                "symbol": "PF_XBTUSD",
                "side": "sell",
                "size": 0.6,
                "price": 60000.0,
                "fillTime": now_iso,
            },
        ]

        self.executor._fetch_position_qty_strict = AsyncMock(return_value=0.0)
        self.executor._fetch_fills_strict = AsyncMock(return_value=fills)

        status, qty, reason = await self.executor._resolve_live_position_propagation(
            kraken_symbol="PF_XBTUSD",
            position_side="long",
            expected_qty=1.0,
            entry_order_id=entry_oid,
            entry_timestamp=now_iso,
            correlated_order_ids={closing_oid},
            max_attempts=1,
        )

        self.assertEqual(status, "GENUINELY_CLOSED")
        self.assertIn("closed_qty=1.0 >= expected=1.0", reason)

    async def test_2_meme_fill_present_deux_fois_ignore_doublon(self):
        """Test 2 : Le même fill présent deux fois (même fill_id) ne doit pas être compté deux fois."""
        entry_oid = "33fa91aa-0000-4000-8000-000000000001"
        closing_oid = "33fa91aa-0000-4000-8000-000000000002"
        now_iso = datetime.now(timezone.utc).isoformat()

        # Le fill a une taille de 0.5. Si compté 2 fois, total=1.0 (faux GENUINELY_CLOSED).
        # Grâce à la déduplication, total=0.5 < 1.0 (INDETERMINATE).
        fills = [
            {
                "fill_id": "FID-DUPLICATE",
                "order_id": closing_oid,
                "symbol": "PF_XBTUSD",
                "side": "sell",
                "size": 0.5,
                "price": 60000.0,
                "fillTime": now_iso,
            },
            {
                "fill_id": "FID-DUPLICATE",
                "order_id": closing_oid,
                "symbol": "PF_XBTUSD",
                "side": "sell",
                "size": 0.5,
                "price": 60000.0,
                "fillTime": now_iso,
            },
        ]

        self.executor._fetch_position_qty_strict = AsyncMock(return_value=0.0)
        self.executor._fetch_fills_strict = AsyncMock(return_value=fills)

        status, qty, reason = await self.executor._resolve_live_position_propagation(
            kraken_symbol="PF_XBTUSD",
            position_side="long",
            expected_qty=1.0,
            entry_order_id=entry_oid,
            entry_timestamp=now_iso,
            correlated_order_ids={closing_oid},
            max_attempts=1,
        )

        self.assertEqual(status, "INDETERMINATE")

    async def test_3_plusieurs_fills_quantites_differentes(self):
        """Test 3 : Plusieurs fills avec quantités hétérogènes cumulées avec exactitude."""
        entry_oid = "33fa91aa-0000-4000-8000-000000000001"
        closing_oid = "33fa91aa-0000-4000-8000-000000000002"
        now_iso = datetime.now(timezone.utc).isoformat()

        fills = [
            {"fill_id": "F1", "order_id": closing_oid, "symbol": "PF_ETHUSD", "side": "buy", "size": 0.12, "fillTime": now_iso},
            {"fill_id": "F2", "order_id": closing_oid, "symbol": "PF_ETHUSD", "side": "buy", "size": 0.38, "fillTime": now_iso},
            {"fill_id": "F3", "order_id": closing_oid, "symbol": "PF_ETHUSD", "side": "buy", "size": 0.50, "fillTime": now_iso},
        ]

        self.executor._fetch_position_qty_strict = AsyncMock(return_value=0.0)
        self.executor._fetch_fills_strict = AsyncMock(return_value=fills)

        status, qty, reason = await self.executor._resolve_live_position_propagation(
            kraken_symbol="PF_ETHUSD",
            position_side="short",
            expected_qty=1.0,
            entry_order_id=entry_oid,
            entry_timestamp=now_iso,
            correlated_order_ids={closing_oid},
            max_attempts=1,
        )

        self.assertEqual(status, "GENUINELY_CLOSED")
        self.assertIn("closed_qty=1.0", reason)

    async def test_4_fill_sans_fill_id_rejete_fail_closed(self):
        """Test 4 : Fill sans fill_id (None ou vide) rejeté sans inventer d'identifiant."""
        entry_oid = "33fa91aa-0000-4000-8000-000000000001"
        closing_oid = "33fa91aa-0000-4000-8000-000000000002"
        now_iso = datetime.now(timezone.utc).isoformat()

        fills = [
            {
                "fill_id": None,
                "order_id": closing_oid,
                "symbol": "PF_XBTUSD",
                "side": "sell",
                "size": 1.0,
                "fillTime": now_iso,
            },
            {
                "fill_id": "",
                "order_id": closing_oid,
                "symbol": "PF_XBTUSD",
                "side": "sell",
                "size": 1.0,
                "fillTime": now_iso,
            },
        ]

        self.executor._fetch_position_qty_strict = AsyncMock(return_value=0.0)
        self.executor._fetch_fills_strict = AsyncMock(return_value=fills)

        status, qty, reason = await self.executor._resolve_live_position_propagation(
            kraken_symbol="PF_XBTUSD",
            position_side="long",
            expected_qty=1.0,
            entry_order_id=entry_oid,
            entry_timestamp=now_iso,
            correlated_order_ids={closing_oid},
            max_attempts=1,
        )

        self.assertEqual(status, "INDETERMINATE")

    async def test_5_doublon_fill_partiel_ne_devient_pas_complet(self):
        """Test 5 : Un fill partiel dupliqué reste strictement partiel et retourne INDETERMINATE."""
        entry_oid = "33fa91aa-0000-4000-8000-000000000001"
        closing_oid = "33fa91aa-0000-4000-8000-000000000002"
        now_iso = datetime.now(timezone.utc).isoformat()

        # 3 copies du même fill partiel de 0.4
        fills = [
            {"fill_id": "PARTIAL-1", "order_id": closing_oid, "symbol": "PF_XBTUSD", "side": "sell", "size": 0.4, "fillTime": now_iso},
            {"fill_id": "PARTIAL-1", "order_id": closing_oid, "symbol": "PF_XBTUSD", "side": "sell", "size": 0.4, "fillTime": now_iso},
            {"fill_id": "PARTIAL-1", "order_id": closing_oid, "symbol": "PF_XBTUSD", "side": "sell", "size": 0.4, "fillTime": now_iso},
        ]

        self.executor._fetch_position_qty_strict = AsyncMock(return_value=0.0)
        self.executor._fetch_fills_strict = AsyncMock(return_value=fills)

        status, qty, reason = await self.executor._resolve_live_position_propagation(
            kraken_symbol="PF_XBTUSD",
            position_side="long",
            expected_qty=1.0,
            entry_order_id=entry_oid,
            entry_timestamp=now_iso,
            correlated_order_ids={closing_oid},
            max_attempts=1,
        )

        self.assertEqual(status, "INDETERMINATE")

    async def test_6_quantite_seuil_tolerance(self):
        """Test 6 : Vérification exacte du seuil de tolérance (1e-5)."""
        entry_oid = "33fa91aa-0000-4000-8000-000000000001"
        closing_oid = "33fa91aa-0000-4000-8000-000000000002"
        now_iso = datetime.now(timezone.utc).isoformat()

        self.executor._fetch_position_qty_strict = AsyncMock(return_value=0.0)

        # Cas 6a : Juste en dessous du seuil (1.0 - 2e-5) -> INDETERMINATE
        fills_below = [
            {"fill_id": "F-BELOW", "order_id": closing_oid, "symbol": "PF_XBTUSD", "side": "sell", "size": 1.0 - 2e-5, "fillTime": now_iso}
        ]
        self.executor._fetch_fills_strict = AsyncMock(return_value=fills_below)
        status, _, _ = await self.executor._resolve_live_position_propagation(
            kraken_symbol="PF_XBTUSD",
            position_side="long",
            expected_qty=1.0,
            entry_order_id=entry_oid,
            entry_timestamp=now_iso,
            correlated_order_ids={closing_oid},
            max_attempts=1,
        )
        self.assertEqual(status, "INDETERMINATE")

        # Cas 6b : Dans la marge de tolérance (1.0 - 5e-6 >= 1.0 - 1e-5) -> GENUINELY_CLOSED
        fills_above = [
            {"fill_id": "F-ABOVE", "order_id": closing_oid, "symbol": "PF_XBTUSD", "side": "sell", "size": 1.0 - 5e-6, "fillTime": now_iso}
        ]
        self.executor._fetch_fills_strict = AsyncMock(return_value=fills_above)
        status, _, _ = await self.executor._resolve_live_position_propagation(
            kraken_symbol="PF_XBTUSD",
            position_side="long",
            expected_qty=1.0,
            entry_order_id=entry_oid,
            entry_timestamp=now_iso,
            correlated_order_ids={closing_oid},
            max_attempts=1,
        )
        self.assertEqual(status, "GENUINELY_CLOSED")

    # ──────────────────────────────────────────────────────────────────────────
    # ÉTAPE 2 : PERSISTANCE APRÈS ACCEPTATION KRAKEN
    # ──────────────────────────────────────────────────────────────────────────

    @patch("backend.services.live_trades_service.LiveTradesService.create_live_trade")
    @patch("backend.services.live_trades_service.LiveTradesService.update_protection_status")
    @patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined")
    async def test_7_echec_persistance_sl_declenche_quarantaine_fail_closed(
        self, mock_quarantine, mock_update_status, mock_create_trade
    ):
        """Test 7 : Si l'écriture SQLite échoue après acceptation du SL, fail-closed + quarantaine."""
        mock_create_trade.return_value = MagicMock(id="TRADE-1")
        
        # Intention succeeds, ATTACHED persistence fails
        def fake_update(symbol, protection_type, status, **kwargs):
            if status == "INTENT_RECORDED":
                return True
            if status == "ATTACHED":
                return False
            return True

        mock_update_status.side_effect = fake_update
        mock_quarantine.return_value = True

        tracker = KrakenTradeTracker(
            order_id="ENTRY-123",
            client_oid="BOT_PF_XBTUSD_123",
            symbol="PF_XBTUSD",
            side="long",
            filled_qty=1.0,
            expected_exposure=1.0,
            status="OPEN",
        )
        self.executor._active_trackers["PF_XBTUSD"] = tracker

        self.executor.check_position_integrity = AsyncMock(return_value=(True, None, 1.0))
        self.executor._fetch_position_qty_strict = AsyncMock(return_value=1.0)
        self.executor._place_stop_order_rest = AsyncMock(return_value={
            "success": True,
            "order_id": "KRAKEN-SL-UUID-999",
            "verified": True,
        })
        self.executor._place_order_rest = AsyncMock(return_value={
            "success": True,
            "order_id": "ENTRY-123",
            "client_order_id": "BOT_PF_XBTUSD_123",
            "is_partial_fill": False,
        })
        self.executor._confirm_executed_qty = AsyncMock(return_value={
            "state": "CONFIRMED",
            "qty": 1.0,
            "source": "fills",
            "fills_qty": 1.0,
            "position_qty": 1.0,
            "vwap": 60000.0,
            "matched_fills": [{"fillTime": datetime.now(timezone.utc).isoformat()}],
        })
        self.executor._resolve_live_position_propagation = AsyncMock(return_value=("CONFIRMED", 1.0, None))
        self.executor.attach_protection_intention = MagicMock(return_value=True)

        res = await self.executor.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=1.0,
            order_type="market",
            entry_price=60000.0,
            stop_loss=58000.0,
        )

        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "MANUAL_RECONCILIATION_REQUIRED")
        self.assertTrue(self.executor._global_entry_lock)
        self.assertIn("PF_XBTUSD", self.executor._reconciliation_required_symbols)
        active_tracker = self.executor._active_trackers["PF_XBTUSD"]
        self.assertEqual(active_tracker.sl_tp_state, "SQLITE_PERSIST_FAILED")
        self.assertEqual(active_tracker.status, "MANUAL_RECONCILIATION_REQUIRED")
        mock_quarantine.assert_called_once()

    @patch("backend.services.live_trades_service.LiveTradesService.create_live_trade")
    @patch("backend.services.live_trades_service.LiveTradesService.update_protection_status")
    @patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined")
    async def test_8_echec_persistance_tp_declenche_quarantaine_fail_closed(
        self, mock_quarantine, mock_update_status, mock_create_trade
    ):
        """Test 8 : Si l'écriture SQLite échoue après acceptation du TP, fail-closed + quarantaine."""
        mock_create_trade.return_value = MagicMock(id="TRADE-1")
        # SL succeeds in SQLite, TP ATTACHED fails in SQLite
        def fake_update(symbol, protection_type, status, **kwargs):
            if protection_type == "sl":
                return True
            if status == "INTENT_RECORDED":
                return True
            if status == "ATTACHED":
                return False
            return True

        mock_update_status.side_effect = fake_update
        mock_quarantine.return_value = True

        tracker = KrakenTradeTracker(
            order_id="ENTRY-123",
            client_oid="BOT_PF_XBTUSD_123",
            symbol="PF_XBTUSD",
            side="long",
            filled_qty=1.0,
            expected_exposure=1.0,
            status="OPEN",
        )
        self.executor._active_trackers["PF_XBTUSD"] = tracker

        self.executor.check_position_integrity = AsyncMock(return_value=(True, None, 1.0))
        self.executor._fetch_position_qty_strict = AsyncMock(return_value=1.0)
        self.executor._place_stop_order_rest = AsyncMock(return_value={
            "success": True,
            "order_id": "KRAKEN-SL-UUID-999",
            "verified": True,
        })
        self.executor._place_tp_order_rest = AsyncMock(return_value={
            "success": True,
            "order_id": "KRAKEN-TP-UUID-888",
            "verified": True,
        })
        self.executor._place_order_rest = AsyncMock(return_value={
            "success": True,
            "order_id": "ENTRY-123",
            "client_order_id": "BOT_PF_XBTUSD_123",
            "is_partial_fill": False,
        })
        self.executor._confirm_executed_qty = AsyncMock(return_value={
            "state": "CONFIRMED",
            "qty": 1.0,
            "source": "fills",
            "fills_qty": 1.0,
            "position_qty": 1.0,
            "vwap": 60000.0,
            "matched_fills": [{"fillTime": datetime.now(timezone.utc).isoformat()}],
        })
        self.executor._resolve_live_position_propagation = AsyncMock(return_value=("CONFIRMED", 1.0, None))
        self.executor.attach_protection_intention = MagicMock(return_value=True)

        res = await self.executor.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=1.0,
            order_type="market",
            entry_price=60000.0,
            stop_loss=58000.0,
            take_profit=65000.0,
        )

        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "MANUAL_RECONCILIATION_REQUIRED")
        self.assertTrue(self.executor._global_entry_lock)
        self.assertIn("PF_XBTUSD", self.executor._reconciliation_required_symbols)
        active_tracker = self.executor._active_trackers["PF_XBTUSD"]
        self.assertEqual(active_tracker.sl_tp_state, "SQLITE_PERSIST_FAILED")
        mock_quarantine.assert_called_once()

    # ──────────────────────────────────────────────────────────────────────────
    # ÉTAPE 3 : VÉRIFICATION POSTÉRIEURE À LA POSE DU SL
    # ──────────────────────────────────────────────────────────────────────────

    @patch("backend.services.live_trades_service.LiveTradesService.create_live_trade")
    @patch("backend.services.live_trades_service.LiveTradesService.update_protection_status")
    async def test_9_cloture_immediate_par_sl_reconnue_par_fills_correles(self, mock_update_status, mock_create_trade):
        """Test 9 : SL exécuté immédiatement sur le marché (pos=0) confirmé par fill corrélé."""
        mock_create_trade.return_value = MagicMock(id="TRADE-1")
        mock_update_status.return_value = True
        entry_oid = "ENTRY-123"
        sl_oid = "KRAKEN-SL-UUID-123"
        now_iso = datetime.now(timezone.utc).isoformat()

        tracker = KrakenTradeTracker(
            order_id=entry_oid,
            client_oid="BOT_PF_XBTUSD_123",
            symbol="PF_XBTUSD",
            side="long",
            filled_qty=1.0,
            expected_exposure=1.0,
            status="OPEN",
        )
        self.executor._active_trackers["PF_XBTUSD"] = tracker

        self.executor._place_order_rest = AsyncMock(return_value={
            "success": True,
            "order_id": entry_oid,
            "client_order_id": "BOT_PF_XBTUSD_123",
            "is_partial_fill": False,
        })
        self.executor._confirm_executed_qty = AsyncMock(return_value={
            "state": "CONFIRMED",
            "qty": 1.0,
            "source": "fills",
            "fills_qty": 1.0,
            "position_qty": 1.0,
            "vwap": 60000.0,
            "matched_fills": [{"fillTime": now_iso}],
        })
        self.executor.attach_protection_intention = MagicMock(return_value=True)

        # 1er resolve_live_position_propagation (avant SL): position confirmée
        # 2ème resolve_live_position_propagation (après échec SL car déjà exécuté): GENUINELY_CLOSED
        async def fake_resolve(**kwargs):
            if kwargs.get("correlated_order_ids"):
                return ("GENUINELY_CLOSED", 0.0, "Correlated closing fills confirmed for cycle (closed_qty=1.0 >= expected=1.0)")
            return ("CONFIRMED", 1.0, None)

        self.executor._resolve_live_position_propagation = AsyncMock(side_effect=fake_resolve)

        # SL non présent dans open orders car exécuté instantanément
        self.executor.check_position_integrity = AsyncMock(return_value=(True, None, 1.0))
        self.executor._fetch_position_qty_strict = AsyncMock(return_value=0.0)
        self.executor._place_stop_order_rest = AsyncMock(return_value={
            "success": False,
            "order_id": sl_oid,
            "verified": False,
            "error": "Protection order not found in Kraken open orders carnet after retries",
        })

        res = await self.executor.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=1.0,
            order_type="market",
            entry_price=60000.0,
            stop_loss=58000.0,
        )

        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "POSITION_GENUINELY_CLOSED")
        active_tracker = self.executor._active_trackers["PF_XBTUSD"]
        self.assertEqual(active_tracker.status, "CLOSED")
        self.assertEqual(active_tracker.expected_exposure, 0.0)
        self.assertNotIn("PF_XBTUSD", self.executor._reconciliation_required_symbols)

    @patch("backend.services.live_trades_service.LiveTradesService.create_live_trade")
    @patch("backend.services.live_trades_service.LiveTradesService.update_protection_status")
    @patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined")
    async def test_10_pos_zero_sans_fills_correles_rejete_fail_closed(self, mock_quarantine, mock_update_status, mock_create_trade):
        """Test 10 : pos=0 après échec SL sans fills corrélés -> rejet fail-closed et quarantaine."""
        mock_create_trade.return_value = MagicMock(id="TRADE-1")
        mock_update_status.return_value = True
        mock_quarantine.return_value = True
        entry_oid = "ENTRY-123"
        sl_oid = "KRAKEN-SL-UUID-123"
        now_iso = datetime.now(timezone.utc).isoformat()

        tracker = KrakenTradeTracker(
            order_id=entry_oid,
            client_oid="BOT_PF_XBTUSD_123",
            symbol="PF_XBTUSD",
            side="long",
            filled_qty=1.0,
            expected_exposure=1.0,
            status="OPEN",
        )
        self.executor._active_trackers["PF_XBTUSD"] = tracker

        self.executor._place_order_rest = AsyncMock(return_value={
            "success": True,
            "order_id": entry_oid,
            "client_order_id": "BOT_PF_XBTUSD_123",
            "is_partial_fill": False,
        })
        self.executor._confirm_executed_qty = AsyncMock(return_value={
            "state": "CONFIRMED",
            "qty": 1.0,
            "source": "fills",
            "fills_qty": 1.0,
            "position_qty": 1.0,
            "vwap": 60000.0,
            "matched_fills": [{"fillTime": now_iso}],
        })
        self.executor.attach_protection_intention = MagicMock(return_value=True)

        # 1er resolve: CONFIRMED
        # 2ème resolve: INDETERMINATE (aucun fill corrélé ne justifie pos=0)
        async def fake_resolve(**kwargs):
            if kwargs.get("correlated_order_ids"):
                return ("INDETERMINATE", 0.0, "Position not visible without valid correlated closing fill")
            return ("CONFIRMED", 1.0, None)

        self.executor._resolve_live_position_propagation = AsyncMock(side_effect=fake_resolve)

        self.executor.check_position_integrity = AsyncMock(return_value=(True, None, 1.0))
        self.executor._fetch_position_qty_strict = AsyncMock(return_value=0.0)
        self.executor._place_stop_order_rest = AsyncMock(return_value={
            "success": False,
            "order_id": sl_oid,
            "verified": False,
            "error": "Protection order rejected by exchange",
        })

        res = await self.executor.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=1.0,
            order_type="market",
            entry_price=60000.0,
            stop_loss=58000.0,
        )

        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "MANUAL_RECONCILIATION_REQUIRED")
        self.assertTrue(self.executor._global_entry_lock)
        self.assertIn("PF_XBTUSD", self.executor._reconciliation_required_symbols)
        active_tracker = self.executor._active_trackers["PF_XBTUSD"]
        self.assertEqual(active_tracker.status, "MANUAL_RECONCILIATION_REQUIRED")
        mock_quarantine.assert_called_once()


if __name__ == "__main__":
    unittest.main()
