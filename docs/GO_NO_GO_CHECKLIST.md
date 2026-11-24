# GO / NO-GO Checklist — Rollout Paper → Micro-Live → Live

## Structure

Trois paliers séquentiels. Chaque palier a :
- une **durée minimale** d'observation
- des **critères GO** avec seuils précis
- des **critères d'arrêt immédiat** (NO-GO hard)
- un **rollback** vers le palier précédent si échec

---

## PALIER 1 — Paper (72 h minimum)

### Configuration requise
```env
TRADING_MODE=paper
AUTO_EXECUTE=false          # ou shadow
BOT_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT
ADAPTIVE_RISK_ENABLED=true
RECONCILER_ENABLED=true
```

### Critères GO (tous doivent être verts)

| # | Métrique | Seuil GO | Source | Calcul | Interprétation si KO | Action si KO |
|---|----------|----------|--------|--------|----------------------|-------------|
| 1 | Rejection rate globale | < 80% | `GET /api/analytics/rejection-dashboard` → `rejection_rate_pct` | `total_rejected / total_signals × 100` | Sur-filtrage — trop de signaux bloqués inutilement | Identifier le filtre top-1 via `filter_breakdown`, relaxer son seuil |
| 2 | Slippage p95 | < 40 bps | `GET /api/analytics/execution-quality-live` → `slippage.p95` | Percentile 95 des `slippage_bps` enregistrés | Market impact trop élevé ou ordre mal typé | Vérifier order type (limit vs market), vérifier spread moyen |
| 3 | Slippage p99 | < 80 bps | Idem → `slippage.p99` | Percentile 99 | Queue de distribution slippage toxique | Ajouter filtre spread ou réduire `SLIPPAGE_KILL_THRESHOLD_BPS` |
| 4 | Fill delay p95 | < 2000 ms | Idem → `fill_delay.p95` | Percentile 95 des fill_delay_ms | Latence réseau ou broker surchargé | Vérifier connectivité, envisager `MAX_SIGNAL_AGE_SEC` plus strict |
| 5 | Orphan count (72h) | 0 | `GET /api/analytics/execution-quality-live` → `reconciler.orphans_detected` | Compteur cumulé | Position fantôme côté exchange non trackée | Investiguer manuellement via exchange UI |
| 6 | Ghost count (72h) | 0 | Idem → `reconciler.ghosts_cleaned` | Compteur cumulé | State interne désynchronisé | Vérifier le cycle de life-cycle des positions |
| 7 | Emergency close failures | 0 | Idem → `emergency_close.failures` | Compteur cumulé sur 72h | Impossible de fermer une position en urgence | Vérifier API key permissions, connectivité |
| 8 | Latency rejects | < 5% du total signals | `rejection_dashboard.filter_breakdown.SIGNAL_TOO_OLD` / `total_signals` | Ratio | Signaux trop lents — pipeline trop lent ou `MAX_SIGNAL_AGE_SEC` trop strict | Si > 10% : augmenter `MAX_SIGNAL_AGE_SEC` de 5→8s |
| 9 | ARE mode distribution | `blocked` < 15% des éval. | `GET /api/analytics/adaptive-risk` → `by_mode.blocked / total_decisions` | Ratio | ARE trop agressif — bloque trop de trades | Vérifier quel facteur domine (reasons), relaxer le seuil correspondant |
| 10 | Shadow profit missed | Pas de filtre > 60% profitable bloqué | `GET /api/analytics/shadow-analytics` → par filtre | Pour chaque filtre : `profitable_blocked / total_blocked` | Un filtre bloque systématiquement des trades gagnants | Relaxer ou désactiver ce filtre spécifique |
| 11 | Governor snapshots valides | 5 sections non-vides | `GET /api/analytics/performance-governor` | Vérifier : rejection_dashboard, execution_quality, strategy_health, shadow_analytics, adaptive_risk | Instrumentation cassée | Debug import errors dans les logs |
| 12 | Tests CI passent | 97/97 PASS | `pytest tests/test_phase5c_safety.py tests/test_phase6_governance.py tests/test_phase7_adaptive_risk.py` | Sortie pytest | Régression dans le code | Fix avant de continuer |

