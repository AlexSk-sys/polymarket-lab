@echo off
REM Daily longshot-NO paper cycle: resolve old positions, scan the 3-8 day
REM window, write the report. No orders; paper ledger only.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m longshot.cli resolve >> longshot_cycle.log 2>&1
".venv\Scripts\python.exe" -m longshot.cli scan --stake 20 --max-new 5 --max-open 20 --days-min 3 --days-max 8 >> longshot_cycle.log 2>&1
".venv\Scripts\python.exe" -m longshot.cli report --out longshot_report.json >> longshot_cycle.log 2>&1
