# 📁 Index Complet des Fichiers

## Vue d'ensemble du workspace

Ce document fournit une cartographie complète du projet **bitget-smc-auto-trader** avec le rôle de chaque dossier et fichier clé.

---

## 🗂️ Structure des dossiers

```
bitget-smc-auto-trader/
├── backend/          # Code serveur Python (API, clients broker, moteur)
├── config/           # Configuration centralisée
├── components/       # Composants React frontend
├── pages/            # Pages React (Dashboard, Governance, etc.)
├── hooks/            # Hooks React personnalisés
├── services/         # Services frontend (API calls)
├── smc_futures/      # Moteur SMC (Smart Money Concepts)
├── tests/            # Tests unitaires et d'intégration
├── docs/             # Documentation technique détaillée
├── tools/            # Scripts utilitaires
├── scripts/          # Scripts de déploiement et maintenance
├── observability/    # Monitoring, métriques, alertes
├── reports/          # Templates de rapports
├── market_data/      # Données de marché (cache)
├── data/             # Données persistantes
├── systemd/          # Fichiers de service Linux
└── logs/             # Logs d'exécution (généré automatiquement)
```

---

## 📂 backend/ — Serveur Python

| Fichier | Rôle |
|---------|------|
| `order_manager.py` | Gestion des ordres réels (execute, cancel, modify) |
| `risk_manager.py` | Gestion du risque (kill-switch, limites journalières) |
| `broker_router.py` | Routeur multi-broker unifié |
| `bitget_v2_mix_client.py` | Client REST Bitget v2 Mix API |
| `kraken_futures_client.py` | Client REST Kraken Futures |
| `kraken_futures_rest.py` | Wrapper REST Kraken |
| `kraken_rest_client.py` | Client REST Kraken générique |
| `api_smc_engine.py` | Serveur FastAPI principal |
| `routes/governance.py` | Endpoints API de gouvernance |
| `shadow_engine.py` | Moteur shadow pour comparaison |
| `ws_server.py` | Serveur WebSocket temps réel |

---

## 📂 config/ — Configuration

| Fichier | Rôle |
|---------|------|
| `smc_config.py` | Configuration centralisée depuis `.env` |
| `*.py` | Paramètres spécifiques par module |

---

## 📂 smc_futures/ — Moteur SMC

| Fichier | Rôle |
|---------|------|
| `smc_engine.py` | Moteur d'analyse SMC principal |
| `mtf_analyzer.py` | Analyse multi-timeframe |
| `structure.py` | Détection de structure de marché (CHoCH, BOS) |
| `order_blocks.py` | Détection des blocs d'ordres |
| `fair_value_gaps.py` | Détection des Fair Value Gaps (FVG) |
| `liquidity.py` | Cartographie des zones de liquidité |
| `bias_engine.py` | Moteur de biais directionnel |

---

## 📂 tests/ — Tests

| Pattern | Contenu |
|---------|---------|
| `test_phase8*.py` | Tests kill-switch et circuit breaker |
| `test_phase9*.py` | Tests gestion du risque avancée |
| `test_phase10*.py` | Tests d'exécution et ordre manager |
| `test_phase11*.py` | Tests de gouvernance et audit |
| `test_order_manager*.py` | Tests unitaires order manager |
| `test_smc_*.py` | Tests du moteur SMC |

---

## 📂 pages/ — Pages React

| Fichier | Rôle |
|---------|------|
| `Dashboard.tsx` | Dashboard principal |
| `SystemGovernance.tsx` | Page de gouvernance système |
| `SmcAnalysis.tsx` | Visualisation de l'analyse SMC |
| `RiskManagement.tsx` | Interface de gestion du risque |

---

## 📂 docs/ — Documentation

| Fichier | Contenu |
|---------|---------|
| `API.md` | Documentation de l'API REST |
| `ARCHITECTURE.md` | Architecture technique |
| `HANDOFF_PACKAGE.md` | Package de transfert |
| `RUNBOOK_OPSEC.md` | Runbook opérationnel |
| `KRAKEN_FUTURES_SETUP.md` | Guide de configuration Kraken |
| `STARTUP_AND_API_GUIDE.md` | Guide de démarrage et API |

---

## 📄 Fichiers racine importants

| Fichier | Rôle |
|---------|------|
| `run.py` | Point d'entrée principal du bot |
| `safe-live.env` | Template de variables d'environnement |
| `.env` | Variables d'environnement (NON versionné) |
| `package.json` | Dépendances Node.js (frontend) |
| `vite.config.ts` | Configuration Vite (frontend) |
| `App.tsx` | Composant React racine |
| `docker-compose.prod.yml` | Configuration Docker production |

---

## 📖 Ordre de lecture recommandé

Pour comprendre le projet rapidement, lire dans cet ordre :

1. **`README_REAL_ONLY_START_HERE.md`** — Vue d'ensemble et point d'entrée
2. **`DELIVERY_SUMMARY.md`** — Ce qui a été livré
3. **`ENV_VARS.md`** — Variables de configuration
4. **`IMPLEMENTATION_GUIDE_REAL_ONLY.md`** — Détails techniques
5. **`THREE_CRITICAL_CHANGES.md`** — Actions immédiates
6. **`GO_LIVE_CHECKLIST_REAL_ONLY.md`** — Checklist de passage en live
7. **`SECURITY_AUDIT_REPORT.md`** — Considérations de sécurité

---

## ✅ Étapes de vérification

### 1. Vérifier l'installation
```bash
pip install -r requirements.txt
npm install
```

### 2. Lancer les tests
```bash
python -m pytest tests/ -x --tb=short
```

### 3. Vérifier la configuration
```bash
python verify_config.py
```

### 4. Démarrer le bot
```bash
python run.py
```

### 5. Démarrer le frontend
```bash
npm run dev
```
