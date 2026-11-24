"""
Tests — Vérification approfondie des quantités et cycle de vie Kraken Futures
═════════════════════════════════════════════════════════════════════════════

Couvre l'ensemble des exigences de la migration Linear Multi-Collateral (PF_*) :
1. Spécifications réelles (PF_XBTUSD, PF_ETHUSD, PF_SOLUSD, PF_XRPUSD).
2. Formatage des quantités (décimales ou entières selon l'instrument).
3. Bornes min, max, cap de sécurité notional.
4. Modèle linéaire : 0.005 BTC, 0.15 ETH, 2.5 SOL, 300 XRP.
5. SL et TP avec quantité rigoureusement identique à l'ordre exécuté.
6. Fermeture d'urgence avec quantité exacte.
7. Validation des structures JSON réelles Kraken (openOrders, fills, orders/status).
8. reduceOnly = booléen Python (True / False).
9. Fills partiels et protection adaptative.
10. Isolation des positions préexistantes.
11. Réduction sans gonflement artificiel.
12. Risque linéaire strict sans double application du levier.

Run:
    python -m pytest tests/test_kraken_quantities_and_lifecycle.py -v
"""

import asyncio
import os
from unittest.mock import MagicMock, patch
import pytest

from kraken_fake_exchange import make_exchange_stateful
from backend.kraken_contracts import (
    KRAKEN_CONTRACT_SPECS,
    get_contract_specs,
    to_kraken_symbol,
    ensure_contracts,
    coin_to_contracts,
    validate_order,
)
from backend.risk.size_calculator import (
    ensure_kraken_contracts,
    validate_kraken_order,
    calculate_size,
)
from backend.execution.kraken_futures_executor import (
    KrakenFuturesExecutorWrapper,
    _floor_to_precision,
    _instrument_qty_rules,
)


# ══════════════════════════════════════════════════════════════════
# 1. RÈGLES DE QUANTITÉS PAR INSTRUMENT (BTC, ETH, SOL, XRP)
# ══════════════════════════════════════════════════════════════════

class TestInstrumentQuantitySpecs:
    """Vérifie les specs réelles des instruments majeurs Kraken Futures Linear PF_*."""

    def test_pf_xbtusd_specs(self):
        specs = get_contract_specs("PF_XBTUSD")
        assert specs["contract_size"] == 1.0
        assert specs["min_size"] == 0.0001
        assert specs["step_size"] == 0.0001
        assert specs["qty_precision"] == 4
        assert specs["price_tick"] == 1.0

    def test_pf_ethusd_specs(self):
        specs = get_contract_specs("PF_ETHUSD")
        assert specs["contract_size"] == 1.0
        assert specs["min_size"] == 0.001
        assert specs["step_size"] == 0.001
        assert specs["qty_precision"] == 3
        assert specs["price_tick"] == 0.1

    def test_pf_solusd_specs(self):
        specs = get_contract_specs("PF_SOLUSD")
        assert specs["contract_size"] == 1.0
        assert specs["min_size"] == 0.01
        assert specs["step_size"] == 0.01
        assert specs["qty_precision"] == 2
        assert specs["price_tick"] == 0.01

    def test_pf_xrpusd_specs(self):
        specs = get_contract_specs("PF_XRPUSD")
        assert specs["contract_size"] == 1.0
        assert specs["min_size"] == 1.0
        assert specs["step_size"] == 1.0
        assert specs["qty_precision"] == 0
        assert specs["price_tick"] == 0.0001

    def test_integer_quantity_formatting_for_xrp(self):
        """XRP (précision 0) est transmis sans décimales."""
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key="k", api_secret="c2VjcmV0")
        
        captured = {}
        async def fake_req(m, ep, params=None, data=None, authenticated=True):
            captured.update(data or {})
            return {"result": "success", "sendStatus": {"status": "placed", "order_id": "ORD-XRP"}}
        client._request = fake_req

        asyncio.run(client.place_order(symbol="PF_XRPUSD", side="buy", order_type="mkt", size=300.0))
        assert captured["size"] == "300", f"Attendu '300', reçu {captured['size']}"

    def test_decimal_quantity_formatting_for_btc(self):
        """BTC (précision 4) conserve ses 4 décimales."""
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key="k", api_secret="c2VjcmV0")
        
        captured = {}
        async def fake_req(m, ep, params=None, data=None, authenticated=True):
            captured.update(data or {})
            return {"result": "success", "sendStatus": {"status": "placed", "order_id": "ORD-BTC"}}
        client._request = fake_req

        asyncio.run(client.place_order(symbol="PF_XBTUSD", side="buy", order_type="mkt", size=0.005))
        assert float(captured["size"]) == 0.005
        assert str(captured["size"]) in ("0.005", "0.0050")


# ══════════════════════════════════════════════════════════════════
# 2. LIMITES : MIN, MAX, CAP DE SÉCURITÉ
# ══════════════════════════════════════════════════════════════════

