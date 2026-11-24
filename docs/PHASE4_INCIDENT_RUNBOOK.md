# RUNBOOK : Phase 4 Asymmetric Incident Response

Ce document traite des cas marginaux (edge cases) liés à l'exécution asymétrique **Maker-Taker** (Phase 4).

## 1. Maker Late Fill (Remplissage Tardif)
**Symptôme :** Le bot a annulé l'ordre Maker (Primary Leg) pour cause de timeout ou décalage de prix, mais l'exchange confirme le remplissage *après* l'annulation.
- **Action Automatique :** Le bot détecte la position orpheline via `get_open_positions`. Il passe en **HARD-LOCK**.
- **Réponse Opérateur :**
    1. Vérifier si le Taker a été envoyé. Si non, vous avez une "Orphan Position".
    2. Exécuter `scripts/emergency_kill.py` pour aplatir la position.
    3. Analyser les logs `[MAKER_LATE_FILL]` pour comprendre la latence de l'exchange.

## 2. Maker Partial Fill (Remplissage Partiel)
**Symptôme :** L'ordre Maker n'est rempli qu'à 20% avant que le prix ne s'échappe.
- **Politique :** Le bot traite la partie remplie comme un trade valide et ajuste la taille du Taker (Secondary Leg) pour correspondre exactement au partiel.
- **Risque :** Si le partiel est inférieur au `min_notional` de l'exchange secondaire.
- **Réponse Opérateur :**
    1. Si le Taker échoue pour taille insuffisante, la position Maker reste ouverte et non-couverte.
    2. Le bot déclenche une alerte `CRITICAL_ASYMMETRIC_MISMATCH`.
    3. Fermeture manuelle requise via UI ou script d'urgence.

## 3. Taker Reject / Timeout
**Symptôme :** La jambe Maker est remplie à 100%, mais l'ordre Taker (Market) sur l'autre exchange est rejeté (ex: Liquidity, Insufficient Margin).
- **Conséquence :** Exposition directionnelle non souhaitée (Unhedged).
- **Action Automatique :** Le bot tente un **Retry immédiat (Max 3)**. Si échec permanent -> **HARD-LOCK**.
- **Réponse Opérateur :**
    1. **URGENCE :** Couvrir la position manuellement sur l'exchange secondaire ou fermer la position primaire.
    2. Vérifier les rapports de collatéral sur l'exchange qui a rejeté.

## 4. État "HARD-LOCK" & Reprise
**Symptôme :** Le dashboard affiche `HARD LOCK STATUS: LOCKED`. Tout nouveau signal est ignoré (`signal dropped: risk_locked`).
- **Cause :** Détection d'une anomalie fatale (Mismatch de position, échec de rollback, rejets critiques successifs).
- **Procédure de Reset :**
    1. **Audit** : Identifier la cause racine dans `journalctl -u smc_bot -f`.
    2. **Flatten** : Garantir que `TOTAL_OPEN_POSITIONS == 0`.
    3. **Restart** : `sudo systemctl restart smc_bot`.
    4. *Note : Aucun bouton UI ne peut reset un Hard-Lock par design.*

## 5. Monitoring des Rejets Stale
- **Métrique :** `rejects_total{reason="STALE_DATA"}`.
- **Action :** Si le taux dépasse 15% sur 5min, le réseau est dégradé.
- **Décision :** Passer temporairement en `AUTO_EXECUTE_MODE=false` via le dashboard le temps de la stabilisation.
