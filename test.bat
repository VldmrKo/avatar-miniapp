@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "APP=%~dp0"
set "PYTHONPATH=%APP%src"
set "PY=%APP%.venv\Scripts\python.exe"

rem ASCII-only on purpose: cmd decodes .bat with the OEM codepage.

if not exist "%PY%" (
  echo [ERROR] no virtual environment. Run run-local.bat once.
  exit /b 1
)

rem Portable ffmpeg. The tests call plain "ffmpeg", and it is NOT on PATH:
rem it lives in C:\Avatars\tools\ffmpeg\bin. Without this the media tests die
rem with "cannot find the file specified" and it looks like broken code.
for %%I in ("%APP%..") do set "ROOT=%%~fI"
if exist "%ROOT%\tools\ffmpeg\bin\ffmpeg.exe" set "PATH=%ROOT%\tools\ffmpeg\bin;%PATH%"

rem pytest-asyncio is what makes async tests run AT ALL. Without it pytest
rem does not stop - it reports every async test as "not natively supported"
rem and the run looks like a wall of failures in someone else's code.
rem So we check for it and install the dev extras once, instead of letting
rem the next person read fifty tracebacks about a missing plugin.
"%PY%" -c "import pytest_asyncio" >nul 2>&1
if errorlevel 1 (
  echo installing test dependencies, one moment...
  pushd "%APP%"
  "%PY%" -m pip install --quiet -e "..\avatar-core" || (popd & exit /b 1)
  "%PY%" -m pip install --quiet -e ".[dev]" || (popd & exit /b 1)
  popd
)

rem Same for aiogram: without it the Telegram tests do not fail, they are
rem SKIPPED - and a green run that quietly checked nothing is worse than red.
"%PY%" -c "import aiogram, truststore, aiohttp_socks" >nul 2>&1
if errorlevel 1 (
  echo installing aiogram and truststore, one moment...
  "%PY%" -m pip install --quiet "aiogram>=3.13,<4" truststore "aiohttp-socks>=0.8" || exit /b 1
)

"%PY%" -m pytest "%APP%tests" -q
endlocal