### NO-GO immédiat (arrêt paper)
- Crash ou restart non planifié du processus
- Mémoire RSS > baseline + 100 MB sur 24h (fuite mémoire)
- Exception non catchée répétée dans les logs (> 10/h)

### Durée : **72 h minimum**, idéal 5 jours
### Critère GO → Palier 2 : **12/12 verts**

---

## PALIER 2 — Micro-Live (1 semaine minimum)

### Configuration requise
```env
TRADING_MODE=live
AUTO_EXECUTE=true
BOT_SYMBOLS=BTCUSDT,ETHUSDT    # 2 symboles max
BOT_LEVERAGE=2                   # Levier réduit
BOT_RISK_PCT=0.002              # 0.2% par trade
SMC_MAX_TRADES_DAY=4            # Max 4 trades/jour
SMC_MAX_DAILY_LOSS_PCT=0.5      # Stop serré 0.5%
MULTI_BROKER_MODE=disabled       # 1 broker seulement
RECONCILER_ENABLED=true
ADAPTIVE_RISK_ENABLED=true
```

### Matrice de monitoring

| # | Métrique | Seuil WARNING (⚠️) | Seuil CRITICAL (🔴) | Source | Action opérateur WARNING | Action opérateur CRITICAL |
|---|----------|-------------------|---------------------|--------|--------------------------|---------------------------|
| 1 | Slippage p50 | > 15 bps | > 30 bps | `/api/analytics/execution-quality-live` | Observer, noter tendance | Pause trading, vérifier spread |
| 2 | Slippage p95 | > 40 bps | > 60 bps | Idem | Réduire taille si possible | Pause + investiguer market impact |
| 3 | Fill delay p95 | > 2000 ms | > 5000 ms | Idem | Vérifier réseau | Pause + changer serveur/DC |
| 4 | Orphans (cumulé) | ≥ 1 | ≥ 2 | Idem → reconciler | Investiguer l'orphan manuellement | Pause + flatten manuel + rollback |
| 5 | Ghosts (cumulé) | ≥ 1 | ≥ 2 | Idem | Investiguer le ghost | Rollback → paper |
| 6 | Emergency close | 1 déclenché | 1 failure | Idem | Review le trade concerné | Rollback immédiat |
| 7 | Daily PnL | < -0.3% | < -0.5% | `/api/governance/regime` → daily_pnl | Observer, ne pas intervenir | Trading auto-stoppé (daily loss limit) |
| 8 | Rejection rate | > 70% | > 85% | `/api/analytics/rejection-dashboard` | Identifier filtre dominant | Pause + calibration |
| 9 | ARE blocked rate | > 20% des éval. | > 40% des éval. | `/api/analytics/adaptive-risk` | Vérifier facteur dominant | Pause + relaxer seuil ARE |
| 10 | ARE defensive rate | > 40% des éval. | > 60% des éval. | Idem | Observer calibrage | Relaxer `ADAPTIVE_RISK_DD_*` ou `ATR_*` |
| 11 | Latency rejects | > 3/jour | > 8/jour | rejection_dashboard → SIGNAL_TOO_OLD | Vérifier pipeline | Augmenter `MAX_SIGNAL_AGE_SEC` |
| 12 | Winrate 7j | < 35% | < 25% | `/api/analytics/strategy-health` | Observer, pas d'action immédiate | Pause + review stratégie |
| 13 | Profit factor 7j | < 0.8 | < 0.5 | Idem | Observer | Pause + rollback paper |
| 14 | Trades exécutés / jour | 0 sur 24h actif | 0 sur 48h actif | `/api/analytics/performance-governor` | Vérifier si sur-filtrage | Diagnostiquer pipeline complet |
| 15 | Broker API errors | > 3/jour | > 10/jour | Logs `[API_ERROR]` | Vérifier status broker | Pause + rollback |

### Critères d'arrêt immédiat (rollback → paper)
- Emergency close failure (1 seul suffit)
- Ghost confirmé après 2 cycles
- PnL journalier < -0.5% (daily loss trigger)
- 3+ orphans en 24h
- Crash/restart non planifié
- API broker indisponible > 5 minutes

### Critères de passage Palier 3

