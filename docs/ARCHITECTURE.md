# Architecture du Bitget SMC Auto-Trader

## Vue d'ensemble

```
┌──────────────────────────────────────────────────────────────────┐
│                        FRONTEND (React/Vite)                     │
│                          localhost:3000                          │
│  - Dashboard, ChartVisualizer, Multi-Broker Monitor              │
│  - WebSocket for real-time signals                               │
│  - Manual trade execution UI                                     │
└──────────────────────────────────────────────────────────────────┘
                               │ REST / WebSocket
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                     SMC ENGINE API (FastAPI)                     │
│                          localhost:8000                          │
│                                                                  │
│  Multi-Broker:      /api/multi/status, /api/multi/start          │
│  Market:            /api/v1/market/ticker, /api/smc/market/*     │
│  Positions:         /api/positions (Historical)                  │
│  Live Trades:       /api/live/trades (Real-time active)          │
│  Bot Control:       /api/bot/kill-switch, /api/bot/stats         │
│  WebSocket:         /ws, /ws/trades                              │
└──────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│               MultiBrokerEngine (smc_futures/engine/)            │
│  - Background loop: run_strategy_loop()                          │
│  - Feeds data to SignalBuilder & Strategy                        │
│  - Triggers execution on configured brokers (Bitget, Kraken)     │
└──────────────────────────────┬───────────────────────────────────┘
                               │
           ┌───────────────────┼───────────────────┐
           ▼                   ▼                   ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐
│  SignalBuilder  │  │   MTFBiasEngine │  │ Execution Executors │
│  - Build signal │  │  - HTF/MTF/LTF  │  │ - Bitget Executor   │
│  - Apply RR     │  │  - Bias calc    │  │ - Kraken Executor   │
│  - Validate     │  │  - Counter-trend│  │ - Balances limits   │
└─────────────────┘  └─────────────────┘  └─────────────────────┘
```

## Modules principaux

### 1. smc_futures/engine/multi_broker_engine.py
Le cœur du système. Remplace l'ancien `orchestrator.py`. Coordonne les flux pour tous les symboles et brokers configurés simultanément:

- Boucle asynchrone permanente (`run_strategy_loop`).
- Si `MULTI_BROKER_MIRROR=1`, tout ordre généré par le `SignalBuilder` (généralement alimenté par Bitget) sera poussé *sur tous les brokers connectés*.
- Coordonne les 3 tiers de l'analyse MTF:

| Tier | Timeframe | Rôle | Données |
|------|-----------|------|---------|
| HTF | 4H | Context/Bias | Direction du marché |
| MTF | 15m | Setup/Zones | Order Blocks, FVG |
| LTF | 5m | Execution | Trigger CHoCH/BOS |

### 2. smc_futures/engine/strategy/

#### signal_builder.py
Construit les signaux de trading:
- Valide le bias HTF
- Vérifie les conditions de sweep
- Applique les fallbacks (BOS+FVG, ATR zone)
- Calcule le R:R avec tolérance epsilon

#### mtf_bias.py
Moteur de biais multi-timeframe:
- Gate HTF: MIN_HTF_CANDLES (300 par défaut)
- Classification: BULLISH/BEARISH/NEUTRAL
- Gestion counter-trend

#### liquidity.py
Détection des sweeps de liquidité:
- Session high/low tracking
- Wick ATR threshold
- Cluster tolerance

### 3. smc_futures/engine/execution/

#### bitget_executor.py
Exécution des ordres Bitget V2:
- Signature HMAC-SHA256 correcte (prehash = ts+method+path+?query+body)
- Leverage clamping (max 10x)
- TPSL synchronisé.

#### kraken_executor.py
Exécution des ordres Kraken Futures:
- Multi-broker compatible
- Vérification pré-trade du solde USD estimé.

#### order_manager_v2.py
Guard-rails et exécution (version multi-broker):
- Interdit le double trading.
- Daily trade limit, Max daily loss
- RR validation avec epsilon.

