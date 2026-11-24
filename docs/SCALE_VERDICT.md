# SCALE VERDICT : ÉVALUATION QUANTITATIVE MULTI-EXCHANGE

**Date du Test:** 2026-02-28
**Cadre:** Phase 3 (Async Event Replay & Fault Injection)
**Moteur:** `StrictMirrorOrderManager` vs `FaultInjectingMockAdapter`

Ce rapport détaille les résultats des Stress Tests mécaniques. L'objectif était de déterminer le "Scale Ceiling" (Plafond de viabilité) du bot lorsqu'il tourne en mode "Mirror Strict" (1 signal = 2 exchanges simultanés).

---

## 1. Résumé des Profils de Charge (Stress Test)

Une série de 250 Intensions (Signaux) a été tirée asynchronement avec un degré de concurrence de 10 workers constants vers le VRAI Moteur de Risque Cœur.

### Résultat Brut :
- **Profil A: Baseline Ideal Market (50 Trades, < 50ms latence, 0% Rejet).**
  - **Exécution:** 0.19 secondes.
  - **Rollback Rate:** 0.0%
  - **Verdict:** Le `asyncio.gather` effectue une répartition réseau virtuellement instantanée. Le coût CPU est nul.
- **Profil B: High Latency Network (100 Trades, Jitter Extrême ~800ms, 0% Rejet).**
  - **Exécution:** 5.81 secondes.
  - **Rollback Rate:** 0.0%
  - **Verdict:** L'OrderManager est insensible à la latence tant que les requêtes ne touchent pas le Timeout strict (5.0s). Les 10 workers épongent le réseau lent parfaitement.
- **Profil C: Toxic Fault Injection (100 Trades, 15% Rejet Kraken, 2% 30s-Timeout).**
  - **Exécution:** 5.09 secondes.
  - **Rollback Rate:** ~17% (Déclenché par les Rejects Kraken & Timeouts).
  - **Race Conditions:** Multiples interpellations de la *Double-Fill Defense* suite à un Timeout résolu tardivement.

---

## 2. Plafond de Viabilité (Scale Ceiling)

Le mode `Strict Mirror` introduit une asymétrie de fragilité : **Le système global est aussi faible que le pire de ses exchanges au temps T.**

### Limite Latence
Le plafond mécanique est de **4.9 secondes**. Au-delà de 5.0 secondes réseau d'attente sur UNE jambe, le Timeout est prononcé et force l'OrderManager à envoyer une rafale Market *sur la jambe opposée réussie*. Par conséquent, un hébergement VPS éloigné des serveurs d'API de Futures (ex: Tokyo pour Bitget, Londres pour Kraken) ruinera mathématiquement vos PNLs à cause de l'arbitrage de latence sur les "Rollbacks".

### Limite Taux d'Erreur (Reject Threshold)
D'après l'analyse probabiliste des CSV metrics, si le taux de rejet naturel (Insufficent Margin, Rate Limit) de Bitget est de 0% mais que Kraken oscille à 2.0%, **2% de TOUS VOS TRADES Bitget subiront un Slippage fois deux** (1 fill + 1 reversed fill manuel immédiat). 
Ce "Coût de Friction Asynchrone" siphonne le Sharpe Ratio exponentiellement.

---

## 3. VERDICT QUANTITATIF DE SCALING

**GO CONDITIONNEL POUR LE "MICRO-CAPITAL" :** L'architecture gère les exceptions complexes de la meilleure manière algorithmique possible (Read-Back Flat, Kill-Switch Mirroring). Le bot ne plantera pas et votre capital est mécaniquement protégé d'un Drawdown explosif asymétrique.

**NO-GO POUR "HEAVY SCALE" TANT QUE :**
Tant qu'il s'exécute en "Mirror Strict" simultané au marché, le bot est structurellement incompatible avec des capitaux massifs à cause du **Double Slippage Rollback Cost**.

### Roadmap pour Débloquer le Scaling :
Si/quand le Micro-capital de la Phase 3 réussit ses tests en live, il faudra implémenter la **Phase 4 : Asymmetric Execution** :
* *Option A (Maker/Taker)* : L'intent SMC place une intention Post-Only Maker sur Bitget. Si et SEULEMENT SI l'ordre Bitget est `Filled` (prouvé par WS user-data), le bot émet immédiatement un Taker/Market sur Kraken de taille correspondante pour mirrorer le reste de la position. (Ou vice versa selon les frais). Cela détruit 100% de la surface de "Rollback" au prix d'un risque modeste de prix d'exécution asynchrone ("Leg-in Risk").
