@echo off
setlocal
cd /d "%~dp0"

echo [Logos] Setting up environment...
if not exist ".venv" (
    echo [Logos] Creating Python virtual environment...
    python -m venv .venv
)

echo [Logos] Installing dependencies...
call .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
call .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt

echo [Logos] Launching interactive TUI installer...
call .venv\Scripts\python.exe setup.py
pause
