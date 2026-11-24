# PHASE 4: ASYMMETRIC EXECUTION (MAKER-TAKER)

## 1. Pourquoi l'Exécution Asymétrique ?
Le *StrictMirrorOMS* (Market/Market) mis en place lors des phases précédentes offre une sécurité algorithmique absolue (100% fail-closed, protection contre la désynchronisation). Cependant, le rapport de la Phase 3 (`SCALE_VERDICT.md`) a démontré une limite financière critique : le **coût de Rollback** (Double Slippage).
Dès lors que le système fait face à une latence inégale, un rejet, ou un timeout réseau sur un exchange, l'annulation de la position Market acquise sur l'exchange sain coûte 2x le Bid/Ask spread (Leg in, Leg out). À haute fréquence ou gros capital, l'alpha statistique de la stratégie se fait dévorer.

**La Solution :** Ne plus agresser deux Orderbooks simultanément.
1. Nous positionnons un ordre **Post-Only (Maker Limit)** sur l'exchange **Primary**.
2. Nous attendons asynchrone la confirmation *absolue* du Fill (Total ou Partiel).
3. Nous *Hedgeons* (Market ou Limit Taker) sur la jambe **Secondary** uniquement la quote-part qui a été remplie.

Si le Primary "miss" ou time-out, nous l'annulons. Zéro perte, zéro spread payé. Le trade est silencieusement ignoré. Nous éliminons 100% de la surface de Rollback.

---

## 2. The Asymmetric State Machine

Pour garantir la sécurité et gérer le "Leg-In Risk" asymétrique, nous introduisons une machine à états (State Machine) stricte traquant chaque intention de trading (Intent_ID).

### 2.1. Définition des États (States)
1. **CREATED** : L'intent SMC est ingéré.
2. **RISK_OK** : Validé par le GlobalRiskEngine (Stale OK, Spread OK, Maker viability OK).
3. **PRIMARY_PLACED** : Ordre Post-Only placé sur l'exchange Primary. Polling/WS en attente.
4. **PRIMARY_PARTIAL** : Ordre Primary partiellement exécuté.
5. **PRIMARY_FILLED** : Ordre Primary complétement exécuté.
6. **PRIMARY_TIMEOUT_CANCELED** : L'ordre Primary n'a pas été *Filled* dans le délai maximum et a été annulé avec succès.
7. **SECONDARY_HEDGE_PLACED** : L'ordre Taker a été envoyé au Secondary pour hedger le volume exact du Primary (Partiel ou Full).
8. **COMPLETED** : Le trade est fully hedged avec succès.
9. **FAILED_SAFE** : L'intent n'a pas abouti, mais aucune position nue n'est engagée (ex: Reject au placement, Timeout clean, Risk rejected).
10. **UNKNOWN_STATE / ORPHAN_RISK_DETECTED** : L'OMS a perdu la communication au milieu de l'état "Hedge" ou la jambe Hedge a été rejetée. **Position Orpheline Nette potentielle.**
11. **LATE_FILL_DETECTED** : Un fill Primary est arrivé APRÈS l'acquittement de son annulation verbale (Exchange Race Condition).

### 2.2. Tableau des Transitions & Timeouts
| État Courant | Condition / Trigger | Prochain État | Action et Logs |
| :--- | :--- | :--- | :--- |
| `CREATED` | Validé par Risk Engine | `PRIMARY_PLACED` | Place Limit Post-Only (Maker). Log ID. |
| `PRIMARY_PLACED` | WebSocket/Poll = Filled | `PRIMARY_FILLED` | Log. Trigger Secondary Hedge. |
| `PRIMARY_PLACED` | WebSocket/Poll = Partial | `PRIMARY_PARTIAL` | Attend ou annule reste selon `partial_fill_policy`. Trigger Hedge. |
| `PRIMARY_PLACED` | Timeout `max_wait_ms` > 5000 | `PRIMARY_TIMEOUT_CANCELED` | `cancel_order(Primary)`. Vérifie Read-Back=0. |
| `PRIMARY_FILLED` | -- (Transition interne) | `SECONDARY_HEDGE_PLACED` | Place Market Order(Secondary) pour la `fill_qty`. |
| `SECONDARY_HEDGE_PLACED`| Fill Secondary Confirmé | `COMPLETED` | Trade Asymétrique fini. Metric `asymmetric_completed_total`. |
| `SECONDARY_HEDGE_PLACED`| Reject Secondary | `ORPHAN_RISK_DETECTED` | **[CRITICAL]** Alerte PagerDuty. Execute `close_position_market(Primary)` (Auto-Flatten). |
| `PRIMARY_TIMEOUT_CANCELED`| WS signale "Filled" après Cancel | `LATE_FILL_DETECTED` | **[CRITICAL]** Alerte PagerDuty. Execute `close_position_market(Primary)`. |

---

## 3. Configuration & Exécution Policies

### Variables Globales
- **`EXECUTION_MODE`** : `STRICT_MIRROR` (Default/Fallback) ou `ASYMMETRIC_MAKER_TAKER`.
- **`AS_PRIMARY_EXCHANGE`** : `BITGET` ou `KRAKEN` (Peut être affiné par symbole plus tard).

### Profil Asymétrique
- **`max_wait_primary_fill_ms`** : Ex: `4500` ms. Si aucun Fill avant ce temps, la target Limit devient trop exposée au signal decay. Cancel immédiat.
- **`partial_fill_policy`** : `CANCEL_REST` (Hedger uniquement ce qui a été touché et annuler le Limit résiduel pour éviter d'autres Leg-in).
- **`secondary_exec_policy`** : `MARKET_HEDGE` (Priorité absolue pour mitiger l'exposition "nue" une fois le Maker rempli). Un "Limit Taker" exposerait à un "Slippage Non-Fill" intolérable institutionnellement.
- **`maker_price_policy`** : `BBA_POST_ONLY` (Au BBA actuel). Le Risk Engine rejettera "Maker Viability" si le Spread global ne justifie pas le BBA.

### Invariants Safety
- **Invariant 1 ("No Naked Position")** : Tout état orphelin (Reject Hedge, Crash Hedge, Late Fill) déclenche instantanément la fermeture active et brutale de la patte incriminée au Marché.
- **Invariant 2 ("Global Halt")** : Atteindre `ORPHAN_RISK_DETECTED` ou `LATE_FILL_DETECTED` engage impérativement le `KILL_SWITCH_ACTIVE = True` sur l'ensemble du GlobalRiskEngine.

---

## 4. Runbook d'Incidents Asymétriques

| Log Critique Observé | Cause Probable | Mitigation Automatique | Action Requise (Humain) |
| :--- | :--- | :--- | :--- |
| `🚨 ORPHAN_RISK_DETECTED` | L'exchange Secondary (Taker) a Crash / Insufficient Margin / Limit Rate et n'a pas pu Hedge. La patte Primary est "Un-hedged". | L'Adapter Secondary lâche une erreur. Une commande `close_position(Primary)` est envoyée. Si celle-ci échoue -> Appel du script `emergency_kill.py` via cron externe (DR). | Check API limites Secondary. Check Margin Secondary. Réarmer OMS. |
| `🚨 LATE_FILL_DETECTED` | Le serveur Primary (Exchange) a accusé réception d'un CANCELLATION, mais l'ordre est passé "Filled" 1 milliseconde avant dans leur Moteur Interne. | Auto-Flatten de la patte Primary (Market Inverse). Trigger Risk Lock. | Analyser la latence. Si fréquent, augmenter le `max_wait_primary_fill_ms`. |
