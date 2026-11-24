# INSTITUTIONAL VERDICT & GO/NO-GO CHECKLIST

**Date de l'Audit:** 2026-02-28
**Environnement Cible:** Production (Micro-Capital Live)
**Exchanges:** Kraken (Futures/Derivatives) & Bitget (V2 Mix)
**Mode Opératoire:** Strict Mirror (Multi-Exchange)

---

## A) REVUE GLOBALE : END-TO-END LIFECYCLE

Voici la trace du cycle de vie d'un trade au sein de l'architecture durcie (Phase 0.8 -> Phase 2), incluant l'identification des risques et leur contrôle.

1. **Tick (Data Ingestion)**
   - *Action* : WebSockets ou REST reçoivent le prix.
   - *Contrôle* : Vérification WS desync / reconnexions. Le timestamp du tick est lié au signal généré.
2. **Signal (Stateless Intent Generator - SMC Engine)**
   - *Action* : Le SMC Engine calcule une intention pure (stateless) sous forme de JSON (`buy`, `qty`, `timestamp`).
   - *Risque* : Signal construit sur des données corrompues.
   - *Contrôle* : Le `correlation_id` est apposé. Le signal n'effectue 0 appels réseaux directs.
3. **Validation (Global Risk Engine)**
   - *Action* : Interception obligatoire du signal.
   - *Contrôle Latence* : Bloque le signal si `Δ latency > 250ms` (Stale Data).
   - *Contrôle Microstructure* : Bloque si l'Order Book Spread est > `5.0 bps` ou si la taille > `50%` de la liquidité L1.
   - *Arrêt d'Urgence* : Bloque si le `kill_switch_active` est True.
   - *Emission* : Log `[REJECT]` vers Prometheus/JSON en cas d'échec.
4. **OMS Dispatch (`StrictMirrorOrderManager`)**
   - *Action* : Si validé, l'OMS instancie deux coroutines asynchrones, une vers `BitgetAdapter` et une vers `KrakenAdapter`.
   - *Risque* : L'un des deux exchange ne répond pas ou rejette l'ordre.
   - *Contrôle* : Timeout imposé de `5.0s`. Toute requête "hung" (suspendue) est détruite.
5. **Fills & Read-Back Verification**
   - *Action* : L'OMS attend le statut de réussite ou d'échec des deux côtés.
   - *Cas Idéal* : 100% de réussite. Le PNL commence à courir.
   - *Cas Partiel (Rollback Trigger)* : Bitget réussit, Kraken échoue.
       - L'OMS déclenche un IOC/Market inverse sur Bitget.
       - Enclenche le `Kill Switch` global (Bloque la boucle Risk).
       - Vérification `get_open_positions` pour garantir le FLAT après le rollback.
6. **Double-Fill Race Condition Defense**
   - *Risque* : Pendant le rollback de Bitget, Kraken revient en ligne et valide en retard le 1er ordre échoué.
   - *Contrôle* : Le check post-rollback détecte `position > 0` sur Kraken, emet `DOUBLE_FILL_DETECTED` (Alerte PagerDuty C-Level), et exécute un `close_all` de force.
7. **JSON Observability & Metrics**
   - *Action* : À chaque étape, les actions émettent vers `observability/logger.py` (JSON) et incrémentent `prometheus`.
   - *Contrôle* : `RedactingFormatter` assure que même en mode DEBUG les secrets restent `[REDACTED]`.

---

## B) CHECKLIST GO/NO-GO (MICRO-CAPITAL LIVE)

### 1) OpSec
- [ ] O1. **(No-env)** Aucun fichier `.env` n'existe physiquement sur le disque de production (`ls -la .env == 2`).
- [ ] O2. **(Secrets Injection)** `scripts/fetch_secrets.sh` peuple correctement la mémoire via un KMS formel.
- [ ] O3. **(Redaction)** Prouvé: logger une fausse clé donne `[REDACTED]` dans stdout.
- [ ] O4. **(Hardening)** `smc_bot.service` tourne en tant qu'utilisateur non-root (`bitgetbot`) avec `ProtectSystem=full`.

### 2) Data Freshness & WS Desync
- [ ] D1. **(Stale Ticks)** Les logs prouvent que les ticks `> 250ms` sont systématiquement dropped (`STALE_DATA` metrics incrementing).
- [ ] D2. **(Reconciler)** Le module reconciliateur scanne périodiquement DB vs Exchange et incrémente `WS_DESYNC_COUNT` si anomalie.
- [ ] D3. **(Clock Skew)** La synchronisation NTP du VPS/Serveur est confirmée à `< 5ms` de dérive.

