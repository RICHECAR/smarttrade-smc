# 🔴 AUDIT DASHBOARD — DIAGNOSTIC BRUTAL

**Date**: Audit complet du pipeline PnL (Exchange → DB → API → Frontend)  
**Verdict global**: **DÉFAILLANT** — Le dashboard affiche des données PnL incohérentes et potentiellement fausses.

---

## INVENTAIRE DES BUGS — PAR SÉVÉRITÉ

### 🔴 CRITIQUE #1 : DEUX SYSTÈMES DE POSITIONS PARALLÈLES (DIVERGENCE GARANTIE)

**Fichiers**: `backend/routes/stats.py` vs `backend/routes/positions.py`  
**Impact**: Le dashboard affiche des chiffres qui se contredisent mutuellement.

**Le problème**:  
- `Dashboard.tsx` → appelle `/api/stats/summary` → lit la table `live_trades`
- `PerformanceAnalyticsPanel.tsx` → appelle `/api/positions/stats/brokers` → lit la table `persisted_positions`

Ces deux tables sont **alimentées par des pipelines différents**, avec des modèles différents, des calculs différents, et des moments d'écriture différents. **Aucune garantie de synchronisation.**

**Conséquence concrète**: L'utilisateur voit un PnL total de +150$ dans le widget principal (source: live_trades), et un PnL de +87$ dans le panel analytics (source: persisted_positions). C'est inacceptable.

---

### 🔴 CRITIQUE #2 : FILTRE DATE SUR `ts_open` AU LIEU DE `ts_close`

**Fichier**: `backend/routes/stats.py` lignes 68-73  
**Code problématique**:
```python
# Commentaire dit: "Date filter on ts_close (not ts_open)"
# Mais le code fait:
LiveTrade.ts_open >= start_dt,
LiveTrade.ts_open <= end_dt,
```

**Impact**: Un trade ouvert le 1er janvier et fermé le 5 janvier avec +500$ de PnL :
- Si l'utilisateur filtre du 3 au 10 janvier → **ce trade est EXCLU** du summary
- Le PnL affiché est faux, amputé de tous les trades longue durée

**Même bug répliqué dans**: `/rr-series`, `/heatmap`, `/equity-curve` — tous filtrent sur `ts_open`.

---

### 🔴 CRITIQUE #3 : DOUBLE ACCUMULATION PnL DANS LE RÉCONCILIATEUR

**Fichier**: `backend/services/trade_reconciler.py` lignes 148-178  
**Scénario**:

1. Le réconciliateur détecte un trade OPEN absent de l'exchange
2. Il appelle `add_fill(action="REDUCE", pnl=X)` pour chaque fill → **le pnl du parent est incrémenté** (`trade.pnl += X`)
3. Ensuite il appelle `close_live_trade(pnl=total_pnl)` → **le pnl du parent est ÉCRASÉ** par `total_pnl`

**Résultat**: Le PnL est correct **seulement par accident** car `close_live_trade` écrase la valeur accumulée. Mais si un fill REDUCE est ajouté avant et que la logique change, c'est un double-comptage.

