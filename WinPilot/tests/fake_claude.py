"""A stand-in for `claude -p --output-format stream-json`, used by cli_bridge_test.py.

It reads its MCP config like Claude Code does, connects to WinPilot's tool gateway over HTTP with the
given headers, runs a small fixed plan of tool calls, and prints stream-json events.
"""
import asyncio
import json
import re
import sys
import time
import uuid

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

TITLE = "WinControlTest"


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def emit(event):
    print(json.dumps(event), flush=True)


async def main():
    prompt = sys.stdin.read()
    assert arg("--tools") == "" and arg("--allowedTools") == "mcp__winpilot", sys.argv
    assert "--strict-mcp-config" in sys.argv and arg("--append-system-prompt-file"), sys.argv
    server = json.load(open(arg("--mcp-config"), encoding="utf-8"))["mcpServers"]["winpilot"]
    session_id = arg("--resume") or str(uuid.uuid4())
    emit({"type": "system", "subtype": "init", "session_id": session_id,
          "mcp_servers": [{"name": "winpilot", "status": "connected"}]})
    if "sleep" in prompt:  # for the Stop test
        time.sleep(60)
    async with httpx2.AsyncClient(headers=server["headers"], timeout=600) as http:
        async with streamable_http_client(server["url"], http_client=http) as streams:
            async with ClientSession(*tuple(streams)[:2]) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert {"get_ui_elements", "click_element", "read_activity_log", "move_mouse"} <= names, names
                if arg("--resume"):
                    emit({"type": "assistant", "message": {"content": [
                        {"type": "text", "text": f"Resumed session; prompt was: {prompt[-40:]}"}]}})
                else:
                    r = await s.call_tool("get_ui_elements", {"window": TITLE})
                    button = int(re.search(r'\[(\d+)\] Button "Press me"', r.content[0].text)[1])
                    r = await s.call_tool("click_element", {"element_id": button})
                    emit({"type": "assistant", "message": {"content": [
                        {"type": "text", "text": f"click: {'error' if r.is_error else 'ok'}"}]}})
                    r = await s.call_tool("read_activity_log", {"source": "agent"})
                    emit({"type": "assistant", "message": {"content": [
                        {"type": "text", "text": "log: " + r.content[0].text.replace("\n", " | ")}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session_id,
          "result": "Done."})


asyncio.run(main())
