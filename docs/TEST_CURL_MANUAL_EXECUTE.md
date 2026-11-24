# Test CURL: Manual Trade Execution

## Prérequis
```bash
# Démarrer le serveur SMC Engine
cd c:/Users/cleor/Downloads/bitget-smc-auto-trader
.venv/Scripts/python.exe -m uvicorn smc_futures.api_smc_engine:app --host 0.0.0.0 --port 8000
```

## 1. Vérifier le statut de l'exécution manuelle
```bash
curl -X GET http://localhost:8000/api/manual/status
```

**Réponse attendue:**
```json
{
  "enabled": true,
  "executor_ready": true,
  "order_manager_ready": false,
  "locked_symbols": [],
  "timestamp": "2026-02-20T16:00:00Z"
}
```

## 2. Pre-flight check (valider avant exécution)
```bash
curl -X POST http://localhost:8000/api/manual/preflight \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "XRPUSDT",
    "side": "short",
    "entry_price": 1.4011,
    "stop_loss": 1.4062,
    "take_profit": 1.3807,
    "leverage": 10,
    "risk_pct": 1.0
  }'
```

**Réponse si OK:**
```json
{
  "can_execute": true,
  "reason": "OK",
  "computed_qty": 150.0,
  "min_qty": 1.0,
  "min_notional": 5.0,
  "notional": 210.165,
  "risk_amount": 10.0,
  "risk_deviation_pct": 5.2,
  "max_risk_deviation_pct": 50.0,
  "account_balance": 1000.0,
  "has_open_position": false,
  "is_locked": false
}
```

**Réponse si rejeté (position ouverte):**
```json
{
  "can_execute": false,
  "reason": "POSITION_ALREADY_OPEN",
  "has_open_position": true
}
```

**Réponse si rejeté (qty trop petite):**
```json
{
  "can_execute": false,
  "reason": "raw_qty < min_qty",
  "computed_qty": 0.5,
  "min_qty": 1.0
}
```

## 3. Exécuter un trade manuel
```bash
curl -X POST http://localhost:8000/api/manual/execute \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "XRPUSDT",
    "side": "short",
    "entry_price": 1.4011,
    "stop_loss": 1.4062,
    "take_profit": 1.3807,
    "leverage": 10,
    "risk_pct": 1.0
  }'
```

**Réponse succès:**
```json
{
  "success": true,
  "reason": "OK",
  "order_id": "1234567890123456789",
  "symbol": "XRPUSDT",
  "side": "short",
  "qty": 150.0,
  "entry_price": 1.4011,
  "stop_loss": 1.4062,
  "take_profit": 1.3807,
  "trade_id": "uuid-xxx"
}
```

**Réponse erreur:**
```json
{
  "success": false,
  "reason": "POSITION_ALREADY_OPEN"
}
```

---

## Codes de rejet possibles

| Reason | Description |
|--------|-------------|
| `MANUAL_TRADES_DISABLED` | `ALLOW_MANUAL_TRADES=0` dans config |
| `EXECUTOR_NOT_AVAILABLE` | Clés API non configurées |
| `EXECUTION_IN_PROGRESS` | Un trade est déjà en cours sur ce symbole (lock) |
| `POSITION_ALREADY_OPEN` | Position existante sur ce symbole |
| `ZERO_BALANCE` | Balance compte = 0 |
| `INVALID_STOP_DISTANCE` | SL invalide (entry = SL) |
| `raw_qty < min_qty` | Quantité calculée < minimum du contrat |
| `notional < min_notional` | Notional calculé < minimum ($5) |
| `risk_deviation_exceeds_max` | Déviation de risque > 50% |

---

## Configuration

Dans `.env`:
```env
# Activer/désactiver les trades manuels
ALLOW_MANUAL_TRADES=1
```

---

## Flow UI

1. L'utilisateur voit un signal avec bouton "Execute Live Order"
2. Le bouton est **disabled** si:
   - Mode simulation (liveMode=false)
   - Pas de clés API configurées
   - Position déjà ouverte sur ce symbole
   - Exécution en cours
3. Au clic:
   - Preflight check (validation complète)
   - Affichage détails (qty, risk, etc.)
   - Confirmation utilisateur
   - Exécution via même pipeline que le bot
   - Toast succès/erreur avec détails
4. Après succès:
   - Trade persisté en SQLite
   - Event WS `trade_opened` broadcasté
   - Positions rafraîchies

---

## Notes

- Le bouton utilise le **même pipeline** que le bot (contract_specs, sizing, preflight, order_manager)
- Un **lock par symbole** empêche les double-clics ou conflits bot/manuel
- Les logs sont marqués `origin=manual_button` pour distinguer des trades auto
- Le trade est persisté dans `live_trades` avec `preset=manual_button`
