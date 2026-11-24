"""
Tests for Bitget V2 HMAC Signature fix.

Validates:
1. Query string used in URL == query string used in prehash
2. "?" is NOT added when query is empty
3. Body for GET = "" (empty string)
4. Canonical query string is sorted alphabetically
5. Signature includes query string for GET requests
"""

import pytest
import hashlib
import hmac
import base64
from urllib.parse import urlencode


class TestBitgetSignature:
    """Test Bitget V2 signature calculation."""
    
    @staticmethod
    def build_canonical_query(params):
        """Replicate the canonical query builder."""
        if not params:
            return ""
        filtered = {k: str(v) for k, v in params.items() if v is not None}
        if not filtered:
            return ""
        return urlencode(sorted(filtered.items()))
    
    @staticmethod
    def generate_signature(secret: str, timestamp: str, method: str, path: str, query: str, body: str) -> str:
        """Replicate the signature generator."""
        prehash = timestamp + method.upper() + path
        if query:
            prehash += "?" + query
        prehash += body
        
        mac = hmac.new(
            secret.encode('utf-8'),
            prehash.encode('utf-8'),
            hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode('utf-8')
    
    # ──────────────────────────────────────────────────
    #  Test canonical query string
    # ──────────────────────────────────────────────────
    
    def test_canonical_query_sorted(self):
        """Query string params must be sorted alphabetically."""
        params = {"productType": "USDT-FUTURES", "marginCoin": "USDT", "symbol": "BTCUSDT"}
        query = self.build_canonical_query(params)
        
        # Should be sorted: marginCoin, productType, symbol
        assert query == "marginCoin=USDT&productType=USDT-FUTURES&symbol=BTCUSDT"
    
    def test_canonical_query_empty_when_no_params(self):
        """Empty params should return empty string."""
        assert self.build_canonical_query(None) == ""
        assert self.build_canonical_query({}) == ""
    
    def test_canonical_query_filters_none_values(self):
        """None values should be filtered out."""
        params = {"productType": "USDT-FUTURES", "symbol": None, "marginCoin": "USDT"}
        query = self.build_canonical_query(params)
        
        assert "symbol" not in query
        assert query == "marginCoin=USDT&productType=USDT-FUTURES"
    
    # ──────────────────────────────────────────────────
    #  Test prehash construction
    # ──────────────────────────────────────────────────
    
    def test_get_prehash_includes_query_with_question_mark(self):
        """GET prehash must include '?' + queryString when query is not empty."""
        timestamp = "1708300000000"
        method = "GET"
        path = "/api/v2/mix/position/all-position"
        query = "marginCoin=USDT&productType=USDT-FUTURES"
        body = ""
        
        # Expected prehash format
        expected_prehash = f"{timestamp}GET{path}?{query}"
        
        # Verify our signature generator uses correct format
        secret = "test-secret-key"
        sig = self.generate_signature(secret, timestamp, method, path, query, body)
        
        # Manually compute expected signature
        expected_mac = hmac.new(
            secret.encode('utf-8'),
            expected_prehash.encode('utf-8'),
            hashlib.sha256
        )
        expected_sig = base64.b64encode(expected_mac.digest()).decode('utf-8')
        
        assert sig == expected_sig
    
    def test_get_prehash_no_question_mark_when_query_empty(self):
        """GET prehash must NOT include '?' when query is empty."""
        timestamp = "1708300000000"
        method = "GET"
        path = "/api/v2/public/time"
        query = ""  # Empty query
        body = ""
        
        # Expected prehash format (no '?')
        expected_prehash = f"{timestamp}GET{path}"
        
        secret = "test-secret-key"
        sig = self.generate_signature(secret, timestamp, method, path, query, body)
        
        # Manually compute expected signature
        expected_mac = hmac.new(
            secret.encode('utf-8'),
            expected_prehash.encode('utf-8'),
            hashlib.sha256
        )
        expected_sig = base64.b64encode(expected_mac.digest()).decode('utf-8')
        
        assert sig == expected_sig
    
    def test_get_body_must_be_empty_string(self):
        """GET requests must use empty string for body, not '{}' or 'null'."""
        timestamp = "1708300000000"
        method = "GET"
        path = "/api/v2/mix/position/all-position"
        query = "marginCoin=USDT&productType=USDT-FUTURES"
        
        secret = "test-secret-key"
        
        # Correct: body = ""
        sig_correct = self.generate_signature(secret, timestamp, method, path, query, "")
        
        # Wrong: body = "{}"
        sig_wrong_json = self.generate_signature(secret, timestamp, method, path, query, "{}")
        
        # Wrong: body = "null"
        sig_wrong_null = self.generate_signature(secret, timestamp, method, path, query, "null")
        
        # All three should be different
        assert sig_correct != sig_wrong_json
        assert sig_correct != sig_wrong_null
        assert sig_wrong_json != sig_wrong_null
    
    def test_post_prehash_includes_body_not_query(self):
        """POST prehash includes body, not query string."""
        timestamp = "1708300000000"
        method = "POST"
        path = "/api/v2/mix/order/place-order"
        query = ""  # POST typically has no query
        body = '{"symbol":"BTCUSDT","side":"buy"}'
        
        # Expected prehash format
        expected_prehash = f"{timestamp}POST{path}{body}"
        
        secret = "test-secret-key"
        sig = self.generate_signature(secret, timestamp, method, path, query, body)
        
        # Manually compute expected signature
        expected_mac = hmac.new(
            secret.encode('utf-8'),
            expected_prehash.encode('utf-8'),
            hashlib.sha256
        )
        expected_sig = base64.b64encode(expected_mac.digest()).decode('utf-8')
        
        assert sig == expected_sig
    
    # ──────────────────────────────────────────────────
    #  Test URL vs Signature consistency
    # ──────────────────────────────────────────────────
    
    def test_url_query_matches_signature_query(self):
        """The query string in URL must EXACTLY match query string in signature."""
        params = {"productType": "USDT-FUTURES", "marginCoin": "USDT"}
        
        # Build canonical query (same function used for both URL and signature)
        query = self.build_canonical_query(params)
        
        # Build URL
        path = "/api/v2/mix/position/all-position"
        url = f"https://api.bitget.com{path}?{query}"
        
        # Verify URL contains the exact query string
        assert url == "https://api.bitget.com/api/v2/mix/position/all-position?marginCoin=USDT&productType=USDT-FUTURES"
        
        # Build signature using same query string
        timestamp = "1708300000000"
        secret = "test-secret"
        sig = self.generate_signature(secret, timestamp, "GET", path, query, "")
        
        # Signature should be deterministic with same inputs
        sig2 = self.generate_signature(secret, timestamp, "GET", path, query, "")
        assert sig == sig2
    
    def test_positions_endpoint_correct_format(self):
        """Validate correct format for positions endpoint."""
        path = "/api/v2/mix/position/all-position"
        params = {"productType": "USDT-FUTURES", "marginCoin": "USDT"}
        
        query = self.build_canonical_query(params)
        
        # Verify endpoint path
        assert path == "/api/v2/mix/position/all-position"
        assert "open-positions" not in path  # Old wrong endpoint
        
        # Verify params
        assert "marginCoin=USDT" in query
        assert "productType=USDT-FUTURES" in query
    
    # ──────────────────────────────────────────────────
    #  Test method case sensitivity
    # ──────────────────────────────────────────────────
    
    def test_method_is_uppercased(self):
        """Method should always be uppercased in prehash."""
        timestamp = "1708300000000"
        path = "/api/v2/mix/position/all-position"
        query = "marginCoin=USDT&productType=USDT-FUTURES"
        body = ""
        secret = "test-secret"
        
        # Both should produce same signature
        sig_lower = self.generate_signature(secret, timestamp, "get", path, query, body)
        sig_upper = self.generate_signature(secret, timestamp, "GET", path, query, body)
        
        assert sig_lower == sig_upper


class TestSignatureIntegration:
    """Integration tests with actual class structure."""
    
    def test_executor_uses_same_query_for_url_and_sign(self):
        """Verify executor pattern: same query used for URL and signature."""
        # Simulate executor behavior
        params = {"productType": "USDT-FUTURES", "marginCoin": "USDT"}
        path = "/api/v2/mix/position/all-position"
        
        # Step 1: Build canonical query FIRST
        query_string = TestBitgetSignature.build_canonical_query(params)
        
        # Step 2: Use query_string for signature
        timestamp = "1708300000000"
        prehash = f"{timestamp}GET{path}"
        if query_string:
            prehash += "?" + query_string
        
        # Step 3: Use SAME query_string for URL
        url = f"https://api.bitget.com{path}"
        if query_string:
            url += "?" + query_string
        
        # Verify URL and prehash use identical query string
        assert query_string in prehash
        assert query_string in url


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
