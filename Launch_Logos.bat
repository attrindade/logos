@echo off
setlocal
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\python.exe" main.py
exit
