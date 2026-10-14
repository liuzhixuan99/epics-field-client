@echo off
cd /d "%~dp0"
if not exist ".windows-venv\Scripts\python.exe" (
  call setup.cmd
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
".windows-venv\Scripts\python.exe" -m epics_field_client %*
if errorlevel 1 pause
