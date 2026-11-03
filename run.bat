@echo off
cd /d "%~dp0"
set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%PY%" (
  echo Creating virtual environment...
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python not found in PATH.
    pause
    exit /b 1
  )
  python -m venv "%VENV%"
  if errorlevel 1 (
    echo Failed to create .venv
    pause
    exit /b 1
  )
)

"%PY%" -c "import paramiko, tkinterdnd2, mcp" 2>nul
if errorlevel 1 (
  echo Installing dependencies into .venv...
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
  )
)

"%PY%" server_connector.py
if errorlevel 1 pause
