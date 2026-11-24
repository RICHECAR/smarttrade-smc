# 📘 Guide d'Implémentation — Mode REAL ONLY

## Vue d'ensemble

Ce guide détaille les modifications de code nécessaires pour passer du mode simulation au mode trading réel. Chaque section est autonome et peut être appliquée indépendamment.

---

## Section A : Configuration centralisée

### Fichier : `config/smc_config.py`

La configuration centralisée charge toutes les variables depuis `.env` et les expose via le dictionnaire `SMC_CONFIG`.

```python
# Exemple d'utilisation
from config.smc_config import SMC_CONFIG

api_key = SMC_CONFIG["BITGET_API_KEY"]
max_loss = SMC_CONFIG["MAX_DAILY_LOSS_PCT"]
```

### Points clés
- Toutes les variables ont des **valeurs par défaut sûres**
- Les booléens sont parsés avec `str.lower() in ('true', '1', 'yes')`
- Les valeurs numériques sont castées avec gestion d'erreur
- `ENABLE_REAL_TRADING` est `false` par défaut (sécurité)

---

## Section B : Méthode `execute()` — Order Manager

### Fichier : `backend/order_manager.py`

La méthode `execute()` est le point d'entrée pour passer un ordre :

```python
async def execute(self, signal: dict) -> dict:
    """
    Exécute un signal de trading.

    Étapes :
    1. Validation pré-vol (risk_manager.validate_precheck)
    2. Calcul de la taille de position (sizing)
    3. Envoi de l'ordre via BrokerRouter
    4. Enregistrement du trade dans l'historique
    5. Mise à jour du P&L journalier
    """
```

### Validation pré-vol
Avant chaque ordre, 8 vérifications sont effectuées :
1. Kill-switch inactif
2. Trading réel activé (`ENABLE_REAL_TRADING=true`)
3. Session institutionnelle active
4. Perte journalière < seuil max
5. Nombre de trades < seuil max
6. Cooldown respecté
7. Pas de position existante sur le symbole
8. Levier ≤ 10x

---

## Section C : Auto-exécution des signaux

### Fichier : `backend/smc_engine.py` (hook)

Le moteur SMC analyse les données de marché et génère des signaux. L'auto-exécution connecte ces signaux à l'`order_manager` :

```python
# Dans le moteur SMC, après détection d'un signal
if SMC_CONFIG.get("AUTO_EXEC_ENABLED"):
    result = await order_manager.execute(signal)
    logger.info(f"Auto-exec: {result}")
```

### Contrôle
- `AUTO_EXEC_ENABLED=true` dans `.env` pour activer
- `AUTO_EXEC_ENABLED=false` pour logger sans exécuter (mode observation)

---

## Section D : Logging HTTP complet

### Fonctionnement
Chaque appel API REST est loggé avec :
- URL de la requête
- Méthode HTTP (GET, POST, DELETE)
- Code de statut de la réponse
- Durée de l'appel en millisecondes
- Corps de la réponse en cas d'erreur

### Configuration des logs
```python
# Rotation automatique des logs
# Fichier : logs/smc_bot.log
# Taille max : 10 MB par fichier
# Conservation : 5 fichiers de rotation
```

### Niveaux de log recommandés
| Environnement | Niveau | Variable |
|---------------|--------|----------|
| Développement | DEBUG | `LOG_LEVEL=DEBUG` |
| Production | INFO | `LOG_LEVEL=INFO` |
| Débogage prod | WARNING | `LOG_LEVEL=WARNING` |

---

## Section E : Vérification API au démarrage

### Fonctionnement
Au démarrage du bot, le `BrokerRouter` vérifie la connexion à chaque broker configuré :

```python
async def startup_check(self):
    """Vérifie la connexion API au démarrage."""
    for broker_name, client in self.clients.items():
        try:
            result = await client.get_account_info()
            logger.info(f"✅ {broker_name} connecté - Solde: {result['balance']}")
        except Exception as e:
            logger.error(f"❌ {broker_name} échec connexion: {e}")
            raise SystemExit(f"Impossible de se connecter à {broker_name}")
```

### Vérifications effectuées
1. **Authentification** : Les clés API sont valides
2. **Permissions** : Le compte a les droits de trading futures
3. **Solde** : Le compte a un solde suffisant
4. **Connectivité** : Le serveur API répond dans les 5 secondes

---

## 🔄 Ordre d'application recommandé

1. **Section A** — Configurer les variables d'environnement
2. **Section E** — Vérifier la connexion API
3. **Section B** — Tester un ordre en dry-run
4. **Section C** — Activer l'auto-exécution
5. **Section D** — Vérifier les logs

---

## ⚠️ Points d'attention

- **Ne jamais** committer le fichier `.env` dans git
- **Toujours** démarrer en mode observation (`ENABLE_REAL_TRADING=false`)
- **Tester** le kill-switch avant d'activer le trading réel
- **Surveiller** les logs pendant les premières heures de trading
- **Limiter** le levier à 5x maximum pendant la phase initiale
