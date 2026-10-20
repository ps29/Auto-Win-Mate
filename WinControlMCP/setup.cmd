@echo off
rem Creates the Python environment for the WinControl MCP server.
rem Optional: pass --register to also register it (user scope) with the `claude` agent CLI.
setlocal
cd /d "%~dp0"

python -m venv .venv || exit /b 1
.venv\Scripts\python.exe -m pip install --upgrade pip >nul
.venv\Scripts\python.exe -m pip install -r requirements.txt || (
    rem A corrupted pip cache can make every package look unavailable.
    echo Retrying without pip's cache...
    .venv\Scripts\python.exe -m pip install --no-cache-dir -r requirements.txt || exit /b 1
)
echo Environment ready: %~dp0.venv

if /i "%~1"=="--register" (
    claude mcp remove wincontrol --scope user >nul 2>&1
    claude mcp add --scope user wincontrol -- "%~dp0.venv\Scripts\python.exe" "%~dp0server.py"
)
