# Operations Runbook — Démarrage, Monitoring, Maintenance

## 1. Démarrage Paper

### Pré-requis
- [ ] `.env` copié depuis `safe-live.env` et adapté
- [ ] API keys configurées et testées
- [ ] `TRADING_MODE=paper`
- [ ] `AUTO_EXECUTE=false`
- [ ] Tests CI passent : `pytest tests/test_phase5c_safety.py tests/test_phase6_governance.py tests/test_phase7_adaptive_risk.py -v`
- [ ] Base de données migrée / prête

### Lancement
```bash
# Démarrage backend
python run.py
# ou
docker-compose up -d

# Vérification
curl -s http://localhost:8000/api/governance/health | python -m json.tool
# → all_passed: true

curl -s http://localhost:8000/api/analytics/performance-governor | python -m json.tool
# → 5 sections présentes
```

### Vérifications post-démarrage (dans les 15 min)
1. `GET /api/governance/health` → `all_passed: true`
2. `GET /api/analytics/performance-governor` → snapshot non vide
3. `GET /api/governance/regime` → regime détecté
4. Prometheus scrape : métriques `p6_*` et `p7_*` visibles
5. Logs : aucune erreur `[CRITICAL]` ou `[ERROR]` répété

---

## 2. Démarrage Micro-Live

### Pré-requis Paper validés (cf. GO_NO_GO_CHECKLIST.md Palier 1)
- [ ] 12/12 critères GO paper validés
- [ ] 72h+ de paper sans incident
- [ ] Calibration initiale effectuée (latency guard + slippage au minimum)

### Configuration micro-live
```env
TRADING_MODE=live
AUTO_EXECUTE=true
BOT_SYMBOLS=BTCUSDT,ETHUSDT
BOT_LEVERAGE=2
BOT_RISK_PCT=0.002
SMC_MAX_TRADES_DAY=4
SMC_MAX_DAILY_LOSS_PCT=0.5
MULTI_BROKER_MODE=disabled
RECONCILER_ENABLED=true
ADAPTIVE_RISK_ENABLED=true
```

### Lancement
```bash
# 1. Mettre à jour .env avec les valeurs micro-live
# 2. Restart
docker-compose restart smc-engine

# 3. Vérification immédiate
curl -s http://localhost:8000/api/governance/health | python -m json.tool
curl -s http://localhost:8000/api/multi/status | python -m json.tool
curl -s http://localhost:8000/api/multi/balances | python -m json.tool
```

### Monitoring renforcé — Premières 4h
- Checker toutes les 30 min : `GET /api/analytics/performance-governor`
- Vérifier chaque trade exécuté manuellement sur exchange UI
- Vérifier slippage du premier trade vs attendu

---

## 2bis. Surveillance première heure live — 6 métriques critiques

> **Règle quant :** si ces 6 métriques sont vertes, ~95 % des problèmes critiques n'existent pas.

### Dashboard minimal à garder ouvert

| # | Tuile | Métrique |
|---|-------|----------|
| 1 | Rejection rate | `p6_rejection_total / total_signals` |
| 2 | Slippage p95 | `p6_slippage_bps` |
| 3 | Fill delay p95 | `p6_fill_delay_ms` |
| 4 | Adaptive risk mode distribution | `p7_adaptive_risk_mode_total` |
| 5 | Reconciler orphan / ghost count | `p6_reconciler_orphans` / `p6_reconciler_ghosts` |
| 6 | Emergency close failures | `p6_emergency_close_failures` |

---

### M1 — Rejection Rate (tous filtres)

**Détecte :** filtres trop stricts, bug filtre, mauvais régime, liquidity trap mal calibré, correlation filter mal initialisé.

| Niveau | Valeur |
|--------|--------|
| ✅ Normal | < 60 % |
| ⚠️ Warning | 60–80 % |
| 🔴 Critique | > 80 % |

