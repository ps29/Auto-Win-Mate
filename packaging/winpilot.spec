# PyInstaller spec: builds dist\WinPilot\ with four programs that share one set of libraries:
#   AutoWinMate.exe    the main launcher: a window with buttons that open WinPilot or ClickBot
#   WinPilot.exe       the tray app (no console)
#   ClickBot.exe       the auto-clicker
#   WinControlMCP.exe  the stdio MCP server, for Claude Code and other MCP clients
# Build with packaging\build.cmd (it also makes the installer).
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
PATHS = [str(ROOT / "WinPilot"), str(ROOT / "WinControlMCP"), str(ROOT / "ClickBot")]
ICON = str(Path(SPECPATH) / "winpilot.ico")
# OmniParser's torch (about 1 GB) stays out of the package; detect_elements is simply not offered.
EXCLUDES = ["torch", "torchvision", "numpy", "matplotlib", "scipy", "pandas", "IPython", "pytest"]
HIDDEN = ["win32com.client", "win32clipboard", "comtypes.stream"] + collect_submodules("pywinauto")
# uvicorn (the Claude Code bridge's HTTP server) picks its protocol and loop modules at runtime.
PILOT_HIDDEN = ["pystray._win32"] + collect_submodules("uvicorn")
DATAS = collect_data_files("sv_ttk")

pilot = Analysis([str(ROOT / "WinPilot" / "pilot.py")], pathex=PATHS, hiddenimports=HIDDEN + PILOT_HIDDEN,
                 datas=DATAS,
                 excludes=EXCLUDES)
server = Analysis([str(ROOT / "WinControlMCP" / "server.py")], pathex=PATHS, hiddenimports=HIDDEN,
                  excludes=EXCLUDES + ["pystray", "sv_ttk"])  # clickbot needs tkinter

launcher = Analysis([str(ROOT / "launcher" / "launcher.py")], excludes=EXCLUDES + ["pystray", "sv_ttk", "PIL"])
clickbot = Analysis([str(ROOT / "ClickBot" / "clickbot.py")], excludes=EXCLUDES + ["pystray", "sv_ttk", "PIL"])

launcher_exe = EXE(PYZ(launcher.pure), launcher.scripts, [], exclude_binaries=True, name="AutoWinMate", icon=ICON,
                   console=False, version=str(Path(SPECPATH) / "version_info.txt"))
clickbot_exe = EXE(PYZ(clickbot.pure), clickbot.scripts, [], exclude_binaries=True, name="ClickBot", icon=ICON,
                   console=False, version=str(Path(SPECPATH) / "version_info.txt"))
pilot_exe = EXE(PYZ(pilot.pure), pilot.scripts, [], exclude_binaries=True, name="WinPilot", icon=ICON,
                console=False, version=str(Path(SPECPATH) / "version_info.txt"))
server_exe = EXE(PYZ(server.pure), server.scripts, [], exclude_binaries=True, name="WinControlMCP", icon=ICON,
                 console=True, version=str(Path(SPECPATH) / "version_info.txt"))

COLLECT(launcher_exe, launcher.binaries, launcher.datas, clickbot_exe, clickbot.binaries, clickbot.datas,
        pilot_exe, pilot.binaries, pilot.datas, server_exe, server.binaries, server.datas, name="WinPilot")
