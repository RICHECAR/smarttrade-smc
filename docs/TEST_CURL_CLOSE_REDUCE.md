# Test CURL: Close/Reduce Position (v2 - Optimized)

## Prérequis
```bash
# Démarrer le serveur SMC Engine
cd c:/Users/cleor/Downloads/bitget-smc-auto-trader
python run.py
```

## 1. Vérifier l'état (Health Check)
```bash
curl -X GET http://localhost:8000/health
```

## 2. Lister les positions ouvertes (DB)
```bash
curl -X GET http://localhost:8000/api/live/trades/open
```

---

## 3. Fermer une position complète (CLOSE) - v2 Optimized

**Endpoint: `POST /api/live/position/close/{symbol}`**

### Caractéristiques v2:
- ✅ **Lock par symbole** : empêche double-close (UI + bot / double-clic)
- ✅ **Idempotent** : retourne OK si pas de position ou déjà en cours
- ✅ **reduceOnly=True** : toujours activé
- ✅ **Polling de confirmation** avec timeout configurable
- ✅ **Persistence DB** + **broadcast WebSocket**

```bash
# Fermeture automatique (détecte le side)
curl -X POST "http://localhost:8000/api/live/position/close/BTCUSDT"

# Fermeture d'un side spécifique
curl -X POST "http://localhost:8000/api/live/position/close/BTCUSDT?hold_side=long"
curl -X POST "http://localhost:8000/api/live/position/close/SOLUSDT?hold_side=short"

# Avec timeout custom et raison
curl -X POST "http://localhost:8000/api/live/position/close/BTCUSDT?reason=take_profit&timeout_sec=15"
```

### Paramètres

| Paramètre | Type | Défaut | Description |
|-----------|------|--------|-------------|
| `hold_side` | string | auto | "long" ou "short" (auto-détecté si omis) |
| `reason` | string | "manual_close" | Raison pour logs (ex: "take_profit", "stop_loss") |
| `timeout_sec` | float | 10.0 | Timeout de confirmation (1-60 sec) |

### Réponses

**Succès (position fermée):**
```json
{
  "status": "success",
  "success": true,
  "symbol": "BTCUSDT",
  "message": "Position fully closed",
  "order_id": "1234567890",
  "side": "long",
  "qty_closed": 0.001,
  "qty_remaining": 0.0,
  "close_price": 50000.0,
  "entry_price": 49500.0,
  "pnl": 0.50,
  "fee": 0.03,
  "percent_requested": 100.0,
  "close_reason": "manual_close",
  "trade_id": "uuid-xxx"
}
```

**Idempotent - Pas de position:**
```json
{
  "status": "already_closed",
  "success": true,
  "symbol": "BTCUSDT",
  "message": "No open position found"
}
```

**Idempotent - Close déjà en cours:**
```json
{
  "status": "in_progress",
  "success": true,
  "symbol": "BTCUSDT",
  "message": "Close already in progress (order_id=1234567890)",
  "order_id": "1234567890"
}
```

**Timeout (ordre envoyé, confirmation incertaine):**
```json
{
  "status": "unknown",
  "success": false,
  "symbol": "BTCUSDT",
  "message": "Order sent but confirmation timed out (remaining=0.0005)",
  "order_id": "1234567890"
}
```

---

## 4. Réduire une position (REDUCE) - v2 Optimized

**Endpoint: `POST /api/live/position/reduce/{symbol}`**

```bash
# Réduire de 50% (défaut)
curl -X POST "http://localhost:8000/api/live/position/reduce/BTCUSDT?pct=50"

# Réduire de 25%
curl -X POST "http://localhost:8000/api/live/position/reduce/SOLUSDT?pct=25&hold_side=long"

# Réduire de 75% avec raison
curl -X POST "http://localhost:8000/api/live/position/reduce/ETHUSDT?pct=75&reason=partial_tp"
```

### Paramètres

| Paramètre | Type | Défaut | Description |
|-----------|------|--------|-------------|
| `pct` | float | 50.0 | Pourcentage à réduire (1-99) |
| `hold_side` | string | auto | "long" ou "short" (auto-détecté si omis) |
| `reason` | string | "partial_close" | Raison pour logs |
| `timeout_sec` | float | 10.0 | Timeout de confirmation (1-60 sec) |

### Réponses

**Succès (réduction partielle):**
```json
{
  "status": "partial",
  "success": true,
  "symbol": "BTCUSDT",
  "message": "Position reduced by 50%",
  "order_id": "1234567891",
  "side": "long",
  "qty_closed": 0.0005,
  "qty_remaining": 0.0005,
  "close_price": 50500.0,
  "entry_price": 50000.0,
  "pnl": 0.25,
  "fee": 0.015,
  "percent_requested": 50.0,
  "close_reason": "partial_close",
  "trade_id": "uuid-xxx"
}
```

**Auto-conversion en full close (si remaining < min_qty):**
```json
{
  "status": "success",
  "success": true,
  "symbol": "BTCUSDT",
  "message": "Position fully closed",
  "qty_remaining": 0.0
}
```

**Rejeté (qty trop petite):**
```json
{
  "status": "rejected",
  "success": false,
  "symbol": "BTCUSDT",
  "message": "Quantity validation failed: qty 0.00001 < min_qty 0.001"
}
```

---

## 5. Vérifier le statut d'un close en cours

**Endpoint: `GET /api/live/position/close-status/{symbol}`**

```bash
curl -X GET "http://localhost:8000/api/live/position/close-status/BTCUSDT"
```

**Réponse:**
```json
{
  "in_progress": false,
  "order_id": null,
  "symbol": "BTCUSDT"
}
```

