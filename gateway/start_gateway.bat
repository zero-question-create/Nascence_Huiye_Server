@echo off
REM Start the unified Nascence server (admin panel + web client, one-click)
REM Admin:  http://127.0.0.1:8787/admin
REM Client: http://127.0.0.1:8787/  (or standalone web client on port 8987)
cd /d "%~dp0.."
set VENV=venv
if not exist "%VENV%\Scripts\python.exe" (
  echo [ERROR] Virtual env not found: %VENV%\
  pause
  exit /b 1
)
"%VENV%\Scripts\python.exe" webui.py
pause