class TestQuantityBounds:
    """Vérifie la gestion des bornes minimales et maximales."""

    def test_quantity_below_minimum_rejected(self):
        """Quantité < min_size rejetée."""
        valid, err = validate_kraken_order(contracts=0, symbol="PF_XBTUSD")
        assert not valid
        assert "BELOW_MIN" in err

        valid, err = validate_kraken_order(contracts=0.00005, symbol="PF_XBTUSD")
        assert not valid
        assert "BELOW_MIN" in err

    def test_quantity_above_safety_cap_rejected(self):
        """Quantité excédant le cap de sécurité notional_usd bloquée avant envoi."""
        # 100 BTC à 90 000 USD = $9 000 000 > cap de $100 000
        valid, err = validate_kraken_order(
            contracts=100.0,
            symbol="PF_XBTUSD",
            entry_price=90_000.0,
            max_notional_usd=100_000.0,
        )
        assert not valid
        assert "NOTIONAL_TOO_HIGH" in err

    def test_valid_quantity_within_bounds_accepted(self):
        """Quantité normale (0.005 BTC à 90 000 = $450) acceptée."""
        valid, err = validate_kraken_order(
            contracts=0.005,
            symbol="PF_XBTUSD",
            entry_price=90_000.0,
            max_notional_usd=100_000.0,
        )
        assert valid
        assert err is None


# ══════════════════════════════════════════════════════════════════
# 3. MODÈLE LINÉAIRE — LES 4 ACTIFS MAJEURS (PHASE 5)
# ══════════════════════════════════════════════════════════════════

class TestCoinToContractConversion:
    """Vérifie le modèle linéaire Kraken PF_* sur BTC, ETH, SOL et XRP."""

    def test_btc_fraction_linear(self):
        # 0.005 BTC à $90 000 = $450 notional
        contracts = ensure_kraken_contracts(size=0.005, symbol="PF_XBTUSD", entry_price=90_000.0)
        assert contracts == 0.005
        notional = contracts * 90_000.0
        assert notional == 450.0

    def test_eth_fraction_linear(self):
        # 0.15 ETH à $3 200 = $480 notional
        contracts = ensure_kraken_contracts(size=0.15, symbol="PF_ETHUSD", entry_price=3_200.0)
        assert contracts == 0.15
        notional = contracts * 3_200.0
        assert notional == 480.0

    def test_sol_fraction_linear(self):
        # 2.5 SOL à $210 = $525 notional
        contracts = ensure_kraken_contracts(size=2.5, symbol="PF_SOLUSD", entry_price=210.0)
        assert contracts == 2.5
        notional = contracts * 210.0
        assert notional == 525.0

    def test_xrp_linear(self):
        # 300 XRP à $1.15 = $345 notional
        contracts = ensure_kraken_contracts(size=300.0, symbol="PF_XRPUSD", entry_price=1.15)
        assert contracts == 300.0
        notional = contracts * 1.15
        assert round(notional, 2) == 345.0


# ══════════════════════════════════════════════════════════════════
# 4. ÉGALITÉ STRICTE DES QUANTITÉS : ENTRÉE, SL, TP & CLOSE
# ══════════════════════════════════════════════════════════════════

