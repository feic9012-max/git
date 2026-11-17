@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run install_web.cmd first.
    exit /b 1
)
".venv\Scripts\python.exe" rbpms_web.py --port 0 --open %*
