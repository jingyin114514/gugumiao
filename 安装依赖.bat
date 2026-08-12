@echo off
chcp 65001 >nul
title 灯号监控 · 安装依赖（首次运行请双击）
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有找到 Python。
  echo 请先到 https://www.python.org/downloads/ 安装 Python 3.8 以上版本，
  echo 安装时务必勾选 "Add Python to PATH"。
  pause
  exit /b 1
)

if not exist ".venv" (
  echo 正在创建虚拟环境 .venv ...
  python -m venv .venv
)

echo 正在安装依赖（akshare、pandas 等，约 1~3 分钟）...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

if not exist "config.json" (
  echo 首次使用：由 config.example.json 生成默认配置 config.json
  copy /y config.example.json config.json >nul
)

echo.
echo 安装完成！以后直接双击「启动面板.bat」即可。
pause
