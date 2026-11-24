# KRAKEN PHASE 2 — Hardening Report

**Date**: 2 avril 2026
**Status**: COMPLÉTÉ

---

## Contexte

Ce repo était à l'origine un trader Bitget. La Phase 1 a renommé le service
principal en `KrakenService`, changé les types publics, et créé des alias CSS.
La Phase 2 éradique la dette résiduelle et blinde le repo contre tout retour.

---

## Changements Phase 2

### 1. CSS / Design System — ÉRADIQUÉ

| Action | Détail |
|--------|--------|
| Tailwind config | Supprimé la palette `bitget:{}`, conservé uniquement `kraken:{}` |
| Classes CSS | ~220 occurrences `bitget-*` remplacées par `kraken-*` dans 6 fichiers |
| Fichiers touchés | `index.html`, `App.tsx`, `ChartVisualizer.tsx`, `PerformanceAnalyticsPanel.tsx`, `PositionsTable.tsx`, `SettingsView.tsx`, `Dashboard.tsx` |

### 2. Headers HTTP — MIGRÉ

| Ancien | Nouveau | Statut |
|--------|---------|--------|
| `X-BITGET-KEY` | `X-API-KEY` | Frontend migré, backend accepte les deux |
| `X-BITGET-SECRET` | `X-API-SECRET` | Idem |
| `X-BITGET-PASSPHRASE` | `X-API-PASSPHRASE` | Idem |

**Chaîne complète** : `krakenService.ts` → `server.cjs` → `orchestrator_with_ollama.py`

Le `server.cjs` accepte les anciens headers en fallback pour la transition.

### 3. Routes API — RENOMMÉ

| Ancien endpoint | Nouveau endpoint | Compatibilité |
|-----------------|------------------|---------------|
| `POST /api/sign/bitget` | `POST /api/sign/kraken` | Les deux routes actives |
| `POST /api/validate/bitget` | `POST /api/validate/kraken` | Les deux routes actives |

### 4. Executors Bitget — PURGE

**5 fichiers supprimés** (code mort confirmé) :

| Fichier | Raison |
|---------|--------|
| `backend/bitget_v2_mix_client.py` | Client dupliqué, seul consommateur (`smarttrade_api.py`) est lui-même mort |
| `backend/exchanges/bitget_futures.py` | Legacy jamais importé en production |
| `backend/brokers/bitget_broker.py` | Wrapper jamais instancié |
| `backend/execution/bitget_executor.py` | Wrapper inutile autour du vrai executor |
| `backend/engine/execution/bitget_futures.py` | Fichier orphelin, 0 import |

**4 fichiers conservés** (code actif, connectent réellement à Bitget API) :

| Fichier | Justification |
|---------|---------------|
| `smc_futures/engine/execution/bitget_v2_client.py` | Client API Bitget central, utilisé par `app_context.py` |
| `smc_futures/engine/execution/bitget_executor.py` | Executor principal, utilisé par orchestrateur + multi-broker |
| `backend/engine/execution/bitget_v2_executor.py` | Executor V2 actif, utilisé par `multi_broker_router.py` |
| `backend/market/bitget_market.py` | Market data Bitget, utilisé par `multi_broker_engine.py` |

Ces 4 fichiers sont des **intégrations broker légitimes** (Bitget est un exchange réel,
secondaire et désactivé mais fonctionnel en multi-broker). Ils NE sont PAS de la dette.

### 5. Autres nettoyages

| Zone | Action |
|------|--------|
| `BrokerName` enum (TS) | Supprimé entrée `BITGET` |
| DB defaults | 5 colonnes `default="bitget"` → `default="kraken"` |
| `package.json` | `"name": "bitget-smc-auto-trader"` → `"kraken-smc-auto-trader"` |
| `backend/package.json` | keyword `"bitget"` → `"kraken"` |
| `.env.example` | Supprimé sections legacy Bitget commentées |
| `backend/engine/execution/__init__.py` | Corrigé import cassé vers fichier supprimé |
| `backend/exchanges/__init__.py` | Nettoyé imports Bitget supprimés |
| `backend/execution/__init__.py` | Nettoyé imports Bitget supprimés |
| Commentaires | ~30 commentaires "Bitget" → termes neutres dans fichiers protégés |
| `__pycache__` | 13 fichiers `.pyc` Bitget supprimés |

### 6. Garde-fous anti-régression

**Script CI** : `scripts/check_no_bitget_debt.py`
- Scanne 23 fichiers dans les zones protégées
- Vérifie que 5 fichiers supprimés restent absents
- Vérifie que Tailwind n'a pas de palette `bitget`
- Exceptions documentées pour les types data-contract (`Dashboard.tsx`, `PerformanceAnalyticsPanel.tsx`)

**Test pytest** : `tests/test_no_bitget_debt.py`
- 10 tests de non-régression
- CSS, headers, Tailwind, fichiers supprimés, __init__.py, enum, service, scanner

---

## Zones Legacy Autorisées (Temporairement)

Ces zones contiennent `"bitget"` comme **identifiant de broker** légitime dans
un système multi-broker. Ce n'est PAS de la dette d'identité :

| Zone | Nb fichiers | Justification |
|------|-------------|---------------|
| `backend/routes/` | 6 | Routes API multi-broker (filtres, stats par broker) |
| `backend/services/` | 7 | Services multi-broker (balance, reconciliation) |
| `backend/risk/` | 4 | Specs contrats/marges par broker |
| `backend/models/` | 5 | Modèles DB avec champ `broker` |
| `smc_futures/engine/` | 6 | Moteur d'exécution multi-broker |
| `tests/` | ~25 | Tests des fonctionnalités multi-broker |
| `.env`, `.env.backup` | 2 | Credentials Bitget (désactivés) |

**Roadmap de suppression** : Si le multi-broker Bitget est définitivement
abandonné, supprimer les 4 fichiers executor conservés et toutes les
références `"bitget"` dans le backend. Estimation : ~500 lignes à nettoyer.

---

## Conventions Officielles

| Sujet | Convention |
|-------|-----------|
| Broker primaire | Kraken |
| Service frontend | `KrakenService` |
| Headers API | `X-API-KEY`, `X-API-SECRET`, `X-API-PASSPHRASE` |
| CSS theme | `kraken-cyan`, `kraken-dark`, `kraken-card`, etc. |
| Package name | `kraken-smc-auto-trader` |
| DB default broker | `"kraken"` |
| Nouveaux fichiers | **INTERDIT** de créer `bitget_*.py` ou d'ajouter `bitget-*` CSS |

---

## Verdict Final

| Catégorie | Status |
|-----------|--------|
| Identité visuelle (CSS) | **ÉRADIQUÉ** |
| Headers HTTP | **MIGRÉ** (compatibilité transitoire active) |
| Routes API | **RENOMMÉ** (compatibilité transitoire active) |
| Code mort | **SUPPRIMÉ** (5 fichiers) |
| Frontend logique | **NETTOYÉ** |
| Documentation | **À JOUR** |
| Garde-fous CI | **ACTIFS** |
| Multi-broker backend | **TOLÉRÉ** (identifiant broker légitime) |
