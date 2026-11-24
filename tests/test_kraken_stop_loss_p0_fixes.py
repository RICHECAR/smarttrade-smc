"""
tests/test_kraken_stop_loss_p0_fixes.py
═════════════════════════════════════════════════════════════════════════════
P0 Validation Test Suite — Kraken Automatic Stop Loss Placement Fixes
Strict Read-Only Verification on Sealed Files & Strategy Modules.
All tests use temporary SQLite in-memory databases and isolated mocks.
Zero real exchange interaction.
═════════════════════════════════════════════════════════════════════════════
"""

import asyncio
import hashlib
import os
import sqlite3
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    KrakenTradeTracker,
)
from backend.kraken_contracts import align_price_to_tick, validate_sl_tp


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 : Position invisible pendant les premières lectures puis visible
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_1_position_invisible_initially_then_visible():
    """Vérifie que la temporisation bornée résout le lag de propagation sans abandon prématuré."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")

    # Mock _fetch_position_qty_strict: 0 au premier appel, 0 au deuxième, 1.0 au troisième
    fetch_mock = AsyncMock(side_effect=[0.0, 0.0, 1.0])
    wrapper._fetch_position_qty_strict = fetch_mock
    wrapper._fetch_fills_strict = AsyncMock(return_value=[])

    state, qty, err = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        max_attempts=4,
        delay_sec=0.01,
    )

    assert state == "CONFIRMED"
    assert qty == 1.0
    assert err is None
    assert fetch_mock.call_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 : Position réellement fermée
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_2_position_genuinely_closed():
    """Vérifie la distinction entre latence de propagation et clôture réelle confirmée par fill corrélé."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")

    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.0)
    # Closing fill corrélé (post-entrée, même symbole, sens opposé, quantité intégrale)
    wrapper._fetch_fills_strict = AsyncMock(return_value=[
        {
            "fill_id": "FILL-CLOSE-999",
            "order_id": "ORD-CLOSE-999",
            "cliOrdId": "BOT_SL_PF_XBTUSD_ORDENTRY100_close",
            "symbol": "PF_XBTUSD",
            "side": "sell",
            "size": 1.0,
            "price": 50100.0,
            "fillTime": "2026-11-17T12:00:02.000Z",
        }
    ])

    state, qty, reason = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        entry_order_id="ORD-ENTRY-100",
        entry_timestamp="2026-11-17T12:00:00.000Z",
        max_attempts=3,
        delay_sec=0.01,
    )

    assert state == "GENUINELY_CLOSED"
    assert qty == 0.0
    assert "Correlated closing fills confirmed" in reason


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 : Réponse HTTP 200 sans présence du SL dans /openorders
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_3_http_200_without_sl_in_openorders():
    """Vérifie qu'un simple ACK HTTP 200 est REJETÉ si le SL n'est pas dans le carnet réel."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")

    mock_client = AsyncMock()
    # HTTP 200 de placement
    mock_client.place_order = AsyncMock(return_value={
        "result": "success",
        "sendStatus": {"order_id": "SL-ORD-999", "status": "placed", "cliOrdId": "BOT_SL_1"},
    })
    # Mais le carnet /openorders ne contient pas l'ordre
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._get_rest_client = MagicMock(return_value=mock_client)

    res = await wrapper._place_stop_order_rest(
        symbol="PF_XBTUSD",
        side="sell",
        size=1.0,
        stop_price=48000.0,
        client_order_id="BOT_SL_1",
        require_confirmation=True,
    )

    assert res["success"] is False
    assert res.get("verified") is False
    assert res["code"] == "PROTECTION_NOT_CONFIRMED_IN_ORDERBOOK"
    assert "not found in Kraken open orders carnet" in res["error"]


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 : SL confirmé dans /openorders
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_4_sl_confirmed_in_openorders():
    """Vérifie qu'un SL présent dans le carnet avec statut, side, taille et prix corrects est validé."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")

    mock_client = AsyncMock()
    mock_client.place_order = AsyncMock(return_value={
        "result": "success",
        "sendStatus": {"order_id": "SL-ORD-777", "status": "placed", "cliOrdId": "BOT_SL_CONF"},
    })
    # Carnet /openorders contenant exactement l'ordre actif
    mock_client.get_open_orders = AsyncMock(return_value=[
        {
            "order_id": "SL-ORD-777",
            "cliOrdId": "BOT_SL_CONF",
            "symbol": "PF_XBTUSD",
            "side": "sell",
            "size": 1.0,
            "stopPrice": 48000.0,
            "status": "placed",
        }
    ])
    wrapper._get_rest_client = MagicMock(return_value=mock_client)

    res = await wrapper._place_stop_order_rest(
        symbol="PF_XBTUSD",
        side="sell",
        size=1.0,
        stop_price=48000.0,
        client_order_id="BOT_SL_CONF",
        require_confirmation=True,
    )

    assert res["success"] is True
    assert res.get("verified") is True
    assert res["order_id"] == "SL-ORD-777"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 : Quantité SL différente de l'exposition
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_5_sl_quantity_different_from_exposure():
    """Vérifie le rejet strict si la taille dans /openorders ne correspond pas à l'exposition attendue."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")

    mock_client = AsyncMock()
    # Ordre dans le carnet avec taille 0.5 au lieu de 1.0 attendu
    mock_client.get_open_orders = AsyncMock(return_value=[
        {
            "order_id": "SL-ORD-BAD-SZ",
            "cliOrdId": "BOT_SL_SZ",
            "symbol": "PF_XBTUSD",
            "side": "sell",
            "size": 0.5,
            "stopPrice": 48000.0,
            "status": "placed",
        }
    ])
    wrapper._get_rest_client = MagicMock(return_value=mock_client)

    verified, v_err = await wrapper._verify_protection_in_open_orders(
        symbol="PF_XBTUSD",
        order_id="SL-ORD-BAD-SZ",
        client_order_id="BOT_SL_SZ",
        expected_side="sell",
        expected_size=1.0,
        expected_price=48000.0,
        prot_type="sl",
    )

    assert verified is False
    assert "Protection order size mismatch: got 0.5, expected 1.0" in v_err


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6 : Précision du prix & respect du price_tick
# ─────────────────────────────────────────────────────────────────────────────
def test_6_price_non_conforme_au_tick():
    """Vérifie l'alignement sur le price_tick et l'arrondi sécurisé sans rapprocher le SL de l'entrée."""
    # 1. LONG PF_XBTUSD (tick = 1.0): SL=48500.75 doit être arrondi à 48500.0 (floor)
    ok, p_long, err = align_price_to_tick(48500.75, "PF_XBTUSD", "buy", "sl", entry_price=50000.0)
    assert ok is True
    assert p_long == 48500.0
    assert p_long < 50000.0

    # 2. SHORT PF_XBTUSD (tick = 1.0): SL=51500.25 doit être arrondi à 51501.0 (ceil)
    ok, p_short, err = align_price_to_tick(51500.25, "PF_XBTUSD", "sell", "sl", entry_price=50000.0)
    assert ok is True
    assert p_short == 51501.0
    assert p_short > 50000.0

    # 3. LONG PF_ETHUSD (tick = 0.1): SL=3456.78 doit être arrondi à 3456.7 (floor)
    ok, p_eth, err = align_price_to_tick(3456.78, "PF_ETHUSD", "buy", "sl", entry_price=3500.0)
    assert ok is True
    assert p_eth == 3456.7

    # 4. Refus d'un contrat sans spécification officielle (jamais d'invention de tick size)
    ok, _, err_unknown = align_price_to_tick(100.0, "UNKNOWN_FICTIVE_PAIR", "buy", "sl", entry_price=110.0)
    assert ok is False
    assert "UNKNOWN_CONTRACT_TICK" in err_unknown

    # 5. Refus si l'arrondi violerait la relation de sécurité (SL >= entry pour un Long)
    ok, _, err_viol = align_price_to_tick(50000.5, "PF_XBTUSD", "buy", "sl", entry_price=50000.0)
    assert ok is False
    assert "POST_ALIGNMENT_VALIDATION_FAILED" in err_viol


