"""WinPilot - tell an AI what to do on your Windows desktop.

A tray app with a chat box. You type an instruction; an LLM (a local model served by Lemonade,
or Claude through the Claude Code CLI) plans it and drives the desktop through the WinControl tools
(..\\WinControlMCP\\server.py), which keep their failsafe, allowlist and action log.
Input actions (clicks, typing, dragging, launching apps) wait for your approval unless you allow them.
The Activity tab records what happens on the desktop (activity.py); the agent can read it.

Run:  run_pilot.bat [--hidden]

Global hotkeys:
    Ctrl+Alt+Space  show / hide the window
    Ctrl+Alt+X      stop the running task
Failsafe: push the mouse into the top-left screen corner and every input tool refuses to act.
"""
import asyncio
import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import queue
import re
import sys
import threading
import time
import types
import urllib.error
import winreg
import urllib.request
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

FROZEN = getattr(sys, "frozen", False)  # running as the packaged WinPilot.exe
HERE = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
if FROZEN:  # the install folder may be read-only: keep the action log with the user's data
    os.environ.setdefault("WINCONTROL_LOG", str(Path(os.environ.get("LOCALAPPDATA", HERE)) / "WinPilot" / "actions.log"))
else:
    sys.path.insert(0, os.environ.get("WINPILOT_SERVER_DIR", str(HERE.parent / "WinControlMCP")))
import server  # noqa: E402  (imports ClickBot too, which makes this process DPI-aware)
import activity  # noqa: E402
import cli_bridge  # noqa: E402
import computer  # noqa: E402
import housekeeping  # noqa: E402
import omniparser  # noqa: E402  (WinControlMCP\omniparser.py: optional vision element detection)

import mss  # noqa: E402
import pystray  # noqa: E402
from PIL import Image, ImageDraw, ImageTk  # noqa: E402

cb = server.cb
user32 = cb.user32
cb._sig(user32.SetWindowDisplayAffinity, wt.BOOL, wt.HWND, wt.DWORD)

WDA_EXCLUDEFROMCAPTURE = 0x11
SW_HIDE, SW_SHOWNOACTIVATE, SW_SHOW, SW_RESTORE = 0, 4, 5, 9
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000

# ---------------------------------------------------------------- settings

CONFIG = Path(os.environ.get("APPDATA", HERE)) / "WinPilot" / "settings.json"
DEFAULTS = {
    "backend": "lemonade",                    # "lemonade" (local, the default) or "claude_cli" (Claude Code)
    "lemonade_url": "http://localhost:13305/api/v1",
    "lemonade_model": "Qwen3-VL-8B-Instruct-GGUF",
    "local_style": "tools",                   # "tools" (tool calling), "fara" (Fara-7B) or "uitars" (UI-TARS-1.5)
    "claude_cli_path": "",                    # blank: find `claude` on PATH
    "claude_cli_model": "",                   # blank: Claude Code's default; or opus, sonnet, haiku, a model id
    "effort": "medium",
    "max_steps": 40,
    "ask_before_input": True,
    "hide_during_actions": True,
    "record_input": True,                     # log your own clicks and keys (never the text you type)
    "takeover": True,                         # moving the mouse yourself stops the running task
    "keep_logs_days": 30,                     # delete daily activity logs older than this
    "input_speed": "human",                   # "human", "careful" or "instant"
}
PROVIDERS = {"lemonade": "Local LLM (Lemonade)", "claude_cli": "Claude Code CLI"}
INPUT_SPEEDS = {"human": "Human-like", "careful": "Human-like, slower", "instant": "Instant (fastest)"}
LOCAL_STYLES = {"tools": "Tool calling (Qwen3-VL and similar)", "fara": "Fara-7B (screenshots only)",
                "uitars": "UI-TARS-1.5 (screenshots only)"}
CLI_MODELS = ["", "opus", "sonnet", "haiku"]


def load_settings():
    cfg = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG.read_text(encoding="utf-8"))
        cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    if cfg["backend"] not in PROVIDERS:  # e.g. "claude" from versions that used the Claude API
        cfg["backend"] = "lemonade"
    cfg["lemonade_url"] = lemonade_base(cfg["lemonade_url"])
    return cfg


def lemonade_base(url):
    """Accept any form of Lemonade's address: http://host:port, .../api/v1, or a full
    .../chat/completions link copied from the Lemonade app. Returns the API base (.../api/v1)."""
    url = (url or DEFAULTS["lemonade_url"]).strip().rstrip("/")
    for tail in ("/chat/completions", "/completions", "/models"):
        if url.endswith(tail):
            url = url[: -len(tail)]
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url if url.endswith("/v1") else url + "/api/v1"


def save_settings(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

# ---------------------------------------------------------------- LLM backends

SYSTEM = """You control the user's Windows desktop through tools, on their behalf. The user types \
requests into a small control window called WinPilot. It is hidden from screenshots and window \
lists; never try to interact with it.

How to work:
- Each task may start with a summary of what happened on the desktop since the last one. Call \
read_activity_log to see more, e.g. what the user just did or what your earlier actions changed.
- Start with list_windows. Prefer get_ui_elements with click_element / set_text, which are precise.
{input_help}
- Use wait_for instead of fixed waits, read_page_text to read a page or document, \
get_ui_elements(web_only=True) inside browsers, clipboard + ctrl+v to enter long text, and \
arrange_window to move or maximize windows. detect_elements (when available) finds clickable \
regions by vision in apps without UI elements.
- After each action, check the result (get_ui_elements again, or a screenshot) before moving on.
- The user may have to approve input actions. If one is denied, don't retry it: adjust the plan or ask.
- If the request is ambiguous, or would delete, overwrite, send, buy or close something the user may \
care about, stop and ask first.
- When you are finished, reply in one or two sentences: what you did, or what is blocking you."""


INPUT_HELP_TOOLS = """- Use screenshot with click(x, y, window=...) only when an app exposes no usable elements.
- You control the mouse (click, move_mouse to hover, drag, scroll) and the keyboard (type_text, \
press_keys; arrows, home/end and ctrl+arrows move the text cursor). Call get_cursor to see where \
the pointer, keyboard focus and text cursor are before typing."""
def _post_json(url, body=None, timeout=600):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Lemonade returned HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Can't reach Lemonade at {url} ({e.reason}). Is Lemonade Server running?") from e


def lemonade_models(url):
    return [m["id"] for m in _post_json(lemonade_base(url) + "/models", timeout=5).get("data", [])]


class LemonadeBackend:
    """OpenAI-compatible chat completions on a local Lemonade Server."""

    def __init__(self, cfg, tools):
        self.url, self.model = lemonade_base(cfg["lemonade_url"]), cfg["lemonade_model"]
        self.tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                        "parameters": t.input_schema}} for t in tools]
        self.messages = [{"role": "system", "content": SYSTEM.format(input_help=INPUT_HELP_TOOLS)}]

    def mark(self):
        return len(self.messages)

    def rollback(self, mark):
        del self.messages[max(1, mark):]

    def add_user(self, text):
        self.messages.append({"role": "user", "content": text})

    def step(self):
        # Small local contexts fill up fast: keep only the newest screenshot.
        images = [m for m in self.messages if m["role"] == "user" and isinstance(m["content"], list)]
        for m in images[:-1]:
            m["content"] = "(An earlier screenshot was removed to save context.)"
        body = {"model": self.model, "messages": self.messages, "tools": self.tools, "tool_choice": "auto"}
        msg = _post_json(self.url + "/chat/completions", body)["choices"][0]["message"]
        text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            c["id"] = c.get("id") or f"call_{len(self.messages)}_{i}"
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = None
            calls.append((c["id"], c["function"]["name"], args))
        entry = {"role": "assistant", "content": text}
        if msg.get("tool_calls"):
            entry["tool_calls"] = msg["tool_calls"]
        self.messages.append(entry)
        return text, calls, False

    def add_results(self, results):
        shots = []
        for res in results:
            self.messages.append({"role": "tool", "tool_call_id": res["id"], "content": res["text"] or "(no output)"})
            shots += res["images"]
        if shots:
            parts = [{"type": "text", "text": "Screenshot returned by the tool call above:"}]
            parts += [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}} for mime, data in shots]
            self.messages.append({"role": "user", "content": parts})

