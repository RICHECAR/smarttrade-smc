# Plan de Calibration — Filtres & Adaptive Risk Engine

## Principe fondamental

> **Une seule famille de paramètres ajustée à la fois.**  
> Observer 48 h minimum entre chaque changement.  
> Ne jamais ajuster deux filtres simultanément — impossible d'attribuer l'effet.

---

## Ordre de calibration recommandé

| Priorité | Famille | Raison |
|----------|---------|--------|
| 1 | Latency Guard | Impact binaire (accept/reject), facile à mesurer |
| 2 | Slippage thresholds | Impact direct sur P&L, données immédiates |
| 3 | Extreme Volatility Filter | Faux positifs courants, impact majeur |
| 4 | Regime Filter | Impact sur la stratégie, besoin de données |
| 5 | Adaptive Risk Engine | Ajustement fin, nécessite contexte multi-facteur |
| 6 | Liquidity Trap | Dépend de la qualité heatmap, calibrer en dernier |
| 7 | Correlation Filter | Impact sur multi-symbole, besoin de données croisées |

---

## 1. Latency Guard

### Variable(s)
| Variable | Défaut | Range recommandé |
|----------|--------|-----------------|
| `MAX_SIGNAL_AGE_SEC` | `5.0` | 3.0 – 15.0 |

### Diagnostic

| Symptôme | Cause probable | Métrique à observer |
|----------|---------------|---------------------|
| **Trop strict** : > 5% des signaux rejetés `SIGNAL_TOO_OLD` | Seuil trop bas pour la latence réelle du pipeline | `rejection_dashboard.filter_breakdown.SIGNAL_TOO_OLD / total_signals` |
| **Trop laxiste** : Trades exécutés sur prix stale, slippage élevé | Seuil trop haut, le marché bouge entre signal et exécution | Corrélation `signal_age` vs `slippage_bps` dans les logs |

### Procédure
1. Observer le taux de rejection `SIGNAL_TOO_OLD` pendant 48h
2. Si > 10% : augmenter `MAX_SIGNAL_AGE_SEC` par pas de +2s
3. Si < 1% et slippage élevé : réduire par pas de -1s
4. Cible idéale : 1-3% de rejects signal_age

### Cadence : Semaine 1 du paper

---

## 2. Slippage Thresholds

### Variables
| Variable | Défaut | Range recommandé |
|----------|--------|-----------------|
| `MAX_ENTRY_SLIPPAGE_ATR` | `0.3` | 0.1 – 0.5 |
| `MAX_ENTRY_SLIPPAGE_BPS` | `30` | 15 – 60 |
| `SLIPPAGE_KILL_THRESHOLD_BPS` | `60` | 40 – 100 |
| `SLIPPAGE_KILL_COOLDOWN_SEC` | `300` | 120 – 600 |

### Diagnostic

| Symptôme | Cause probable | Métrique |
|----------|---------------|----------|
| **Trop strict** : Kills fréquents (> 1/jour), positions fermées immédiatement | Warning threshold trop bas pour le spread normal | `p6_emergency_close_total{outcome="success"}` rate, `p6_slippage_bps` p50 |
| **Trop laxiste** : Entries avec 50+ bps de slippage non détectées | Seuils trop hauts, market impact non mesuré | `p6_slippage_bps` histogram, distribution > 30 bps |

### Procédure
1. Collecter distribution de slippage sur 48h paper
2. Le p50 donne le slippage "normal" pour ce broker/symbole
3. `MAX_ENTRY_SLIPPAGE_BPS` = p50 × 2 (arrondi par 5)
4. `SLIPPAGE_KILL_THRESHOLD_BPS` = p95 × 1.5 (arrondi par 10)
5. Ajuster par pas de ±5 bps

### Cadence : Semaine 1, après latency guard

---

## 3. Extreme Volatility Filter

### Variables
| Variable | Défaut | Range recommandé |
|----------|--------|-----------------|
| `EXTREME_VOLATILITY_MAX_ATR_RATIO` | `2.5` | 2.0 – 4.0 |
| `ATR_MEAN_WINDOW` | `100` | 50 – 200 |
| `VOLATILITY_FILTER_MIN_ATR_RATIO` | `0.4` | 0.3 – 0.6 |

### Diagnostic