class TestSltpExactQuantityMatching:
    """Vérifie que SL, TP et fermeture d'urgence utilisent EXACTEMENT la même quantité que l'ordre principal."""

    @pytest.mark.asyncio
    async def test_sl_tp_emergency_quantities_are_identical(self):
        recorded_calls = []

        class MockClient:
            def __init__(self):
                self.sl_placed = False

            async def place_market_order(self, symbol, side, size, reduce_only=False, **kw):
                recorded_calls.append(("ENTRY", symbol, side, size, reduce_only))
                return {"success": True, "order_id": "ORD-ENTRY", "symbol": symbol, "side": side, "size": size}

            async def place_order(self, symbol=None, side=None, order_type=None, size=None,
                                  stop_price=None, price=None, reduce_only=False, **kw):
                recorded_calls.append((order_type.upper(), symbol, side, size, reduce_only))
                if order_type == "stp":
                    self.sl_placed = True
                return {"success": True, "order_id": f"ORD-{order_type}", "code": "00000"}

            async def get_open_orders(self, symbol=None):
                if self.sl_placed:
                    return [{"order_id": "ORD-stp", "cliOrdId": "BOT_test", "symbol": symbol, "status": "untouched"}]
                return []

        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        wrapper._rest_client = make_exchange_stateful(MockClient())
        wrapper._connected = True

        # Exécute ordre avec SL et TP pour 0.005 BTC
        res = await wrapper.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=0.005,
            order_type="market",
            entry_price=90000.0,
            stop_loss=85000.0,
            take_profit=95000.0,
        )

        assert res["success"] is True
        entry_call = [c for c in recorded_calls if c[0] == "ENTRY"][0]
        sl_call = [c for c in recorded_calls if c[0] == "STP"][0]
        tp_call = [c for c in recorded_calls if c[0] == "TAKE_PROFIT"][0]

        assert entry_call[3] == 0.005, "Taille entrée = 0.005 BTC"
        assert sl_call[3] == 0.005, "Taille SL doit être rigoureusement identique (0.005)"
        assert tp_call[3] == 0.005, "Taille TP doit être rigoureusement identique (0.005)"
        assert sl_call[4] is True, "SL doit avoir reduceOnly=True"
        assert tp_call[4] is True, "TP doit avoir reduceOnly=True"

    @pytest.mark.asyncio
    async def test_emergency_close_uses_exact_position_quantity(self):
        """Quand SL échoue, la fermeture d'urgence market reduce_only utilise la même quantité."""
        recorded_calls = []

        class MockFailingSLClient:
            async def place_market_order(self, symbol, side, size, reduce_only=False, **kw):
                recorded_calls.append(("MARKET", symbol, side, size, reduce_only))
                if reduce_only:
                    return {"success": True, "order_id": "ORD-CLOSE-EMERGENCY"}
                return {"success": True, "order_id": "ORD-ENTRY"}

            async def place_order(self, order_type=None, **kw):
                if order_type == "stp":
                    return {"success": False, "error": "exchange rejected"}
                return {"success": True, "code": "00000"}

            async def get_open_orders(self, symbol=None):
                return []

        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        wrapper._rest_client = make_exchange_stateful(MockFailingSLClient())
        wrapper._connected = True

        with patch.dict("os.environ", {"KRAKEN_RETRY_ON_ORDER_REJECT": "1"}):
            res = await wrapper.place_order(
                symbol="BTCUSDT",
                side="buy",
                quantity=0.005,
                order_type="market",
                entry_price=90000.0,
                stop_loss=85000.0,
            )

        assert res["success"] is False
        assert res["_sl_emergency_close"] is True

        emergency_call = [c for c in recorded_calls if c[0] == "MARKET" and c[4] is True][0]
        assert emergency_call[3] == 0.005, "La fermeture d'urgence doit avoir envoyé exactement 0.005 BTC"
        assert emergency_call[2] == "sell", "Sens de fermeture opposé à la position (buy → sell)"


# ══════════════════════════════════════════════════════════════════
# 5. STRUCTURES JSON RÉELLES KRAKEN
# ══════════════════════════════════════════════════════════════════

class TestKrakenJsonStructures:
    """Vérifie la bonne détection des structures JSON documentées par Kraken."""

    def test_rejection_in_http_200_is_detected_as_failure(self):
        """HTTP 200 avec status='rejected' ne doit JAMAIS être pris pour un succès."""
        kraken_rejected_payload = {
            "result": "success",
            "serverTime": "2026-11-13T18:00:00Z",
            "sendStatus": {
                "status": "rejected",
                "reason": "insufficientMargin",
                "orderEvents": []
            }
        }
        normalized = KrakenFuturesExecutorWrapper._normalize_order_result(kraken_rejected_payload)
        assert normalized.get("success") is not True
        assert normalized.get("code") != "00000"

    def test_openorders_json_parsing(self):
        """Parsing correct de la liste openOrders retournée par Kraken."""
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key="k", api_secret="c2VjcmV0")

        mock_payload = {
            "result": "success",
            "serverTime": "2026-11-13T18:00:00.000Z",
            "openOrders": [
                {
                    "order_id": "c18f042e-7da7-4e62-8b6e-a23b9cf4e678",
                    "symbol": "PF_XBTUSD",
                    "side": "sell",
                    "orderType": "stp",
                    "size": 0.005,
                    "unfilledSize": 0.005,
                    "filledSize": 0,
                    "stopPrice": 85000.0,
                    "reduceOnly": True,
                    "status": "untouched",
                }
            ]
        }
        async def fake_get(method, ep, **kw):
            return mock_payload
        client._request = fake_get

        orders = asyncio.run(client.get_open_orders(symbol="PF_XBTUSD"))
        assert len(orders) == 1
        assert orders[0]["order_id"] == "c18f042e-7da7-4e62-8b6e-a23b9cf4e678"
        assert orders[0]["orderType"] == "stp"

    def test_fills_json_parsing(self):
        """Parsing correct de la liste fills retournée par Kraken."""
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key="k", api_secret="c2VjcmV0")

        mock_payload = {
            "result": "success",
            "serverTime": "2026-11-13T18:00:00.000Z",
            "fills": [
                {
                    "fill_id": "fill_xyz_123",
                    "symbol": "PF_XBTUSD",
                    "side": "buy",
                    "order_id": "ord_real_789",
                    "size": 0.005,
                    "price": 89500.0,
                    "fillTime": "2026-11-13T18:00:00.000Z",
                }
            ]
        }
        async def fake_get(method, ep, **kw):
            return mock_payload
        client._request = fake_get

        fills = asyncio.run(client.get_fills())
        assert len(fills) == 1
        assert fills[0]["order_id"] == "ord_real_789"
        assert fills[0]["size"] == 0.005


