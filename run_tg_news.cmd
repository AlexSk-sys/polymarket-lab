@echo off
REM Telegram news scan: reads tg_channel_registry.json, writes news_signals.json.
REM Uses the userbot session from telegram-mcp; run every 5-15 minutes.
cd /d "%~dp0"
"C:\Users\AgentPC\source\repos\telegram-mcp\.venv\Scripts\python.exe" tg_news.py
