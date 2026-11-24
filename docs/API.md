# API Reference — SMC Trading Bot

## Base URL

```
http://localhost:8000
```

## Authentication

La plupart des endpoints sont publics (pour le frontend). Les opérations sur Bitget utilisent les clés configurées côté serveur.

---

## Health & Status

### GET /api/health

Vérifie l'état du backend.

**Response:**
```json
{
  "status": "ok",
  "service": "smc-trader",
  "version": "2.0",
  "trading_mode": "live",
  "auto_execute_mode": "true",
  "timestamp": "2026-02-20T12:00:00Z"
}
```

> **Note Frontend:** Le frontend vérifie que `service === "smc-trader"` pour afficher "CONNECTED".

---

### GET /api/status

Statut détaillé du trading bot.

**Response:**
```json
{
  "trading_mode": "live",
  "auto_execute_mode": "true",
  "auto_execute_enabled": true,
  "shadow_mode": false,
  "protection_status": {
    "kill_switch": false,
    "kill_switch_reason": ""
  },
  "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"],
  "timeframes": {
    "htf": "4h",
    "mtf": "15m",
    "ltf": "5m"
  },
  "safeguards": {
    "max_trades_per_day": 24,
    "max_open_per_symbol": 1,
    "trade_cooldown_sec": 300,
    "min_rr": 1.5
  }
}
```

---

## Account & Balance

### GET /api/v2/account/balance

Récupère le solde du compte USDT-FUTURES.

**Response:**
```json
{
  "code": "00000",
  "data": [{
    "available": 20.27,
    "equity": 23.44,
    "marginCoin": "USDT"
  }]
}
```

---

### GET /api/v2/position/leverage

Récupère le leverage configuré pour un symbole.

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| symbol | string | Yes | Trading pair (e.g., BTCUSDT) |

**Response:**
```json
{
  "code": "00000",
  "data": {
    "symbol": "BTCUSDT",
    "leverage": 10,
    "marginMode": "isolated"
  }
}
```

---

## Market Data

### GET /api/v1/market/ticker

Récupère le prix actuel d'un symbole.

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| symbol | string | Yes | Trading pair |
| productType | string | No | Default: USDT-FUTURES |

**Response:**
```json
{
  "symbol": "BTCUSDT",
  "price": 67759.80,
  "mark_price": 67759.80,
  "index_price": 67760.00,
  "bid1": 67759.00,
  "ask1": 67760.00,
  "source_field": "markPrice"
}
```

---

### GET /api/smc/market/candles

Récupère les bougies OHLCV avec cache SQLite.

**Query Parameters:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| symbol | string | Yes | - | Trading pair |
| tf | string | No | 5m | Timeframe: 1m, 5m, 15m, 30m, 1h, 4h, 1d |
| limit | int | No | 200 | Number of candles (10-1000) |

**Response:**
```json
[
  {"ts": 1729123456000, "open": 67000.0, "high": 67500.0, "low": 66800.0, "close": 67200.0, "volume": 1234.5},
  ...
]
```

---

### GET /api/v1/positions

Récupère les positions ouvertes.

**Response:**
```json
{
  "code": "00000",
  "positions": [
    {
      "symbol": "XRPUSDT",
      "side": "long",
      "size": 100.0,
      "entryPrice": 1.42,
      "markPrice": 1.43,
      "unrealizedPnl": 1.00,
      "leverage": 10
    }
  ],
  "count": 1
}
```

---

## Order Execution

### POST /api/v2/order/dry-run

Calcule la taille de position sans envoyer d'ordre (risk sizing).

**Request Body:**
```json
{
  "symbol": "BTCUSDT",
  "side": "long",
  "entry_price": 67000.0,
  "stop_loss": 66500.0,
  "take_profit": 68000.0,
  "risk_pct": 1.0,
  "leverage": 10
}
```

**Response (Success):**
```json
{
  "ok": true,
  "orderId": "DRYRUN-538799A1E888",
  "status": "simulated",
  "payloadPreview": {
    "symbol": "BTCUSDT",
    "side": "long",
    "size": 0.004,
    "stopLoss": 66500.0,
    "takeProfit": 68000.0
  },
  "riskCalc": {
    "accountBalance": 23.28,
    "riskPct": 1.0,
    "riskAmount": 0.23,
    "slDistance": 500.0,
    "rawSize": 0.00465,
    "qtyStep": 0.001,
    "minQty": 0.001
  }
}
```

**Response (Size too small):**
```json
{
  "ok": false,
  "reason": "Computed size 0.0004 below minimum 0.001"
}
```

