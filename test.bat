@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
set "APP=%~dp0"
set "PYTHONPATH=%APP%src"
"%APP%.venv\Scripts\python.exe" -m pytest "%APP%tests" -q
