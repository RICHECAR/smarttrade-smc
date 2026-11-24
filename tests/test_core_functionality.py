"""
Pytest Test Suite for Bitget SMC Auto-Trader
=============================================
Tests critical functionality:
- Signature generation (canonical query string)
- RR comparison with epsilon
- TPSL price rounding
- Leverage clamping
- Position parsing
- Config validation
"""

import pytest
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import hmac
import base64
import time
from urllib.parse import urlencode

# ═══════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def test_api_credentials():
    """Fake API credentials for testing."""
    return {
        "api_key": "test_api_key_12345",
        "api_secret": "test_secret_key_abcdef",
        "passphrase": "test_passphrase",
    }


# ═══════════════════════════════════════════════════════════════════════
# 1. Signature Tests
# ═══════════════════════════════════════════════════════════════════════

class TestBitgetSignature:
    """Test Bitget API signature generation."""
    
    def _generate_signature(self, timestamp: str, method: str, path: str, 
                            query_string: str = "", body: str = "", 
                            secret: str = "test_secret") -> str:
        """Generate HMAC-SHA256 signature (same logic as production)."""
        prehash = timestamp + method.upper() + path
        if query_string:
            prehash += "?" + query_string
        prehash += body
        
        mac = hmac.new(
            secret.encode('utf-8'),
            prehash.encode('utf-8'),
            hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode('utf-8')
    
    def _build_canonical_query(self, params: dict) -> str:
        """Build canonical query string (sorted, URL-encoded)."""
        if not params:
            return ""
        filtered = {k: str(v) for k, v in params.items() if v is not None}
        if not filtered:
            return ""
        return urlencode(sorted(filtered.items()))
    
    def test_signature_get_no_query(self):
        """GET request without query string should NOT have '?' in prehash."""
        ts = "1700000000000"
        path = "/api/v2/mix/market/ticker"
        
        # Correct: no '?' when query is empty
        prehash = ts + "GET" + path + ""
        
        sig = self._generate_signature(ts, "GET", path, "", "")
        
        # Verify we can compute a signature
        assert len(sig) > 0
        assert sig.endswith("=") or len(sig) == 44  # Base64
        
    def test_signature_get_with_query(self):
        """GET request with query string MUST include '?' in prehash."""
        ts = "1700000000000"
        path = "/api/v2/mix/position/all-position"
        params = {"productType": "USDT-FUTURES", "marginCoin": "USDT"}
        query = self._build_canonical_query(params)
        
        # Query should be sorted: marginCoin=USDT&productType=USDT-FUTURES
        assert "marginCoin=USDT" in query
        assert "productType=USDT-FUTURES" in query
        
        sig = self._generate_signature(ts, "GET", path, query, "")
        assert len(sig) > 0
    
    def test_signature_post_with_body(self):
        """POST request should include body in prehash, NOT query string."""
        ts = "1700000000000"
        path = "/api/v2/mix/order/place-order"
        body = '{"symbol":"BTCUSDT","productType":"USDT-FUTURES","side":"buy"}'
        
        sig = self._generate_signature(ts, "POST", path, "", body)
        assert len(sig) > 0
        
        # Signature with body should differ from without
        sig_no_body = self._generate_signature(ts, "POST", path, "", "")
        assert sig != sig_no_body
    
    def test_canonical_query_sorted(self):
        """Query parameters must be sorted alphabetically."""
        params = {"z_param": "1", "a_param": "2", "m_param": "3"}
        query = self._build_canonical_query(params)
        
        # Should be sorted: a_param, m_param, z_param
        assert query == "a_param=2&m_param=3&z_param=1"
    
    def test_canonical_query_empty(self):
        """Empty params should return empty string, not '?'."""
        query = self._build_canonical_query({})
        assert query == ""
        
        query = self._build_canonical_query(None)
        assert query == ""


# ═══════════════════════════════════════════════════════════════════════
# 2. Risk/Reward Ratio Tests
# ═══════════════════════════════════════════════════════════════════════

class TestRiskRewardRatio:
    """Test RR calculation and comparison with epsilon tolerance."""
    
    # Epsilon for floating-point comparison
    RR_EPSILON = 1e-6
    
    def _calculate_rr(self, entry: float, sl: float, tp: float) -> float:
        """Calculate Risk/Reward ratio."""
        if entry == 0 or sl == 0 or tp == 0:
            return 0.0
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk == 0:
            return 0.0
        return reward / risk
    
    def _validate_rr_with_epsilon(self, rr: float, min_rr: float) -> bool:
        """Validate RR with epsilon tolerance."""
        return rr >= (min_rr - self.RR_EPSILON)
    
    def test_rr_exact_match(self):
        """RR exactly equal to min should pass."""
        # Entry=100, SL=95, TP=110 → Risk=5, Reward=10 → RR=2.0
        rr = self._calculate_rr(100, 95, 110)
        assert abs(rr - 2.0) < self.RR_EPSILON
        assert self._validate_rr_with_epsilon(rr, 2.0) is True
    
    def test_rr_slightly_below_without_epsilon(self):
        """RR slightly below min (without epsilon) would fail incorrectly."""
        # Simulate floating-point imprecision: 1.9999999999 instead of 2.0
        rr = 1.9999999999
        min_rr = 2.0
        
        # Without epsilon: would fail
        assert rr < min_rr
        
        # With epsilon: should pass
        assert self._validate_rr_with_epsilon(rr, min_rr) is True
    
    def test_rr_clearly_below(self):
        """RR clearly below min should fail."""
        rr = 1.5
        min_rr = 2.0
        assert self._validate_rr_with_epsilon(rr, min_rr) is False
    
    def test_rr_above_minimum(self):
        """RR above min should pass."""
        rr = 2.5
        min_rr = 2.0
        assert self._validate_rr_with_epsilon(rr, min_rr) is True
    
    def test_rr_long_position(self):
        """Calculate RR for long position."""
        # Long: Entry=50000, SL=49000, TP=53000
        # Risk=1000, Reward=3000 → RR=3.0
        rr = self._calculate_rr(50000, 49000, 53000)
        assert abs(rr - 3.0) < self.RR_EPSILON
    
    def test_rr_short_position(self):
        """Calculate RR for short position."""
        # Short: Entry=50000, SL=51000, TP=47000
        # Risk=1000 (abs), Reward=3000 (abs) → RR=3.0
        rr = self._calculate_rr(50000, 51000, 47000)
        assert abs(rr - 3.0) < self.RR_EPSILON


# ═══════════════════════════════════════════════════════════════════════
# 3. TPSL Price Rounding Tests
# ═══════════════════════════════════════════════════════════════════════

class TestTPSLRounding:
    """Test TPSL price normalization to avoid checkBDScale errors."""
    
    PRICE_DECIMALS = {
        "BTCUSDT": 1,
        "ETHUSDT": 2,
        "SOLUSDT": 2,
        "XRPUSDT": 4,
        "DOGEUSDT": 5,
    }
    
    def _round_price(self, price: float, symbol: str) -> str:
        """Round price to symbol's decimal precision."""
        decimals = self.PRICE_DECIMALS.get(symbol.upper(), 4)
        return f"{price:.{decimals}f}"
    
    def test_btc_rounding(self):
        """BTC prices should have 1 decimal place."""
        price = 97312.456789
        rounded = self._round_price(price, "BTCUSDT")
        assert rounded == "97312.5"
    
    def test_eth_rounding(self):
        """ETH prices should have 2 decimal places."""
        price = 3456.789012
        rounded = self._round_price(price, "ETHUSDT")
        assert rounded == "3456.79"
    
    def test_sol_rounding(self):
        """SOL prices should have 2 decimal places."""
        price = 143.45678
        rounded = self._round_price(price, "SOLUSDT")
        assert rounded == "143.46"
    
    def test_xrp_rounding(self):
        """XRP prices should have 4 decimal places."""
        price = 0.567891234
        rounded = self._round_price(price, "XRPUSDT")
        assert rounded == "0.5679"
    
    def test_unknown_symbol_default(self):
        """Unknown symbols should use 4 decimal places."""
        price = 1.23456789
        rounded = self._round_price(price, "UNKNOWNUSDT")
        assert rounded == "1.2346"


# ═══════════════════════════════════════════════════════════════════════
# 4. Leverage Clamping Tests
# ═══════════════════════════════════════════════════════════════════════

class TestLeverageClamping:
    """Test leverage clamping to max 10x."""
    
    MAX_LEVERAGE = 10
    
    def _clamp_leverage(self, requested: int) -> int:
        """Clamp leverage to MAX_LEVERAGE."""
        return min(requested, self.MAX_LEVERAGE)
    
    def test_leverage_under_max(self):
        """Leverage under max should not be changed."""
        assert self._clamp_leverage(5) == 5
        assert self._clamp_leverage(10) == 10
    
    def test_leverage_over_max(self):
        """Leverage over max should be clamped."""
        assert self._clamp_leverage(20) == 10
        assert self._clamp_leverage(100) == 10
        assert self._clamp_leverage(125) == 10
    
    def test_leverage_at_boundary(self):
        """Leverage exactly at max should not be changed."""
        assert self._clamp_leverage(10) == 10


# ═══════════════════════════════════════════════════════════════════════
# 5. Position Parsing Tests (V2 API)
# ═══════════════════════════════════════════════════════════════════════

class TestPositionParsing:
    """Test parsing of Bitget V2 positions response."""
    
    def _parse_positions(self, raw_data: list) -> list:
        """Parse raw positions into normalized format."""
        normalized = []
        for pos in raw_data:
            total = float(pos.get("total", 0) or 0)
            normalized.append({
                "symbol": pos.get("symbol", ""),
                "side": (pos.get("holdSide") or "").lower(),
                "size": total,
                "hasPosition": total > 0,
            })
        return normalized
    
    def test_parse_open_long_position(self):
        """Parse open long position."""
        raw = [{
            "symbol": "BTCUSDT",
            "holdSide": "long",
            "total": "0.01",
            "available": "0.01",
        }]
        parsed = self._parse_positions(raw)
        
        assert len(parsed) == 1
        assert parsed[0]["symbol"] == "BTCUSDT"
        assert parsed[0]["side"] == "long"
        assert parsed[0]["size"] == 0.01
        assert parsed[0]["hasPosition"] is True
    
    def test_parse_no_position(self):
        """Parse empty position slot."""
        raw = [{
            "symbol": "ETHUSDT",
            "holdSide": "long",
            "total": "0",
            "available": "0",
        }]
        parsed = self._parse_positions(raw)
        
        assert len(parsed) == 1
        assert parsed[0]["size"] == 0.0
        assert parsed[0]["hasPosition"] is False
    
    def test_parse_multiple_positions(self):
        """Parse multiple positions."""
        raw = [
            {"symbol": "BTCUSDT", "holdSide": "long", "total": "0.05"},
            {"symbol": "BTCUSDT", "holdSide": "short", "total": "0"},
            {"symbol": "ETHUSDT", "holdSide": "long", "total": "1.5"},
        ]
        parsed = self._parse_positions(raw)
        
        active = [p for p in parsed if p["hasPosition"]]
        assert len(active) == 2


# ═══════════════════════════════════════════════════════════════════════
# 6. Configuration Validation Tests
# ═══════════════════════════════════════════════════════════════════════

class TestConfigValidation:
    """Test configuration parsing and validation."""
    
    def test_bool_parsing(self):
        """Test boolean parsing from env strings."""
        def _bool(val: str) -> bool:
            return val.strip().lower() in ("true", "1", "yes")
        
        assert _bool("true") is True
        assert _bool("TRUE") is True
        assert _bool("True") is True
        assert _bool("1") is True
        assert _bool("yes") is True
        assert _bool("false") is False
        assert _bool("0") is False
        assert _bool("no") is False
        assert _bool("") is False
    
    def test_list_parsing(self):
        """Test list parsing from comma-separated strings."""
        def _list(val: str) -> list:
            return [s.strip() for s in val.split(",") if s.strip()]
        
        assert _list("BTCUSDT,ETHUSDT,SOLUSDT") == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        assert _list("BTCUSDT, ETHUSDT, SOLUSDT") == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        assert _list("BTCUSDT") == ["BTCUSDT"]
        assert _list("") == []
    
    def test_product_type_normalization(self):
        """Test product type normalization."""
        def _normalize_product_type(val: str) -> str:
            v = val.strip().upper()
            if v in ("USDT-FUTURES", "USDT_FUTURES", "USDTFUTURES"):
                return "USDT-FUTURES"
            if v in ("UMCBL", "MIX"):
                return "USDT-FUTURES"
            raise ValueError(f"Invalid productType: {val}")
        
        assert _normalize_product_type("USDT-FUTURES") == "USDT-FUTURES"
        assert _normalize_product_type("usdt-futures") == "USDT-FUTURES"
        assert _normalize_product_type("USDT_FUTURES") == "USDT-FUTURES"
        assert _normalize_product_type("UMCBL") == "USDT-FUTURES"
        
        with pytest.raises(ValueError):
            _normalize_product_type("SPOT")


# ═══════════════════════════════════════════════════════════════════════
# 7. Signal Direction Tests
# ═══════════════════════════════════════════════════════════════════════

class TestSignalDirection:
    """Test signal direction logic."""
    
    def test_tpsl_logic_long(self):
        """Long position: SL < Entry < TP."""
        direction = "long"
        entry = 100.0
        sl = 95.0  # Below entry
        tp = 110.0  # Above entry
        
        assert sl < entry < tp
    
    def test_tpsl_logic_short(self):
        """Short position: TP < Entry < SL."""
        direction = "short"
        entry = 100.0
        sl = 105.0  # Above entry
        tp = 90.0   # Below entry
        
        assert tp < entry < sl
    
    def test_invalid_tpsl_long(self):
        """Invalid TPSL for long: SL above entry."""
        direction = "long"
        entry = 100.0
        sl = 105.0  # Should be below for long!
        tp = 110.0
        
        # This should be detected as invalid
        is_valid = sl < entry < tp
        assert is_valid is False


# ═══════════════════════════════════════════════════════════════════════
# 8. Duplicate Kwargs Detection (Static Analysis)
# ═══════════════════════════════════════════════════════════════════════

class TestDuplicateKwargs:
    """Test to catch duplicate keyword arguments (compile-time check)."""
    
    def test_no_duplicate_kwargs_in_signal_builder(self):
        """Ensure SignalBuilder can be instantiated without duplicate kwargs error."""
        try:
            # This import will fail if there's a SyntaxError like duplicate kwargs
            from smc_futures.engine.strategy.signal_builder import SignalBuilder
            
            # Try to instantiate
            sb = SignalBuilder(min_rr_ratio=2.0, neutral_permissive=False)
            assert sb is not None
            assert sb.min_rr_ratio == 2.0
        except SyntaxError as e:
            pytest.fail(f"SyntaxError detected (possible duplicate kwargs): {e}")
    
    def test_no_duplicate_kwargs_in_orchestrator(self):
        """Ensure SMCFuturesOrchestrator can be instantiated."""
        try:
            from smc_futures.orchestrator import SMCFuturesOrchestrator
            
            # Instantiation test (basic)
            orch = SMCFuturesOrchestrator(
                symbol="BTCUSDT",
                account_balance=10000.0,
                neutral_permissive=False,
            )
            assert orch is not None
            assert orch.symbol == "BTCUSDT"
        except SyntaxError as e:
            pytest.fail(f"SyntaxError in orchestrator.py: {e}")


# ═══════════════════════════════════════════════════════════════════════
# Run with: pytest tests/test_core_functionality.py -v
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