**Action si > 80 % pendant 15 min :**
1. Consulter `p6_rejection_by_filter`
2. Identifier le filtre dominant
3. Vérifier regime filter + liquidity trap

---

### M2 — Slippage réel

**Détecte :** problème d'exécution, mauvais type d'ordre, latence exchange, book thin, spread anormal.

| Niveau | Valeur |
|--------|--------|
| ✅ Normal | < 25 bps |
| ⚠️ Warning | 25–40 bps |
| 🔴 Critique | > 40 bps |

**Action si > 40 bps :**
1. Vérifier profondeur du book
2. Vérifier fill delay
3. Vérifier symboles tradés

---

### M3 — Fill Delay

**Détecte :** latence API, exchange instable, websocket dégradé, réseau lent.

| Niveau | Valeur |
|--------|--------|
| ✅ Normal | < 1000 ms |
| ⚠️ Warning | 1000–2000 ms |
| 🔴 Critique | > 2000 ms |

**Action si > 2s :**
1. Vérifier exchange status
2. Vérifier latence serveur
3. Surveiller slippage (corrélation fréquente)

---

### M4 — Adaptive Risk Mode Distribution

**Détecte :** ARE trop agressif, volatilité extrême, drawdown inattendu, exécution dégradée.

Modes : `normal` · `reduced` · `defensive` · `blocked`

**Distribution attendue (première heure) :**

| Mode | Attendu |
|------|---------|
| normal | 50–70 % |
| reduced | 20–40 % |
| defensive | < 10 % |
| blocked | < 5 % |

**Action si `blocked > 20 %` :**
1. Vérifier ATR ratio
2. Vérifier regime filter
3. Vérifier slippage

---

### M5 — Reconciler Events

**Détecte :** divergence exchange / état interne, problème d'exécution, position mal fermée.

| Niveau | Valeur |
|--------|--------|
| ✅ Normal | 0 |
| ⚠️ Warning | 1 |
| 🔴 Critique | ≥ 1 persistant |

**Action si orphan > 0 :**
1. Vérifier positions exchange
2. Vérifier emergency close logs
3. **Ne jamais ignorer**

---

### M6 — Emergency Close Failures

> ⛔ **Seul signal qui justifie un stop immédiat.**

**Détecte :** ordre de fermeture rejeté, erreur API, problème de position size, bug broker.

| Niveau | Valeur |
|--------|--------|
| ✅ Normal | 0 |
| 🔴 Critique | ≥ 1 |

**Action si > 0 :**
```text
1. PAUSE TRADING immédiate
2. Inspecter logs
3. Ne pas reprendre avant résolution
```

---

### Plan de surveillance première heure

#### T+5 min
```bash
# Rejection rate
curl -s http://localhost:8000/api/analytics/rejection-dashboard | python -m json.tool | grep rejection_rate

# ARE distribution
curl -s http://localhost:8000/api/analytics/adaptive-risk | python -m json.tool | grep -E "mode|blocked"

# Reconciler
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool | grep -E "orphans|ghosts"
```

#### T+15 min
```bash
# Slippage
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool | grep slippage

# Fill delay
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool | grep fill_delay
```

#### T+30 min
```bash
# ARE modes (distribution complète)
curl -s http://localhost:8000/api/analytics/adaptive-risk | python -m json.tool

# Shadow profit missed
curl -s http://localhost:8000/api/analytics/strategy-health | python -m json.tool | grep shadow
```

#### T+60 min — Décision
```text
Si 6 métriques vertes → continuer live
Si warning persistant  → ajuster 1 paramètre (cf. CALIBRATION_PLAN.md)
Si critique            → rollback paper (cf. §6 Rollback)
```

---

### 5 patterns d'incidents les plus fréquents — première heure live

> Ces 5 patterns surprennent **presque tous les moteurs de trading** lors de leur première heure en production.
> Ils sont classés par fréquence d'apparition décroissante.

---

#### P1 — « Le mur de rejets silencieux » (≈70 % des premiers déploiements)

