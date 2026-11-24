# Decision Matrix for Asia Thresholds (Manual / Automatic)

> On ne rollback PAS sur un trade. On rollback sur une **tendance statistique**.

## 🧮 KPIs de décision (session ASIA uniquement)

### ✅ KPIs “KEEP” (seuils validés)
Les nouveaux thresholds sont **conservés** si **TOUS** sont vrais sur ≥ 20 trades Asia :

| KPI               | Condition             |
| ----------------- | --------------------- |
| Win Rate          | ≥ 45%                 |
| Expectancy        | > 0                   |
| Profit Factor     | ≥ 1.2                 |
| Max Drawdown Asia | ≤ 1.5 × DD historique |
| Trades/day Asia   | ≤ +50% vs baseline    |

### ⚠️ KPIs “WARNING” (surveillance)
Si **1 seul** de ces critères est atteint :

| KPI             | Condition          |
| ----------------- | ------------------ |
| Win Rate          | 40–45%             |
| Série de pertes   | ≥ 4                |
| Drawdown          | +20% vs baseline   |
| Overtrading       | > +60% trades Asia |

➡️ **Action** : Réduire `ASIA_RISK_FACTOR` (ex: 0.5 → 0.35).

### ❌ KPIs “ROLLBACK” (immédiat)
Rollback automatique si **un seul** est vrai :

| KPI              | Condition                   |
| ----------------- | --------------------------- |
| Win Rate Asia     | < 40% après ≥ 20 trades     |
| Profit Factor     | < 1.0                       |
| Max DD            | > 2 × DD historique         |
| Trades sans edge  | ≥ 10 trades consécutifs R<1 |

➡️ **Action** :
```yaml
BTC: 6.0 | ETH: 5.0 | ALT: 4.0
ASIA_RISK_FACTOR: 1.0
```
