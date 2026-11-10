@echo off
title Qwantonomous Flattrade Fix

cd /d "C:\Users\John\Downloads\qwantonomous-flattrade-fix-1.0.3\qwantonomous-flattrade-fix-1.0.3"

echo Starting Qwantonomous Flattrade Fix...
echo.

uv run app.py

echo.
echo Application stopped.
pause