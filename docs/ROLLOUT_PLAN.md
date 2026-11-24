# Plan de Rollout — Phase 6 Observability & Performance Governance

## Vue d'ensemble

Déploiement progressif en **3 paliers** avec critères de passage objectifs.  
Chaque palier doit être validé **72 h minimum** avant passage au suivant.

---

## Palier 1 — Paper Mode (Shadow)
> **Objectif** : Valider que l'instrumentation fonctionne, sans exposition au marché.

### Configuration
```env
TRADING_MODE=paper
SHADOW_MODE=true
AUTO_EXECUTE_ENABLED=false
```

### Checklist de validation
| # | Critère | Seuil | Comment vérifier |
|---|---------|-------|------------------|
| 1 | Governor produit des snapshots valides | 4 dashboards non-vides | `GET /api/analytics/performance-governor` |
| 2 | Rejections classifiées hard/soft | Ratio cohérent (hard > 50%) | Dashboard rejection `hard_rejects / total` |
| 3 | Shadow blocks enregistrés | ≥ 1 filter tracké | `GET /api/analytics/shadow-analytics` |
| 4 | Prometheus scrape OK | Métriques `p6_*` visibles | Grafana → Explore → `p6_signal_evaluated_total` |
| 5 | Reconciler cycles = 0 anomalies | ghosts=0, orphans=0 | `GET /api/analytics/execution-quality-live` |
| 6 | Mémoire stable 24h | RSS < baseline + 50MB | `docker stats` ou `/proc/PID/status` |
| 7 | Tests Phase 6 passent en CI | 18/18 PASSED | `pytest tests/test_phase6_governance.py` |
| 8 | Taux de rejet raisonnable | rejection_rate < 80% | `GET /api/analytics/rejection-dashboard` → `rejection_rate_pct` |

### Durée : 72 h minimum
### Critère GO → Palier 2 : **8/8 verts**

---

## Palier 2 — Micro-Live
> **Objectif** : Valider en conditions réelles avec exposition minimale.

### Configuration
```env
TRADING_MODE=live
SHADOW_MODE=false
AUTO_EXECUTE_ENABLED=true
RISK_PER_TRADE_PCT=0.002        # 0.2% au lieu de 1%
MAX_TRADES_PER_DAY=3            # Limité
MAX_DAILY_LOSS_PCT=1.0          # Stop serré
DEFAULT_LEVERAGE=2              # Levier réduit
RECONCILER_ENABLED=true
```

### Checklist de validation
| # | Critère | Seuil | Comment vérifier |
|---|---------|-------|------------------|
| 1 | Slippage moyen | < 15 bps p50 | Dashboard execution quality → slippage.p50 |
| 2 | Fill delay | < 2000 ms p95 | Dashboard execution quality → fill_delay.p95 |
| 3 | Emergency close success rate | 100% (ou aucun déclenché) | execution_quality.emergency_close.successes |
| 4 | Reconciler ghosts | 0 sur 72h | reconciler.ghosts_cleaned == 0 |
| 5 | Reconciler orphans | ≤ 1 en 72h | reconciler.orphans_detected ≤ 1 |
| 6 | Daily loss | Jamais > MAX_DAILY_LOSS_PCT | Prometheus `p6_strategy_pnl` |
| 7 | Rejection rate cohérent | Soft < 40% du total | rejection_dashboard.soft_rejects / total |
| 8 | Winrate paper vs micro-live delta | < 15 pts d'écart | Comparer strategy_health.winrate_pct |
| 9 | Aucun crash/restart non planifié | 0 redémarrage | `docker ps --format` uptime |
| 10 | Shadow filter cost insight | ≥ 1 filter avec données | shadow_analytics.filters |

### Durée : 72 h minimum (idéal : 1 semaine)
### Critère GO → Palier 3 : **10/10 verts**

---

## Palier 3 — Live Production 
> **Objectif** : Paramètres normaux, monitoring continu.

