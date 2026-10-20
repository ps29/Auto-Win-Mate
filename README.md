# Auto-Win-Mate

Windows 11 desktop automation, in two parts:

| Folder | What it is |
|---|---|
| [`ClickBot/`](ClickBot) | A desktop GUI auto-clicker. Record or build click/keyboard scripts, target a specific window, and replay them with hotkeys. |
| [`WinControlMCP/`](WinControlMCP) | An [MCP](https://modelcontextprotocol.io) server that lets AI agents see and control the desktop: list windows, read an app's buttons and fields through UI Automation, click, type, scroll and take screenshots. It works with cloud models and with local models served by [Lemonade Server](https://lemonade-server.ai). |
| [`WinPilot/`](WinPilot) | A tray app where you tell an AI (Claude API or a local Lemonade model) what to do in plain language. It carries the task out with the WinControl tools while you watch, and asks before each click or keystroke. |

Both use the Win32 API directly (`SendInput`, window enumeration, low-level hooks). WinControlMCP reuses ClickBot's input code, and WinPilot reuses WinControlMCP's tools.

## Requirements
- Windows 10 or 11
- Python 3.10+ (tested on 3.14)

## ClickBot
No dependencies beyond the Python standard library.
```bat
ClickBot\run_clickbot.bat
```
| Hotkey | Action |
|---|---|
| F6 | Start / stop playback |
| F7 | Start / stop recording clicks |
| F8 | Capture the mouse position as a click step |
| Mouse in the top-left corner | Emergency stop |

Features:
- Clicks can be stored relative to the target window, so they still land correctly after the window moves.
- Playback can pause automatically while the target window isn't in front.
- Repeat count, speed and random offset/delay are adjustable.
- A quick-clicker mode clicks at a fixed interval.
- Scripts save and load as JSON.

## WinControlMCP
```bat
cd WinControlMCP
setup.cmd                          :: create .venv and install requirements
setup.cmd --register               :: ...and register the server with the `claude` agent CLI
```
To use it with any other MCP client, run `.venv\Scripts\python.exe server.py` over stdio.

See [WinControlMCP/README.md](WinControlMCP/README.md) for the tool list, safety controls and local-model setup.

## WinPilot
```bat
WinPilotun_pilot.bat
```
Type a task, approve each action as it comes up, and watch the feed. Ctrl+Alt+Space shows or hides the window. See [WinPilot/README.md](WinPilot/README.md).

## Safety
- **Failsafe:** move the mouse into the top-left screen corner to stop all input.
- **Allowlist:** `WINCONTROL_ALLOWED_EXES` limits which applications the MCP server may send input to.
- **Protected text:** `set_text` won't overwrite existing text unless the call explicitly asks to.
- **Log:** every MCP action is written to `WinControlMCP/actions.log`.
- **Admin apps:** Windows blocks input to apps running as Administrator unless the automation runs elevated too. The tools report this as an error rather than failing silently.
- **Your own work:** don't point automation at apps holding unsaved work you care about. Some apps (e.g. Notepad) restore previous sessions when they launch.
- **Games:** many online games ban simulated input. Check a game's rules first.

## Testing
```bat
cd WinControlMCP
.venv\Scripts\python.exe tests\smoke_test.py
```
The smoke test opens its own throwaway window and only sends input to it.
