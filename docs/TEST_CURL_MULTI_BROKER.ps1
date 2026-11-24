# Multi-Broker API Test Commands
# ═══════════════════════════════════════════════════════════════════

# Prerequisites:
# - Start the backend server: python -m smc_futures.api_smc_engine
# - Server default: http://localhost:8000

$API = "http://localhost:8000"

# ─────────────────────────────────────────────────────────────────────
# STATUS & HEALTH
# ─────────────────────────────────────────────────────────────────────

# Get multi-broker status
Invoke-RestMethod -Uri "$API/api/multi/status" -Method GET

# Simple health check
Invoke-RestMethod -Uri "$API/api/multi/health" -Method GET


# ─────────────────────────────────────────────────────────────────────
# BALANCES
# ─────────────────────────────────────────────────────────────────────

# Get all balances (cached)
Invoke-RestMethod -Uri "$API/api/multi/balances" -Method GET

# Get all balances (force refresh from exchanges)
Invoke-RestMethod -Uri "$API/api/multi/balances?refresh=true" -Method GET

# Get Bitget balance only
Invoke-RestMethod -Uri "$API/api/multi/balances/bitget" -Method GET

# Get Kraken balance only
Invoke-RestMethod -Uri "$API/api/multi/balances/kraken?refresh=true" -Method GET


# ─────────────────────────────────────────────────────────────────────
# BALANCE HISTORY
# ─────────────────────────────────────────────────────────────────────

# Get balance history (7 days, all brokers)
Invoke-RestMethod -Uri "$API/api/multi/balance/history" -Method GET

# Get balance history for Bitget only (30 days)
Invoke-RestMethod -Uri "$API/api/multi/balance/history?broker=bitget&days=30" -Method GET

# Get latest balances
Invoke-RestMethod -Uri "$API/api/multi/balance/latest" -Method GET

# Force record current balances to history
Invoke-RestMethod -Uri "$API/api/multi/balance/record" -Method POST


# ─────────────────────────────────────────────────────────────────────
# POSITIONS
# ─────────────────────────────────────────────────────────────────────

# Get all open positions from all brokers
Invoke-RestMethod -Uri "$API/api/multi/positions" -Method GET

# Get positions from Bitget only
Invoke-RestMethod -Uri "$API/api/multi/positions/bitget" -Method GET


# ─────────────────────────────────────────────────────────────────────
# TRADING LOOP CONTROL
# ─────────────────────────────────────────────────────────────────────

# Start multi-broker trading loop (also starts balance scheduler)
Invoke-RestMethod -Uri "$API/api/multi/start" -Method POST

# Stop multi-broker trading loop (also stops balance scheduler)
Invoke-RestMethod -Uri "$API/api/multi/stop" -Method POST


# ─────────────────────────────────────────────────────────────────────
# ORDERS
# ─────────────────────────────────────────────────────────────────────

# Place order on specific broker (Bitget)
$orderBody = @{
    symbol = "BTCUSDT"
    side = "buy"
    quantity = 0.001
    order_type = "market"
    broker = "bitget"
} | ConvertTo-Json

Invoke-RestMethod -Uri "$API/api/multi/order" -Method POST -Body $orderBody -ContentType "application/json"

# Place order on all brokers
$multiOrderBody = @{
    symbol = "BTCUSDT"
    side = "buy"
    quantity_per_broker = 0.001
    order_type = "market"
} | ConvertTo-Json

Invoke-RestMethod -Uri "$API/api/multi/order/all" -Method POST -Body $multiOrderBody -ContentType "application/json"


# ─────────────────────────────────────────────────────────────────────
# CURL EQUIVALENTS (for bash/curl users)
# ─────────────────────────────────────────────────────────────────────

<# 
# Get all balances with refresh
curl -X GET "http://localhost:8000/api/multi/balances?refresh=true"

# Get balance history
curl -X GET "http://localhost:8000/api/multi/balance/history?days=7"

# Start trading loop
curl -X POST "http://localhost:8000/api/multi/start"

# Force record balance
curl -X POST "http://localhost:8000/api/multi/balance/record"

# Place order
curl -X POST "http://localhost:8000/api/multi/order" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTCUSDT","side":"buy","quantity":0.001,"broker":"bitget"}'
#>
