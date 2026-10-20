@echo off
rem Runs an agent CLI on a local Lemonade model with only the wincontrol tools loaded.
rem   local-agent.cmd                -> interactive session (each tool call needs approval)
rem   local-agent.cmd "do something" -> one-shot run; wincontrol tools are auto-approved
rem Requires Lemonade ctx_size >= 32768 (lemonade config set ctx_size=32768).
setlocal

set ENABLE_TOOL_SEARCH=false
if not defined MODEL set MODEL=Qwen3-VL-8B-Instruct-GGUF

rem Generate the MCP config from this folder's location so the repo works from any path.
set "HERE=%~dp0"
set "HERE=%HERE:\=/%"
set "CFG=%TEMP%\wincontrol-mcp.json"
> "%CFG%" echo {"mcpServers":{"wincontrol":{"command":"%HERE%.venv/Scripts/python.exe","args":["%HERE%server.py"]}}}
set "COMMON=--strict-mcp-config --mcp-config '%CFG%'"

if "%~1"=="" (
    lemonade launch claude -m %MODEL% --agent-args "%COMMON%"
) else (
    lemonade launch claude -m %MODEL% --agent-args "-p '%~1' %COMMON% --settings '%~dp0local-model-settings.json'"
)
