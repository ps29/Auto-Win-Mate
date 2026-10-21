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
- **Local LLM (Lemonade)**, the default: any model served by [Lemonade Server](https://lemonade-server.ai); **Refresh** lists the installed ones. Use a vision model such as `Qwen3-VL-8B-Instruct-GGUF` so screenshots work, and set `lemonade config set ctx_size=32768`. Small models handle short, specific tasks best. Nothing leaves your PC.
- **Claude Code CLI:** uses the `claude` CLI you are already logged in to, so no API key. WinPilot serves its desktop tools to Claude Code over a local, token-protected connection and runs `claude -p` with **only** those tools: no file, shell or web access, and no other MCP servers. Every action still goes through the approval bar, the activity log and the safety checks. Follow-up tasks continue the same Claude Code session. Pick a model (blank = Claude Code's default, or `opus`, `sonnet`, `haiku`) and an effort in Settings.
- **Local model type** picks how WinPilot talks to a local model: *Tool calling* (Qwen3-VL and similar), or one of two models trained to use a computer from screenshots alone: [Fara-7B](https://huggingface.co/microsoft/Fara-7B) (Microsoft; trained mostly on web tasks) and [UI-TARS-1.5](https://github.com/bytedance/UI-TARS) (ByteDance). Load their GGUF builds in Lemonade and choose the matching type.

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
- **Approval (optional):** off by default, so the agent just does the task. Turn on **Ask before clicking, typing, dragging or launching apps** in Settings to get an **Allow / Allow all this task / Deny** bar before each input action. Reading windows, hovering and screenshots never ask.
- **Cursor control:** the agent clicks, hovers (`move_mouse`), drags, scrolls and types. It moves the text cursor with arrow, Home/End and Ctrl+arrow keys, and checks where the pointer, keyboard focus and text cursor are with `get_cursor`. The panel under the agent's view shows the live mouse position.
- **Human-like input:** by default the agent moves the mouse along curved paths and types at a person's pace. Change it under Settings > Safety and privacy > **Input speed** (Human-like, slower, or Instant).
- **Take over any time:** move the mouse yourself while a task runs and WinPilot stops it (turn this off in Settings). When approvals are on, moving the mouse to answer one doesn't count.
- **Vision element detection:** after running `..\WinControlMCP\setup_omniparser.cmd` once, the agent can find buttons by vision (OmniParser) in apps without UI elements.
- WinPilot leaves itself out of screenshots and window lists, and hides while an input action runs, so its own window never gets clicked or typed into.

All the WinControl safety controls still apply: `WINCONTROL_ALLOWED_EXES`, protection against overwriting text, the failsafe corner, and the log in `WinControlMCP\actions.log`.

## Housekeeping
- Activity logs older than **Keep activity logs** days (Settings > More, default 30) are deleted at startup and once a
  day. **Clean up now** runs that immediately.
- `WinControlMCP\actions.log` rotates at 5 MB (keeps `actions.log.1` to `.3`).
- `..\maintenance.cmd` also clears Python and pip caches and checks the environment.

## Testing
```bat
..\WinControlMCP\.venv\Scripts\python.exe tests\agent_test.py
```
The agent test replaces the model with a scripted plan and drives WinControlMCP's throwaway test window. It covers allow, deny and allow-all, the activity log the agent writes and reads, and the activity summary added to the next task.
```bat
..\WinControlMCP\.venv\Scripts\python.exe tests\activity_test.py
```
The activity test checks that shortcuts and key names are logged, typed text is only counted, and a quick
mouse sweep counts as a takeover.
```bat
..\WinControlMCP\.venv\Scripts\python.exe tests\computer_test.py
..\WinControlMCP\.venv\Scripts\python.exe tests\housekeeping_test.py
..\WinControlMCP\.venv\Scripts\python.exe tests\cli_bridge_test.py
```
The Claude Code test uses a stand-in for `claude` (`tests/fake_claude.py`), so it needs no login and uses no Claude
usage.
The computer test checks the Fara-7B and UI-TARS parsers, and runs screen actions on the
test window through a scaled screen.
