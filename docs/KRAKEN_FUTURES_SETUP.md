# Configuration Kraken Futures — Guide de démarrage

## ⚠️ IMPORTANT : Clés API Kraken

Kraken utilise **deux systèmes d'API séparés** qui NE sont PAS interchangeables :

| Plateforme | URL API | Créer des clés |
|------------|---------|----------------|
| Kraken Spot | `api.kraken.com` | https://www.kraken.com/u/security/api |
| **Kraken Futures** | `futures.kraken.com` | https://futures.kraken.com/trade/settings/api |

## 📋 Étapes de configuration

### 1. Créer des clés API Kraken Futures

1. Connectez-vous à https://futures.kraken.com
2. Allez dans **Settings** → **API**
3. Cliquez sur **Create API Key**
4. Donnez un nom et sélectionnez les permissions :
   - ✅ `Read` - Pour lire le solde
   - ✅ `Trade` - Pour passer des ordres (si nécessaire)
   - ✅ `Withdrawal` - NON recommandé (sécurité)
5. Copiez la **Public Key** et la **Private Key** (base64)

### 2. Mettre à jour le fichier .env

```env
# Kraken Futures credentials  
KRAKEN_API_KEY=votre_public_key_futures
KRAKEN_API_SECRET=votre_private_key_futures_base64
KRAKEN_ENABLED=true
```

### 3. Tester la connexion

```bash
# Test rapide
python tests/test_kraken_auth.py

# Debug détaillé
python tests/debug_kraken_auth.py
```

## 🔐 Algorithme de signature Kraken Futures

L'authentification Kraken Futures utilise HMAC-SHA512 :

```
1. message = postData + nonce + endpoint
2. hash = SHA256(message)
3. signature = HMAC-SHA512(hash, base64_decode(api_secret))
4. authent = base64_encode(signature)
```

Headers requis :
- `APIKey` : Clé publique
- `Nonce` : Timestamp en millisecondes
- `Authent` : Signature base64

## 🧪 Endpoints supportés

| Route API | Méthode | Description |
|-----------|---------|-------------|
| `/api/kraken/balance` | GET | Solde du compte |
| `/api/kraken/positions` | GET | Positions ouvertes |
| `/api/kraken/status` | GET | Test de connexion |
| `/api/kraken/check-margin` | POST | Vérifier marge suffisante |

## ❌ Erreur "authenticationError"

Si vous obtenez cette erreur, vérifiez :

1. **Mauvaises clés** : Vous utilisez des clés Kraken Spot au lieu de Futures
2. **Permissions** : La clé n'a pas les permissions `Read`
3. **Expiration** : La clé a expiré ou a été révoquée
4. **Format** : Le secret doit être en base64 (caractères alphanumériques + `/+=`)

## 📡 Exemple de code Python

```python
from backend.services.kraken_balance_service import get_kraken_balance_client

async def check_kraken_balance():
    client = get_kraken_balance_client()
    
    # Test connexion
    status = await client.test_connection()
    if not status['success']:
        print(f"Erreur: {status['error']}")
        return
    
    # Obtenir le solde
    balance = await client.get_balance()
    print(f"Disponible: ${balance.available:.2f}")
    print(f"Total: ${balance.total:.2f}")
    print(f"PnL non réalisé: ${balance.unrealized_pnl:.4f}")
```

## 📊 Structure de réponse balance

```json
{
  "available": 5000.00,
  "total": 5250.50,
  "margin_used": 250.50,
  "unrealized_pnl": 0.0,
  "currency": "USD",
  "timestamp": "2024-01-15T10:30:00Z"
}
```
