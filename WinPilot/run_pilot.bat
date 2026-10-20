@echo off
rem Starts WinPilot. Pass --hidden to start in the tray (e.g. from the Startup folder).
rem Uses WinControlMCP's Python environment, creating it on first run.
setlocal
set "VENV=%~dp0..\WinControlMCP\.venv\Scripts"
if not exist "%VENV%\python.exe" call "%~dp0..\WinControlMCP\setup.cmd" || exit /b 1
"%VENV%\python.exe" -c "import anthropic, pystray" 2>nul || (
    "%VENV%\python.exe" -m pip install -r "%~dp0requirements.txt" || exit /b 1
)
start "" "%VENV%\pythonw.exe" "%~dp0pilot.py" %*
