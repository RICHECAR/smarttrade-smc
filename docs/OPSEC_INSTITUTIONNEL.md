# OPSEC INSTITUTIONNEL : Protocole de Sécurité Multi-Exchange

Ce document définit les standards de sécurité opérationnelle exigés pour faire tourner le bot SMC en mode Live sur des capitaux réels via Kraken Futures et Bitget.

---

## 1. MATRICE DE PERMISSIONS (Kraken + Bitget)

Le principe fondamental est le **Moindre Privilège**. L'interface web de l'exchange (et potentiellement l'API de retrait de fonds) ne doit **jamais** être accessible depuis les serveurs d'exécution. Les retraits sont interdits au niveau des clés API.

### A. Clés d'Exécution (`EXEC_KEYS`) - Le Bot Principal

| Permission Exchange | Nécessaire ? | Pourquoi ? |
| :--- | :---: | :--- |
| **Futures Trading / Orders** | **OUI** | Pour placer, modifier et annuler des ordres d'entrée et de sortie sur les contrats dérivés. |
| **Read Positions** | **OUI** | Pour l'OMS, vérifier l'état des trades en cours et effectuer la réconciliation interne. |
| **Read Balances / Wallet** | **OUI** | Obligatoire pour calculer le risk management dynamique en fonction de la balance (Account Balance). |
| Spot Trading | NON | Le bot opère exclusivement sur les dérivés. (À désactiver stictement). |
| Transferts Internes (Inter-wallet) | NON | Le bot ne bouge pas les fonds entre margin et spot. |
| **Withdrawals (Retraits)** | **NON !** | Sécurité fondamentale. Permet d'éviter le vol de capitaux en cas de compromission serveur. **À vérifier manuellement sur l'UI Exchange**. |
| API IP Whitelist | **OUI** | Obligatoire, fixe l'IP du serveur d'exécution (Azure / AWS / VPS). |

### B. Clés du Kill-Switch (`KILL_KEYS`) - `emergency_kill.py` & `trade_reconciler_strict.py`

| Permission Exchange | Nécessaire ? | Pourquoi ? |
| :--- | :---: | :--- |
| **Futures Trading / Orders** | **OUI** | Pour envoyer les annulations globales (Cancel) et réduire les positions au Market (Close). |
| **Read Positions / Orders** | **OUI** | Pour le Read-Back du Kill-Switch et les checks continus du `trade_reconciler_strict.py`. |
| Read Balances / Wallet | NON | Le kill-switch n'a pas besoin de calculer le risque, il flush tout. |
| Spot / Transferts / Retraits | NON | Strictement interdit. |
| API IP Whitelist | **OUI** | Doit être restreinte à l'IP du serveur hébergeant le bot/kill-switch, ou une IP de management (VPN d'admin). |

**Règle d'or :** Créez physiquement 4 (ou plus) clés API distinctes sur l'interface de chaque exchange. Ne réutilisez jamais une `EXEC_KEY` pour le Kill-Switch. Souvent, la création de Sous-Comptes (Sub-accounts) dédiés à l'API permet de compartimenter encore mieux et d'isoler la marge globale.

---

## 2. ARCHITECTURE SECRETS (DEV vs PROD)

Le fichier `.env` sur le disque est un antipattern en production car un RCE (Remote Code Execution) ou un dump de répertoire l'expose immédiatement. 

### En Développement (Local)
- Fichier `.env` utilisé.
- Ignoré dans le `.gitignore`.
- Vérifié via `os.getenv` classique.

### En Production (Déploiement)
- Le bot doit **refuser de démarrer** s'il trouve un fichier `.env` physique (pour forcer la bonne pratique).
- L'injection des secrets se fait au moment de l'instanciation du processus parent, directement dans la mémoire vive de l'OS.

#### Flux Choisi : Secrets Manager injecté en Environment Variables
Au lieu de forcer le code Python à faire des appels réseau complexes et bloquants vers AWS KMS/Secrets Manager ou HashiCorp Vault (ce qui rajoute une latence, un point de défaillance, et expose le process à des timeouts de token IAM), nous utiliserons l'**approche d'infrastructure orientée processus**.

1. **Où le secret vit :** AWS Secrets Manager ou HashiCorp Vault ou Azure Key Vault ou GitHub Actions Secrets.
2. **Comment il est injecté au runtime :** 
   - Lors du démarrage, l'orchestrateur (ex: Docker swarm, Kubernetes, ECS, ou un simple script systemd utilisant le AWS CLI) va interroger AWS/Vault, récupérer les paires clé/valeur, et les passer comme *Variables d'Environnement en mémoire* au processus `python main.py`.
   - Ainsi, l'application Python lit `os.environ` normalement (comme en dev), mais aucun fichier `.env` n'existe sur le disque.
