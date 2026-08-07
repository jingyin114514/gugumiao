@echo off
chcp 65001 >nul
title 灯号监控面板
cd /d "%~dp0"

set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

set PYTHONUTF8=1
%PYEXE% dashboard.py
echo.
echo 面板窗口已结束。若浏览器未自动打开，请手动访问 http://127.0.0.1:8765/
echo 若提示"面板已经在运行中"，说明面板已启动，直接刷新浏览器即可。
pause
