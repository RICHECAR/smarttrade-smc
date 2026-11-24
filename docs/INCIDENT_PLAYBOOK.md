# Incident Playbook — Procédures Opérationnelles

## Conventions

- **Continue** : le problème est mineur, continuer à surveiller
- **Pause** : arrêter les nouvelles entrées, laisser les positions ouvertes se gérer
- **Rollback** : couper tout, flatten si nécessaire, revenir au palier précédent

---

## INC-01 — Rejection Explosion

### Symptômes
- `p6_rejection_total` rate > 50 rejets / 5 min
- `rejection_rate_pct` passe de ~40% à > 85% en < 1h
- Alerte Prometheus `RejectionExplosion`
- 0 trade exécuté alors que des signaux arrivent

### Diagnostic rapide
```bash
# 1. Quel filtre domine ?
curl -s http://localhost:8000/api/analytics/rejection-dashboard | python -m json.tool
# Regarder : filter_breakdown → le filtre avec le plus de rejets

# 2. Est-ce un filtre hard ou soft ?
# hard_rejects vs soft_rejects ratio

# 3. Les signaux arrivent-ils ?
# total_signals devrait augmenter
```

### Actions
| Condition | Décision |
|-----------|----------|
| Un seul filtre domine (> 70%) | **Continue** — vérifier si le marché justifie le filtrage (vol extrême, news) |
| Filtre `SIGNAL_TOO_OLD` domine | **Continue** — vérifier latence réseau, augmenter `MAX_SIGNAL_AGE_SEC` si nécessaire |
| Filtre `EXTREME_VOLATILITY` domine | **Continue** — le filtre fait son travail si vol réellement extrême |
| Tous les filtres rejettent | **Pause** — problème systémique (data feed cassé, market structure brisée) |
| Rejection rate > 95% pendant > 1h | **Pause** — investiguer complétement avant reprise |

### Post-mortem
- Noter : date, durée, filtre dominant, contexte marché
- Si faux positif confirmé : planifier calibration du filtre concerné

---

## INC-02 — Orphan Detected

### Symptômes
- `p6_reconciler_orphans_total` augmente
- Alerte Prometheus `ReconcilerOrphanDetected`
- Position visible sur exchange mais pas dans le state interne

### Diagnostic rapide
```bash
# 1. Vérifier les positions exchange
curl -s http://localhost:8000/api/positions/open/all | python -m json.tool

# 2. Vérifier le state interne
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool
# → reconciler.orphans_detected

# 3. Vérifier sur l'exchange directement (UI Bitget)
# Comparer les positions ouvertes avec le state interne
```

### Actions
| Condition | Décision |
|-----------|----------|
| 1 orphan, size petite | **Continue** — surveiller, le reconciler track l'âge |
| 1 orphan, PnL négatif croissant | **Pause** — flatten manuellement via exchange UI |
| `auto_flatten` activé et aucun failure | **Continue** — le système gère |
| orphan persiste > 30 min | **Pause** — flatten manuellement |
| 2+ orphans en 24h | **Rollback** → paper, investiguer synchro state |

### Flatten manuel
```bash
# Via API
curl -X POST http://localhost:8000/api/live/position/close/BTCUSDT

# Vérifier
curl -s http://localhost:8000/api/live/position/close-status/BTCUSDT
```

### Post-mortem
- Identifier pourquoi le state a divergé (crash pendant exécution ? fill partiel non reporté ?)
- Vérifier les logs `[ORDER_MANAGER]` et `[RECONCILER]` autour du timestamp

---

## INC-03 — Ghost Detected

### Symptômes
- `p6_reconciler_ghosts_total` augmente (après 2 cycles de confirmation)
- State interne dit "position ouverte" mais exchange a rien
- Alerte Prometheus `ReconcilerGhostDetected`

### Diagnostic rapide
```bash
# 1. Vérifier si le ghost a été nettoyé
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool
# → reconciler.ghosts_cleaned

# 2. Vérifier les positions internes
curl -s http://localhost:8000/api/live/trades/open | python -m json.tool
```

### Actions
| Condition | Décision |
|-----------|----------|
| Ghost nettoyé automatiquement | **Continue** — le reconciler a fait son travail |
| Ghost non nettoyé après 2 cycles | **Pause** — forcer le nettoyage |
| 2+ ghosts en 24h | **Rollback** → paper — problème de synchro systémique |

### Nettoyage forcé
```bash
curl -X POST http://localhost:8000/api/live/admin/force-reconcile
```

### Post-mortem
- Un ghost = un trade fermé côté exchange sans que le bot l'ait fermé (SL exchange-side ?)
- Vérifier si le SL/TP a été touché côté exchange pendant un downtime du bot

---

## INC-04 — Emergency Close Failure

### Symptômes
- `p6_emergency_close_total{outcome="failure"}` > 0
- Alerte Prometheus `EmergencyCloseFailure`
- Position toujours ouverte après un slippage kill ou daily loss trigger

