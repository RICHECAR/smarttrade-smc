#!/usr/bin/env python
"""Debug Kraken Futures authentication with different endpoint formats."""

import asyncio
import base64
import hashlib
import hmac
import os
import time
import httpx
from dotenv import load_dotenv
load_dotenv()

KRAKEN_FUTURES_URL = 'https://futures.kraken.com'
api_key = os.getenv('KRAKEN_API_KEY', '')
api_secret = os.getenv('KRAKEN_API_SECRET', '')

async def test_endpoint(endpoint: str, use_slash_in_sig: bool = True):
    """Test a specific endpoint format."""
    print(f"\n{'='*60}")
    print(f"Endpoint for signature: '{endpoint}'")
    print(f"Using slash in signature: {use_slash_in_sig}")
    print('='*60)
    
    nonce = str(int(time.time() * 1000))
    post_data = ''
    
    # Build signature message
    sig_endpoint = endpoint if use_slash_in_sig else endpoint.lstrip('/')
    message = (post_data + nonce + sig_endpoint).encode('utf-8')
    
    print(f"Message: '{post_data + nonce + sig_endpoint}'")
    
    sha256_hash = hashlib.sha256(message).digest()
    secret_bytes = base64.b64decode(api_secret)
    hmac_sig = hmac.new(secret_bytes, sha256_hash, hashlib.sha512).digest()
    signature = base64.b64encode(hmac_sig).decode('utf-8')
    
    # URL always needs the slash
    url = KRAKEN_FUTURES_URL + ('/' if not endpoint.startswith('/') else '') + endpoint.lstrip('/')
    
    headers = {
        'APIKey': api_key,
        'Nonce': nonce,
        'Authent': signature,
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    print(f"URL: {url}")
    print(f"Headers: APIKey={api_key[:12]}... Nonce={nonce}")
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        data = resp.json()
        
        if data.get('result') == 'success':
            print(f"\n✓ SUCCESS!")
            accounts = data.get('accounts', {})
            print(f"  Accounts keys: {list(accounts.keys())}")
            return True
        else:
            error = data.get('error', 'unknown')
            print(f"\n✗ FAILED: {error}")
            return False


async def main():
    print("KRAKEN FUTURES AUTH DEBUG")
    print(f"API Key: {api_key}")
    print(f"Secret: {api_secret[:20]}... ({len(api_secret)} chars)")
    
    # Test various combinations
    tests = [
        # (endpoint, use_slash_in_sig)
        ('/derivatives/api/v3/accounts', True),
        ('/derivatives/api/v3/accounts', False),
        ('derivatives/api/v3/accounts', True),
        ('derivatives/api/v3/accounts', False),
    ]
    
    for endpoint, use_slash in tests:
        try:
            success = await test_endpoint(endpoint, use_slash)
            if success:
                print(f"\n\n*** WORKING CONFIGURATION FOUND! ***")
                print(f"    Endpoint: '{endpoint}'")
                print(f"    Slash in sig: {use_slash}")
                return
        except Exception as e:
            print(f"Exception: {e}")
    
    print("\n\nNo configuration worked. Possible issues:")
    print("1. API keys are for Kraken Spot, not Futures")
    print("2. API key doesn't have 'Read' permission")
    print("3. API key/secret format is invalid")


if __name__ == "__main__":
    asyncio.run(main())
