@echo off
chcp 65001 >nul
title Stock Light Monitor
cd /d "%~dp0"

set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

set PYTHONUTF8=1
%PYEXE% dashboard.py
echo.
echo Panel stopped. If browser did not open, visit http://127.0.0.1:8765/
echo If it says the panel is already running, just refresh the browser.
pause
