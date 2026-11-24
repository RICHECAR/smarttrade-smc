@echo off
echo ==========================================
echo SMC Trading Bot - Restart All Services
echo ==========================================
echo.

REM 1. Stop all services
echo Stopping all services...
call "%~dp0stop_all.bat" <nul

REM 2. Wait a moment for ports to be released
echo.
echo Waiting for ports to be released...
timeout /t 3 /nobreak >nul

REM 3. Start all services
echo.
echo Starting all services...
call "%~dp0start_all.bat"