| Symptôme | Cause probable | Métrique |
|----------|---------------|----------|
| **Trop strict** : > 10% des signaux bloqués `EXTREME_VOLATILITY` pendant sessions normales | Seuil ATR ratio trop bas, ou fenêtre ATR moyenne trop courte | `rejection_dashboard.filter_breakdown.EXTREME_VOLATILITY`, check si ces rejections arrivent pendant London/NY overlap |
| **Trop laxiste** : Trades exécutés en conditions extrêmes (news, flash crash), pertes larges | Seuil trop haut, fenêtre trop longue (lisse trop) | Trades avec PnL < -2× risk_pct ET ATR ratio > 2.0 au moment du trade |
| **Dead market aussi** : Trop de `DEAD_MARKET_LOW_VOLATILITY` en session Asie | `VOLATILITY_FILTER_MIN_ATR_RATIO` trop strict pour les sessions calmes | filter_breakdown.DEAD_MARKET et horaire |

### Procédure
1. Mesurer la distribution ATR ratio sur 72h (toutes sessions)
2. Le p95 de la distribution = seuil naturel extrême
3. `EXTREME_VOLATILITY_MAX_ATR_RATIO` = max(p95, 2.5)
4. Augmenter par pas de +0.3 si trop de blocages
5. Réduire `ATR_MEAN_WINDOW` si la distribution est trop smooth (50-100 bougies)

### Cadence : Semaine 2

---

## 4. Regime Filter

### Variables
| Variable | Défaut | Range recommandé |
|----------|--------|-----------------|
| `REGIME_TREND_THRESHOLD` | `4` | 3 – 6 |
| `REGIME_HIGH_VOL_ATR_RATIO` | `2.0` | 1.5 – 2.5 |
| `REGIME_LOW_VOL_ATR_RATIO` | `0.5` | 0.3 – 0.7 |
| `REGIME_LOOKBACK` | `20` | 10 – 40 |
| `REGIME_COMPRESSION_RATIO` | `1.0` | 0.7 – 1.5 |

### Diagnostic

| Symptôme | Cause probable | Métrique |
|----------|---------------|----------|
| **Trop strict** : > 30% des signaux bloqués par regime mismatch | `REGIME_TREND_THRESHOLD` trop bas (détecte TREND trop facilement), ou HIGH/LOW_VOL seuils trop agressifs | `p6_strategy_trades_total` par regime, `rejection_dashboard.filter_breakdown.REGIME_*` |
| **Trop laxiste** : Trades mean-reversion en TREND (grosses pertes), breakout en RANGE (faux breakouts) | Détection trop late ou seuils trop hauts | PnL négatif net par regime+strategy combo dans `strategy_health` |
| **Oscillation** : Regime change > 5×/jour | Lookback trop court ou thresholds trop proches | `GET /api/analytics/regime-history` → count changes/24h |

### Procédure
1. Observer la distribution des régimes sur 72h : `GET /api/analytics/regime-history`
2. Si TREND détecté > 60% du temps : augmenter `REGIME_TREND_THRESHOLD` de +1
3. Si HIGH_VOL détecté > 20% du temps en conditions normales : augmenter `REGIME_HIGH_VOL_ATR_RATIO` de +0.3
4. Si oscillation : augmenter `REGIME_LOOKBACK` de +5

### Cadence : Semaine 2-3

---

## 5. Adaptive Risk Engine

### Variables — Volatilité
| Variable | Défaut | Range |
|----------|--------|-------|
| `ADAPTIVE_RISK_ATR_REDUCED` | `1.5` | 1.2 – 2.0 |
| `ADAPTIVE_RISK_ATR_DEFENSIVE` | `2.0` | 1.5 – 2.5 |
| `ADAPTIVE_RISK_ATR_BLOCK` | `2.8` | 2.0 – 3.5 |

### Variables — Drawdown
| Variable | Défaut | Range |
|----------|--------|-------|
| `ADAPTIVE_RISK_DD_REDUCED_PCT` | `1.5` | 1.0 – 2.5 |
| `ADAPTIVE_RISK_DD_DEFENSIVE_PCT` | `2.5` | 1.5 – 3.5 |
| `ADAPTIVE_RISK_DD_BLOCK_PCT` | `3.5` | 2.5 – 5.0 |

### Variables — Performance
| Variable | Défaut | Range |
|----------|--------|-------|
| `ADAPTIVE_RISK_MIN_WR` | `0.45` | 0.35 – 0.55 |
| `ADAPTIVE_RISK_MIN_PF` | `1.05` | 0.90 – 1.20 |
| `ADAPTIVE_RISK_WR_WINDOW` | `20` | 10 – 50 |

### Variables — Execution
| Variable | Défaut | Range |
|----------|--------|-------|
| `ADAPTIVE_RISK_MAX_RECENT_KILLS` | `2` | 1 – 5 |
| `ADAPTIVE_RISK_MAX_RECENT_SLIPPAGE_BPS` | `35` | 20 – 60 |
| `ADAPTIVE_RISK_MAX_RECENT_FILL_DELAY_MS` | `2000` | 1000 – 5000 |

