# RUNBOOK & SCALING OPSEC (PHASE 4)

## Architecture Rappel
Le bot évolue désormais en exécution Asymétrique Maker-Taker. Toute défaillance provoque soit un Cooldown Temporel (`cooldown_duration_ms`), soit un Global Lock définitif via le PagerDuty/Kill Switch.

---

## 1. MÉTROLOGIE QUOTIDIENNE (Checklist Opérateur)
Vérifiez chaque jour les compteurs suivants :
1. `GET /health/execution` -> `kill_switch_active` == `false`.
2. Prometheus `orphan_detected_total` == `0`.
3. Prometheus `late_fill_total` == `0`.
4. `config/scaling_policy.yaml` -> Vérifier que Tier correspond au capital engagé.

> **Aucune intervention humaine n'est tolérée si tous ces KPIs sont au vert.**

---

## 2. INCIDENTS CRITIQUES & INTERVENTIONS

### 🚨 ALERTE : `ORPHAN_RISK_DETECTED` ou `LATE_FILL_DETECTED` (>0)
- **Symptôme :** Le bot a subi un incident insoluble asynchrone (ex: API Kraken Crash *pendant* le Hedge d'un ordre Bitget Fill).
- **Conséquence API :** Le bot s'est VÉROUILLÉ DEFINITIVEMENT. Il a liquidé la patte primaire via un `Market Flatten`.
- **RÉPONSE HUMAINE OBLIGATOIRE :**
  1. Ouvrez Bitget et/ou Kraken manuellement.
  2. Vérifiez que TOUTES les positions sont à ZÉRO.
  3. Comprenez pourquoi le Hedge a échoué (Rate Limit ? Maintenance Externe ? Marge ?).
  4. Redémarrez le Bot physiquement (Systemctl restart smc_bot). Il n'y a **aucun unlock logiciel auto**.

### ⚠️ ALERTE : `COOLDOWN_ACTIVE` (Gauge = 1)
- **Symptôme :** Le bot a subi un échec de validation bénin (Primary Limit Order expiré, Size < Minimum Notional).
- **Conséquence API :** Il rejette absolument toutes les intentions d'achat/vente pour prévenir le churn API (par défaut: 60s).
- **RÉPONSE HUMAINE :**
  - **SENS STRICTEMENT INTERDIT : N'intervenez pas.** Le Bot va purger le Cooldown de lui-même à échéance du timer. C'est fait pour le calmer.

---

## 3. CHECK PRE-LIVE
Avant de démarrer un Docker / Unit / Systemd :
`python scripts/pre_live_check.py`

Ce script inspecte les compteurs de Failsafe actuels pour refuser tout démarrage sur une DB ou Memoire corrompue. S'il renvoie `NO-GO`, vous devez purger `prometheus` et réparer l'audit.
