#!/usr/bin/env python
"""
Debug Kraken Futures Authentication
════════════════════════════════════

Low-level debug test to understand exactly what's needed for Kraken Futures auth.
"""

import asyncio
import base64
import hashlib
import hmac
import os
import time
from dotenv import load_dotenv
import httpx

load_dotenv()

# Configuration
KRAKEN_FUTURES_BASE = "https://futures.kraken.com"


def debug_signature(api_key: str, api_secret: str, endpoint: str, post_data: str = ""):
    """Generate signature with debug output."""
    
    nonce = str(int(time.time() * 1000))
    
    print(f"\n{'='*60}")
    print("DEBUG: Signature Generation")
    print(f"{'='*60}")
    print(f"Input:")
    print(f"  api_key:    {api_key[:12]}...")
    print(f"  api_secret: {api_secret[:16]}...")
    print(f"  endpoint:   {endpoint}")
    print(f"  post_data:  '{post_data}'")
    print(f"  nonce:      {nonce}")
    
    # Step 1: Concatenate
    message = (post_data + nonce + endpoint).encode("utf-8")
    print(f"\nStep 1 - Concatenate (postData + nonce + endpoint):")
    print(f"  message (raw): '{post_data + nonce + endpoint}'")
    print(f"  message (bytes): {message[:80]}...")
    
    # Step 2: SHA-256
    sha256_hash = hashlib.sha256(message).digest()
    print(f"\nStep 2 - SHA256 hash:")
    print(f"  hash (hex): {sha256_hash.hex()[:64]}...")
    
    # Step 3: Decode secret
    try:
        secret_decoded = base64.b64decode(api_secret)
        print(f"\nStep 3 - Decode API secret:")
        print(f"  secret length (decoded): {len(secret_decoded)} bytes")
    except Exception as e:
        print(f"\nStep 3 - ERROR decoding API secret: {e}")
        return None, nonce
    
    # Step 4: HMAC-SHA512
    hmac_sig = hmac.new(secret_decoded, sha256_hash, hashlib.sha512).digest()
    print(f"\nStep 4 - HMAC-SHA512:")
    print(f"  signature (hex): {hmac_sig.hex()[:64]}...")
    
    # Step 5: Base64 encode
    signature = base64.b64encode(hmac_sig).decode("utf-8")
    print(f"\nStep 5 - Base64 encode:")
    print(f"  signature: {signature[:40]}...")
    
    return signature, nonce


async def test_kraken_futures_auth():
    """Test Kraken Futures authentication with debug output."""
    
    api_key = os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_API_SECRET", "")
    
    print(f"\n{'='*60}")
    print("KRAKEN FUTURES AUTHENTICATION DEBUG")
    print(f"{'='*60}")
    
    print(f"\nCredentials:")
    print(f"  KRAKEN_API_KEY:    {api_key[:16] + '...' if len(api_key) > 16 else 'NOT SET'}")
    print(f"  KRAKEN_API_SECRET: {'SET (' + str(len(api_secret)) + ' chars)' if api_secret else 'NOT SET'}")
    
    if not api_key or not api_secret:
        print("\n[ERROR] Missing credentials")
        return
    
    # Test endpoint
    endpoint = "/derivatives/api/v3/accounts"
    url = KRAKEN_FUTURES_BASE + endpoint
    
    print(f"\nTarget URL: {url}")
    
    # Generate signature
    signature, nonce = debug_signature(api_key, api_secret, endpoint)
    
    if not signature:
        print("\n[ERROR] Signature generation failed")
        return
    
    # Prepare headers
    headers = {
        "APIKey": api_key,
        "Nonce": nonce,
        "Authent": signature,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    
    print(f"\n{'='*60}")
    print("REQUEST DETAILS")
    print(f"{'='*60}")
    print(f"Method: GET")
    print(f"URL: {url}")
    print(f"Headers:")
    for k, v in headers.items():
        if k == "Authent":
            print(f"  {k}: {v[:40]}...")
        else:
            print(f"  {k}: {v}")
    
    # Make request
    print(f"\n{'='*60}")
    print("RESPONSE")
    print(f"{'='*60}")
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers=headers)
        
        print(f"Status: {response.status_code}")
        print(f"Response: {response.text[:500]}")
        
        if response.status_code == 200:
            data = response.json()
            if data.get("result") == "error":
                print(f"\n[API ERROR] {data.get('error')}")
                
                if "authenticationError" in str(data.get("error", "")):
                    print("\n" + "!"*60)
                    print("AUTHENTICATION FAILED!")
                    print("!"*60)
                    print("\nPossible causes:")
                    print("1. You're using Kraken SPOT API keys, not FUTURES keys")
                    print("   - Spot keys: https://www.kraken.com/u/security/api")
                    print("   - Futures keys: https://futures.kraken.com/trade/settings/api")
                    print("2. The API key doesn't have the required permissions")
                    print("3. The API key/secret format is incorrect")
            else:
                print("\n[SUCCESS] Authentication working!")
                print(f"Accounts data: {data}")
        else:
            print(f"\n[HTTP ERROR] Status {response.status_code}")


if __name__ == "__main__":
    asyncio.run(test_kraken_futures_auth())
