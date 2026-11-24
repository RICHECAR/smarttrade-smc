"""
Tests de non-régression — Corrections P0 (Audit Kraken, Nov 2026)
══════════════════════════════════════════════════════════════════

Ces tests vérifient EXCLUSIVEMENT les 3 corrections P0 appliquées :

  P0-001 : kraken_futures_executor.py — Emergency close résultat capturé,
           success=False retourné si SL échoue (position nue ou fermée).

  P0-002 : broker_router.py — _check_order_after_timeout valide symbol+side+qty
           avant de retourner SUCCESS (évite les faux positifs post-timeout).

  P0-003 : kraken_futures_executor.py — TP failure flag explicite + status,
           success N'EST PAS mis à False (SL toujours en place, pas d'urgence).

Run:
    python -m pytest tests/test_p0_fixes.py -v
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────
# Fake clients réutilisés dans plusieurs tests
# ─────────────────────────────────────────────────────────────────

@dataclass
class FakeKrakenOrderResult:
    success: bool
    order_id: Optional[str] = None
    symbol: Optional[str] = None
    side: Optional[str] = None
    size: Optional[float] = None
    error: Optional[str] = None
    raw_response: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "broker": "kraken",
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "error": self.error,
            "timestamp": self.timestamp.isoformat(),
        }


class FakeClientBase:
    """Client minimal : market order réussit, SL/TP réussissent par défaut."""

    def __init__(self):
        self.calls: list = []

    async def place_market_order(self, symbol, side, size, reduce_only=False, **kw):
        self.calls.append(("place_market_order", symbol, side, size, reduce_only))
        return FakeKrakenOrderResult(
            success=True, order_id="ORD-MKT-001", symbol=symbol, side=side, size=size
        )

    async def place_order(self, symbol=None, side=None, order_type=None, size=None,
                          stop_price=None, price=None, reduce_only=False, **kw):
        self.calls.append(("place_order", symbol, side, order_type, size, stop_price or price))
        return {"success": True, "order_id": "ORD-UNI-001", "code": "00000"}

    async def get_positions(self):
        return []


from kraken_fake_exchange import make_exchange_stateful


def make_wrapper(client):
    from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
    wrapper = KrakenFuturesExecutorWrapper(api_key="test", api_secret="dGVzdA==")
    wrapper._rest_client = make_exchange_stateful(client)
    wrapper._connected = True
    return wrapper


# ══════════════════════════════════════════════════════════════════
# P0-001 : Emergency close — résultat CAPTURÉ et success=False
# ══════════════════════════════════════════════════════════════════

class TestP0001EmergencyClose:
    """
    P0-001 FIX : Quand le SL échoue sur tous les retries, le bot effectue une
    fermeture d'urgence. Avant le fix, le résultat de cette fermeture était ignoré
    et success restait True. Après le fix :
      - Le résultat est capturé.
      - success=False dans TOUS les cas (position fermée OU nue).
      - _sl_emergency_close_ok indique si la fermeture a réussi.
      - status est 'SL_FAILED_POSITION_EMERGENCY_CLOSED' ou 'SL_FAILED_POSITION_NAKED'.
    """

    @pytest.mark.asyncio
    async def test_sl_fail_emergency_close_succeeds_returns_false(self):
        """
        SL échoue × N retries → emergency close RÉUSSIT.
        Attendu : success=False, status='SL_FAILED_POSITION_EMERGENCY_CLOSED',
                  _sl_emergency_close_ok=True.
        """
        client = FakeClientBase()

        # SL (order_type="stp") → toujours refusé
        _orig = client.place_order
        async def fail_sl(symbol=None, side=None, order_type=None, **kwargs):
            if order_type == "stp":
                return {"success": False, "error": "rejected by exchange", "code": "REJECT"}
            return await _orig(symbol=symbol, side=side, order_type=order_type, **kwargs)

        client.place_order = fail_sl
        wrapper = make_wrapper(client)

        with patch.dict("os.environ", {"KRAKEN_RETRY_ON_ORDER_REJECT": "1"}):
            result = await wrapper.place_order(
                symbol="BTCUSDT", side="buy", quantity=10,
                order_type="market", stop_loss=60000.0,
            )

        # ── Assertions P0-001 ──────────────────────────────────────────────
        assert result.get("success") is False, (
            "P0-001: success doit être False quand SL échoue, même si emergency close réussit"
        )
        assert result.get("sl_attached") is False
        assert result.get("_sl_emergency_close") is True
        assert result.get("_sl_emergency_close_ok") is True, (
            "P0-001: _sl_emergency_close_ok doit être True quand la fermeture d'urgence réussit"
        )
        assert result.get("status") == "SL_FAILED_POSITION_EMERGENCY_CLOSED", (
            f"P0-001: status inattendu: {result.get('status')}"
        )

    @pytest.mark.asyncio
    async def test_sl_fail_emergency_close_also_fails_returns_naked(self):
        """
        SL échoue × N retries → emergency close AUSSI ÉCHOUE.
        Attendu : success=False, status='SL_FAILED_POSITION_NAKED',
                  _sl_emergency_close_ok=False.
        """
        client = FakeClientBase()

        # Toute fermeture (market reduce_only) et tout SL → refusé
        async def fail_all(symbol=None, side=None, order_type=None,
                           reduce_only=False, size=None, stop_price=None,
                           price=None, **kwargs):
            return {"success": False, "error": "network timeout", "code": "TIMEOUT"}

        client.place_order = fail_all

        # On overrides aussi _place_order_rest pour simuler échec fermeture d'urgence
        from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
        wrapper = KrakenFuturesExecutorWrapper(api_key="test", api_secret="dGVzdA==")
        wrapper._connected = True

        # _place_order_rest doit aussi échouer pour les deux appels (entry + emergency)
        call_count = {"n": 0}
        async def fake_place_order_rest(symbol, side, quantity, order_type,
                                        price=None, reduce_only=False, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Premier appel = ordre principal → succès
                return {"success": True, "code": "00000", "order_id": "MKT-001"}
            else:
                # Appels suivants = SL (via _place_stop_order_rest) ou emergency close → échec
                return {"success": False, "error": "network timeout", "code": "TIMEOUT"}

        async def fake_place_stop_order_rest(symbol, side, size, stop_price):
            return {"success": False, "error": "rejected", "code": "REJECT"}

        wrapper._place_order_rest = fake_place_order_rest
        wrapper._place_stop_order_rest = fake_place_stop_order_rest

        # Real-fill confirmation: the entry (MKT-001, 10 contracts) is filled and open.
        async def _fills(last_fill_time=None):
            return [{"order_id": "MKT-001", "symbol": "PF_XBTUSD", "size": 10}]

        async def _positions(force_refresh=False):
            if call_count["n"] == 0:
                return []
            return [{"symbol": "PF_XBTUSD", "side": "long", "size": 10}]

        client.get_fills = _fills
        client.get_positions = _positions
        wrapper._rest_client = client

        with patch.dict("os.environ", {"KRAKEN_RETRY_ON_ORDER_REJECT": "1"}):
            result = await wrapper.place_order(
                symbol="BTCUSDT", side="buy", quantity=10,
                order_type="market", stop_loss=60000.0,
            )

        assert result.get("success") is False, (
            "P0-001: success doit être False quand SL ET emergency close échouent"
        )
        assert result.get("_sl_emergency_close") is True
        assert result.get("_sl_emergency_close_ok") is False, (
            "P0-001: _sl_emergency_close_ok doit être False si la fermeture d'urgence échoue"
        )
        assert result.get("status") == "SL_FAILED_POSITION_NAKED", (
            f"P0-001: status inattendu: {result.get('status')} — position nue non signalée"
        )

    @pytest.mark.asyncio
    async def test_sl_success_does_not_trigger_emergency(self):
        """
        Cas nominal : SL réussit → PAS de fermeture d'urgence, success=True.
        Vérifie qu'on n'a pas cassé le chemin nominal.
        """
        client = FakeClientBase()
        wrapper = make_wrapper(client)

        result = await wrapper.place_order(
            symbol="BTCUSDT", side="buy", quantity=10,
            order_type="market", stop_loss=60000.0,
        )

        assert result.get("success") is True
        assert result.get("sl_attached") is True
        assert result.get("_sl_emergency_close") is not True, (
            "P0-001: emergency close ne doit PAS être déclenché si SL réussit"
        )


# ══════════════════════════════════════════════════════════════════
# P0-002 : _check_order_after_timeout — validation symbol+side+qty
# ══════════════════════════════════════════════════════════════════

class TestP0002TimeoutPositionCheck:
    """
    P0-002 FIX : Avant le fix, _check_order_after_timeout retournait SUCCESS
    pour N'IMPORTE QUELLE position existante sur le broker après un timeout.
    Après le fix, la position doit correspondre à l'intent (symbol + side + qty ±30%).
    """

    def _make_intent(self, symbol="BTCUSDT", side="buy", quantity=10.0):
        """Crée un TradeIntent minimal via la classe du projet."""
        try:
            from backend.execution.broker_router import TradeIntent
            intent = TradeIntent(
                symbol=symbol,
                side=side,
                quantity=quantity,
                entry_price=50000.0,
                stop_loss=48000.0,
            )
        except Exception:
            # Fallback si TradeIntent a des champs obligatoires supplémentaires
            class FakeIntent:
                def __init__(self):
                    self.symbol = symbol
                    self.side = side
                    self.quantity = quantity
            intent = FakeIntent()
        return intent

    def _make_router_with_executor(self, positions: list):
        """Crée un BrokerRouter minimal avec un executor fake qui retourne `positions`."""
        from backend.execution.broker_router import BrokerRouter

        class FakeExecutor:
            async def get_positions(self, symbol=None):
                return positions

        router = BrokerRouter.__new__(BrokerRouter)
        router._executors = {"kraken": FakeExecutor()}
        router._execution_timeout = 30.0

        # get_broker_symbol: retourne PF_XBTUSD pour BTCUSDT/kraken
        def get_broker_symbol(sym, broker):
            mapping = {
                "BTCUSDT": "PF_XBTUSD",
                "ETHUSDT": "PF_ETHUSD",
            }
            return mapping.get(sym.upper(), sym)

        router.get_broker_symbol = get_broker_symbol
        return router

    @pytest.mark.asyncio
    async def test_matching_position_returns_success(self):
        """
        Position correspond exactement → BrokerResult(success=True).
        """
        positions = [{"symbol": "PF_XBTUSD", "side": "long", "size": 10.0}]
        router = self._make_router_with_executor(positions)
        intent = self._make_intent(symbol="BTCUSDT", side="buy", quantity=10.0)

        result = await router._check_order_after_timeout("kraken", intent)

        assert result is not None
        assert result.success is True, "P0-002: position correspondante → SUCCESS attendu"

    @pytest.mark.asyncio
    async def test_wrong_side_does_not_confirm(self):
        """
        Position existe mais côté opposé (short au lieu de long)
        → doit retourner None, pas SUCCESS.
        """
        positions = [{"symbol": "PF_XBTUSD", "side": "short", "size": 10.0}]
        router = self._make_router_with_executor(positions)
        intent = self._make_intent(symbol="BTCUSDT", side="buy", quantity=10.0)

        result = await router._check_order_after_timeout("kraken", intent)

        assert result is None, (
            "P0-002: position côté OPPOSÉ ne doit PAS confirmer l'intent"
        )

    @pytest.mark.asyncio
    async def test_wrong_symbol_does_not_confirm(self):
        """
        Position existe sur un symbole différent (ETH au lieu de BTC)
        → doit retourner None, pas SUCCESS.
        """
        positions = [{"symbol": "PF_ETHUSD", "side": "long", "size": 10.0}]
        router = self._make_router_with_executor(positions)
        intent = self._make_intent(symbol="BTCUSDT", side="buy", quantity=10.0)

        result = await router._check_order_after_timeout("kraken", intent)

        assert result is None, (
            "P0-002: position sur un AUTRE symbole ne doit PAS confirmer l'intent"
        )

    @pytest.mark.asyncio
    async def test_quantity_too_different_does_not_confirm(self):
        """
        Position avec qty très différente (> 30% d'écart) → None.
        """
        positions = [{"symbol": "PF_XBTUSD", "side": "long", "size": 50.0}]  # 400% d'écart vs 10
        router = self._make_router_with_executor(positions)
        intent = self._make_intent(symbol="BTCUSDT", side="buy", quantity=10.0)

        result = await router._check_order_after_timeout("kraken", intent)

        assert result is None, (
            "P0-002: position avec qty trop éloignée (>30%) ne doit PAS confirmer l'intent"
        )

    @pytest.mark.asyncio
    async def test_quantity_within_30pct_confirms(self):
        """
        Position avec qty dans ±30% → SUCCESS (partial fill accepté).
        """
        positions = [{"symbol": "PF_XBTUSD", "side": "long", "size": 12.0}]  # +20% vs 10
        router = self._make_router_with_executor(positions)
        intent = self._make_intent(symbol="BTCUSDT", side="buy", quantity=10.0)

        result = await router._check_order_after_timeout("kraken", intent)

        assert result is not None
        assert result.success is True, (
            "P0-002: qty dans ±30% (partial fill) → SUCCESS attendu"
        )

    @pytest.mark.asyncio
    async def test_no_positions_returns_none(self):
        """
        Aucune position → None (timeout sans confirmation).
        """
        router = self._make_router_with_executor([])
        intent = self._make_intent(symbol="BTCUSDT", side="buy", quantity=10.0)

        result = await router._check_order_after_timeout("kraken", intent)

        assert result is None, "P0-002: aucune position → None attendu"

    @pytest.mark.asyncio
    async def test_timeout_result_has_no_order_id(self):
        """
        Confirmation post-timeout → order_id=None (pas de faux broker_order_id).
        """
        positions = [{"symbol": "PF_XBTUSD", "side": "long", "size": 10.0}]
        router = self._make_router_with_executor(positions)
        intent = self._make_intent(symbol="BTCUSDT", side="buy", quantity=10.0)

        result = await router._check_order_after_timeout("kraken", intent)

        assert result is not None
        assert result.order_id is None, (
            "P0-002: order_id doit être None après timeout (pas de faux broker_order_id)"
        )


# ══════════════════════════════════════════════════════════════════
# P0-003 : TP failure — flag explicite, success non altéré si SL OK
# ══════════════════════════════════════════════════════════════════

class TestP0003TpFailure:
    """
    P0-003 FIX : Avant le fix, si le TP échouait sur tous les retries,
    le bot retournait success=True sans aucun flag d'alerte.
    Après le fix :
      - _tp_failed=True
      - status='TP_FAILED_SL_ONLY'
      - success reste à la valeur de l'ordre principal (True si SL en place)
      - tp_attached=False
    La position N'est PAS fermée (SL est en place).
    """

    @pytest.mark.asyncio
    async def test_tp_fail_flags_set_correctly(self):
        """
        TP échoue × N retries avec SL OK.
        Attendu : success=True (position protégée par SL), _tp_failed=True,
                  status='TP_FAILED_SL_ONLY', tp_attached=False, sl_attached=True.
        """
        client = FakeClientBase()

        _orig = client.place_order
        async def fail_tp(symbol=None, side=None, order_type=None, **kwargs):
            if order_type == "take_profit":
                return {"success": False, "error": "tp rejected", "code": "REJECT"}
            return await _orig(symbol=symbol, side=side, order_type=order_type, **kwargs)

        client.place_order = fail_tp
        wrapper = make_wrapper(client)

        with patch.dict("os.environ", {"KRAKEN_RETRY_ON_ORDER_REJECT": "1"}):
            result = await wrapper.place_order(
                symbol="BTCUSDT", side="buy", quantity=10,
                order_type="market", stop_loss=60000.0, take_profit=70000.0,
            )

        # SL OK → position protégée → success=True (pas de fermeture d'urgence pour TP)
        assert result.get("success") is True, (
            "P0-003: success doit rester True si SL est en place malgré l'échec TP"
        )
        assert result.get("sl_attached") is True
        assert result.get("tp_attached") is False
        assert result.get("_tp_failed") is True, (
            "P0-003: _tp_failed doit être True si TP échoue sur tous les retries"
        )
        assert result.get("status") == "TP_FAILED_SL_ONLY", (
            f"P0-003: status inattendu: {result.get('status')}"
        )

    @pytest.mark.asyncio
    async def test_tp_success_no_failed_flag(self):
        """
        Cas nominal : TP réussit → PAS de _tp_failed, tp_attached=True.
        Vérifie qu'on n'a pas cassé le chemin nominal.
        """
        client = FakeClientBase()
        wrapper = make_wrapper(client)

        result = await wrapper.place_order(
            symbol="BTCUSDT", side="buy", quantity=10,
            order_type="market", stop_loss=60000.0, take_profit=70000.0,
        )

        assert result.get("success") is True
        assert result.get("tp_attached") is True
        assert result.get("_tp_failed") is not True, (
            "P0-003: _tp_failed ne doit PAS être True si TP réussit"
        )
        assert result.get("status") != "TP_FAILED_SL_ONLY"

    @pytest.mark.asyncio
    async def test_tp_fail_does_not_trigger_emergency_close(self):
        """
        TP échoue avec SL OK → PAS de fermeture d'urgence.
        La position reste ouverte avec son SL.
        """
        client = FakeClientBase()

        _orig = client.place_order
        market_calls = []

        async def track_calls(symbol=None, side=None, order_type=None,
                               size=None, stop_price=None, price=None,
                               reduce_only=False, **kwargs):
            # Échec TP uniquement
            if order_type == "take_profit":
                return {"success": False, "error": "tp rejected", "code": "REJECT"}
            # On track les market + reduce_only pour détecter une fermeture d'urgence
            return await _orig(symbol=symbol, side=side, order_type=order_type,
                                size=size, stop_price=stop_price, price=price,
                                reduce_only=reduce_only, **kwargs)

        client.place_order = track_calls

        # On overrides aussi place_market_order pour tracker les reduce_only
        emergency_closes = []
        orig_market = client.place_market_order
        async def track_market(symbol, side, size, reduce_only=False, **kw):
            if reduce_only:
                emergency_closes.append((symbol, side, size))
            return await orig_market(symbol, side, size, reduce_only=reduce_only, **kw)

        client.place_market_order = track_market
        wrapper = make_wrapper(client)

        with patch.dict("os.environ", {"KRAKEN_RETRY_ON_ORDER_REJECT": "1"}):
            result = await wrapper.place_order(
                symbol="BTCUSDT", side="buy", quantity=10,
                order_type="market", stop_loss=60000.0, take_profit=70000.0,
            )

        assert len(emergency_closes) == 0, (
            f"P0-003: fermeture d'urgence NE doit PAS être déclenchée sur échec TP seul. "
            f"Closes détectées: {emergency_closes}"
        )
        assert result.get("_tp_failed") is True
        assert result.get("success") is True


# ══════════════════════════════════════════════════════════════════
# PERSISTENCE PROTECTION : SL_FAILED_... n'est jamais persisté OPEN
# ══════════════════════════════════════════════════════════════════

class TestPersistenceProtection:
    """
    Vérifie que les statuts d'échec de SL ne peuvent jamais être insérés
    comme trades ouverts dans la table live_trades via trade_persistence_hook.
    """

    @pytest.mark.asyncio
    async def test_sl_failed_emergency_closed_not_persisted(self):
        from backend.services.trade_persistence_hook import on_trade_opened
        res = await on_trade_opened(
            order_id="TEST_ORD_001",
            symbol="BTCUSDT",
            side="buy",
            qty=1.0,
            entry=60000.0,
            stop=58000.0,
            tp=65000.0,
            broker="kraken",
            status="SL_FAILED_POSITION_EMERGENCY_CLOSED",
        )
        assert res is None, "SL_FAILED_POSITION_EMERGENCY_CLOSED ne doit jamais créer de LiveTrade"

    @pytest.mark.asyncio
    async def test_sl_failed_naked_not_persisted(self):
        from backend.services.trade_persistence_hook import on_trade_opened
        res = await on_trade_opened(
            order_id="TEST_ORD_002",
            symbol="BTCUSDT",
            side="buy",
            qty=1.0,
            entry=60000.0,
            stop=58000.0,
            tp=65000.0,
            broker="kraken",
            status="SL_FAILED_POSITION_NAKED",
        )
        assert res is None, "SL_FAILED_POSITION_NAKED ne doit jamais créer de LiveTrade"

    @pytest.mark.asyncio
    async def test_tp_failed_sl_only_is_tracked_as_open(self):
        """
        TP_FAILED_SL_ONLY : La position a son SL confirmé sur Kraken et doit
        rester suivie comme OPEN dans live_trades.
        """
        from backend.services.trade_persistence_hook import on_trade_opened
        with patch("backend.services.live_trades_service.LiveTradesService.create_live_trade_from_order") as mock_create:
            mock_create.return_value = MagicMock(id="trade_123", status="OPEN")
            res = await on_trade_opened(
                order_id="TEST_ORD_003",
                symbol="BTCUSDT",
                side="buy",
                qty=1.0,
                entry=60000.0,
                stop=58000.0,
                tp=65000.0,
                broker="kraken",
                status="TP_FAILED_SL_ONLY",
            )
            assert res is not None, "TP_FAILED_SL_ONLY doit être conservé car la position est protégée par SL"
            assert mock_create.called
            # Vérifier que le statut transmis au service est 'open'
            called_context = mock_create.call_args[0][1]
            assert called_context.get("status") == "open"


# ══════════════════════════════════════════════════════════════════
# COMPATIBILITÉ PARAMÈTRES API KRAKEN FUTURES
# ══════════════════════════════════════════════════════════════════

class TestKrakenApiCompliance:
    """
    Vérifie la conformité exacte des paramètres transmis à Kraken Futures REST API :
    - POST /derivatives/api/v3/sendorder
    - stopPrice pour stp et take_profit
    - triggerSignal='mark'
    - reduceOnly='true'
    - size integer string
    """

    @pytest.mark.asyncio
    async def test_stop_loss_payload_compliance(self):
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key="key", api_secret="c2VjcmV0")
        
        captured_data = {}
        async def fake_request(method, endpoint, params=None, data=None, authenticated=True):
            captured_data.update(data or {})
            return {"result": "success", "sendStatus": {"status": "placed", "order_id": "SL-ORD-999"}}

        client._request = fake_request

        res = await client.place_order(
            symbol="PF_XBTUSD",
            side="sell",
            order_type="stp",
            size=5.0,
            stop_price=55000.0,
            reduce_only=True,
            trigger_signal="mark",
        )

        assert res.success is True
        assert captured_data.get("orderType") == "stp"
        assert captured_data.get("symbol") == "PF_XBTUSD"
        assert captured_data.get("side") == "sell"
        assert captured_data.get("size") == "5"
        assert captured_data.get("stopPrice") == "55000.0"
        assert captured_data.get("reduceOnly") is True
        assert captured_data.get("triggerSignal") == "mark"

    @pytest.mark.asyncio
    async def test_take_profit_payload_compliance(self):
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key="key", api_secret="c2VjcmV0")
        
        captured_data = {}
        async def fake_request(method, endpoint, params=None, data=None, authenticated=True):
            captured_data.update(data or {})
            return {"result": "success", "sendStatus": {"status": "placed", "order_id": "TP-ORD-999"}}

        client._request = fake_request

        res = await client.place_order(
            symbol="PF_XBTUSD",
            side="sell",
            order_type="take_profit",
            size=5.0,
            stop_price=65000.0,
            reduce_only=True,
            trigger_signal="mark",
        )

        assert res.success is True
        assert captured_data.get("orderType") == "take_profit"
        assert captured_data.get("stopPrice") == "65000.0"
        assert captured_data.get("reduceOnly") is True
        assert captured_data.get("triggerSignal") == "mark"


