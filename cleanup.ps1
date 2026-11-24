###############################################################################
# cleanup.ps1 - Trading Bot Maintenance Script (Windows PowerShell)
#
# Usage:
#   .\cleanup.ps1                          # Mode SAFE (par defaut)
#   .\cleanup.ps1 -Aggressive              # Mode AGRESSIF (confirmation)
#   .\cleanup.ps1 -DryRun                  # Simulation
#   .\cleanup.ps1 -DryRun -Aggressive      # Simulation mode agressif
#
# Planification Windows (Planificateur de taches):
#   schtasks /create /tn "TradingBotCleanup" /tr "powershell -ExecutionPolicy Bypass -File C:\chemin\cleanup.ps1" /sc monthly /d 1 /st 03:00
###############################################################################

param(
    [switch]$DryRun,
    [switch]$Aggressive,
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

# --- Configuration ---
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CleanupLog = Join-Path $ProjectDir "cleanup.log"
$LogRetentionDays = 30
$MaxLogSizeMB = 100
$MinDiskFreeGB = 10
$script:TotalFreed = 0

if ($Help) {
    Write-Host "cleanup.ps1 - Script de nettoyage Trading Bot"
    Write-Host ""
    Write-Host "Usage:"
    Write-Host "  .\cleanup.ps1                     Mode SAFE (defaut)"
    Write-Host "  .\cleanup.ps1 -Aggressive         Mode AGRESSIF"
    Write-Host "  .\cleanup.ps1 -DryRun             Simulation"
    Write-Host "  .\cleanup.ps1 -DryRun -Aggressive Simulation agressif"
    exit 0
}

# --- Protected files/dirs (never delete) ---
$ProtectedFiles = @(
    ".env", ".env.local", ".env.example", ".env.backup.pre-safety",
    "safe-live.env", "run.py", "package.json", "package-lock.json",
    "tsconfig.json", "vite.config.ts", "docker-compose.prod.yml",
    "pytest.ini", "trades.db", "README.md", "CHANGELOG.md"
)

$ProtectedDirs = @(
    "backend", "core", "services", "config", "hooks", "components",
    "pages", "notifications", "observability", "orchestrator", "portfolio",
    "smc_futures", "scripts", "tools", "systemd", "docs", ".git", "tests"
)

# --- Utility functions ---
function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $Message"
    Write-Host $line
    Add-Content -Path $CleanupLog -Value $line -Encoding ASCII
}

function Write-Section {
    param([string]$Title)
    Write-Log "========================================================"
    Write-Log "  $Title"
    Write-Log "========================================================"
}

function Format-Size {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) { return "{0:N2} GB" -f ($Bytes / 1GB) }
    if ($Bytes -ge 1MB) { return "{0:N2} MB" -f ($Bytes / 1MB) }
    if ($Bytes -ge 1KB) { return "{0:N2} KB" -f ($Bytes / 1KB) }
    return "$Bytes B"
}

function Get-FolderSize {
    param([string]$Path)
    $result = (Get-ChildItem $Path -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum
    if ($null -eq $result) { return 0 }
    return $result
}

function Remove-SafeFile {
    param([string]$FilePath)
    $name = Split-Path $FilePath -Leaf
    if ($name -in $ProtectedFiles) {
        Write-Log "  PROTECTED (skip): $FilePath"
        return
    }
    $item = Get-Item $FilePath -ErrorAction SilentlyContinue
    if ($null -eq $item) { return }
    $size = $item.Length
    if ($DryRun) {
        Write-Log "  [DRY-RUN] Would delete: $FilePath ($(Format-Size $size))"
    } else {
        Remove-Item $FilePath -Force -ErrorAction SilentlyContinue
        Write-Log "  DELETED: $FilePath ($(Format-Size $size))"
    }
    $script:TotalFreed += $size
}

function Remove-SafeDir {
    param([string]$DirPath)
    $name = Split-Path $DirPath -Leaf
    if ($name -in $ProtectedDirs) {
        Write-Log "  PROTECTED (skip): $DirPath"
        return
    }
    $size = Get-FolderSize $DirPath
    if ($DryRun) {
        Write-Log "  [DRY-RUN] Would delete dir: $DirPath ($(Format-Size $size))"
    } else {
        Remove-Item $DirPath -Recurse -Force -ErrorAction SilentlyContinue
        Write-Log "  DELETED dir: $DirPath ($(Format-Size $size))"
    }
    $script:TotalFreed += $size
}

# --- Check disk space ---
function Test-DiskSpace {
    Write-Section "DISK SPACE CHECK"
    $drive = (Get-Item $ProjectDir).PSDrive.Name
    $disk = Get-PSDrive $drive
    $freeGB = [math]::Round($disk.Free / 1GB, 2)
    Write-Log "Free space: ${freeGB} GB (drive ${drive}:)"
    if ($freeGB -lt $MinDiskFreeGB) {
        Write-Log "WARNING: Disk space critical (<${MinDiskFreeGB} GB)"
    }
}

# --- Step 1: Python caches ---
function Clear-PythonCaches {
    Write-Section "PYTHON CACHES CLEANUP"
    $count = 0

    Get-ChildItem $ProjectDir -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\\.venv\\' -and $_.FullName -notmatch '\\node_modules\\' } |
        ForEach-Object {
            Remove-SafeDir $_.FullName
            $count++
        }
    Write-Log "  __pycache__ dirs processed: $count"

    Get-ChildItem $ProjectDir -Recurse -File -Filter "*.pyc" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\\.venv\\' -and $_.FullName -notmatch '\\node_modules\\' } |
        ForEach-Object { Remove-SafeFile $_.FullName }

    Get-ChildItem $ProjectDir -Recurse -Directory -Filter ".pytest_cache" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\\.venv\\' } |
        ForEach-Object { Remove-SafeDir $_.FullName }

    foreach ($cache in @(".mypy_cache", ".ruff_cache", ".cache")) {
        $target = Join-Path $ProjectDir $cache
        if (Test-Path $target) { Remove-SafeDir $target }
    }

    Write-Log "  Python caches cleaned"
}

# --- Step 2: Old logs (>30 days) ---
function Clear-OldLogs {
    Write-Section "OLD LOGS CLEANUP (>$LogRetentionDays days)"
    $cutoff = (Get-Date).AddDays(-$LogRetentionDays)

    $logDirs = @(
        (Join-Path $ProjectDir "logs"),
        (Join-Path $ProjectDir "reports")
    )

    foreach ($logDir in $logDirs) {
        if (Test-Path $logDir) {
            Write-Log "  Scanning: $logDir"
            Get-ChildItem $logDir -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Extension -in @(".log", ".jsonl", ".txt") -and $_.LastWriteTime -lt $cutoff } |
                ForEach-Object { Remove-SafeFile $_.FullName }
        }
    }

    Get-ChildItem $ProjectDir -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -eq ".log" -and $_.LastWriteTime -lt $cutoff } |
        ForEach-Object { Remove-SafeFile $_.FullName }

    Write-Log "  Old logs cleaned"
}

