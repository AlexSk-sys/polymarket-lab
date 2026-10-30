@echo off
REM Daily longshot paper cycle. Core: 0.02-0.10 YES -> buy NO, 3-8 days.
REM Satellite: 0.40-0.60 YES -> buy NO, experimental, smaller stake.
cd /d "%~dp0"

".venv\Scripts\python.exe" -m longshot.cli resolve --ledger longshot_paper.json >> longshot_cycle.log 2>&1
".venv\Scripts\python.exe" -m longshot.cli scan --ledger longshot_paper.json --stake 20 --max-new 5 --max-open 20 --days-min 3 --days-max 8 --min-price 0.02 --max-price 0.10 --buffer 0.005 --strategy core >> longshot_cycle.log 2>&1
".venv\Scripts\python.exe" -m longshot.cli report --ledger longshot_paper.json --out longshot_report.json >> longshot_cycle.log 2>&1

".venv\Scripts\python.exe" -m longshot.cli resolve --ledger longshot_mid_paper.json >> longshot_cycle.log 2>&1
".venv\Scripts\python.exe" -m longshot.cli scan --ledger longshot_mid_paper.json --stake 10 --max-new 5 --max-open 10 --days-min 3 --days-max 8 --min-price 0.40 --max-price 0.60 --buffer 0.01 --strategy mid >> longshot_cycle.log 2>&1
".venv\Scripts\python.exe" -m longshot.cli report --ledger longshot_mid_paper.json --out longshot_mid_report.json >> longshot_cycle.log 2>&1
