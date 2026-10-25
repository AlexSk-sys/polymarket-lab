@echo off
REM Live dashboard: serves the folder and marks open positions to market
REM on every poll. Leave this window open.
cd /d "%~dp0"
".venv\Scripts\python.exe" pm_lab.py dashboard --stake 20
start "" http://127.0.0.1:8765/dashboard_local.html
".venv\Scripts\python.exe" serve.py --port 8765 --stake 20