### 3) Risk Limits (Global, Cross-Exchange)
- [ ] R1. **(Spread Gate)** Impossible de passer `EXECUTE` si Bid/Ask spread > Seuil config (ex: 5 bps) via RiskEngine.
- [ ] R2. **(Liquidity Gate)** L'ordre maximal est cappé stricto sensu à `< 50%` du sommet du book (L1).
- [ ] R3. **(Daily Loss Limit)** Coupure totale si PNL_DRAWDOWN_GAUGE est déclenché.

### 4) OMS Execution + Rollback + Double-fill
- [ ] E1. **(Mirror Simultaneous)** Les ordres Bitget et Kraken (Derivatives UNIQUEMENT) sont expédiés en parallèle exact.
- [ ] E2. **(Timeout strict)** Configuré à 5.0s max. Une pendaison génère un Cancel immédiat.
- [ ] E3. **(Rollback)** Test d'intégration validé: Rejet unilatéral = Clôture marché de l'autre patte.
- [ ] E4. **(Flat Verification)** Le retour du rollback est vérifié: Read-back = 0 Positions.
- [ ] E5. **(Double-fill Defense)** La race condition est prise en compte (Alerte + Flatten Failsafe existants).

### 5) Observability / Alerts + Runbooks
- [ ] M1. **(JSON Format)** Logs machine-readable actifs par défaut.
- [ ] M2. **(Correlation ID)** Chaque trade porte un ID unique, liant l'Intent au Résultat OMS.
- [ ] M3. **(Prometheus)** `http://localhost:8000/metrics` répond un payload valide listant `trades_total`, `rejects_total`.
- [ ] M4. **(Grafana)** Dashboard initial minimum viable (MVI) affichant les rollbacks.
- [ ] M5. **(Runbook OOB)** `RUNBOOK_OPSEC.md` validé, stocké hors du serveur (notion/vault).

### 6) Disaster Recovery
- [ ] K1. **(OOB Segregation)** `emergency_kill.py` utilise exclusivement des clefs `KILL_XXX` séparées des `EXEC_XXX`.
- [ ] K2. **(IP Whitelist)** Les `KILL_KEYS` sont whitelists sur IP Prod VPS **ET** IP VPN Secours de l'opérateur.
- [ ] K3. **(No Withdrawals)** API Keys sans aucun droit de transfert/retrait. Audit manuel effectué sur le GUIs des brokers.
- [ ] K4. **(Rotation Plan)** La date de fin de vie (T+90) de la clé actuelle est fixée dans l'agenda.
- [ ] K5. **(Fail-Fast Start)** Le bot crachera au `systemctl start` sans logs clairs si des droits API "Exec" manquent, sans exposer les variables.

---

## C) POLITIQUE DE "PAPER -> MICRO -> SCALE" (Trame de 6 semaines)

Le passage en production obéit à une montée en charge stricte.

| Semaine | Phase | Capital (Exchange) | Max Leverage | Objectif Principal | Metrics Validation | Action Si Échec (Rollback/Reject>3) |
| --- | --- | --- | --- | --- | --- | --- |
| **S1-S2** | **Paper Trading** | N/A (Testnet) | 1x | Prouver la pipeline Async OMS sans risque. | 0 Rollback, Stale < 5%, Fills Mirrorisés. | Correction code (Back to dev) |
| **S3** | **Micro-Capital** | **$100 - $200** | 1x | Expositions réelles (liquidity, spread_bps réels). | `partial_exec_total=0`, PnL cohérent. | Kill-Switch manuel, dump logs. |
| **S4** | **Consolidation** | $500 - $1000 | 1x-2x | Vérification des fills (slippage tracking). | % fill > 98%, `rejects_total(liquidity)=0`. | Rester en Micro-Capital. |
| **S5-S6** | **Ramp-up (Scale)**| Accrément de +15% / semaine | Selon strat risk | Scaling institutionnel mesuré. | Sharpe stable, Latency dispatch < 50ms. | Gel des fonds (No scale). |

**Seuils d'arrêt (Auto-Flat) :**
- `rollback_total % trades_total > 5%` sur 48h.
- `STALE_TICK_DROP_RATE > 15%` (problème structurel réseau VPS <-> Broker).
- `pnl_drawdown_pct < -3%` dans la journée (Daily Loss).

---

## D) TOP 10 RISQUES RÉSIDUELS (Post-Hardening)

1. **Désynchronisation Base de Données (DB Desync) / Process Crash:**
   - *Risque:* Si la DB SQLite/PostgreSQL crashe au moment exact du "Fill", le state réel de broker vs le State DB diffère.
   - *Mitigation:* `TradeReconciler` en asynchrone corrige cela en 10 secondes. Acceptation temporaire du délai.