class VisionLocalBackend:
    """Local computer-use models that act from screenshots alone: Fara-7B and UI-TARS-1.5.
    Each turn the model gets a fresh screenshot and answers with actions in its own text format,
    which computer.parse_fara / parse_uitars turn into WinPilot's action dicts."""

    KEEP_SCREENSHOTS = 3

    def __init__(self, cfg, tools, style):
        self.url, self.model, self.style = lemonade_base(cfg["lemonade_url"]), cfg["lemonade_model"], style
        self.screen = computer.Screen(computer.fara_fit if style == "fara" else computer.uitars_fit)
        self.messages = [{"role": "system", "content": computer.FARA_SYSTEM}] if style == "fara" else []
        self.base = len(self.messages)
        self.turn = 0

    def mark(self):
        return len(self.messages)

    def rollback(self, mark):
        del self.messages[max(self.base, mark):]

    def _image(self):
        mime, data = self.screen.screenshot()
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}

    def add_user(self, text):
        prompt = computer.UITARS_PROMPT + text if self.style == "uitars" else text
        self.messages.append({"role": "user", "content": [{"type": "text", "text": prompt}, self._image()]})

    def step(self):
        shots = [m for m in self.messages if m["role"] == "user" and isinstance(m["content"], list)]
        for m in shots[:-self.KEEP_SCREENSHOTS]:  # small contexts: drop old screenshots, keep their text
            m["content"] = [part for part in m["content"] if part["type"] == "text"] or "(screenshot removed)"
        body = {"model": self.model, "messages": self.messages, "temperature": 0}
        reply = _post_json(self.url + "/chat/completions", body)["choices"][0]["message"].get("content") or ""
        reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S).strip()
        self.messages.append({"role": "assistant", "content": reply})
        parse = computer.parse_fara if self.style == "fara" else computer.parse_uitars
        try:
            thought, actions = parse(reply)
        except (ValueError, SyntaxError) as e:
            if self.turn == 0 or not reply:
                raise RuntimeError(f"The model's reply isn't in the {LOCAL_STYLES[self.style]} format ({e}). "
                                   "Check the model and 'Local model type' in Settings.") from e
            return reply, [], False  # plain text after some actions: treat it as the final answer
        self.turn += 1
        finish = next((a for a in actions if a["kind"] == "finish"), None)
        if finish:
            status = {"success": "Done.", "failure": "I couldn't finish this task."}
            end = status.get(finish.get("text", ""), finish.get("text", ""))
            return "\n".join(t for t in (thought, end) if t) or "Done.", [], False
        return thought, [(f"v{self.turn}_{i}", "vision", a, "vision") for i, a in enumerate(actions)], False

    def add_results(self, results):
        notes = [r["text"] for r in results if r["error"]]
        text = "Previous action failed: " + " / ".join(notes) if notes else "Action done. Here is the new screen."
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}, self._image()]})


def friendly_error(e):
    """Turn errors into something a user can act on."""
    return str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"

# ---------------------------------------------------------------- agent

NEEDS_APPROVAL = {"click_element", "set_text", "click", "type_text", "press_keys", "scroll", "start_app", "drag",
                  "clipboard"}
MOVES_FOCUS = NEEDS_APPROVAL | {"focus_window", "move_mouse", "get_cursor"}  # WinPilot hides while these run
TARGET_FOCUS = {"type_text", "press_keys", "get_cursor"}  # need the user's window in front, not WinPilot
LOG_TOOL = types.SimpleNamespace(
    name="read_activity_log",
    description="Read the desktop activity log: what the user, you (the agent) and Windows did recently "
                "(window switches, windows opened/closed, clicks, shortcuts, how much was typed; never the "
                "typed text). Newest last.",
    input_schema={"type": "object", "properties": {
        "last": {"type": "integer", "default": 30, "description": "How many entries (max 200)."},
        "source": {"type": "string", "enum": ["all", "you", "agent", "system"], "default": "all",
                   "description": "'you' is the user, 'agent' is you, 'system' is window changes."}}})