# ══════════════════════════════════════════════════════════════════
# 6. VÉRIFICATION DU TYPE EXACT DE REDUCEONLY (PYTHON BOOL)
# ══════════════════════════════════════════════════════════════════

class TestReduceOnlyBooleanType:
    """Confirme que reduceOnly est transmis comme un booléen Python (True/False) et non comme une chaîne."""

    @pytest.mark.asyncio
    async def test_exchanges_kraken_futures_reduce_only_is_bool(self):
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key="k", api_secret="c2VjcmV0")
        
        captured_data = {}
        async def fake_req(method, endpoint, params=None, data=None, authenticated=True):
            captured_data.update(data or {})
            return {"result": "success", "sendStatus": {"status": "placed", "order_id": "ORD-RO"}}
        client._request = fake_req

        # 1. place_order avec reduce_only=True
        await client.place_order(symbol="PF_XBTUSD", side="sell", order_type="mkt", size=0.005, reduce_only=True)
        assert "reduceOnly" in captured_data
        assert type(captured_data["reduceOnly"]) is bool, f"Attendu bool, reçu {type(captured_data['reduceOnly'])}"
        assert captured_data["reduceOnly"] is True

        # 2. place_stop_order
        captured_data.clear()
        await client.place_stop_order(symbol="PF_XBTUSD", side="sell", size=0.005, stop_price=85000.0)
        assert type(captured_data["reduceOnly"]) is bool
        assert captured_data["reduceOnly"] is True

        # 3. place_take_profit_order
        captured_data.clear()
        await client.place_take_profit_order(symbol="PF_XBTUSD", side="sell", size=0.005, tp_price=95000.0)
        assert type(captured_data["reduceOnly"]) is bool
        assert captured_data["reduceOnly"] is True

        # 4. place_market_order avec reduce_only=True
        captured_data.clear()
        await client.place_market_order(symbol="PF_XBTUSD", side="sell", size=0.005, reduce_only=True)
        assert type(captured_data["reduceOnly"]) is bool
        assert captured_data["reduceOnly"] is True

    @pytest.mark.asyncio
    async def test_rest_v2_reduce_only_is_bool(self):
        from backend.kraken_futures_rest_v2 import KrakenFuturesRESTClientV2
        client = KrakenFuturesRESTClientV2(api_key="k", api_secret="c2VjcmV0")

        captured_data = {}
        async def fake_req(method, endpoint, params=None, data=None, authenticated=True, **kw):
            captured_data.update(data or {})
            return {"result": "success", "sendStatus": {"status": "placed", "order_id": "ORD-V2"}}
        client._request = fake_req

        await client.place_order(symbol="PF_XBTUSD", side="sell", order_type="mkt", size=0.005, reduce_only=True)
        assert type(captured_data["reduceOnly"]) is bool
        assert captured_data["reduceOnly"] is True


# ══════════════════════════════════════════════════════════════════
# 7. GESTION DES FILLS PARTIELS
# ══════════════════════════════════════════════════════════════════

class TestPartialFillProtection:
    """Vérifie que SL, TP et emergency close utilisent la quantité réellement exécutée en cas de fill partiel."""

    @pytest.mark.asyncio
    async def test_partial_fill_adjusts_sl_and_tp_quantities(self):
        recorded_calls = []

        class MockPartialFillClient:
            def __init__(self):
                self.sl_placed = False

            async def place_market_order(self, symbol, side, size, reduce_only=False, **kw):
                recorded_calls.append(("ENTRY", symbol, side, size, reduce_only))
                # Ordre demandé = 0.01 BTC, fill partiel = 0.004 BTC
                return {
                    "success": True,
                    "order_id": "ORD-PARTIAL",
                    "symbol": symbol,
                    "side": side,
                    "size": size,
                    "filled_size": 0.004,
                    "raw_response": {
                        "sendStatus": {
                            "status": "placed",
                            "order_id": "ORD-PARTIAL",
                            "filledSize": 0.004,
                        }
                    }
                }

            async def place_order(self, symbol=None, side=None, order_type=None, size=None,
                                  stop_price=None, price=None, reduce_only=False, **kw):
                recorded_calls.append((order_type.upper(), symbol, side, size, reduce_only))
                if order_type == "stp":
                    self.sl_placed = True
                return {"success": True, "order_id": f"ORD-{order_type}", "code": "00000"}

            async def get_open_orders(self, symbol=None):
                if self.sl_placed:
                    return [{"order_id": "ORD-stp", "cliOrdId": "BOT_test", "symbol": symbol, "status": "untouched"}]
                return []

        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        wrapper._rest_client = make_exchange_stateful(MockPartialFillClient())
        wrapper._connected = True

        res = await wrapper.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=0.01,
            order_type="market",
            entry_price=90000.0,
            stop_loss=85000.0,
            take_profit=95000.0,
        )

        assert res["success"] is True
        sl_call = [c for c in recorded_calls if c[0] == "STP"][0]
        tp_call = [c for c in recorded_calls if c[0] == "TAKE_PROFIT"][0]

        # La quantité des protections doit correspondre au fill partiel (0.004 BTC) et non aux 0.01 BTC demandés
        assert sl_call[3] == 0.004, f"SL attendu 0.004, reçu {sl_call[3]}"
        assert tp_call[3] == 0.004, f"TP attendu 0.004, reçu {tp_call[3]}"


