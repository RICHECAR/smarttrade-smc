@echo off
echo ==========================================
echo SMC Trading Bot - Full Stack Startup
echo ==========================================
echo.

REM 1. Start Python SMC Engine (FastAPI backend on port 8000)
echo [1/3] Starting SMC Engine (FastAPI) on port 8000...
start "SMC Engine (FastAPI:8000)" cmd /k ".\.venv\Scripts\activate && python run.py"

REM Wait a moment for backend to start
timeout /t 3 /nobreak >nul

REM 2. Start Node Backend Proxy (optional, on port 5000)
echo [2/3] Starting Node Backend Proxy on port 5000...
start "Node Backend (Express:5000)" cmd /k "cd backend && node server.cjs"

REM 3. Start React Frontend (Vite on port 3000)
echo [3/3] Starting React Frontend on port 3000...
start "React Frontend (Vite:3000)" cmd /k "npm run dev"

echo.
echo ==========================================
echo All services starting...
echo.
echo   - FastAPI Backend: http://localhost:8000
echo   - Node Proxy:      http://localhost:5000
echo   - React Frontend:  http://localhost:3000
echo.
echo Test backend: curl http://localhost:8000/api/health
echo ==========================================
pause