**Ce qui se passe :**
Le moteur démarre, reçoit des signaux, mais n'exécute rien. Aucun trade pendant 20–40 min.
L'opérateur pense que le marché est calme. En réalité, **les filtres rejettent 95–100 % des signaux**.

**Pourquoi :**
Les filtres sont calibrés en paper avec des données historiques propres. En live, les données arrivent
avec du bruit, du spread réel, une latence réseau, et des micro-gaps que le paper ne simule pas.
Les filtres les plus coupables :
- `SIGNAL_TOO_OLD` — le signal met 200 ms de plus à arriver en live qu'en paper
- `EXTREME_VOLATILITY` — l'ATR live diffère légèrement du paper → seuil franchi
- `LIQUIDITY_TRAP` — spread réel plus large que simulé → rejet systématique

**Détection précoce (avant que le rejet atteigne 80 %) :**
```bash
# Voir quel filtre domine AVANT que le taux explose
curl -s http://localhost:8000/api/analytics/rejection-dashboard | jq '.top_filters'
```
Signal d'alerte précoce : `rejection > 60 %` ET `1 filtre > 50 % des rejets`.
Cause racine la plus fréquente : paper utilise OHLC, live utilise bid/ask + spread réel.

**Comment le voir (stade avancé) :**
```bash
# Rejection rate = 95%+ mais total_signals augmente
curl -s http://localhost:8000/api/analytics/rejection-dashboard | python -m json.tool
```

**Résolution :**
1. Consulter `filter_breakdown` → identifier le filtre dominant
2. Vérifier : regime filter thresholds, liquidity trap distance, ATR ratio
3. Si `SIGNAL_TOO_OLD` : augmenter `MAX_SIGNAL_AGE_SEC` de 1–2s
4. Si `LIQUIDITY_TRAP` : vérifier spread réel vs seuil configuré
5. **Ne pas désactiver tous les filtres — ajuster un seul à la fois**

**Prévention :** exécuter 24h de paper avec `LATENCY_SIMULATION=true` avant le live.

---

#### P2 — « Le slippage fantôme » (≈50 % des premiers déploiements)

**Ce qui se passe :**
Les premiers trades passent, mais le slippage réel est 3–8× supérieur au slippage paper.
Le PnL plonge dès les premières exécutions. L'opérateur ne comprend pas pourquoi la stratégie
qui était rentable en paper perd immédiatement.

**Pourquoi :**
En paper, le fill est simulé au mid-price ou au prix du signal. En live :
- L'ordre market hit le **deuxième ou troisième niveau du book** sur les paires peu liquides
- Le **spread bid/ask** réel est 2–5× plus large que le spread paper (surtout hors heures US/EU)
- Les ordres passent pendant des **micro-trous de liquidité** (1–3 secondes après une mèche)

**Détection précoce :**
Le slippage moyen peut paraître correct, mais le PnL est immédiatement négatif.
```bash
# Vérifier le spread réel vs attendu
curl -s http://localhost:8000/api/analytics/execution-quality-live | jq '.slippage_stats'
curl -s http://localhost:8000/api/market/depth | jq '.spread'
```
Signal précoce : `p95 slippage < 40 bps` MAIS `spread réel > expected spread`.
Cause racine : paper ne simule ni le spread dynamique ni la profondeur du book.

**Comment le voir (stade avancé) :**
```bash
# p95 slippage > 40 bps alors que paper était < 10 bps
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool | grep -A3 slippage
```

**Résolution :**
1. Vérifier la profondeur du book sur les symboles tradés
2. Vérifier le type d'ordre (market vs limit)
3. Limiter aux paires les plus liquides (`BTCUSDT`, `ETHUSDT` uniquement en micro-live)
4. Vérifier si les trades passent pendant les heures creuses (00h–06h UTC)
5. Si slippage persiste : réduire `BOT_RISK_PCT` pour diminuer la taille des ordres