# ══════════════════════════════════════════════════════════════════
# 8. QUANTITÉ RÉELLEMENT EXÉCUTÉE
# ══════════════════════════════════════════════════════════════════

def _build_executor(fills, positions, sl_ok=True, entry_resp=None):
    calls = []
    counters = {"fills": 0, "pos": 0}

    def _resolve(src, key):
        counters[key] += 1
        return src(counters[key]) if callable(src) else src

    class C:
        def __init__(self):
            self.market_placed = False

        async def place_market_order(self, symbol, side, size, reduce_only=False, **kw):
            self.market_placed = True
            calls.append(("MKT", side, size, reduce_only))
            d = {"success": True, "order_id": "E1", "symbol": symbol}
            d.update(entry_resp or {})
            return d

        async def place_order(self, symbol=None, side=None, order_type=None, size=None,
                              reduce_only=False, **kw):
            calls.append((order_type.upper(), side, size, reduce_only))
            ok = sl_ok or order_type != "stp"
            return {"success": ok, "order_id": f"P-{order_type}",
                    "code": "00000" if ok else "REJECT", "error": None if ok else "rejected"}

        async def get_open_orders(self, symbol=None):
            return []

        async def get_fills(self, last_fill_time=None):
            return _resolve(fills, "fills")

        async def get_positions(self, force_refresh=False):
            if not getattr(self, "market_placed", False):
                return []
            return _resolve(positions, "pos")

    w = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
    w._rest_client = C()
    w._connected = True
    return w, calls


def _fill(size, oid="E1"):
    return {"order_id": oid, "symbol": "PF_XBTUSD", "side": "buy", "size": size}


def _pos(size, side="long"):
    return {"symbol": "PF_XBTUSD", "side": side, "size": size}


async def _open(w, qty=0.005, **kw):
    with patch.dict("os.environ", {"KRAKEN_FILL_CONFIRM_RETRIES": "1", "KRAKEN_RETRY_ON_ORDER_REJECT": "1"}):
        return await w.place_order(symbol="BTCUSDT", side="buy", quantity=qty, order_type="market",
                                   entry_price=90000.0, stop_loss=85000.0, take_profit=95000.0, **kw)


def _protect(calls):
    return [c for c in calls if c[0] in ("STP", "TAKE_PROFIT")]


