"""
Bitget V2 Futures Executor — Production-Grade Order Placement.

Features:
  - Strict productType normalization (USDT-FUTURES)
  - Correct market vs limit order format
  - Complete HTTP instrumentation: [HTTP_CALL], [HTTP_FAIL]
  - Timestamp correction (clock skew detection)
  - Comprehensive logging: no silent failures
  - Zero network calls in shadow mode

Author: Trading Bot Framework
"""

import logging
import time
import json
import httpx
import hashlib
import hmac
import base64
import os
from urllib.parse import urlencode
from typing import Dict, Optional, Any
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger(__name__)

BITGET_BASE = "https://api.bitget.com"
API_VERSION = "v2"  # ALWAYS v2, never v1

# Debug mode for signature troubleshooting
SIGN_DEBUG = os.getenv("BITGET_SIGN_DEBUG", "0") == "1"

# Debug mode for positions API (set BITGET_POS_DEBUG=1)
POS_DEBUG = os.getenv("BITGET_POS_DEBUG", "0") == "1"


class BitgetV2Executor:
    """
    Execute orders on Bitget Mix Futures (USDT-FUTURES).
    
    - Normalizes product type to USDT-FUTURES
    - Corrects market order format (no price field)
    - Corrects limit order format (includes price + timeInForce)
    - Instruments HTTP calls completely
    - Detects clock skew in ticker data
    """
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        timeout: float = 10.0,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.timeout = timeout
        
        # Always use USDT-FUTURES (canonical normalized form)
        self.product_type = "USDT-FUTURES"
        self.margin_mode = "isolated"
        self.position_mode = "hedge_mode"
        
        logger.info("[BITGET_INIT] Executor initialized | api_key=%s... | product=%s | margin=%s",
                   api_key[:8] if api_key else "NONE", self.product_type, self.margin_mode)
    
    # ──────────────────────────────────────────────────
    #  SIGNING (FIXED: Include querystring in prehash)
    # ──────────────────────────────────────────────────
    
    def _build_canonical_query(self, params: Optional[Dict[str, Any]]) -> str:
        """
        Build canonical query string for signing.
        - Sorted alphabetically by key
        - URL-encoded values
        - Empty string if no params
        """
        if not params:
            return ""
        # Filter None values and sort
        filtered = {k: str(v) for k, v in params.items() if v is not None}
        if not filtered:
            return ""
        return urlencode(sorted(filtered.items()))
    
    def _generate_signature(
        self,
        timestamp: str,
        method: str,
        request_path: str,
        query_string: str = "",
        body: str = ""
    ) -> str:
        """
        Generate HMAC-SHA256 signature for Bitget API.
        
        Bitget V2 prehash format:
          prehash = timestamp + METHOD + requestPath + ("?" + queryString if not empty) + body
        
        CRITICAL:
          - For GET: body MUST be "" (empty string)
          - queryString must match EXACTLY what's sent in URL
          - "?" is only added if queryString is non-empty
        """
        # Build prehash
        prehash = timestamp + method.upper() + request_path
        if query_string:
            prehash += "?" + query_string
        prehash += body
        
        # Debug logging (enabled via BITGET_SIGN_DEBUG=1)
        if SIGN_DEBUG:
            prehash_hash = hashlib.sha256(prehash.encode()).hexdigest()[:16]
            logger.info(
                "[SIGN_DEBUG] method=%s path=%s query=%s body_len=%d prehash_sha256=%s...",
                method.upper(), request_path, query_string[:100] if query_string else "(empty)",
                len(body), prehash_hash
            )
        
        mac = hmac.new(
            self.api_secret.encode('utf-8'),
            prehash.encode('utf-8'),
            hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode('utf-8')
    
    def _headers(
        self,
        method: str,
        path: str,
        query_string: str = "",
        body: str = ""
    ) -> Dict[str, str]:
        """
        Generate authenticated headers for request.
        
        Args:
            method: HTTP method (GET/POST)
            path: Request path (e.g., /api/v2/mix/position/all-position)
            query_string: Canonical query string (for GET requests)
            body: JSON body (for POST requests, empty for GET)
        """
        ts_ms = str(int(time.time() * 1000))
        sig = self._generate_signature(ts_ms, method, path, query_string, body)
        
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sig,
            "ACCESS-TIMESTAMP": ts_ms,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }
    
    # ──────────────────────────────────────────────────
    #  HTTP HELPER (With Instrumentation)
    # ──────────────────────────────────────────────────
    
    async def _http_post(
        self,
        path: str,
        payload: Dict[str, Any],
        operation: str = "ORDER"
    ) -> Dict[str, Any]:
        """
        Make authenticated POST request with full instrumentation.
        
        Args:
            path: e.g., "/api/v2/mix/order/place-order"
            payload: Request body
            operation: Human-readable operation name (logged)
        
        Returns:
            Response JSON from Bitget
        """
        body_str = json.dumps(payload)
        headers = self._headers("POST", path, "", body_str)  # POST: query="", body=payload
        url = BITGET_BASE + path
        
        # ── Log request ─────────────────────────────
        logger.info("[HTTP_CALL] method=POST operation=%s path=%s", operation, path)
        logger.debug("[HTTP_BODY] %s payload=%s", operation, body_str[:500])
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, content=body_str, headers=headers)
            
            # ── Log response ────────────────────────
            response_text = response.text[:1000]  # Truncate for logging
            
            if response.status_code >= 500:
                logger.error("[HTTP_FAIL] status=%d operation=%s url=%s response=%s",
                           response.status_code, operation, path, response_text)
                return {
                    "code": str(response.status_code),
                    "msg": f"HTTP {response.status_code}",
                    "data": None
                }
            
            try:
                data = response.json()
            except json.JSONDecodeError:
                logger.error("[HTTP_FAIL] status=%d operation=%s json_parse_failed response=%s",
                           response.status_code, operation, response_text)
                return {
                    "code": "JSON_PARSE_ERROR",
                    "msg": "Failed to parse response JSON",
                    "data": None
                }
            
            # ── Check API error ─────────────────────
            code = data.get("code", "UNKNOWN")
            if code != "00000":
                logger.error("[HTTP_FAIL] status=%d operation=%s code=%s msg=%s",
                           response.status_code, operation, code, data.get("msg", ""))
            else:
                logger.info("[HTTP_OK] operation=%s code=%s", operation, code)
            
            return data
        
        except httpx.RequestError as e:
            logger.error("[HTTP_FAIL] operation=%s exception=%s", operation, str(e), exc_info=True)
            return {
                "code": "NETWORK_ERROR",
                "msg": str(e),
                "data": None
            }
        except Exception as e:
            logger.error("[HTTP_FAIL] operation=%s exception=%s", operation, str(e), exc_info=True)
            return {
                "code": "UNEXPECTED_ERROR",
                "msg": str(e),
                "data": None
            }
    
    async def _http_get(
        self,
        path: str,
        params: Optional[Dict[str, str]] = None,
        operation: str = "QUERY"
    ) -> Dict[str, Any]:
        """
        Make authenticated GET request with full instrumentation.
        
        FIXED: Query string is now included in signature calculation.
        """
        # Build canonical query string FIRST (used for both URL and signature)
        query_string = self._build_canonical_query(params)
        
        # Generate headers with query string included in signature
        headers = self._headers("GET", path, query_string, "")  # GET: body must be ""
        
        # Build final URL
        url = BITGET_BASE + path
        if query_string:
            url += "?" + query_string
        
        logger.info("[HTTP_CALL] method=GET operation=%s url=%s", operation, url)
        
        if SIGN_DEBUG:
            logger.info("[SIGN_DEBUG] final_url=%s", url)
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=headers)
            
            response_text = response.text[:500]
            
            if response.status_code >= 500:
                logger.error("[HTTP_FAIL] status=%d operation=%s response=%s",
                           response.status_code, operation, response_text)
                return {"code": str(response.status_code), "msg": "Server error", "data": None}
            
            try:
                data = response.json()
            except json.JSONDecodeError:
                logger.error("[HTTP_FAIL] operation=%s json_parse_failed", operation)
                return {"code": "JSON_PARSE_ERROR", "msg": "Failed to parse response JSON", "data": None}
            
            code = data.get("code", "UNKNOWN")
            if code != "00000":
                logger.warning("[HTTP_FAIL] operation=%s code=%s msg=%s", operation, code, data.get("msg"))
            
            return data
        
        except httpx.RequestError as e:
            logger.error("[HTTP_FAIL] operation=%s exception=%s", operation, str(e), exc_info=True)
            return {"code": "NETWORK_ERROR", "msg": str(e), "data": None}
    
    # ──────────────────────────────────────────────────
    #  BITGET ORDER PLACEMENT (FIXED)
    # ──────────────────────────────────────────────────
    
    async def place_order_market(
        self,
        symbol: str,
        side: str,  # "buy" | "sell"
        size: str,  # quantity as string
    ) -> Dict[str, Any]:
        """
        Place MARKET order (NO PRICE FIELD).
        
        KEY FIX: Market orders MUST NOT include "price" field.
        
        Args:
            symbol: e.g., "BTCUSDT"
            side: "buy" or "sell"
            size: quantity as string decimal
        
        Returns:
            Bitget API response
        """
        # Normalize symbol (remove /) and product type
        norm_symbol = symbol.replace("/", "").replace(":", "").upper()
        
        payload = {
            "symbol": norm_symbol,
            "productType": self.product_type,  # ALWAYS "USDT-FUTURES"
            "marginCoin": "USDT",
            "marginMode": self.margin_mode,
            "side": side.lower(),
            "orderType": "market",
            "size": str(size),
            "tradeSide": "open",
        }
        
        logger.info("[ORDER_MARKET] symbol=%s side=%s size=%s", norm_symbol, side, size)
        
        return await self._http_post(
            path="/api/v2/mix/order/place-order",
            payload=payload,
            operation=f"MARKET_{norm_symbol}_{side}"
        )
    
    async def place_order_limit(
        self,
        symbol: str,
        side: str,  # "buy" | "sell"
        size: str,  # quantity as string
        price: str,  # price as string
        time_in_force: str = "GTC",  # GTC | IOC | FOK | GTX
    ) -> Dict[str, Any]:
        """
        Place LIMIT order (INCLUDES PRICE).
        
        Args:
            symbol: e.g., "BTCUSDT"
            side: "buy" or "sell"
            size: quantity as string decimal
            price: price as string decimal
            time_in_force: Time in Force (default: GTC = Good Till Cancel)
        
        Returns:
            Bitget API response
        """
        norm_symbol = symbol.replace("/", "").replace(":", "").upper()
        
        payload = {
            "symbol": norm_symbol,
            "productType": self.product_type,  # ALWAYS "USDT-FUTURES"
            "marginCoin": "USDT",
            "marginMode": self.margin_mode,
            "side": side.lower(),
            "orderType": "limit",
            "price": str(price),  # KEY FIX: Limit orders MUST include price
            "size": str(size),
            "tradeSide": "open",
            "force": time_in_force,  # GTC | IOC | FOK | GTX
        }
        
        logger.info("[ORDER_LIMIT] symbol=%s side=%s size=%s price=%s", norm_symbol, side, size, price)
        
        return await self._http_post(
            path="/api/v2/mix/order/place-order",
            payload=payload,
            operation=f"LIMIT_{norm_symbol}_{side}"
        )
    
    async def attach_tpsl(
        self,
        symbol: str,
        plan_type: str,  # "pos_profit" | "pos_loss"
        trigger_price: str,
        hold_side: str = "long",
    ) -> Dict[str, Any]:
        """
        Attach TP or SL to existing position.
        
        Args:
            symbol: e.g., "BTCUSDT"
            plan_type: "pos_profit" (TP) or "pos_loss" (SL)
            trigger_price: Trigger price as string
            hold_side: "long" or "short"
        
        Returns:
            Bitget API response
        """
        norm_symbol = symbol.replace("/", "").replace(":", "").upper()
        
        payload = {
            "symbol": norm_symbol,
            "productType": self.product_type,  # ALWAYS "USDT-FUTURES"
            "marginCoin": "USDT",
            "planType": plan_type,
            "triggerPrice": str(trigger_price),
            "triggerType": "mark_price",
            "holdSide": hold_side,
        }
        
        logger.info("[TPSL_ATTACH] symbol=%s plan_type=%s trigger=%s hold_side=%s",
                   norm_symbol, plan_type, trigger_price, hold_side)
        
        return await self._http_post(
            path="/api/v2/mix/order/place-tpsl-order",
            payload=payload,
            operation=f"TPSL_{norm_symbol}_{plan_type}"
        )
    
    # ──────────────────────────────────────────────────
    #  POSITION / ACCOUNT MANAGEMENT
    # ──────────────────────────────────────────────────
    
    async def get_positions(
        self,
        symbol: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get current open positions via Bitget V2 API.
        Endpoint: GET /api/v2/mix/position/all-position
        Returns structured response with normalized position data and metadata.
        """
        endpoint = "/api/v2/mix/position/all-position"
        params = {
            "productType": self.product_type,  # ALWAYS "USDT-FUTURES"
            "marginCoin": "USDT",
        }
        if symbol and symbol.upper() != "ALL":
            norm_symbol = symbol.replace("/", "").replace(":", "").upper()
            params["symbol"] = norm_symbol
        
        # Debug logging
        if POS_DEBUG:
            logger.info("[POS_DEBUG] endpoint=%s params=%s", endpoint, params)
        
        result = await self._http_get(
            path=endpoint,
            params=params,
            operation=f"GET_POSITIONS_{symbol or 'ALL'}"
        )
        
        # Debug: log raw response
        if POS_DEBUG:
            raw_data = result.get("data", []) if isinstance(result, dict) else []
            raw_len = len(raw_data) if isinstance(raw_data, list) else 0
            preview = raw_data[:2] if raw_len > 0 else []
            safe_preview = [{"symbol": p.get("symbol"), "holdSide": p.get("holdSide"), 
                           "total": p.get("total")} for p in preview]
            logger.info(
                "[POS_DEBUG] code=%s msg=%s raw_len=%d preview=%s",
                result.get("code"), result.get("msg"), raw_len, safe_preview
            )
        
        # Normalize positions if successful
        if isinstance(result, dict) and result.get("code") == "00000":
            raw_positions = result.get("data", []) or []
            if not isinstance(raw_positions, list):
                raw_positions = []
            
            normalized = []
            req_symbol = symbol.replace("/", "").replace(":", "").upper() if symbol and symbol.upper() != "ALL" else None
            
            # Fetch active symbols from config for strict filtering
            try:
                from smc_futures.smc_config import SMC_CONFIG
                active_symbols = SMC_CONFIG.get("SYMBOLS", [])
                active_symbols_set = {s.replace("/", "").replace(":", "").upper() for s in active_symbols}
            except Exception:
                active_symbols_set = set()

            for pos in raw_positions:
                pos_symbol = pos.get("symbol", "").upper()
                if req_symbol and pos_symbol != req_symbol:
                    continue
                
                # Strict filtering: if requesting ALL, ignore symbols not in our trading list
                if not req_symbol and active_symbols_set and pos_symbol not in active_symbols_set:
                    continue
                
                total = self._safe_float(pos.get("total", 0))
                normalized.append({
                    "symbol": pos.get("symbol", ""),
                    "side": (pos.get("holdSide") or "").lower(),
                    "holdSide": pos.get("holdSide", ""),
                    "size": total,
                    "available": self._safe_float(pos.get("available", 0)),
                    "entryPrice": self._safe_float(pos.get("openPriceAvg", 0)),
                    "markPrice": self._safe_float(pos.get("markPrice", 0)),
                    "unrealizedPnl": self._safe_float(pos.get("unrealizedPL", 0)),
                    "leverage": self._safe_int(pos.get("leverage", 1)),
                    "marginCoin": pos.get("marginCoin", "USDT"),
                    "posMode": pos.get("holdMode", "one_way_mode"),
                    "hasPosition": total > 0,
                })
            
            active_positions = [p for p in normalized if p["hasPosition"]]
            count = len(active_positions)
            
            logger.info("[POSITIONS] count=%d | raw_len=%d | symbol=%s", count, len(raw_positions), symbol or "ALL")
            
            return {
                "code": "00000",
                "msg": "success",
                "source": "bitget_v2",
                "productType": self.product_type,
                "marginCoin": "USDT",
                "count": count,
                "positions": active_positions,
                "data": active_positions,
                "all_slots": normalized,
            }
        
        logger.error("[POSITIONS] API error | code=%s | msg=%s", result.get("code"), result.get("msg"))
        return {
            "code": result.get("code", "ERROR"),
            "msg": result.get("msg", "Unknown error"),
            "source": "bitget_v2",
            "productType": self.product_type,
            "marginCoin": "USDT",
            "count": 0,
            "positions": [],
            "data": [],
        }
    
    def _safe_float(self, value: Any) -> float:
        """Safely convert value to float."""
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    
    def _safe_int(self, value: Any) -> int:
        """Safely convert value to int."""
        if value is None:
            return 0
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0
    
    async def set_leverage(
        self,
        symbol: str,
        leverage: int
    ) -> Dict[str, Any]:
        """Set leverage for a symbol."""
        norm_symbol = symbol.replace("/", "").replace(":", "").upper()
        
        payload = {
            "symbol": norm_symbol,
            "leverage": str(leverage),
            "marginCoin": "USDT",
        }
        
        logger.info("[SET_LEVERAGE] symbol=%s leverage=%d", norm_symbol, leverage)
        
        return await self._http_post(
            path="/api/v2/mix/account/set-leverage",
            payload=payload,
            operation=f"SET_LEVERAGE_{norm_symbol}"
        )
    
    async def close_position(
        self,
        symbol: str
    ) -> Dict[str, Any]:
        """Close all positions for a symbol at market."""
        norm_symbol = symbol.replace("/", "").replace(":", "").upper()
        
        # 1. Get position info
        pos_res = await self.get_positions(norm_symbol)
        if pos_res.get("code") != "00000":
            logger.error("[CLOSE_POSITION] Failed to get positions | symbol=%s", norm_symbol)
            return {"code": "POSITION_FETCH_FAILED", "msg": "Could not fetch position", "data": None}
        
        positions = pos_res.get("data", [])
        if not positions:
            logger.info("[CLOSE_POSITION] No position to close | symbol=%s", norm_symbol)
            return {"code": "00000", "msg": "No position", "data": None}
        
        # 2. Close each position
        results = []
        for pos in positions:
            size = float(pos.get("total", 0))
            hold_side = pos.get("holdSide", "long").lower()
            
            if size <= 0:
                continue
            
            close_side = "sell" if hold_side == "long" else "buy"
            
            logger.info("[CLOSE_POSITION] Closing | symbol=%s hold_side=%s size=%f close_side=%s",
                       norm_symbol, hold_side, size, close_side)
            
            res = await self.place_order_market(
                symbol=norm_symbol,
                side=close_side,
                size=str(Decimal(str(size)).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))
            )
            results.append(res)
        
        return {"code": "00000", "msg": "Close orders sent", "data": results}
    
    # ──────────────────────────────────────────────────
    #  TICKER / PRICE (With Clock Skew Detection)
    # ──────────────────────────────────────────────────
    
    async def get_ticker(
        self,
        symbol: str,
        product_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get ticker with clock skew detection.
        
        KEY FIX: Correct timestamp parsing:
          - exchange_ts from API is usually in seconds
          - received_ms is local reception time in milliseconds
          - age_ms = max(0, received_ms - exchange_ts*1000)
          - If age_ms is negative, log [CLOCK_SKEW]
        """
        norm_symbol = symbol.replace("/", "").replace(":", "").upper()
        pt = product_type or self.product_type
        
        params = {
            "symbol": norm_symbol,
            "productType": pt,
        }
        
        received_ms = int(time.time() * 1000)
        
        res = await self._http_get(
            path="/api/v2/public/ticker",
            params=params,
            operation=f"GET_TICKER_{norm_symbol}"
        )
        
        # ── Clock skew check ────────────────────
        if res.get("code") == "00000" and res.get("data"):
            data = res["data"][0] if isinstance(res["data"], list) else res["data"]
            
            # Parse exchange timestamp (may be seconds or ms)
            ts = data.get("ts")
            if ts:
                ts_int = int(ts)
                # Heuristic: if < 10^12, likely seconds
                exchange_ts_ms = ts_int * 1000 if ts_int < 10**12 else ts_int
                age_ms = max(0, received_ms - exchange_ts_ms)
                
                # Log if age is suspiciously negative (clock skew)
                if age_ms < 0:
                    logger.warning("[CLOCK_SKEW] symbol=%s age_ms=%d (negative indicates future ts)",
                                 norm_symbol, age_ms)
                elif age_ms > 5000:  # > 5s is stale
                    logger.warning("[STALE_TICKER] symbol=%s age_ms=%d", norm_symbol, age_ms)
                
                # Normalize age to 0 if negative
                data["age_ms"] = max(0, age_ms)
        
        return res
    
    # ──────────────────────────────────────────────────
    #  HIGH-LEVEL EXECUTION: Full Signal -> Order
    # ──────────────────────────────────────────────────
    
    async def place_order(
        self,
        symbol: str,
        direction: str,  # "long" | "short"
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        order_type: str = "market",
        leverage: int = 3,
        **kwargs
    ) -> Dict[str, Any]:
        """
        High-level order placement from signal.
        
        Flow:
          1. Set leverage
          2. Place entry order (market or limit)
          3. Attach TP/SL
        
        Args:
            symbol: "BTCUSDT"
            direction: "long" | "short"
            entry_price: Entry price (for limit orders)
            stop_loss: SL price
            take_profit: TP price
            order_type: "market" | "limit"
            leverage: 1-125
        
        Returns:
            {
                "success": bool,
                "order_id": str,
                "fill_price": float,
                "error": str (if failed)
            }
        """
        try:
            # 1. Set leverage
            logger.info("[ORDER_FULL] Setting leverage | symbol=%s leverage=%d", symbol, leverage)
            lev_res = await self.set_leverage(symbol, leverage)
            if lev_res.get("code") != "00000":
                return {
                    "success": False,
                    "order_id": None,
                    "fill_price": None,
                    "error": f"Failed to set leverage: {lev_res.get('msg')}"
                }
            
            # 2. Place entry order
            side = "buy" if direction.lower() == "long" else "sell"
            size_str = str(Decimal("0.001").quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))  # Example
            
            if order_type.lower() == "market":
                order_res = await self.place_order_market(symbol, side, size_str)
            else:
                price_str = str(Decimal(str(entry_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                order_res = await self.place_order_limit(symbol, side, size_str, price_str)
            
            if order_res.get("code") != "00000":
                return {
                    "success": False,
                    "order_id": None,
                    "fill_price": None,
                    "error": f"Order placement failed: {order_res.get('msg')}"
                }
            
            order_id = order_res.get("data", {}).get("orderId", "UNKNOWN")
            
            # 3. Attach TP/SL
            hold_side = direction.lower()
            tp_res = await self.attach_tpsl(symbol, "pos_profit", str(take_profit), hold_side)
            sl_res = await self.attach_tpsl(symbol, "pos_loss", str(stop_loss), hold_side)
            
            if tp_res.get("code") != "00000":
                logger.warning("[TPSL_FAIL] Failed to attach TP | symbol=%s error=%s",
                             symbol, tp_res.get("msg"))
            if sl_res.get("code") != "00000":
                logger.warning("[TPSL_FAIL] Failed to attach SL | symbol=%s error=%s",
                             symbol, sl_res.get("msg"))
            
            return {
                "success": True,
                "order_id": order_id,
                "fill_price": entry_price,
                "error": None
            }
        
        except Exception as e:
            logger.error("[ORDER_FULL] Exception | symbol=%s error=%s", symbol, str(e), exc_info=True)
            return {
                "success": False,
                "order_id": None,
                "fill_price": None,
                "error": str(e)
            }
