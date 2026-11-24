#!/usr/bin/env python3
import os
import sys
import time
import hmac
import base64
import hashlib
import asyncio
import logging
from typing import Dict
from urllib.parse import urlencode

try:
    from dotenv import load_dotenv
    import httpx
except ImportError:
    print("CRITICAL: python-dotenv and httpx are required. Run: pip install python-dotenv httpx")
    sys.exit(3)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] OOB-KILL: %(message)s")
logger = logging.getLogger("emergency_kill")

EXIT_FLAT = 0
EXIT_PARTIAL = 2
EXIT_ERROR = 3

class StandaloneBitget:
    def __init__(self):
        self.api_key = os.getenv("KILL_BITGET_API_KEY")
        self.secret_key = os.getenv("KILL_BITGET_SECRET_KEY")
        self.passphrase = os.getenv("KILL_BITGET_PASSPHRASE")
        self.base_url = "https://api.bitget.com"
        self.ready = bool(self.api_key and self.secret_key and self.passphrase)

    def _sign(self, method: str, path: str, body: str = "") -> dict:
        timestamp = str(int(time.time() * 1000))
        message = timestamp + method + path + body
        mac = hmac.new(self.secret_key.encode('utf-8'), message.encode('utf-8'), digestmod='sha256')
        sign = base64.b64encode(mac.digest()).decode('utf-8')
        return {
            'BASE-API-KEY': self.api_key,
            'BASE-API-SIGN': sign,
            'BASE-API-TIMESTAMP': timestamp,
            'BASE-API-PASSPHRASE': self.passphrase,
            'Content-Type': 'application/json'
        }

    async def cancel_all_orders(self, client: httpx.AsyncClient) -> bool:
        if not self.ready: return False
        path = "/api/v2/mix/order/cancel-all-orders"
        body = '{"productType":"USDT-FUTURES"}'
        headers = self._sign("POST", path, body)
        try:
            logger.info("BITGET: Canceling all orders...")
            resp = await client.post(f"{self.base_url}{path}", headers=headers, content=body, timeout=5)
            data = resp.json()
            if data.get("code") == "00000":
                logger.info("BITGET: Cancel orders dispatched.")
                return True
            logger.error(f"BITGET: Cancel error: {data}")
            return False
        except Exception as e:
            logger.error(f"BITGET: Cancel exception: {e}")
            return False

    async def close_all_positions(self, client: httpx.AsyncClient) -> bool:
        if not self.ready: return False
        path = "/api/v2/mix/order/close-all-positions"
        body = '{"productType":"USDT-FUTURES"}'
        headers = self._sign("POST", path, body)
        try:
            logger.info("BITGET: Dispatching Market Close All Positions...")
            resp = await client.post(f"{self.base_url}{path}", headers=headers, content=body, timeout=10)
            data = resp.json()
            if data.get("code") == "00000":
                logger.info("BITGET: Close positions instructions sent.")
                return True
            logger.error(f"BITGET: Close positions error: {data}")
            return False
        except Exception as e:
            logger.error(f"BITGET: Close positions exception: {e}")
            return False

    async def get_state(self, client: httpx.AsyncClient) -> tuple[int, int]:
        if not self.ready: return 0, 0
        try:
            path_ord = "/api/v2/mix/order/orders-pending"
            query = "?productType=USDT-FUTURES"
            headers_ord = self._sign("GET", path_ord + query)
            resp_ord = await client.get(f"{self.base_url}{path_ord}{query}", headers=headers_ord, timeout=5)
            ord_cnt = len(resp_ord.json().get("data", {}).get("entrustList", [])) or 0

            path_pos = "/api/v2/mix/position/all-position"
            headers_pos = self._sign("GET", path_pos + query)
            resp_pos = await client.get(f"{self.base_url}{path_pos}{query}", headers=headers_pos, timeout=5)
            pos_list = resp_pos.json().get("data", [])
            active_pos = sum(1 for p in pos_list if float(p.get("total", 0)) > 0)
            return ord_cnt, active_pos
        except Exception as e:
            logger.error(f"BITGET: Read-back exception: {e}")
            return -1, -1


