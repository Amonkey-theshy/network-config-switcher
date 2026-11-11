@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" network_switcher.py
) else (
    python network_switcher.py
)
if errorlevel 1 pause
