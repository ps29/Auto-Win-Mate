"""WinPilot - tell an AI what to do on your Windows desktop.

A tray app with a chat box. You type an instruction; an LLM (the Claude API, or a local
model served by Lemonade) plans it and drives the desktop through the WinControl tools
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("WINPILOT_SERVER_DIR", str(HERE.parent / "WinControlMCP")))
import server  # noqa: E402  (imports ClickBot too, which makes this process DPI-aware)
import activity  # noqa: E402

import mss  # noqa: E402
import pystray  # noqa: E402
from PIL import Image, ImageDraw, ImageTk  # noqa: E402

cb = server.cb
user32 = cb.user32
crypt32 = ctypes.WinDLL("crypt32")
cb._sig(user32.SetWindowDisplayAffinity, wt.BOOL, wt.HWND, wt.DWORD)
cb._sig(cb.kernel32.LocalFree, ctypes.c_void_p, ctypes.c_void_p)

WDA_EXCLUDEFROMCAPTURE = 0x11
SW_HIDE, SW_SHOWNOACTIVATE, SW_SHOW, SW_RESTORE = 0, 4, 5, 9
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000

# ---------------------------------------------------------------- settings

CONFIG = Path(os.environ.get("APPDATA", HERE)) / "WinPilot" / "settings.json"
DEFAULTS = {
    "backend": "claude",                      # "claude" or "lemonade"
    "claude_model": "claude-opus-5-5",
    "effort": "medium",
    "api_key": "",                            # blank: ANTHROPIC_API_KEY or an `ant auth login` profile
    "lemonade_url": "http://localhost:8000/api/v1",
    "lemonade_model": "Qwen3-VL-8B-Instruct-GGUF",
    "max_steps": 40,
    "ask_before_input": True,
    "hide_during_actions": True,
    "record_input": True,                     # log your own clicks and keys (never the text you type)
}
CLAUDE_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


for _fn in (crypt32.CryptProtectData, crypt32.CryptUnprotectData):
    cb._sig(_fn, wt.BOOL, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_Blob))


def _dpapi(data, protect):
    """Encrypt/decrypt bytes for the current Windows user (so the API key isn't stored in plain text)."""
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in, blob_out = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        cb.kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def load_settings():
    cfg = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG.read_text(encoding="utf-8"))
        cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
        if saved.get("api_key_dpapi"):
            cfg["api_key"] = _dpapi(base64.b64decode(saved["api_key_dpapi"]), False).decode()
    except (OSError, ValueError):
        pass
    return cfg


def save_settings(cfg):
    data = {k: v for k, v in cfg.items() if k != "api_key"}
    if cfg["api_key"]:
        data["api_key_dpapi"] = base64.b64encode(_dpapi(cfg["api_key"].encode(), True)).decode()
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")

# ---------------------------------------------------------------- LLM backends

SYSTEM = """You control the user's Windows desktop through tools, on their behalf. The user types \
requests into a small control window called WinPilot. It is hidden from screenshots and window \
lists; never try to interact with it.

How to work:
- Each task may start with a summary of what happened on the desktop since the last one. Call \
read_activity_log to see more, e.g. what the user just did or what your earlier actions changed.
- Start with list_windows. Prefer get_ui_elements with click_element / set_text, which are precise. \
Use screenshot with click(x, y, window=...) only when an app exposes no usable elements.
- You control the mouse (click, move_mouse to hover, drag, scroll) and the keyboard (type_text, \
press_keys; arrows, home/end and ctrl+arrows move the text cursor). Call get_cursor to see where \
the pointer, keyboard focus and text cursor are before typing.
- After each action, check the result (get_ui_elements again, or a screenshot) before moving on.
- The user may have to approve input actions. If one is denied, don't retry it: adjust the plan or ask.
- If the request is ambiguous, or would delete, overwrite, send, buy or close something the user may \
care about, stop and ask first.
- When you are finished, reply in one or two sentences: what you did, or what is blocking you."""


class Refused(Exception):
    pass