**Prévention :** ne jamais micro-live avec plus de 2 paires. Commencer par BTC+ETH.

---

#### P3 — « L'ARE panique froide » (≈40 % des premiers déploiements)

**Ce qui se passe :**
Le système Adaptive Risk Engine (ARE) passe en mode `blocked` dans les 10 premières minutes
et n'en sort plus. Aucun trade ne passe. Les métriques M1–M3 sont vertes, mais l'ARE verrouille tout.

**Pourquoi :**
L'ARE observe les premières métriques live et sur-réagit :
- Le premier fill a un slippage élevé (normal pour le tout premier ordre) → l'ARE dégrade immédiatement
- L'ATR ratio live diffère du paper de quelques % → le seuil `blocked` est franchi
- Le drawdown intraday part de 0 → le moindre tick négatif produit un drawdown relatif de 100 %
  par rapport au « gain attendu » → l'ARE interprète ça comme un stress extrême

**Détection précoce :**
```bash
# Distribution ARE — détecter le blocage AVANT qu'il persiste
curl -s http://localhost:8000/api/analytics/adaptive-risk | jq '.mode_distribution'
```
Signal précoce : `blocked > 20 %` OU `defensive > 40 %` dès T+5 min.
Cause fréquente : l'ARE voit trop peu de trades → estimation instable.

**Comment le voir (stade avancé) :**
```bash
# blocked > 50%, aucun trade depuis le démarrage
curl -s http://localhost:8000/api/analytics/adaptive-risk | python -m json.tool
```

**Résolution :**
1. Vérifier quel facteur ARE domine : `atr_factor`, `slippage_factor`, `drawdown_factor`
2. Si `slippage_factor` : le premier trade a biaisé la moyenne → attendre 3–5 trades
3. Si `drawdown_factor` : normal en début de session, l'ARE se stabilise après ~30 min
4. Si tout est bloqué > 30 min : forcer temporairement `ADAPTIVE_RISK_WARMUP_TRADES=5`
   pour que l'ARE ne bloque pas avant d'avoir vu 5 fills
5. Correction typique : augmenter `ADAPTIVE_RISK_WR_WINDOW` pour stabiliser l'estimation

**Prévention :** configurer `ADAPTIVE_RISK_WARMUP_MINUTES=15` pour donner un warm-up à l'ARE.

---

#### P4 — « Le ghost au premier redémarrage » (≈30 % des premiers déploiements)

**Ce qui se passe :**
Le moteur est redémarré pendant la première heure (pour un fix, un ajustement de config, ou un crash).
Au restart, le reconciler détecte un **ghost** : le state interne contient une position qui n'existe plus
sur l'exchange.

**Pourquoi :**
- Le moteur avait une position ouverte au moment du restart
- Pendant le downtime (même 10 secondes), un SL/TP a été triggeré côté exchange
- Le moteur redémarre avec l'ancien state → position « fantôme »
- Le reconciler doit faire 2 cycles pour confirmer que c'est un vrai ghost (par design, pour éviter
  les faux positifs pendant la reconnexion websocket)

**Détection précoce :**
```bash
# Vérifier immédiatement après tout restart
curl -s http://localhost:8000/api/analytics/performance-governor | jq '.reconciler'
```
Signal : `ghost_count > 0` dans les 2 min post-restart.
Cause : SL/TP triggeré pendant le downtime, le state n'a pas reçu le callback.

**Comment le voir :**
```bash
# ghosts_detected > 0 dans les minutes suivant un restart
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool | grep -E "ghosts|orphans"
```

**Résolution :**
1. Si le ghost est auto-nettoyé (`ghosts_cleaned > 0`) → **normal**, le reconciler fait son travail
2. Si le ghost persiste après 3 min → forcer le reconcile :
   ```bash
   curl -X POST http://localhost:8000/api/live/admin/force-reconcile
   ```
3. Vérifier sur l'exchange UI que la position est bien fermée
4. Si le PnL du ghost trade n'est pas comptabilisé → l'ajouter manuellement au journal

