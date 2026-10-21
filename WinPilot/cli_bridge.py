"""Claude Code CLI as WinPilot's model.

WinPilot serves its own tools over a local MCP endpoint (streamable HTTP on 127.0.0.1, guarded by a
random bearer token) and runs `claude -p` headless against it. Claude Code gets only these tools: its
built-in file/shell tools are switched off and no other MCP servers are loaded. Every tool call runs
through WinPilot's Agent, so approvals, the activity log, window hiding and the failsafe all apply.
"""
import asyncio
import base64
import functools
import inspect
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import server

SERVER_NAME = "winpilot"
CREATE_NO_WINDOW = 0x08000000


class _TokenGuard:
    """ASGI middleware: HTTP requests need 'Authorization: Bearer <token>' (lifespan events pass)."""

    def __init__(self, app, token):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization") != self.expected:
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        await self.app(scope, receive, send)


async def read_activity_log(last: int = 30, source: str = "all") -> str:
    """Read the desktop activity log: what the user, you (the agent) and Windows did recently (window
    switches, windows opened/closed, clicks, shortcuts, how much was typed; never the typed text).
    source: 'all', 'you' (the user), 'agent' (you) or 'system' (window changes). Newest last."""


async def schedule_task(prompt: str, time: str, repeat: str = "once", date: str = "", days: str = "") -> str:
    """Schedule a task for WinPilot to do later, once or on a repeat. prompt: the complete instruction to
    carry out then. time: local time like '23:00' or '11 pm'. repeat: once, daily, weekdays or weekly.
    date (once): YYYY-MM-DD, blank for the next time that clock time comes. days (weekly): e.g. 'mon,wed'."""


async def list_scheduled_tasks() -> str:
    """List the scheduled tasks with their ids and next run times."""


async def cancel_scheduled_task(task_id: str) -> str:
    """Delete a scheduled task by the id from list_scheduled_tasks."""


LOCAL_TOOLS = {f.__name__: f for f in (read_activity_log, schedule_task, list_scheduled_tasks, cancel_scheduled_task)}


