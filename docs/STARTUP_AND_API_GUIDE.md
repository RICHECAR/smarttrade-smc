# SMC Trading Bot — Startup & API Guide
═══════════════════════════════════════════════════════════════════════════════

## Table des Matières
1. [Lancement de l'Application](#1-lancement-de-lapplication)
2. [Endpoints REST API](#2-endpoints-rest-api)
3. [WebSocket](#3-websocket)
4. [Configuration Sessions Europe/Paris](#4-configuration-sessions-europeparis)
5. [Paramètres AsiaWeekend Strategy](#5-paramètres-asiaweekend-strategy)
6. [Troubleshooting](#6-troubleshooting)

---

## 1. Lancement de l'Application

### ⚠️ COMMANDE UNIQUE DE LANCEMENT

```bash
# Production (recommandé)
python run.py

# Développement avec hot-reload
python run.py --reload

# Port alternatif
python run.py --port 8001
```

### ❌ NE PAS UTILISER

```bash
# DEPRECATED — ces commandes lancent la mauvaise app ou échouent
python -m uvicorn smc_futures.orchestrator:app ...
python -m uvicorn smc_futures.api_smc_engine:app ...
python -m uvicorn backend.smarttrade_api:app ...
```

### Vérification du Démarrage

Au boot, l'API affiche toutes les routes enregistrées:

```
[BOOT_START] SMC Engine API v4.0 starting...
[ROUTES] Registered API routes:
  GET      /health
  GET      /api/health
  GET      /api/status
  ...
  WS       /ws
  WS       /ws/trades
[ROUTES] Total: 25 routes
[BOOT_COMPLETE] SMC Engine API ready
```

---

## 2. Endpoints REST API

### Health & Status

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check simple |
| `/api/health` | GET | Alias pour compatibilité |
| `/api/status` | GET | Status détaillé (mode, safeguards, symboles) |
| `/api/bot/stats` | GET | Statistiques du bot (trades, PnL) |

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/bot/kill-switch` | POST | Enable/disable kill-switch |
| `/api/manual/execute` | POST | Exécution manuelle d'un signal |

### Multi-Broker Endpoints (v4)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/multi/status` | GET | Status complet Dual-Broker |
| `/api/multi/start` | POST | Démarre la boucle Multi-Broker |
| `/api/multi/stop` | POST | Stoppe la boucle Multi-Broker |
| `/api/multi/balances` | GET | Agrège le solde (Bitget + Kraken) |
| `/api/multi/positions`| GET | Positions sur tous les brokers |

### Trades & Positions (Historique)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/live/trades/open` | GET | Trades en cours (Active) |
| `/api/live/trades` | GET | Historique Live (params: include_archived=true/false) |
| `/api/positions` | GET | Persisted Positions pour Analytics |
| `/api/v1/positions` | GET | Anciennes Positions Bitget directes |

### Market Data

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/market/ticker` | GET | Ticker (params: symbol) |
| `/api/smc/market/candles` | GET | Candles (params: symbol, tf, limit) |

### Statistics

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/stats/summary` | GET | Résumé (from, to) |
| `/api/stats/rr-series` | GET | Série R:R |
| `/api/stats/heatmap` | GET | Heatmap horaire |
| `/api/stats/sessions` | GET | Stats par session |
| `/api/stats/presets` | GET | Stats par preset |

---

## 3. WebSocket

### Endpoints

| URL | Description |
|-----|-------------|
| `ws://localhost:8000/ws` | WebSocket principal (alias) |
| `ws://localhost:8000/ws/trades` | WebSocket trades en temps réel |

### Messages Reçus

```json
// Connexion établie
{"type": "connected", "message": "Connected to SMC Trades WebSocket", "connected_clients": 2}

// Trade ouvert
{"type": "trade_opened", "trade": {...}, "timestamp": "2026-02-21T10:30:00Z"}

// Trade fermé
{"type": "trade_closed", "trade": {...}, "timestamp": "2026-02-21T11:45:00Z"}

// Heartbeat (toutes les 30s)
{"type": "heartbeat", "timestamp": "...", "connected_clients": 2}

// Signal généré
{"type": "signal", "symbol": "BTCUSDT", "signal": {...}}

// Données stale
{"type": "DATA_STALE", "symbol": "BTCUSDT", "age_ms": 15000}
```

### Commandes Client

```javascript
// Keep-alive ping
ws.send("ping");  // Réponse: "pong"

// Status check
ws.send("status");  // Réponse: {"type": "status", "connected": true, "clients": N}
```

### Origines Autorisées

Le WebSocket accepte les connexions de:
- `http://localhost:*`
- `http://127.0.0.1:*`
- Toutes origines en mode dev (`*`)

---

## 4. Configuration Sessions Europe/Paris

### Timezone

```env
# Timezone pour détection des sessions
TZ=Europe/Paris
# ou
STRATEGY_TIMEZONE=Europe/Paris
```

### Sessions de Trading (Heures Europe/Paris)

| Session | Heures (Paris) | Stratégie |
|---------|----------------|-----------|
| Asia | 01:00 - 09:00 | AsiaWeekendStrategy (range mean reversion) |
| London | 09:00 - 14:00 | DefaultSMCStrategy (trend following) |
| Overlap | 14:00 - 17:30 | DefaultSMCStrategy (highest volume) |
| NY | 17:30 - 22:00 | DefaultSMCStrategy |
| Off | 22:00 - 01:00 | AsiaWeekendStrategy |
| Weekend | Sat/Sun | AsiaWeekendStrategy |

### Variables de Configuration

```env
# Asia session window (Europe/Paris)
ASIA_START=01:00
ASIA_END=09:00

# Activer stratégie Asia
ENABLE_ASIA_STRATEGY=1

# Activer stratégie Weekend
ENABLE_WEEKEND_STRATEGY=1

# Sessions SMC actives (London/NY pour trend following)
SMC_SESSIONS=london,ny
```

---

## 5. Paramètres AsiaWeekend Strategy

### Configuration de Base

```env
# R:R Target (plus conservateur que SMC standard)
ASIA_WEEKEND_TP_RR_MIN=1.2
ASIA_WEEKEND_TP_RR_MAX=2.0

# Target mid-range au lieu de la limite opposée
ASIA_TARGET_MID_RANGE=1

# Risque réduit pour faible liquidité (75% du normal)
ASIA_RISK_FACTOR=0.75
```

### Détection de Range

```env
# Lookback pour détecter le range
ASIA_RANGE_LOOKBACK=20

# Taille de range en ATR
ASIA_MIN_RANGE_ATR=0.5
ASIA_MAX_RANGE_ATR=3.0

# Seuils par asset (BTC a des ranges plus larges)
ASIA_MAX_RANGE_RATIO_BTC=6.0
ASIA_MAX_RANGE_RATIO_ETH=5.0
ASIA_MAX_RANGE_RATIO_ALT=4.0

# Weekend: multiplier les seuils (30% plus tolérant)
ASIA_WEEKEND_RELAX_RANGE=1.3
```

### Trending Range Mode

Permet de trader des ranges larges si la structure est claire:

```env
ASIA_TRENDING_RANGE_ALLOWED=1
ASIA_TRENDING_RANGE_MIN_RR=1.0
ASIA_TRENDING_RANGE_MAX_RR=1.5
ASIA_TRENDING_RANGE_RISK_MULT=0.75  # 75% du risque normal
```

### No-Sweep Bypass (NOUVEAU)

Permet mean reversion SANS sweep si OB/FVG + BOS/CHoCH confirment:

```env
# Activer le bypass (0=off, 1=on)
ASIA_ALLOW_NO_SWEEP_WITH_CONFIRMATION=0

# Exiger BOS/CHoCH pour le bypass
ASIA_NO_SWEEP_REQUIRE_BOS=1

# Exiger OB ou FVG pour le bypass
ASIA_NO_SWEEP_REQUIRE_ZONE=1

# R:R minimum (plus strict que normal)
ASIA_NO_SWEEP_MIN_RR=1.5

# Risque réduit (50% du normal)
ASIA_NO_SWEEP_RISK_MULT=0.5
```

**Conditions du bypass:**
1. Prix à la limite du range (≤15% du bord)
2. OB ou FVG valide près du prix
3. BOS/CHoCH confirme la direction de reversal
4. R:R ≥ 1.5
5. Risque réduit de 50%

### Soft Mean Reversion (WEEKEND ONLY) 🆕

Mode **très conservateur** pour augmenter la fréquence des trades le week-end,
SANS dégrader le risk management:

```env
# Activer le mode soft (0=off, 1=on) — WEEKEND SEULEMENT
ENABLE_WEEKEND_SOFT_MEAN_REVERSION=0

# R:R cible (conservateur: 1.0-1.3 pour quick profits)
WEEKEND_SOFT_MIN_RR=1.0
WEEKEND_SOFT_MAX_RR=1.3

# Facteur de risque (réduction de 40% par rapport au risque normal)
WEEKEND_SOFT_RISK_FACTOR=0.6

# Limite de trades par symbole (évite surexposition)
WEEKEND_SOFT_MAX_TRADES_PER_SYMBOL=1

# Exiger OB aligné (recommandé = 1)
WEEKEND_SOFT_REQUIRE_OB=1

# Exiger FVG aligné (recommandé = 1)
WEEKEND_SOFT_REQUIRE_FVG=1
```

**Conditions du mode SOFT:**
1. **Weekend obligatoire** (Samedi/Dimanche)
2. Range valide avec trend_strength ≤ 0.7
3. Prix près du range (≤20% du bord)
4. **OB ET FVG** doivent être alignés dans la même direction
5. R:R entre 1.0 et 1.3 (conservateur)
6. Risque réduit de 40% (`WEEKEND_SOFT_RISK_FACTOR=0.6`)
7. Limite de 1 trade/symbole/weekend

**Quand utiliser SOFT mode:**
- Week-ends calmes où le sweep standard ne se produit pas
- Volatilité basse mais range bien défini
- Pour augmenter la fréquence sans augmenter le risque

**Hiérarchie des modes (priorité):**
1. **Sweep** — Si sweep détecté, utiliser sweep (meilleur setup)
2. **No-Sweep Bypass** — Si OB/FVG + BOS/CHoCH confirment
3. **Soft Mean Reversion** — Weekend-only, OB+FVG alignés, aucune structure requise

---

## 6. Troubleshooting

### WebSocket 403 Forbidden

**Cause**: Origine non autorisée ou connexion avant démarrage complet.

**Solution**:
1. Vérifier que le frontend utilise `http://localhost:*`
2. Attendre `[BOOT_COMPLETE]` dans les logs avant de connecter
3. Vérifier les logs: `[WS] ACCEPTING connection | route=/ws | origin=...`

### API 404 Not Found

**Cause**: Mauvaise application lancée ou routers non inclus.

**Solution**:
1. Utiliser UNIQUEMENT `python run.py`
2. Vérifier les routes au boot: `[ROUTES] Registered API routes:`
3. Confirmer que le port est correct (8000 par défaut)

### HTF Stale Warning

**Message**: `[STALE] BTCUSDT | htf_age_sec=35000 > threshold=1350`

**Cause**: Données HTF (4H) non rafraîchies.

**Solution**:
1. Le fix automatique force le refresh HTF si les données sont stale
2. Logs de diagnostic ajoutés:
   ```
   [HTF_REFRESH] BTCUSDT | OK | candles=300 | last_candle_age_sec=7200 | fetch_count=5 | bias=bullish
   ```

### Beaucoup de NO_RANGE_SWEEP

**Cause**: La stratégie Asia exige un sweep de range qui ne se produit pas toujours.

**Solution**:
1. Activer le bypass no-sweep:
   ```env
   ASIA_ALLOW_NO_SWEEP_WITH_CONFIRMATION=1
   ```
2. Le bot utilisera OB/FVG + BOS/CHoCH comme confirmation alternative

---

## Changelog des Fixes

### v4.0 (2026-02-28)

1. **Multi-Broker Mirroring** 🚀:
   - Exécution asynchrone concurrente sur Bitget V2 et Kraken Futures.
   - Nouvelle logique `MultiBrokerEngine` qui unifie les boucles marché.
   - Variables d'environnement `MULTI_BROKER_MIRROR=1` et `KRAKEN_ENABLED=1`.
2. **Safeguard Fail-Fast**:
   - Vérifie la cohérence des soldes USD (≥ 50 USD) au démarrage avant d'autoriser la Live Execution.
3. **Séparation Base de Données (Anti-Pollution Dashboard)**:
   - La table `LiveTrade` enregistre tout le cycle de vie brut par broker.
   - La table `PersistedPosition` n'agrège que les fermetures pour les KPIs du Dashboard.
   - Background Task: Purge automatiquement les ordres **FAILED/SKIPPED** après 5 minutes (Visibles uniquement via le paramètre `?include_archived=true`).

### v3.2 (2026-02-21)

1. **Soft Mean Reversion Mode** 🆕:
   - Mode weekend-only pour trades sans sweep NI structure
   - Exige OB + FVG alignés (double confluence)
   - R:R conservateur (1.0-1.3)
   - Risque réduit de 40% par défaut
   - Limite 1 trade/symbole/weekend
   - Activé via `ENABLE_WEEKEND_SOFT_MEAN_REVERSION=1`

### v3.1 (2026-02-21)

1. **WebSocket**:
   - Logs détaillés de connexion/déconnexion
   - Vérification d'origine explicite
   - Support `status` command

2. **API**:
   - Log de toutes les routes au démarrage
   - Version 3.1 dans metadata

3. **HTF Stale**:
   - Refresh HTF forcé si données stale
   - Métriques: `htf_fetch_count`, `last_htf_candle_time`
   - Logs améliorés avec `next_refresh_in_sec`

4. **AsiaWeekend**:
   - Option `ASIA_ALLOW_NO_SWEEP_WITH_CONFIRMATION`
   - Bypass avec OB/FVG + BOS/CHoCH
   - R:R et risque configurables pour bypass
