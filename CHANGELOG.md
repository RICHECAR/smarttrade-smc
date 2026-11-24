# Changelog

Tous les changements notables de ce projet sont documentés ici.

## [2.3.1] - 2026-02-19

### 🎯 SOFT BYPASS — Plus d'opportunités SMC valides

#### Problème résolu
Le filtre `NO_LIQUIDITY_SWEEP` bloquait trop d'opportunités valides, même quand:
- BOS/CHoCH était confirmé
- Un Order Block frais était présent
- Le HTF bias était respecté

#### Solution: ALLOW_NO_SWEEP_WITH_OB
Nouveau flag permettant l'entrée sur **BOS + OB frais** même sans sweep de liquidité.
C'est un "soft bypass" — **pas du forçage**, juste un assouplissement SMC propre.

#### Configuration
```env
# Activer le soft bypass (recommandé pour small accounts)
ALLOW_NO_SWEEP_WITH_OB=true

# RR minimum pour soft bypass (default = 1.5, même que global)
SOFT_BYPASS_MIN_RR=1.5

# Age maximum de l'OB (bars) pour soft bypass
SOFT_BYPASS_MAX_OB_AGE=15
```

#### Sécurités maintenues
- ✅ RR >= 1.5 (configurable)
- ✅ Respect du HTF bias (pas de counter-trend)
- ✅ BOS ou CHoCH confirmé requis
- ✅ Order Block frais et aligné avec le bias

#### Log clair
```
[SWEEP_BYPASS_SOFT] XRPUSDT | reason=BOS+OB | htf_bias=bullish | RR_required>=1.5
```

#### Hint amélioré
Si les conditions soft bypass sont présentes mais désactivées:
```
hint: enable ALLOW_NO_SWEEP_WITH_OB=1 (BOS+OB available, safe bypass)
```

#### Fichiers modifiés
- **Modifié:** [signal_builder.py](smc_futures/engine/strategy/signal_builder.py)
- **Modifié:** [smc_config.py](smc_futures/smc_config.py)
- **Modifié:** [orchestrator.py](smc_futures/orchestrator.py)
- **Tests:** [test_soft_bypass.py](tests/test_soft_bypass.py) (11 tests)

---

## [2.3.0] - 2026-02-19

### ✨ SMALL ACCOUNT MODE — Croissance de petit capital

#### Objectif
Permettre de faire croître un petit capital (~$20-100) sans dépôt, en tradant de manière safe et intelligente.

#### Configuration
```env
# Activer le mode petit compte
SMALL_ACCOUNT_MODE=true

# Risque de départ (progression automatique)
SMALL_ACC_RISK_START=1.5

# Risque maximum (hard cap)
SMALL_ACC_RISK_MAX=3.0

# Incrément par rejet (0.25% = conservateur)
SMALL_ACC_RISK_STEP=0.25
```

#### Fonctionnalités

##### 1. Éligibilité automatique des symboles
- Calcul temps réel: équity vs minQty/minNotional de chaque symbole
- Auto-désactivation des symboles inéligibles (BTC/ETH au début)
- Logs explicites: `[SYMBOL_ELIGIBLE]` / `[SYMBOL_INELIGIBLE]`

##### 2. Auto-tuning progressif du risque
- Démarre à `SMALL_ACC_RISK_START` (1.5%)
- Incrémente de `SMALL_ACC_RISK_STEP` (0.25%) après 2 rejets consécutifs
- Plafonné à `SMALL_ACC_RISK_MAX` (3.0%)
- Log: `[RISK_AUTOTUNE] SOLUSDT | risk: 1.50% -> 1.75%`

##### 3. Prévention de l'overtrading
- Cooldown dynamique basé sur l'équité
- Cooldown augmenté après séries perdantes
- Max 1 position par symbole (inchangé)

