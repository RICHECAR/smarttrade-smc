"""
Tests unitaires stricts P0 — Identité et Timestamp du Cycle de Position Kraken Futures
Validation exhaustive de la corrélation stricte du cycle et de la normalisation UTC des timestamps.

Scénarios obligatoires :
1. Fill de clôture entre l'entrée réelle et l'appel de résolution (doit être reconnu GENUINELY_CLOSED).
2. Ancien fill historique (antérieur à l'entrée, doit être rejeté -> INDETERMINATE).
3. Vente manuelle sur le même symbole (non corrélée au cycle, doit être rejetée -> INDETERMINATE).
4. Clôture provenant d'un autre cycle (identifiant d'un autre cycle, doit être rejetée -> INDETERMINATE).
5. Timestamp ISO avec fuseau UTC (valide, normalisé en UTC epoch float).
6. Timestamp ISO sans fuseau (naïf, rejeté comme ambigu -> INDETERMINATE).
7. Timestamp manquant (None ou vide -> INDETERMINATE).
8. Clôture partielle (ne doit JAMAIS retourner GENUINELY_CLOSED).
9. Clôture complète avec identifiants corrélés (validée GENUINELY_CLOSED).
10. API fills indisponible (exception réseau ou parsing -> fail-closed INDETERMINATE).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 : Fill de clôture entre l'entrée réelle et l'appel de résolution
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_1_fill_de_cloture_entre_entree_reelle_et_appel_resolution():
    """
    Entrée réelle confirmée à T1 (12:00:00.000Z).
    Clôture exchange survenue à T2 (12:00:00.400Z).
    L'appel de résolution a lieu à T3 (12:00:01.000Z).
    Le fill de clôture à T2 est bien >= T1 et corrélé au cycle : il doit être reconnu GENUINELY_CLOSED.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.0)

    # Fill de clôture corrélé survenu entre T1 et T3
    wrapper._fetch_fills_strict = AsyncMock(return_value=[
        {
            "fill_id": "FILL-SL-100",
            "order_id": "ORD-SL-100",
            "cliOrdId": "BOT_SL_PF_XBTUSD_ORDENTRY100_abc",
            "symbol": "PF_XBTUSD",
            "side": "sell",
            "size": 1.0,
            "price": 50100.0,
            "fillTime": "2026-11-17T12:00:00.400Z",
        }
    ])

    state, qty, reason = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        entry_order_id="ORD-ENTRY-100",
        entry_client_oid="BOT_PF_XBTUSD_ENTRY_100",
        entry_timestamp="2026-11-17T12:00:00.000Z",  # T1 réel du fill d'entrée
        max_attempts=2,
        delay_sec=0.01,
    )

    assert state == "GENUINELY_CLOSED"
    assert qty == 0.0
    assert "Correlated closing fills confirmed for cycle" in reason


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 : Ancien fill historique
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_2_ancien_fill_historique():
    """Un fill SELL antérieur au timestamp du fill d'entrée doit être ignoré."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.0)

    # Fill SELL survenu à 10:00:00Z alors que l'entrée est à 12:00:00Z
    wrapper._fetch_fills_strict = AsyncMock(return_value=[
        {
            "order_id": "ORD-SL-OLD",
            "cliOrdId": "BOT_SL_PF_XBTUSD_ORDENTRY100_old",
            "symbol": "PF_XBTUSD",
            "side": "sell",
            "size": 1.0,
            "price": 49000.0,
            "fillTime": "2026-11-17T10:00:00.000Z",
        }
    ])

    state, qty, reason = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        entry_order_id="ORD-ENTRY-100",
        entry_timestamp="2026-11-17T12:00:00.000Z",
        max_attempts=2,
        delay_sec=0.01,
    )

    assert state == "INDETERMINATE"
    assert qty == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 : Vente manuelle sur le même symbole
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_3_vente_manuelle_meme_symbole():
    """
    Un ordre de vente manuel sur le compte (sans identifiant du bot ni client_oid corrélé)
    ne doit JAMAIS être attribué au cycle du bot.
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.0)

    # Fill opposé récent, mais d'origine manuelle web UI (pas de client_oid bot)
    wrapper._fetch_fills_strict = AsyncMock(return_value=[
        {
            "order_id": "ORD-MANUAL-SELL-888",
            "cliOrdId": None,
            "symbol": "PF_XBTUSD",
            "side": "sell",
            "size": 1.0,
            "price": 50200.0,
            "fillTime": "2026-11-17T12:00:05.000Z",
        }
    ])

    state, qty, reason = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        entry_order_id="ORD-ENTRY-100",
        entry_client_oid="BOT_PF_XBTUSD_ENTRY_100",
        entry_timestamp="2026-11-17T12:00:00.000Z",
        max_attempts=2,
        delay_sec=0.01,
    )

    # Rejeté car non corrélé au cycle -> INDETERMINATE fail-closed
    assert state == "INDETERMINATE"
    assert qty == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 : Clôture provenant d'un autre cycle
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_4_cloture_provenant_autre_cycle():
    """
    Un fill de clôture portant la signature d'un AUTRE cycle (Cycle 1) ne doit pas
    provoquer la fermeture du cycle actuel (Cycle 2).
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.0)

    # Fill portant le token du cycle 1 : ORDENTRY001
    wrapper._fetch_fills_strict = AsyncMock(return_value=[
        {
            "order_id": "ORD-SL-CYCLE-1",
            "cliOrdId": "BOT_SL_PF_XBTUSD_ORDENTRY001_xyz",
            "symbol": "PF_XBTUSD",
            "side": "sell",
            "size": 1.0,
            "price": 50100.0,
            "fillTime": "2026-11-17T12:00:05.000Z",
        }
    ])

    # Cycle actuel : ORD-ENTRY-200
    state, qty, reason = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        entry_order_id="ORD-ENTRY-200",
        entry_client_oid="BOT_PF_XBTUSD_ENTRY_200",
        entry_timestamp="2026-11-17T12:00:00.000Z",
        max_attempts=2,
        delay_sec=0.01,
    )

    # Rejeté car appartient au cycle 1, pas au cycle 2
    assert state == "INDETERMINATE"
    assert qty == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 : Timestamp ISO avec fuseau UTC
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_5_timestamp_iso_avec_fuseau_utc():
    """Un timestamp ISO explicite avec suffixe 'Z' ou décalage '+00:00' est valide et normalisé."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")

    ts1 = wrapper._parse_fill_timestamp("2026-11-17T12:00:00.000Z")
    ts2 = wrapper._parse_fill_timestamp("2026-11-17T12:00:00+00:00")

    assert ts1 is not None
    assert ts2 is not None
    assert ts1 == ts2

    # Validation en résolution
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.0)
    wrapper._fetch_fills_strict = AsyncMock(return_value=[
        {
            "fill_id": "FILL-SL-UTC",
            "order_id": "ORD-SL-UTC",
            "cliOrdId": "BOT_SL_PF_XBTUSD_ORDENTRY100_utc",
            "symbol": "PF_XBTUSD",
            "side": "sell",
            "size": 1.0,
            "price": 50100.0,
            "fillTime": "2026-11-17T12:00:01.000Z",
        }
    ])

    state, qty, reason = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        entry_order_id="ORD-ENTRY-100",
        entry_timestamp="2026-11-17T12:00:00.000Z",
        max_attempts=2,
        delay_sec=0.01,
    )
    assert state == "GENUINELY_CLOSED"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6 : Timestamp ISO sans fuseau (Naïf)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_6_timestamp_iso_sans_fuseau():
    """
    Un timestamp ISO naïf (sans 'Z' ni offset) DOIT être rejeté comme ambigu.
    Il ne doit JAMAIS être interprété silencieusement comme UTC.
    Résultat : INDETERMINATE (Fail-closed).
    """
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")

    # Le parseur doit explicitement retourner None pour un format naïf
    assert wrapper._parse_fill_timestamp("2026-11-17T12:00:00.000") is None
    assert wrapper._parse_fill_timestamp("2026-11-17 12:00:00") is None

    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.0)
    wrapper._fetch_fills_strict = AsyncMock(return_value=[
        {
            "order_id": "ORD-SL-NAIVE",
            "cliOrdId": "BOT_SL_PF_XBTUSD_ORDENTRY100_naive",
            "symbol": "PF_XBTUSD",
            "side": "sell",
            "size": 1.0,
            "price": 50100.0,
            "fillTime": "2026-11-17T12:00:01.000",  # SANS FUSEAU !
        }
    ])

    state, qty, reason = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        entry_order_id="ORD-ENTRY-100",
        entry_timestamp="2026-11-17T12:00:00.000",  # Naïf également
        max_attempts=2,
        delay_sec=0.01,
    )

    # Rejet strict car timestamp ambigu -> fail-closed
    assert state == "INDETERMINATE"
    assert qty == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# TEST 7 : Timestamp manquant
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_7_timestamp_manquant():
    """Si le timestamp d'entrée ou de clôture est absent (None ou vide) -> INDETERMINATE."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")

    assert wrapper._parse_fill_timestamp(None) is None
    assert wrapper._parse_fill_timestamp("") is None

    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.0)
    wrapper._fetch_fills_strict = AsyncMock(return_value=[
        {
            "order_id": "ORD-SL-NOTIME",
            "cliOrdId": "BOT_SL_PF_XBTUSD_ORDENTRY100_notime",
            "symbol": "PF_XBTUSD",
            "side": "sell",
            "size": 1.0,
            "price": 50100.0,
            "fillTime": None,  # MANQUANT !
        }
    ])

    state, qty, reason = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        entry_order_id="ORD-ENTRY-100",
        entry_timestamp=None,  # MANQUANT !
        max_attempts=2,
        delay_sec=0.01,
    )

    assert state == "INDETERMINATE"
    assert qty == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# TEST 8 : Clôture partielle
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_8_cloture_partielle():
    """Une clôture partielle (0.4 / 1.0) ne doit JAMAIS retourner GENUINELY_CLOSED."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")

    # Cas 8.1 : Reste 0.6 ouvert et visible sur l'exchange -> CONFIRMED avec 0.6
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.6)
    wrapper._fetch_fills_strict = AsyncMock(return_value=[
        {
            "order_id": "ORD-SL-PART",
            "cliOrdId": "BOT_SL_PF_XBTUSD_ORDENTRY100_part",
            "symbol": "PF_XBTUSD",
            "side": "sell",
            "size": 0.4,
            "price": 50100.0,
            "fillTime": "2026-11-17T12:00:01.000Z",
        }
    ])

    state, qty, reason = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        entry_order_id="ORD-ENTRY-100",
        entry_timestamp="2026-11-17T12:00:00.000Z",
        max_attempts=2,
        delay_sec=0.01,
    )
    assert state == "CONFIRMED"
    assert qty == 0.6

    # Cas 8.2 : Net pos à 0 mais fills ne couvrent que 0.4 -> INDETERMINATE
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.0)
    state_b, qty_b, reason_b = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        entry_order_id="ORD-ENTRY-100",
        entry_timestamp="2026-11-17T12:00:00.000Z",
        max_attempts=2,
        delay_sec=0.01,
    )
    assert state_b == "INDETERMINATE"
    assert qty_b == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# TEST 9 : Clôture complète avec identifiants corrélés
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_9_cloture_complete_avec_identifiants_correles():
    """Clôture totale avec order_id/client_oid corrélé, taille totale et timestamp valide -> GENUINELY_CLOSED."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.0)

    wrapper._fetch_fills_strict = AsyncMock(return_value=[
        {
            "fill_id": "FILL-SL-EXACT",
            "order_id": "ORD-SL-EXACT",
            "cliOrdId": "BOT_SL_PF_XBTUSD_ORDENTRY100_full",
            "symbol": "PF_XBTUSD",
            "side": "sell",
            "size": 1.0,
            "price": 50150.0,
            "fillTime": "2026-11-17T12:00:01.200Z",
        }
    ])

    state, qty, reason = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        entry_order_id="ORD-ENTRY-100",
        entry_client_oid="BOT_PF_XBTUSD_ENTRY_100",
        entry_timestamp="2026-11-17T12:00:00.000Z",
        correlated_client_oids={"BOT_SL_PF_XBTUSD_ORDENTRY100_full"},
        max_attempts=2,
        delay_sec=0.01,
    )

    assert state == "GENUINELY_CLOSED"
    assert qty == 0.0
    assert "Correlated closing fills confirmed for cycle" in reason


# ─────────────────────────────────────────────────────────────────────────────
# TEST 10 : API fills indisponible
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_10_api_fills_indisponible():
    """En cas d'exception réseau ou d'indisponibilité de l'API /fills -> Fail-closed INDETERMINATE."""
    wrapper = KrakenFuturesExecutorWrapper(api_key="mock", api_secret="mock")
    wrapper._fetch_position_qty_strict = AsyncMock(return_value=0.0)

    # API /fills inaccessible
    wrapper._fetch_fills_strict = AsyncMock(side_effect=RuntimeError("Kraken API 502 Bad Gateway"))

    state, qty, reason = await wrapper._resolve_live_position_propagation(
        kraken_symbol="PF_XBTUSD",
        position_side="long",
        expected_qty=1.0,
        entry_order_id="ORD-ENTRY-100",
        entry_timestamp="2026-11-17T12:00:00.000Z",
        max_attempts=2,
        delay_sec=0.01,
    )

    assert state == "INDETERMINATE"
    assert qty == 0.0
    assert "without valid correlated closing fill" in reason
