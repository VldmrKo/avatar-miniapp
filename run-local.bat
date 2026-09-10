@echo off
chcp 65001 >nul
rem Local run. Web part in the browser at http://127.0.0.1:8081
rem ASCII-only on purpose: cmd decodes .bat with the OEM codepage.
set "PYTHONUTF8=1"
set "APP=%~dp0"
set "VENV=%APP%.venv"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%PY%" (
  echo Creating virtual environment...
  python -m venv "%VENV%" || exit /b 1
  "%PY%" -m pip install --upgrade pip --quiet
  "%PY%" -m pip install --quiet -e "%APP%..\avatar-core" || exit /b 1
  "%PY%" -m pip install --quiet aiohttp maxapi==1.2.2 pillow pytest || exit /b 1
)

rem The venv may predate the Telegram bot. Installing a package that is
rem already there costs nothing; a missing one costs a confusing ImportError
rem at startup, and only for whoever switches messengers.
"%PY%" -c "import aiogram, truststore" >nul 2>&1
if errorlevel 1 (
  echo installing aiogram and truststore, one moment...
  "%PY%" -m pip install --quiet "aiogram>=3.13,<4" truststore || exit /b 1
)

set "PYTHONPATH=%APP%src"
"%PY%" -m avatar_miniapp %*