#### clients (bitget_v2_client, kraken_client)
Clients market data:
- `get_candles` (avec pagination)
- Gestion des WebSocket data stream

### Séparation de la Base de Données (Database Schema)

L'architecture v4 opère une **stricte séparation** entre :
1. **`LiveTrade`** (`backend/models/live_trade.py`) : Enregistre le cycle de vie brut des brokers, ordre ID par ordre ID. Utile pour reconstruire / relancer un thread arrêté.
2. **`PersistedPosition`** (`backend/models/persisted_position.py`) : Regroupe les exécutions sous-jacentes pour l'affichage *métier* dans le dashboard de "Performance Analytics". 

*Note: Une boucle `auto_archive_failed_loop` purge visuellement du tableau de bord tout ordre REST ou Kraken tombé en erreur (ex: Balance insufisante) pour garantir l'intégrité des Analytics de performance (qui ne doivent comptabiliser que les PnL clotûrés).*

### 4. smc_futures/smc_config.py
Configuration centralisée depuis .env:
- Mode de trading (live/paper)
- Auto-execute (true/shadow/false)
- Risk parameters
- Fallback modes

### 5. smc_futures/app_context.py
Container d'injection de dépendances:
- Config partagée
- Metrics
- OrderManager
- WebSocket manager

## Flux de données

```
1. WS/HTTP → API
       │
       ▼
2. bitget_client.get_candles() / kraken_client.get_candles()
       │
       ▼
3. MultiBrokerEngine.compute_signals()
       │
       ▼
4. SignalBuilder.build_signal() (utilise HTF Bias / MTF Setup / LTF Trigger)
       │
       ├── [NO_SIGNAL] → Wait next candle
       │
       ├── [REJECTED]  → Log reason, wait
       │
       └── [CREATED]   → Signal found
              │
              ▼
5. OrderManagerV2.pre_flight_checks()
       │
       ├── [FAIL] → Log reason, skip
       │
       └── [PASS] → Execute
              │
              ▼
6. MirrorTrading (execute_order_all_brokers)
       │
       ├── BitgetExecutor.place_order_market()
       └── KrakenExecutor.place_order_market()
```

## Logs standardisés

| Tag | Description |
|-----|-------------|
| `[SIGNAL_ACCEPTED]` | Signal généré et prêt |
| `[SIGNAL_REJECTED]` | Signal refusé (avec reason) |
| `[FILTER_REJECTED]` | Filtré par session/cooldown |
| `[EXEC_DECISION]` | Décision d'exécution |
| `[ORDER_SENT]` | Ordre envoyé à Bitget |
| `[ORDER_REJECTED]` | Ordre rejeté (guard-rail) |
| `[TPSL_SENT]` | TP/SL attaché |
| `[HTTP_OK]` | Appel API réussi |
| `[HTTP_FAIL]` | Appel API échoué |

## Configuration critique

### Mode Live + Auto-Execute (Mirror Multi-Broker)
```env
TRADING_MODE=live
AUTO_EXECUTE_MODE=true
MULTI_BROKER_MIRROR=1
KRAKEN_ENABLED=1
CONFIRM_LIVE_MULTI_BROKER=true
```

### Mode Paper (Safe)
```env
TRADING_MODE=paper
AUTO_EXECUTE=false
```

### Mode Shadow (Dry-run)
```env
TRADING_MODE=live
AUTO_EXECUTE=shadow
```

## Sécurités intégrées

1. **Kill Switch** - Bloque immédiatement toute exécution
2. **Max Daily Loss** - Auto kill-switch si perte > X%
3. **Max Trades/Day** - Limite le nombre d'ordres
4. **Cooldown** - Temps minimum entre trades
5. **Leverage Clamp** - Max 10x forcé
6. **TPSL Rounding** - Évite erreur checkBDScale
7. **Signature Correcte** - Évite erreur 40009
8. **Position Check** - Évite doublons