**Le vrai risque**: Si `close_live_trade` vérifie `if already CLOSED → skip` (ce qu'il fait), et que `add_fill` a déjà marqué le trade comme `CLOSED` via un fill de type `CLOSE`, alors `close_live_trade` retourne le trade sans correction, et le PnL accumulé par les REDUCE est conservé (potentiellement faux si les REDUCE ne couvrent pas tout).

---

### 🟠 HAUTE #4 : FUNDING RATES NON INCLUS DANS LE PnL RÉALISÉ

**Fichier**: `backend/models/live_trade.py` — `calculate_pnl()`  
**Formule actuelle**:
```python
raw_pnl = (close_price - entry) * qty  # LONG
return raw_pnl - fee
```

**Ce qui manque**: Les funding rates (frais de financement) facturés toutes les 8h sur les marchés futures. Pour un trade maintenu 24h avec un funding de 0.01%, l'erreur est de ~0.03% du notional. Pour un trade de 10,000$ maintenu 3 jours, c'est ~$9 d'erreur non comptabilisée.

**Note**: Le `kraken_pnl_service.py` (unrealized PnL) inclut le funding. Mais le calcul du PnL réalisé à la clôture ne l'inclut **jamais**.

---

### 🟠 HAUTE #5 : HEURISTIQUE DE MATCHING TROP AGRESSIVE DANS LE RÉCONCILIATEUR

**Fichier**: `backend/services/trade_reconciler.py` — `_fetch_bitget_fills()` lignes ~230-250  
**Code**:
```python
# Heuristic B: Symbol + Opposite Side + Occurred after trade opened
elif fill_side == target_order_side and fill_ts > (trade.ts_open.timestamp() * 1000):
    is_match = True  # ← ATTRAPE N'IMPORTE QUEL FILL OPPOSÉ !
```

**Impact**: Si deux trades LONG sont ouverts sur BTCUSDT en même temps, le fill de clôture du trade A peut être attribué au trade B. Le PnL des deux trades est alors faux.

---

### 🟠 HAUTE #6 : TRADES SANS FILLS → FERMÉS AVEC `pnl=None` ET `close_price=entry`

**Fichier**: `backend/services/trade_reconciler.py` lignes 135-145  
Quand aucun fill n'est trouvé, le trade est fermé avec:
```python
close_price=trade.entry,  # ← FAUX, ce n'est pas le vrai prix
pnl=None,                 # ← Comptabilisé comme 0 dans les stats
```

**Impact**: Un trade qui a fait -200$ apparaît comme 0$ dans les stats car `pnl is None` l'exclut du calcul.

---

### 🟡 MOYENNE #7 : CHAMP `notional` INCOHÉRENT (Commentaire vs Code)

**Fichier**: `backend/models/live_trade.py` ligne 79 vs `backend/services/live_trades_service.py` ligne 202
- **Commentaire du modèle**: `# qty * entry * leverage`
- **Code réel**: `notional = qty * entry` (SANS leverage)

Ce n'est pas un bug de PnL (le PnL ne dépend pas de ce champ), mais c'est un risque si un futur code utilise `notional` en croyant que le leverage est inclus.

---

### 🟡 MOYENNE #8 : FRAIS DE TRADING HARDCODÉS

- **Bitget**: Le réconciliateur utilise le champ `fee` du fill (correct)
- **Kraken unrealized PnL**: `kraken_pnl_service.py` hardcode 0.04% par leg
- **Problème**: Les frais réels dépendent du tier VIP, du type d'ordre (maker/taker), et changent. Un taker Bitget à 0.06% vs un maker à 0.02% = erreur de 3x.

---

### 🟢 BASSE #9 : `pnl=0.0` PAR DÉFAUT À LA CRÉATION

**Fichier**: `backend/models/live_trade.py` — `pnl = Column(Float, default=0.0)`  
Un trade OPEN a `pnl=0.0`. Si le filtre stats exclut `pnl IS NULL` mais inclut `pnl=0.0`, les trades OPEN à 0.0 polluent les stats. **Actuellement sauvé** par le filtre `status='CLOSED'`, mais fragile.

---

## RÉSUMÉ: IMPACT SUR LE DASHBOARD

| Composant | Source données | Fiable ? | Problème |
|-----------|---------------|----------|----------|
| PnL Total (Dashboard) | `/api/stats/summary` → live_trades | ❌ | Filtre date sur ts_open, pas ts_close |
| Win Rate (Dashboard) | `/api/stats/summary` → live_trades | ⚠️ | Correct SI les PnL sous-jacents sont justes |
| Analytics Panel (KPIs) | `/api/positions/stats/brokers` → persisted_positions | ❌ | Source différente = chiffres différents |
| Equity Curve | `/api/stats/equity-curve` → live_trades | ❌ | Même filtre date cassé |
| Heatmap | `/api/stats/heatmap` → live_trades | ❌ | Même filtre date cassé |
| R:R Series | `/api/stats/rr-series` → live_trades | ❌ | Même filtre date cassé |
| Table des positions | `/api/positions` → persisted_positions | ⚠️ | Table différente, pas les mêmes trades |

---

## PLAN DE CORRECTION

1. **Unifier sur UNE source**: `live_trades` est la source de vérité (plus complète, plus proche du broker)
2. **Corriger le filtre date**: Filtrer sur `ts_close` pour les trades fermés
3. **Corriger le réconciliateur**: Éviter la double-accumulation, ajouter un garde-fou de quantité dans l'heuristique
4. **Aligner le frontend**: `PerformanceAnalyticsPanel` → même source que `Dashboard.tsx`
5. **Corriger le commentaire notional**: Documenter le vrai calcul
6. **Ajouter metrics institutionnelles**: expectancy, Sharpe ratio, Calmar ratio, streak tracking
