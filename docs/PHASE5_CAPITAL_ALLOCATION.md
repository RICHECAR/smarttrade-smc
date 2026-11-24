# Phase 5 : Allocation Adaptative du Capital & Budget de Risque

## Vue d'ensemble
La Phase 5 introduit un moteur d'allocation adaptative du capital de grade institutionnel. Ce système ajuste dynamiquement le risque et le capital en fonction de la qualité d'exécution en temps réel et de la stabilité de l'infrastructure, tout en maintenant strictement les garanties fail-closed du bot.

## Composants Principaux

### 1. `CapitalAllocator` (`smc_futures/engine/risk/capital_allocator.py`)
Le cœur du système, gérant les régimes de capital et calculant les multiplicateurs de risque.
- **Régimes de Capital** :
    - `FROZEN` : Trading bloqué suite à des incidents critiques (ex : orphelins, remplissages tardifs).
    - `MICRO` : Trading restreint (25% du risque) pour les tests ou après une récupération.
    - `RETRACTION` : Réduction d'urgence (10% du risque) due à un taux élevé de stale.
    - `NORMAL` : Régime opérationnel standard (100% du risque).
    - `EXPANSION` : Risque augmenté (120%) après une stabilité prolongée.
- **Formule du Multiplicateur de Risque** :
    - `multiplicateur = clamp(multiplicateur_regime * (score_qualité - pénalité_latence))`
    - Assure que le risque s'ajuste dynamiquement même au sein d'un régime stable.

### 2. Intégration `GlobalRiskEngine`
- Initialise et interroge le `CapitalAllocator` avant chaque intention de trade.
- Applique les blocages basés sur le régime et transmet le `risk_multiplier` à l'OMS.

### 3. Application par le `OrderManager`
- Reçoit le `risk_multiplier` et l'applique directement à la taille de position calculée.
- Fournit une seconde couche de défense, garantissant que les limites de capital ne sont jamais dépassées.

## Observabilité

### Métriques Prometheus
- `CAPITAL_REGIME` : État actuel de l'allocateur par symbole.
- `RISK_MULTIPLIER` : Le multiplicateur effectif appliqué.
- `CAPITAL_ALLOCATED_PCT` : Pourcentage du risque de base actuellement alloué.
- `EXECUTION_QUALITY_SCORE` : Score en temps réel (0.0 - 1.0) de la qualité d'exécution.

### Logs Structurés JSON
- `CAPITAL_REGIME_CHANGE` : Émis à chaque transition avec la raison et les états ancien/nouveau.
- Intégration avec les logs d'exécution standard pour une auditabilité complète.

## Sécurité & Récupération
- **Fail-Closed** : Toute dérive d'infrastructure détectée (haute latence) ou erreurs critiques déclenche une réduction immédiate ou un gel.
- **Scaling Progressif** : Les transitions ascendantes nécessitent une stabilité significative dans le temps (ex : 72 heures pour l'Expansion).
- **Override Manuel** : Le bot peut être gelé manuellement via les kill-switches existants, que le `CapitalAllocator` respecte.
