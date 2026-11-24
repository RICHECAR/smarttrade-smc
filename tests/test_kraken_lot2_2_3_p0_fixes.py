"""
Tests unitaires et d'intégration ciblés - Lot 2.2.3:
Correction des deux défauts P0 dans backend/execution/kraken_futures_executor.py

P0-1: Suppression totale des fallbacks TypeError sur get_positions
- _check_preflight_position_gate: appel exclusif force_refresh=True, fail-closed sans fallback
- _fetch_position_qty_strict: appel exclusif force_refresh=True, propagation directe d'erreur sans fallback

P0-2: Persistance durable des anomalies et verrouillage fail-closed
- Vérification stricte du retour exact True de record_exchange_quarantine
- Traitement de tout retour != True (False, None, exception) comme erreur critique (PERSISTENCE_FAILED_LOCKED)
- Indexation distincte de chaque ordre orphelin sans collision ni écrasement
- Clé stable documentée POS_{ex_sym} pour les positions nettes agrégées (sans ordre fictif)
- Maintien du verrou global et startup_reconciliation_completed=False tant qu'une anomalie subsiste
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import sqlite3

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)


# ══════════════════════════════════════════════════════════════════
# P0-1: SUPPRESSION DES FALLBACKS TYPEERROR (Tests 1 à 8)
# ══════════════════════════════════════════════════════════════════

class TestP01TypeErrorFallbackRemoval:
    """Validation stricte de la suppression des fallbacks TypeError sur get_positions."""

    @pytest.mark.asyncio
    async def test_1_preflight_gate_calls_force_refresh_true(self):
        """1. _check_preflight_position_gate appelle get_positions avec force_refresh=True."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])
        wrapper._rest_client = client

        passed, err, size = await wrapper._check_preflight_position_gate("PF_XBTUSD")
        assert passed is True
        assert err is None
        assert size == 0.0
        client.get_positions.assert_called_once_with(force_refresh=True)

    @pytest.mark.asyncio
    async def test_2_preflight_gate_typeerror_fails_closed_no_fallback(self):
        """2. En cas de TypeError dans get_positions, fail-closed immédiat sans aucun fallback."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        # Simuler un client ne supportant pas force_refresh ou levant TypeError
        client.get_positions = AsyncMock(side_effect=TypeError("get_positions() got an unexpected keyword argument 'force_refresh'"))
        wrapper._rest_client = client

        passed, err, size = await wrapper._check_preflight_position_gate("PF_XBTUSD")
        assert passed is False
        assert err == "POSITION_CHECK_FAILED"
        assert size is None
        # Doit être appelé exactement 1 fois: aucun second appel sans force_refresh
        assert client.get_positions.call_count == 1

    @pytest.mark.asyncio
    async def test_3_preflight_gate_runtime_error_fails_closed(self):
        """3. En cas d'erreur réseau/API, fail-closed immédiat (POSITION_CHECK_FAILED)."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(side_effect=RuntimeError("Kraken API 502 Bad Gateway"))
        wrapper._rest_client = client

        passed, err, size = await wrapper._check_preflight_position_gate("PF_XBTUSD")
        assert passed is False
        assert err == "POSITION_CHECK_FAILED"
        assert size is None
        assert client.get_positions.call_count == 1

    @pytest.mark.asyncio
    async def test_4_preflight_gate_none_response_fails_closed(self):
        """4. Si get_positions renvoie None, fail-closed immédiat."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=None)
        wrapper._rest_client = client

        passed, err, size = await wrapper._check_preflight_position_gate("PF_XBTUSD")
        assert passed is False
        assert err == "POSITION_CHECK_FAILED"
        assert size is None

    @pytest.mark.asyncio
    async def test_5_preflight_gate_existing_position_blocks_entry(self):
        """5. Détection stricte d'une position existante: bloque l'entrée."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "size": 0.05, "side": "long"}
        ])
        wrapper._rest_client = client

        passed, err, size = await wrapper._check_preflight_position_gate("PF_XBTUSD")
        assert passed is False
        assert err == "ENTRY_BLOCKED_EXISTING_POSITION"
        assert size == 0.05

    @pytest.mark.asyncio
    async def test_6_fetch_position_qty_strict_calls_force_refresh_true(self):
        """6. _fetch_position_qty_strict appelle get_positions avec force_refresh=True."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "size": 0.02, "side": "long"}
        ])
        wrapper._rest_client = client

        qty = await wrapper._fetch_position_qty_strict("PF_XBTUSD", "long")
        assert qty == 0.02
        client.get_positions.assert_called_once_with(force_refresh=True)

    @pytest.mark.asyncio
    async def test_7_fetch_position_qty_strict_typeerror_raises_no_fallback(self):
        """7. En cas de TypeError sur get_positions, _fetch_position_qty_strict lève directement sans fallback."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(side_effect=TypeError("unexpected keyword 'force_refresh'"))
        wrapper._rest_client = client

        with pytest.raises(TypeError, match="unexpected keyword 'force_refresh'"):
            await wrapper._fetch_position_qty_strict("PF_XBTUSD", "long")

        # Pas de retry / fallback silencieux sans force_refresh
        assert client.get_positions.call_count == 1

    @pytest.mark.asyncio
    async def test_8_fetch_position_qty_strict_api_error_raises(self):
        """8. Si get_positions renvoie None ou une erreur API, _fetch_position_qty_strict lève RuntimeError."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value={"result": "error", "error": "InternalServerError"})
        wrapper._rest_client = client

        with pytest.raises(RuntimeError, match="Kraken positions API error"):
            await wrapper._fetch_position_qty_strict("PF_XBTUSD", "long")


# ══════════════════════════════════════════════════════════════════
# P0-2: PERSISTANCE DURABLE DES ANOMALIES (Tests 9 à 16)
# ══════════════════════════════════════════════════════════════════

class TestP02DurableQuarantinePersistence:
    """Validation de la persistance stricte des anomalies et du verrou fail-closed."""

    @pytest.mark.asyncio
    async def test_9_quarantine_persistence_success_true(self):
        """9. Quand record_exchange_quarantine retourne True, l'anomalie est enregistrée et isolée."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "size": 0.01, "side": "long", "price": 50000.0}
        ])
        client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = client
        wrapper._connected = True

        with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]):
            with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
                with patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", return_value=True) as mock_record:
                    report = await wrapper.reconcile_on_startup()

        # Vérifier appel de persistance avec la clé stable POS_PF_XBTUSD
        assert mock_record.call_count == 1
        call_kwargs = mock_record.call_args.kwargs
        assert call_kwargs["symbol"] == "PF_XBTUSD"
        assert call_kwargs["order_id"] == "POS_PF_XBTUSD"
        assert call_kwargs["anomaly_type"] == "position"
        assert call_kwargs["qty"] == 0.01
        assert call_kwargs["side"] == "long"

        # Le verrou global reste fermé car une anomalie non résolue a été détectée
        assert wrapper._global_entry_lock is True
        assert wrapper._startup_reconciliation_completed is False
        assert report["status"] == "UNRESOLVED_ANOMALIES_LOCKED"

    @pytest.mark.asyncio
    async def test_10_quarantine_persistence_failed_false_triggers_lock(self):
        """10. Si record_exchange_quarantine retourne False, fail-closed PERSISTENCE_FAILED_LOCKED."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_ETHUSD", "size": 0.5, "side": "short", "price": 3000.0}
        ])
        client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = client
        wrapper._connected = True

        with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]):
            with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
                with patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", return_value=False):
                    report = await wrapper.reconcile_on_startup()

        assert wrapper._global_entry_lock is True
        assert wrapper._global_entry_lock_reason == "PERSISTENCE_FAILED_LOCKED"
        assert wrapper._startup_reconciliation_completed is False
        assert report["status"] == "PERSISTENCE_FAILED_LOCKED"
        assert report["global_entry_lock"] is True
        assert report["startup_secure"] is False

    @pytest.mark.asyncio
    async def test_11_quarantine_persistence_none_triggers_lock(self):
        """11. Si record_exchange_quarantine retourne None (non-booléen strict True), fail-closed immédiat."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])
        client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "ORD-ORPHAN-1", "symbol": "PF_XBTUSD", "orderType": "limit", "size": 0.1, "side": "buy"}
        ])
        wrapper._rest_client = client
        wrapper._connected = True

        with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]):
            with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
                with patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", return_value=None):
                    report = await wrapper.reconcile_on_startup()

        assert wrapper._global_entry_lock is True
        assert wrapper._global_entry_lock_reason == "PERSISTENCE_FAILED_LOCKED"
        assert wrapper._startup_reconciliation_completed is False
        assert report["status"] == "PERSISTENCE_FAILED_LOCKED"

    @pytest.mark.asyncio
    async def test_12_quarantine_persistence_exception_triggers_lock(self):
        """12. Si record_exchange_quarantine lève une exception (ex: SQLite lock), capture et fail-closed."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])
        client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "ORD-ORPHAN-2", "symbol": "PF_SOLUSD", "orderType": "limit", "size": 10.0, "side": "sell"}
        ])
        wrapper._rest_client = client
        wrapper._connected = True

        with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]):
            with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
                with patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", side_effect=sqlite3.OperationalError("database is locked")):
                    report = await wrapper.reconcile_on_startup()

        assert wrapper._global_entry_lock is True
        assert wrapper._global_entry_lock_reason == "PERSISTENCE_FAILED_LOCKED"
        assert wrapper._startup_reconciliation_completed is False
        assert report["status"] == "PERSISTENCE_FAILED_LOCKED"

    @pytest.mark.asyncio
    async def test_13_unknown_order_on_known_symbol_persistence_exception(self):
        """13. Exception de persistance sur ordre inconnu rattaché à un symbole connu: capture et fail-closed."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "size": 0.005, "side": "long"}
        ])
        # Ordre SL normal + un ordre parasite non répertorié
        client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "SL-1", "symbol": "PF_XBTUSD", "orderType": "stop", "size": 0.005, "side": "sell"},
            {"order_id": "ORD-PARASITE-1", "symbol": "PF_XBTUSD", "orderType": "limit", "size": 0.002, "side": "buy"},
        ])
        wrapper._rest_client = client
        wrapper._connected = True

        mock_trade = MagicMock(
            symbol="PF_XBTUSD", order_id="ORD-ORIG", client_oid="BOT_ORIG",
            side="long", qty=0.005,
            get_raw_exchange=lambda: {"protections": {"sl": {"order_id": "SL-1"}}}
        )

        with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]):
            with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[mock_trade]):
                with patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", side_effect=RuntimeError("DB write fail")):
                    report = await wrapper.reconcile_on_startup()

        assert wrapper._global_entry_lock is True
        assert wrapper._global_entry_lock_reason == "PERSISTENCE_FAILED_LOCKED"
        assert wrapper._startup_reconciliation_completed is False
        assert report["status"] == "PERSISTENCE_FAILED_LOCKED"

    @pytest.mark.asyncio
    async def test_14_multiple_orphan_orders_same_symbol_distinct_indexing(self):
        """14. Plusieurs ordres orphelins sur le même symbole: préservation des IDs distincts sans collision."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])
        client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "KRAKEN-ORD-101", "symbol": "PF_XBTUSD", "orderType": "limit", "size": 0.01, "side": "buy", "cliOrdId": "CID-101"},
            {"order_id": "KRAKEN-ORD-102", "symbol": "PF_XBTUSD", "orderType": "stop", "size": 0.01, "side": "sell", "cliOrdId": "CID-102"},
            {"order_id": "KRAKEN-ORD-103", "symbol": "PF_XBTUSD", "orderType": "take_profit", "size": 0.01, "side": "sell", "cliOrdId": "CID-103"},
        ])
        wrapper._rest_client = client
        wrapper._connected = True

        recorded_orders = []
        def mock_record(**kwargs):
            recorded_orders.append(kwargs)
            return True

        with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]):
            with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
                with patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", side_effect=mock_record):
                    report = await wrapper.reconcile_on_startup()

        # 3 appels de persistance distincts avec les vrais IDs Kraken
        assert len(recorded_orders) == 3
        ids_persisted = [r["order_id"] for r in recorded_orders]
        assert "KRAKEN-ORD-101" in ids_persisted
        assert "KRAKEN-ORD-102" in ids_persisted
        assert "KRAKEN-ORD-103" in ids_persisted

        # Indexation distincte dans _active_trackers et _orphan_order_trackers
        assert "KRAKEN-ORD-101" in wrapper._active_trackers
        assert "KRAKEN-ORD-102" in wrapper._active_trackers
        assert "KRAKEN-ORD-103" in wrapper._active_trackers
        assert wrapper._active_trackers["KRAKEN-ORD-101"].client_oid == "CID-101"
        assert wrapper._active_trackers["KRAKEN-ORD-102"].client_oid == "CID-102"
        assert wrapper._active_trackers["KRAKEN-ORD-103"].client_oid == "CID-103"

    @pytest.mark.asyncio
    async def test_15_aggregated_position_and_orders_coexist_distinct_keys(self):
        """15. Position nette + ordres orphelins sur même symbole: coexistence sans collision."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_ETHUSD", "size": 2.5, "side": "long", "price": 3100.0}
        ])
        client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "ORD-ETH-SL", "symbol": "PF_ETHUSD", "orderType": "stop", "size": 2.5, "side": "sell"},
            {"order_id": "ORD-ETH-TP", "symbol": "PF_ETHUSD", "orderType": "take_profit", "size": 2.5, "side": "sell"},
        ])
        wrapper._rest_client = client
        wrapper._connected = True

        recorded = []
        def mock_record(**kwargs):
            recorded.append(kwargs)
            return True

        with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]):
            with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
                with patch("backend.services.live_trades_service.LiveTradesService.record_exchange_quarantine", side_effect=mock_record):
                    report = await wrapper.reconcile_on_startup()

        # 1 position + 2 ordres = 3 quarantines enregistrées
        assert len(recorded) == 3
        pos_records = [r for r in recorded if r["anomaly_type"] == "position"]
        ord_records = [r for r in recorded if r["anomaly_type"] == "order"]

        assert len(pos_records) == 1
        assert pos_records[0]["order_id"] == "POS_PF_ETHUSD"
        assert pos_records[0]["qty"] == 2.5

        assert len(ord_records) == 2
        ord_ids = [r["order_id"] for r in ord_records]
        assert "ORD-ETH-SL" in ord_ids
        assert "ORD-ETH-TP" in ord_ids

        # Coexistence dans les trackers sans écrasement
        assert "POS_PF_ETHUSD" in wrapper._active_trackers
        assert "ORD-ETH-SL" in wrapper._active_trackers
        assert "ORD-ETH-TP" in wrapper._active_trackers
        assert wrapper._active_trackers["POS_PF_ETHUSD"].filled_qty == 2.5
        assert wrapper._active_trackers["ORD-ETH-SL"].expected_exposure == 2.5

    @pytest.mark.asyncio
    async def test_16_reboot_restores_quarantined_and_maintains_fail_closed_lock(self):
        """16. Redémarrage avec anomalies en base: restauration fidèle et verrouillage fail-closed maintenu."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])
        client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = client
        wrapper._connected = True

        q1 = MagicMock(symbol="PF_AVAXUSD", order_id="POS_PF_AVAXUSD", client_oid="BOT_AVAX", side="long", qty=15.0)
        q2 = MagicMock(symbol="PF_XBTUSD", order_id="ORD-EXTRA-PARASITE-999", client_oid="BOT_BTC", side="buy", qty=0.001)

        with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[q1, q2]):
            with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
                report = await wrapper.reconcile_on_startup()

        assert wrapper._global_entry_lock is True
        assert wrapper._global_entry_lock_reason == "UNRESOLVED_ANOMALIES_LOCKED"
        assert wrapper._startup_reconciliation_completed is False
        assert report["startup_secure"] is False
        assert report["status"] == "UNRESOLVED_ANOMALIES_LOCKED"
        assert "PF_AVAXUSD" in wrapper._reconciliation_required_symbols
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        assert "POS_PF_AVAXUSD" in wrapper._active_trackers
        assert "ORD-EXTRA-PARASITE-999" in wrapper._active_trackers
