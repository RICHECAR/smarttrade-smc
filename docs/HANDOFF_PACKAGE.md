# HANDOFF PACKAGE: SMC Auto Trader (Institutional Grade)

Ce document fournit l'architecture complète, l'état actuel et les flux du bot de trading SMC multi-exchange (Kraken Futures + Bitget Mix V2). Ce package est conçu pour transférer le contexte formel et exploitable à un autre ingénieur ou agent autonome.

## 1. Arborescence Finale & Rôles des Modules

```text
bitget-smc-auto-trader/
├── smc_futures/
│   ├── engine/
│   │   ├── risk/
│   │   │   └── global_risk_engine.py      # Validateur/Rejecteur hard (stale, spread, liquidity)
│   │   ├── execution/
│   │   │   ├── base_adapter.py            # Interface Async commune
│   │   │   ├── bitget_adapter.py          # Adaptateur Bitget V2 Mix
│   │   │   ├── kraken_adapter.py          # Adaptateur Kraken Derivatives (API v3)
│   │   │   └── strict_oms.py              # OMS Async (Strict Mirror + Rollback/Read-Back Flat)
│   │   └── replay/
│   │       ├── event_replayer.py          # Moteur de stress-test asynchrone (Phase 3)
│   │       ├── fault_mocks.py             # Mocks injectant latence réseau, rejets et timeouts (30s)
│   │       └── metrics_exporter.py        # Export CSV des performances du stress test
│   ├── security/
│   │   ├── secrets.py                     # Gestion stricte (fail-fast exit) des variables d'environnement
│   │   └── redaction.py                   # Nettoyage automatique des chaînes secrètes dans l'output
│   ├── observability/
│   │   ├── logger.py                      # JSON Logger + Injection Corrélation ID (Intent) + Redaction
│   │   ├── metrics.py                     # Exporter Prometheus (latency_ms, trades_total, reject_total) sur port 8000
│   │   └── alerts.py                      # Alerting critique pseudo-PagerDuty
│   └── tests/
│       ├── test_phase2_microstructure.py  # Tests unitaires Microstructure & Double-fill race conditions
│       └── stress_tests.py                # Profils de charge CSV (Baseline, High Latency, Toxic Fault)
├── backend/
│   └── services/
│       └── trade_reconciler_strict.py     # Boucle d'arrière-plan de réconciliation DB vs Exchanges réels
├── scripts/
│   ├── emergency_kill.py                  # Kill-switch OOB indépendant n'employant que des `KILL_KEYS`
│   └── fetch_secrets.sh                   # Script d'injection runtime en mémoire via KMS/Vault (Anti-.env)
├── observability/
│   ├── docker-compose.observability.yml   # Stack Prometheus (9090) & Grafana (3000) locale
│   └── prometheus.yml                     # Configuration de scraping (cible: 127.0.0.1:8000)
├── systemd/
│   └── smc_bot.service                    # Démon systemd durci (ProtectSystem=full, PrivateTmp, seg. bitgetbot)
└── docs/                                  # Livrables institutionnels (RUNBOOK, OPSEC, VERDICTS)
```

## 2. Diagramme des Flux (End-to-End Lifecycle)

```text
[Data/Tick] ---> [SMC Engine] (Stateless Intent)
                       |
                       v
[GlobalRiskEngine] --> (Stale > 250ms?) ----> (NO) -> REJECT via Prometheus
   (Pre-flight)    --> (Spread > 5bps?) ----> (NO) -> REJECT via Prometheus
                   --> (L1 Liquidity < target?) -> (NO) -> REJECT
                       |
                       v (YES / Risk OK)
[StrictMirrorOMS] ---> (Concurrent Async Dispatch Timeout 5.0s)
                       |
           +-----------+-----------+
           |                       |
           v                       v
    [BitgetAdapter]        [KrakenAdapter]
           |                       |
           +-----------+-----------+
                       |
               (Wait for All Fills)
                       |
          +------------+------------+
          |                         |
(100% Success on Both)       (Partial Fill / Timeout / Reject on 1 leg)
    -> [COMPLETED]                  |
                                    v
                            [ROLLBACK TRIGGERED]
                                    |---> Alerte CRITICAL PagerDuty
                                    |---> Inverse Market Order sur la Jambe Success
                                    |---> Kill Switch Activé (Risk_locked=True)
                                    v
                           [READ-BACK VERIFICATION]
                                    |---> get_open_positions() == 0 ?
                (YES) -> [FLAT]     |---> (NO) -> DOUBLE-FILL RACE DETECTED!
                                                  (Force Full Flat + Alert Extreme)
```