# --- Step 3: Large log rotation ---
function Invoke-LogRotation {
    Write-Section "LOG ROTATION (>${MaxLogSizeMB} MB)"
    $maxBytes = $MaxLogSizeMB * 1MB

    Get-ChildItem $ProjectDir -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Extension -in @(".log", ".jsonl") -and
            $_.Length -gt $maxBytes -and
            $_.FullName -notmatch '\\\.venv\\' -and
            $_.FullName -notmatch '\\node_modules\\'
        } |
        ForEach-Object {
            $origSize = $_.Length
            if ($DryRun) {
                Write-Log "  [DRY-RUN] Would truncate: $($_.FullName) ($(Format-Size $origSize) -> keep last 10MB)"
            } else {
                $keepBytes = 10MB
                $content = [System.IO.File]::ReadAllBytes($_.FullName)
                $start = [math]::Max(0, $content.Length - $keepBytes)
                $trimmed = New-Object byte[] ($content.Length - $start)
                [Array]::Copy($content, $start, $trimmed, 0, $trimmed.Length)
                [System.IO.File]::WriteAllBytes($_.FullName, $trimmed)
                Write-Log "  TRUNCATED: $($_.FullName) ($(Format-Size $origSize) -> 10 MB)"
            }
            $script:TotalFreed += ($origSize - 10MB)
        }

    Write-Log "  Log rotation done"
}

# --- Step 4: Temp files ---
function Clear-TempFiles {
    Write-Section "TEMP FILES CLEANUP"

    $tempPatterns = @(
        "test_out*.txt", "test_err*.txt", "test_result*.txt",
        "test_output*.txt", "test_output*.log", "tests_output*.txt",
        "test_full_audit*.txt", "test_safeguards_*.txt",
        "test_mb_*.txt", "pytest_out*.txt", "pytest_full_run*.txt",
        "pytest_phase*.txt", "chantier*_output.txt", "verify_output.txt",
        "failures_list.txt", "found_files.txt", "stress_results_*.csv",
        "asym_results_*.csv"
    )

    foreach ($pattern in $tempPatterns) {
        Get-ChildItem $ProjectDir -File -Filter $pattern -ErrorAction SilentlyContinue |
            Where-Object { $_.DirectoryName -eq $ProjectDir } |
            ForEach-Object { Remove-SafeFile $_.FullName }
    }

    Get-ChildItem $ProjectDir -File -Filter "tmp_*.py" -ErrorAction SilentlyContinue |
        Where-Object { $_.DirectoryName -eq $ProjectDir } |
        ForEach-Object { Remove-SafeFile $_.FullName }

    # Massive files (> 1GB)
    Get-ChildItem $ProjectDir -File -ErrorAction SilentlyContinue |
        Where-Object { $_.DirectoryName -eq $ProjectDir -and $_.Length -gt 1GB } |
        ForEach-Object {
            $fname = $_.Name
            if ($fname -notin $ProtectedFiles) {
                Write-Log "  MASSIVE FILE detected: $($_.FullName) ($(Format-Size $_.Length))"
                if ($Aggressive) {
                    Remove-SafeFile $_.FullName
                } else {
                    Write-Log "  SAFE mode: file kept. Use -Aggressive to delete."
                }
            }
        }

    Write-Log "  Temp files cleaned"
}

