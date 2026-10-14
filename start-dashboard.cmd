@echo off
setlocal
cd /d "%~dp0"
if exist "%USERPROFILE%\.platformio\penv\Scripts\python.exe" (
  "%USERPROFILE%\.platformio\penv\Scripts\python.exe" "%~dp0dashboard\server.py" --open %*
) else (
  py -3 "%~dp0dashboard\server.py" --open %*
)
if errorlevel 1 pause
