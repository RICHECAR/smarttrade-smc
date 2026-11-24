# 🚀 Checklist de Déploiement — Bot de Trading Multi-Symboles

## ✅ Étapes de vérification (Avant le passage en live)

### 1. **Vérification de la configuration**

```bash
# Vérifier que SMC_CONFIG charge tous les symboles
python -c "from smc_futures.smc_config import SMC_CONFIG; print('SYMBOLES:', SMC_CONFIG['SYMBOLS'])"
# Résultat attendu : SYMBOLES: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT']
```

Si des symboles manquent, vérifiez `.env` :
```
BOT_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT
```

### 2. **Variables d'environnement**

Requises dans `.env` pour que l'auto-trading fonctionne :

```env
# ⚠️ CRITIQUE
TRADING_MODE=live                    # PAS paper
AUTO_EXECUTE=true                    # Active l'exécution auto (avec protections)
BOT_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT

# Identifiants API Bitget
BITGET_API_KEY=<votre_cle>
BITGET_API_SECRET=<votre_secret>
BITGET_PASSPHRASE=<votre_passphrase>

# Protections (commencez prudemment !)
SMC_MAX_TRADES_DAY=24                # Max trades/jour sur tous les symboles
SMC_TRADE_COOLDOWN=120               # Min secondes entre les trades
```

### 3. **Vérification du démarrage**

Au lancement du serveur FastAPI :

```bash
python -m uvicorn smc_futures.api_smc_engine:app --port 8000 --reload
```

Surveillez les logs pour :

```
✅ [BOOT] Orchestrator initialized for BTCUSDT
✅ [BOOT] Orchestrator initialized for ETHUSDT
✅ [BOOT] Orchestrator initialized for SOLUSDT
✅ [BOOT] Orchestrator initialized for XRPUSDT
🚀 Starting Background Strategy Loop (V3 — Identical Paths)...
```

**Si vous voyez uniquement BTC initialisé**, vérifiez :
- `.env` BOT_SYMBOLS contient les 4 symboles
- Pas d'erreurs de syntaxe dans orchestrator.py (lancez : `python -m py_compile smc_futures/orchestrator.py`)

### 4. **Vérification de santé de l'API**

```bash
curl http://localhost:8000/api/health
# Attendu : {"status": "ok", "auto_execute_mode": "true"}
```

### 5. **Surveillance de la collecte de données**

Dans les logs, vous devez voir pour chaque symbole :
```
INFO:smc_futures.engine.execution.bitget_v2_client:[TICKER_PARSED] BTCUSDT price=67143.2000 ...
INFO:smc_futures.engine.execution.bitget_v2_client:[TICKER_PARSED] ETHUSDT price=4567.8900 ...
INFO:smc_futures.engine.execution.bitget_v2_client:[TICKER_PARSED] SOLUSDT price=82.2280 ...
INFO:smc_futures.engine.execution.bitget_v2_client:[TICKER_PARSED] XRPUSDT price=2.5600 ...
```

Si des symboles n'apparaissent pas :
1. **Vérifiez l'initialisation de l'orchestrateur** dans les logs
2. **Vérifiez BOT_SYMBOLS** dans `.env`
3. **Vérifiez que .env est chargé** : `python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.getenv('BOT_SYMBOLS'))"`

### 6. **Tests unitaires (Validation de sécurité)**

```bash
pytest tests/test_auto_exec_hook.py -v
pytest tests/test_live_safeguards.py -v
pytest tests/test_shadow_zero_network.py -v
```

Attendu : **24/25 PASS**

### 7. **Protections d'exécution actives**

Vérifiez que les 8 protections fonctionnent :

```bash
curl http://localhost:8000/api/bot/stats
# Attendu : {"daily_trades": 0, "kill_switch": false, "trades_by_symbol": {...}}
```

### 8. **Test du Kill-Switch (Arrêt d'urgence)**

```bash
curl -X POST http://localhost:8000/api/bot/kill-switch \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "reason": "Test"}'

# Surveillez les logs pour :
# ERROR:[ORDER_MGR] [KILL_SWITCH] ENABLED | reason=Test
```

Tous les ordres doivent être bloqués immédiatement.

---

## 🔴 Dépannage

