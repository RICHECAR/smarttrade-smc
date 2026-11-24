# ═══════════════════════════════════════════════════════════════════════════════
# TEST: Multi-Broker Dry-Run Order Test
# ═══════════════════════════════════════════════════════════════════════════════
#
# This script tests the multi-broker execution path WITHOUT placing real orders.
# Use it to validate that both Bitget and Kraken are properly configured.
#
# Prerequisites:
#   1. Backend server running: python run.py
#   2. Environment configured with KRAKEN_ENABLED=true and MULTI_BROKER_MIRROR=true
#
# Usage:
#   .\docs\TEST_CURL_MULTI_ORDER_DRYRUN.ps1
#
# For LIVE test execution (actually places orders):
#   Set ALLOW_TEST_EXECUTION=1 in .env
#   Then use dry_run=false in the request
#
# ═══════════════════════════════════════════════════════════════════════════════

$BASE_URL = "http://localhost:8000"

Write-Host ""
Write-Host "═══════════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  MULTI-BROKER DRY-RUN TEST" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────────
# Test 1: Check multi-broker status
# ─────────────────────────────────────────────────────────────────────────────────
Write-Host "1. Checking multi-broker status..." -ForegroundColor Yellow

try {
    $statusResponse = Invoke-RestMethod -Uri "$BASE_URL/api/multi/status" -Method GET -TimeoutSec 10
    Write-Host "   ✅ Status endpoint OK" -ForegroundColor Green
    Write-Host "   Brokers:" -ForegroundColor Gray
    $statusResponse.brokers | ConvertTo-Json -Depth 3 | Write-Host
} catch {
    Write-Host "   ❌ Status check failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "   Make sure the backend server is running: python run.py" -ForegroundColor Yellow
    exit 1
}

Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────────
# Test 2: Check manual execution status
# ─────────────────────────────────────────────────────────────────────────────────
Write-Host "2. Checking manual execution status..." -ForegroundColor Yellow

try {
    $manualStatus = Invoke-RestMethod -Uri "$BASE_URL/api/manual/status" -Method GET -TimeoutSec 10
    Write-Host "   ✅ Manual status OK" -ForegroundColor Green
    Write-Host "   Multi-broker enabled: $($manualStatus.multi_broker_enabled)" -ForegroundColor Gray
    Write-Host "   Brokers: $($manualStatus.enabled_brokers -join ', ')" -ForegroundColor Gray
} catch {
    Write-Host "   ❌ Manual status check failed: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────────
# Test 3: DRY-RUN order test (NO REAL ORDERS)
# ─────────────────────────────────────────────────────────────────────────────────
Write-Host "3. Running DRY-RUN order test..." -ForegroundColor Yellow
Write-Host "   (This does NOT place real orders - just validates configuration)" -ForegroundColor Gray

$testPayload = @{
    symbol = "BTCUSDT"
    quantity = 0.001
    side = "buy"
    dry_run = $true
} | ConvertTo-Json

try {
    $testResponse = Invoke-RestMethod -Uri "$BASE_URL/api/multi/order/test" -Method POST -Body $testPayload -ContentType "application/json" -TimeoutSec 15
    
    Write-Host ""
    Write-Host "   === DRY-RUN RESULTS ===" -ForegroundColor Cyan
    
    # Overall status
    if ($testResponse.overall_ready) {
        Write-Host "   ✅ READY: Both brokers configured for mirror trading!" -ForegroundColor Green
    } elseif ($testResponse.ready_count -eq 1) {
        Write-Host "   ⚠️  PARTIAL: Only one broker ready" -ForegroundColor Yellow
    } else {
        Write-Host "   ❌ NOT READY: No brokers available" -ForegroundColor Red
    }
    
    Write-Host ""
    Write-Host "   Signal ID: $($testResponse.signal_id)" -ForegroundColor Gray
    
    Write-Host ""
    Write-Host "   Environment:" -ForegroundColor Gray
    $testResponse.environment | ConvertTo-Json | Write-Host
    
    Write-Host ""
    Write-Host "   Brokers:" -ForegroundColor Gray
    $testResponse.brokers | ConvertTo-Json -Depth 3 | Write-Host
    
    Write-Host ""
    Write-Host "   Balances:" -ForegroundColor Gray
    $testResponse.balances | ConvertTo-Json -Depth 2 | Write-Host
    
    Write-Host ""
    Write-Host "   Symbol Mapping:" -ForegroundColor Gray
    $testResponse.symbol_mapping | ConvertTo-Json | Write-Host
    
    if ($testResponse.issues.Count -gt 0) {
        Write-Host ""
        Write-Host "   Issues:" -ForegroundColor Yellow
        foreach ($issue in $testResponse.issues) {
            Write-Host "     - $issue" -ForegroundColor Yellow
        }
    }
    
    Write-Host ""
    Write-Host "   Simulation:" -ForegroundColor Gray
    $testResponse.simulation | ConvertTo-Json -Depth 2 | Write-Host
    
} catch {
    Write-Host "   ❌ Dry-run test failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "   Response: $($_.ErrorDetails.Message)" -ForegroundColor Red
}

Write-Host ""
Write-Host "═══════════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  TEST COMPLETE" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Gray
Write-Host "  - If both brokers show 'ready: true', multi-broker is configured correctly" -ForegroundColor Gray
Write-Host "  - To test REAL execution:" -ForegroundColor Gray
Write-Host "    1. Set ALLOW_TEST_EXECUTION=1 in .env" -ForegroundColor Gray
Write-Host "    2. Use: POST /api/multi/order/test with dry_run=false" -ForegroundColor Gray
Write-Host "  - For manual trades: POST /api/manual/execute with brokers=['bitget','kraken']" -ForegroundColor Gray
Write-Host "  - Watch logs for [MULTI_EXEC] messages during execution" -ForegroundColor Gray
Write-Host ""