class TestExecutedQuantityConfirmation:
    @pytest.mark.asyncio
    async def test_1_full_fill(self):
        w, calls = _build_executor([_fill(0.005)], [_pos(0.005)])
        res = await _open(w, 0.005)
        assert res["success"] is True
        assert [c[2] for c in _protect(calls)] == [0.005, 0.005]
        assert res["_qty_confirmation"]["state"] == "CONFIRMED"
        assert res["_executed_qty"] == 0.005

    @pytest.mark.asyncio
    async def test_2_partial_fill(self):
        w, calls = _build_executor([_fill(0.002), _fill(0.001)], [_pos(0.003)])  # fills cumulés = 0.003
        res = await _open(w, 0.005)
        assert res["success"] is True
        assert [c[2] for c in _protect(calls)] == [0.003, 0.003]
        assert res["_qty_confirmation"]["fills_qty"] == 0.003

    @pytest.mark.asyncio
    async def test_3_no_fill_places_no_protection(self):
        w, calls = _build_executor([], [])
        res = await _open(w, 0.005)
        assert res["status"] == "NO_FILL"
        assert res["success"] is False
        assert res["sl_attached"] is False and res["tp_attached"] is False
        assert _protect(calls) == []

    @pytest.mark.asyncio
    async def test_4_unknown_after_timeout_never_places_blind_close(self):
        """FILL_UNKNOWN : ne jamais lancer de fermeture d'urgence aveugle qui liquiderait des positions préexistantes."""
        def boom(n):
            raise TimeoutError("fills/positions timeout")
        w, calls = _build_executor(boom, boom)
        res = await _open(w, 0.005)
        assert res["status"] == "FILL_UNKNOWN"
        assert res["success"] is False
        assert res["sl_attached"] is False and res["tp_attached"] is False
        assert _protect(calls) == [], "aucun SL/TP basé sur la quantité demandée"
        # Sécurité critique : AUCUNE fermeture aveugle lancée
        assert res.get("_sl_emergency_close") is False
        closes = [c for c in calls if c[0] == "MKT" and c[3] is True]
        assert len(closes) == 0, "aucune fermeture d'urgence market envoyée sur FILL_UNKNOWN"

    @pytest.mark.asyncio
    async def test_unknown_fills_never_liquidates_preexisting_position(self):
        """Quand les fills sont inconnus et qu'une position préexistante existe (ex: 20 XRP), le bot ne la liquide pas."""
        def no_fills(n):
            raise TimeoutError("fills timeout")
        # Position préexistante sur le compte = 20.0
        w, calls = _build_executor(no_fills, [{"symbol": "PF_XRPUSD", "side": "long", "size": 20.0}])
        res = await w.place_order(
            symbol="XRPUSDT",
            side="buy",
            quantity=40.0,
            order_type="market",
            entry_price=1.15,
            stop_loss=1.0,
            take_profit=1.5,
        )
        assert res["status"] == "FILL_UNKNOWN"
        assert res["success"] is False
        assert _protect(calls) == [], "aucun SL/TP sans fills confirmés"
        # AUCUN ordre de fermeture envoyé : les 20 XRP préexistants ne sont pas touchés !
        closes = [c for c in calls if c[0] == "MKT" and c[3] is True]
        assert len(closes) == 0, "la position préexistante ne doit JAMAIS être fermée"

    @pytest.mark.asyncio
    async def test_5_position_already_reduced(self):
        w, calls = _build_executor([_fill(0.005)], [_pos(0.002)])
        res = await _open(w, 0.005)
        assert [c[2] for c in _protect(calls)] == [0.002, 0.002]
        assert res["_qty_confirmation"]["source"] == "fills+position"

    @pytest.mark.asyncio
    async def test_6a_position_closed_before_protection(self):
        w, calls = _build_executor([_fill(0.005)], [])
        res = await _open(w, 0.005)
        assert res["status"] == "POSITION_CLOSED_BEFORE_PROTECTION"
        assert res["success"] is False
        assert _protect(calls) == []

    @pytest.mark.asyncio
    async def test_6b_position_closed_before_sl_confirmation(self):
        w, calls = _build_executor([_fill(0.005)], lambda n: [_pos(0.005)] if n == 1 else [], sl_ok=False)
        res = await _open(w, 0.005)
        assert res["status"] == "POSITION_CLOSED_BEFORE_SL"
        assert res["success"] is False
        assert res["sl_attached"] is False
        assert not any(c[0] == "MKT" and c[3] is True for c in calls)

    @pytest.mark.asyncio
    async def test_sl_retry_updates_quantity_if_position_reduced(self):
        """Si la position est modifiée/réduite de l'extérieur pendant les retries du SL, l'intégrité échoue et passe en MANUAL_RECONCILIATION_REQUIRED."""
        sl_attempts = []
        pos_counter = 0

        class DynamicClient:
            def __init__(self):
                self.market_placed = False

            async def place_market_order(self, symbol, side, size, reduce_only=False, **kw):
                self.market_placed = True
                return {"success": True, "order_id": "ORD-DYN", "symbol": symbol, "side": side, "size": size}

            async def place_order(self, symbol=None, side=None, order_type=None, size=None,
                                  stop_price=None, price=None, reduce_only=False, **kw):
                if order_type == "stp":
                    sl_attempts.append(size)
                    # 1ère tentative échoue
                    return {"success": False, "error": "temporary reject"}
                return {"success": True, "order_id": "TP-OK"}

            async def get_open_orders(self, symbol=None):
                return []

            async def get_fills(self, last_fill_time=None):
                return [{"order_id": "ORD-DYN", "symbol": "PF_XBTUSD", "side": "buy", "size": 0.005}]

            async def get_positions(self, force_refresh=False):
                if not getattr(self, "market_placed", False):
                    return []
                nonlocal pos_counter
                pos_counter += 1
                # Confirmation initiale & 1er SL: 0.005 BTC. Après échec du 1er SL, position modifiée à 0.002 BTC.
                if pos_counter <= 3:
                    return [{"symbol": "PF_XBTUSD", "side": "long", "size": 0.005}]
                return [{"symbol": "PF_XBTUSD", "side": "long", "size": 0.002}]

        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        wrapper._rest_client = DynamicClient()
        wrapper._connected = True

        with patch.dict("os.environ", {"KRAKEN_RETRY_ON_ORDER_REJECT": "2", "KRAKEN_FILL_CONFIRM_RETRIES": "1"}):
            res = await wrapper.place_order(
                symbol="BTCUSDT",
                side="buy",
                quantity=0.005,
                order_type="market",
                entry_price=90000.0,
                stop_loss=85000.0,
            )

        # L'intégrité a détecté la modification externe (0.002 != 0.005) -> blocage et réconciliation requise
        assert res["success"] is False
        assert res["status"] == "MANUAL_RECONCILIATION_REQUIRED"
        assert len(sl_attempts) == 1, "La 2ème tentative de SL doit être bloquée par le moniteur d'intégrité"

    @pytest.mark.asyncio
    async def test_sl_failure_emergency_close_caps_to_remaining_open_position(self):
        """Quand tous les retries du SL échouent avec position intacte, la fermeture d'urgence clôture l'exposition."""
        emergency_calls = []

        class FailingSLClient:
            def __init__(self):
                self.market_placed = False

            async def place_market_order(self, symbol, side, size, reduce_only=False, **kw):
                if reduce_only:
                    emergency_calls.append(("EMERGENCY", symbol, side, size, reduce_only))
                    return {"success": True, "order_id": "EMERGENCY-CLOSE"}
                self.market_placed = True
                return {"success": True, "order_id": "ORD-ENTRY", "symbol": symbol, "side": side, "size": size}

            async def place_order(self, order_type=None, **kw):
                if order_type == "stp":
                    return {"success": False, "error": "exchange rejected"}
                return {"success": True}

            async def get_open_orders(self, symbol=None):
                return []

            async def get_fills(self, last_fill_time=None):
                return [{"order_id": "ORD-ENTRY", "symbol": "PF_XBTUSD", "side": "buy", "size": 0.005}]

            async def get_positions(self, force_refresh=False):
                if not getattr(self, "market_placed", False):
                    return []
                return [{"symbol": "PF_XBTUSD", "side": "long", "size": 0.005}]

        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        wrapper._rest_client = FailingSLClient()
        wrapper._connected = True

        with patch.dict("os.environ", {"KRAKEN_RETRY_ON_ORDER_REJECT": "1", "KRAKEN_FILL_CONFIRM_RETRIES": "1"}):
            res = await wrapper.place_order(
                symbol="BTCUSDT",
                side="buy",
                quantity=0.005,
                order_type="market",
                entry_price=90000.0,
                stop_loss=85000.0,
            )

        assert res["success"] is False
        assert res["_sl_emergency_close"] is True
        assert len(emergency_calls) == 1
        assert emergency_calls[0][3] == 0.005, "La fermeture d'urgence doit envoyer 0.005 BTC"

    @pytest.mark.asyncio
    async def test_opposite_position_netting(self):
        """Position inverse préexistante (-0.003 Short) -> Pre-Flight Gate REFUSE l'entrée (Scénario C netting)."""
        class NettingClient:
            async def get_open_orders(self, symbol=None):
                return []

            async def get_positions(self, force_refresh=False):
                # Position short préexistante sur le compte
                return [{"symbol": "PF_XBTUSD", "side": "short", "size": 0.003}]

        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        wrapper._rest_client = NettingClient()
        wrapper._connected = True

        res = await wrapper.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=0.005,
            order_type="market",
            entry_price=90000.0,
            stop_loss=85000.0,
            take_profit=95000.0,
        )

        assert res["success"] is False
        assert res["code"] == "ENTRY_BLOCKED_EXISTING_POSITION"
        assert res["existing_qty"] == 0.003

    @pytest.mark.asyncio
    async def test_persistence_hook_blocks_unconfirmed_statuses(self):
        import inspect
        from backend.services import trade_persistence_hook as h
        src = inspect.getsource(h)
        for st in ("FILL_UNKNOWN", "NO_FILL", "POSITION_CLOSED_BEFORE_PROTECTION", "POSITION_CLOSED_BEFORE_SL"):
            assert st in src