3. **Comment le code le lit :** Via le nouveau module Python `security/secrets.py`.
4. **Comment éviter les logs accidentels :** Via un un wrapper de logging `security/redaction.py` qui intercepte chaque ligne de log et masque les chaînes de cryptographie (masquage dynamique ou filtrage regex des clés définies en mémoire).

---

## 3. SÉPARATION DES CLÉS (Exécution vs OOB)

Le fait de séparer les clés permet d'avoir 2 canaux distincts (2 limit-rate distincts) et garantit que si le bot corrompt ses clés `EXEC` ou dépasse ses limits websockets, les requêtes du script OOB `KILL` via des clés et endpoints différents passeront avec la plus haute priorité.

**Structure d'Environnement Requise :**

```env
# ==== EXECUTION KEYS (Bot Principal) ====
EXEC_BITGET_API_KEY="..."
EXEC_BITGET_SECRET_KEY="..."
EXEC_BITGET_PASSPHRASE="..."

EXEC_KRAKEN_API_KEY="..."
EXEC_KRAKEN_API_SECRET="..."

# ==== KILL SWITCH KEYS (OOB Script & Reconciler) ====
KILL_BITGET_API_KEY="..."
KILL_BITGET_SECRET_KEY="..."
KILL_BITGET_PASSPHRASE="..."

KILL_KRAKEN_API_KEY="..."
KILL_KRAKEN_API_SECRET="..."
```

---

## 4. IP ALLOWLIST : PROCÉDURE + CHECKLIST

Contrairement aux wallets décentralisés, les CEX offrent l'énorme avantage de la restriction par bloc IP.

### Déploiement Cloud avec IP Statique
1. Louez un VPS ou instanciez une VM Cloud (AWS EC2, DigitalOcean Droplet, etc.) et **associez impérativement une "Elastic IP" / "Reserved IP"**. L'IP ne doit jamais changer au reboot.
2. Déployez le bot sur ce serveur. Vous pouvez utiliser Docker.
3. Allez sur Bitget > API Management > Créer. Cochez `Bind IP address`. Saisissez uniquement cette Elastic IP. Répétez pour Kraken.

### Plan de Secours (IP changeante ou migration serveur)
Si le serveur maître crash de façon irrécupérable, votre OOB Kill-Switch sur le serveur backup **ne marchera pas** si l'IP du backup n'est pas whitelisted.
- **Solution A :** Whitelister d'avance les *deux* IPs (serveur primaire, serveur OOB de secours) lors de la création de la clé. La majorité des exchanges l'autorisent.
- **Solution B (Plus stricte) :** Garder le Kill-Switch sur l'ordinateur portable du Head of Trading (IP locale via VPN), et whitelister l'IP sortante du VPN entreprise, en complément de l'IP du serveur cloud, **uniquement sur les KILL_KEYS**.
Ainsi, le développeur peut "Tuer" le système depuis son PC, sans pouvoir initier de trades avec les EXEC_keys.

### Checklist (Avant mise en production)
- [ ] Retraits désactivés sur *TOUTES* les clés API (Exec et Kill).
- [ ] IP Bindings appliqués sur *TOUTES* les clés API (pas de champ IP vide "0.0.0.0").
- [ ] Les IDs de sous-comptes sont vérifiés (transferts interdits).
- [ ] Pas de code commit sur Git contenant un fichier `.env`.

---

## 6. PROCÉDURE D'INCIDENT ET ROTATION

### A. Si une fuite de clé (Leak) est suspectée :
*Exemple: Code open-sourcé par erreur, serveur piraté, log pushé sur Slack.*
1. **Couper les clés :** Se connecter immédiatement via Interface Web Kraken & Bitget, supprimer/désactiver les clés compromises (`EXEC_KEYS`).
2. **Lancer le Kill-Switch OOB :** Via les `KILL_KEYS` depuis la machine locale sécurisée (si non compromise), ou forcer le "Close All" via le dashboard de l'UI Exchange.
3. **Regénérer et nettoyer :** Recréer un set de clés, changer tous les passwords de serveurs cloud.
4. **Re-allowlist IP :** Refaire le whitelisting strict.

### B. Rotation planifiée (Best Practice)
- **Fréquence :** Standard institutionnel = tous les 90 jours.
- **Étapes :**
  1. Générer de nouvelles `EXEC_KEYS_V2` et `KILL_KEYS_V2` (avec les mêmes IP et permissions limitées).
  2. Sur le Secret Manager (ou vault), planifier le remplacement et le redeploy à une heure creuse (dimanche soir/week-end).
  3. Relancer les containers.
  4. Lancer le `trade_reconciler_strict.py` (ou surveiller les logs) pour vérifier la bonne connexion.
  5. Une fois OK, supprimer les `_V1` de l'exchange (révocation de l'ancien token).
