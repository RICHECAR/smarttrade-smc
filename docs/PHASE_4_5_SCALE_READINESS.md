# Phase 4.5 : Protocole de Préparation au Passage à l'Échelle

Ce document définit les exigences institutionnelles pour le scaling du capital dans le système `bitget-smc-auto-trader`.

## 1. Porte de Préparation au Scaling (Décision Interne)

Le scaling n'est autorisé que lorsque le système est en état **VERT**.

### VERT : Scaling AUTORISÉ
- `stale_rejection_rate` < 5% (sur 24h)
- `orphan_detected_total` == 0
- `late_fill_total` == 0
- `volatility_sigma` < 0.005 (50 bps)

### AMBRE : Scaling GELÉ (Observationnel)
- `stale_rejection_rate` > 5% mais < 15%
- Pics de `volatility_sigma` observés
- Action : Les opérations doivent investiguer l'infrastructure (latences WS, charge VPS).

### ROUGE : Scaling INTERDIT (Critique)
- `orphan_detected_total` > 0
- `late_fill_total` > 0
- `stale_rejection_rate` > 15%
- Action : Audit critique requis. Le scaling est verrouillé.

## 2. Corrélation Volatilité ↔ Latence

La Phase 4.5 introduit le `LatencyVolatilityAnalyzer`. Son rôle est de prouver si l'infrastructure peut supporter les régimes de haute volatilité.

- Si **Volatilité ↑** et **Taux Stale ↑** linéairement : Le système est à sa limite physique. Ne pas scaler.
- Si **Volatilité ↑** et **Taux Stale reste stable** : L'infrastructure est robuste. Le scaling est plus sûr.

## 3. Opérations & API

Vérifiez la préparation en temps réel via :
`GET /api/health/scale_readiness?symbol=BTCUSDT`

Exemple de réponse JSON :
```json
{
  "symbol": "BTCUSDT",
  "is_ready": true,
  "status": "GREEN",
  "failures": [],
  "stale_rate": 0.02,
  "volatility_sigma": 0.0012,
  "risk_level": "NORMAL"
}
```
