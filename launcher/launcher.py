"""AutoWinMate.exe: a small window that opens WinPilot (AI desktop agent) or ClickBot (auto-clicker).

Packaged, it starts the sibling WinPilot.exe / ClickBot.exe; from source it starts the scripts with the
WinControlMCP virtual environment's pythonw.
"""
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent.parent
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: outlive the launcher

APPS = [
    ("WinPilot", "Tell an AI what to do on your desktop", "WinPilot.exe", ROOT / "WinPilot" / "pilot.py"),
    ("ClickBot", "Record and replay clicks and keystrokes", "ClickBot.exe", ROOT / "ClickBot" / "clickbot.py"),
]


def command(exe, script):
    if FROZEN:
        return [str(ROOT / exe)]
    pythonw = ROOT / "WinControlMCP" / ".venv" / "Scripts" / "pythonw.exe"
    return [str(pythonw if pythonw.exists() else Path(sys.executable).with_name("pythonw.exe")), str(script)]


def main():
    root = tk.Tk()
    root.title("Auto-Win-Mate")
    root.resizable(False, False)
    status = tk.StringVar(value="Pick an app to open.")

    def launch(name, exe, script):
        cmd = command(exe, script)
        try:
            subprocess.Popen(cmd, cwd=str(Path(cmd[-1]).parent), creationflags=DETACHED, close_fds=True)
        except OSError as e:
            status.set(f"Couldn't start {name}: {e.strerror or e}")
            return
        status.set(f"Opened {name}.")

    frame = ttk.Frame(root, padding=20)
    frame.pack()
    ttk.Label(frame, text="Auto-Win-Mate", font=("Segoe UI", 16, "bold")).grid(columnspan=2, pady=(0, 14))
    for row, (name, blurb, exe, script) in enumerate(APPS, start=1):
        ttk.Button(frame, text=f"Open {name}", width=18,
                   command=lambda n=name, e=exe, s=script: launch(n, e, s)).grid(row=row, column=0, pady=4, sticky="w")
        ttk.Label(frame, text=blurb, foreground="#555").grid(row=row, column=1, padx=(12, 0), sticky="w")
    ttk.Label(frame, textvariable=status).grid(columnspan=2, pady=(14, 0), sticky="w")
    root.mainloop()


if __name__ == "__main__":
    main()