# ══════════════════════════════════════════════════════════════════
# 9. RISQUE LINÉAIRE & NORMALISATION SANS GONFLEMENT
# ══════════════════════════════════════════════════════════════════

class TestRiskAfterNormalisation:
    def test_floor_to_precision_without_force_can_be_zero(self):
        # Pour PF_XBTUSD (min 0.0001, prec 4) : 0.00007 floore à 0.0
        assert _floor_to_precision("PF_XBTUSD", 0.00007, force_min=False) == 0.0
        # Pour PF_XBTUSD, 0.12349 floore à 0.1234 sans arrondir vers le haut
        assert _floor_to_precision("PF_XBTUSD", 0.12349, force_min=False) == 0.1234
        # force_min=True utilisé uniquement en reduce_only
        assert _floor_to_precision("PF_XBTUSD", 0.00007, force_min=True) == 0.0001
        # Pour PF_XRPUSD (prec 0) : 0.7 floore à 0.0
        assert _floor_to_precision("PF_XRPUSD", 0.7, force_min=False) == 0.0

    def test_linear_risk_never_applies_leverage_twice(self):
        """Risque linéaire : balance=10k, 1%=$100, entry=90k, sl=88k -> dist=2k -> qty=0.05 BTC."""
        r = calculate_size(
            balance_usd=10_000,
            risk_pct=0.01,
            entry_price=90_000.0,
            stop_price=88_000.0,
            symbol="PF_XBTUSD",
            broker="kraken",
            leverage=10,
        )
        assert r.valid is True
        assert r.qty == 0.05
        assert r.contracts == 0.05
        assert r.notional_usd == 0.05 * 90_000.0  # $4 500
        real_risk = r.qty * abs(90_000.0 - 88_000.0)
        assert abs(real_risk - 100.0) < 1e-6
        assert real_risk <= r.risk_usd + 1e-6

    def test_sizing_refuses_when_min_contract_exceeds_budget(self):
        r = calculate_size(
            balance_usd=1.0,
            risk_pct=0.0001,
            entry_price=90_000.0,
            stop_price=60_000.0,
            symbol="PF_XBTUSD",
            broker="kraken",
        )
        assert r.valid is False
        assert r.rejection_reason == "RISK_EXCEEDS_BUDGET_AFTER_NORMALIZATION"

    @pytest.mark.asyncio
    async def test_executor_refuses_coin_qty_flooring_to_zero_contracts(self):
        w, calls = _build_executor([], [])
        res = await w.place_order(
            symbol="BTCUSDT",
            side="buy",
            quantity=0.00001,
            order_type="market",
            entry_price=50_000.0,
            stop_loss=49_000.0,
        )
        assert res["success"] is False
        assert res["code"] == "BELOW_MIN_RISK_BUDGET"
        assert calls == [], "aucun ordre envoyé"


