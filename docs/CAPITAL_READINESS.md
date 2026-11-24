# CAPITAL READINESS — Tier Assessment Protocol
## Phase 11 — Capital Gating from 0€ to 1M€

---

## Tiers

| Tier | Capital Max | Prérequis |
|------|------------|-----------|
| PAPER | 0€ | Système installé |
| TIER_1 | 1 000€ | Preflight OK, paper 24h stable |
| TIER_2 | 10 000€ | 7j live, 0 incident critique |
| TIER_3 | 50 000€ | 30j live, drawdown < 10%, Sharpe > 0.5 |
| TIER_4 | 100 000€ | 90j live, drawdown < 8%, Sharpe > 1.0 |
| TIER_5 | 1 000 000€ | 180j live, métriques institutionnelles |

---

## PAPER → TIER_1 (0€ → 1 000€)

### Conditions obligatoires:
- [ ] `python scripts/go_live_check.py` → READY
- [ ] Paper trading 24h sans crash
- [ ] Toutes les alertes Telegram fonctionnelles
- [ ] Backup automatique vérifié
- [ ] Drills passés (9/9)
- [ ] `.env` configuré avec `TRADING_MODE=live`
- [ ] Kill switch testé manuellement
- [ ] Safe mode testé manuellement

### Métriques minimales (paper):
- Uptime > 99%
- 0 position orpheline
- 0 kill switch non-intentionnel
- Latence p99 < 2s

---

## TIER_1 → TIER_2 (1 000€ → 10 000€)

### Conditions obligatoires:
- [ ] 7 jours consécutifs en live avec capital réel
- [ ] 0 incident critique (kill switch, safe mode non-intentionnel)
- [ ] 0 position orpheline
- [ ] Tous les SL/TP ont été placés correctement
- [ ] Backup quotidien vérifié
- [ ] Preflight passé chaque jour
- [ ] Journal d'incident tenu

### Métriques minimales:
- Win rate > 40%
- Drawdown max < 15%
- Ratio trades exécutés / signaux > 90%
- 0 emergency close

---

## TIER_2 → TIER_3 (10 000€ → 50 000€)

### Conditions obligatoires:
- [ ] 30 jours consécutifs en live
- [ ] Aucune interruption > 1h non planifiée
- [ ] Recovery protocol testé en live au moins 1 fois
- [ ] Consistency checker stable (0 drift non-résolu)

### Métriques minimales:
- Sharpe ratio > 0.5
- Drawdown max < 10%
- Profit factor > 1.3
- Expectancy > 0
- Slippage moyen < 30 bps

---

## TIER_3 → TIER_4 (50 000€ → 100 000€)

### Conditions obligatoires:
- [ ] 90 jours consécutifs
- [ ] Stress test passé (volatilité extrême documentée)
- [ ] Procédures d'incident rodées (au moins 2 incidents gérés)

### Métriques minimales:
- Sharpe ratio > 1.0
- Drawdown max < 8%
- Profit factor > 1.5
- 0 perte > 3% en un jour
- Corrélation intra-portfolio < 0.6

---

## TIER_4 → TIER_5 (100 000€ → 1 000 000€)

### Conditions obligatoires:
- [ ] 180 jours live
- [ ] Audit complet du système par un tiers (ou self-audit documenté)
- [ ] Disaster recovery testé (restore complet depuis backup)
- [ ] Multi-broker diversification validée

### Métriques institutionnelles:
- Sharpe > 1.5
- Drawdown max < 5%
- Sortino > 2.0
- VaR 99% respecté
- 0 incident critique sur 90j glissants

---

## Commandes utiles

```bash
# Évaluation actuelle
python scripts/go_live_check.py --capital

# Backup avant changement de tier
python scripts/go_live_check.py --backup

# Validation complète
python scripts/go_live_check.py

# Dashboard opérationnel
python scripts/go_live_check.py --dashboard
```

---

## Règle d'or

> **Ne JAMAIS monter de tier sans avoir rempli 100% des conditions.**
> Un drawdown de 5% sur 1 000€ = 50€.
> Un drawdown de 5% sur 100 000€ = 5 000€.
> La discipline de gating EST votre avantage.
