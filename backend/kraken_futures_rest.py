"""
Kraken Futures REST Client — Correct Implementation
════════════════════════════════════════════════════

⚠️  IMPORTANT: Ce client est pour Kraken FUTURES uniquement!
    Endpoint: https://futures.kraken.com/derivatives/api/v3/
    Clés API: https://futures.kraken.com/trade/settings/api

Algorithme de signature Kraken Futures:
  1. message = postData + nonce + endpoint
  2. hash = SHA256(message)  
  3. signature = HMAC-SHA512(hash, base64_decode(secret))
  4. return base64_encode(signature)

Reference: https://docs.futures.kraken.com/#authentication
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("smarttrade.kraken_futures_rest")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

KRAKEN_FUTURES_BASE_URL = "https://futures.kraken.com"
KRAKEN_FUTURES_URL_PATH = "/derivatives/api/v3"  # For URL
KRAKEN_FUTURES_SIGN_PATH = "/api/v3"             # For signature (NO /derivatives!)
DEFAULT_TIMEOUT = 15.0


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class KrakenFuturesBalance:
    """Parsed Kraken Futures balance."""
    available: float
    total: float
    margin_used: float
    unrealized_pnl: float
    currency: str = "USD"
    timestamp: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": round(self.available, 2),
            "total": round(self.total, 2),
            "margin_used": round(self.margin_used, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "currency": self.currency,
            "timestamp": self.timestamp or datetime.now(timezone.utc).isoformat()
        }


# ══════════════════════════════════════════════════════════════════════════════
# SIGNATURE FUNCTION - KRAKEN FUTURES
# ══════════════════════════════════════════════════════════════════════════════

def sign_kraken_futures(
    api_secret: str,
    endpoint: str,
    nonce: str,
    post_data: str = ""
) -> str:
    """
    Signe une requête REST Kraken Futures.
    
    Algorithme officiel Kraken Futures:
    1. message = postData + nonce + endpoint
    2. hash = SHA256(message)
    3. signature = HMAC-SHA512(hash, base64_decode(secret))
    4. return base64_encode(signature)
    
    Args:
        api_secret: Secret API encodé en base64
        endpoint: Chemin complet (ex: /derivatives/api/v3/accounts)
        nonce: Nonce unique (timestamp en millisecondes)
        post_data: Données POST encodées URL (vide pour GET)
        
    Returns:
        Signature encodée en base64
    """
    # Step 1: Construire le message
    message = (post_data + nonce + endpoint).encode("utf-8")
    logger.debug("[SIGN] Message: %s", post_data + nonce + endpoint)
    
    # Step 2: Hash SHA-256 du message
    sha256_hash = hashlib.sha256(message).digest()
    
    # Step 3: Décoder le secret et signer avec HMAC-SHA512
    try:
        secret_bytes = base64.b64decode(api_secret)
    except Exception as e:
        logger.error("[SIGN] Échec décodage secret: %s", e)
        raise ValueError(f"Format secret API invalide: {e}")
    
    hmac_sig = hmac.new(secret_bytes, sha256_hash, hashlib.sha512).digest()
    
    # Step 4: Encoder la signature en base64
    signature = base64.b64encode(hmac_sig).decode("utf-8")
    logger.debug("[SIGN] Signature générée: %s...", signature[:20])
    
    return signature


def get_kraken_futures_headers(
    api_key: str,
    api_secret: str,
    endpoint: str,
    post_data: str = ""
) -> Dict[str, str]:
    """
    Génère les headers authentifiés pour Kraken Futures.
    
    Headers requis:
    - APIKey: Clé publique
    - Nonce: Timestamp en millisecondes
    - Authent: Signature HMAC-SHA512
    """
    nonce = str(int(time.time() * 1000))
    signature = sign_kraken_futures(api_secret, endpoint, nonce, post_data)
    
    headers = {
        "APIKey": api_key,
        "Nonce": nonce,
        "Authent": signature,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    logger.debug("[HEADERS] APIKey=%s... Nonce=%s", api_key[:12], nonce)
    return headers


# ══════════════════════════════════════════════════════════════════════════════
# KRAKEN FUTURES REST CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class KrakenFuturesRESTClient:
    """
    Client REST Kraken Futures avec authentification correcte.
    
    Usage:
        client = KrakenFuturesRESTClient()
        balance = await client.get_balance()
        print(f"Disponible: ${balance.available}")
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT
    ):
        self.api_key = api_key or os.getenv("KRAKEN_API_KEY", "")
        self.api_secret = api_secret or os.getenv("KRAKEN_API_SECRET", "")
        self.timeout = timeout
        self._balance_cache: Optional[KrakenFuturesBalance] = None
        self._cache_time: float = 0
        
        if self.api_key:
            logger.info("[KRAKEN_FUTURES] Client initialisé | key=%s...", self.api_key[:16])
        else:
            logger.warning("[KRAKEN_FUTURES] Aucune clé API configurée")
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Exécute une requête REST Kraken Futures authentifiée.
        
        Args:
            method: Méthode HTTP (GET/POST)
            endpoint: Chemin endpoint (ex: /accounts)
            params: Paramètres query pour GET
            data: Données body pour POST
            
        Returns:
            Réponse JSON parsée
            
        Raises:
            ValueError: Erreur API
            httpx.HTTPError: Erreur réseau
        """
        # IMPORTANT: URL utilise /derivatives/api/v3, mais signature utilise /api/v3
        url_endpoint = f"{KRAKEN_FUTURES_URL_PATH}{endpoint}"
        sign_endpoint = f"{KRAKEN_FUTURES_SIGN_PATH}{endpoint}"
        url = f"{KRAKEN_FUTURES_BASE_URL}{url_endpoint}"
        
        # Construire les données POST
        post_data = ""
        if data:
            post_data = urllib.parse.urlencode(data)
        
        # Obtenir les headers authentifiés (utiliser sign_endpoint, pas url_endpoint!)
        headers = get_kraken_futures_headers(
            self.api_key, self.api_secret, sign_endpoint, post_data
        )
        
        logger.info("[KRAKEN_FUTURES] %s %s", method.upper(), url_endpoint)
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if method.upper() == "GET":
                resp = await client.get(url, headers=headers, params=params)
            else:
                resp = await client.post(url, headers=headers, content=post_data)
            
            logger.debug("[KRAKEN_FUTURES] HTTP Status: %d", resp.status_code)
            
            if resp.status_code != 200:
                logger.error("[KRAKEN_FUTURES] HTTP %d: %s", resp.status_code, resp.text[:300])
                resp.raise_for_status()
            
            result = resp.json()
            
            # Vérifier les erreurs API
            if result.get("result") == "error":
                error = result.get("error", "Erreur inconnue")
                logger.error("[KRAKEN_FUTURES] Erreur API: %s", error)
                raise ValueError(f"Kraken Futures API error: {error}")
            
            logger.debug("[KRAKEN_FUTURES] Réponse OK: %s", str(result)[:200])
            return result
    
    async def get_accounts_raw(self) -> Dict[str, Any]:
        """
        Récupère les données brutes du compte depuis /accounts.
        
        Returns:
            Réponse API brute avec structure accounts
        """
        return await self._request("GET", "/accounts")
    
    async def get_balance(self, force_refresh: bool = False) -> KrakenFuturesBalance:
        """
        Récupère le solde du compte Kraken Futures.
        
        Parse la réponse /accounts pour extraire les infos de solde.
        Résultats cachés pendant 60 secondes.
        
        Args:
            force_refresh: Ignorer le cache
            
        Returns:
            KrakenFuturesBalance avec available, total, margin_used, pnl
        """
        # Vérifier le cache (TTL 60s)
        if not force_refresh and self._balance_cache:
            if time.time() - self._cache_time < 60:
                logger.debug("[KRAKEN_FUTURES] Retour du cache balance")
                return self._balance_cache
        
        # Récupérer les données fraîches
        data = await self.get_accounts_raw()
        
        accounts = data.get("accounts", {})
        server_time = data.get("serverTime", "")
        
        logger.debug("[KRAKEN_FUTURES] Accounts reçus: %s", list(accounts.keys()))
        
        # Essayer d'abord le compte flex (multi-collateral)
        flex = accounts.get("flex", {})
        if flex:
            balance = KrakenFuturesBalance(
                available=float(flex.get("availableMargin", 0)),
                total=float(flex.get("portfolioValue", 0)),
                margin_used=float(flex.get("initialMargin", 0)),
                unrealized_pnl=float(flex.get("pnl", 0)),
                timestamp=server_time
            )
            logger.info("[KRAKEN_FUTURES] Balance FLEX: available=%.2f total=%.2f", 
                       balance.available, balance.total)
        else:
            # Fallback vers le compte cash
            cash = accounts.get("cash", {})
            balances = cash.get("balances", {})
            usd = float(balances.get("usd", balances.get("USD", 0)))
            
            balance = KrakenFuturesBalance(
                available=usd,
                total=usd,
                margin_used=0.0,
                unrealized_pnl=0.0,
                timestamp=server_time
            )
            logger.info("[KRAKEN_FUTURES] Balance CASH: available=%.2f", balance.available)
        
        # Mettre à jour le cache
        self._balance_cache = balance
        self._cache_time = time.time()
        
        return balance
    
    async def get_balance_dict(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Retourne le solde en dict (pour réponses API)."""
        balance = await self.get_balance(force_refresh)
        return balance.to_dict()
    
    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """
        Récupère les positions ouvertes depuis /openpositions.
        
        Returns:
            Liste des positions ouvertes
        """
        data = await self._request("GET", "/openpositions")
        positions = data.get("openPositions", [])
        logger.info("[KRAKEN_FUTURES] %d positions ouvertes", len(positions))
        return positions
    
    async def test_connection(self) -> Dict[str, Any]:
        """
        Teste la connexion et l'authentification API.
        
        Returns:
            Dict avec success, authenticated, balance, error
        """
        try:
            balance = await self.get_balance(force_refresh=True)
            return {
                "success": True,
                "authenticated": True,
                "balance": balance.to_dict(),
                "error": None
            }
        except ValueError as e:
            error_msg = str(e)
            logger.error("[KRAKEN_FUTURES] Test connexion échoué: %s", error_msg)
            return {
                "success": False,
                "authenticated": False,
                "balance": None,
                "error": error_msg
            }
        except Exception as e:
            logger.error("[KRAKEN_FUTURES] Erreur connexion: %s", e)
            return {
                "success": False,
                "authenticated": False,
                "balance": None,
                "error": f"Erreur réseau: {e}"
            }


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_client_instance: Optional[KrakenFuturesRESTClient] = None