def _call_server(name, args):
    """Run a WinControl tool. Callable from any thread (the agent's, or the Claude Code gateway's)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(server.mcp.call_tool(name, args))
    finally:
        loop.close()


class Agent(threading.Thread):
    """Runs one task at a time: model request -> approval -> tool call -> repeat."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app, self.events = app, app.events
        self.tasks = queue.Queue()
        self.stop_flag, self.decided = threading.Event(), threading.Event()
        self.decision = "deny"
        self.backend, self.backend_key = None, None
        self.elements = {}  # element id -> description, from the latest get_ui_elements
        self.allow_all = False
        self.log = app.log
        self.seen_id = 0  # activity entries up to here were already shown to the model
        self.awaiting = False  # waiting for the user to approve: moving the mouse then isn't a takeover
        self.calm_until = 0.0  # the user's hand is still on the mouse right after Send or Allow

    def emit(self, kind, data=None):
        self.events.put((kind, data))

    def stop(self):
        self.stop_flag.set()
        self.decide("deny")

    def decide(self, verdict):
        self.decision = verdict
        self.decided.set()

    def run(self):
        self.tool_defs = [t for t in asyncio.run(server.mcp.list_tools())
                          if t.name != "detect_elements" or omniparser.available()] + [LOG_TOOL]
        self.gateway = None  # WinPilot's tools served to Claude Code, started on first use
        while True:
            text, cfg = self.tasks.get()
            self.stop_flag.clear()
            self.calm_until = time.monotonic() + 2.0
            self.allow_all = not cfg["ask_before_input"]
            self.log.add("agent", "task", f'started task: "{text[:120]}"')
            outcome = "finished task"
            try:
                self._task(text, cfg)
            except Exception as e:
                outcome = "task failed"
                self.emit("error", friendly_error(e))
            finally:
                if self.stop_flag.is_set():
                    outcome = "task stopped by you"
                self.log.add("agent", "task", outcome)
                self.seen_id = self.log.last_id
                self.emit("done")

    def _task(self, text, cfg):
        self.cfg = cfg
        style = cfg.get("local_style", "tools")
        if cfg["backend"] == "claude_cli":
            return self._task_cli(text, cfg)
        if style in ("fara", "uitars"):
            key = ("vision", cfg["lemonade_url"], cfg["lemonade_model"], style)
            make = lambda c, t: VisionLocalBackend(c, t, style)  # noqa: E731
        else:
            key, make = ("lemonade", cfg["lemonade_url"], cfg["lemonade_model"]), LemonadeBackend
        if self.backend is None or key != self.backend_key:
            self.backend, self.backend_key = make(cfg, self.tool_defs), key
        b = self.backend
        mark = b.mark()
        b.add_user(self._with_recent_activity(text))
        for step in range(cfg["max_steps"]):
            if self.stop_flag.is_set():
                self.emit("info", "Stopped.")
                return
            self.emit("status", "Thinking...")
            try:
                reply, calls, more = b.step()
            except Exception:
                if step == 0:  # nothing happened yet: forget the message so the chat stays valid
                    b.rollback(mark)
                raise
            if reply:
                self.emit("assistant", reply)
            if not calls:
                if more:
                    continue
                return
            b.add_results(self._run_calls(calls))
        self.emit("error", f"Stopped after {cfg['max_steps']} steps (the limit is in Settings).")

    def _task_cli(self, text, cfg):
        """Claude Code runs the whole loop itself; its tool calls come back through the gateway into
        _cli_tool, on the gateway's threads, while this thread follows Claude's output."""
        if self.gateway is None:
            self.gateway = cli_bridge.Gateway([t.name for t in self.tool_defs], self._cli_tool)
        key = ("claude_cli", cfg["claude_cli_path"], cfg["claude_cli_model"], cfg["effort"])
        if not isinstance(self.backend, cli_bridge.ClaudeCli) or key != self.backend_key:
            system = SYSTEM.format(input_help=INPUT_HELP_TOOLS)
            self.backend = cli_bridge.ClaudeCli(cfg, self.gateway, system, CONFIG.parent / "claude-cli")
            self.backend_key = key
        self.cli_calls = 0
        self.emit("status", "Claude is working...")
        result = self.backend.run(self._with_recent_activity(text), lambda t: self.emit("assistant", t),
                                  self.stop_flag)
        if result is None:
            self.emit("info", "Stopped.")

    def _cli_tool(self, name, args):
        """One tool call from Claude Code -> (text, images, error)."""
        self.cli_calls += 1
        if self.cli_calls > self.cfg["max_steps"]:
            self.stop_flag.set()
            self.emit("error", f"Stopped after {self.cfg['max_steps']} actions (the limit is in Settings).")
            return "Stopped: the step limit was reached.", [], True
        self.emit("status", "Claude is working...")
        r = self._run_tool(f"cli{self.cli_calls}", name, args)
        return r["text"], r["images"], r["error"]

    def _with_recent_activity(self, text):
        """Prefix the task with what happened since the model last looked (append-only, so history stays valid)."""
        recent = [e for e in self.log.query(last=25, after_id=self.seen_id) if e["source"] != "agent"]
        if not recent:
            return text
        lines = "\n".join(activity.format_entry(e) for e in recent)
        return f"What happened on the desktop since your last task (newest last):\n{lines}\n\nTask: {text}"

    def _run_calls(self, calls):
        """Run one model turn's calls in order. Screen actions stop at the first failure, as the
        computer toolset requires; the rest of that turn's screen actions are reported as not run."""
        results, failed = [], False
        for call_id, name, args, toolset in (c if len(c) == 4 else (*c, None) for c in calls):
            if toolset and failed:
                results.append({"id": call_id, "text": "Not executed: an earlier computer action in this turn failed.",
                                "images": [], "error": True})
            elif toolset:
                results.append(self._run_screen_action(call_id, name, args, toolset))
                failed = results[-1]["error"]
            else:
                results.append(self._run_tool(call_id, name, args))
        return results

    def _approve(self, action):
        """Ask the user (unless they allowed everything for this task). True if allowed."""
        if self.allow_all:
            return True
        self.decided.clear()
        self.awaiting = True
        self.emit("approve", action)
        self.decided.wait()
        self.awaiting, self.calm_until = False, time.monotonic() + 2.0
        if self.decision == "deny":
            self.emit("info", "Denied.")
            self.log.add("agent", "denied", f"you denied: {action}")
            return False
        self.allow_all = self.decision == "all"
        return True

    def _run_screen_action(self, call_id, name, args, toolset):
        """A parsed screen action from a local computer-use model (Fara-7B / UI-TARS)."""
        def result(text, images=(), error=False):
            return {"id": call_id, "text": text, "images": list(images), "error": error}

        if self.stop_flag.is_set():
            return result("Not run: the user stopped the task.", error=True)
        screen = self.backend.screen
        try:
            act = args
            action = computer.describe(act, screen)
        except (KeyError, TypeError, ValueError) as e:
            self.emit("tool_error", f"{name}: {e}")
            return result(f"Invalid action: {e}", error=True)
        self.emit("tool", f"{act['kind']}: {action}")
        is_input = act["kind"] in computer.INPUT_KINDS
        if is_input and not self._approve(action):
            return result("The user denied this action. Don't retry it; adjust the plan or ask the user.", error=True)
        self.emit("status", f"Running {act['kind']}...")
        hidden = ((is_input or act["kind"] == "move") and self.cfg["hide_during_actions"]
                  and self.app.hide_for_action())
        try:
            if act["kind"] in ("type", "key", "hold"):
                self.app.restore_target_focus()
            text, image = computer.perform(act, screen)
        except Exception as e:
            msg = str(e) or type(e).__name__
            self.emit("tool_error", msg)
            self.log.add("agent", "error", f"{action} - failed: {msg.splitlines()[0][:150]}")
            return result(msg, error=True)
        finally:
            if hidden:
                self.app.show_after_action()
        self.log.add("agent", "action" if is_input else "look", action)
        if text != "OK":
            self.emit("result", text)
        if image:
            self.emit("image", image[1])
        return result(text, [image] if image else [])

    def _run_tool(self, call_id, name, args):
        def result(text, images=(), error=False):
            return {"id": call_id, "text": text, "images": list(images), "error": error}

        if self.stop_flag.is_set():
            return result("Not run: the user stopped the task.", error=True)
        if not isinstance(args, dict):
            self.emit("tool_error", f"{name}: arguments were not valid JSON")
            return result("Invalid arguments: send a JSON object matching the tool's schema.", error=True)
        self.emit("tool", f"{name}({json.dumps(args, ensure_ascii=False)[:200]})")
        if name == LOG_TOOL.name:
            source = args.get("source") if args.get("source") in ("you", "agent", "system") else "all"
            try:
                last = max(1, min(int(args.get("last") or 30), 200))
            except (TypeError, ValueError):
                last = 30
            text = "\n".join(activity.format_entry(e) for e in self.log.query(last, source)) or "The log is empty."
            self.emit("result", text)
            return result(text)
        action = self._describe(name, args)
        if args.get("window") not in (None, ""):
            try:
                if server._resolve(args["window"]) == self.app.hwnd:
                    return result("That is the WinPilot control window; it can't be automated.", error=True)
            except ValueError:
                pass  # the tool reports a bad window itself
        if name in NEEDS_APPROVAL and not self._approve(action):
            return result("The user denied this action. Don't retry it; adjust the plan or ask the user.", error=True)
        self.emit("status", f"Running {name}...")
        hidden = name in MOVES_FOCUS and self.cfg["hide_during_actions"] and self.app.hide_for_action()
        try:
            if name in TARGET_FOCUS and args.get("window") is None:
                self.app.restore_target_focus()
            r = _call_server(name, args)
        except Exception as e:
            msg = str(e).removeprefix(f"Error executing tool {name}: ")
            self.emit("tool_error", msg)
            self.log.add("agent", "error", f"{action} - failed: {(msg.splitlines() or [''])[0][:150]}")
            return result(msg, error=True)
        finally:
            if hidden:
                self.app.show_after_action()
        text = "\n".join(c.text for c in r.content if c.type == "text")
        images = [(c.mime_type, c.data) for c in r.content if c.type == "image"]
        if name == "list_windows":
            text = "\n".join(l for l in text.splitlines() if not l.startswith(f"hwnd={self.app.hwnd} "))
        if name == "get_ui_elements":
            self.elements = {int(m[1]): m[2] for m in re.finditer(r"^\s*\[(\d+)\] (.+)$", text, re.M)}
        self.log.add("agent", "action" if name in NEEDS_APPROVAL else "look", action)
        self.emit("result", text)
        for _, data in images:
            self.emit("image", data)
        return result(text, images, bool(r.is_error))

    def _describe(self, name, a):
        """One line telling the user what an input action is about to do."""
        def element(i):
            return self.elements.get(i, f"element [{i}]")

        def window(w):
            return f" in '{w}'" if w not in (None, "") else ""

        if name == "click_element":
            return f"{'Double-click' if a.get('double') else 'Click'} {element(a.get('element_id'))}"
        if name == "set_text":
            how = " (replacing its current text)" if a.get("replace_existing") else ""
            enter = " and press Enter" if a.get("press_enter") else ""
            return f'Type "{str(a.get("text", ""))[:80]}" into {element(a.get("element_id"))}{how}{enter}'
        if name == "click":
            where = window(a.get("window"))
            if not where:
                try:
                    hwnd = cb.root_of(user32.WindowFromPoint(wt.POINT(int(a["x"]), int(a["y"]))))
                    where = f" on '{cb.window_title(hwnd) or cb.process_name(hwnd)}'"
                except (KeyError, TypeError, ValueError):
                    pass
            return f"{a.get('button', 'left').title()}-click at ({a.get('x')}, {a.get('y')}){where}"
        if name == "type_text":
            target = window(a.get("window")) or f" in '{cb.window_title(self.app.last_external)}'"
            return f'Type "{str(a.get("text", ""))[:120]}"{target}'
        if name == "press_keys":
            return f"Press {a.get('keys')}{window(a.get('window'))}"
        if name == "scroll":
            n = int(a.get("amount", 0) or 0)
            return f"Scroll {'up' if n > 0 else 'down'} {abs(n)}{window(a.get('window'))}"
        if name == "start_app":
            return f"Launch {a.get('command')} {a.get('arguments', '')}".strip()
        if name == "move_mouse":
            return f"Move the mouse to ({a.get('x')}, {a.get('y')}){window(a.get('window'))}"
        if name == "drag":
            return (f"Drag from ({a.get('from_x')}, {a.get('from_y')}) to ({a.get('to_x')}, {a.get('to_y')})"
                    f"{window(a.get('window'))}")
        if name == "clipboard":
            return ("Read your clipboard" if a.get("action") == "get"
                    else f'Put "{str(a.get("text", ""))[:80]}" on the clipboard')
        if name == "wait_for":
            return f"Wait for {repr(a.get('text')) + ' in ' if a.get('text') else ''}'{a.get('window')}'"
        if name == "arrange_window":
            return f"{str(a.get('action', '')).title()} '{a.get('window')}'"
        looks = {"list_windows": "List open windows", "get_cursor": "Check the mouse and text cursor",
                 "read_page_text": f"Read the text of '{a.get('window')}'",
                 "detect_elements": f"Find clickable regions by vision{window(a.get('window'))}",
                 "get_ui_elements": f"Read the controls of '{a.get('window')}'", "wait": f"Wait {a.get('ms')} ms",
                 "screenshot": f"Take a screenshot{window(a.get('window')) or ' of the desktop'}",
                 "focus_window": f"Bring '{a.get('window')}' to the front"}
        return looks.get(name, f"{name} {json.dumps(a)}")