---

### POST /api/v1/order

Place un ordre sur Bitget (requiert AUTO_EXECUTE=true ou SHADOW_MODE).

**Request Body:**
```json
{
  "symbol": "BTCUSDT",
  "side": "buy",
  "orderType": "market",
  "size": 0.004,
  "stopLoss": 66500.0,
  "takeProfit": 68000.0
}
```

**Response:**
```json
{
  "success": true,
  "orderId": "123456789",
  "status": "filled",
  "fillPrice": 67000.0
}
```

---

## Manual Execution API

### POST /api/manual/preflight

Vérifie si un trade manuel peut être exécuté (validation + sizing).

**Request Body:**
```json
{
  "symbol": "BTCUSDT",
  "side": "long",
  "entry_price": 67000.0,
  "stop_loss": 66500.0,
  "take_profit": 68000.0,
  "leverage": 10,
  "risk_pct": 1.0
}
```

**Response:**
```json
{
  "can_execute": true,
  "reason": "Preflight OK",
  "computed_qty": 0.004,
  "min_qty": 0.001,
  "min_notional": 5.0,
  "notional": 268.0,
  "risk_amount": 0.23,
  "account_balance": 23.28,
  "has_open_position": false,
  "is_locked": false
}
```

---

### POST /api/manual/execute

Exécute un trade manuel (si preflight OK).

**Request Body:**
```json
{
  "symbol": "BTCUSDT",
  "side": "long",
  "entry_price": 67000.0,
  "stop_loss": 66500.0,
  "take_profit": 68000.0,
  "leverage": 10,
  "risk_pct": 1.0
}
```

**Response:**
```json
{
  "success": true,
  "reason": "Order placed successfully",
  "order_id": "123456789",
  "symbol": "BTCUSDT",
  "side": "long",
  "qty": 0.004,
  "entry_price": 67000.0,
  "stop_loss": 66500.0,
  "take_profit": 68000.0
}
```

---

### GET /api/manual/status

Statut du système de trades manuels.

**Response:**
```json
{
  "manual_trades_allowed": true,
  "executor_ready": true,
  "active_locks": [],
  "recent_executions": []
}
```

---

## Bot Control

### POST /api/bot/kill-switch

Active/désactive le kill-switch (bloque tous les ordres).

**Request Body:**
```json
{
  "enabled": true,
  "reason": "Manual stop"
}
```

**Response:**
```json
{
  "status": "ok",
  "kill_switch_enabled": true,
  "reason": "Manual stop",
  "timestamp": "2026-02-20T12:00:00Z"
}
```

---

### GET /api/bot/stats

Statistiques du bot.

**Response:**
```json
{
  "daily_trades": 5,
  "trades_by_symbol": {
    "BTCUSDT": 2,
    "XRPUSDT": 3
  },
  "pnl_usd": 12.50,
  "rejection_count": 1,
  "last_trade_time_ms": 1729123456000
}
```

---

## WebSocket

### WS /ws ou /ws/trades

WebSocket pour les événements temps réel.

**Connection:**
```javascript
const ws = new WebSocket('ws://localhost:8000/ws');
```

**Events reçus:**
```json
// Signal généré
{
  "type": "signal",
  "symbol": "BTCUSDT",
  "signal": {
    "direction": "long",
    "entry": 67000.0,
    "stop_loss": 66500.0,
    "take_profit": 68000.0,
    "confidence": 0.85
  }
}

// Trade ouvert
{
  "type": "trade_opened",
  "trade": {...},
  "timestamp": "2026-02-20T12:00:00Z"
}

// Trade fermé
{
  "type": "trade_closed",
  "trade": {...},
  "pnl": 12.50,
  "timestamp": "2026-02-20T12:30:00Z"
}

// Heartbeat (toutes les 30s)
{
  "type": "heartbeat",
  "connected_clients": 3,
  "timestamp": "2026-02-20T12:00:30Z"
}
```

---

## Error Responses

Tous les endpoints peuvent retourner:

```json
{
  "error": "error_code",
  "message": "Human readable message",
  "detail": "Optional details"
}
```

**Common Error Codes:**

| Code | HTTP | Description |
|------|------|-------------|
| `auto_execute_disabled` | 403 | AUTO_EXECUTE n'est pas activé |
| `no_executor` | 503 | Clés API Bitget non configurées |
| `missing_quantity` | 400 | Quantité manquante ou invalide |
| `kill_switch_active` | 403 | Kill-switch activé |
| `position_check_failed` | 500 | Erreur lors de la vérification des positions |
