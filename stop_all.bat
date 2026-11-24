@echo off
echo ==========================================
echo SMC Trading Bot - Stop All Services
echo ==========================================
echo.

REM Utilise les titres de fenêtre définis dans start_all.bat
REM pour cibler UNIQUEMENT les process du projet.

set "STOPPED=0"
set "ERRORS=0"

REM 1. Stop React Frontend (Vite)
echo [1/3] Stopping React Frontend (Vite:3000)...
taskkill /FI "WINDOWTITLE eq React Frontend (Vite:3000)*" /T /F >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo       [OK] React Frontend stopped
    set /a STOPPED+=1
) else (
    echo       [--] React Frontend was not running
)

REM 2. Stop Node Backend Proxy
echo [2/3] Stopping Node Backend (Express:5000)...
taskkill /FI "WINDOWTITLE eq Node Backend (Express:5000)*" /T /F >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo       [OK] Node Backend stopped
    set /a STOPPED+=1
) else (
    echo       [--] Node Backend was not running
)

REM 3. Stop Python SMC Engine (FastAPI)
echo [3/3] Stopping SMC Engine (FastAPI:8000)...
taskkill /FI "WINDOWTITLE eq SMC Engine (FastAPI:8000)*" /T /F >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo       [OK] SMC Engine stopped
    set /a STOPPED+=1
) else (
    echo       [--] SMC Engine was not running
)

echo.
echo ==========================================
echo Stop complete: %STOPPED% service(s) stopped
echo ==========================================
echo.

REM Vérification alternative: kill par port (si les fenêtres ont été fermées manuellement)
echo Checking for orphan processes on ports...

REM Port 8000 (FastAPI)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    echo   Found orphan process on port 8000 (PID: %%a) - killing...
    taskkill /PID %%a /F >nul 2>&1
)

REM Port 5000 (Node)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000 " ^| findstr "LISTENING"') do (
    echo   Found orphan process on port 5000 (PID: %%a) - killing...
    taskkill /PID %%a /F >nul 2>&1
)

REM Port 3000 (Vite)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":3000 " ^| findstr "LISTENING"') do (
    echo   Found orphan process on port 3000 (PID: %%a) - killing...
    taskkill /PID %%a /F >nul 2>&1
)

echo.
echo All services stopped.
echo ==========================================
pause