def get_kraken_futures_rest_client() -> KrakenFuturesRESTClient:
    """Retourne l'instance singleton du client Kraken Futures REST."""
    global _client_instance
    if _client_instance is None:
        _client_instance = KrakenFuturesRESTClient()
    return _client_instance


# ══════════════════════════════════════════════════════════════════════════════
# TEST
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    """Test du client Kraken Futures REST."""
    print("=" * 60)
    print("KRAKEN FUTURES REST CLIENT - TEST")
    print("=" * 60)
    
    api_key = os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_API_SECRET", "")
    
    print(f"\nAPI Key: {api_key[:20]}..." if len(api_key) > 20 else f"\nAPI Key: {api_key or 'NON CONFIGURÉE'}")
    print(f"Secret: {len(api_secret)} caractères")
    
    if not api_key or not api_secret:
        print("\n❌ Clés API manquantes dans .env")
        print("   Créez vos clés sur: https://futures.kraken.com/trade/settings/api")
        return
    
    client = KrakenFuturesRESTClient(api_key, api_secret)
    
    print("\n" + "-" * 40)
    print("Test de connexion...")
    print("-" * 40)
    
    result = await client.test_connection()
    
    if result["success"]:
        print("\n✅ SUCCÈS - Authentification OK!")
        b = result["balance"]
        print(f"\n  Disponible:   ${b['available']:,.2f}")
        print(f"  Total:        ${b['total']:,.2f}")
        print(f"  Marge:        ${b['margin_used']:,.2f}")
        print(f"  PnL:          ${b['unrealized_pnl']:,.4f}")
    else:
        print(f"\n❌ ÉCHEC: {result['error']}")
        
        if "authenticationError" in str(result["error"]):
            print("\n  Causes possibles:")
            print("  1. Clés Kraken SPOT au lieu de FUTURES")
            print("     → Créez des clés sur: https://futures.kraken.com/trade/settings/api")
            print("  2. Permission 'Read' manquante sur la clé")
            print("  3. Clé expirée ou révoquée")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    asyncio.run(main())