## 3. Politiques Non Négociables Mises en Œuvre
- **Kraken Derivatives Seulement** : Strictement des appels à `futures.kraken.com/derivatives/api/v3`. Interdiction d'API Spot.
- **OpSec Fail-Fast (PROD)** : Si `TRADING_MODE=live`, le système `secrets.py` s'arrête net (Fail-closed) s'il détecte un fichier physique `.env`.
- **Secret Segregation** : Les clés `EXEC_` (Bot) sont ségréguées des `KILL_` (`emergency_kill.py`). Aucun droit de "Withdrawal" ou "Transfer" toléré.
- **Stale Hard-Drops** : `stale_threshold_ms` est fixé à 250ms. Le `GlobalRiskEngine` coupe l'intention si elle a trop voyagé.
- **Strict Mirror Mode** : Aucune exécution "unilatérale" permise. 1 intention = 2 ordres. Le moindre échec déclenche un Rollback de protection totale. L'OMS ne produit jamais de "silent fail".
- **Observabilité Instrumentée** : Port 8000. L'Observabilité n'est pas "en option", c'est une exigence de surveillance en temps réel pour justifier le fail-closed.

## 4. Configurations & Paramètres Clés

**Mapping des variables d'environnement (`.env.example` pour le DEV / Injection Vault pour PROD)**
```ini
TRADING_MODE=dev # live | dev | paper
AUTO_EXECUTE_MODE=false
KRAKEN_ENABLED=true
CONFIRM_LIVE_MULTI_BROKER=false

# Bot Execution Keys (Droits: Lecture, Trade)
EXEC_BITGET_API_KEY=...
EXEC_BITGET_SECRET_KEY=...
EXEC_BITGET_PASSPHRASE=...
EXEC_KRAKEN_API_KEY=...
EXEC_KRAKEN_API_SECRET=...

# Emergency Kill Keys (Droits: Lecture, Trade-Only/Clôture)
KILL_BITGET_API_KEY=...
KILL_BITGET_SECRET_KEY=...
KILL_BITGET_PASSPHRASE=...
KILL_KRAKEN_API_KEY=...
KILL_KRAKEN_API_SECRET=...
```
**Options Globales Risk Engine** (Configurables)
- `max_spread_bps` = 5.0
- `max_liquidity_consumption_pct` = 0.50
- OMS timeout strict = 5.0s

## 5. Repro Steps ("How to Run")

Pour valider l'architecture en isolation ou préparer la production, suivez cet enchaînement progressif :

1. **Step 1: Tests Unitaires Locaux**
   Validation du socle Microstructure et des mocks de Rollback :
   ```bash
   python -m pytest -v smc_futures/tests/
   ```
2. **Step 2: Observability Stack**
   Déploiement des visualisations (Prometheus sur `localhost:9090` / Grafana sur `localhost:3000`) :
   ```bash
   cd observability && docker-compose -f docker-compose.observability.yml up -d
   ```
3. **Step 3: Event Replay / Scaling Profiler**
   Stress-test formel. Génère des CSV évaluant le "Rollback Rate" face à la latence sans engager de fonds :
   ```bash
   $env:PYTHONPATH="."; python smc_futures/tests/stress_tests.py
   ```
   *Note: Surveiller Prometheus pendant l'exécution du script.*
4. **Step 4: Micro-Live (Production)**
   Démarrage de l'agent en direct via le service durci OS (injecte KMS `fetch_secrets.sh` sans exposer l'env) :
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl start smc_bot
   ```

## 6. Checklist de Déploiement Micro-Capital (GO/NO-GO Condensée)
1. [ ] Aucune API Key en dur, aucun fichier `.env` sur le serveur live.
2. [ ] Isolation OS confirmée dans le `.service` (NoNewPrivileges, PrivateTmp).
3. [ ] Script `emergency_kill.py` exécuté et fonctionnel `exit code 0` (Flat check ok) via les `KILL_KEYS` exclusives.
4. [ ] Synchronisation NTP serveur < 5ms.
5. [ ] Tests Unitaires / Event Replay Metrics vérifiés `Pass`.
6. [ ] Exécution limitée par configuration formelle à $100-$200 de Nominale (Micro-Capital limit).
7. [ ] Dashboards Grafana connectés. Log "pagerduty_mock" correctement catché en urgence.

## 7. Known Limitations & Next Phase (Phase 4)

**Limitations Actuelles (Risque d'Arbitrage Latence) :**
Le `StrictMirrorOMS` est économiquement très cher à opérer sous latence réseau. S'il est 100% "safe" algorithmiquement de Rollback/Failsafe, le "Coût de Rollback" (Double Slippage sur la jambe réussie qui est reversée manuellement Market contre le flux) mange rapidement l'alpha statistique de votre SMC à partir de 2-3% de taux de rejet. Le "Heavy Scale" de capitaux dans ces termes en Market/Market simultané est à proscrire.

**Prochaine Phase Recommandée : Phase 4 (Asymmetric Execution Maker-Taker)**
Pour débloquer la friction économique, l'architecture doit passer :
1. De : `Bitget(MKT) + Kraken(MKT) en parallèle`
2. À : `Primary_Exchange(Maker Post-Only) -> Attente Fill WebSockets -> Secondary_Exchange(Taker Hedge)`.
Cette stratégie élimine l'angoisse structurelle et onéreuse de la désynchronisation et du Rollback défensif, acceptant un "Leg-in Risk" asymétrique parfaitement géré par une *State Machine* interne. Cela permet de conserver des limites strictes Fail-Closed tout en évitant le parasitage Market/Maker fees.
