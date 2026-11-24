# ⚡ 3 Changements Critiques à Appliquer

## Résumé

Ces 3 modifications sont **indispensables** pour activer le mode trading réel. Chacune peut être appliquée par simple copier-coller.

---

## Changement 1 : Activer le trading réel dans `.env`

### Fichier : `.env`

```env
# AVANT (mode observation)
ENABLE_REAL_TRADING=false
AUTO_EXEC_ENABLED=false

# APRÈS (mode réel)
ENABLE_REAL_TRADING=true
AUTO_EXEC_ENABLED=true
```

### Pourquoi ?
- `ENABLE_REAL_TRADING=true` permet l'envoi d'ordres réels au broker
- `AUTO_EXEC_ENABLED=true` connecte les signaux SMC à l'exécution automatique
- Sans ces deux variables à `true`, le bot fonctionne en mode observation uniquement

---

## Changement 2 : Configurer les limites de risque

### Fichier : `.env`

```env
# Limites de risque recommandées pour le démarrage
MAX_DAILY_LOSS_PCT=3.0
MAX_TRADES_PER_DAY=5
TRADE_COOLDOWN_SEC=300
MAX_LEVERAGE=5
RISK_PER_TRADE_PCT=1.0
```

### Pourquoi ?
- `MAX_DAILY_LOSS_PCT=3.0` → Kill-switch automatique si perte > 3% du capital
- `MAX_TRADES_PER_DAY=5` → Maximum 5 trades par jour (évite le over-trading)
- `TRADE_COOLDOWN_SEC=300` → 5 minutes minimum entre chaque trade
- `MAX_LEVERAGE=5` → Levier plafonné à 5x (prudent pour le démarrage)
- `RISK_PER_TRADE_PCT=1.0` → Risquer max 1% du capital par trade

### Progression recommandée
| Semaine | Max perte/jour | Trades/jour | Levier |
|---------|---------------|-------------|--------|
| 1-2 | 2% | 3 | 3x |
| 3-4 | 3% | 5 | 5x |
| 5+ | 5% | 10 | 10x |

---

## Changement 3 : Renseigner les clés API

### Fichier : `.env`

```env
# Broker principal : Bitget
BROKER=bitget
BITGET_API_KEY=votre_cle_api_ici
BITGET_API_SECRET=votre_secret_api_ici
BITGET_PASSPHRASE=votre_passphrase_ici

# Broker secondaire (optionnel) : Kraken
KRAKEN_API_KEY=votre_cle_kraken_ici
KRAKEN_API_SECRET=votre_secret_kraken_ici
```

### Pourquoi ?
- Le bot a besoin des clés API pour communiquer avec le broker
- Les clés doivent avoir les **permissions futures/trading** activées
- La passphrase Bitget est celle définie lors de la création de la clé API

### Comment obtenir les clés ?

#### Bitget
1. Aller sur [bitget.com](https://www.bitget.com) → Profil → Gestion API
2. Créer une nouvelle clé avec permissions : **Lecture + Trading Futures**
3. Copier : API Key, Secret Key, Passphrase

#### Kraken
1. Aller sur [futures.kraken.com](https://futures.kraken.com) → Paramètres → API
2. Créer une nouvelle clé avec permissions : **Trading**
3. Copier : API Key, API Secret

---

## ✅ Vérification après application

Après avoir appliqué les 3 changements, vérifier :

```bash
# 1. Tester la configuration
python -c "from config.smc_config import SMC_CONFIG; print('OK')"

# 2. Tester la connexion
python -c "
from backend.broker_router import BrokerRouter
import asyncio
br = BrokerRouter()
result = asyncio.run(br.test_connection())
print(result)
"

# 3. Démarrer le bot
python run.py
```

Les logs doivent afficher :
```
INFO  - ✅ Configuration chargée
INFO  - ✅ Broker connecté
INFO  - ✅ Trading réel ACTIVÉ
```
