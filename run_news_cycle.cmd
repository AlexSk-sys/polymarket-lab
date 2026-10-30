@echo off
REM Full news cycle: Telegram scan -> paper ledger -> settlement/report.
REM Scheduled every 15 minutes; launch through run_news_cycle_hidden.vbs.
cd /d "%~dp0"
"C:\Users\AgentPC\source\repos\telegram-mcp\.venv\Scripts\python.exe" tg_news.py >> news_cycle.log 2>&1
".venv\Scripts\python.exe" news_strategy.py >> news_cycle.log 2>&1
".venv\Scripts\python.exe" pm_lab.py resolve news_paper.json --write-back --report news_paper_report.json --max-abs-move 0.05 >> news_cycle.log 2>&1
