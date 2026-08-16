@echo off
setlocal

REM Run from repository root (location of this script)
cd /d "%~dp0"

REM Start review frontend server in a separate terminal window
start "Review Frontend Server" cmd /k "cd /d "%~dp0" && pixi run python src\review_frontend.py"

REM Small delay so server can start listening
timeout /t 2 /nobreak >nul

REM Open browser to review app
start "" "http://localhost:8787"

endlocal