# --- Step 5: Docker ---
function Clear-Docker {
    Write-Section "DOCKER CLEANUP"

    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCmd) {
        Write-Log "  Docker not found, skipping"
        return
    }

    try {
        $null = docker info 2>&1
        if ($LASTEXITCODE -ne 0) { throw "not running" }
    } catch {
        Write-Log "  Docker daemon not accessible, skipping"
        return
    }

    if ($DryRun) {
        Write-Log "  [DRY-RUN] Docker system df:"
        docker system df 2>$null
        return
    }

    if ($Aggressive) {
        Write-Log "  AGGRESSIVE: docker system prune -a --volumes"
        docker system prune -a --volumes -f 2>$null
    } else {
        Write-Log "  SAFE: docker prune (no volumes)"
        docker container prune -f 2>$null
        docker image prune -f 2>$null
        docker network prune -f 2>$null
        docker builder prune -f 2>$null
    }

    Write-Log "  Docker cleaned"
}

# --- Step 6: Old market data ---
function Clear-MarketData {
    Write-Section "OLD MARKET DATA CLEANUP"
    $cutoff = (Get-Date).AddDays(-$LogRetentionDays)

    $dataDirs = @(
        (Join-Path $ProjectDir "market_data"),
        (Join-Path $ProjectDir "data")
    )

    foreach ($dir in $dataDirs) {
        if (Test-Path $dir) {
            Write-Log "  Scanning: $dir"
            Get-ChildItem $dir -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.Extension -in @(".csv", ".json", ".parquet", ".pkl") -and
                    $_.LastWriteTime -lt $cutoff
                } |
                ForEach-Object { Remove-SafeFile $_.FullName }
        }
    }

    Write-Log "  Old market data cleaned"
}

# --- Step 7 (Aggressive): node_modules ---
function Clear-NodeModules {
    if (-not $Aggressive) { return }
    Write-Section "NODE_MODULES CLEANUP (AGGRESSIVE)"
    $nm = Join-Path $ProjectDir "node_modules"
    if (Test-Path $nm) {
        $size = Get-FolderSize $nm
        if ($DryRun) {
            Write-Log "  [DRY-RUN] Would delete node_modules ($(Format-Size $size))"
            Write-Log "  [DRY-RUN] -> Run npm install after"
        } else {
            Remove-Item $nm -Recurse -Force -ErrorAction SilentlyContinue
            Write-Log "  DELETED node_modules ($(Format-Size $size))"
            Write-Log "  Run npm install to reinstall dependencies"
        }
        $script:TotalFreed += $size
    }
}

# --- Step 8: Build artifacts ---
function Clear-BuildArtifacts {
    Write-Section "BUILD ARTIFACTS CLEANUP"
    foreach ($dir in @("dist", "build", ".next", ".nuxt")) {
        $target = Join-Path $ProjectDir $dir
        if (Test-Path $target) { Remove-SafeDir $target }
    }
    Write-Log "  Build artifacts cleaned"
}

###############################################################################
# --- MAIN EXECUTION ---
###############################################################################

$modeText = "SAFE"
if ($Aggressive) { $modeText = "AGGRESSIVE" }
$dryText = "NO"
if ($DryRun) { $dryText = "YES" }

Write-Log "============================================================"
Write-Log "  TRADING BOT CLEANUP - $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Log "  Mode: $modeText  |  Dry-run: $dryText"
Write-Log "============================================================"
Write-Log "Project dir: $ProjectDir"

# Confirm aggressive mode
if ($Aggressive -and (-not $DryRun)) {
    Write-Host ""
    Write-Host "AGGRESSIVE MODE enabled. This will:" -ForegroundColor Red
    Write-Host "   - Delete ALL Docker images, volumes, containers"
    Write-Host "   - Delete node_modules"
    Write-Host "   - Delete massive files (>1GB)"
    Write-Host "   - Delete dist/"
    Write-Host ""
    $confirm = Read-Host "Confirm? (type YES in caps)"
    if ($confirm -ne "YES") {
        Write-Log "Cancelled by user"
        exit 0
    }
}

Test-DiskSpace
Clear-PythonCaches
Clear-OldLogs
Invoke-LogRotation
Clear-TempFiles
Clear-Docker
Clear-MarketData
Clear-NodeModules
Clear-BuildArtifacts

Write-Log ""
Write-Log "============================================================"
Write-Log "  SUMMARY"
Write-Log "============================================================"
Write-Log "Estimated space freed: $(Format-Size $script:TotalFreed)"
Write-Log "Mode: $modeText"
Write-Log "Dry-run: $dryText"

Test-DiskSpace
Write-Log "Cleanup completed successfully"
