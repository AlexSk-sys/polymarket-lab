@echo off
REM Live dashboard: serves the folder and marks open positions to market
REM on every poll. Leave this window open.
cd /d "%~dp0"
start "" http://127.0.0.1:8765/dashboard.html
".venv\Scripts\python.exe" serve.py --port 8765 --stake 20