### **Seules les données BTC apparaissent, pas ETH/SOL/XRP**

```
Correction : Mettre à jour .env avec les 4 symboles et redémarrer FastAPI
BOT_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT
```

### **Aucun signal n'est généré**

```
Causes possibles :
1. La boucle de stratégie tourne mais aucun setup trouvé
   → Vérifiez les logs pour [HTF] [MTF] [LTF]
2. Les filtres sont trop stricts (ex: filtre de session)
   → Réduisez MIN_RR, activez ALLOW_COUNTERTREND=true
3. Seules les données BTCUSDT existent
   → Voir la section "Seules les données BTC" ci-dessus
```

### **Les ordres ne s'exécutent pas malgré AUTO_EXECUTE=true**

```
Vérifiez :
1. État du kill-switch : curl http://localhost:8000/api/bot/stats
2. Rejets pré-vol : tail -f logs (cherchez [REJECT])
3. Erreurs API Bitget : grep "HTTP_FAIL\|ERROR" logs
4. Position déjà ouverte : Vérifiez l'interface web Bitget
```

### **TRADING_MODE=live mais pas d'ordres ?**

```
Causes probables :
1. AUTO_EXECUTE=false ou non défini
   → Correction : AUTO_EXECUTE=true dans .env
2. BITGET_API_KEY/SECRET non définis
   → Vérifiez dans .env (ne doit pas être "your_key")
3. Solde insuffisant sur le compte
   → Vérifiez le solde USDT sur l'interface Bitget
```

---

## 📊 Phases de déploiement en production

### **Phase 1 : Tests unitaires** ✅
```bash
pytest tests/test_*.py -v
# Attendu : 24+ PASS
```

### **Phase 2 : Mode Shadow 24h**
```env
TRADING_MODE=live
AUTO_EXECUTE=shadow          # Mode DRY_RUN
BOT_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT
```
Surveillez `[DRY_RUN] WOULD_SEND` dans les logs (0 ordres réels).

### **Phase 3 : Live minimal** (après Phase 2 ✅)
```env
TRADING_MODE=live
AUTO_EXECUTE=true
SMC_MAX_TRADES_DAY=1         # Commencer avec 1 trade/jour
BOT_RISK_PCT=0.01            # 1% par trade
```
Surveillez le premier cycle de trade de bout en bout.

### **Phase 4 : Montée en charge progressive** (après Phase 3 ✅)
```
Jour 5-7 :   SMC_MAX_TRADES_DAY=2, BOT_RISK_PCT=0.01
Jour 8+ :    SMC_MAX_TRADES_DAY=5, BOT_RISK_PCT=0.02
```

---

## 🎯 Critères de succès

- ✅ Les 4 symboles (BTC, ETH, SOL, XRP) récupèrent les données
- ✅ La boucle de stratégie tourne sans plantage
- ✅ Les vérifications pré-vol bloquent les trades dangereux
- ✅ Le kill-switch bloque tous les ordres immédiatement
- ✅ Réinitialisation quotidienne à minuit UTC
- ✅ Tests unitaires passent

---

## 🆘 Procédures d'urgence

### **Arrêt immédiat (Kill-Switch)**
```bash
curl -X POST http://localhost:8000/api/bot/kill-switch \
  -d '{"enabled": true, "reason": "URGENCE"}' \
  -H "Content-Type: application/json"
# → Tous les ordres bloqués immédiatement
```

### **Retour au mode Shadow**
1. Arrêtez FastAPI (`Ctrl+C`)
2. Éditez `.env` : `AUTO_EXECUTE=shadow`
3. Redémarrez : `python -m uvicorn smc_futures.api_smc_engine:app --port 8000`
→ Prochains signaux = simulés (0 risque)

### **Désactivation complète**
```env
AUTO_EXECUTE=false
# → Signaux uniquement, pas d'exécution
```

---

## 📞 Support

Vérifiez les logs pour :
- `[REJECT]` — Protections pré-vol
- `[ORDER]` — Passage d'ordres
- `[EXEC]` — Résultat d'exécution
- `[HTTP_FAIL]` — Erreurs API
- `[CLOCK_SKEW]` — Problèmes de synchronisation temporelle

Toutes les opérations sont loggées avec horodatage + contexte du symbole.