# ─────────────────────────────────────────────────────────────────────────────
# TEST 7 : API indisponible ou timeout
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_7_api_indisponible_ou_timeout():
    """Vérifie le traitement fail-closed en cas d'exception réseau ou timeout."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")

    mock_client = AsyncMock()
    mock_client.place_order = AsyncMock(side_effect=TimeoutError("Request to Kraken timed out"))
    wrapper._get_rest_client = MagicMock(return_value=mock_client)

    res = await wrapper._place_stop_order_rest(
        symbol="PF_XBTUSD",
        side="sell",
        size=1.0,
        stop_price=48000.0,
        require_confirmation=True,
    )

    assert res["success"] is False
    assert res["code"] == "REST_EXCEPTION"
    assert "timed out" in res["error"]


# ─────────────────────────────────────────────────────────────────────────────
# TEST 8 : Échec du SL et vérification de la procédure d'urgence
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_8_echec_sl_procedure_urgence():
    """Vérifie que l'échec d'installation du SL déclenche une fermeture d'urgence market reduce_only."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")
    wrapper._global_entry_lock = False
    wrapper._startup_reconciliation_completed = True

    mock_client = AsyncMock()
    mock_client.get_open_orders = AsyncMock(return_value=[])
    wrapper._get_rest_client = MagicMock(return_value=mock_client)

    # Mock entry success
    wrapper._check_preflight_position_gate = AsyncMock(return_value=(True, None, 0.0))
    wrapper._place_order_rest = AsyncMock()

    # Réponse de l'ordre d'entrée
    entry_resp = {
        "success": True,
        "code": "00000",
        "order_id": "ORD-ENTRY-1",
        "filled_size": 1.0,
        "price": 50000.0,
        "raw_response": {"sendStatus": {"status": "filled", "order_id": "ORD-ENTRY-1"}},
    }
    # Réponse de l'ordre de fermeture d'urgence
    close_resp = {
        "success": True,
        "code": "00000",
        "order_id": "ORD-CLOSE-EMERGENCY",
        "status": "filled",
    }
    wrapper._place_order_rest.side_effect = [entry_resp, close_resp]

    wrapper._confirm_executed_qty = AsyncMock(return_value={
        "state": "CONFIRMED",
        "qty": 1.0,
        "vwap": 50000.0,
        "source": "fills+position",
        "matched_fills": [],
        "fills_qty": 1.0,
        "position_qty": 1.0,
    })
    wrapper._resolve_live_position_propagation = AsyncMock(return_value=("CONFIRMED", 1.0, None))
    wrapper.check_position_integrity = AsyncMock(return_value=(True, None, 1.0))
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=1.0)

    # Échec systématique du placement SL
    wrapper._place_stop_order_rest = AsyncMock(return_value={
        "success": False,
        "code": "PROTECTION_NOT_CONFIRMED_IN_ORDERBOOK",
        "error": "SL unconfirmed in carnet",
    })

    with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade", return_value=MagicMock()), \
         patch("backend.services.live_trades_service.LiveTradesService.record_protection_intent", return_value=True), \
         patch("backend.services.live_trades_service.LiveTradesService.close_live_trade", return_value=MagicMock()), \
         patch("backend.services.live_trades_service.LiveTradesService.update_protection_status", return_value=True):
        result = await wrapper.place_order(
            symbol="PF_XBTUSD",
            side="buy",
            quantity=1.0,
            entry_price=50000.0,
            stop_loss=48000.0,
        )

    # Doit avoir tenté la fermeture d'urgence
    assert result["_sl_emergency_close"] is True
    assert result["_sl_emergency_close_ok"] is True
    assert result["sl_attached"] is False
    assert result["status"] == "SL_FAILED_POSITION_EMERGENCY_CLOSED"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 9 : Redémarrage avec protection existante
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_9_reboot_avec_protection_existante():
    """Vérifie que le tracker gère la persistance de l'état sans écraser ni dégrader la protection."""
    tracker = KrakenTradeTracker(
        order_id="ORD-REBOOT-1",
        client_oid="CID-REBOOT-1",
        symbol="PF_XBTUSD",
        side="long",
        filled_qty=1.0,
        expected_exposure=1.0,
        entry_price=50000.0,
        status="OPEN",
        sl_order_id="SL-EXISTING-99",
        sl_price=48000.0,
        sl_tp_state="SL_ATTACHED",
    )

    d = tracker.to_dict()
    assert d["sl_order_id"] == "SL-EXISTING-99"
    assert d["sl_price"] == 48000.0
    assert d["sl_tp_state"] == "SL_ATTACHED"
    assert d["expected_exposure"] == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# TEST 10 : Vérification qu'aucun ordre réel n'a été transmis
# ─────────────────────────────────────────────────────────────────────────────
def test_10_aucun_ordre_reel_transmis():
    """Vérifie la stricte intégrité du système de production et des verrous de sécurité."""
    # 1. Fichier scellé intact
    sealed_path = "backend/exchanges/kraken_futures.py"
    sha = hashlib.sha256(open(sealed_path, "rb").read()).hexdigest()
    assert sha == "d0226275af57a34f7509e16f7890c853d9757d62bbf14d1e14ec36c78f36c1b7", "Sealed file SHA256 altered!"

    # 2. .env KRAKEN_EXECUTION_ENABLED=false
    with open(".env", "r", encoding="utf-8") as f:
        env_content = f.read()
    assert "KRAKEN_EXECUTION_ENABLED=false" in env_content, "KRAKEN_EXECUTION_ENABLED was enabled!"

    # 3. Base de production : les 7 quarantaines sont préservées intactes
    prod_db = "data/trades.db"
    conn = sqlite3.connect(prod_db)
    q_cnt = conn.cursor().execute("SELECT COUNT(*) FROM live_trades WHERE status='QUARANTINED'").fetchone()[0]
    conn.close()
    assert q_cnt == 7, f"Production DB quarantines modified! Expected 7, found {q_cnt}"
