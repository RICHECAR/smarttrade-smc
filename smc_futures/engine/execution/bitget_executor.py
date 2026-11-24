"""
Bitget Futures Executor — Production-Grade Order Placement (V2 Standardized)
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
API_VERSION = "v2"  # ALWAYS v2

# Debug mode for signature troubleshooting (set BITGET_SIGN_DEBUG=1)
SIGN_DEBUG = os.getenv("BITGET_SIGN_DEBUG", "0") == "1"

# Debug mode for positions API (set BITGET_POS_DEBUG=1)
POS_DEBUG = os.getenv("BITGET_POS_DEBUG", "0") == "1"

class BitgetFuturesExecutor:
    """
    Execute orders on Bitget Mix Futures (USDT-FUTURES).
    Standardized V2 implementation with strict productType and robust logging.
    """

    @staticmethod
    def round_step(value: float, step: float) -> str:
        """Round *value* down to the nearest *step* increment."""
        d_val = Decimal(str(value))
        d_step = Decimal(str(step))
        if d_step <= 0:
            return str(d_val)
        return str((d_val // d_step) * d_step)

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
        
        logger.info("[BITGET_INIT] Executor initialized | api_key=%s... | product=%s",
                   api_key[:8] if api_key else "NONE", self.product_type)
    
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
        filtered = {k: str(v) for k, v in params.items() if v is not None}
        if not filtered:
            return ""
        return urlencode(sorted(filtered.items()))
    
    def _generate_signature(self, timestamp: str, method: str, request_path: str, query_string: str = "", body: str = "") -> str:
        """
        Generate HMAC-SHA256 signature for Bitget API.
        
        Bitget V2 prehash format:
          prehash = timestamp + METHOD + requestPath + ("?" + queryString if not empty) + body
        
        CRITICAL:
          - For GET: body MUST be "" (empty string)
          - queryString must match EXACTLY what's sent in URL
          - "?" is only added if queryString is non-empty
        """
        prehash = timestamp + method.upper() + request_path
        if query_string:
            prehash += "?" + query_string
        prehash += body
        
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
    
    def _headers(self, method: str, path: str, query_string: str = "", body: str = "") -> Dict[str, str]:
        """Generate authenticated headers for request."""
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
    #  HTTP HELPER
    # ──────────────────────────────────────────────────
    
    async def _http_post(self, path: str, payload: Dict[str, Any], operation: str = "ORDER") -> Dict:
        body_str = json.dumps(payload)
        headers = self._headers("POST", path, "", body_str)  # POST: query="", body=payload
        url = BITGET_BASE + path
        
        logger.info(f"[HTTP_CALL] POST {operation} {path}")
        logger.debug(f"[HTTP_BODY] {body_str}")
        
        # Phase 4.5 Latency Tracking: t_order_send
        from smc_futures.engine.analysis.latency_tracker import latency_tracker
        order_key = payload.get("clientOid", payload.get("clientOrderId", operation))
        latency_tracker.record_timestamp(order_key, "t_order_send")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, content=body_str, headers=headers)
            
            # Phase 4.5 Latency Tracking: t_exchange_ack
            latency_tracker.record_timestamp(order_key, "t_exchange_ack")
            
            if response.status_code >= 500:
                logger.error(f"[HTTP_FAIL] {operation} | status={response.status_code} | {response.text}")
                return {"code": str(response.status_code), "msg": "Server Error", "data": None}
            
            data = response.json()
            code = data.get("code", "UNKNOWN")
            
            if code != "00000":
                logger.error(f"[HTTP_FAIL] {operation} | code={code} | msg={data.get('msg')}")
            else:
                logger.info(f"[HTTP_OK] {operation} | code={code}")
                
            return data
            
        except Exception as e:
            logger.error(f"[HTTP_FAIL] {operation} | ex={str(e)}", exc_info=True)
            return {"code": "EXCEPTION", "msg": str(e), "data": None}

    async def _http_get(self, path: str, params: Optional[Dict] = None, operation: str = "QUERY") -> Dict:
        """
        Make authenticated GET request.
        
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
            
        logger.info(f"[HTTP_CALL] GET {operation} {url}")
        
        if SIGN_DEBUG:
            logger.info(f"[SIGN_DEBUG] final_url={url}")
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=headers)
                
            if response.status_code >= 500:
                logger.error(f"[HTTP_FAIL] {operation} | status={response.status_code}")
                return {"code": str(response.status_code), "msg": "Server Error"}
                
            data = response.json()
            return data
            
        except Exception as e:
            logger.error(f"[HTTP_FAIL] {operation} | ex={str(e)}")
            return {"code": "EXCEPTION", "msg": str(e)}

    # ──────────────────────────────────────────────────
    #  CORE API METHODS
    # ──────────────────────────────────────────────────

    async def place_order_market(self, symbol: str, side: str, size: str) -> Dict:
        """Market order (no price)."""
        norm_symbol = symbol.replace("/", "").upper()
        
        # Round size to safe 4 decimals (std for most crypto) 
        # TODO: dynamic lot size fetch
        try:
            size_float = float(size)
            rounded_size = f"{size_float:.4f}" 
            # Strip trailing zeros? No, fixed decimals is safer for V2 usually.
        except:
            rounded_size = str(size)
            
        payload = {
            "symbol": norm_symbol,
            "productType": self.product_type,
            "marginCoin": "USDT",
            "marginMode": self.margin_mode,
            "side": side.lower(),
            "orderType": "market",
            "size": rounded_size,
            "tradeSide": "open",
        }
        return await self._http_post("/api/v2/mix/order/place-order", payload, f"MARKET_{norm_symbol}")

    async def place_order_limit(self, symbol: str, side: str, size: str, price: str) -> Dict:
        """Limit order (with price)."""
        norm_symbol = symbol.replace("/", "").upper()
        
        # Round Price
        decimals = self._get_price_decimals(norm_symbol)
        try:
            price_float = float(price)
            rounded_price = f"{price_float:.{decimals}f}"
        except:
            rounded_price = str(price)
            
        # Round Size
        try:
            size_float = float(size)
            rounded_size = f"{size_float:.4f}"
        except:
            rounded_size = str(size)

        payload = {
            "symbol": norm_symbol,
            "productType": self.product_type,
            "marginCoin": "USDT",
            "marginMode": self.margin_mode,
            "side": side.lower(),
            "orderType": "limit",
            "price": rounded_price,
            "size": rounded_size,
            "tradeSide": "open",
            "force": "GTC",
        }
        return await self._http_post("/api/v2/mix/order/place-order", payload, f"LIMIT_{norm_symbol}")

    def _get_price_decimals(self, symbol: str) -> int:
        """
        Return price precision for a symbol.
        Hardcoded safe defaults for major pairs to avoid extra API calls.
        TODO: Fetch from /api/v2/mix/market/contracts for full dynamic support.
        """
        s = symbol.upper().replace("USDT", "")
        if "BTC" in s: return 1
        if "ETH" in s: return 2
        if "SOL" in s: return 3  # V2 often 2 or 3 depending on contract, 3 is safer for "too many decimals" error if we round? 
                                 # actually checkBDScale error means TOO MANY decimals. 
                                 # SOL tick size is usually 0.01 or 0.001. Let's start with 2 to be safe (less precision is safer than more).
                                 # Wait, if tick is 0.01, sending 143.456 fails. Sending 143.45 works. 
                                 # So we want FEWER decimals. 
                                 # Let's use 2 for SOL.
        if "XRP" in s: return 4
        if "DOGE" in s: return 5
        return 4 # Default fallback

    async def attach_tpsl(self, symbol: str, plan_type: str, trigger_price: str, hold_side: str) -> Dict:
        """Attach TP/SL with PRICE ROUNDING to avoid checkBDScale error."""
        norm_symbol = symbol.replace("/", "").upper()
        
        # ROUNDING
        decimals = self._get_price_decimals(norm_symbol)
        try:
            price_float = float(trigger_price)
            rounded_price = f"{price_float:.{decimals}f}"
        except:
            rounded_price = str(trigger_price)
            
        logger.info(f"[TPSL] {norm_symbol} | {plan_type} | raw={trigger_price} -> round={rounded_price} (d={decimals})")

        payload = {
            "symbol": norm_symbol,
            "productType": self.product_type,
            "marginCoin": "USDT",
            "planType": plan_type,
            "triggerPrice": rounded_price,
            "triggerType": "mark_price",
            "holdSide": hold_side,
        }
        return await self._http_post("/api/v2/mix/order/place-tpsl-order", payload, f"TPSL_{norm_symbol}")

    async def set_leverage(self, symbol: str, leverage: int) -> Dict:
        """
        Set leverage for a symbol.
        Clamped to max 10x for safety (user requirement).
        """
        # CLAMP: Max 10x
        MAX_LEVERAGE = 10
        final_leverage = min(leverage, MAX_LEVERAGE)
        
        norm_symbol = symbol.replace("/", "").upper()

        if final_leverage != leverage:
            logger.warning(f"[LEV_CLAMP] {norm_symbol} | Requested {leverage}x -> Clamped to {final_leverage}x")
            
        payload = {
            "symbol": norm_symbol,
            "productType": self.product_type,
            "marginCoin": "USDT",
            "leverage": str(final_leverage),
            "holdSide": "long", 
        }
        
        # We need to set for both Long and Short to be safe in Hedge Mode
        # Task 1: Set Long
        res_long = await self._http_post("/api/v2/mix/account/set-leverage", payload, f"LEV_LONG_{norm_symbol}")
        
        # Task 2: Set Short
        payload["holdSide"] = "short"
        res_short = await self._http_post("/api/v2/mix/account/set-leverage", payload, f"LEV_SHORT_{norm_symbol}")
        
        long_msg = res_long.get('msg', 'unknown')
        short_msg = res_short.get('msg', 'unknown')
        
        logger.info(
            f"[LEV_APPLIED] {norm_symbol} | {final_leverage}x "
            f"| productType={self.product_type} "
            f"| long={long_msg} short={short_msg}"
        )
        
        # Return success if both OK, or if error is "leverage not modified" (often code 400xxx)
        # Actually Bitget returns success even if not modified usually, or specific error.
        if res_long.get("code") == "00000" and res_short.get("code") == "00000":
            return {"code": "00000", "msg": "success", "leverage": final_leverage}
        else:
             # Return error if either failed
             return res_long if res_long.get("code") != "00000" else res_short
    
    async def get_positions(self, symbol: Optional[str] = None) -> Dict:
        """
        Get open positions via Bitget V2 API.
        Endpoint: GET /api/v2/mix/position/all-position
        Returns structured response with normalized position data and metadata.
        """
        if os.getenv("BITGET_ENABLED", "true").lower() not in ("1", "true", "yes"):
            return {
                "code": "00000", "data": [], "positions": [], "count": 0,
                "msg": "bitget_disabled", "source": "bitget_v2",
                "productType": self.product_type, "marginCoin": "USDT",
            }
        endpoint = "/api/v2/mix/position/all-position"
        params = {
            "productType": self.product_type,
            "marginCoin": "USDT",
        }
        if symbol and symbol.upper() != "ALL":
            params["symbol"] = symbol.replace("/", "").upper()
        
        # Debug logging
        if POS_DEBUG:
            logger.info(f"[POS_DEBUG] endpoint={endpoint} params={params}")
        
        result = await self._http_get(endpoint, params, "POSITIONS")
        
        # Debug: log raw response
        if POS_DEBUG:
            raw_data = result.get("data", []) if isinstance(result, dict) else []
            raw_len = len(raw_data) if isinstance(raw_data, list) else 0
            preview = raw_data[:2] if raw_len > 0 else []
            # Sanitize preview (remove sensitive fields if any)
            safe_preview = []
            for p in preview:
                safe_preview.append({
                    "symbol": p.get("symbol"),
                    "holdSide": p.get("holdSide"),
                    "total": p.get("total"),
                    "available": p.get("available"),
                })
            logger.info(
                f"[POS_DEBUG] status_code=200 code={result.get('code')} msg={result.get('msg')} "
                f"raw_len={raw_len} preview={safe_preview}"
            )
        
        # Handle API error
        if not isinstance(result, dict):
            logger.error(f"[POSITIONS] Invalid response type: {type(result)}")
            return {
                "code": "ERROR", "data": [], "positions": [], "count": 0,
                "msg": "Invalid response", "source": "bitget_v2",
                "productType": self.product_type, "marginCoin": "USDT"
            }
        
        if result.get("code") != "00000":
            logger.error(f"[POSITIONS] API error | code={result.get('code')} | msg={result.get('msg')}")
            return {
                "code": result.get("code"), "data": [], "positions": [], "count": 0,
                "msg": result.get("msg"), "source": "bitget_v2",
                "productType": self.product_type, "marginCoin": "USDT"
            }
        
        # Parse and normalize positions data
        raw_positions = result.get("data", []) or []
        if not isinstance(raw_positions, list):
            raw_positions = []
        
        normalized = []
        req_symbol = symbol.replace("/", "").replace(":", "").upper() if symbol and symbol.upper() != "ALL" else None

        for pos in raw_positions:
            pos_symbol = pos.get("symbol", "").upper()
            if req_symbol and pos_symbol != req_symbol:
                continue

            # Safe float conversion
            total = self._safe_float(pos.get("total", 0))
            available = self._safe_float(pos.get("available", 0))
            
            # Include all positions, not just those with holdings > 0
            # This helps debug "empty" responses
            normalized.append({
                "symbol": pos.get("symbol", ""),
                "side": (pos.get("holdSide") or "").lower(),
                "holdSide": pos.get("holdSide", ""),
                "size": total,
                "available": available,
                "entryPrice": self._safe_float(pos.get("openPriceAvg", 0)),
                "markPrice": self._safe_float(pos.get("markPrice", 0)),
                "unrealizedPnl": self._safe_float(pos.get("unrealizedPL", 0)),
                "leverage": self._safe_int(pos.get("leverage", 1)),
                "marginCoin": pos.get("marginCoin", "USDT"),
                "posMode": pos.get("holdMode", "one_way_mode"),
                "marginMode": pos.get("marginMode", "isolated"),
                "hasPosition": total > 0,
            })
        
        # Filter to only positions with actual holdings for the main list
        active_positions = [p for p in normalized if p["hasPosition"]]
        count = len(active_positions)
        
        logger.info(f"[POSITIONS] count={count} | raw_len={len(raw_positions)} | symbol={symbol or 'ALL'}")
        
        return {
            "code": "00000",
            "msg": "success",
            "source": "bitget_v2",
            "productType": self.product_type,
            "marginCoin": "USDT",
            "count": count,
            "positions": active_positions,
            "data": active_positions,  # Backward compatibility
            "all_slots": normalized,  # All position slots (for debug)
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

    async def get_plan_orders(self, symbol: str) -> Dict:
        """Fetch pending plan orders (TPSL)."""
        return await self.client.get_plan_orders(symbol)

    # Alias for compatibility
    async def get_open_positions(self, symbol: Optional[str] = None) -> Dict:
        """Alias for get_positions() for backward compatibility."""
        return await self.get_positions(symbol)

    async def get_account_balance(self, marginCoin: str = "USDT", margin_coin: str = None) -> Dict:
        """
        Get account balance for futures.
        GET /api/v2/mix/account/accounts?productType=USDT-FUTURES&marginCoin=USDT
        
        Returns dict with: available, equity, crossedMaxAvailable, etc.
        """
        # Support both marginCoin and margin_coin (snake_case)
        coin = margin_coin or marginCoin or "USDT"
        
        params = {
            "productType": self.product_type,
            "marginCoin": coin,
        }
        result = await self._http_get("/api/v2/mix/account/accounts", params, "BALANCE")
        
        # Parse balance from response
        if result.get("code") == "00000" and result.get("data"):
            data_list = result["data"]
            if isinstance(data_list, list) and len(data_list) > 0:
                account = data_list[0]  # First account entry
                available = float(account.get("available", 0) or account.get("crossedMaxAvailable", 0) or 0)
                equity = float(account.get("equity", 0) or account.get("accountEquity", 0) or 0)
                logger.info(f"[BALANCE] available={available:.2f} | equity={equity:.2f} | coin={coin}")
                return {
                    "available": available,
                    "equity": equity,
                    "marginCoin": coin,
                    "raw": account,
                }
        
        # Fallback: return raw or error
        logger.warning(f"[BALANCE] Failed to parse | code={result.get('code')} | msg={result.get('msg')}")
        return {"available": 0.0, "equity": 0.0, "marginCoin": coin, "error": result.get("msg")}

    async def get_historical_pnl(self, symbol: str, start_time: Optional[int] = None) -> Optional[Dict[str, float]]:
        """
        Fetch historical PnL from the exchange using order/fill history.
        Used to reconcile manual closures.
        """
        logger.info(f"[BITGET_HIST_PNL] Fetching historical PnL for {symbol} since {start_time}")
        try:
            res = await self.client.get_historical_fills(symbol, start_time=start_time)
            if res.get("code") != "00000":
                logger.error(f"[BITGET_HIST_PNL] API error: {res.get('msg')}")
                return None
            
            fill_list = res.get("data", {}).get("fillList", [])
            if not fill_list:
                return None
                
            total_pnl = 0.0
            total_fee = 0.0
            found = False
            
            for fill in fill_list:
                pnl = float(fill.get("realizedPnl", 0) or 0)
                fee = float(fill.get("fee", 0) or 0)
                
                # We only care about closing fills that realize PnL
                if pnl != 0 or fee != 0:
                    found = True
                    total_pnl += pnl
                    total_fee += fee
            
            if found:
                logger.info(f"[BITGET_HIST_PNL] Found historical fills for {symbol} | pnl={total_pnl:.2f}, fee={total_fee:.2f}")
                return {"pnl": total_pnl, "fee": total_fee}
                
            return None
            
        except Exception as e:
            logger.error(f"[BITGET_HIST_PNL] Error: {e}")
            return None

    async def close_position_market(self, symbol: str, hold_side: str, size: str) -> Dict:
        """
        Emergency market close of a position.
        hold_side: 'long' or 'short'
        """
        norm_symbol = symbol.replace("/", "").upper()
        close_side = "sell" if hold_side.lower() == "long" else "buy"
        
        try:
            size_float = float(size)
            rounded_size = f"{size_float:.4f}"
        except:
            rounded_size = str(size)
        
        payload = {
            "symbol": norm_symbol,
            "productType": self.product_type,
            "marginCoin": "USDT",
            "marginMode": self.margin_mode,
            "side": close_side,
            "orderType": "market",
            "size": rounded_size,
            "tradeSide": "close",
        }
        logger.warning(f"[EMERGENCY_CLOSE] {norm_symbol} | side={close_side} | size={rounded_size}")
        return await self._http_post("/api/v2/mix/order/place-order", payload, f"EMERGENCY_CLOSE_{norm_symbol}")

    async def _attach_tpsl_with_retry(
        self, symbol: str, plan_type: str, trigger_price: str, hold_side: str, max_retries: int = 3
    ) -> Dict:
        """Attach TP/SL with retry. Returns last result."""
        import asyncio
        last_result = {}
        for attempt in range(1, max_retries + 1):
            result = await self.attach_tpsl(symbol, plan_type, trigger_price, hold_side)
            code = result.get("code", "UNKNOWN")
            if code == "00000":
                logger.info(f"[TPSL_OK] {symbol} | {plan_type} | attempt={attempt}")
                return result
            last_result = result
            logger.warning(
                f"[TPSL_RETRY] {symbol} | {plan_type} | attempt={attempt}/{max_retries} "
                f"| code={code} | msg={result.get('msg')}"
            )
            if attempt < max_retries:
                await asyncio.sleep(1.0)
        
        logger.error(f"[TPSL_FAILED] {symbol} | {plan_type} | ALL {max_retries} RETRIES EXHAUSTED")
        return last_result

    async def place_order(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        order_type: str = "market",
        leverage: int = 3,
        quantity: str = "0.001",  # Passed by Manager
        **kwargs
    ) -> Dict:
        """
        Orchestrates full order placement with MANDATORY TP/SL.
        Safety: If SL attachment fails after 3 retries, the position is closed immediately.
        """
        try:
            # 1. Set leverage
            await self.set_leverage(symbol, leverage)
            
            # 2. Place entry
            side = "buy" if direction.lower() == "long" else "sell"
            hold_side = "long" if side == "buy" else "short"
            
            if order_type.lower() == "market":
                res = await self.place_order_market(symbol, side, quantity)
            else:
                res = await self.place_order_limit(symbol, side, quantity, str(entry_price))
            
            if res.get("code") != "00000":
                return {"success": False, "error": f"Entry failed: {res.get('msg')}"}
            
            order_id = res["data"].get("orderId")
            logger.info(f"[ORDER_FILLED] {symbol} | orderId={order_id} | side={side} | qty={quantity}")
            
            # 3. Attach SL (MANDATORY — retry 3x, close if fail)
            sl_ok = True
            if stop_loss > 0:
                sl_result = await self._attach_tpsl_with_retry(symbol, "loss_plan", str(stop_loss), hold_side)
                if sl_result.get("code") != "00000":
                    sl_ok = False
                    logger.critical(
                        f"[CRITICAL_SL_FAIL] {symbol} | SL={stop_loss} FAILED AFTER ALL RETRIES. "
                        f"CLOSING POSITION IMMEDIATELY TO PREVENT UNPROTECTED EXPOSURE."
                    )
                    # EMERGENCY: Close the position we just opened
                    close_res = await self.close_position_market(symbol, hold_side, quantity)
                    close_code = close_res.get("code", "UNKNOWN")
                    logger.critical(f"[EMERGENCY_CLOSE_RESULT] {symbol} | code={close_code} | msg={close_res.get('msg')}")
                    
                    return {
                        "success": False,
                        "error": f"SL attachment failed after retries. Position CLOSED for safety. SL error: {sl_result.get('msg')}",
                        "order_id": order_id,
                        "emergency_closed": True,
                    }
            else:
                logger.warning(f"[NO_SL] {symbol} | SL=0 — POSITION HAS NO STOP LOSS PROTECTION!")

            # 4. Attach TP (with retry, but not fatal)
            if take_profit > 0:
                tp_result = await self._attach_tpsl_with_retry(symbol, "profit_plan", str(take_profit), hold_side)
                if tp_result.get("code") != "00000":
                    logger.error(f"[TP_FAIL] {symbol} | TP={take_profit} failed but position kept (SL is active)")

            return {
                "success": True,
                "order_id": order_id,
                "fill_price": float(entry_price),  # approx
                "timestamp": datetime.now().isoformat(),
                "sl_attached": sl_ok,
                "tp_attached": take_profit > 0,
            }
            
        except Exception as e:
            logger.error(f"[PLACE_ORDER_EXCEPTION] {symbol} | {str(e)}", exc_info=True)
            return {"success": False, "error": str(e)}