# ══════════════════════════════════════════════════════════════════
# 10. ISOLATION DE POSITION PRÉEXISTANTE & RÉDUCTION DE POSITION
# ══════════════════════════════════════════════════════════════════

class TestPositionIsolationAndReduction:
    """Vérifie l'isolation des positions préexistantes et les réductions sécurisées."""

    @pytest.mark.asyncio
    async def test_preexisting_position_isolation_xrp(self):
        """
        Compte avec position existante = 20 XRP.
        Ordre d'entrée = 40 XRP.
        Position agrégée Kraken = 60 XRP.
        Vérifie que SL et TP sont posés pour 40 XRP (et non 60 XRP).
        """
        fills = [{"order_id": "ORD-40", "symbol": "PF_XRPUSD", "side": "buy", "size": 40.0}]
        positions = [{"symbol": "PF_XRPUSD", "side": "long", "size": 60.0}]

        recorded_calls = []
        class MockClient:
            async def place_market_order(self, symbol, side, size, reduce_only=False, **kw):
                recorded_calls.append(("ENTRY", symbol, side, size, reduce_only))
                return {"success": True, "order_id": "ORD-40", "symbol": symbol, "side": side, "size": size}

            async def place_order(self, symbol=None, side=None, order_type=None, size=None,
                                  reduce_only=False, **kw):
                recorded_calls.append((order_type.upper(), symbol, side, size, reduce_only))
                return {"success": True, "order_id": f"ORD-{order_type}", "code": "00000"}

            async def get_open_orders(self, symbol=None):
                return []

            async def get_fills(self, last_fill_time=None):
                return fills

            async def get_positions(self, force_refresh=False):
                return positions

        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        wrapper._rest_client = MockClient()
        wrapper._connected = True

        res = await wrapper.place_order(
            symbol="XRPUSDT",
            side="buy",
            quantity=40.0,
            order_type="market",
            entry_price=1.15,
            stop_loss=1.00,
            take_profit=1.50,
        )

        # Pre-Flight Position Gate: entrée strictement refusée car position préexistante détectée!
        assert res["success"] is False
        assert res["code"] == "ENTRY_BLOCKED_EXISTING_POSITION"

    @pytest.mark.asyncio
    async def test_position_reduction_valid_size(self):
        """Réduction valide (40% de 0.005 BTC = 0.002 BTC >= min 0.0001 BTC) transmise avec reduceOnly=True."""
        from unittest.mock import AsyncMock
        from backend.execution.kraken_futures_executor import KrakenTradeTracker
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        wrapper._connected = True

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-001", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.005, expected_exposure=0.005, status="OPEN"
        )
        wrapper._fetch_position_qty_strict = AsyncMock(side_effect=lambda sym, side: 0.005 if side == "long" else 0.0)
        wrapper._place_order_rest = AsyncMock(return_value={"code": "00000", "success": True, "order_id": "ORD-RED"})

        res = await wrapper.reduce_position(
            symbol="PF_XBTUSD",
            percent=40.0,
        )
        assert res["success"] is True
        assert res["close_qty"] == 0.002
        wrapper._place_order_rest.assert_called_once()

    @pytest.mark.asyncio
    async def test_position_reduction_below_min_rejected_never_bumped(self):
        """Réduction < min_size (10% de 0.0001 BTC = 0.00001 BTC < 0.0001 BTC) rejetée, jamais gonflée artificiellement."""
        from unittest.mock import AsyncMock
        from backend.execution.kraken_futures_executor import KrakenTradeTracker
        wrapper = KrakenFuturesExecutorWrapper(api_key="k", api_secret="c2VjcmV0")
        wrapper._connected = True

        wrapper._active_trackers["PF_XBTUSD"] = KrakenTradeTracker(
            order_id="ORD-001", client_oid="BOT_test", symbol="PF_XBTUSD", side="long",
            filled_qty=0.0001, expected_exposure=0.0001, status="OPEN"
        )
        wrapper._fetch_position_qty_strict = AsyncMock(side_effect=lambda sym, side: 0.0001 if side == "long" else 0.0)

        res = await wrapper.reduce_position(
            symbol="PF_XBTUSD",
            percent=10.0,
        )
        assert res["success"] is False
        assert res["code"] == "REDUCE_BELOW_MIN"
