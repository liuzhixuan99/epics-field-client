@echo off
cd /d "%~dp0"
py -3 -c "import sys; sys.exit(sys.version_info < (3,11))" >nul 2>&1
if not errorlevel 1 (
  py -3 -m venv .windows-venv
) else (
  python -c "import sys; sys.exit(sys.version_info < (3,11))" >nul 2>&1
  if errorlevel 1 (
    echo Python 3.11+ was not found. Use the Windows portable application instead.
    exit /b 1
  )
  python -m venv .windows-venv
)
if errorlevel 1 exit /b 1
if exist wheelhouse (
  ".windows-venv\Scripts\python.exe" -m pip install --no-index --find-links wheelhouse -r requirements.txt
) else (
  ".windows-venv\Scripts\python.exe" -m pip install -r requirements.txt
)
if errorlevel 1 exit /b 1
echo Setup complete. Run start.cmd.
exit /b 0
