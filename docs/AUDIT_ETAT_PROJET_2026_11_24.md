# Audit Technique & État du Projet — SmartTrade
*Mis à jour le 24 novembre 2026*

## 1. Architecture Active (Autorité Unique SMC V3)
- **Stratégie unique** : Moteur SMC V3 (`smc_futures/orchestrator.py`) — l'ancienne boucle placeholder SMA20/50 de `MultiBrokerEngine` est verrouillée (`MULTI_BROKER_STRATEGY_CALLBACK_REQUIRED`).
- **Exécuteur principal crypto** : **Kraken Futures LIVE** (`PF_XBTUSD`, `PF_ETHUSD`, `PF_SOLUSD`) via `KrakenFuturesExecutorWrapper` (Phase 1.9.10-R1).
- **Exécuteur multi-actifs** : **MetaTrader 5** (`backend/execution/mt5_executor.py` + `backend/execution/mt5_forex_smc_loop.py`) — miroir crypto SMC V3 + boucle Forex/Métaux `4h/15m/5m` (`EURUSD`, `GBPUSD`, `USDJPY`, `XAUUSD`).
- **Frontend & Mobilité** : React + Vite (`pages/Dashboard.tsx`) avec proxy relatif `/api` et `/ws`, compatible accès distant sécurisé via **Tailscale**.

## 2. Contrôles de Sécurité Validés
- **Kraken Futures (Phase 1.9.10-R1)** :
  - Carnet d'ordres WS v1 synchronisé (`KrakenFuturesBookFeed`) avec détachement fail-closed et reconnexion automatique avec backoff (`5s → 15s → 30s → 60s`).
  - Fermeture d'urgence vérifiée (`reduce_only=True`) + quarantaine + sync SQLite si VWAP réel non confirmé.
  - Déduplication stricte des fills par `fill_id`.
  - Placement et vérification du SL de remplacement *avant* annulation de l'ancien SL.
  - Plafond de risque indépendant obligatoire (`KRAKEN_MAX_TRADE_RISK_USD`), prise en compte des frais taker et du slippage.
  - Réconciliation automatique `ReconciliationV2` des positions fermées par SL/TP sur l'exchange.
- **MetaTrader 5** :
  - Verrou `MT5_ALLOW_LIVE` (bloque les comptes réels tant qu'il n'est pas explicitement activé).
  - Plafond de lot (`MT5_MAX_LOT_SIZE=0.01`), anti-stacking (`MT5_MAX_OPEN_PER_SYMBOL=1`), respect automatique de `trade_stops_level` (`retcode=10016`).
  - Garde-fou horaire Forex/Métaux : aucune ouverture hors session + clôture automatique avant rollover quotidien (`20h55 UTC`) et avant la fermeture du vendredi (`20h30 UTC`).

## 3. Validation Technique & Live (24 novembre 2026)
- **Tests automatisés Kraken** : `34 / 34 passés` (`pytest --noconftest tests/test_phase1_9_10_r1_fixes.py tests/test_phase1_9_10_post_fill_protections.py`).
- **Frontend TypeScript** : `npx tsc --noEmit` → `0` erreur.
- **Exécution LIVE vérifiée** :
  - Kraken Futures LIVE : ouvertures `PF_XBTUSD`, `PF_ETHUSD`, `PF_SOLUSD` avec `100 %` d'attachement SL/TP vérifié (`verified=True`).
  - MetaTrader 5 REAL (`FusionMarkets-Live`) : aller-retour `EURUSD 0,01 lot` (`retcode=10009`, SL/TP attachés, clôture propre).
- **Base SQLite** : `PRAGMA integrity_check → ok`, sauvegardes atomiques dans `data/backups/`.

## 4. Feuille de Route Industrialisation
1. Dépôt Git privé + pipeline CI (GitHub Actions : `pytest` + `tsc`).
2. Conteneurisation `Dockerfile` / `docker-compose.yml` pour le backend Linux/Kraken.
3. Consolidation d'un track-record vérifiable (6–12 mois, Profit Factor net > 1,3, Drawdown < 10 %).
