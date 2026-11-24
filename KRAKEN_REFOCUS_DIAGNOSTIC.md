# DIAGNOSTIC BRUT — Recentrage Kraken Exclusif

## Résumé Exécutif

**STATUT : PHASE 1 COMPLÈTE** — 1903 tests passing, 0 failures.

Le système a été recentré sur Kraken comme broker primaire exclusif. Tous les defaults implicites `"bitget"` ont été changés, le frontend a été rebrandé, le service API a été renommé `KrakenService`, et le backtest data loader réécrit pour l'API Kraken OHLC.

### Travail complété ✅

| # | Zone | Statut |
|---|------|--------|
| 1 | **Defaults implicites "bitget"** (15+ fichiers backend) | ✅ Tous → `"kraken"` |
| 2 | **Frontend branding** (App.tsx, index.html) | ✅ "Kraken SMC Trader" |
| 3 | **Frontend service** (bitgetService.ts) | ✅ Renommé `krakenService.ts` / `KrakenService` |
| 4 | **Types TypeScript** (BitgetTicker, BITGET_API_URL) | ✅ → `Ticker`, `API_BASE_URL` |
| 5 | **Backtest data loader** (real_data_loader.py) | ✅ Réécrit pour Kraken OHLC API |
| 6 | **Dashboard** (positions, status) | ✅ Kraken-only |
| 7 | **PerformanceAnalyticsPanel** (broker filter) | ✅ Simplifié kraken/combined |
| 8 | **Tests** (21 failures corrigés) | ✅ 1903 passed, 0 failed |

### Reste à faire (Phase 2 — optionnel)

| # | Zone | Priorité |
|---|------|----------|
| 1 | Supprimer fichiers Bitget executor (9 fichiers) | P2 |
| 2 | Supprimer tests Bitget-only (44 tests) | P2 |
| 3 | Renommer 156+ classes CSS `bitget-*` → `kraken-*` | P3 (aliases en place) |
| 4 | Simplifier multi_broker_router → Kraken-only | P2 |
| 5 | Funding réalisé non tracké | P0 |
| 6 | Liquidation price = 0.0 | P1 |
- `backend/brokers/bitget_broker.py`
- `backend/market/bitget_market.py`
- `backend/exchanges/bitget_futures.py`
- `smc_futures/engine/execution/bitget_executor.py`
- `smc_futures/engine/execution/bitget_v2_client.py`

## 3. ABSTRACTIONS MULTI-BROKER À SIMPLIFIER

- `backend/engine/execution/multi_broker_router.py` → Kraken-only ou supprimé
- `backend/routes/multi_broker.py` → 11 endpoints `/api/multi/*` → à déprécier
- `backend/market_data/multi_broker_market.py` → Utiliser kraken_market_client.py
- `smc_futures/multi_broker_loop.py` → Retirer spawn Bitget
- `smc_futures/engine/multi_broker_engine.py` → Retirer BITGET de l'enum

## 4. FRONTEND — 156+ CLASSES CSS "bitget-*"

Toutes les couleurs UI sont namespaced "bitget-" : `bg-bitget-card`, `border-bitget-border`, `text-bitget-cyan`, etc.
→ Renommer en palette neutre (ou "kraken-" si branding voulu).

## 5. FRONTEND — COMPOSANTS MULTI-BROKER

- `Dashboard.tsx` : cartes Bitget + Kraken côte à côte, endpoints `/api/multi/*`
- `PerformanceAnalyticsPanel.tsx` : sélecteur combined/bitget/kraken
- `App.tsx` : imports BitgetService, 190+ refs bitget-*
- `services/bitgetService.ts` : service complet d'appel API Bitget

## 6. TESTS BITGET À SUPPRIMER

- `tests/test_bitget_v2_client.py` (12 tests)
- `tests/test_bitget_v2_payloads.py` (8 tests)
- `tests/test_bitget_v2_silent_bugs.py` (13 tests)
- `tests/test_bitget_signature_fix.py` (11 tests)

## 7. LACUNES KRAKEN CRITIQUES

| # | Issue | Priorité | Impact |
|---|-------|----------|--------|
| 1 | Funding réalisé non trackté | P0 | ~0.03%/24h erreur comptable |
| 2 | Liquidation price = 0.0 | P1 | Métriques de risque incomplètes |
| 3 | Symbol normalization crude dans strict reconciler | P1 | Détection mismatch défaillante |
| 4 | Fees hardcodés 0.04% | P2 | Surestimation gros comptes |
| 5 | PnL accumulation fragile | P2 | Double-count possible |

---

*Diagnostic généré automatiquement. Implémentation en cours.*