##### 4. API & Statut
- **Nouveau endpoint:** `GET /api/v1/status`
  ```json
  {
    "equity": 23.0,
    "small_account": {
      "enabled": true,
      "eligible_symbols": ["XRPUSDT", "SOLUSDT", "ETHUSDT"],
      "ineligible_symbols": ["BTCUSDT"],
      "next_equity_thresholds": {
        "BTCUSDT": 95.0
      },
      "risk_pct": {"base": 1.5, "current": 1.75, "max": 3.0}
    }
  }
  ```

#### Seuils d'équité estimés (à 1.5% risque)
| Symbole | Équité min ~$ |
|---------|---------------|
| XRPUSDT | 5 |
| DOGEUSDT | 8 |
| SOLUSDT | 15 |
| ETHUSDT | 32 |
| BTCUSDT | 95 |

#### Fichiers ajoutés/modifiés
- **Nouveau:** [small_account_manager.py](smc_futures/engine/small_account_manager.py)
- **Modifié:** [app_context.py](smc_futures/app_context.py)
- **Modifié:** [orchestrator.py](smc_futures/orchestrator.py)
- **Modifié:** [smc_config.py](smc_futures/smc_config.py)
- **Tests:** [test_small_account.py](tests/test_small_account.py) (15 tests)

---

## [2.2.0] - 2026-02-19

### 🐛 Corrections

#### Erreur 45111 "less than minimum order quantity" — ÉLIMINÉE
- **Problème:** Les ordres avec qty trop petite (ex: SOL 0.0042, XRP 0.23) étaient rejetés par Bitget avec l'erreur 45111.
- **Cause:** Pas de validation `minQty` / `minNotional` avant l'envoi à l'API.
- **Solution:** 
  - **Nouveau module:** [contract_specs.py](smc_futures/engine/execution/contract_specs.py) — Contraintes de contrat par symbole
  - **Validation pré-vol:** Intégrée dans `OrderManager.execute_signal()` AVANT l'envoi
  - **Logs explicites:** `[QTY_CHECK]`, `[QTY_CLAMPED]`, `[ORDER_REJECTED_PRE_FLIGHT]`
- **Fichiers modifiés:** 
  - [contract_specs.py](smc_futures/engine/execution/contract_specs.py) (nouveau)
  - [order_manager.py](smc_futures/engine/execution/order_manager.py)

#### Comportement de validation
| Situation | Action | Résultat |
|-----------|--------|----------|
| qty ≥ minQty, notional ≥ minNotional | Aucune | `OK` — ordre envoyé |
| qty < minQty, clamp ≤ 50% risque | Arrondi vers le haut | `CLAMPED` — ordre envoyé avec qty ajustée |
| qty << minQty, clamp > 50% risque | Rejet pré-vol | `ORDER_REJECTED_PRE_FLIGHT` — pas d'appel API |
| notional < minNotional | Même logique | Clamp ou rejet selon déviation |

### ✨ Spécifications de contrats

#### Symboles supportés (statique)
| Symbole | minQty | minNotional | qtyStep | priceScale |
|---------|--------|-------------|---------|------------|
| BTCUSDT | 0.001 | 5.0 | 0.001 | 1 |
| ETHUSDT | 0.01 | 5.0 | 0.01 | 2 |
| SOLUSDT | 0.1 | 5.0 | 0.1 | 2 |
| XRPUSDT | 1.0 | 5.0 | 1.0 | 4 |
| DOGEUSDT | 10.0 | 5.0 | 1.0 | 5 |
| + 5 autres... | | | | |

#### Fetch dynamique (optionnel)
```python
from smc_futures.engine.execution.contract_specs import fetch_contract_specs_from_api
specs = await fetch_contract_specs_from_api("ARBUSDT")
```

### 🧪 Tests ajoutés

- **Nouveau fichier:** [tests/test_contract_specs.py](tests/test_contract_specs.py)
- **17 tests:** Validation qty, clamp, rejet, scénarios réels (SOL/XRP)
- **Nouveau fichier:** [tests/test_risk_upscale.py](tests/test_risk_upscale.py)
- **8 tests:** Auto risk upscale, SYMBOL_INELIGIBLE, boundary cases

### ✨ AUTO_RISK_UPSCALE (Nouveau)