**Prévention :** avant tout restart, vérifier `positions ouvertes = 0` :
```bash
curl -s http://localhost:8000/api/positions/open/all | python -m json.tool
```

---

#### P5 — « Le fill delay en cascade » (≈25 % des premiers déploiements)

**Ce qui se passe :**
La latence de fill augmente progressivement : 800 ms → 1500 ms → 3000 ms → timeout.
Le slippage suit en cascade. Les ordres suivants partent sur des prix périmés.
Éventuellement, un ordre partiellement rempli crée un orphan.

**Pourquoi :**
- L'exchange applique un **rate limiting** progressif sur les comptes nouveaux ou à faible volume
- Le websocket est connecté mais les **confirmations de fill** arrivent par le REST fallback
  (plus lent) au lieu du websocket
- Un burst de 3–4 signaux rapprochés sature la file d'ordres → les fills s'empilent
- En cas de maintenance partielle de l'exchange, les fills sont retardés sans erreur explicite

**Détection précoce :**
Courbe typique d'un fill delay en cascade : `300 ms → 800 ms → 1500 ms → 2500 ms`.
```bash
# Surveiller la tendance AVANT le passage critique
curl -s http://localhost:8000/api/analytics/execution-quality-live | jq '.fill_delay'
```
Signal précoce : fill delay en hausse monotone sur 3 ordres consécutifs.
Cause fréquente : rate limiting exchange ou congestion API.

**Comment le voir (stade avancé) :**
```bash
# fill_delay p95 > 2000 ms, en augmentation
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool | grep -A3 fill_delay

# Vérifier si le websocket est sain
curl -s http://localhost:8000/api/governance/health | python -m json.tool | grep ws
```

**Résolution :**
1. Vérifier le status page de l'exchange (Bitget system status)
2. Vérifier que le websocket est bien connecté (pas en fallback REST)
3. Si rate-limit : réduire `SMC_MAX_TRADES_DAY` temporairement
4. Si latence réseau : vérifier la latence serveur → exchange avec `ping`
5. Si un orphan est créé → suivre procédure INC-02

**Prévention :** ne pas dépasser 4 trades/jour en micro-live. Espacer les symboles.

---

#### Résumé — triage rapide première heure

| Pattern | Signal d'alerte | Gravité | Temps avant impact |
|---------|----------------|---------|-------------------|
| P1 — Mur de rejets | 0 trade + rejection > 95 % | ⚠️ Moyen | 15–30 min (aucun trade = aucun risque) |
| P2 — Slippage fantôme | Slippage > 40 bps sur premiers trades | 🔴 Élevé | Immédiat (PnL impacté dès le 1er fill) |
| P3 — ARE panique froide | `blocked > 50 %` dès T+5 | ⚠️ Moyen | 10 min (bloque le trading, pas de perte) |
| P4 — Ghost au restart | `ghosts > 0` après restart | 🔴 Élevé | 1–3 min (position fantôme dans le state) |
| P5 — Fill delay cascade | Fill delay > 2s en hausse | 🔴 Élevé | 5–15 min (crée orphans + slippage) |

> **P1 et P3 bloquent le trading** → pas de perte directe, mais pas de gain non plus.
> **P2, P4 et P5 impactent le PnL ou le state** → action immédiate requise.

---

### Indicateur synthétique de santé — première heure

| Métrique | Seuil vert |
|----------|------------|
| Rejection rate | < 70 % |
| Slippage p95 | < 40 bps |
| Fill delay p95 | < 2 s |
| ARE blocked | < 10 % |
| Reconciler orphan | 0 |
| Emergency close failures | 0 |

Si ces 6 sont verts → **le moteur est sain**.

---

### Signaux d'arrêt immédiat (très rares)

Stop temporaire obligatoire si :

| Signal | Seuil |
|--------|-------|
| Orphan persistant | > 0 pendant > 5 min |
| Emergency close failure | ≥ 1 |
| Fill delay extrême | > 5 s |
| Slippage extrême | > 80 bps |

