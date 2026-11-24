# RUNBOOK OPSEC : Multi-Broker Bot

Ce Runbook détaille les procédures d'urgences et de maintenance institutionnelle exigées pour le bot SMC Trader (Kraken & Bitget).

⚠️ **Ce document est confidentiel et s'adresse aux opérateurs SRE / Security Engineers / Head of Trading.**

---

## 🚀 GO LIVE CHECKLIST

Avant toute exécution de la branche `master` ou `production` vers l'environnement réel :

### 1. Hardening Système (Serveur)
- [ ] Le serveur tourne sur Linux avec `systemd`. L'utilisateur dédié (`bitgetbot`) ne possède aucun privilège `sudo`.
- [ ] Le fichier `.env` a été impérativement **SUPPRIMÉ** du serveur via `rm -f /opt/bitget-smc-auto-trader/.env`.
- [ ] Le service `/etc/systemd/system/smc_bot.service` applique les contraintes strictes : `ProtectSystem=full`, `PrivateTmp=true`, `NoNewPrivileges=true`.

### Politique de Données Stale en Live (Phase 4)

En `TRADING_MODE=live`, le bot applique une politique de **Hard Drop** pour les données de marché périmées :
- **Seuil** : 250ms (Source : `SMC_CONFIG["STALE_THRESHOLD_MS"]`).
- **Pas de Retry** : Le retry hérité `asyncio.sleep(1.0)` est désactivé en live pour éviter de bloquer la boucle d'exécution.
- **Monitoring** : Chaque tick stale rejeté incrémente la métrique `rejects_total{reason="STALE_DATA"}`.
- **Seuil d'Alerte** : **STALE_DATA > 15% sur une fenêtre de 5 minutes** indique un incident avec le fournisseur de données ou le réseau. Investigation IMMÉDIATE de la connectivité VPS requise.

### 2. Gestion des Secrets
- [ ] Un backend de configuration secret (AWS Secrets Manager, Azure Key Vault, ou HashiCorp Vault) est configuré et héberge le JSON avec les `EXEC_KEYS` et `KILL_KEYS`.
- [ ] Le script `/opt/bitget-smc-auto-trader/scripts/fetch_secrets.sh` est valide, possède un `chmod 700`, et est `chown bitgetbot:bitgetbot`.
- [ ] L'orchestrateur (ou AWS IAM) autorise l'instance Cloud à pull ces secrets en lecture seule (GetSecretValue).

### 3. Contrôle des Clefs API *(Directement via UI Kraken & Bitget)*
- [ ] Les `EXEC_KEYS` et les `KILL_KEYS` sont physiquement des identifiants différents.
- [ ] **Withdrawals / Retraits** : Rigidement interdits (décoché) sur **toutes** les clés.
- [ ] IP Binding appliqué : Les IPs du VPS ou la NAT Gateway AWS sont renseignées pour les permissions `EXEC_KEYS`.
- [ ] IP Binding appliqué sur `KILL_KEYS` : Inclut le VPS **ET** l'IP du VPN entreprise du C-Level ou Lead Dev en cas d'intervention distante out-of-band.

### 4. Tests d'Intégration Manuels
- [ ] Démarrez manuellement `python scripts/emergency_kill.py` pour vérifier que le KILL_KEYS logging se connecte aux deux exchanges `[pass 1] verified flat`.
- [ ] **Preuve Machine du FLAT** : Avant tout passage en live, exécutez un script de position ou vérifiez via UI.
    - Script : `python scripts/pre_live_check.py` (doit retourner `TOTAL_OPEN_POSITIONS: 0`).
    - UI Kraken : Onglet 'Positions' == vide.
    - UI Bitget : Onglet 'Positions' == vide.
- [ ] Vérifiez que la DB ou les logs remontent les infos avec le tag `[REDACTED]` dynamiquement si l'on écrit par erreur un mot de passe dans le code.

---

## 🚨 INCIDENT RESPONSE : FUITES (LEAK)

**Cas typique :** Une clé API a été exposée dans un log Slack, un dépôt public, ou un ingénieur social engineering.

1. **STOP THE BLEEDING (Immédiat - T0)**
   - Exécutez le script d'urgence immédiatement depuis le VPN autorisé :
     ```bash
     export KILL_BITGET_API_KEY="..."
     export KILL_...="..."
     python scripts/emergency_kill.py
     ```
   - Connectez-vous aux interfaces Web (Bitget & Kraken) par les portails institutionnels (SubAccounts).
   - Accédez à API Management -> **SUPPRIMER DÉFINITIVEMENT** les clés API compromises ou au moindre doute. Ne les désactivez pas, supprimez-les.
   - **Note sur le verrouillage (HARD-LOCK)** : Si une anomalie fatale (ORPHAN/DESYNC) est détectée, le bot définit `risk_locked = true` et `cooldown = infinite`. **SEUL un redémarrage physique (systemd restart)** après investigation permet la reprise du trading. Le cooldown logiciel est ignoré dans cet état.

2. **REMEDIATION (T+15m)**
   - Stoppez le daemon : `sudo systemctl stop smc_bot`.
   - Lancez un Security Audit rapide sur le code source ou la méthode de transmission pour identifier la brèche (Avez-vous créé un `.env` par pitié ?).

3. **RECOVERY (T+60m)**
   - Si la fuite de la brèche est neutralisée, créez de nouvelles clés `EXEC_KEYS` et `KILL_KEYS` avec nommage strict (ex: `SMC_PROD_EXEC_V2.1`).
   - Mettez à jour le Vault / AWS Secrets Manager.
   - Relancez le processus systemd qui re-fetchera le vault : `sudo systemctl start smc_bot`.

---

## 🔄 ROTATION DES CLÉS (PLANIFIÉE 90 JOURS)

Toute clé API "Institutionnelle" doit mourir de façon prédéterminée. Pour la rotation, ne jamais altérer les IP bindings pendant le swap pour éviter un lock exchange.

### Procédure de la Maintenance :
1. Créez les nouvelles séries de clés sur Bitget & Kraken (`V_NEXT`).
2. Configurer les whitelists IP identiques aux clés existantes.
3. Allez dans Hashicorp Vault ou AWS Secrets Manager, créez une nouvelle **version** du secret avec `V_NEXT`.
4. Effectuez un Graceful Restart :
   - `sudo systemctl stop smc_bot` (Attend que toutes les websockets ferment).
   - *Optionnel: vérifiez que le statut de l'exchange est FLAT*.
   - `sudo systemctl start smc_bot` (Va fetch la nouvelle _VERSION_ de clefs aws).
5. Surveillez les logs `sudo journalctl -u smc_bot -f`.
6. Si les premiers ticks websocket sont verts et qu'aucun rejet auth n'intervient, allez sur Kraken/Bitget et supprimez la veille clef V_OLD pour tuer les anciens tunnels non-terminés.
= > L'indisponibilité totale requise est de l'ordre de ~4 secondes.