#### Fonctionnalité
Ajustement automatique du risque quand `raw_qty < minQty` pour atteindre la quantité minimum requise.

#### Configuration
```env
# Activer l'auto-upscale du risque
AUTO_RISK_UPSCALE=true

# Risque maximum autorisé lors de l'upscale (3% par défaut)
AUTO_RISK_UPSCALE_MAX_PCT=3.0
```

#### Comportement
| Condition | Action | Log |
|-----------|--------|-----|
| qty ≥ minQty | Pas d'upscale | — |
| qty < minQty, risk_requis ≤ max | Upscale risque | `[RISK_UPSCALE] 1.0% -> 2.5%` |
| qty < minQty, risk_requis > max | Rejet | `[RISK] SYMBOL_INELIGIBLE` |

#### Fichiers modifiés
- [trade_validator.py](smc_futures/engine/strategy/trade_validator.py) — Logique upscale
- [smc_config.py](smc_futures/smc_config.py) — Nouveaux paramètres
- [orchestrator.py](smc_futures/orchestrator.py) — Passage des params
- [.env.example](.env.example) — Documentation

---

## [2.1.0] - 2026-02-19

### 🐛 Corrections

#### Comparaison R:R avec Epsilon
- **Problème:** La comparaison `rr >= min_rr` pouvait échouer pour des valeurs comme `1.9999999 < 2.0` à cause de l'imprécision floating-point.
- **Solution:** Ajout d'une constante `RR_EPSILON = 1e-6` et comparaison `rr >= (min_rr - RR_EPSILON)`.
- **Fichiers:** [signal_builder.py](smc_futures/engine/strategy/signal_builder.py#L17), [order_manager.py](smc_futures/engine/execution/order_manager.py#L27)

#### Pagination HTF Candles
- **Problème:** La méthode `get_candles_paginated` ne faisait pas de vraie pagination (récupérait les mêmes candles à chaque appel).
- **Solution:** Utilisation de `endTime` pour paginater vers le passé correctement.
- **Fichiers:** [bitget_v2_client.py](smc_futures/engine/execution/bitget_v2_client.py#L325)

### ✨ Améliorations

#### Configuration
- **Ajout:** Fichier `.env.example` complet avec tous les paramètres documentés.
- **Organisation:** Variables groupées par catégorie (API, Trading Mode, Risk, SMC, etc.).

#### Tests Pytest
- **Nouveau fichier:** [tests/test_core_functionality.py](tests/test_core_functionality.py)
- **Couverture:**
  - Signature HMAC-SHA256 (canonical query, empty query)
  - Comparaison R:R avec epsilon
  - Arrondi TPSL par symbole
  - Clamp leverage
  - Parsing positions V2
  - Validation configuration

#### Documentation
- **Nouveau fichier:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **README mis à jour:**
  - Installation Windows
  - Modes de trading
  - Configuration SMC
  - Troubleshooting erreurs Bitget
  - Structure projet

### 📋 Audit complet

#### Configurations vérifiées
| Variable | Statut | Note |
|----------|--------|------|
| `TRADING_MODE` | ✅ | live/paper |
| `AUTO_EXECUTE` | ✅ | true/shadow/false |
| `BITGET_REAL_ONLY` | ✅ | Enforcement strict |
| `MIN_RR` | ✅ | Avec epsilon |
| `BOT_LEVERAGE` | ✅ | Clamp 10x max |

#### Endpoints Bitget V2 vérifiés
| Endpoint | Statut | Note |
|----------|--------|------|
| Positions | ✅ | `/api/v2/mix/position/all-position` |
| Set Leverage | ✅ | Body inclut `productType` |
| Place Order | ✅ | Signature correcte |
| TPSL | ✅ | Arrondi selon priceScale |

#### Bug "neutral_permissive dupliqué"
- **Statut:** Non trouvé dans le code actuel
- **Vérification:** Import Python et instanciation OK

---

## [2.0.0] - Antérieur

Version initiale avec support Bitget V2 API.
