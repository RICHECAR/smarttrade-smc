"""
Tests for backend.kraken_contracts — Centralized Kraken Futures contract model.
═══════════════════════════════════════════════════════════════════════════════

Covers:
  1. Coin → contract conversion (ensure_contracts, coin_to_contracts)
  2. Invalid size blocked (validate_order)
  3. Reduce-only rules
  4. SL/TP validation
  5. Dispatch validation via executor wrapper
  6. Error taxonomy mapping
  7. Order normalization
  8. Symbol mapping roundtrip

Run: pytest tests/test_kraken_contracts_model.py -v
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.kraken_contracts import (
    ensure_contracts,
    coin_to_contracts,
    validate_order,
    validate_sl_tp,
    classify_kraken_error,
    normalize_order_response,
    get_contract_specs,
    to_kraken_symbol,
    from_kraken_symbol,
    KRAKEN_CONTRACT_SPECS,
    KRAKEN_STATUS_MAP,
)


# ═══════════════════════════════════════════════════════════════
#  1. COIN → CONTRACT CONVERSION (LINEAR MULTI-COLLATERAL)
# ═══════════════════════════════════════════════════════════════

class TestCoinToContractConversion:
    """Test ensure_contracts and coin_to_contracts in native linear units."""

    def test_fractional_btc_with_price(self):
        """0.003042 BTC floored to 4 dec -> 0.003 BTC (native crypto units)."""
        result = ensure_contracts(0.003042, "PF_XBTUSD", price=87000.0)
        assert result == 0.003
        assert isinstance(result, float)

    def test_fractional_eth_with_price(self):
        """0.1234 ETH floored to 3 dec -> 0.123 ETH."""
        result = ensure_contracts(0.1234, "PF_ETHUSD", price=3500.0)
        assert result == 0.123
        assert isinstance(result, float)

    def test_integer_passthrough(self):
        """250 XRP (0 precision) -> 250 integer."""
        result = ensure_contracts(250, "PF_XRPUSD")
        assert result == 250
        assert isinstance(result, int)

    def test_integer_float_passthrough(self):
        """250.0 XRP -> 250 (int)."""
        result = ensure_contracts(250.0, "PF_XRPUSD")
        assert result == 250
        assert isinstance(result, int)

    def test_tiny_qty_no_price_returns_min(self):
        """0.00001 BTC < 0.0001 min -> floors below min."""
        result = ensure_contracts(0.00001, "PF_XBTUSD")
        assert result < 0.0001

    def test_coin_to_contracts_basic(self):
        """Explicit coin_to_contracts: 0.5 BTC -> 0.5 BTC."""
        result = coin_to_contracts(0.5, 60000.0, "PF_XBTUSD")
        assert result == 0.5
        assert isinstance(result, float)

    def test_coin_to_contracts_small_value(self):
        """coin_to_contracts: small value -> floors correctly."""
        result = coin_to_contracts(0.00001, 100.0, "PF_XBTUSD")
        assert result < 0.0001

    def test_coin_to_contracts_zero_price(self):
        """Zero price -> preserved native units."""
        result = coin_to_contracts(1.0, 0.0, "PF_XBTUSD")
        assert result == 1.0

    def test_coin_to_contracts_negative_qty(self):
        """Negative qty -> 0.0."""
        result = coin_to_contracts(-1.0, 87000.0, "PF_XBTUSD")
        assert result == 0.0

    def test_large_quantity_passthrough(self):
        """Valid quantity -> passthrough."""
        result = ensure_contracts(5.5, "PF_ETHUSD")
        assert result == 5.5

    def test_result_always_int(self):
        """XRP and DOGE (precision 0) return int, others return float."""
        cases = [
            (0.003, "PF_XBTUSD", 87000.0, float),
            (2.5, "PF_ETHUSD", 0.0, float),
            (1.55, "PF_SOLUSD", 0.0, float),
            (10.5, "PF_XRPUSD", 50.0, int),
        ]
        for qty, sym, price, exp_type in cases:
            result = ensure_contracts(qty, sym, price=price)
            assert isinstance(result, exp_type), f"ensure_contracts({qty}, {sym}) returned {type(result)}, expected {exp_type}"


# ═══════════════════════════════════════════════════════════════
#  2. INVALID SIZE BLOCKED
# ═══════════════════════════════════════════════════════════════

class TestInvalidSizeBlocked:
    """Test validate_order blocks invalid sizes."""

    def test_zero_contracts_blocked(self):
        valid, err = validate_order("PF_XBTUSD", "buy", 0)
        assert not valid
        assert "INVALID_SIZE" in err

    def test_negative_contracts_blocked(self):
        valid, err = validate_order("PF_XBTUSD", "buy", -5)
        assert not valid
        assert "INVALID_SIZE" in err

    def test_non_numeric_contracts_blocked(self):
        """validate_order requires number, passing string should fail."""
        valid, err = validate_order("PF_XBTUSD", "buy", "invalid")
        assert not valid
        assert "INVALID_SIZE" in err

    def test_valid_contracts_pass(self):
        valid, err = validate_order("PF_XBTUSD", "buy", 0.01)
        assert valid
        assert err is None

    def test_min_contract_validates(self):
        valid, err = validate_order("PF_XBTUSD", "buy", 0.0001)
        assert valid

    def test_invalid_side_blocked(self):
        valid, err = validate_order("PF_XBTUSD", "long", 0.01)
        assert not valid
        assert "INVALID_SIDE" in err

    def test_limit_without_price_blocked(self):
        valid, err = validate_order("PF_XBTUSD", "buy", 0.01, order_type="limit", price=0.0)
        assert not valid
        assert "INVALID_PRICE" in err

    def test_limit_with_price_passes(self):
        valid, err = validate_order("PF_XBTUSD", "buy", 0.01, order_type="limit", price=87000.0)
        assert valid

    def test_notional_cap_exceeded(self):
        valid, err = validate_order("PF_XBTUSD", "buy", 2.0, price=87000.0, max_notional_usd=100_000)
        assert not valid
        assert "NOTIONAL_TOO_HIGH" in err

    def test_notional_cap_ok(self):
        valid, err = validate_order("PF_XBTUSD", "buy", 0.5, price=87000.0, max_notional_usd=100_000)
        assert valid


# ═══════════════════════════════════════════════════════════════
#  3. REDUCE-ONLY RULES
# ═══════════════════════════════════════════════════════════════

class TestReduceOnlyRules:
    """Test reduce_only/intent coherence validation."""

    def test_reduce_only_on_open_blocked(self):
        valid, err = validate_order("PF_XBTUSD", "buy", 100, reduce_only=True, intent="open")
        assert not valid
        assert "REDUCE_ONLY_ON_OPEN" in err

    def test_reduce_only_on_close_passes(self):
        valid, err = validate_order("PF_XBTUSD", "sell", 100, reduce_only=True, intent="close")
        assert valid

    def test_no_reduce_only_on_open_passes(self):
        valid, err = validate_order("PF_XBTUSD", "buy", 100, reduce_only=False, intent="open")
        assert valid


# ═══════════════════════════════════════════════════════════════
#  4. SL/TP VALIDATION
# ═══════════════════════════════════════════════════════════════

class TestSlTpValidation:
    """Test SL/TP price coherence checks."""

    # Long side
    def test_long_valid_sl_tp(self):
        valid, err = validate_sl_tp(entry_price=100.0, stop_loss=95.0, take_profit=110.0, side="buy")
        assert valid

    def test_long_sl_above_entry_blocked(self):
        valid, err = validate_sl_tp(entry_price=100.0, stop_loss=105.0, take_profit=None, side="buy")
        assert not valid
        assert "SL_INVALID_LONG" in err

    def test_long_tp_below_entry_blocked(self):
        valid, err = validate_sl_tp(entry_price=100.0, stop_loss=None, take_profit=90.0, side="buy")
        assert not valid
        assert "TP_INVALID_LONG" in err

    def test_long_sl_at_entry_blocked(self):
        """SL == entry is invalid (would trigger immediately)."""
        valid, err = validate_sl_tp(entry_price=100.0, stop_loss=100.0, take_profit=None, side="buy")
        assert not valid

    # Short side
    def test_short_valid_sl_tp(self):
        valid, err = validate_sl_tp(entry_price=100.0, stop_loss=105.0, take_profit=90.0, side="sell")
        assert valid

    def test_short_sl_below_entry_blocked(self):
        valid, err = validate_sl_tp(entry_price=100.0, stop_loss=95.0, take_profit=None, side="sell")
        assert not valid
        assert "SL_INVALID_SHORT" in err

    def test_short_tp_above_entry_blocked(self):
        valid, err = validate_sl_tp(entry_price=100.0, stop_loss=None, take_profit=110.0, side="sell")
        assert not valid
        assert "TP_INVALID_SHORT" in err

    # Edge cases
    def test_no_sl_no_tp_valid(self):
        valid, err = validate_sl_tp(entry_price=100.0, stop_loss=None, take_profit=None, side="buy")
        assert valid

    def test_zero_sl_ignored(self):
        """SL=0 should be treated as absent (not validated)."""
        valid, err = validate_sl_tp(entry_price=100.0, stop_loss=0, take_profit=None, side="buy")
        assert valid

    def test_side_aliases(self):
        """'long' and 'short' aliases work."""
        valid, _ = validate_sl_tp(entry_price=100.0, stop_loss=95.0, take_profit=None, side="long")
        assert valid
        valid, _ = validate_sl_tp(entry_price=100.0, stop_loss=105.0, take_profit=None, side="short")
        assert valid


# ═══════════════════════════════════════════════════════════════
#  5. DISPATCH VALIDATION (executor symbol mapping)
# ═══════════════════════════════════════════════════════════════

class TestDispatchSymbolMapping:
    """Test symbol mapping consistency between kraken_contracts and executor."""

    def test_executor_symbol_mapping_matches_contracts(self):
        from backend.execution.kraken_futures_executor import (
            to_kraken_symbol as exec_to_kraken,
            SYMBOL_MAP_TO_KRAKEN,
        )
        # Every symbol in executor must resolve the same as in kraken_contracts
        for bitget_sym, expected in SYMBOL_MAP_TO_KRAKEN.items():
            assert exec_to_kraken(bitget_sym) == to_kraken_symbol(bitget_sym), \
                f"Mismatch for {bitget_sym}"

    def test_already_kraken_format_no_double_prefix(self):
        """PF_XBTUSD → PF_XBTUSD (not PF_PF_XBTUSD)."""
        assert to_kraken_symbol("PF_XBTUSD") == "PF_XBTUSD"
        assert to_kraken_symbol("PI_XBTUSD") == "PI_XBTUSD"

    def test_roundtrip_all_symbols(self):
        """from_kraken(to_kraken(x)) == x for all mapped symbols."""
        from backend.kraken_contracts import BITGET_TO_KRAKEN
        for bitget_sym in BITGET_TO_KRAKEN:
            kraken_sym = to_kraken_symbol(bitget_sym)
            back = from_kraken_symbol(kraken_sym)
            assert back == bitget_sym, f"Roundtrip failed: {bitget_sym} → {kraken_sym} → {back}"

    def test_case_insensitive(self):
        """Symbol mapping is case-insensitive."""
        assert to_kraken_symbol("btcusdt") == "PF_XBTUSD"
        assert to_kraken_symbol("BtCuSdT") == "PF_XBTUSD"

    def test_format_variations(self):
        """Handle BTC/USDT, BTC:USDT, etc."""
        assert to_kraken_symbol("BTC/USDT") == "PF_XBTUSD"
        assert to_kraken_symbol("BTC:USDT") == "PF_XBTUSD"
        assert to_kraken_symbol("BTC-USDT") == "PF_XBTUSD"


# ═══════════════════════════════════════════════════════════════
#  6. ERROR TAXONOMY MAPPING
# ═══════════════════════════════════════════════════════════════

class TestErrorTaxonomyMapping:
    """Test Kraken error status → normalized error code mapping."""

    def test_invalid_size(self):
        assert classify_kraken_error("invalidSize") == "INVALID_SIZE"

    def test_insufficient_funds(self):
        assert classify_kraken_error("insufficientFunds") == "INSUFFICIENT_FUNDS"

    def test_insufficient_margin(self):
        assert classify_kraken_error("insufficientMargin") == "INSUFFICIENT_MARGIN"

    def test_rate_limited(self):
        assert classify_kraken_error("rateLimitExceeded") == "RATE_LIMITED"
        assert classify_kraken_error("apiLimitExceeded") == "RATE_LIMITED"

    def test_success_statuses(self):
        assert classify_kraken_error("placed") == "SUCCESS"
        assert classify_kraken_error("filled") == "SUCCESS"
        assert classify_kraken_error("partiallyFilled") == "SUCCESS"

    def test_unknown_status_message_fallback(self):
        """Unknown status but message contains 'size' → INVALID_SIZE."""
        assert classify_kraken_error("unknownStatus", "invalid size parameter") == "INVALID_SIZE"

    def test_unknown_status_no_message(self):
        """Completely unknown → ORDER_REJECTED."""
        assert classify_kraken_error("bizarre_status") == "ORDER_REJECTED"

    def test_all_mapped_statuses_covered(self):
        """Every key in KRAKEN_STATUS_MAP returns its value."""
        for status, expected in KRAKEN_STATUS_MAP.items():
            assert classify_kraken_error(status) == expected

    def test_market_suspended(self):
        assert classify_kraken_error("marketSuspended") == "MARKET_CLOSED"

    def test_api_key_issues(self):
        assert classify_kraken_error("accountInactive") == "API_KEY_INVALID"
        assert classify_kraken_error("apiKeyNotFound") == "API_KEY_MISSING"


# ═══════════════════════════════════════════════════════════════
#  7. ORDER NORMALIZATION
# ═══════════════════════════════════════════════════════════════

class TestOrderNormalization:
    """Test normalize_order_response for success and error paths."""

    def test_success_simple(self):
        result = normalize_order_response({"success": True, "order_id": "abc123"})
        assert result["success"] is True
        assert result["order_id"] == "abc123"
        assert result["error_code"] is None

    def test_success_with_code(self):
        result = normalize_order_response({"code": "00000", "data": {"orderId": "xyz"}})
        assert result["success"] is True
        assert result["order_id"] == "xyz"

    def test_success_from_send_status(self):
        result = normalize_order_response({
            "success": True,
            "sendStatus": {
                "order_id": "kraken-001",
                "filledSize": 100,
                "avgFillPrice": 87500.0,
            },
        })
        assert result["success"] is True
        assert result["order_id"] == "kraken-001"
        assert result["filled_qty"] == 100
        assert result["avg_price"] == 87500.0

    def test_error_response(self):
        result = normalize_order_response({
            "success": False,
            "status": "invalidSize",
            "error": "Size too small",
        })
        assert result["success"] is False
        assert result["error_code"] == "INVALID_SIZE"
        assert "Size too small" in result["raw_message"]

    def test_error_from_send_status(self):
        result = normalize_order_response({
            "sendStatus": {
                "status": "insufficientFunds",
                "errorMessage": "Not enough margin",
            },
        })
        assert result["success"] is False
        assert result["error_code"] == "INSUFFICIENT_FUNDS"

    def test_empty_response(self):
        result = normalize_order_response({})
        assert result["success"] is False

    def test_normalized_has_all_keys(self):
        """Every normalized response has the 6 standard keys."""
        expected_keys = {"success", "order_id", "filled_qty", "avg_price", "error_code", "raw_message"}
        for raw in [
            {"success": True, "order_id": "ok"},
            {"success": False, "error": "fail"},
            {},
        ]:
            result = normalize_order_response(raw)
            assert expected_keys.issubset(result.keys()), f"Missing keys in {result}"


# ═══════════════════════════════════════════════════════════════
#  8. CONTRACT SPECS & EDGE CASES
# ═══════════════════════════════════════════════════════════════

class TestContractSpecs:
    """Test contract spec lookup and defaults for Linear Multi-Collateral."""

    def test_known_symbol_specs(self):
        specs = get_contract_specs("PF_XBTUSD")
        assert specs["contract_type"] == "linear"
        assert specs["min_size"] == 0.0001
        assert specs["qty_precision"] == 4

    def test_bitget_format_resolved(self):
        specs = get_contract_specs("BTCUSDT")
        assert specs["contract_type"] == "linear"
        assert specs["min_size"] == 0.0001

    def test_unknown_symbol_gets_defaults(self):
        specs = get_contract_specs("PF_UNKNOWN")
        assert specs["contract_type"] == "linear"
        assert specs["min_size"] == 1.0

    def test_all_symbols_have_linear_contracts(self):
        """ALL Kraken Futures symbols have valid linear specifications."""
        for sym, specs in KRAKEN_CONTRACT_SPECS.items():
            assert specs["contract_type"] == "linear", f"{sym} not linear!"
            assert specs["min_size"] > 0, f"{sym} min_size <= 0!"
            assert specs["qty_precision"] >= 0, f"{sym} negative precision!"

    def test_15_symbols_defined(self):
        """At least 15 symbols are defined."""
        assert len(KRAKEN_CONTRACT_SPECS) >= 15