### Diagnostic rapide
```bash
# 1. Vérifier le statut
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool
# → emergency_close.failures

# 2. Vérifier les positions toujours ouvertes
curl -s http://localhost:8000/api/live/trades/open | python -m json.tool

# 3. Vérifier la connectivité broker
curl -s http://localhost:8000/api/multi/health | python -m json.tool
```

### Actions
| Condition | Décision |
|-----------|----------|
| API broker en erreur | **Rollback immédiat** — flatten manuellement via exchange UI |
| Position small, PnL proche de 0 | **Pause** — retenter le close manuellement |
| Position large ou PnL en chute | **Rollback immédiat** — flatten via exchange UI MAINTENANT |

### Close manuel d'urgence
```bash
# Via API (retry)
curl -X POST http://localhost:8000/api/live/position/close/BTCUSDT

# Si API ne fonctionne pas → Exchange UI directement
# Bitget > Futures > Positions > Close All
```

### Post-mortem
- **TOUJOURS consigner** un emergency close failure
- Vérifier : permissions API, rate limits, maintenance exchange, network
- Ne pas reprendre le trading sans avoir compris la cause

---

## INC-05 — High Slippage

### Symptômes
- `p6_slippage_bps` p95 > 40 bps (warning)
- `p6_slippage_bps` p99 > 80 bps (critical)
- Alertes Prometheus `HighSlippageWarning` / `HighSlippageCritical`

### Diagnostic rapide
```bash
# 1. Distribution slippage actuelle
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool
# → slippage.p50, .p95, .p99

# 2. Vérifier si un symbole spécifique
# Regarder les logs [SLIPPAGE] pour identifier le symbole
```

### Actions
| Condition | Décision |
|-----------|----------|
| p95 entre 40-60 bps | **Continue** — surveiller, probablement vol temporaire |
| p95 > 60 bps constant | **Pause** — vérifier spread moyen, réduire taille |
| p99 > 80 bps | **Pause** — envisager limit orders (`SMC_ORDER_TYPE=limit`) |
| Kill switch triggered | Géré automatiquement (cooldown 5min) |
| Slippage élevé sur 1 symbole uniquement | Retirer ce symbole de `BOT_SYMBOLS` |

### Post-mortem
- Comparer slippage par symbole et par session
- Vérifier si corrélé avec volume de marché faible (session Asie ?)
- Envisager : réduire size, passer en limit, augmenter cooldown

---

## INC-06 — Extreme Fill Delay

### Symptômes
- `p6_fill_delay_ms` p95 > 2000 ms (warning)
- `p6_fill_delay_ms` p95 > 5000 ms (critical)
- Alertes Prometheus `HighFillDelay` / `ExtremeFillDelay`

### Diagnostic rapide
```bash
# 1. Latence actuelle
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool
# → fill_delay.p50, .p95

# 2. Status broker
curl -s http://localhost:8000/api/multi/health | python -m json.tool

# 3. Test réseau
ping api.bitget.com
```

### Actions
| Condition | Décision |
|-----------|----------|
| p95 entre 2-5s, transitoire | **Continue** — congestion temporaire |
| p95 > 5s constant | **Pause** — problème réseau ou broker |
| p95 > 10s | **Rollback** — trading avec cette latence est dangereux |
| Corrélé avec heure spécifique | Ajuster `SMC_SESSIONS` pour éviter cette plage |

### Post-mortem
- Identifier si le problème est réseau (ping), broker (API status), ou local (CPU)
- Envisager `MAX_SIGNAL_AGE_SEC` plus strict si latence structurelle

---

## INC-07 — ARE Blocked Storm

### Symptômes
- `p7_adaptive_risk_block_total` augmente rapidement
- `adaptive_risk.by_mode.blocked` > 40% des décisions
- Très peu ou aucun trade exécuté malgré des signaux

### Diagnostic rapide
```bash
# 1. Distribution des modes ARE
curl -s http://localhost:8000/api/analytics/adaptive-risk | python -m json.tool
# → by_mode, block_reasons

# 2. Quel facteur bloque ?
# block_reasons : vol_extreme, dd_critical, exec_kills
```

### Actions
| Condition | Décision |
|-----------|----------|
| `vol_extreme` domine | **Continue** — l'ARE protège correctement en vol extrême |
| `dd_critical` domine | **Continue** — drawdown réel justifie le blocage |
| `exec_kills` domine + are des vrais kills | **Continue** — qualité d'exécution dégradée, ARE a raison |
| `exec_kills` domine + 0 vrai kill (faux positif) | **Pause** — augmenter `ADAPTIVE_RISK_MAX_RECENT_KILLS` |
| Blocked > 50% pendant > 24h sans raison marché | **Pause** — calibration ARE nécessaire |

### Post-mortem
- Identifier le facteur dominant
- Comparer avec les conditions de marché réelles
- Planifier calibration si faux positifs confirmés (cf. CALIBRATION_PLAN.md)

