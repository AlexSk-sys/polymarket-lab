@echo off
REM Hourly forward capture. 60-minute horizon settles fast, so this produces
REM a verdict in 1-3 days instead of weeks.
REM Schedule with Task Scheduler, repeat every 1 hour.
cd /d "%~dp0"

".venv\Scripts\python.exe" pm_lab.py signals --watch watch_big.json --min-usdc 2000 --min-price 0.10 --max-price 0.90 --max-age 7200 --append --out signals_hourly.json

REM --write-back persists the computed returns into the file, otherwise the
REM dashboard and the next run would both see zeros
".venv\Scripts\python.exe" pm_lab.py evaluate signals_hourly.json --horizons 60,240 --write-back

REM refresh the live dashboard payload (the open page polls it every 30 s)
".venv\Scripts\python.exe" pm_lab.py dashboard --stake 20

REM static snapshots, for the record
".venv\Scripts\python.exe" pm_lab.py chart --file signals_hourly.json --horizon 60 --stake 20 --png equity_forward_60m.png
".venv\Scripts\python.exe" pm_lab.py chart --file paper.jsonl --stake 20 --png equity_paper.png
