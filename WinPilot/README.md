# WinPilot

A tray app for telling an AI what to do on your Windows desktop. It controls the mouse and keyboard, and keeps an activity log of the desktop that it can read. You type an instruction such as "open Notepad and write a shopping list" or "in MyApp, fill the Name field with Pravesh and press Save". The model plans the steps and carries them out through the [WinControl](../WinControlMCP) tools. You watch each step in a live feed.

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
On first launch, **Settings** opens so you can pick the provider and press **Test connection**. Settings also has **Start with Windows** and **Create desktop shortcut**.
- **Claude API:** `claude-opus-5-5` by default (you can switch to Sonnet or Haiku). Leave the key blank to use `ANTHROPIC_API_KEY` or an `ant auth login` profile. A key typed into Settings is stored encrypted with Windows DPAPI in `%APPDATA%\WinPilot\settings.json`.
- **Lemonade (local):** any model served by [Lemonade Server](https://lemonade-server.ai); **Refresh** lists the installed ones. Use a vision model such as `Qwen3-VL-8B-Instruct-GGUF` so screenshots work, and set `lemonade config set ctx_size=32768`. Small models handle short, specific tasks best.

The conversation carries over between tasks, so follow-ups like "now save it" work. **New chat** starts fresh.

## Activity log
The **Activity** tab records what happens on the desktop, filterable by who did it:
- **Agent:** every action it takes or looks at, denials, and task start/end.
- **You:** clicks, scrolls, shortcuts (`ctrl+s`) and named keys (Enter, Tab...). Typed text is **only counted**
  ("typed 12 characters"), never stored. Turn this off in Settings, or pause recording from the tab.
- **Windows:** window switches, and windows opening and closing.

The agent sees what happened since its last task at the start of each new task, and can read more with its
`read_activity_log` tool. The log is also saved to `%APPDATA%\WinPilot\logs\activity-YYYY-MM-DD.jsonl`
(**Open log folder**).

## Watching and controlling
- **Feed:** what the model says, every tool call, and a preview of each result.
- **Agent's view:** the latest screenshot the model took. Tick **Live view** to see a desktop thumbnail that refreshes every 1.5 s.
- **Approval:** clicks, typing, key presses, scrolling, dragging and app launches each show an **Allow / Allow all this task / Deny** bar first. Reading windows, hovering, checking the cursor and taking screenshots don't ask. You can turn approval off in Settings.
- **Cursor control:** the agent clicks, hovers (`move_mouse`), drags, scrolls and types. It moves the text cursor with arrow, Home/End and Ctrl+arrow keys, and checks where the pointer, keyboard focus and text cursor are with `get_cursor`. The panel under the agent's view shows the live mouse position.
- WinPilot leaves itself out of screenshots and window lists, and hides while an input action runs, so its own window never gets clicked or typed into.

All the WinControl safety controls still apply: `WINCONTROL_ALLOWED_EXES`, protection against overwriting text, the failsafe corner, and the log in `WinControlMCP\actions.log`.

## Testing
```bat
..\WinControlMCP\.venv\Scripts\python.exe tests\agent_test.py
```
The agent test replaces the model with a scripted plan and drives WinControlMCP's throwaway test window. It covers allow, deny and allow-all, the activity log the agent writes and reads, and the activity summary added to the next task.
```bat
..\WinControlMCP\.venv\Scripts\python.exe tests\activity_test.py
```
The activity test checks that shortcuts and key names are logged and typed text is only counted.