---

## INC-08 — Latency Reject Storm

### Symptômes
- `SIGNAL_TOO_OLD` rejects > 10/heure
- Aucun trade exécuté malgré des signaux générés
- Pipeline strategy → order_manager prend trop de temps

### Diagnostic rapide
```bash
# 1. Taux de rejection signal_age
curl -s http://localhost:8000/api/analytics/rejection-dashboard | python -m json.tool
# → filter_breakdown.SIGNAL_TOO_OLD

# 2. Vérifier la charge CPU
# Haut CPU = le pipeline est lent
# Win: Get-Process python | Select-Object CPU, WorkingSet

# 3. Vérifier si data feed est lent
# Logs [MARKET_DATA] pour les timestamps
```

### Actions
| Condition | Décision |
|-----------|----------|
| CPU < 50%, network OK | Augmenter `MAX_SIGNAL_AGE_SEC` +2s, **Continue** |
| CPU > 80% | **Pause** — kill processus non essentiels |
| Data feed en retard (timestamps stale) | **Pause** — restart data provider |
| Constant (> 6h) | **Rollback** si micro-live, diagnostiquer infrastructure |

---

## INC-09 — Broker API Unstable

### Symptômes
- HTTP 429 (rate limit), 500, 503 dans les logs
- `api.bitget.com` timeout ou erreurs réseau
- Fill delay extrême ou trades non confirmés

### Diagnostic rapide
```bash
# 1. Health check broker
curl -s http://localhost:8000/api/multi/health | python -m json.tool

# 2. Connectivité directe
curl -s https://api.bitget.com/api/v2/mix/account/accounts 2>&1 | head -5

# 3. Vérifier rate limits
# Logs [API_ERROR] pour les codes HTTP, pattern 429
```

### Actions
| Condition | Décision |
|-----------|----------|
| 429 sporadique (1-2/h) | **Continue** — rate limit normal, backoff auto |
| 429 fréquent (> 5/h) | **Pause** — réduire polling frequency |
| 500/503 broker | **Pause** — maintenance exchange probable |
| Timeout > 10s | **Pause** — vérifier si exchange est en maintenance |
| API down > 5 min | **Rollback immédiat** — flatten tout manuellement via exchange UI |

### Rollback spécifique
```bash
# 1. Kill switch
export KILL_SWITCH=true
# 2. Flatten via exchange UI (pas via API si API instable)
# 3. Attendre que le broker soit stable (vérifier status page)
# 4. Reprendre en paper pour valider la connectivité
```

---

## INC-10 — Divergence Exchange vs State Interne

### Symptômes
- Le bot dit "pas de position" mais l'exchange en a
- Ou inversement : le bot dit "position ouverte" mais l'exchange n'en a pas
- Reconciler détecte orphans ET ghosts simultanément

### Diagnostic rapide
```bash
# 1. State interne
curl -s http://localhost:8000/api/live/trades/open | python -m json.tool

# 2. Positions exchange
curl -s http://localhost:8000/api/positions/open/all | python -m json.tool

# 3. Reconciler status
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool
# → reconciler section

# 4. Comparer manuellement
# Exchange UI vs réponse API vs state interne
```

### Actions
| Condition | Décision |
|-----------|----------|
| 1 divergence mineure, corrigée par reconciler | **Continue** |
| Divergence persistante après reconciler | **Rollback immédiat** |
| Divergence + positions en perte | **Rollback immédiat** + flatten manuel |
| Multiple divergences | **Rollback immédiat** — problème systémique |

### Procédure de resync complète
```bash
# 1. KILL
export KILL_SWITCH=true
export AUTO_EXECUTE=false

# 2. Flatten tout côté exchange (via UI)

# 3. Force reconcile
curl -X POST http://localhost:8000/api/live/admin/force-reconcile

# 4. Vérifier DB interne
curl -s http://localhost:8000/api/live/admin/db-status | python -m json.tool

# 5. Ne reprendre que quand positions exchange == state interne == 0
```

### Post-mortem
- **TOUJOURS** documenter une divergence
- Identifier la cause : crash, fill partiel, timeout, SL exchange-side
- Vérifier les logs dans l'intervalle du problème

---

## Template Post-Mortem

```markdown
# Post-Mortem — [INC-XX] [Titre court]

**Date** : YYYY-MM-DD HH:MM UTC
**Durée** : X minutes
**Impact** : [aucun trade perdu / N$ de perte / service interrompu]
**Palier** : [paper / micro-live / live]

## Timeline
- HH:MM — Premier symptôme observé
- HH:MM — Diagnostic commencé
- HH:MM — Décision prise : [continue/pause/rollback]
- HH:MM — Résolution
- HH:MM — Retour à la normale

## Cause racine
[Description technique de la cause]

## Actions correctives
- [ ] Action 1
- [ ] Action 2

## Leçon apprise
[Ce qu'on fera différemment la prochaine fois]
```
