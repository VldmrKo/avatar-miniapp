@echo off
chcp 65001 >nul
rem Local run of the TELEGRAM bot. ASCII-only on purpose: cmd decodes .bat
rem with the OEM codepage.
rem
rem Everything is set here and nowhere else: no need to edit secrets.env and
rem no way to forget it set to telegram afterwards. Only the bot token lives
rem in secrets.env, as TELEGRAM_BOT_TOKEN.
rem
rem Own data directory on purpose: user ids in MAX and Telegram collide, and
rem a shared directory would glue two different people into one conversation.
setlocal
set "APP=%~dp0"
set "MINIAPP_MESSENGER=telegram"
set "MINIAPP_CHAT_ENABLED=1"
set "MINIAPP_DATA_DIR=%APP%data-tg"
set "MINIAPP_PORT=8082"

rem No lock: one process on this machine, nothing to negotiate with.
set "MINIAPP_H3_LOCK="

rem Corporate networks here inspect TLS with their own root certificate.
rem Windows trusts it, Python does not, and every call to api.telegram.org
rem dies with "self-signed certificate in certificate chain". This makes
rem Python read the same store the browser does. NOT the same as turning
rem verification off - that is exactly the door the inspection exists for.
set "MINIAPP_TRUST_OS_CERTS=1"

call "%APP%run-local.bat" %*
endlocal
