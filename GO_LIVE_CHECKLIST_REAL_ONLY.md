# 🚀 Checklist de Passage en Live — Mode REAL ONLY

## Phase 1 : Pré-vol — Configuration

### ✅ Étape 1 : Variables d'environnement
- [ ] Copier `safe-live.env` vers `.env`
- [ ] Renseigner `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_PASSPHRASE`
- [ ] Renseigner `KRAKEN_API_KEY`, `KRAKEN_API_SECRET` (si multi-broker)
- [ ] Configurer `BROKER=bitget` ou `BROKER=kraken` ou `BROKER=multi`
- [ ] Vérifier `ENABLE_REAL_TRADING=false` pour commencer en observation

### ✅ Étape 2 : Validation de la configuration
```bash
python -c "from config.smc_config import SMC_CONFIG; print(SMC_CONFIG)"
```
- [ ] Aucune erreur affichée
- [ ] Les clés API sont masquées dans la sortie

### ✅ Étape 3 : Test de connexion
```bash
python test_connection.ps1
```
Ou manuellement :
```bash
python -c "
from backend.broker_router import BrokerRouter
br = BrokerRouter()
print(br.test_connection())
"
```
- [ ] Réponse `{'success': True}` reçue

---

## Phase 2 : Pré-vol — Vérification du code

### ✅ Étape 4 : Tests unitaires
```bash
python -m pytest tests/ -x --tb=short
```
- [ ] 857+ tests passent
- [ ] 0 échecs

### ✅ Étape 5 : Tests de safeguards
```bash
python -m pytest tests/test_phase8*.py tests/test_phase9*.py tests/test_phase10*.py tests/test_phase11*.py -x
```
- [ ] 422 tests P8-P11C passent
- [ ] Kill-switch fonctionne
- [ ] Limites de risque respectées

### ✅ Étape 6 : Vérification frontend
```bash
npm run dev
```
- [ ] Dashboard accessible sur `http://localhost:5173`
- [ ] Onglet GOVERNANCE visible
- [ ] Pas d'erreurs TypeScript

---

## Phase 3 : Mode Observation (Dry Run)

### ✅ Étape 7 : Démarrage en observation
```bash
# .env
ENABLE_REAL_TRADING=false
AUTO_EXEC_ENABLED=true
```
```bash
python run.py
```
- [ ] Le bot démarre sans erreur
- [ ] Les signaux SMC sont détectés et loggés
- [ ] Aucun ordre réel n'est envoyé
- [ ] Les logs affichent `[DRY-RUN]` devant chaque signal

### ✅ Étape 8 : Vérification des logs
```bash
# Vérifier les logs temps réel
tail -f logs/smc_bot.log
```
- [ ] Signaux CHoCH/BOS détectés
- [ ] Sessions institutionnelles correctes (Londres, NY)
- [ ] Aucune erreur API

---

## Phase 4 : Passage en Live

### ⚠️ Étape 9 : Activation du trading réel
```bash
# .env
ENABLE_REAL_TRADING=true
AUTO_EXEC_ENABLED=true
MAX_DAILY_LOSS_PCT=3.0
MAX_TRADES_PER_DAY=5
TRADE_COOLDOWN_SEC=300
```
- [ ] Relancer le bot : `python run.py`
- [ ] Vérifier dans les logs : `Trading réel ACTIVÉ`

### ⚠️ Étape 10 : Test kill-switch
```bash
curl -X POST http://localhost:8000/api/kill-switch \
  -H "Content-Type: application/json" \
  -d '{"action": "activate"}'
```
- [ ] Réponse `200 OK` reçue
- [ ] Aucun nouvel ordre n'est accepté
- [ ] Désactiver : `{"action": "deactivate"}`

### ✅ Étape 11 : Monitoring continu
- [ ] Vérifier le PnL sur le dashboard
- [ ] Surveiller la perte journalière (ne doit pas dépasser `MAX_DAILY_LOSS_PCT`)
- [ ] Vérifier le nombre de trades (ne doit pas dépasser `MAX_TRADES_PER_DAY`)

---

## 🆘 Procédures d'urgence

### Kill-Switch immédiat
```bash
# Via API
curl -X POST http://localhost:8000/api/kill-switch -d '{"action": "activate"}'

# Via le dashboard
# Cliquer sur le bouton rouge "KILL SWITCH" dans l'onglet Governance
```

### Arrêt complet
```bash
# Windows
.\stop_all.bat

# Linux/systemd
sudo systemctl stop smc-bot
```

### Diagnostic rapide
```bash
# Vérifier l'état du bot
curl http://localhost:8000/api/health

# Vérifier les positions ouvertes
curl http://localhost:8000/api/positions

# Vérifier le P&L journalier
curl http://localhost:8000/api/daily-pnl
```

---

## 🐛 Débogage courant

| Symptôme | Cause probable | Solution |
|----------|---------------|----------|
| `APIError: invalid key` | Clé API invalide ou expirée | Régénérer les clés sur Bitget/Kraken |
| `Kill-switch actif` | Perte journalière dépassée | Vérifier PnL, réinitialiser le lendemain |
| `Cooldown actif` | Trade trop récent | Attendre `TRADE_COOLDOWN_SEC` secondes |
| `Pas de signal` | Hors session institutionnelle | Vérifier `TRADING_SESSIONS` dans le config |
| `Levier refusé` | Levier > 10x demandé | Réduire le levier dans la config |