```json
{
  "in_progress": true,
  "order_id": "1234567890",
  "symbol": "BTCUSDT"
}
```

---

## 6. Consulter les fills d'un trade
```bash
curl -X GET "http://localhost:8000/api/live/trades/{trade_id}/fills"
```

**Réponse:**
```json
[
  {
    "id": "fill-uuid-1",
    "trade_id": "trade-uuid",
    "order_id": "bitget-order-id",
    "action": "OPEN",
    "qty": 0.001,
    "price": 50000.0,
    "fee": 0.03,
    "pnl": 0.0,
    "ts": "2026-02-20T10:30:00Z"
  },
  {
    "id": "fill-uuid-2",
    "trade_id": "trade-uuid",
    "order_id": "bitget-order-id-2",
    "action": "REDUCE",
    "qty": 0.0005,
    "price": 50500.0,
    "fee": 0.015,
    "pnl": 0.25,
    "ts": "2026-02-20T12:00:00Z"
  },
  {
    "id": "fill-uuid-3",
    "trade_id": "trade-uuid",
    "order_id": "bitget-order-id-3",
    "action": "CLOSE",
    "qty": 0.0005,
    "price": 51000.0,
    "fee": 0.015,
    "pnl": 0.25,
    "ts": "2026-02-20T14:00:00Z"
  }
]
```

---

## 7. WebSocket pour événements temps réel
```javascript
// Connexion WebSocket
const ws = new WebSocket('ws://localhost:8000/ws/trades');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  
  switch(data.type) {
    case 'trade_opened':
      console.log('Trade ouvert:', data.trade);
      break;
    case 'trade_closed':
      console.log('Trade fermé:', data.trade);
      break;
    case 'trade_reduced':
      console.log('Trade réduit:', data.trade, 'Fill:', data.fill);
      break;
    case 'position_closed':
      console.log('Position fermée (v2):', data.data);
      break;
    case 'position_reduced':
      console.log('Position réduite (v2):', data.data);
      break;
    case 'heartbeat':
      console.log('Heartbeat:', data.timestamp);
      break;
  }
};
```

### Événements WebSocket v2

**`position_closed`** (fermeture complète):
```json
{
  "type": "position_closed",
  "data": {
    "symbol": "BTCUSDT",
    "side": "long",
    "qty_closed": 0.001,
    "remaining_qty": 0.0,
    "order_id": "1234567890",
    "close_price": 50500.0,
    "pnl": 0.50,
    "reason": "manual_close"
  },
  "timestamp": "2026-02-21T10:30:00Z"
}
```

**`position_reduced`** (réduction partielle):
```json
{
  "type": "position_reduced",
  "data": {
    "symbol": "BTCUSDT",
    "side": "long",
    "qty_closed": 0.0005,
    "remaining_qty": 0.0005,
    "order_id": "1234567891",
    "close_price": 50500.0,
    "pnl": 0.25,
    "reason": "partial_close"
  },
  "timestamp": "2026-02-21T10:30:00Z"
}
```

---

## 8. Vérification du statut WebSocket
```bash
curl -X GET http://localhost:8000/ws/info
```

**Réponse:**
```json
{
  "ws_url": "ws://localhost:8000/ws/trades",
  "message": "Connect to WebSocket for real-time trade updates",
  "events": ["trade_opened", "trade_closed", "trade_reduced", "position_closed", "position_reduced", "heartbeat"],
  "connected_clients": 2
}
```

---

## Notes importantes

### Lock par symbole (Sécurité v2)
- Un seul close/reduce peut s'exécuter par symbole à la fois
- Si un autre close est demandé pendant l'exécution, retourne `status: in_progress` (pas d'erreur)
- Timeout de lock = 2 secondes

### Idempotence (v2)
| Situation | Réponse | success |
|-----------|---------|---------|
| Pas de position | `already_closed` | ✅ true |
| Close déjà en cours | `in_progress` | ✅ true |
| Position fermée | `success` | ✅ true |
| Erreur API | `failed` | ❌ false |
| Timeout confirmation | `unknown` | ❌ false |

### reduceOnly=True (Sécurité)
Les ordres de close/reduce utilisent `tradeSide: "close"` dans le payload Bitget, ce qui équivaut à `reduce_only=True`. Cela évite de "flipper" la position (passer de long à short ou inversement).

### Validation qty
- La quantité est arrondie au `qty_step` du contrat (ex: 0.001 pour BTC)
- Si qty < min_qty après arrondi → rejeté
- Si reduce_pct fait que remaining < min_qty → auto-close complet

### Confirmation avec polling (v2)
- Après envoi de l'ordre, le service poll la position toutes les 200ms (avec backoff)
- Si position diminue comme attendu → `success`
- Si timeout → `unknown` (ordre probablement exécuté mais pas confirmé)

### Observabilité (Logs structurés)
```
[CLOSE] START symbol=BTCUSDT percent=100% reason=manual_close timeout=10.0s hold_side=auto
[CLOSE] POSITION_FOUND symbol=BTCUSDT side=long size=0.001 entry=50000.0 mark=50500.0
[CLOSE] QTY_CALCULATED symbol=BTCUSDT close_qty=0.001 remaining=0.0 is_full=True
[CLOSE] ORDER_SENT order_id=1234567890 reduceOnly=True
[CLOSE] CONFIRMED symbol=BTCUSDT remaining_qty=0.0 pnl=0.50
[CLOSE] DB_UPDATED trade_id=uuid-xxx
[CLOSE] WS_BROADCAST symbol=BTCUSDT event=position_closed
```
