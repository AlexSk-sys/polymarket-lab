@echo off
REM Daily paper cycle. Broad longshot scanning is PAUSED after the 2025-2026
REM tag out-of-sample failed. Resolve existing positions and report only.
cd /d "%~dp0"

".venv\Scripts\python.exe" -m longshot.cli resolve --ledger longshot_paper.json >> longshot_cycle.log 2>&1
".venv\Scripts\python.exe" -m longshot.cli report --ledger longshot_paper.json --out longshot_report.json >> longshot_cycle.log 2>&1

".venv\Scripts\python.exe" -m longshot.cli resolve --ledger longshot_mid_paper.json >> longshot_cycle.log 2>&1
".venv\Scripts\python.exe" -m longshot.cli report --ledger longshot_mid_paper.json --out longshot_mid_report.json >> longshot_cycle.log 2>&1
