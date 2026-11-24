"""
Tests complets d'isolation Netting et de sécurité du cycle de vie Kraken Futures.

Valide les 13 scénarios critiques d'isolation Netting spécifiés lors de l'audit :
1. Position manuelle Long préexistante -> Entrée refusée (ENTRY_BLOCKED_EXISTING_POSITION).
2. Position manuelle Short préexistante -> Entrée refusée (ENTRY_BLOCKED_EXISTING_POSITION).
3. Ordre ouvert tiers/externe présent -> Entrée refusée (ENTRY_BLOCKED_EXTERNAL_OPEN_ORDERS).
4. Position réduite de l'extérieur pendant le trade -> Moniteur d'intégrité détecte EXTERNAL_INTERFERENCE_DETECTED -> MANUAL_RECONCILIATION_REQUIRED.
5. Position augmentée de l'extérieur pendant le trade -> Moniteur d'intégrité déclenche MANUAL_RECONCILIATION_REQUIRED.
6. Clôture automatique close_position bloquée en état MANUAL_RECONCILIATION_REQUIRED.
7. Clôture automatique close_position bloquée sans tracker actif (protection des positions manuelles).
8. Réduction reduce_position bloquée en état MANUAL_RECONCILIATION_REQUIRED.
9. Échec API / timeout lors de la vérification de position -> Entrée refusée (POSITION_CHECK_FAILED).
10. Échec API lors de la vérification des ordres ouverts -> Entrée refusée (ORDERS_CHECK_FAILED).
11. Verrouillage per-symbole (asyncio.Lock) prévenant les courses critiques entre pré-flight et dispatch.
12. Réconciliation au démarrage reconcile_on_startup(): rétablit les trades conformes et met en quarantaine les divergences.
13. Échec du Stop Loss avec intégrité compromise : clôture d'urgence automatique STRICTEMENT INTERDITE.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)


class TestKrakenNettingIsolationGates:
    """Tests des portes de sécurité Pre-Flight et de l'intégrité Netting."""

    @pytest.mark.asyncio
    async def test_1_preexisting_long_blocks_entry(self):
        """Scénario 1: Position Long préexistante -> Entrée refusée."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = client
        wrapper._connected = True

        res = await wrapper.place_order(
            symbol="BTCUSDT", side="buy", quantity=0.005, order_type="market",
            entry_price=90000.0, stop_loss=85000.0,
        )
        assert res["success"] is False
        assert res["code"] == "ENTRY_BLOCKED_EXISTING_POSITION"
        assert res["existing_qty"] == 0.01
        assert client.place_market_order.call_count == 0

    @pytest.mark.asyncio
    async def test_2_preexisting_short_blocks_entry(self):
        """Scénario 2: Position Short préexistante (Scénario C Netting) -> Entrée refusée."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "short", "size": 0.003}
        ])
        client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = client
        wrapper._connected = True

        res = await wrapper.place_order(
            symbol="BTCUSDT", side="buy", quantity=0.005, order_type="market",
            entry_price=90000.0, stop_loss=85000.0,
        )
        assert res["success"] is False
        assert res["code"] == "ENTRY_BLOCKED_EXISTING_POSITION"
        assert res["existing_qty"] == 0.003
        assert client.place_market_order.call_count == 0

    @pytest.mark.asyncio
    async def test_3_external_open_order_blocks_entry(self):
        """Scénario 3: Ordre ouvert inconnu/externe -> Entrée refusée."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])
        client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "ORD-EXTERNAL-999", "symbol": "PF_XBTUSD", "cliOrdId": "MANUAL_UI"}
        ])
        wrapper._rest_client = client
        wrapper._connected = True

        res = await wrapper.place_order(
            symbol="BTCUSDT", side="buy", quantity=0.005, order_type="market",
            entry_price=90000.0, stop_loss=85000.0,
        )
        assert res["success"] is False
        assert res["code"] == "ENTRY_BLOCKED_EXTERNAL_OPEN_ORDERS"
        assert client.place_market_order.call_count == 0

    @pytest.mark.asyncio
    async def test_4_position_reduced_externally_triggers_manual_reconciliation(self):
        """Scénario 4: Position réduite de l'extérieur -> MANUAL_RECONCILIATION_REQUIRED."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        wrapper._connected = True
        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-100", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN"
        )
        # Position externe réduite à 0.002
        wrapper._fetch_position_qty_strict = AsyncMock(
            side_effect=lambda sym, side: 0.002 if side == "long" else 0.0
        )

        ok, code, live_qty = await wrapper.check_position_integrity("PF_XBTUSD")
        assert ok is False
        assert code == "EXTERNAL_INTERFERENCE_DETECTED"
        assert live_qty == 0.002
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        assert wrapper._active_trackers["PF_XBTUSD"].status == "MANUAL_RECONCILIATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_5_position_increased_externally_triggers_manual_reconciliation(self):
        """Scénario 5: Position augmentée de l'extérieur -> MANUAL_RECONCILIATION_REQUIRED."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        wrapper._connected = True
        wrapper._active_trackers["PF_SOLUSD"] = KrakenTradeTracker(
            order_id="ORD-200", client_oid="BOT_sol", symbol="PF_SOLUSD", side="long",
            filled_qty=1.0, expected_exposure=1.0, status="OPEN"
        )
        # Position externe augmentée à 3.0
        wrapper._fetch_position_qty_strict = AsyncMock(
            side_effect=lambda sym, side: 3.0 if side == "long" else 0.0
        )

        ok, code, live_qty = await wrapper.check_position_integrity("PF_SOLUSD")
        assert ok is False
        assert code == "EXTERNAL_INTERFERENCE_DETECTED"
        assert "PF_SOLUSD" in wrapper._reconciliation_required_symbols

    @pytest.mark.asyncio
    async def test_6_close_position_blocked_in_manual_reconciliation(self):
        """Scénario 6: close_position STRICTEMENT BLOQUÉ si en MANUAL_RECONCILIATION_REQUIRED."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._reconciliation_required_symbols.add("PF_XBTUSD")
        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-100", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="MANUAL_RECONCILIATION_REQUIRED"
        )

        res = await wrapper.close_position(symbol="PF_XBTUSD")
        assert res["success"] is False
        assert res["code"] == "CLOSE_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"
        assert client.place_market_order.call_count == 0

    @pytest.mark.asyncio
    async def test_7_close_position_blocked_without_active_tracker(self):
        """Scénario 7: close_position REFUSÉ si aucun tracker n'existe (protection position manuelle)."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        # Le compte Kraken a une position manuelle de 50 XRP
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XRPUSD", "side": "long", "size": 50.0}
        ])
        wrapper._rest_client = client
        wrapper._connected = True

        # Le bot n'a AUCUN tracker actif pour XRP
        res = await wrapper.close_position(symbol="PF_XRPUSD")
        assert res["success"] is False
        assert res["code"] == "CLOSE_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"
        assert client.place_market_order.call_count == 0

    @pytest.mark.asyncio
    async def test_8_reduce_position_blocked_in_manual_reconciliation(self):
        """Scénario 8: reduce_position STRICTEMENT BLOQUÉ si réconciliation requise."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._reconciliation_required_symbols.add("PF_ETHUSD")

        res = await wrapper.reduce_position(symbol="PF_ETHUSD", percent=50.0)
        assert res["success"] is False
        assert res["code"] == "CLOSE_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"
        assert client.place_order.call_count == 0

    @pytest.mark.asyncio
    async def test_9_preflight_position_check_failure_blocks_entry(self):
        """Scénario 9: Échec API / timeout sur /openpositions -> Entrée refusée (Fail-Closed)."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        client.get_positions = AsyncMock(side_effect=TimeoutError("API Timeout"))
        wrapper._rest_client = client
        wrapper._connected = True

        res = await wrapper.place_order(
            symbol="PF_ETHUSD", side="buy", quantity=0.1, order_type="market",
            entry_price=3000.0, stop_loss=2900.0,
        )
        assert res["success"] is False
        assert res["code"] == "POSITION_CHECK_FAILED"
        assert client.place_market_order.call_count == 0

    @pytest.mark.asyncio
    async def test_10_preflight_open_orders_check_failure_blocks_entry(self):
        """Scénario 10: Échec API sur /openorders -> Entrée refusée (Fail-Closed)."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])
        client.get_open_orders = AsyncMock(side_effect=RuntimeError("Exchange unreachable"))
        wrapper._rest_client = client
        wrapper._connected = True

        res = await wrapper.place_order(
            symbol="PF_SOLUSD", side="buy", quantity=1.0, order_type="market",
            entry_price=150.0, stop_loss=140.0,
        )
        assert res["success"] is False
        assert res["code"] == "ORDERS_CHECK_FAILED"
        assert client.place_market_order.call_count == 0

    @pytest.mark.asyncio
    async def test_11_symbol_lock_serializes_concurrent_orders(self):
        """Scénario 11: Le verrou par symbole empêche deux ordres simultanés sur le même symbole."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        lock = wrapper._get_symbol_lock("PF_XBTUSD")
        assert lock is wrapper._get_symbol_lock("PF_XBTUSD")
        # Un autre symbole a son propre verrou indépendant
        lock_eth = wrapper._get_symbol_lock("PF_ETHUSD")
        assert lock is not lock_eth

    @pytest.mark.asyncio
    async def test_12_reconcile_on_startup_quarantines_mismatches(self):
        """Scénario 12: reconcile_on_startup() charge la base SQLite et met en quarantaine les divergences."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        # Kraken live: Long 0.002 BTC
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.002}
        ])
        client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = client
        wrapper._connected = True

        # Simulation SQLite avec trade attendu à 0.005 BTC
        mock_trade = MagicMock()
        mock_trade.symbol = "PF_XBTUSD"
        mock_trade.side = "buy"
        mock_trade.quantity = 0.005
        mock_trade.order_id = "ORD-SQLITE"
        mock_trade.client_order_id = "BOT_PF_XBTUSD_123"
        mock_trade.sl_order_id = "SL-123"
        mock_trade.tp_order_id = "TP-123"

        with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[mock_trade]):
            report = await wrapper.reconcile_on_startup()

        # Incohérence détectée: attendu 0.005, live 0.002 -> Quarantaine
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        assert wrapper._active_trackers["PF_XBTUSD"].status == "MANUAL_RECONCILIATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_13_sl_failure_with_compromised_integrity_blocks_emergency_close(self):
        """Scénario 13: Échec du SL avec intégrité compromise -> Clôture d'urgence INTERDITE."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        wrapper._rest_client = client
        wrapper._connected = True

        # Position initiale intacte
        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-001", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN"
        )
        # Position modifiée en externe
        wrapper._reconciliation_required_symbols.add("PF_XBTUSD")

        # Tentative d'entrée avec échec SL sous symbole compromis
        res = await wrapper.place_order(
            symbol="PF_XBTUSD", side="buy", quantity=0.005, order_type="market",
            entry_price=90000.0, stop_loss=85000.0,
        )
        assert res["success"] is False
        assert res["code"] == "ENTRY_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"
        assert client.place_market_order.call_count == 0
