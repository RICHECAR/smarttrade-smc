# Phase 4.5 : Préparation au Passage à l'Échelle & Durcissement Volatilité

## Vue d'ensemble
La Phase 4.5 introduit une infrastructure de monitoring de grade institutionnel pour assurer la protection du capital pendant les périodes de forte volatilité. Le système corrèle désormais la volatilité du marché avec la latence d'exécution afin de détecter les goulets d'étranglement physiques de l'infrastructure avant toute augmentation de capital.

## Composants Principaux

### 1. Fenêtres de Latence (`LatencyTracker`)
Instrumentation de précision du cycle de vie d'un ordre. Capture 5 timestamps clés :
- **t_signal** : Point de décision de la stratégie.
- **t_order_send** : Pré-appel API REST.
- **t_exchange_ack** : Réponse de l'API REST (Confirmation/OrderId).
- **t_ws_update** : Premier événement de mise à jour WebSocket.
- **t_fill** : Événement de remplissage final ou partiel.

**Métriques :** `rest_rtt`, `decision_to_send`, `ack_to_ws`, et `end_to_end` (Signal vers WS).

### 2. Analyse Volatilité ↔ Latence (`LatencyVolatilityAnalyzer`)
Module statistique avancé qui effectue :
- **Sigma Glissant** : Écart-type des log-returns (intervalles de 1 minute).
- **Corrélation** : Coefficients de Pearson et Spearman entre volatilité et taux de rejets stale.
- **Régression de Huber** : Régression linéaire robuste pour détecter si la volatilité est le facteur principal de la latence (pente > 10.0 + R² > 0.6 = limite physique suspectée).

### 3. Porte de Préparation au Scaling (`ScaleReadinessGate`)
Système à trois états respecté avant toute augmentation d'allocation de capital :
- 🟢 **VERT** : `stale_rate < 5%`, `orphan=0`, `late_fill=0`, `sigma < 0.005`. Prêt pour le scaling.
- 🟡 **AMBRE** : `stale_rate 5-15%` ou pics de volatilité. Scaling suspendu, surveillance requise.
- 🔴 **ROUGE** : `stale_rate > 15%`, `orphan > 0`, ou `late_fill > 0`. Scaling bloqué, investigation requise.

## Endpoints API

- `GET /api/health/scale_readiness?symbol=BTCUSDT` : Retourne le statut détaillé de la porte et les métriques.
- `GET /api/health/execution` : Retourne un résumé de performance incluant les histogrammes de latence.

## Checklist de Validation
- [ ] Vérifier que Prometheus exporte `latency_histogram` et `volatility_sigma`.
- [ ] Vérifier `/api/health/scale_readiness` pour un statut "GREEN" en conditions normales.
- [ ] Simuler un taux élevé de stale (AMBER/RED) et vérifier la transition de la porte.