class ClaudeBackend:
    """Anthropic Messages API. History is append-only so thinking blocks stay valid."""

    def __init__(self, cfg, tools):
        import anthropic
        self.client = anthropic.Anthropic(api_key=cfg["api_key"] or None, max_retries=3)
        self.model, self.effort = cfg["claude_model"], cfg["effort"]
        self.tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                      for t in tools]
        self.messages = []

    def mark(self):
        return len(self.messages)

    def rollback(self, mark):
        del self.messages[mark:]

    def add_user(self, text):
        self.messages.append({"role": "user", "content": text})

    def step(self):
        extra = {}
        if self.model in FALLBACK_MODELS:  # re-run a declined request on Anthropic's recommended model
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        r = self.client.beta.messages.create(
            model=self.model, max_tokens=16000, system=SYSTEM, tools=self.tools, messages=self.messages,
            cache_control={"type": "ephemeral"}, output_config={"effort": self.effort}, **extra)
        if r.stop_reason == "refusal":
            why = getattr(r.stop_details, "explanation", None) if r.stop_details else None
            raise Refused(why or "The model declined this request.")
        self.messages.append({"role": "assistant", "content": r.content})
        text = "\n".join(b.text for b in r.content if b.type == "text").strip()
        calls = [(b.id, b.name, b.input) for b in r.content if b.type == "tool_use"]
        return text, calls, r.stop_reason == "pause_turn"

    def add_results(self, results):
        blocks = []
        for res in results:
            content = [{"type": "text", "text": res["text"] or "(no output)"}]
            content += [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
                        for mime, data in res["images"]]
            blocks.append({"type": "tool_result", "tool_use_id": res["id"], "content": content,
                           "is_error": res["error"]})
        self.messages.append({"role": "user", "content": blocks})


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
    return [m["id"] for m in _post_json(url.rstrip("/") + "/models", timeout=5).get("data", [])]


class LemonadeBackend:
    """OpenAI-compatible chat completions on a local Lemonade Server."""

    def __init__(self, cfg, tools):
        self.url, self.model = cfg["lemonade_url"].rstrip("/"), cfg["lemonade_model"]
        self.tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                        "parameters": t.input_schema}} for t in tools]
        self.messages = [{"role": "system", "content": SYSTEM}]

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

def friendly_error(e):
    """Turn API and network errors into something a user can act on."""
    try:
        import anthropic
        if isinstance(e, anthropic.AuthenticationError) or (  # no key at all raises a plain TypeError
                isinstance(e, (anthropic.AnthropicError, TypeError)) and "authentication" in str(e).lower()):
            return "No valid Claude API key. Open Settings and add your key (or set ANTHROPIC_API_KEY)."
        if isinstance(e, anthropic.PermissionDeniedError):
            return "Your API key can't use this model. Pick another model in Settings."
        if isinstance(e, anthropic.NotFoundError):
            return "Model not found. Check the model name in Settings."
        if isinstance(e, anthropic.RateLimitError):
            return "The Claude API is rate-limiting you. Wait a minute and try again."
        if isinstance(e, anthropic.APIConnectionError):
            return "Can't reach the Claude API. Check your internet connection."
        if isinstance(e, anthropic.APIStatusError):
            return f"The Claude API returned an error ({e.status_code}): {e.message}"
    except ImportError:
        pass
    return str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"

# ---------------------------------------------------------------- agent

NEEDS_APPROVAL = {"click_element", "set_text", "click", "type_text", "press_keys", "scroll", "start_app", "drag"}
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


