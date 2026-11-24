#!/usr/bin/env python
"""Test different signature formats for Kraken Futures."""

import asyncio
import base64
import hashlib
import hmac
import os
import time
import httpx
from dotenv import load_dotenv
load_dotenv()

api_key = os.getenv('KRAKEN_API_KEY', '').strip()
api_secret = os.getenv('KRAKEN_API_SECRET', '').strip()


async def test_signature(sign_endpoint: str, url_endpoint: str) -> bool:
    """Test a specific signature endpoint format."""
    nonce = str(int(time.time() * 1000))
    post_data = ''
    
    # Build message
    message = (post_data + nonce + sign_endpoint).encode('utf-8')
    sha256_hash = hashlib.sha256(message).digest()
    secret_bytes = base64.b64decode(api_secret)
    hmac_sig = hmac.new(secret_bytes, sha256_hash, hashlib.sha512).digest()
    signature = base64.b64encode(hmac_sig).decode('utf-8')
    
    url = f'https://futures.kraken.com{url_endpoint}'
    headers = {
        'APIKey': api_key,
        'Nonce': nonce,
        'Authent': signature,
    }
    
    print(f'Sign endpoint: {sign_endpoint}')
    print(f'URL: {url}')
    print(f'Message: {post_data + nonce + sign_endpoint[:40]}...')
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        data = resp.json()
        result = data.get('result')
        if result == 'success':
            print('✅ SUCCESS!')
            accounts = data.get('accounts', {})
            print(f'Accounts keys: {list(accounts.keys())}')
            return True
        else:
            error = data.get('error', 'unknown')
            print(f'❌ Error: {error}')
            return False


async def main():
    print("=" * 60)
    print("KRAKEN FUTURES SIGNATURE FORMAT TEST")
    print("=" * 60)
    print(f"API Key: {api_key[:20]}...")
    print(f"Secret: {len(api_secret)} chars")
    print()
    
    # Test different endpoint formats for signature
    formats_to_try = [
        # (sign_endpoint, url_endpoint)
        ('/derivatives/api/v3/accounts', '/derivatives/api/v3/accounts'),  # Full path
        ('/api/v3/accounts', '/derivatives/api/v3/accounts'),              # Without /derivatives
    ]
    
    for sign_ep, url_ep in formats_to_try:
        print("-" * 40)
        success = await test_signature(sign_ep, url_ep)
        print()
        if success:
            print(f"\n✅ Working format: sign='{sign_ep}'")
            return
    
    print("\n❌ Aucun format de signature n'a fonctionné.")
    print("   Les clés API sont probablement des clés Kraken Spot, pas Futures.")
    print("   Créez des clés Futures sur: https://futures.kraken.com/trade/settings/api")


if __name__ == "__main__":
    asyncio.run(main())
