@echo off
cd /d "%~dp0"

REM Create virtual environment if it doesn't exist
if not exist ".venv\" (
    python -m venv .venv
)

REM Activate the virtual environment
call .venv\Scripts\activate.bat

REM Install requirements if requirements.txt exists
if exist requirements.txt (
    pip install --upgrade pip
    pip install -r requirements.txt
)

REM Run main.py headless (no console window)
start /min "" pythonw app\main.py