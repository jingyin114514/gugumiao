@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if not defined PYEXE where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE if exist "C:\Users\34807\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "PYEXE=C:\Users\34807\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if not defined PYEXE (
  echo [错误] 未找到 Python，请先安装 Python 3.8+ 或创建 .venv
  pause
  exit /b 1
)

set PYTHONUTF8=1
%PYEXE% main.py %*
pause
