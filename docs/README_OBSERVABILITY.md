# SMC Auto Trader - Observability & Metrics

Cette section explique comment démarrer la stack de monitoring locale pour visualiser l'état du bot en temps réel.

## Lancement Rapide (Docker Compose)
1. Assurez-vous d'avoir Docker et `docker-compose` installés.
2. Allez dans le répertoire `observability/`.
3. Lancez la stack en arrière-plan :
   ```bash
   cd observability
   docker-compose -f docker-compose.observability.yml up -d
   ```

## Accès aux Interfaces
- **Prometheus** : `http://localhost:9090`
- **Grafana** : `http://localhost:3000`
  - *Login par défaut* : `admin` / `admin_smc`

## Métriques Prometheus Exposées
Le bot démarre un thread Python `prometheus_client` sur le port **8000**. Les signaux suivants sont scrappés toutes les 5 secondes :
- `trades_total` (Counter) : Nombres de requêtes envoyées à l'OMS (dispatch split par exchange, side, symbol).
- `rejects_total` (Counter) : Nombre de validations échouées par le Global Risk Engine. Motifs tracés: `STALE_DATA`, `SPREAD_TOO_WIDE`, `LOW_LIQUIDITY`.
- `partial_exec_total` (Counter) : Exécutions incomplètes attrapées par le Strict Mirroring de l'OMS. 
- `rollback_total` (Counter) : Rollbacks de sécurités enclenchés.
- `ws_desync_count` (Counter) : Désynchronisations Data vs Positions critiques détectées.

### Exemple de requêtes (PromQL) :
*Taux de rejet pour liquidité trop faible sur 5m :*
`rate(rejects_total{reason="LOW_LIQUIDITY"}[5m])`

*Taux de rollbacks (Crash / Partial execution) :*
`rate(rollback_total[1h])`
