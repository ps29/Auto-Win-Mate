# WinPilot

A tray app for telling an AI what to do on your Windows desktop. You type an instruction such as "open Notepad and write a shopping list" or "in MyApp, fill the Name field with Pravesh and press Save". The model plans the steps and carries them out through the [WinControl](../WinControlMCP) tools. You watch each step in a live feed.

## Run
```bat
run_pilot.bat            :: opens the window (creates the Python environment on first run)
run_pilot.bat --hidden   :: starts in the tray, e.g. from shell:startup
```
| Shortcut | Action |
|---|---|
| Ctrl+Alt+Space | Show / hide the window |
| Ctrl+Alt+X | Stop the running task |
| Mouse in the top-left screen corner | Failsafe: every input tool refuses to act |

Closing the window keeps WinPilot running in the tray. Use the tray menu's **Quit** to exit.

## Models
Pick the provider in **Settings**:
- **Claude API:** `claude-opus-5-5` by default (you can switch to Sonnet or Haiku). Leave the key blank to use `ANTHROPIC_API_KEY` or an `ant auth login` profile. A key typed into Settings is stored encrypted with Windows DPAPI in `%APPDATA%\WinPilot\settings.json`.
- **Lemonade (local):** any model served by [Lemonade Server](https://lemonade-server.ai); **Refresh** lists the installed ones. Use a vision model such as `Qwen3-VL-8B-Instruct-GGUF` so screenshots work, and set `lemonade config set ctx_size=32768`. Small models handle short, specific tasks best.

The conversation carries over between tasks, so follow-ups like "now save it" work. **New chat** starts fresh.

## Watching and controlling
- **Feed:** what the model says, every tool call, and a preview of each result.
- **Agent's view:** the latest screenshot the model took. Tick **Live view** to see a desktop thumbnail that refreshes every 1.5 s.
- **Approval:** clicks, typing, key presses, scrolling and app launches each show an **Allow / Allow all this task / Deny** bar first. Reading windows and taking screenshots don't ask. You can turn approval off in Settings.
- WinPilot leaves itself out of screenshots and window lists, and hides while an input action runs, so its own window never gets clicked or typed into.

All the WinControl safety controls still apply: `WINCONTROL_ALLOWED_EXES`, protection against overwriting text, the failsafe corner, and the log in `WinControlMCP\actions.log`.

## Testing
```bat
..\WinControlMCP\.venv\Scripts\python.exe tests\agent_test.py
```
The test replaces the model with a scripted plan and drives WinControlMCP's throwaway test window. It covers allow, deny and allow-all.
