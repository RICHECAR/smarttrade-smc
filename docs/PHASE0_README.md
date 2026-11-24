# Phase 0: Institutional Security Setup

Ce document présente l'implémentation de la **Phase 0** de la refonte institutionnelle du bot de trading SMC. L'objectif est de sécuriser totalement le capital avec un kill-switch asynchrone indépendant (OOB) et un renforcement des contraintes de risque.

## Livrables Terminés (Phase 0 & 0.5)

1. **`scripts/emergency_kill.py` (OOB Kill-Switch V2 - STRICT)**
   - Réécrit de zéro avec `aiohttp` et `asyncio` pour envoyer les instructions de Cancel / Flatten en **parallèle** sur Kraken et Bitget.
   - Intègre un "**Read-Back**" : Il ping `orders-pending`/`openpositions` après le cancel/close. S'il n'est pas "FLAT", il exécute un `RETRY` (max 1), puis quitte.
   - Exit codes stricts : `0` = FLAT vérifié, `2` = PARTIAL FAIL, `3` = UNKNOWN ERROR.
2. **`smc_config.py` & `trade_validator.py` (Harden Risk Limits)**
   - Hardblock et supression du paramètre anti-quant `AUTO_RISK_UPSCALE`. Un signal sous la `minQty` ou le `minNotional` est désormais purement annulé, peu importe la taille du compte.
   - Contrainte extrême de data refresh : `STALE_THRESHOLD_MS` passe de 500ms à **250ms**.
3. **`smc_futures/engine/execution/order_manager.py` (Strict Mirror)**
   - Tout *exécution multi-broker* échouée partiellement déclenche un rollback immédiat sur le broker fonctionnel et lock le Bot entier (Global Kill Switch).
4. **`scripts/trade_reconciler_strict.py` (Reconciliation Indépendante)**
   - Daemon asynchrone fonctionnant hors FastAPI. Il compare infiniment la DB SQLite vs les states exchanges Kraken/Bitget.
   - Si le nombre de positions ouvertes diverge -> Active le `kill_switch` dans la DB.
5. **`smc_futures/engine/execution/base_adapter.py` (Base Exchange)**
   - Prototype de la `BaseExchangeAdapter`, `KrakenAdapter` et `BitgetAdapter`, normalisant le nettoyage de marché (`cancel_all_orders`, `close_all_positions`, `healthcheck`, `get_open_positions`).

---

## 🚀 Lancement & Test du Kill-Switch

### Lancement
Avant de l'appeler, assurez-vous d'avoir configuré votre `.env` avec ces clés :
```env
BITGET_API_KEY="..."
BITGET_SECRET_KEY="..."
BITGET_PASSPHRASE="..."

KRAKEN_API_KEY="..."
KRAKEN_API_SECRET="..."
```

**Commande d'exécution** :
```bash
# S'assure de l'exécution dans le venv Python asynchrone
python scripts/emergency_kill.py
```

### Méthode de test reproductible (OOB Kill-Switch)
1. **Ouvrez un trade test (petite taille) sur Bitget Futures ou Kraken.** Placez quelques Limit Orders.
2. **Testez le RETRY** : Saturez exprès le réseau ou changez vos token pendant l'exécution.
3. **Lancez le script OOB** : `python scripts/emergency_kill.py`. Vous devriez voir les passes :
   `[PASS 1] Executing Cancel + Close` -> `[VERIFY 1] Checking exchange states` -> `✅ COMPLETED: Portfolio confirmed FLAT`
4. L'exit code `echo $?` (Linux) ou `echo %errorlevel%` (Cmd) doit renvoyer `0`.

### Méthode de test reproductible (Reconciliation & Strict Mirror)
1. **Strict Mirror** : Modifiez temporairement une clé API de Kraken. Relancez le bot et l'exécution d'un trade Bitget+Kraken. L'OMS va échouer sur Kraken, logger une `⚠️ STRICT MIRROR FAULT ⚠️`, fermer de force la position tout juste ouverte sur Bitget, et s'auto-verrouiller (Global Kill Switch `True`).
2. **Reconciler Daemon** : Lancez le bot, ouvrez une position test en DB. Allez sur Bitget, fermez manuellement la position depuis le web. Lancez: `python scripts/trade_reconciler_strict.py`. Le daemon détectera `Exchange Pos: 0 | DB Pos: 1` et activera de force l'arrêt `RECONCILIATION_MISMATCH`.

---

## 🔒 OpSec (Sécurité Opérationnelle)

En mode institutionnel, le niveau d'exigence vis-à-vis des API keys requiert un standard strict :

1. **Permissions Minimales (API Keys)**
   - Les clés Kraken & Bitget de production ne doivent **JAMAIS** avoir les droits de "Retrait/Withdrawal".
   - Restreindre l'accès en validant uniquement : "Futures Trading", "Read Positions", "Orders". (Optionnellement spot si le bot hedge un jour).
2. **IP Allowlist**
   - Ajoutez de force l'IP (statique) de votre serveur de déploiement (AWS/Azure/DigitalOcean) directement sur Bitget et Kraken lors de la création de la clé. Ainsi, même en cas de vol du `.env`, la payload sera refusée hors IP.
3. **Isolation de l'ENV**
   - Stockez vos credentials sans utiliser de fichier `.env` sur le serveur si possible (utiliser un secret manager style *AWS Secrets*, *HashiCorp Vault*, ou passer via variables d'environnement de l'OS).
   - Les clés pour l'execution et le kill-switch script pourraient idéalement être dissociées si vos exchanges permettent une ségrégation de clés (sous-comptes).
