@echo off
rem Pregame viewer (Windows, double-click friendly): runs start.ps1, passing arguments through.
rem   start.bat             online (MongoDB Atlas, config in viewer.env)
rem   start.bat --offline   serve the snapshot in fixtures\
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
