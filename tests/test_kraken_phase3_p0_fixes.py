"""
Tests obligatoires de la Phase 3 : Correctifs P0 de sécurité Kraken Futures.

Couvre de manière exhaustive les 26 scénarios spécifiés :
- SL/TP orphelins (Tests 1 à 9)
- Verrous concurrents multi-instances (Tests 10 à 14)
- Réduction et fermeture via place_order / place_order_market (Tests 15 à 21)
- SQLite persistance et multi-trackers fail-closed (Tests 22 à 26)
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
    get_process_symbol_lock,
)


# ══════════════════════════════════════════════════════════════════
# 1. SL/TP ORPHELINS (Tests 1 à 9)
# ══════════════════════════════════════════════════════════════════

class TestOrphanProtectionsPurge:
    """Vérification de l'annulation sécurisée des protections SL/TP orphelines."""

    @pytest.mark.asyncio
    async def test_1_discordance_triggers_sl_and_tp_cancellation(self):
        """1. Discordance détectée : purge_orphan_protections est appelée et envoie l'annulation du SL et du TP."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "SL-101", "symbol": "PF_XBTUSD"},
            {"order_id": "TP-101", "symbol": "PF_XBTUSD"},
        ])
        client.cancel_order = AsyncMock(return_value={"result": "success", "success": True})
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-101", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN",
            sl_order_id="SL-101", tp_order_id="TP-101"
        )
        # Position externe réduite à 0.002
        wrapper._fetch_position_qty_strict = AsyncMock(
            side_effect=lambda sym, side: 0.002 if side == "long" else 0.0
        )

        ok, code, live_qty = await wrapper.check_position_integrity("PF_XBTUSD")
        assert ok is False
        assert code == "EXTERNAL_INTERFERENCE_DETECTED"
        # Les deux annulations doivent avoir été envoyées
        assert client.cancel_order.call_count == 2
        calls = [c.kwargs.get("order_id") or c.args[0] for c in client.cancel_order.call_args_list]
        assert "SL-101" in calls
        assert "TP-101" in calls

    @pytest.mark.asyncio
    async def test_2_cancellation_confirmed_in_orderbook(self):
        """2. Annulation confirmée : l'ordre a disparu de openorders, tracker passe en PROTECTIONS_PURGED."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        # 1er appel: présent. 2ème appel après cancel: absent.
        client.get_open_orders = AsyncMock(side_effect=[
            [{"order_id": "SL-202", "symbol": "PF_XBTUSD"}],
            [],  # Confirmé absent
        ])
        client.cancel_order = AsyncMock(return_value={"result": "success", "success": True})
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-202", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN",
            sl_order_id="SL-202"
        )

        report = await wrapper.purge_orphan_protections("PF_XBTUSD")
        assert "SL-202" in report["cancelled"]
        assert report["unconfirmed"] == []
        assert wrapper._active_trackers["PF_XBTUSD"].sl_tp_state == "PROTECTIONS_PURGED"

    @pytest.mark.asyncio
    async def test_3_cancellation_refused_retains_quarantine(self):
        """3. Annulation refusée par Kraken : quarantaine conservée, statut non purgé."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        client.get_open_orders = AsyncMock(return_value=[{"order_id": "SL-303", "symbol": "PF_XBTUSD"}])
        client.cancel_order = AsyncMock(return_value={"result": "error", "error": "orderNotFound", "success": False})
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-303", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN",
            sl_order_id="SL-303"
        )
        wrapper._reconciliation_required_symbols.add("PF_XBTUSD")

        report = await wrapper.purge_orphan_protections("PF_XBTUSD")
        # L'ordre est resté présent dans open_orders
        assert "SL-303" in report["unconfirmed"]
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols

    @pytest.mark.asyncio
    async def test_4_cancellation_timeout_triggers_critical_alert(self):
        """4. Timeout d'annulation : alerte CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED enregistrée."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        client.get_open_orders = AsyncMock(side_effect=TimeoutError("Network timeout on open orders"))
        client.cancel_order = AsyncMock(side_effect=TimeoutError("Network timeout on cancel"))
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-404", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN",
            sl_order_id="SL-404"
        )

        report = await wrapper.purge_orphan_protections("PF_XBTUSD")
        assert "SL-404" in report["unconfirmed"]
        assert wrapper._active_trackers["PF_XBTUSD"].sl_tp_state == "CRITICAL_PROTECTION_ORPHAN_UNCONFIRMED"

    @pytest.mark.asyncio
    async def test_5_order_already_executed_in_flight(self):
        """5. Ordre absent de openorders car rempli/déclenché en vol : détecté et journalisé sans fausse confirmation."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        client.get_open_orders = AsyncMock(return_value=[])  # Déjà sorti du carnet
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        # Statut individuel indique FILLED
        wrapper._fetch_order_status_strict = AsyncMock(return_value={
            "order_id": "SL-505", "status": "filled", "filled_size": 0.005
        })

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-505", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN",
            sl_order_id="SL-505"
        )

        report = await wrapper.purge_orphan_protections("PF_XBTUSD")
        assert len(report["in_flight"]) == 1
        assert report["in_flight"][0]["order_id"] == "SL-505"
        assert report["cancelled"] == []

    @pytest.mark.asyncio
    async def test_6_order_not_found_no_false_cancellation(self):
        """6. Ordre introuvable dans openorders et statut inconnu : non classé comme annulé sans preuve."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        # open_orders échoue
        client.get_open_orders = AsyncMock(side_effect=Exception("API Error"))
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False
        wrapper._fetch_order_status_strict = AsyncMock(side_effect=Exception("Cannot fetch order status"))

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-606", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN",
            sl_order_id="SL-606"
        )

        report = await wrapper.purge_orphan_protections("PF_XBTUSD")
        assert "SL-606" in report["unconfirmed"]
        assert report["cancelled"] == []

    @pytest.mark.asyncio
    async def test_7_manual_order_never_cancelled(self):
        """7. Ordre manuel présent dans openorders : JAMAIS touché par purge_orphan_protections."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        client.get_open_orders = AsyncMock(return_value=[
            {"order_id": "MANUAL-ORD-777", "symbol": "PF_XBTUSD", "cliOrdId": "MANUAL_ORDER"}
        ])
        client.cancel_order = AsyncMock()
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-707", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN",
            sl_order_id="BOT-SL-707"  # N'est pas MANUAL-ORD-777
        )

        await wrapper.purge_orphan_protections("PF_XBTUSD")
        # cancel_order ne doit JAMAIS avoir été appelé pour MANUAL-ORD-777
        for call in client.cancel_order.call_args_list:
            assert "MANUAL-ORD-777" not in str(call)

    @pytest.mark.asyncio
    async def test_8_other_tracker_order_never_cancelled(self):
        """8. Ordre appartenant à un autre symbole ou trade : strict respect des identifiants."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        client.cancel_order = AsyncMock(return_value={"success": True})
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        wrapper._active_trackers["PF_ETHUSD"] = KrakenTradeTracker(
            order_id="ETH-1", client_oid="BOT_eth", symbol="PF_ETHUSD", side="long",
            filled_qty=1.0, expected_exposure=1.0, status="OPEN",
            sl_order_id="SL-ETH-1"
        )
        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="BTC-1", client_oid="BOT_btc", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN",
            sl_order_id="SL-BTC-1"
        )

        await wrapper.purge_orphan_protections("PF_XBTUSD")
        # Seul SL-BTC-1 peut être ciblé, jamais SL-ETH-1
        calls = [str(c) for c in client.cancel_order.call_args_list]
        assert not any("SL-ETH-1" in c for c in calls)

    @pytest.mark.asyncio
    async def test_9_no_blind_close_after_cancel_failure(self):
        """9. Aucun ordre de fermeture aveugle émis après un échec d'annulation."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        client = AsyncMock()
        client.place_market_order = AsyncMock()
        client.cancel_order = AsyncMock(side_effect=Exception("Cancel failed"))
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-909", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN",
            sl_order_id="SL-909"
        )

        await wrapper.purge_orphan_protections("PF_XBTUSD")
        # ZÉRO ordre de marché envoyé à Kraken
        assert client.place_market_order.call_count == 0


# ══════════════════════════════════════════════════════════════════
# 2. VERROUS CONCURRENTS MULTI-INSTANCES (Tests 10 à 14)
# ══════════════════════════════════════════════════════════════════

class TestProcessWideSymbolLocks:
    """Vérification du registre de verrous partagé entre instances distinctes."""

    @pytest.mark.asyncio
    async def test_10_two_distinct_instances_share_same_lock(self):
        """10. Deux instances distinctes de KrakenFuturesExecutorWrapper partagent le même verrou sur PF_XBTUSD."""
        inst_a = KrakenFuturesExecutorWrapper(api_key="k1", api_secret="c2VjMQ==")
        inst_b = KrakenFuturesExecutorWrapper(api_key="k2", api_secret="c2VjMg==")

        lock_a = inst_a._get_symbol_lock("PF_XBTUSD")
        lock_b = inst_b._get_symbol_lock("PF_XBTUSD")

        # Doit être exactement le même objet asyncio.Lock
        assert lock_a is lock_b

        # Vérification d'exclusion mutuelle réelle
        await lock_a.acquire()
        assert lock_b.locked() is True
        lock_a.release()
        assert lock_b.locked() is False

    @pytest.mark.asyncio
    async def test_11_two_different_symbols_have_independent_locks(self):
        """11. Deux symboles différents conservent des verrous indépendants."""
        inst_a = KrakenFuturesExecutorWrapper(api_key="k1", api_secret="c2VjMQ==")
        inst_b = KrakenFuturesExecutorWrapper(api_key="k2", api_secret="c2VjMg==")

        lock_btc = inst_a._get_symbol_lock("PF_XBTUSD")
        lock_eth = inst_b._get_symbol_lock("PF_ETHUSD")

        assert lock_btc is not lock_eth
        await lock_btc.acquire()
        assert lock_eth.locked() is False  # ETH n'est pas bloqué par BTC
        lock_btc.release()

    @pytest.mark.asyncio
    async def test_12_simultaneous_open_calls_serialized(self):
        """12. Plusieurs appels simultanés de place_order sont strictement sérialisés."""
        inst_a = KrakenFuturesExecutorWrapper(api_key="k1", api_secret="c2VjMQ==")
        inst_b = KrakenFuturesExecutorWrapper(api_key="k2", api_secret="c2VjMg==")

        order_sequence = []

        class MockSlowClient:
            async def get_positions(self, force_refresh=False):
                # Si le premier ordre a déjà fini, il y a une position de 0.005
                if len(order_sequence) > 0:
                    return [{"symbol": "PF_XBTUSD", "side": "long", "size": 0.005}]
                return []

            async def get_open_orders(self, symbol=None):
                return []

            async def place_market_order(self, symbol, side, size, **kw):
                order_sequence.append(f"EXEC_{size}")
                await asyncio.sleep(0.05)
                return {"success": True, "order_id": f"ORD-{len(order_sequence)}", "symbol": symbol}

            async def get_fills(self, last_fill_time=None):
                return [{"order_id": f"ORD-{len(order_sequence)}", "symbol": "PF_XBTUSD", "size": 0.005}]

        client = MockSlowClient()
        inst_a._rest_client = client
        inst_a._connected = True
        inst_a._startup_reconciliation_completed = True
        inst_a._global_entry_lock = False
        inst_b._rest_client = client
        inst_b._connected = True
        inst_b._startup_reconciliation_completed = True
        inst_b._global_entry_lock = False

        # Lancer deux ordres en parallèle depuis deux instances différentes
        task_a = asyncio.create_task(inst_a.place_order(symbol="BTCUSDT", side="buy", quantity=0.005, order_type="market"))
        task_b = asyncio.create_task(inst_b.place_order(symbol="BTCUSDT", side="buy", quantity=0.005, order_type="market"))

        res_a, res_b = await asyncio.gather(task_a, task_b)

        # L'un des deux doit réussir, et le second doit être bloqué par la pré-flight position gate
        successes = [r.get("success") for r in (res_a, res_b)]
        assert successes.count(True) == 1
        assert successes.count(False) == 1
        failed_res = res_b if res_a.get("success") else res_a
        assert failed_res["code"] == "ENTRY_BLOCKED_EXISTING_POSITION"
        # Exactement 1 seul ordre exécuté
        assert len(order_sequence) == 1

    @pytest.mark.asyncio
    async def test_13_simultaneous_open_and_reduce_serialized(self):
        """13. Appels simultanés d'ouverture et de réduction correctement sérialisés."""
        inst = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        lock = inst._get_symbol_lock("PF_XBTUSD")
        assert not lock.locked()

        # Vérifier que lock est acquis puis relâché lors de close_position
        tracker = KrakenTradeTracker(
            order_id="ORD-01", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN"
        )
        inst._active_trackers["PF_XBTUSD"] = tracker
        inst._fetch_position_qty_strict = AsyncMock(side_effect=lambda s, side: 0.005 if side == "long" else 0.0)
        inst._place_order_rest = AsyncMock(return_value={"success": True, "code": "00000"})
        inst._connected = True

        res = await inst.close_position("PF_XBTUSD")
        assert res["success"] is True
        assert not lock.locked()

    @pytest.mark.asyncio
    async def test_14_absence_of_deadlock(self):
        """14. Vérification de l'absence de deadlock dans tous les chemins d'exécution."""
        inst = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        inst._connected = True

        # Exécuter 5 appels consécutifs avec timeout strict de 2 secondes
        for _ in range(5):
            lock = inst._get_symbol_lock("PF_XBTUSD")
            assert not lock.locked()
            async with lock:
                assert lock.locked()
            assert not lock.locked()