# ---------------------------------------------------------------- background threads


class Hotkeys(threading.Thread):
    KEYS = {1: ("toggle", 0x20, "Ctrl+Alt+Space"), 2: ("stop", 0x58, "Ctrl+Alt+X")}

    def __init__(self, events):
        super().__init__(daemon=True)
        self.events = events

    def run(self):
        failed = [label for i, (_, vk, label) in self.KEYS.items()
                  if not user32.RegisterHotKey(None, i, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk)]
        if failed:
            self.events.put(("info", f"Couldn't register {', '.join(failed)}: another program is using it."))
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == cb.WM_HOTKEY and msg.wParam in self.KEYS:
                self.events.put(("hotkey", self.KEYS[msg.wParam][0]))


class LiveView(threading.Thread):
    """Grabs a desktop thumbnail every 1.5 s while live view is on and the window is visible."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app

    def run(self):
        with mss.mss() as s:
            while True:
                time.sleep(1.5)
                if self.app.live_on and user32.IsWindowVisible(self.app.hwnd) and time.time() > self.app.hold_until:
                    shot = s.grab(s.monitors[0])
                    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                    img.thumbnail((960, 960))
                    self.app.events.put(("live", img))


def tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(37, 99, 235))
    d.polygon([(20, 12), (20, 50), (30, 41), (37, 55), (44, 52), (37, 38), (50, 38)], fill="white")
    return img

# ---------------------------------------------------------------- GUI

EXAMPLES = ["What windows are open right now?",
            "Open Notepad and write a short to-do list for today",
            "Open Settings and switch Windows to dark mode",
            "What did I do on the desktop in the last few minutes?"]
PALETTES = {
    "light": {"bg": "#fafafa", "fg": "#1c1c1c", "muted": "#6b6b6b", "user": "#0b5cad", "tool": "#7c3aed",
              "error": "#c42b1c", "you": "#0b5cad", "agent": "#7c3aed", "system": "#6b6b6b",
              "ask_bg": "#fff4ce", "ask_fg": "#3b2f00", "view": "#e6e6e6", "ok": "#0f7b0f"},
    "dark": {"bg": "#1f1f1f", "fg": "#f0f0f0", "muted": "#a0a0a0", "user": "#60a5fa", "tool": "#c4a1ff",
             "error": "#ff99a4", "you": "#60a5fa", "agent": "#c4a1ff", "system": "#a0a0a0",
             "ask_bg": "#433519", "ask_fg": "#ffe9a8", "view": "#2b2b2b", "ok": "#6ccb5f"},
}
WHO = {"you": "You", "agent": "Agent", "system": "Windows"}


def system_theme():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


def shortcut_path(where):
    import win32com.client
    folder = win32com.client.Dispatch("WScript.Shell").SpecialFolders(where)  # "Desktop" or "Startup"
    return Path(folder) / "WinPilot.lnk"


def make_shortcut(where, hidden=False):
    import win32com.client
    icon = CONFIG.parent / "winpilot.ico"
    icon.parent.mkdir(parents=True, exist_ok=True)
    tray_image().save(icon, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    link = win32com.client.Dispatch("WScript.Shell").CreateShortCut(str(shortcut_path(where)))
    if FROZEN:
        link.TargetPath, link.Arguments = sys.executable, "--hidden" if hidden else ""
    else:
        link.TargetPath = str(Path(sys.executable).with_name("pythonw.exe"))
        link.Arguments = f'"{HERE / "pilot.py"}"' + (" --hidden" if hidden else "")
    link.WorkingDirectory = str(HERE)
    link.IconLocation = str(icon)
    link.Description = "WinPilot: tell an AI what to do on your desktop"
    link.save()


class App:
    def __init__(self, root, start_hidden):
        self.root, self.events = root, queue.Queue()
        self.first_run = not CONFIG.exists()
        self.cfg = load_settings()
        self.running, self.live_on, self.hold_until = False, False, 0.0
        self.last_external, self._photo = 0, None
        self.log = activity.ActivityLog(CONFIG.parent / "logs")
        self.log.listeners.append(lambda entry: self.events.put(("activity", entry)))
        self.theme = system_theme()
        self.pal = PALETTES[self.theme]
        try:
            import sv_ttk
            sv_ttk.set_theme(self.theme)
        except ImportError:
            pass
        root.title("WinPilot")
        self._icon = ImageTk.PhotoImage(tray_image())
        root.iconphoto(True, self._icon)
        scale = root.winfo_fpixels("1i") / 96  # the process is DPI-aware, so sizes are physical pixels
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(round(920 * scale), int(sw * 0.9)), min(round(620 * scale), int(sh * 0.8))
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")
        root.minsize(round(600 * scale), round(420 * scale))
        self._build()
        root.update()
        self.hwnd = int(root.wm_frame(), 16)
        user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE)  # keep it out of screenshots
        root.protocol("WM_DELETE_WINDOW", self.hide)

        self.agent = Agent(self)
        self.agent.start()
        Hotkeys(self.events).start()
        LiveView(self).start()
        activity.DesktopMonitor(
            self.log, self.hwnd, record_input=lambda: self.cfg["record_input"],
            watch_takeover=lambda: (self.running and self.cfg["takeover"] and not self.agent.awaiting
                                    and time.monotonic() > self.agent.calm_until),
            on_takeover=lambda: self.events.put(("takeover", None))).start()
        self.tray = pystray.Icon("WinPilot", tray_image(), "WinPilot", pystray.Menu(
            pystray.MenuItem("Show", lambda: self.events.put(("tray", "show")), default=True),
            pystray.MenuItem("Stop task", lambda: self.events.put(("tray", "stop"))),
            pystray.MenuItem("Quit", lambda: self.events.put(("tray", "quit")))))
        self.tray.run_detached()

        cb.set_input_speed(self.cfg["input_speed"])
        self.log.add("system", "app", "WinPilot started")
        self._housekeeping()
        self._update_backend_label()
        self._pump()
        self._poll_foreground()
        if start_hidden:
            root.after(200, self.hide)
        elif self.first_run:
            root.after(400, lambda: SettingsDialog(self, welcome=True))

    # ---- layout

    def _build(self):
        r, pal = self.root, self.pal

        top = ttk.Frame(r, padding=(12, 10, 12, 6))
        top.pack(fill="x")
        ttk.Label(top, text="WinPilot", font=("Segoe UI Semibold", 15)).pack(side="left")
        self.backend_label = ttk.Label(top, foreground=pal["muted"])
        self.backend_label.pack(side="left", padx=(12, 0), pady=(4, 0))
        ttk.Button(top, text="Settings", command=lambda: SettingsDialog(self)).pack(side="right")
        ttk.Button(top, text="New chat", command=self.new_chat).pack(side="right", padx=6)

        # Bottom area first, so the panes can never squeeze it off the window.
        self.statusbar = ttk.Frame(r, padding=(12, 2, 12, 8))
        self.statusbar.pack(fill="x", side="bottom")
        self.status_dot = tk.Label(self.statusbar, text="●", fg=pal["ok"], bg=self._bg(), font=("Segoe UI", 10))
        self.status_dot.pack(side="left")
        self.status = tk.StringVar(value="Ready")
        ttk.Label(self.statusbar, textvariable=self.status).pack(side="left", padx=(4, 12))
        self.progress = ttk.Progressbar(self.statusbar, mode="indeterminate", length=120)
        self.foreground = tk.StringVar()
        ttk.Label(self.statusbar, textvariable=self.foreground, foreground=pal["muted"]).pack(side="right")

        bottom = ttk.Frame(r, padding=(12, 6, 12, 4))
        bottom.pack(fill="x", side="bottom")
        self.examples = ttk.Frame(bottom)
        self.examples.pack(fill="x", pady=(0, 6))
        ttk.Label(self.examples, text="Try:", foreground=pal["muted"]).pack(side="left", padx=(0, 6))
        for text in EXAMPLES[:3]:
            ttk.Button(self.examples, text=text if len(text) < 40 else text[:38] + "...",
                       command=lambda t=text: self._use_example(t)).pack(side="left", padx=(0, 6))
        row = ttk.Frame(bottom)
        row.pack(fill="x")
        self.input = tk.Text(row, height=3, wrap="word", font=("Segoe UI", 11), relief="flat", padx=8, pady=6,
                             bg=pal["bg"], fg=pal["fg"], insertbackground=pal["fg"],
                             highlightthickness=1, highlightbackground=pal["view"], highlightcolor=pal["user"])
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Return>", self._on_enter)
        buttons = ttk.Frame(row)
        buttons.pack(side="right", padx=(8, 0))
        self.send_btn = ttk.Button(buttons, text="Send", style="Accent.TButton", command=self.send, width=9)
        self.send_btn.pack(fill="x")
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled", width=9)
        self.stop_btn.pack(fill="x", pady=(6, 0))
        ttk.Label(bottom, text="Enter sends · Shift+Enter new line · Ctrl+Alt+Space show/hide · "
                               "Ctrl+Alt+X or mouse in the top-left corner stops", foreground=pal["muted"],
                  font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))

        self.approval = tk.Frame(r, bg=pal["ask_bg"], padx=12, pady=10)
        self.approval_text = tk.Label(self.approval, bg=pal["ask_bg"], fg=pal["ask_fg"], justify="left",
                                      anchor="w", font=("Segoe UI Semibold", 10))
        self.approval_text.pack(side="left", fill="x", expand=True)
        ttk.Button(self.approval, text="Deny", command=lambda: self._decide("deny")).pack(side="right")
        ttk.Button(self.approval, text="Allow all this task",
                   command=lambda: self._decide("all")).pack(side="right", padx=6)
        ttk.Button(self.approval, text="Allow", style="Accent.TButton",
                   command=lambda: self._decide("allow")).pack(side="right")

        self.panes = panes = ttk.PanedWindow(r, orient="horizontal")
        self.tabs = ttk.Notebook(panes)
        panes.add(self.tabs, weight=3)
        self._build_chat_tab()
        self._build_activity_tab()

        view = ttk.Frame(panes, padding=(8, 0, 0, 0))
        self.view_caption = ttk.Label(view, text="Agent's view", foreground=pal["muted"])
        self.view_caption.pack(anchor="w")
        self.view = tk.Label(view, bg=pal["view"], fg=pal["muted"], text="Screenshots the agent takes\nshow up here.")
        self.view.pack(fill="both", expand=True, pady=(4, 6))
        self.live_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(view, text="Live desktop view", variable=self.live_var,
                        command=lambda: setattr(self, "live_on", self.live_var.get())).pack(anchor="w")
        self.cursor_info = tk.StringVar()
        ttk.Label(view, textvariable=self.cursor_info, foreground=pal["muted"], font=("Segoe UI", 8),
                  wraplength=320, justify="left").pack(anchor="w", pady=(4, 0))
        panes.add(view, weight=2)
        panes.pack(fill="both", expand=True, padx=12)
        self.input.focus_set()

    def _bg(self):
        return ttk.Style().lookup("TFrame", "background") or self.pal["bg"]

    def _build_chat_tab(self):
        pal = self.pal
        frame = ttk.Frame(self.tabs)
        self.feed = tk.Text(frame, wrap="word", state="disabled", font=("Segoe UI", 10), relief="flat",
                            padx=12, pady=10, bg=pal["bg"], fg=pal["fg"], highlightthickness=0)
        sb = ttk.Scrollbar(frame, command=self.feed.yview)
        self.feed.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.feed.pack(side="left", fill="both", expand=True)
        for tag, opts in {"user": {"foreground": pal["user"], "font": ("Segoe UI Semibold", 10), "spacing1": 10},
                          "assistant": {"foreground": pal["fg"], "spacing1": 4, "spacing3": 4},
                          "tool": {"foreground": pal["tool"], "font": ("Consolas", 9), "spacing1": 2},
                          "result": {"foreground": pal["muted"], "font": ("Consolas", 9),
                                     "lmargin1": 18, "lmargin2": 18},
                          "error": {"foreground": pal["error"]},
                          "info": {"foreground": pal["muted"], "font": ("Segoe UI", 9, "italic")}}.items():
            self.feed.tag_configure(tag, **opts)
        self.tabs.add(frame, text="  Chat  ")
        self._welcome()

    def _build_activity_tab(self):
        pal = self.pal
        frame = ttk.Frame(self.tabs, padding=(0, 6, 0, 0))
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 6))
        self.who_filter = tk.StringVar(value="Everyone")
        box = ttk.Combobox(bar, textvariable=self.who_filter, state="readonly", width=10,
                           values=["Everyone", "You", "Agent", "Windows"])
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda e: self._reload_activity())
        self.search = tk.StringVar()
        search = ttk.Entry(bar, textvariable=self.search, width=22)
        search.pack(side="left", padx=6)
        search.bind("<KeyRelease>", lambda e: self._reload_activity())
        ttk.Button(bar, text="Open log folder",
                   command=lambda: (self.log.folder.mkdir(parents=True, exist_ok=True),
                                    os.startfile(self.log.folder))).pack(side="right")
        ttk.Button(bar, text="Clear view", command=lambda: self.activity.delete(*self.activity.get_children())
                   ).pack(side="right", padx=6)
        self.record_btn = ttk.Button(bar, command=self._toggle_recording, width=13)
        self.record_btn.pack(side="right")
        self._set_record_label()

        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        cols = ("time", "who", "what", "window")
        self.activity = ttk.Treeview(table, columns=cols, show="headings", selectmode="browse")
        for col, title, width, stretch in (("time", "Time", 70, False), ("who", "Who", 70, False),
                                           ("what", "What happened", 280, True), ("window", "Window", 220, True)):
            self.activity.heading(col, text=title, anchor="w")
            self.activity.column(col, width=width, stretch=stretch, anchor="w")
        for source in WHO:
            self.activity.tag_configure(source, foreground=pal[source])
        sb = ttk.Scrollbar(table, command=self.activity.yview)
        self.activity.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.activity.pack(side="left", fill="both", expand=True)
        self.tabs.add(frame, text="  Activity  ")

    def _welcome(self):
        self._write("info", "Tell me what to do on your desktop, and I'll do it with the mouse and keyboard. "
                            "I ask before every click, keystroke or app launch. The Activity tab records "
                            "what happens on the desktop (never the text you type), and I can read it.")

    def _use_example(self, text):
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)
        self.input.focus_set()

    def _housekeeping(self):
        """Delete old activity logs and rotate the action log now, then once a day."""
        def run():
            try:
                housekeeping.clean(int(self.cfg["keep_logs_days"]))
            except Exception as e:
                self.events.put(("info", f"Cleanup failed: {e}"))
        threading.Thread(target=run, daemon=True).start()
        self.root.after(24 * 3600 * 1000, self._housekeeping)

    # ---- activity tab

    def _matches(self, e):
        who = {"You": "you", "Agent": "agent", "Windows": "system"}.get(self.who_filter.get())
        needle = self.search.get().strip().lower()
        return ((not who or e["source"] == who)
                and (not needle or needle in f'{e["detail"]} {e["window"]} {e["exe"]}'.lower()))

    def _add_activity_row(self, e):
        if not self._matches(e):
            return
        at_bottom = self.activity.yview()[1] > 0.98
        window = f'{e["window"]}  [{e["exe"]}]' if e["window"] else ""
        self.activity.insert("", "end", values=(e["time"][11:], WHO[e["source"]], e["detail"], window),
                             tags=(e["source"],))
        rows = self.activity.get_children()
        if len(rows) > 3000:
            self.activity.delete(*rows[:500])
        if at_bottom:
            self.activity.yview_moveto(1.0)

    def _reload_activity(self):
        self.activity.delete(*self.activity.get_children())
        for e in self.log.query(last=3000):
            self._add_activity_row(e)

    def _toggle_recording(self):
        self.log.paused = not self.log.paused
        self._set_record_label()

    def _set_record_label(self):
        self.record_btn.configure(text="▶  Resume" if self.log.paused else "●  Recording")

    # ---- window management (some of these run on the agent thread)

    def show(self):
        user32.ShowWindow(self.hwnd, SW_RESTORE if user32.IsIconic(self.hwnd) else SW_SHOW)
        cb.activate(self.hwnd)
        self.input.focus_set()

    def hide(self):
        user32.ShowWindow(self.hwnd, SW_HIDE)

    def hide_for_action(self):
        if not user32.IsWindowVisible(self.hwnd):
            return False
        user32.ShowWindow(self.hwnd, SW_HIDE)
        time.sleep(0.15)  # let the window under it repaint and take focus
        return True

    def show_after_action(self):
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    def restore_target_focus(self):
        """Keys must not land in WinPilot itself: give focus back to the window the user was in."""
        fg = cb.root_of(user32.GetForegroundWindow())
        if fg in (0, self.hwnd) and self.last_external and user32.IsWindow(self.last_external):
            cb.activate(self.last_external)

    def _poll_foreground(self):
        fg = cb.root_of(user32.GetForegroundWindow())
        if fg and fg != self.hwnd:
            self.last_external = fg
        if self.last_external:
            self.foreground.set(f"Active: {cb.window_title(self.last_external)[:60]}")
        x, y = cb.cursor_pos()
        under = cb.root_of(user32.WindowFromPoint(wt.POINT(x, y)))
        self.cursor_info.set(f"Mouse ({x}, {y}) over {cb.window_title(under)[:50] or cb.process_name(under)}")
        self.root.after(300, self._poll_foreground)

    # ---- chat

    def _on_enter(self, event):
        if not event.state & 0x1:  # Shift+Enter inserts a newline
            self.send()
            return "break"

    def send(self):
        text = self.input.get("1.0", "end").strip()
        if not text or self.running:
            return
        self.input.delete("1.0", "end")
        self.examples.pack_forget()
        self.tabs.select(0)
        self._write("user", f"You: {text}")
        self._set_running(True)
        self.agent.tasks.put((text, dict(self.cfg)))

    def _set_running(self, running):
        self.running = running
        self.send_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        if running:
            self.progress.pack(side="left")
            self.progress.start(12)
            self.status_dot.configure(fg=self.pal["tool"])
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.status_dot.configure(fg=self.pal["ok"])
            self.status.set("Ready")

    def stop(self):
        if self.running:
            self.agent.stop()
            self.status.set("Stopping...")

    def new_chat(self):
        if self.running:
            return
        self.agent.backend = None
        self.feed.configure(state="normal")
        self.feed.delete("1.0", "end")
        self.feed.configure(state="disabled")
        self._welcome()
        self.examples.pack(fill="x", pady=(0, 6), before=self.examples.master.winfo_children()[1])

    def _decide(self, verdict):
        self.approval.pack_forget()
        self.agent.decide(verdict)

    def _write(self, tag, text):
        self.feed.configure(state="normal")
        self.feed.insert("end", text.rstrip() + "\n", tag)
        self.feed.configure(state="disabled")
        self.feed.see("end")

    def _show_image(self, img, caption):
        self.view_caption.configure(text=caption)
        w, h = max(self.view.winfo_width(), 200), max(self.view.winfo_height(), 150)
        img = img.copy()
        img.thumbnail((w, h))
        self._photo = ImageTk.PhotoImage(img)
        self.view.configure(image=self._photo, text="")

    def _notify(self, text):
        if not user32.IsWindowVisible(self.hwnd):
            try:
                self.tray.notify(text[:250], "WinPilot")
            except Exception:
                pass

    def _pump(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "status":
                    self.status.set(data)
                elif kind == "assistant":
                    self._write("assistant", data)
                elif kind == "tool":
                    self._write("tool", f"> {data}")
                elif kind == "result":
                    lines = data.splitlines()
                    preview = "\n".join(lines[:6]) + (f"\n... ({len(lines) - 6} more lines)" if len(lines) > 6 else "")
                    self._write("result", preview or "(no output)")
                elif kind in ("tool_error", "error"):
                    self._write("error", data)
                    if kind == "error":
                        self._notify(data)
                elif kind == "info":
                    self._write("info", data)
                elif kind == "activity":
                    self._add_activity_row(data)
                elif kind == "image":
                    self.hold_until = time.time() + 6  # keep the agent's screenshot up for a moment
                    self._show_image(Image.open(io.BytesIO(base64.b64decode(data))), "Agent's view: last screenshot")
                elif kind == "live":
                    if time.time() > self.hold_until:
                        self._show_image(data, "Live desktop")
                elif kind == "approve":
                    self.approval_text.configure(text=f"Allow?   {data}",
                                                 wraplength=max(300, self.root.winfo_width() - 420))
                    self.approval.pack(fill="x", side="bottom", padx=12, pady=(6, 0), before=self.panes)
                    self.status.set("Waiting for your approval")
                    self.status_dot.configure(fg=self.pal["error"])
                    if not user32.IsWindowVisible(self.hwnd) or user32.IsIconic(self.hwnd):
                        self.show()
                    self.root.bell()
                elif kind == "done":
                    self.approval.pack_forget()
                    self._set_running(False)
                    last = self.feed.get("end-2l", "end-1c").strip()
                    self._notify(last or "Task finished.")
                elif kind == "hotkey":
                    if data == "stop":
                        self.stop()
                    elif user32.IsWindowVisible(self.hwnd) and not user32.IsIconic(self.hwnd):
                        self.hide()
                    else:
                        self.show()
                elif kind == "tray":
                    {"show": self.show, "stop": self.stop, "quit": self.quit}[data]()
                elif kind == "takeover":
                    if self.running:
                        self.agent.stop()
                        self._write("info", "You moved the mouse, so I stopped. (Turn this off in Settings.)")
                elif kind == "call":  # run a function on the Tk thread
                    data()
                if kind == "status" and self.running:
                    self.status_dot.configure(fg=self.pal["tool"])
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _update_backend_label(self):
        c = self.cfg
        model = (c["claude_cli_model"] or "default model") if c["backend"] == "claude_cli" else c["lemonade_model"]
        self.backend_label.configure(text=f"{PROVIDERS[c['backend']]} · {model}")

    def apply_settings(self, cfg):
        self.cfg = cfg
        cb.set_input_speed(cfg["input_speed"])
        save_settings(cfg)
        self._update_backend_label()

    def quit(self):
        self.agent.stop()
        self.tray.stop()
        self.root.destroy()
        os._exit(0)


def test_connection(cfg):
    """Raise on failure, return a short success message."""
    if cfg["backend"] == "lemonade":
        models = lemonade_models(cfg["lemonade_url"])
        if cfg["lemonade_model"] not in models:
            return f"Connected, but '{cfg['lemonade_model']}' isn't installed. Available: {', '.join(models[:6])}"
        return f"Connected to Lemonade. {cfg['lemonade_model']} is available."
    return cli_bridge.test_cli(cfg)


class SettingsDialog(tk.Toplevel):
    def __init__(self, app, welcome=False):
        super().__init__(app.root)
        self.app, c = app, app.cfg
        self.title("WinPilot settings")
        self.resizable(False, False)
        self.transient(app.root)
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        if welcome:
            ttk.Label(outer, text="Welcome to WinPilot! Pick the AI model to use, test it, then save.",
                      font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(0, 10))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        self.vars = {k: (tk.BooleanVar if isinstance(v, bool) else tk.StringVar)(value=v) for k, v in c.items()}
        self.backend = tk.StringVar(value=PROVIDERS[c["backend"]])
        f, row = None, 0

        def section(text):  # each section is a tab, so the dialog fits on small screens
            nonlocal f, row
            f, row = ttk.Frame(tabs, padding=16), 0
            f.columnconfigure(1, weight=1)
            tabs.add(f, text=f"  {text}  ")

        def add(label, widget, note=None):
            nonlocal row
            if label:
                ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 12))
            widget.grid(row=row, column=1, sticky="we", pady=4)
            if note:
                row += 1
                ttk.Label(f, text=note, foreground=app.pal["muted"], wraplength=400,
                          justify="left").grid(row=row, column=1, sticky="w", pady=(0, 4))
            row += 1

        section("Model")
        add("Provider", ttk.Combobox(f, textvariable=self.backend, state="readonly", width=40,
                                     values=list(PROVIDERS.values())),
            "A local model keeps everything on this PC. Claude Code uses your Claude login (no API key).")
        add("Lemonade URL", ttk.Entry(f, textvariable=self.vars["lemonade_url"], width=42))
        lm = ttk.Frame(f)
        self.lemonade_box = ttk.Combobox(lm, textvariable=self.vars["lemonade_model"], width=29)
        self.lemonade_box.pack(side="left", fill="x", expand=True)
        ttk.Button(lm, text="Refresh", command=self._refresh_models).pack(side="left", padx=(6, 0))
        add("Local model", lm, "Use a vision model (e.g. Qwen3-VL) so screenshots work. Needs ctx_size >= 32768.")
        self.style = tk.StringVar(value=LOCAL_STYLES.get(c.get("local_style", "tools"), LOCAL_STYLES["tools"]))
        add("Model type", ttk.Combobox(f, textvariable=self.style, state="readonly", width=40,
                                       values=list(LOCAL_STYLES.values())),
            "Fara-7B and UI-TARS-1.5 are trained to use a computer from screenshots alone; pick the type "
            "that matches the model above.")

        section("Claude Code")
        add("Claude model", ttk.Combobox(f, textvariable=self.vars["claude_cli_model"], values=CLI_MODELS, width=40),
            "Blank uses Claude Code's default. Or opus, sonnet, haiku, or a full model id.")
        add("Effort", ttk.Combobox(f, textvariable=self.vars["effort"], state="readonly",
                                   values=["low", "medium", "high"], width=40), "Higher is more careful but slower.")
        add("Claude Code path", ttk.Entry(f, textvariable=self.vars["claude_cli_path"], width=42),
            "Blank finds `claude` on PATH. Log in once by running `claude` in a terminal. Claude Code only gets "
            "WinPilot's desktop tools here (no file or shell access), and every action still needs your approval.")

        section("Safety and privacy")
        add("Max steps per task", ttk.Spinbox(f, from_=5, to=200, textvariable=self.vars["max_steps"], width=8))
        self.speed = tk.StringVar(value=INPUT_SPEEDS.get(c.get("input_speed", "human"), INPUT_SPEEDS["human"]))
        add("Input speed", ttk.Combobox(f, textvariable=self.speed, state="readonly", width=24,
                                        values=list(INPUT_SPEEDS.values())),
            "Human-like moves the mouse along curved paths and types at a person's pace, with natural pauses. "
            "Moving the mouse yourself mid-movement stops it.")
        add("", ttk.Checkbutton(f, text="Ask before clicking, typing, dragging or launching apps",
                                variable=self.vars["ask_before_input"]))
        add("", ttk.Checkbutton(f, text="Hide this window while the agent acts",
                                variable=self.vars["hide_during_actions"]))
        add("", ttk.Checkbutton(f, text="Stop the task when I move the mouse myself",
                                variable=self.vars["takeover"]))
        add("", ttk.Checkbutton(f, text="Record my own clicks and key presses in the Activity log",
                                variable=self.vars["record_input"]),
            "Only shortcuts and key names are recorded. Typed text is counted, never stored.")

        section("More")
        add("Vision detection", ttk.Label(
            f, wraplength=400, justify="left", foreground=app.pal["ok" if omniparser.available() else "muted"],
            text="OmniParser is installed: the agent can find buttons by vision in apps that expose no UI "
                 "elements." if omniparser.available() else
                 "Not part of the installed app (it needs about 1 GB). It is available when running WinPilot "
                 "from the source repository." if FROZEN else
                 "Not installed. To let the agent find buttons by vision in games and custom-drawn apps, "
                 "run WinControlMCP\\setup_omniparser.cmd once (downloads about 1 GB)."))
        keep = ttk.Frame(f)
        ttk.Spinbox(keep, from_=1, to=3650, textvariable=self.vars["keep_logs_days"], width=6).pack(side="left")
        ttk.Label(keep, text="days").pack(side="left", padx=(6, 0))
        ttk.Button(keep, text="Clean up now", command=self._clean_now).pack(side="left", padx=(16, 0))
        add("Keep activity logs", keep, "Older daily logs are deleted automatically once a day. The action log "
                                        "rotates at 5 MB. maintenance.cmd does a deeper cleanup and health check.")
        self.clean_result = ttk.Label(f, foreground=app.pal["muted"], wraplength=400, justify="left")
        add("", self.clean_result)
        try:
            startup_on = shortcut_path("Startup").exists()
        except Exception:
            startup_on = False
        self.startup = tk.BooleanVar(value=startup_on)
        add("Shortcuts", ttk.Checkbutton(f, text="Start WinPilot in the tray when Windows starts",
                                         variable=self.startup))
        add("", ttk.Button(f, text="Create desktop shortcut", command=self._desktop_shortcut))

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(12, 0))
        ttk.Button(bottom, text="Test connection", command=self._test).pack(side="left")
        self.test_result = ttk.Label(bottom, wraplength=300, justify="left")
        self.test_result.pack(side="left", padx=10)
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="Save", style="Accent.TButton", command=self._save).pack(side="right", padx=8)
        self.grab_set()

    def _collect(self):
        cfg = {k: v.get() for k, v in self.vars.items()}
        cfg["backend"] = next(k for k, v in PROVIDERS.items() if v == self.backend.get())
        cfg["local_style"] = next(k for k, v in LOCAL_STYLES.items() if v == self.style.get())
        cfg["input_speed"] = next(k for k, v in INPUT_SPEEDS.items() if v == self.speed.get())
        cfg["max_steps"] = max(1, int(cfg["max_steps"]))
        cfg["lemonade_url"] = lemonade_base(cfg["lemonade_url"])
        cfg["keep_logs_days"] = max(1, int(cfg["keep_logs_days"]))
        return {k: v.strip() if isinstance(v, str) else v for k, v in cfg.items()}

    def _refresh_models(self):
        try:
            self.lemonade_box.configure(values=lemonade_models(self.vars["lemonade_url"].get()))
            self.lemonade_box.event_generate("<Down>")
        except Exception as e:
            messagebox.showerror("WinPilot", str(e), parent=self)

    def _test(self):
        try:
            cfg = self._collect()
        except ValueError:
            return
        self.test_result.configure(text="Testing...", foreground=self.app.pal["muted"])

        def run():
            try:
                msg, color = test_connection(cfg), self.app.pal["ok"]
            except Exception as e:
                msg, color = friendly_error(e), self.app.pal["error"]
            self.app.events.put(("call", lambda: self.winfo_exists() and self.test_result.configure(
                text=msg, foreground=color)))
        threading.Thread(target=run, daemon=True).start()

    def _clean_now(self):
        try:
            days = max(1, int(self.vars["keep_logs_days"].get()))
        except ValueError:
            return
        self.clean_result.configure(text="Cleaning...")

        def run():
            try:
                text = "\n".join(housekeeping.clean(days))
            except Exception as e:
                text = f"Cleanup failed: {e}"
            self.app.events.put(("call", lambda: self.winfo_exists() and self.clean_result.configure(text=text)))
        threading.Thread(target=run, daemon=True).start()

    def _desktop_shortcut(self):
        try:
            make_shortcut("Desktop")
            messagebox.showinfo("WinPilot", "Added a WinPilot shortcut to your desktop.", parent=self)
        except Exception as e:
            messagebox.showerror("WinPilot", f"Couldn't create the shortcut: {e}", parent=self)

    def _save(self):
        try:
            cfg = self._collect()
        except ValueError:
            messagebox.showerror("WinPilot", "Max steps must be a number.", parent=self)
            return
        try:
            link = shortcut_path("Startup")
            if self.startup.get() and not link.exists():
                make_shortcut("Startup", hidden=True)
            elif not self.startup.get() and link.exists():
                link.unlink()
        except Exception as e:
            messagebox.showerror("WinPilot", f"Couldn't change the startup shortcut: {e}", parent=self)
        if self.app.running:
            messagebox.showinfo("WinPilot", "Settings will apply to the next task.", parent=self)
        self.app.apply_settings(cfg)
        self.destroy()


def main():
    root = tk.Tk()
    App(root, start_hidden="--hidden" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    main()