### Configuration
```env
TRADING_MODE=live
SHADOW_MODE=false
AUTO_EXECUTE_ENABLED=true
RISK_PER_TRADE_PCT=0.01         # 1% standard
MAX_TRADES_PER_DAY=10
MAX_DAILY_LOSS_PCT=3.0
DEFAULT_LEVERAGE=3
RECONCILER_ENABLED=true
```

### Monitoring continu — Alertes Prometheus/Grafana

| Métrique | Alerte si | Sévérité |
|----------|-----------|----------|
| `p6_slippage_bps` p95 | > 40 bps | ⚠️ Warning |
| `p6_slippage_bps` p99 | > 80 bps | 🔴 Critical |
| `p6_fill_delay_ms` p95 | > 5000 ms | ⚠️ Warning |
| `p6_emergency_close_total{outcome="failure"}` | > 0 | 🔴 Critical |
| `p6_reconciler_orphans_total` rate 1h | > 2 | ⚠️ Warning |
| `p6_reconciler_ghosts_total` rate 1h | > 1 | 🔴 Critical |
| `p6_rejection_total` rate 5m | > 50 rejets/5min | ⚠️ Warning |
| `p6_strategy_pnl` any strategy | < -5% daily | 🔴 Critical |

### Revue hebdomadaire
1. **Rejection audit** : Quels filtres rejettent le plus ? Le soft-filter ratio est-il optimal ?
2. **Shadow cost** : Les filtres bloquent-ils des trades gagnants ? Lesquels ajuster ?
3. **Execution quality** : Le slippage moyen est-il stable ? Tendance sur 7j ?
4. **Profit factor par régime** : Le PF est-il > 1.2 en TREND ? RANGE doit être > 1.0.

---

## Procédure de Rollback

À tout palier, si un critère critique échoue :

```bash
# 1. Kill switch immédiat
curl -X POST http://localhost:8000/api/kill-switch

# 2. Revenir en paper mode
export TRADING_MODE=paper
export AUTO_EXECUTE_ENABLED=false
docker-compose restart smc-engine

# 3. Analyser
# Snapshot complet des 4 dashboards au moment du problème
curl http://localhost:8000/api/analytics/performance-governor > incident_snapshot.json

# 4. Post-mortem
# - Quel filtre/check a échoué ?
# - Le governor a-t-il détecté le problème ?
# - L'alerte Prometheus s'est-elle déclenchée ?
```

---

## Grafana — Dashboards recommandés

### Dashboard 1 : Rejection Overview
- Panel 1 : `p6_rejection_total` rate by filter (stacked bar)
- Panel 2 : Hard vs Soft ratio (pie chart)
- Panel 3 : Top 5 symboles rejetés (table)
- Panel 4 : Rejection rate par session (london/ny/asia) (time series)

### Dashboard 2 : Execution Quality
- Panel 1 : `p6_slippage_bps` histogram heat map
- Panel 2 : `p6_fill_delay_ms` p50/p95/p99 time series
- Panel 3 : Emergency close events (annotation overlay)
- Panel 4 : Reconciler activity (orphans/ghosts/flattened counters)

### Dashboard 3 : Strategy Performance
- Panel 1 : `p6_strategy_trades_total` by strategy×regime (bar)
- Panel 2 : `p6_strategy_pnl` gauge per strategy
- Panel 3 : Winrate trend 7j rolling (computed from governor API)
- Panel 4 : Profit factor par régime (table)

### Dashboard 4 : Shadow Mode Analytics
- Panel 1 : Filter cost ranking (bar chart, from API)
- Panel 2 : Blocked signals: won vs lost vs unknown (stacked)
- Panel 3 : Filter-by-filter opportunity cost over time

---

## Résumé des endpoints API Phase 6

| Endpoint | Contenu |
|----------|---------|
| `GET /api/analytics/performance-governor` | Snapshot complet 4 dashboards |
| `GET /api/analytics/rejection-dashboard` | Rejections: by filter/symbol/session, hard/soft |
| `GET /api/analytics/execution-quality-live` | Slippage, fill delay, emergency close, reconciler |
| `GET /api/analytics/strategy-health` | Trades, winrate, PF, breakdown par strategy×régime |
| `GET /api/analytics/shadow-analytics` | Shadow blocks, filter cost ranking |