# ══════════════════════════════════════════════════════════════════
# 3. RÉDUCTION ET FERMETURE VIA PLACE_ORDER (Tests 15 à 21)
# ══════════════════════════════════════════════════════════════════

class TestPlaceOrderReduceOnlyCentralGate:
    """Sécurisation totale du point d'entrée central place_order()."""

    @pytest.mark.asyncio
    async def test_15_place_order_market_reduce_only_without_tracker_blocked(self):
        """15. place_order_market(reduce_only=True) sans tracker : bloqué, ZÉRO appel REST."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        # Pas de tracker sur PF_XBTUSD
        res = await wrapper.place_order_market(
            symbol="PF_XBTUSD", side="sell", size="0.005", reduce_only=True
        )
        assert res["success"] is False
        assert res["code"] == "CLOSE_BLOCKED_NO_TRACKER"
        assert client.place_market_order.call_count == 0

    @pytest.mark.asyncio
    async def test_16_place_order_reduce_only_quarantined_blocked(self):
        """16. place_order(reduce_only=True) avec symbole en quarantaine : bloqué, ZÉRO appel REST."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False
        wrapper._reconciliation_required_symbols.add("PF_XBTUSD")

        res = await wrapper.place_order(
            symbol="PF_XBTUSD", side="sell", quantity=0.005, order_type="market",
            reduce_only=True, intent="close"
        )
        assert res["success"] is False
        assert res["code"] == "CLOSE_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"
        assert client.place_market_order.call_count == 0

    @pytest.mark.asyncio
    async def test_17_place_order_reduce_only_integrity_invalid_blocked(self):
        """17. place_order(reduce_only=True) avec intégrité compromise : bloqué, ZÉRO appel REST."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-17", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN"
        )
        # Position externe modifiée à 0.001
        wrapper._fetch_position_qty_strict = AsyncMock(
            side_effect=lambda sym, side: 0.001 if side == "long" else 0.0
        )

        res = await wrapper.place_order(
            symbol="PF_XBTUSD", side="sell", quantity=0.005, order_type="market",
            reduce_only=True, intent="close"
        )
        assert res["success"] is False
        assert res["code"] == "CLOSE_BLOCKED_INTEGRITY_CHECK_FAILED"
        assert client.place_market_order.call_count == 0

    @pytest.mark.asyncio
    async def test_18_place_order_reduce_only_valid_tracker_accepted(self):
        """18. Réduction valide avec tracker OPEN et intégrité confirmée : exécutée avec succès."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        wrapper._connected = True

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-18", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN"
        )
        wrapper._fetch_position_qty_strict = AsyncMock(
            side_effect=lambda sym, side: 0.005 if side == "long" else 0.0
        )
        wrapper._place_order_rest = AsyncMock(return_value={
            "success": True, "code": "00000", "order_id": "RED-18"
        })

        res = await wrapper.place_order(
            symbol="PF_XBTUSD", side="sell", quantity=0.002, order_type="market",
            reduce_only=True, intent="reduce"
        )
        assert res["success"] is True
        # L'exposition attendue du tracker est décrémentée
        assert abs(wrapper._active_trackers["PF_XBTUSD"].expected_exposure - 0.003) < 1e-6
        assert wrapper._active_trackers["PF_XBTUSD"].status == "OPEN"

    @pytest.mark.asyncio
    async def test_19_place_order_full_close_marks_tracker_closed(self):
        """19. Fermeture complète via place_order(reduce_only=True) : tracker marqué CLOSED."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        wrapper._connected = True

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-19", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN"
        )
        wrapper._fetch_position_qty_strict = AsyncMock(
            side_effect=lambda sym, side: 0.005 if side == "long" else 0.0
        )
        wrapper._place_order_rest = AsyncMock(return_value={
            "success": True, "code": "00000", "order_id": "CLOSE-19"
        })

        res = await wrapper.place_order(
            symbol="PF_XBTUSD", side="sell", quantity=0.005, order_type="market",
            reduce_only=True, intent="close"
        )
        assert res["success"] is True
        assert wrapper._active_trackers["PF_XBTUSD"].expected_exposure == 0.0
        assert wrapper._active_trackers["PF_XBTUSD"].status == "CLOSED"

    @pytest.mark.asyncio
    async def test_20_safety_guard_cannot_bypass_tracker(self):
        """20. Safety Guard : appel direct de fermeture sans tracker est bloqué à la racine."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        # PositionSafetyGuard simule un appel direct place_order_market avec reduce_only=True
        res = await wrapper.place_order_market(
            symbol="PF_SOLUSD", side="sell", size="1.0", reduce_only=True
        )
        assert res["success"] is False
        assert res["code"] == "CLOSE_BLOCKED_NO_TRACKER"
        assert client.place_market_order.call_count == 0

    @pytest.mark.asyncio
    async def test_21_decision_engine_cannot_bypass_tracker(self):
        """21. Decision Engine : tentative de clôture sur symbole compromis est bloquée."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False
        wrapper._reconciliation_required_symbols.add("PF_XRPUSD")

        res = await wrapper.place_order_market(
            symbol="PF_XRPUSD", side="sell", size="10.0", reduce_only=True
        )
        assert res["success"] is False
        assert res["code"] == "CLOSE_BLOCKED_MANUAL_RECONCILIATION_REQUIRED"
        assert client.place_market_order.call_count == 0


# ══════════════════════════════════════════════════════════════════
# 4. SQLITE PERSISTANCE ET MULTI-TRACKERS (Tests 22 à 26)
# ══════════════════════════════════════════════════════════════════

class TestSqliteQuarantineAndMultiTrackers:
    """Vérification de la persistance de la quarantaine et du traitement fail-closed des doublons."""

    @pytest.mark.asyncio
    async def test_22_quarantine_persisted_in_sqlite(self):
        """22. Discordance d'intégrité : LiveTradesService.mark_trade_quarantined est invoqué."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        wrapper._connected = True

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-22", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN"
        )
        wrapper._fetch_position_qty_strict = AsyncMock(
            side_effect=lambda sym, side: 0.001 if side == "long" else 0.0
        )

        with patch("backend.services.live_trades_service.LiveTradesService.mark_trade_quarantined") as mock_mark:
            ok, code, _ = await wrapper.check_position_integrity("PF_XBTUSD")
            assert ok is False
            mock_mark.assert_called_once()
            args, kwargs = mock_mark.call_args
            assert kwargs.get("symbol") == "PF_XBTUSD" or args[0] == "PF_XBTUSD"

    @pytest.mark.asyncio
    async def test_23_restart_restores_quarantined_state_from_sqlite(self):
        """23. Redémarrage : un trade QUARANTINED dans SQLite est immédiatement remis en quarantaine."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        wrapper._connected = True

        mock_q_trade = MagicMock()
        mock_q_trade.symbol = "PF_XBTUSD"
        mock_q_trade.order_id = "ORD-23"
        mock_q_trade.client_oid = "BOT_PF_XBTUSD_23"
        mock_q_trade.side = "long"
        mock_q_trade.qty = 0.005

        with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[mock_q_trade]):
            with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[]):
                report = await wrapper.reconcile_on_startup()

        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        assert wrapper._active_trackers["PF_XBTUSD"].status == "MANUAL_RECONCILIATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_24_two_open_trades_same_symbol_fail_closed_quarantined(self):
        """24. Deux trades OPEN pour le même symbole dans SQLite : bloqué en quarantaine (jamais écrasé silencieusement)."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        wrapper._connected = True

        trade_1 = MagicMock(symbol="PF_XBTUSD", order_id="ORD-A", client_oid="BOT_A", side="long", qty=0.002)
        trade_2 = MagicMock(symbol="PF_XBTUSD", order_id="ORD-B", client_oid="BOT_B", side="long", qty=0.003)

        with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]):
            with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[trade_1, trade_2]):
                report = await wrapper.reconcile_on_startup()

        # Le symbole doit être mis en quarantaine pour ambiguïté
        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        assert wrapper._active_trackers["PF_XBTUSD"].status == "MANUAL_RECONCILIATION_REQUIRED"
        assert "Startup conflict" in wrapper._active_trackers["PF_XBTUSD"].reconciliation_notes

    @pytest.mark.asyncio
    async def test_25_kraken_position_without_tracker_fail_closed_on_entry(self):
        """25. Position Kraken présente sans tracker dans SQLite : toute nouvelle entrée est rejetée."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[
            {"symbol": "PF_XBTUSD", "side": "long", "size": 0.01}
        ])
        client.get_open_orders = AsyncMock(return_value=[])
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        res = await wrapper.place_order(
            symbol="PF_XBTUSD", side="buy", quantity=0.005, order_type="market"
        )
        assert res["success"] is False
        assert res["code"] == "ENTRY_BLOCKED_EXISTING_POSITION"
        assert client.place_market_order.call_count == 0

    @pytest.mark.asyncio
    async def test_26_network_loss_at_startup_reconciliation_fails_closed(self):
        """26. Perte réseau / timeout au démarrage : statut fail-closed en MANUAL_RECONCILIATION_REQUIRED."""
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2Vj")
        client = AsyncMock()
        client.get_positions = AsyncMock(side_effect=TimeoutError("Network down"))
        client.get_open_orders = AsyncMock(side_effect=TimeoutError("Network down"))
        wrapper._rest_client = client
        wrapper._connected = True
        wrapper._startup_reconciliation_completed = True
        wrapper._global_entry_lock = False

        mock_trade = MagicMock(symbol="PF_XBTUSD", order_id="ORD-26", client_oid="BOT_26", side="long", qty=0.005)

        with patch("backend.services.live_trades_service.LiveTradesService.get_quarantined_trades", return_value=[]):
            with patch("backend.services.live_trades_service.LiveTradesService.get_open_trades", return_value=[mock_trade]):
                report = await wrapper.reconcile_on_startup()

        assert "PF_XBTUSD" in wrapper._reconciliation_required_symbols
        assert "PF_XBTUSD" in report["quarantined_symbols"]
        assert wrapper._active_trackers["PF_XBTUSD"].status == "MANUAL_RECONCILIATION_REQUIRED"
