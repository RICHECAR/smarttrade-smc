# RUNBOOK OPS — Playbooks for Critical Incidents
## Phase 11 — Ops-Grade Operations Manual

---

## 1. KILL SWITCH ACTIVÉ

**Symptôme:** Le trading est bloqué, les ordres sont rejetés.

**Diagnostic:**
```bash
python scripts/go_live_check.py --preflight
```

**Actions:**
1. Vérifier la raison dans les logs: `grep "KILL_SWITCH" logs/smarttrade.log`
2. Vérifier les positions ouvertes sur l'exchange (Kraken UI)
3. Si cause identifiée et résolue:
   ```python
   from core.events.event_bus import get_event_bus
   bus = get_event_bus()
   bus.deactivate_kill_switch()
   ```
4. Si cause inconnue: **NE PAS désactiver**. Investiguer d'abord.

**Escalade:** Si position orpheline détectée → Playbook #3.

---

## 2. SAFE MODE ACTIVÉ

**Symptôme:** Trading bloqué, mais système fonctionnel.

**Diagnostic:**
```bash
python -c "from core.safe_mode import get_safe_mode; sm = get_safe_mode(); print(sm.status)"
```

**Actions:**
1. Lire le trigger et la raison
2. Vérifier consistency checker: `grep "CONSISTENCY" logs/smarttrade.log`
3. Si drift résolu:
   ```python
   from core.safe_mode import get_safe_mode
   get_safe_mode().deactivate("operator_name")
   ```
4. Créer un backup avant de reprendre: `python scripts/go_live_check.py --backup`

---

## 3. POSITION ORPHELINE

**Symptôme:** Position sur l'exchange non trackée localement.

**Actions:**
1. **IMMÉDIAT:** Ne pas paniquer. La position a un SL sur l'exchange.
2. Vérifier sur Kraken/Bitget UI: taille, direction, SL/TP
3. Si SL absent: **placer un SL manuellement IMMÉDIATEMENT**
4. Fermer manuellement si nécessaire
5. Investiguer pourquoi la position n'est pas trackée (DB? event bus?)
6. Après résolution: `python scripts/go_live_check.py --drills`

---

## 4. DATABASE CORROMPUE

**Symptôme:** Erreurs SQLite, data inconsistante.

**Actions:**
1. **STOPPER le bot immédiatement**
2. Lister les backups disponibles:
   ```python
   from core.backup_manager import get_backup_manager
   mgr = get_backup_manager()
   for b in mgr.list_backups():
       print(b["filename"], b["timestamp"], b["integrity_ok"])
   ```
3. Choisir le backup le plus récent avec `integrity_ok=True`
4. Restaurer:
   ```python
   mgr.restore_backup("trades_backup_XXXXXXXX_XXXXXX.db")
   ```
5. Vérifier: `python scripts/go_live_check.py --preflight`
6. Redémarrer le bot

---

## 5. EXCHANGE DÉGRADÉ (Kraken/Bitget)

**Symptôme:** Latence élevée, erreurs API, fills lents.

**Diagnostic:**
```bash
python scripts/broker_healthcheck.py
```

**Actions:**
1. Vérifier https://status.kraken.com ou https://www.bitget.com/status
2. Si dégradé > 5 min: activer le kill switch manuellement
3. Si dégradé > 30 min: vérifier toutes les positions, placer SL manuels
4. Attendre la résolution côté exchange
5. Après résolution: healthcheck → preflight → reprendre

---

## 6. DISQUE PLEIN

**Symptôme:** Erreurs d'écriture WAL, backup échoué.

**Actions:**
1. Vérifier l'espace: `Get-PSDrive C`
2. Nettoyer les logs anciens: `ls logs/*.log* | sort | head -5`
3. Purger les vieux backups si nécessaire
4. Vérifier la rotation WAL: les `.bak` s'accumulent-elles?
5. Après nettoyage: `python scripts/go_live_check.py --preflight`

---

## 7. RESTART STORM (services qui crashent en boucle)

**Symptôme:** Logs remplis de restarts, circuit breaker activé.

**Actions:**
1. Vérifier quel service crashe: `grep "CRASHED" logs/smarttrade.log`
2. Vérifier la mémoire: `Get-Process python | Select-Object WS`
3. Si memory leak: redémarrer le process principal
4. Si bug code: corriger et redéployer
5. Si exchange down: voir Playbook #5

---

## 8. DRIFT PERSISTANT

**Symptôme:** Alertes de drift récurrentes, auto-heal échoue.

**Actions:**
1. Identifier le type de drift: balance? position? ghost?
2. Comparer manuellement: DB vs exchange
3. Si drift < 5%: probablement fees/funding non comptabilisés → reset
4. Si drift > 5%: problème systémique → investiguer
5. Si ghost position: Playbook #3
6. Après résolution: forcer un consistency check

---

## 9. ALERTE CHANNEL DOWN (Telegram/Discord)

**Symptôme:** Aucune alerte reçue malgré des événements.

**Actions:**
1. Vérifier `.env`: `ALERT_TELEGRAM_ENABLED`, `ALERT_TELEGRAM_TOKEN`, `ALERT_TELEGRAM_CHAT_ID`
2. Tester manuellement:
   ```python
   import asyncio
   from notifications.alert_manager import AlertManager, AlertLevel
   mgr = AlertManager.get()
   asyncio.run(mgr.send(AlertLevel.INFO, "Test alert"))
   ```
3. Si erreur réseau: vérifier la connectivité internet
4. Si erreur token: régénérer le bot Telegram

**CRITIQUE:** Les alertes sont un filet de sécurité. Sans elles, vous êtes aveugle. Résoudre en priorité.

---

## 10. BACKUP ÉCHOUÉ

**Actions:**
1. Vérifier l'espace disque (Playbook #6)
2. Vérifier les permissions: `python scripts/go_live_check.py --preflight`
3. Tenter un backup manuel: `python scripts/go_live_check.py --backup`
4. Si échec persistant: copier manuellement `data/trades.db`

---

## Checklist Post-Incident

Après TOUT incident:
- [ ] Backup créé
- [ ] Preflight passé
- [ ] Drills exécutés
- [ ] Positions vérifiées sur exchange
- [ ] Alertes fonctionnelles testées
- [ ] Capital readiness réévalué