class Agent(threading.Thread):
    """Runs one task at a time: model request -> approval -> tool call -> repeat."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app, self.events = app, app.events
        self.tasks = queue.Queue()
        self.stop_flag, self.decided = threading.Event(), threading.Event()
        self.decision = "deny"
        self.loop = asyncio.new_event_loop()
        self.backend, self.backend_key = None, None
        self.elements = {}  # element id -> description, from the latest get_ui_elements
        self.allow_all = False
        self.log = app.log
        self.seen_id = 0  # activity entries up to here were already shown to the model

    def emit(self, kind, data=None):
        self.events.put((kind, data))

    def stop(self):
        self.stop_flag.set()
        self.decide("deny")

    def decide(self, verdict):
        self.decision = verdict
        self.decided.set()

    def run(self):
        self.tool_defs = self.loop.run_until_complete(server.mcp.list_tools()) + [LOG_TOOL]
        while True:
            text, cfg = self.tasks.get()
            self.stop_flag.clear()
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
        if cfg["backend"] == "claude":
            key, make = ("claude", cfg["claude_model"], cfg["effort"], cfg["api_key"]), ClaudeBackend
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
            except Refused as e:
                b.rollback(mark)
                self.emit("error", f"Refused: {e}")
                return
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
            b.add_results([self._run_tool(*call) for call in calls])
        self.emit("error", f"Stopped after {cfg['max_steps']} steps (the limit is in Settings).")

    def _with_recent_activity(self, text):
        """Prefix the task with what happened since the model last looked (append-only, so history stays valid)."""
        recent = [e for e in self.log.query(last=25, after_id=self.seen_id) if e["source"] != "agent"]
        if not recent:
            return text
        lines = "\n".join(activity.format_entry(e) for e in recent)
        return f"What happened on the desktop since your last task (newest last):\n{lines}\n\nTask: {text}"

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
        if name in NEEDS_APPROVAL and not self.allow_all:
            self.decided.clear()
            self.emit("approve", action)
            self.decided.wait()
            if self.decision == "deny":
                self.emit("info", "Denied.")
                self.log.add("agent", "denied", f"you denied: {action}")
                return result("The user denied this action. Don't retry it; adjust the plan or ask the user.",
                              error=True)
            self.allow_all = self.decision == "all"
        self.emit("status", f"Running {name}...")
        hidden = name in MOVES_FOCUS and self.cfg["hide_during_actions"] and self.app.hide_for_action()
        try:
            if name in TARGET_FOCUS and args.get("window") is None:
                self.app.restore_target_focus()
            r = self.loop.run_until_complete(server.mcp.call_tool(name, args))
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
        looks = {"list_windows": "List open windows", "get_cursor": "Check the mouse and text cursor",
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
        activity.DesktopMonitor(self.log, self.hwnd, lambda: self.cfg["record_input"]).start()
        self.tray = pystray.Icon("WinPilot", tray_image(), "WinPilot", pystray.Menu(
            pystray.MenuItem("Show", lambda: self.events.put(("tray", "show")), default=True),
            pystray.MenuItem("Stop task", lambda: self.events.put(("tray", "stop"))),
            pystray.MenuItem("Quit", lambda: self.events.put(("tray", "quit")))))
        self.tray.run_detached()

        self.log.add("system", "app", "WinPilot started")
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
                elif kind == "call":  # run a function on the Tk thread
                    data()
                if kind == "status" and self.running:
                    self.status_dot.configure(fg=self.pal["tool"])
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _update_backend_label(self):
        c = self.cfg
        model = c["claude_model"] if c["backend"] == "claude" else c["lemonade_model"]
        self.backend_label.configure(text=f"{'Claude API' if c['backend'] == 'claude' else 'Lemonade'} · {model}")

    def apply_settings(self, cfg):
        self.cfg = cfg
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
    import anthropic
    client = anthropic.Anthropic(api_key=cfg["api_key"] or None, max_retries=1, timeout=60)
    client.messages.create(model=cfg["claude_model"], max_tokens=16, output_config={"effort": "low"},
                           messages=[{"role": "user", "content": "Reply with OK."}])
    return f"Connected to the Claude API. {cfg['claude_model']} answered."


class SettingsDialog(tk.Toplevel):
    def __init__(self, app, welcome=False):
        super().__init__(app.root)
        self.app, c = app, app.cfg
        self.title("WinPilot settings")
        self.resizable(False, False)
        self.transient(app.root)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both")
        f.columnconfigure(1, weight=1)
        self.vars = {k: (tk.BooleanVar if isinstance(v, bool) else tk.StringVar)(value=v) for k, v in c.items()}
        self.backend = tk.StringVar(value="Claude API" if c["backend"] == "claude" else "Lemonade (local)")
        row = 0

        def add(label, widget, note=None):
            nonlocal row
            if label:
                ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 12))
            widget.grid(row=row, column=1, sticky="we", pady=4)
            if note:
                row += 1
                ttk.Label(f, text=note, foreground=app.pal["muted"], font=("Segoe UI", 8), wraplength=420,
                          justify="left").grid(row=row, column=1, sticky="w")
            row += 1

        def section(text):
            nonlocal row
            ttk.Label(f, text=text, font=("Segoe UI Semibold", 11)).grid(row=row, column=0, columnspan=2,
                                                                         sticky="w", pady=(14 if row else 0, 2))
            row += 1

        if welcome:
            ttk.Label(f, text="Welcome to WinPilot! Pick the AI model to use, test it, then save.",
                      font=("Segoe UI Semibold", 11)).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 8))
            row += 1
        section("Model")
        add("Provider", ttk.Combobox(f, textvariable=self.backend, state="readonly", width=44,
                                     values=["Claude API", "Lemonade (local)"]))
        add("Claude model", ttk.Combobox(f, textvariable=self.vars["claude_model"], values=CLAUDE_MODELS, width=44))
        add("Effort", ttk.Combobox(f, textvariable=self.vars["effort"], state="readonly",
                                   values=["low", "medium", "high"], width=44), "Higher is more careful but slower.")
        add("API key", ttk.Entry(f, textvariable=self.vars["api_key"], show="•", width=46),
            "Blank uses ANTHROPIC_API_KEY or an `ant auth login` profile. Stored encrypted for your Windows user.")
        add("Lemonade URL", ttk.Entry(f, textvariable=self.vars["lemonade_url"], width=46))
        lm = ttk.Frame(f)
        self.lemonade_box = ttk.Combobox(lm, textvariable=self.vars["lemonade_model"], width=33)
        self.lemonade_box.pack(side="left", fill="x", expand=True)
        ttk.Button(lm, text="Refresh", command=self._refresh_models).pack(side="left", padx=(6, 0))
        add("Lemonade model", lm, "Use a vision model (e.g. Qwen3-VL) so screenshots work. Needs ctx_size >= 32768.")
        test = ttk.Frame(f)
        ttk.Button(test, text="Test connection", command=self._test).pack(side="left")
        self.test_result = ttk.Label(test, wraplength=330, justify="left")
        self.test_result.pack(side="left", padx=10)
        add("", test)

        section("Safety and privacy")
        add("Max steps per task", ttk.Spinbox(f, from_=5, to=200, textvariable=self.vars["max_steps"], width=8))
        add("", ttk.Checkbutton(f, text="Ask before clicking, typing, dragging or launching apps",
                                variable=self.vars["ask_before_input"]))
        add("", ttk.Checkbutton(f, text="Hide this window while the agent acts",
                                variable=self.vars["hide_during_actions"]))
        add("", ttk.Checkbutton(f, text="Record my own clicks and key presses in the Activity log",
                                variable=self.vars["record_input"]),
            "Only shortcuts and key names are recorded. Typed text is counted, never stored.")

        section("Shortcuts")
        try:
            startup_on = shortcut_path("Startup").exists()
        except Exception:
            startup_on = False
        self.startup = tk.BooleanVar(value=startup_on)
        add("", ttk.Checkbutton(f, text="Start WinPilot in the tray when Windows starts", variable=self.startup))
        add("", ttk.Button(f, text="Create desktop shortcut", command=self._desktop_shortcut))

        btns = ttk.Frame(f)
        btns.grid(row=row, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", style="Accent.TButton", command=self._save).pack(side="right", padx=8)
        self.grab_set()

    def _collect(self):
        cfg = {k: v.get() for k, v in self.vars.items()}
        cfg["backend"] = "claude" if self.backend.get() == "Claude API" else "lemonade"
        cfg["max_steps"] = max(1, int(cfg["max_steps"]))
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
