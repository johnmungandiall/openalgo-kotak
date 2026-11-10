@echo off

:: Check for Administrator privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

:: Set timezone to India Standard Time (UTC+05:30)
tzutil /s "India Standard Time"

:: Synchronize Windows time
w32tm /resync

:: Start OpenAlgo
cd /d "C:\Users\John\Downloads\qwantonomous-flattrade-fix-1.0.3\qwantonomous-flattrade-fix-1.0.3"
uv run app.py

pause