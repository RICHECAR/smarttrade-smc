#!/bin/bash
# -----------------------------------------------------------------------------
# fetch_secrets.sh
# Injection de secrets au runtime (OPSEC Phase 0.8)
# Ce script DOIT appartenir uniquement au compte service (ex: `bitgetbot`)
# et posséder les permissions strictes 700.
# Aucun fichier n'est écrit sur le disque : tout est "sourcé" vers le main.
# -----------------------------------------------------------------------------

set -e # Stop on error

# Ensure execution context is secure
if [ -f "$(pwd)/.env" ]; then
    echo "[SECURITY KILL] .env file found in production! Aborting startup." >&2
    exit 1
fi

echo "[BOOTSTRAP] Fetching secrets from Key Vault..."

# =========================================================================
# EXEMPLE 1 : Utilisation AWS Secrets Manager
# =========================================================================
# fetch_aws_secrets() {
#     local SECRET_ID="smc-prod-secrets"
#     local REGION="eu-west-3"
#     # requires `jq` installed on machine
#     SECRETS_JSON=$(aws secretsmanager get-secret-value --secret-id $SECRET_ID --region $REGION --query SecretString --output text)
#     
#     export EXEC_BITGET_API_KEY=$(echo $SECRETS_JSON | jq -r '.EXEC_BITGET_API_KEY')
#     export EXEC_BITGET_SECRET_KEY=$(echo $SECRETS_JSON | jq -r '.EXEC_BITGET_SECRET_KEY')
#     export EXEC_BITGET_PASSPHRASE=$(echo $SECRETS_JSON | jq -r '.EXEC_BITGET_PASSPHRASE')
# }

# =========================================================================
# EXEMPLE 2 : Utilisation HashiCorp Vault
# =========================================================================
# fetch_vault_secrets() {
#     export VAULT_ADDR="https://vault.mycompany.com:8200"
#     export VAULT_TOKEN="s.YOUR_TEMPORARY_CI_TOKEN" # injected securely by orchestrator
#     
#     SECRETS_JSON=$(vault kv get -format=json secret/data/smc-prod)
#     export EXEC_KRAKEN_API_KEY=$(echo $SECRETS_JSON | jq -r '.data.data.EXEC_KRAKEN_API_KEY')
#     export EXEC_KRAKEN_API_SECRET=$(echo $SECRETS_JSON | jq -r '.data.data.EXEC_KRAKEN_API_SECRET')
# }

# =========================================================================
# MOCK / FALLBACK (POUR DÉMONSTRATION, À REMPLACER PAR AWS/VAULT)
# =========================================================================
export TRADING_MODE="live"
export AUTO_EXECUTE="true"
export KRAKEN_ENABLED="true"
export CONFIRM_LIVE_MULTI_BROKER="true"
export BITGET_PRODUCT_TYPE="USDT-FUTURES"

# En supposant que le parse json AWS s'est bien déroulé... On exporte.
# Ne pas oublier de définir le PYTHONPATH
export PYTHONPATH="$(dirname $(dirname $(realpath $0))):$PYTHONPATH"

# Démarrage direct du bot. Les identifiants secrets ne seront disponibles
# qu'à l'image mémoire du process python child.
echo "[BOOTSTRAP] Credentials Exported (in memory). Proceeding to Launch."
exec python3 smc_futures/orchestrator.py
