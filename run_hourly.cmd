@echo off
REM Hourly forward capture. 60-minute horizon settles fast, so this produces
REM a verdict in 1-3 days instead of weeks.
REM Schedule with Task Scheduler, repeat every 1 hour.
cd /d "%~dp0"

REM ===================================================================
REM Capture is NOT here. It runs from the PolymarketCapture task every
REM 5 minutes (capture.py) because the copy edge decays with latency:
REM 72h replay +7.8% at 60s, +8.9% at 5m, +2.3% at the old ~30m lag.
REM This task only scores, renders and plots.
REM ===================================================================

REM broad research scan once an hour; the 5-minute strategy capture is separate
".venv\Scripts\python.exe" pm_lab.py signals --watch watch_big.json --min-usdc 2000 --min-price 0.10 --max-price 0.90 --max-age 7200 --live-age 3600 --max-new 200 --append --out signals_hourly.json

".venv\Scripts\python.exe" pm_lab.py evaluate signals_hourly.json --horizons 60,240 --write-back --one-per-market --max-adverse-move 0.02

REM hold-to-resolution strategy: broad big-wallet universe, final 0/1 payout
".venv\Scripts\python.exe" pm_lab.py resolve signals_hourly.json --write-back --report strategy_report.json --max-abs-move 0.02 --since 2026-10-28T12:10:00+00:00

REM refresh the live dashboard payload (the open page polls it every 30 s)
".venv\Scripts\python.exe" pm_lab.py dashboard --stake 20 --one-per-market --max-adverse-move 0.02

REM static snapshots, for the record
".venv\Scripts\python.exe" pm_lab.py chart --file signals_hourly.json --horizon 60 --stake 20 --png equity_forward_60m.png --one-per-market --max-adverse-move 0.02
".venv\Scripts\python.exe" pm_lab.py chart --file paper.jsonl --stake 20 --png equity_paper.png
