# WinControl MCP

An MCP server (stdio) that lets AI agents see and control the Windows desktop.

## Setup
```bat
setup.cmd              :: create .venv and install requirements.txt
setup.cmd --register   :: optional: also register it (user scope) with the `claude` agent CLI
```
For other MCP clients, configure a stdio server that runs `.venv\Scripts\python.exe server.py`.

## Tools
| Tool | What it does |
|---|---|
| `list_windows` | Open windows: hwnd, title, exe |
| `focus_window` | Bring a window to the front (by hwnd, title, or exe name) |
| `start_app` | Launch `notepad`, `calc`, `ms-settings:`, a path, a URL... |
| `get_ui_elements` | A window's buttons, fields, menus... as numbered ids |
| `click_element` / `set_text` | Click an element, or fill a field |
| `click` / `type_text` / `press_keys` / `scroll` | Raw mouse and keyboard input |
| `screenshot` | Image of the desktop or a window, with how to map pixels to `click` |
| `wait` | Pause while the UI updates |

## Local models (Lemonade Server)
`local-agent.cmd` runs an MCP-capable agent CLI against a local model served by [Lemonade](https://lemonade-server.ai). The default model is `Qwen3-VL-8B-Instruct-GGUF`; override it with the `MODEL` environment variable.
```bat
local-agent.cmd                                  :: interactive; approve each tool call
local-agent.cmd "fill in the form in MyApp"      :: one-shot; wincontrol tools auto-approved
```
Small models need three settings, which the script handles except for the first:
- **Context size of at least 32768:** run `lemonade config set ctx_size=32768`, then `lemonade unload`. Lemonade's auto-tune may pick 4096, but the agent's system prompt plus tool definitions is about 15k tokens.
- **`ENABLE_TOOL_SEARCH=false`:** otherwise the MCP tools are deferred behind a search step, and a small model concludes it has no GUI tools.
- **Only this MCP server loaded:** a generated `--strict-mcp-config` keeps the prompt small.

An 8B model handles short, specific tasks well (filling a field and clicking a button takes about 30 s). It's much less reliable on long multi-step tasks.

## Safety
- **Failsafe:** with the mouse in the top-left screen corner, every input tool refuses to act.
- **Allowlist:** `WINCONTROL_ALLOWED_EXES=notepad.exe,calculatorapp.exe` restricts which apps receive input.
- **Protected text:** `set_text` refuses to overwrite existing text unless the call passes `replace_existing=True`.
- **Blocked input:** if Windows blocks simulated input (UIPI, i.e. an elevated or system process is in the foreground), the tools return an error instead of a false success.
- **Stuck focus:** when a hidden service window (e.g. GameInputSvc) holds the foreground, window activation falls back to minimize-and-restore.
- **Log:** every action is appended to `actions.log`.

## Testing
```bat
.venv\Scripts\python.exe tests\smoke_test.py
```
The test only interacts with its own throwaway window (`tests/testwin.ps1`).
