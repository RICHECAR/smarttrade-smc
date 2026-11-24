#!/usr/bin/env pwsh
# ════════════════════════════════════════════════════════════
# 🧪 SMC Trading Bot - Connection Test Script
# ════════════════════════════════════════════════════════════
# Tests that the backend API is running and accessible.
#
# Usage:
#   .\test_connection.ps1
#   .\test_connection.ps1 -Port 8001
# ════════════════════════════════════════════════════════════

param(
    [int]$Port = 8000,
    [string]$Host = "127.0.0.1"
)

$BaseUrl = "http://${Host}:${Port}"

Write-Host "═══════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  SMC Trading Bot - Backend Connection Test" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""
Write-Host "Target: $BaseUrl" -ForegroundColor Yellow
Write-Host ""

# Test endpoints
$endpoints = @(
    @{Path="/api/health"; Method="GET"; Description="Health Check"},
    @{Path="/api/status"; Method="GET"; Description="Bot Status"},
    @{Path="/api/v1/market/ticker?symbol=BTCUSDT"; Method="GET"; Description="Market Ticker"},
    @{Path="/api/kraken/status"; Method="GET"; Description="Kraken Status"},
    @{Path="/api/brokers/status"; Method="GET"; Description="Multi-Broker Status"}
)

$passed = 0
$failed = 0

foreach ($ep in $endpoints) {
    $url = "$BaseUrl$($ep.Path)"
    Write-Host -NoNewline "  Testing $($ep.Description)... "
    
    try {
        $response = Invoke-RestMethod -Uri $url -Method $ep.Method -TimeoutSec 5 -ErrorAction Stop
        Write-Host "✓ OK" -ForegroundColor Green
        $passed++
    }
    catch {
        if ($_.Exception.Message -match "Unable to connect|Connection refused") {
            Write-Host "✗ BACKEND NOT RUNNING" -ForegroundColor Red
        } else {
            Write-Host "✗ $($_.Exception.Message)" -ForegroundColor Red
        }
        $failed++
    }
}

Write-Host ""
Write-Host "═══════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  Results: $passed passed, $failed failed" -ForegroundColor $(if ($failed -eq 0) { "Green" } else { "Red" })
Write-Host "═══════════════════════════════════════════════════════════" -ForegroundColor Cyan

if ($failed -gt 0) {
    Write-Host ""
    Write-Host "  ⚠️  Backend not responding. Start it with:" -ForegroundColor Yellow
    Write-Host "      python run.py" -ForegroundColor White
    Write-Host "      # or" -ForegroundColor DarkGray
    Write-Host "      npm run start:backend" -ForegroundColor White
    Write-Host ""
    exit 1
}

exit 0