class Gateway:
    """WinPilot's tools as an MCP server on localhost. run_tool(name, args) -> (text, images, error)
    does the actual work (on a worker thread); images are (mime, base64) pairs."""

    def __init__(self, tool_names, run_tool):
        self.run_tool, self.token = run_tool, secrets.token_urlsafe(32)
        self.mcp = MCPServer(name=SERVER_NAME, instructions=server.mcp.instructions)
        for name in tool_names:
            fn = LOCAL_TOOLS.get(name) or getattr(server, name)
            self.mcp.add_tool(self._wrap(name, fn), name=name, description=inspect.getdoc(fn))
        with socket.socket() as s:  # a free port
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        app = _TokenGuard(self.mcp.streamable_http_app(host="127.0.0.1"), self.token)
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                     log_level="warning", lifespan="on"))
        threading.Thread(target=self._server.run, daemon=True).start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("WinPilot's tool server did not start.")
            time.sleep(0.05)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/mcp"

    def _wrap(self, name, fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def call(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            text, images, error = await asyncio.to_thread(self.run_tool, name, dict(bound.arguments))
            if error:
                raise ToolError(text)
            if images:
                return [text] + [Image(data=base64.b64decode(data), format=mime.split("/")[-1])
                                 for mime, data in images]
            return text
        return call

    def mcp_config(self):
        return {"mcpServers": {SERVER_NAME: {"type": "http", "url": self.url,
                                             "headers": {"Authorization": f"Bearer {self.token}"}}}}

    def close(self):
        self._server.should_exit = True


def find_claude(configured=""):
    """The command that starts Claude Code, as a list. Raises if it isn't installed."""
    if configured:
        return [configured]
    found = shutil.which("claude")
    if not found:
        raise RuntimeError("Claude Code CLI not found. Install it (https://claude.com/claude-code), run "
                           "`claude` once to log in, or set its path in Settings.")
    return [found]


class ClaudeCli:
    """One WinPilot chat with Claude Code. Each task runs `claude -p`; later tasks resume the same
    Claude Code session so the conversation carries over."""

    def __init__(self, cfg, gateway, system_prompt, workdir, command=None):
        self.cmd = command or find_claude(cfg.get("claude_cli_path", ""))
        self.model, self.effort = cfg.get("claude_cli_model", ""), cfg.get("effort", "")
        self.gateway, self.system_prompt, self.workdir = gateway, system_prompt, Path(workdir)
        self.session_id = None
        self.proc = None

    def args(self, config_file, prompt_file):
        a = self.cmd + ["-p", "--output-format", "stream-json", "--verbose", "--strict-mcp-config",
                        "--mcp-config", str(config_file), "--tools", "", "--allowedTools", f"mcp__{SERVER_NAME}",
                        "--append-system-prompt-file", str(prompt_file)]
        if self.model:
            a += ["--model", self.model]
        if self.effort:
            a += ["--effort", self.effort]
        if self.session_id:
            a += ["--resume", self.session_id]
        return a

    def run(self, prompt, on_text, stop_flag):
        """Run one task. Calls on_text(text) for each thing Claude says. Returns the final result text."""
        self.workdir.mkdir(parents=True, exist_ok=True)  # an empty folder: no project CLAUDE.md or settings
        config_file, prompt_file = self.workdir / "mcp.json", self.workdir / "system.md"
        config_file.write_text(json.dumps(self.gateway.mcp_config()), encoding="utf-8")
        prompt_file.write_text(self.system_prompt, encoding="utf-8")
        # Approvals can take a while: let tool calls wait up to 10 minutes.
        env = dict(os.environ, MCP_TOOL_TIMEOUT="600000", MCP_TIMEOUT="30000")
        try:
            self.proc = subprocess.Popen(self.args(config_file, prompt_file), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.workdir,
                                         env=env, text=True, encoding="utf-8", errors="replace",
                                         creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            raise RuntimeError(f"Couldn't start Claude Code ({e}). Check its path in Settings.") from e
        stderr = []
        threading.Thread(target=lambda: stderr.extend(self.proc.stderr), daemon=True).start()
        threading.Thread(target=self._kill_on_stop, args=(stop_flag,), daemon=True).start()
        self.proc.stdin.write(prompt)
        self.proc.stdin.close()
        result = None
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                self.session_id = event.get("session_id") or self.session_id
                status = {s.get("name"): s.get("status") for s in event.get("mcp_servers", [])}
                if status.get(SERVER_NAME) not in (None, "connected"):
                    raise RuntimeError(f"Claude Code couldn't connect to WinPilot's tools ({status[SERVER_NAME]}).")
            elif kind == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        on_text(block["text"].strip())
            elif kind == "result":
                self.session_id = event.get("session_id") or self.session_id
                result = event
        self.proc.wait()
        if stop_flag.is_set():
            return None
        if result is None:
            tail = "".join(stderr).strip().splitlines()[-3:]
            raise RuntimeError("Claude Code stopped unexpectedly" + (f": {' '.join(tail)}" if tail else "."))
        if result.get("is_error"):
            raise RuntimeError(f"Claude Code: {result.get('result') or result.get('subtype', 'error')}")
        return result.get("result", "")

    def _kill_on_stop(self, stop_flag):
        proc = self.proc
        while proc.poll() is None:
            if stop_flag.wait(0.2):
                # Kill the whole tree: claude may run through a .cmd shim with child processes.
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                               creationflags=CREATE_NO_WINDOW)
                return


def test_cli(cfg):
    """Check that Claude Code is installed and logged in. Returns a short message; raises on failure."""
    cmd = find_claude(cfg.get("claude_cli_path", ""))
    r = subprocess.run(cmd + ["-p", "Reply with just: OK", "--output-format", "json", "--tools", ""]
                       + (["--model", cfg["claude_cli_model"]] if cfg.get("claude_cli_model") else []),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                       creationflags=CREATE_NO_WINDOW)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300] or "Claude Code gave no answer.") from None
    if out.get("is_error"):
        raise RuntimeError(f"Claude Code: {out.get('result')}")
    return f"Claude Code is ready ({cmd[0]})."