### Diagnostic

| Symptôme | Cause probable | Métrique |
|----------|---------------|----------|
| **Trop strict** : ARE `blocked` > 15% du temps, `mode=normal` < 40% | Seuils trop bas — le moteur croit le marché dangereux en permanence | `GET /api/analytics/adaptive-risk` → `by_mode` distribution |
| **Trop laxiste** : ARE toujours `normal` même en conditions dégradées | Seuils trop hauts, jamais déclenchés | Idem — `reduced` + `defensive` < 5% |
| **Asymétrie** : Un seul facteur domine les blocages | Un seuil calibré trop strict | `adaptive_risk.block_reasons` → top reason |

### Procédure de calibration ARE
1. **Phase 1 (72h paper)** : Observer la distribution des modes
   - Cible : normal 60-80%, reduced 15-25%, defensive 3-8%, blocked < 5%
2. **Phase 2** : Si blocked > 15%, identifier le facteur dominant via `block_reasons`
3. **Ajuster UN facteur à la fois** :
   - Volatilité trop strict → augmenter `ATR_REDUCED` de +0.2
   - Drawdown trop strict → augmenter `DD_REDUCED_PCT` de +0.5
   - Performance trop strict → réduire `MIN_WR` de -0.05
   - Execution trop strict → augmenter `MAX_RECENT_SLIPPAGE_BPS` de +5
4. **Attendre 48h** entre chaque changement
5. **Vérifier** : le facteur ajusté a perdu en % de blocage sans augmenter les pertes

### Cadence : Semaine 2-4, continue pendant micro-live

---

## 6. Liquidity Trap Filter

### Variables
| Variable | Défaut | Range |
|----------|--------|-------|
| `LIQUIDITY_TRAP_MIN_ATR_DIST` | `0.3` | 0.1 – 0.8 |
| `LIQUIDITY_HEATMAP_MIN_CLUSTER_SIZE` | `3` | 2 – 5 |

### Diagnostic

| Symptôme | Cause probable | Métrique |
|----------|---------------|----------|
| **Trop strict** : > 15% des signaux bloqués `LIQUIDITY_TRAP` | Distance ATR trop haute — beaucoup de clusters proches | `rejection_dashboard.filter_breakdown.LIQUIDITY_TRAP` vs total |
| **Trop laxiste** : Trades entrent puis SL touché par stop hunt immédiat | Distance trop basse, ou heatmap manque des clusters | Trades avec `loss` ET `entry near cluster` (log analysis) |

### Procédure
1. Observer le taux de blocage `LIQUIDITY_TRAP` sur 72h
2. Si > 10% : réduire `MIN_ATR_DIST` par pas de -0.05
3. Si 0% et stop hunts observés : augmenter par pas de +0.1
4. Augmenter `MIN_CLUSTER_SIZE` si trop de faux clusters (bruit)

### Cadence : Semaine 3-4

---

## 7. Correlation Filter

### Variables
| Variable | Défaut | Range |
|----------|--------|-------|
| `CORRELATION_RISK_MULT` | `0.4` | 0.3 – 0.7 |
| `CORRELATION_MAX_POSITIONS` | `2` | 1 – 3 |
| `CORRELATION_GROUPS` | `majors:BTCUSDT,ETHUSDT\|alts:SOLUSDT,XRPUSDT,ADAUSDT` | — |

### Diagnostic

| Symptôme | Cause probable | Métrique |
|----------|---------------|----------|
| **Trop strict** : Trades bloqués `CORRELATION_BLOCKED` alors que les actifs divergent | Groupes trop larges ou `MAX_POSITIONS` = 1 | `rejection_dashboard.filter_breakdown.CORRELATION_BLOCKED`, vérifier corrélation réelle entre symboles |
| **Trop laxiste** : Drawdown amplifié par positions corrélées simultanées | `RISK_MULT` trop haut ou groupes mal définis | PnL négatif simultané sur symboles du même groupe |

### Procédure
1. Observer la fréquence de `CORRELATION_BLOCKED` sur 1 semaine
2. Si > 5 blocages/semaine : revoir les groupes (split si nécessaire)
3. Si drawdown amplifié par corrélation : réduire `RISK_MULT` de -0.1
4. Vérifier la corrélation réelle toutes les 2 semaines

### Cadence : Semaine 4+, revue mensuelle

---

## Tableau de suivi calibration

| Date | Famille | Variable | Ancienne val. | Nouvelle val. | Raison | Résultat après 48h |
|------|---------|----------|---------------|---------------|--------|---------------------|
| | | | | | | |

> Remplir ce tableau à chaque changement. Un seul changement à la fois. Observer 48h minimum.