class StandaloneKraken:
    def __init__(self):
        self.api_key = os.getenv("KILL_KRAKEN_API_KEY")
        self.secret_key = os.getenv("KILL_KRAKEN_API_SECRET")
        self.base_url = "https://futures.kraken.com"
        self.ready = bool(self.api_key and self.secret_key)

    def _sign_headers(self, endpoint: str, post_data: str = "") -> dict:
        message = (post_data + endpoint).encode('utf-8')
        sha256_hash = hashlib.sha256(message).digest()
        mac = hmac.new(base64.b64decode(self.secret_key), sha256_hash, hashlib.sha512)
        sig = base64.b64encode(mac.digest()).decode('utf-8')
        return {'APIKey': self.api_key, 'Authent': sig}

    async def cancel_all_orders(self, client: httpx.AsyncClient) -> bool:
        if not self.ready: return False
        endpoint = "/derivatives/api/v3/cancelallorders"
        headers = self._sign_headers(endpoint)
        try:
            logger.info("KRAKEN: Dispatching Cancel All Orders...")
            resp = await client.post(f"{self.base_url}{endpoint}", headers=headers, timeout=5)
            data = resp.json()
            if data.get("result") == "success":
                logger.info("KRAKEN: Cancelled open orders.")
                return True
            logger.error(f"KRAKEN: Cancel error: {data}")
            return False
        except Exception as e:
            logger.error(f"KRAKEN: Cancel exception: {e}")
            return False

    async def close_all_positions(self, client: httpx.AsyncClient) -> bool:
        if not self.ready: return False
        endpoint = "/derivatives/api/v3/openpositions"
        headers = self._sign_headers(endpoint)
        try:
            logger.info("KRAKEN: Fetching open positions to liquidate...")
            resp = await client.get(f"{self.base_url}{endpoint}", headers=headers, timeout=5)
            data = resp.json()
            if data.get("result") != "success":
                logger.error(f"KRAKEN: Fetch Positions Error: {data}")
                return False
                
            positions = data.get("openPositions", [])
            tasks = []
            for pos in positions:
                size = pos.get("size", 0)
                if size > 0:
                    reduce_side = "sell" if pos.get("side", "").lower() == "long" else "buy"
                    params_dict = {"orderType": "ioc", "symbol": pos.get("symbol"), "side": reduce_side, "size": str(size)}
                    encoded = urlencode(params_dict)
                    send_ep = "/derivatives/api/v3/sendorder"
                    send_hd = self._sign_headers(send_ep, encoded)
                    send_hd["Content-Type"] = "application/x-www-form-urlencoded"
                    
                    logger.info(f"KRAKEN: Market Inverse ({reduce_side.upper()}) for {size} {pos.get('symbol')}")
                    tasks.append(client.post(f"{self.base_url}{send_ep}", headers=send_hd, content=encoded, timeout=5))
            
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, r in enumerate(results):
                    if isinstance(r, Exception):
                        logger.error(f"KRAKEN: Close order issue: {r}")
            return True
        except Exception as e:
            logger.error(f"KRAKEN: Close positions exception: {e}")
            return False

    async def get_state(self, client: httpx.AsyncClient) -> tuple[int, int]:
        if not self.ready: return 0, 0
        try:
            ep_ord = "/derivatives/api/v3/openorders"
            resp_ord = await client.get(f"{self.base_url}{ep_ord}", headers=self._sign_headers(ep_ord), timeout=5)
            ord_cnt = len(resp_ord.json().get("openOrders", []))

            ep_pos = "/derivatives/api/v3/openpositions"
            resp_pos = await client.get(f"{self.base_url}{ep_pos}", headers=self._sign_headers(ep_pos), timeout=5)
            positions = resp_pos.json().get("openPositions", [])
            active_pos = sum(1 for p in positions if float(p.get("size", 0)) > 0)
            return ord_cnt, active_pos
        except Exception as e:
            logger.error(f"KRAKEN: Read-back exception: {e}")
            return -1, -1


async def run_sequence(bg: StandaloneBitget, kr: StandaloneKraken):
    async with httpx.AsyncClient() as client:
        # Pass 1
        await asyncio.gather(bg.cancel_all_orders(client), kr.cancel_all_orders(client))
        await asyncio.sleep(0.5)
        await asyncio.gather(bg.close_all_positions(client), kr.close_all_positions(client))
        
        await asyncio.sleep(1.0)
        
        # Check 1
        bg_ords, bg_pos = await bg.get_state(client)
        kr_ords, kr_pos = await kr.get_state(client)
        
        is_flat = (bg_ords == 0 and bg_pos == 0) and (kr_ords == 0 and kr_pos == 0)
        if is_flat:
            logger.info("✅ SUCCESS: Verified FLAT on all executing exchanges.")
            return EXIT_FLAT
            
        logger.warning(f"⚠️ NOT FLAT: Bitget({bg_ords} ord, {bg_pos} pos) Kraken({kr_ords} ord, {kr_pos} pos). Initiating Retry 1/1.")
        
        # Pass 2 (Retry)
        await asyncio.gather(bg.cancel_all_orders(client), kr.cancel_all_orders(client))
        await asyncio.sleep(0.5)
        await asyncio.gather(bg.close_all_positions(client), kr.close_all_positions(client))
        
        await asyncio.sleep(1.0)
        
        # Check 2
        bg_ords, bg_pos = await bg.get_state(client)
        kr_ords, kr_pos = await kr.get_state(client)
        
        if (bg_ords == 0 and bg_pos == 0) and (kr_ords == 0 and kr_pos == 0):
            logger.info("✅ SUCCESS ON RETRY: Verified FLAT.")
            return EXIT_FLAT
            
        logger.error(f"❌ FINAL FAIL: Bitget({bg_ords}, {bg_pos}) Kraken({kr_ords}, {kr_pos})")
        return EXIT_PARTIAL

def main():
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    bg = StandaloneBitget()
    kr = StandaloneKraken()
    if not bg.ready and not kr.ready:
        logger.error("No valid FAILSAFE credentials found in .env (KILL_BITGET_*, KILL_KRAKEN_*).")
        sys.exit(EXIT_ERROR)
    
    try:
        sys.exit(asyncio.run(run_sequence(bg, kr)))
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        sys.exit(EXIT_ERROR)

if __name__ == "__main__":
    main()
