@echo off
chcp 65001 >nul
title TokenFurnace - LLM Token Console
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Python not found in PATH.
  echo Install Python 3.9+ from https://www.python.org/downloads/
  echo and make sure "Add python.exe to PATH" is checked.
  echo.
  pause
  exit /b 1
)

echo Starting TokenFurnace...
echo Console will open in your browser automatically.
echo Press Ctrl+C in this window to stop.
echo.

python run.py
if errorlevel 1 (
  echo.
  echo [ERROR] TokenFurnace exited with an error. See messages above.
  pause
)