| Prérequis | Seuil | Vérification |
|-----------|-------|--------------|
| Durée minimale | 7 jours calendaires | Date |
| Trades exécutés | ≥ 10 round trips | LiveTrade count |
| Winrate | ≥ 40% sur les 10+ trades | strategy_health |
| Profit factor | ≥ 1.0 | strategy_health |
| Emergency closes | 0 failures | execution_quality |
| Orphans + ghosts | ≤ 1 total | reconciler stats |
| Slippage p95 | < 40 bps stable | execution_quality |
| ARE blocked/total | < 15% | adaptive_risk |
| Aucun rollback | 0 incident de rollback | Historique opérateur |
| Daily loss trigger | Jamais atteint | Dashboard PnL |

### Critère GO → Palier 3 : **10/10 verts**

---

## PALIER 3 — Live Progressif

### Sous-palier 3a — Expansion symboles (2 semaines)
```env
BOT_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT   # +1 symbole
BOT_RISK_PCT=0.005                      # 0.5%
BOT_LEVERAGE=3
SMC_MAX_TRADES_DAY=8
SMC_MAX_DAILY_LOSS_PCT=1.5
```

### Sous-palier 3b — Risque nominal (2 semaines après 3a)
```env
BOT_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT   # Full
BOT_RISK_PCT=0.01                                # 1% standard
BOT_LEVERAGE=3
SMC_MAX_TRADES_DAY=12
SMC_MAX_DAILY_LOSS_PCT=3.0
```

### Sous-palier 3c — Multi-broker (optionnel, après 3b validé)
```env
MULTI_BROKER_MODE=fallback
KRAKEN_ENABLED=true
CONFIRM_LIVE_MULTI_BROKER=true
```

### Montée progressive — Prérequis hebdomadaires

| Semaine | Action | Prérequis |
|---------|--------|-----------|
| S1 | 3a lancé | Palier 2 GO validé |
| S2 | Review 3a | WR ≥ 40%, PF ≥ 1.0, 0 incident |
| S3 | 3b lancé | 3a review OK |
| S4 | Review 3b | PnL net positif, 0 incident |
| S5+ | 3c ou maintien | 3b review OK + décision opérateur |

### Limites max avant revue manuelle obligatoire
- `BOT_RISK_PCT` > 1.5% → revue manuelle
- `BOT_LEVERAGE` > 5 → revue manuelle
- `SMC_MAX_TRADES_DAY` > 15 → revue manuelle
- Ajout d'un nouveau symbole non backtesté → revue manuelle
- `MULTI_BROKER_MODE` = mirror ou split → revue manuelle

---

## Rollback — Procédures

### Paper → Pause
```env
AUTO_EXECUTE=false
KILL_SWITCH=true
```
Vérification : `GET /api/governance/health` → `kill_switch: true`

### Micro-live → Paper
```bash
# 1. Kill switch
curl -X POST http://localhost:8000/api/multi/stop
# 2. Config
export TRADING_MODE=paper
export AUTO_EXECUTE=false
# 3. Restart
docker-compose restart smc-engine
# 4. Flatten positions manuellement si nécessaire
curl -X POST http://localhost:8000/api/live/admin/force-reconcile
```

### Live → Micro-live
```bash
# 1. Réduire d'abord
export BOT_RISK_PCT=0.002
export BOT_SYMBOLS=BTCUSDT,ETHUSDT
export SMC_MAX_TRADES_DAY=4
# 2. Restart
docker-compose restart smc-engine
# 3. Observer 24h avec seuils micro-live
```

### Live → Pause totale (urgence)
```bash
# 1. Kill switch IMMÉDIAT
export KILL_SWITCH=true
# 2. Flatten tout
curl -X POST http://localhost:8000/api/live/admin/force-close-legacy
# 3. Snapshot incident
curl http://localhost:8000/api/analytics/performance-governor > incident_$(date +%s).json
# 4. Config paper forcé
export TRADING_MODE=paper
export AUTO_EXECUTE=false
docker-compose restart smc-engine
```

---

## Vérifications post-rollback

Après tout rollback :
1. `GET /api/governance/health` → `all_passed: true`
2. `GET /api/positions/open/all` → aucune position ouverte
3. Logs : aucune erreur continue
4. Governor snapshot valide (5 sections)
5. Prometheus scrape OK
6. Consigner : date, raison, metrics au moment du rollback