2. **Flash-Crash & Liquidity Vacuum (Slippage Extreme):**
   - *Risque:* Maker Cancellation massif de l'ordre book. L'ordre `Market` slippe violemment.
   - *Mitigation:* Le cap L1 Volume (`max_liquidity_consumption_pct=50%`) protège de l'achat profond dans le carnet.
3. **Kraken Futures Nonce/Rate-Limit Ban:**
   - *Risque:* Kraken est susceptible aux erreurs RateLimit si trop de ping/cancel.
   - *Mitigation:* Utilisation des WebSockets par défaut. Mettre en place un retry_backoff exponentiel `HTTP 429` (À l'ordre du jour si constaté).
4. **Latency Arbitrage (Toxic Order Flow):**
   - *Risque:* Bitget exécute, Kraken freeze 2s puis fail (Partial Fill -> Rollback). Le marché bouge en 2s, le fallback Market reverse génère une perte sèche sur Bitget.
   - *Mitigation:* Accepté comme le "coût de l'hérésie du Multi-Exchange simultané Market". La limite Timeout OMS fixée à 5s borne le risque maximal temporel.
5. **Fuite RAM de l'Event Loop Asyncio:**
   - *Risque:* Un memory leak asyncio finit par `OOMKill` le bot silencieusement.
   - *Mitigation:* Monitoring systemd (`Restart=always`). Cron de redémarrage doux quotidien (Weekends).
6. **Compromission VPS Hôte (Root Access Bypass):**
   - *Risque:* Un malware RCE prend racine sur le VPS.
   - *Mitigation:* Hardening `systemd` + Pas de `.env` + AWS Secrets limitées temporellement complique grandement l'extraction.
7. **Bogue de l'API de base Exchange:**
   - *Risque:* Bitget ment sur le statut "Filled" (Endpoint en cache).
   - *Mitigation:* Intégrer les WebSockets `Trades` user-data event, au lieu du "HTTP Polling" (Prévu Phase future Microstructure).
8. **Dépendance Serveur AWS / Vault KMS:**
   - *Risque:* L'AWS KMS est down au boot. Le bot ne trouve pas ses clefs `EXEC_KEYS` et refuse de démarrer.
   - *Mitigation:* C'est The Feature. On accepte le déni de service comme mécanisme "Safe Default" d'OpSec.
9. **Man-In-The-Middle (SSL Interception):**
   - *Risque:* Les endpoints de l'API sont usurpés.
   - *Mitigation:* Utilisation stricte des CA Standards, et d'un VPS en Datacenter certifié.
10. **Erreur Humaine (Fat Finger Kill Switch):**
    - *Risque:* Execution de `emergency_kill.py` dans le mauvais dossier / mauvais context d'env.
    - *Mitigation:* Validation stricte implémentée (`KILL_KEYS` check obligatoire).

---

## E) DÉCISION FINALE (VERDICT)

### VERDICT: **GO SOUS CONDITIONS** (pour Micro-Capital Uniquement)

L'audit architectural dévoile une **réelle fondation institutionnelle defensive**.
- La ségrégation OpSec empêchant les vols passifs (Leak .env) est exceptionnellement robuste (Fail-fast boot).
- Le Risk Engine intercepte le comportement `stale` mortel pour les strats SMC.
- L'OMS Mirror asynchrone est doté du mécanisme vital d'auto-protection asymétrique (Inverse Rollback).

### Conditions Préalables Critiques avant l'injection $1 au Live :
1. Prouver la réussite de la checklist B à 100% sur le VPS (cibles en particulier : le fetch_secrets, et la configuration du VPN de secours pour le script d'urgence).
2. Complétion de **2 Semaines pleines du "Paper Testing"** sur les brokers testnets/papers respectifs, prouvant un `Rollback_Rate < 2%`. Vous ne mettez de l'argent réel **QUE** lorsque le paper prouve la synchronisation asynchrone parfaite.
3. Paramétrer les alertes PagerDuty/Discord pour notifier lorsque `DOUBLE_FILL_DETECTED` ou `PARTIAL_EXECUTION` apparaît dans les logs structurés Prometheus.

---
**Prochaine Phase Analytique:**
Post-validation GO, la phase la plus utile devient le **Backtesting & Event Replay asynchrone**. Il permet d'injecter du volume de données via le RiskEngine et l'OMS (mockés) pour simuler la latence sans risque financier, stressant l'infrastructure construite aujourd'hui et garantissant le calcul alpha de SMC.