---

### Règle opérateur — première heure

> ⚠️ **Ne toucher aucun paramètre pendant les 30 premières minutes sauf incident critique.**
>
> Les premières métriques ARE sont instables par nature (peu de données).
> Ajuster trop tôt crée plus de problèmes que ça n'en résout.

**Plan de triage recommandé :**

```text
T0       → start
T+15 min → vérifier P1 (rejets) et P3 (ARE bloqué)
T+30 min → vérifier P2 (slippage) et P5 (fill delay)
T+60 min → décision : continuer / ajuster / rollback
```

---

## 3. Operator Dashboard — 12 Tuiles

### Layout recommandé (3×4 grid)

| # | Tuile | Source | Refresh | Vert | Orange | Rouge | Action |
|---|-------|--------|---------|------|--------|-------|--------|
| 1 | **Regime actuel** | `GET /api/governance/regime` → `current_regime` | 60s | TREND/RANGE | LOW_VOL | HIGH_VOL | Surveiller si HIGH_VOL persiste |
| 2 | **Health Gates** | `GET /api/governance/health` → `all_passed` | 30s | `true` | 1 gate fail | `critical_passed: false` | Cf. gate en échec |
| 3 | **PnL Journalier** | `GET /api/governance/regime` → `system_state.daily_pnl` | 60s | > 0 | > -0.3% | > -0.5% | CRITICAL = daily loss trigger |
| 4 | **Rejection Rate** | `GET /api/analytics/rejection-dashboard` → `rejection_rate_pct` | 60s | < 50% | 50-80% | > 80% | Identifier filtre dominant |
| 5 | **Slippage p95** | `GET /api/analytics/execution-quality-live` → `slippage.p95` | 60s | < 20 bps | 20-40 bps | > 40 bps | Cf. INC-05 |
| 6 | **Fill Delay p95** | Idem → `fill_delay.p95` | 60s | < 1000 ms | 1-2s | > 2s | Cf. INC-06 |
| 7 | **ARE Mode** | `GET /api/analytics/adaptive-risk` → `by_mode` | 60s | normal > 60% | blocked > 15% | blocked > 40% | Cf. INC-07 |
| 8 | **ARE Multiplier** | Idem → `multiplier.mean` | 60s | > 0.80 | 0.50-0.80 | < 0.50 | Mode défensif permanent = revoir calibrage |
| 9 | **Reconciler** | `GET /api/analytics/execution-quality-live` → `reconciler` | 30s | 0 orphans + 0 ghosts | 1 orphan | ghost ou 2+ orphans | Cf. INC-02/03 |
| 10 | **Emergency Close** | Idem → `emergency_close` | 30s | 0 events | successes > 0 | failures > 0 | Cf. INC-04 |
| 11 | **Trades Aujourd'hui** | `GET /api/analytics/strategy-health` → daily stats | 120s | 1-8 trades | 0 trades (24h) | 0 trades (48h) + signaux | Pipeline bloqué |
| 12 | **Winrate 7j** | Idem → `winrate_pct` | 300s | > 45% | 35-45% | < 35% | Observer, pas d'action immédiate si < 10 trades |

### Endpoint consolidé recommandé

`GET /api/analytics/operator-dashboard` — retourne les 12 valeurs en un seul appel.

---

## 4. Routine quotidienne opérateur

### Check matin (5 min)
```bash
# 1. Health
curl -s http://localhost:8000/api/governance/health | python -m json.tool | grep all_passed

# 2. PnL veille
curl -s http://localhost:8000/api/governance/regime | python -m json.tool | grep daily_pnl

# 3. Dashboard complet
curl -s http://localhost:8000/api/analytics/operator-dashboard | python -m json.tool

# 4. Incidents overnight
curl -s http://localhost:8000/api/analytics/execution-quality-live | python -m json.tool | grep -E "failures|ghosts|orphans"
```

