# 🔧 Résumé de la Correction Multi-Broker

## Problème initial

L'architecture initiale du bot utilisait des appels directs aux clients Bitget, rendant impossible l'ajout d'autres brokers sans modifier le code en profondeur.

---

## Solution : Architecture BrokerRouter unifiée

### Principe
Un routeur central (`BrokerRouter`) expose une interface unifiée pour tous les brokers. Le code métier n'interagit qu'avec le routeur, jamais directement avec un client broker.

```
┌─────────────────┐
│   SMC Engine     │
│  (Signaux)       │
└────────┬────────┘
         │
┌────────▼────────┐
│  Order Manager   │
│  (Exécution)     │
└────────┬────────┘
         │
┌────────▼────────┐
│  BrokerRouter    │◄── Interface unifiée
├─────────────────┤
│ ┌─────┐ ┌─────┐│
│ │Bitget│ │Kraken││
│ └─────┘ └─────┘│
└─────────────────┘
```

### Interface unifiée

```python
class BrokerRouter:
    async def place_order(self, symbol, side, size, leverage) -> dict
    async def cancel_order(self, order_id) -> dict
    async def get_positions(self) -> list
    async def get_account_info() -> dict
    async def test_connection() -> dict
```

---

## Wrappers Broker

### Bitget (`backend/bitget_v2_mix_client.py`)
- Client REST API v2 Mix
- Support futures USDT-M
- Gestion automatique de la signature HMAC
- Rate limiting intégré

### Kraken (`backend/kraken_futures_client.py`)
- Client REST Kraken Futures
- Support contrats perpétuels
- Authentification Nonce + signature
- Gestion des erreurs spécifiques Kraken

---

## Migration

### Avant (appel direct)
```python
# ❌ Ancien code - couplage fort
from backend.bitget_v2_mix_client import BitgetClient

client = BitgetClient(api_key, secret, passphrase)
result = await client.place_order(...)
```

### Après (via BrokerRouter)
```python
# ✅ Nouveau code - découplage total
from backend.broker_router import BrokerRouter

router = BrokerRouter()  # Charge le broker depuis .env
result = await router.place_order(...)
```

---

## Configuration

### Broker unique
```env
BROKER=bitget
BITGET_API_KEY=...
BITGET_API_SECRET=...
BITGET_PASSPHRASE=...
```

### Multi-broker
```env
BROKER=multi
BITGET_API_KEY=...
BITGET_API_SECRET=...
BITGET_PASSPHRASE=...
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...
```

### Basculement de broker
```env
# Passer de Bitget à Kraken : une seule variable à changer
BROKER=kraken
```

---

## Avantages

| Aspect | Avant | Après |
|--------|-------|-------|
| Ajout d'un broker | Modifier tout le code | Créer 1 wrapper |
| Changement de broker | Recompiler | Changer 1 variable `.env` |
| Tests | Mock du client spécifique | Mock du BrokerRouter |
| Multi-broker | Impossible | Natif |
| Failover | Manuel | Automatique |

---

## Tests

```bash
# Tests du BrokerRouter
python -m pytest tests/ -k "broker" -v

# Tests multi-broker
python -m pytest tests/ -k "multi_broker" -v
```

Couverture : 30+ tests unitaires pour le routeur, incluant :
- Basculement automatique en cas de panne
- Gestion des erreurs par broker
- Mode multi-broker simultané
- Timeout et retry
