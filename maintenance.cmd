@echo off
rem Housekeeping for Auto-Win-Mate: deletes old WinPilot activity logs, rotates the action log,
rem clears Python and pip caches, and checks that the Python environment is healthy.
rem   maintenance.cmd                 keep 30 days of activity logs
rem   maintenance.cmd --keep-days 7   keep 7 days
setlocal
set "PY=%~dp0WinControlMCP\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo The Python environment is missing. Run WinControlMCP\setup.cmd first.
    exit /b 1
)
"%PY%" "%~dp0WinPilot\housekeeping.py" --deep %*
