@echo off
rem Builds WinPilot.exe and WinControlMCP.exe (dist\WinPilot\) and the installer
rem (dist\WinPilot-Setup-<version>.exe).
rem   packaging\build.cmd            build version 2.0.0
rem   packaging\build.cmd 2.1.0      build another version
rem Needs: WinControlMCP\setup.cmd run once, and Inno Setup 6 (winget install JRSoftware.InnoSetup).
setlocal
set "VERSION=%~1"
if "%VERSION%"=="" set "VERSION=2.0.0"
cd /d "%~dp0"
set "PY=..\WinControlMCP\.venv\Scripts\python.exe"
if not exist "%PY%" call ..\WinControlMCP\setup.cmd || exit /b 1
"%PY%" -m pip install -q -r ..\WinPilot\requirements.txt "pyinstaller>=6.22" || exit /b 1

rem Icon and Windows version resource, generated from the app's own tray icon and the version above.
"%PY%" make_assets.py %VERSION% || exit /b 1

"%PY%" -m PyInstaller --noconfirm --clean --distpath ..\dist --workpath ..\build winpilot.spec || exit /b 1
echo Built ..\dist\WinPilot\WinPilot.exe and WinControlMCP.exe

set "ISCC="
for %%P in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
    if exist %%P set "ISCC=%%~P"
)
if not defined ISCC (
    echo Inno Setup 6 not found, so no installer was made. Install it with:
    echo     winget install JRSoftware.InnoSetup
    exit /b 1
)
"%ISCC%" /Q /DAppVersion=%VERSION% WinPilot.iss || exit /b 1
echo Built ..\dist\WinPilot-Setup-%VERSION%.exe
