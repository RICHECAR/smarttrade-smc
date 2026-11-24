# 🏁 Par où commencer — Mode REAL ONLY

## Bienvenue

Ce document est votre **point d'entrée** pour déployer le bot SMC en mode trading réel. Lisez-le en premier.

---

## 📋 Livrables inclus

| # | Document | Contenu |
|---|----------|---------|
| 1 | `README_REAL_ONLY_START_HERE.md` | Ce fichier (point d'entrée) |
| 2 | `DELIVERY_SUMMARY.md` | Résumé complet des livrables |
| 3 | `IMPLEMENTATION_GUIDE_REAL_ONLY.md` | Guide technique détaillé (Sections A-E) |
| 4 | `GO_LIVE_CHECKLIST_REAL_ONLY.md` | Checklist phase par phase |
| 5 | `ENV_VARS.md` | Documentation des variables d'environnement |
| 6 | `DEPLOYMENT_CHECKLIST.md` | Checklist de déploiement |
| 7 | `SECURITY_AUDIT_REPORT.md` | Rapport d'audit de sécurité |
| 8 | `THREE_CRITICAL_CHANGES.md` | 3 changements critiques à appliquer |

---

## 🗂️ Fichiers clés du code

| Fichier | Rôle |
|---------|------|
| `config/smc_config.py` | Configuration centralisée |
| `backend/order_manager.py` | Gestion des ordres réels |
| `backend/risk_manager.py` | Gestion du risque (kill-switch, limites) |
| `backend/broker_router.py` | Routeur multi-broker |
| `backend/routes/governance.py` | API de gouvernance |
| `safe-live.env` | Template `.env` sécurisé |
| `run.py` | Point d'entrée du bot |

---

## ⏱️ Timeline recommandée

### Jour 1 : Préparation
1. Lire `DELIVERY_SUMMARY.md` pour comprendre l'architecture
2. Configurer `.env` à partir de `safe-live.env`
3. Lancer les tests : `python -m pytest tests/ -x`
4. Vérifier la connexion API

### Jour 2 : Observation
1. Démarrer le bot en mode observation (`ENABLE_REAL_TRADING=false`)
2. Surveiller les signaux SMC dans les logs
3. Vérifier que les sessions institutionnelles sont correctes
4. Tester le kill-switch

### Jour 3 : Trading réel
1. Activer `ENABLE_REAL_TRADING=true`
2. Commencer avec un levier faible (2-3x)
3. Surveiller en continu pendant les premières heures
4. Ajuster les paramètres si nécessaire

---

## 📊 Logs attendus

### Démarrage normal
```
INFO  - Configuration chargée depuis .env
INFO  - ✅ Bitget connecté - Solde: XXX USDT
INFO  - Risk Manager initialisé (max perte: 3.0%, max trades/jour: 5)
INFO  - Bot SMC démarré en mode REAL
INFO  - Session active: London (08:00-12:00 UTC)
```

### Signal détecté
```
INFO  - Signal CHoCH détecté sur BTCUSDT (H1)
INFO  - Validation pré-vol: 8/8 checks OK
INFO  - Ordre LONG envoyé: BTCUSDT, taille: 0.01 BTC, levier: 5x
INFO  - Ordre confirmé: ID=123456789
```

### Kill-switch activé
```
WARNING - Kill-switch ACTIVÉ
WARNING - Raison: Perte journalière -3.2% > seuil -3.0%
WARNING - Tous les nouveaux ordres bloqués
```

---

## ❓ FAQ

### Q: Puis-je utiliser le bot en paper trading ?
**R:** Oui, avec `ENABLE_REAL_TRADING=false`. Les signaux sont analysés et loggés mais aucun ordre réel n'est envoyé.

### Q: Comment changer de broker ?
**R:** Modifier `BROKER=kraken` ou `BROKER=bitget` dans `.env`. Pour utiliser les deux simultanément : `BROKER=multi`.

### Q: Que se passe-t-il si la connexion Internet est coupée ?
**R:** Le bot détecte la déconnexion et active automatiquement le kill-switch. Les positions ouvertes conservent leur stop-loss sur le serveur du broker.

### Q: Comment réinitialiser le compteur de perte journalière ?
**R:** Le compteur se réinitialise automatiquement à minuit UTC. Pour une réinitialisation manuelle :
```bash
curl -X POST http://localhost:8000/api/reset-daily-pnl
```

### Q: Le bot fonctionne-t-il 24h/24 ?
**R:** Le bot tourne en continu mais ne trade que pendant les sessions institutionnelles configurées (par défaut : Londres et New York).

### Q: Comment ajouter un nouveau symbole ?
**R:** Ajouter le symbole dans `SYMBOLS` du fichier `.env` :
```
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
```

### Q: Où sont les logs ?
**R:** Dans le dossier `logs/`. Le fichier principal est `logs/smc_bot.log` avec rotation automatique.
