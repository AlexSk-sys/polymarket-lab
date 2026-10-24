@echo off
REM Daily forward cycle. Nothing here is re-fit: prices are stamped now,
REM outcomes are filled in later. Run it, close the window, forget about it.
cd /d "%~dp0"

echo === 1. paper bets on near-certain favourites ===
".venv\Scripts\python.exe" pm_lab.py paper record --threshold 0.70 --min-price 0.10 --max-price 0.90 --min-volume 10000 --max 300
".venv\Scripts\python.exe" pm_lab.py paper settle

echo === 2. fresh entries of no-rebate wallets (60m horizon) ===
".venv\Scripts\python.exe" pm_lab.py signals --watch watch_norebate.json --min-usdc 2000 --min-price 0.10 --max-price 0.90 --max-age 21600 --out signals_forward.json

echo === 3. fresh entries of the single wallet ===
".venv\Scripts\python.exe" pm_lab.py signals --watch watch_one.json --min-usdc 500 --min-price 0.10 --max-price 0.90 --max-age 21600 --out signals_forward_one.json

echo === 4. reports ===
".venv\Scripts\python.exe" pm_lab.py paper report
".venv\Scripts\python.exe" pm_lab.py evaluate signals_forward.json --horizons 60,240
".venv\Scripts\python.exe" pm_lab.py evaluate signals_forward_one.json --horizons 60,240,1440