### Check soir (3 min)
```bash
# 1. Trades du jour
curl -s http://localhost:8000/api/analytics/strategy-health | python -m json.tool

# 2. ARE distribution
curl -s http://localhost:8000/api/analytics/adaptive-risk | python -m json.tool

# 3. Rejection summary
curl -s http://localhost:8000/api/analytics/rejection-dashboard | python -m json.tool | grep -E "rate|breakdown"
```

### Review hebdomadaire (30 min)
1. **Rejection audit** : Quel filtre rejette le plus ? Shadow profit missed par filtre ?
2. **Slippage trend** : p50 stable ? p95 en hausse ?
3. **ARE calibration** : Distribution modes stable ? Facteur dominant ?
4. **PnL par regime** : Quel regime est profitable ? Lequel non ?
5. **Calibration** : Un seuil à ajuster ? (cf. CALIBRATION_PLAN.md — 1 seul à la fois)

---

## 5. Maintenance

### Mise à jour code
```bash
# 1. Passer en paper avant update
export TRADING_MODE=paper
export AUTO_EXECUTE=false
docker-compose restart smc-engine

# 2. Vérifier positions ouvertes = 0
curl -s http://localhost:8000/api/positions/open/all | python -m json.tool

# 3. Pull & rebuild
git pull
docker-compose build smc-engine

# 4. Run tests
pytest tests/ -v

# 5. Restart en mode paper pour validation
docker-compose restart smc-engine

# 6. Valider 1h en paper, puis remettre le palier précédent
```

### Rotation logs
- Les logs sont dans les conteneurs Docker → `docker logs smc-engine --tail 1000`
- Pour archivage : `docker logs smc-engine > logs_$(date +%Y%m%d).txt 2>&1`
- Prometheus data retention : configuré dans `prometheus.yml`

### DB maintenance
```bash
# Aggregate daily analytics (manual trigger if needed)
curl -X POST http://localhost:8000/api/analytics/aggregate-today

# Check DB status
curl -s http://localhost:8000/api/live/admin/db-status | python -m json.tool
```

---

## 6. Rollback — Résumé rapide

| De | Vers | Commandes |
|----|------|-----------|
| Paper | Pause | `KILL_SWITCH=true`, `AUTO_EXECUTE=false` |
| Micro-live | Paper | `TRADING_MODE=paper`, `AUTO_EXECUTE=false`, restart |
| Live | Micro-live | `BOT_RISK_PCT=0.002`, `BOT_SYMBOLS=BTCUSDT,ETHUSDT`, `SMC_MAX_TRADES_DAY=4`, restart |
| Live | Pause (urgence) | `KILL_SWITCH=true`, flatten via exchange UI, `TRADING_MODE=paper`, restart |

Détail complet : cf. GO_NO_GO_CHECKLIST.md § Rollback.

---

## 7. Contacts & Références

| Document | Contenu |
|----------|---------|
| [GO_NO_GO_CHECKLIST.md](GO_NO_GO_CHECKLIST.md) | Critères passage entre paliers |
| [CALIBRATION_PLAN.md](CALIBRATION_PLAN.md) | Procédures de tuning filtre par filtre |
| [INCIDENT_PLAYBOOK.md](INCIDENT_PLAYBOOK.md) | 10 scénarios d'incident avec procédures |
| [ROLLOUT_PLAN.md](ROLLOUT_PLAN.md) | Plan de rollout Phase 6 (historique) |
| [ENV_VARS.md](../ENV_VARS.md) | Documentation complète des variables |
| [safe-live.env](../safe-live.env) | Config production par défaut |

---

## Séquence recommandée

```
Paper 72h+ → GO 12/12 → Micro-live 7j+ → GO 10/10 → Live 3a → 3b → 3c
                                                    ↑
                                             revue hebdo obligatoire
```

Pas de raccourci. Un palier à la fois. Mesurer avant d'ajuster.
