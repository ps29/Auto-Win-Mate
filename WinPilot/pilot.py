"""WinPilot - tell an AI what to do on your Windows desktop.

A tray app with a chat box. You type an instruction; an LLM (the Claude API, or a local
model served by Lemonade) plans it and drives the desktop through the WinControl tools
(..\\WinControlMCP\\server.py), which keep their failsafe, allowlist and action log.
Input actions (clicks, typing, launching apps) wait for your approval unless you allow them.

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
import urllib.error
import urllib.request
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("WINPILOT_SERVER_DIR", str(HERE.parent / "WinControlMCP")))
import server  # noqa: E402  (imports ClickBot too, which makes this process DPI-aware)

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
- Start with list_windows. Prefer get_ui_elements with click_element / set_text, which are precise. \
Use screenshot with click(x, y, window=...) only when an app exposes no usable elements.
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

# ---------------------------------------------------------------- agent

NEEDS_APPROVAL = {"click_element", "set_text", "click", "type_text", "press_keys", "scroll", "start_app"}
MOVES_FOCUS = NEEDS_APPROVAL | {"focus_window"}  # the WinPilot window hides while these run
KEYBOARD = {"type_text", "press_keys"}


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

    def emit(self, kind, data=None):
        self.events.put((kind, data))

    def stop(self):
        self.stop_flag.set()
        self.decide("deny")

    def decide(self, verdict):
        self.decision = verdict
        self.decided.set()

    def run(self):
        self.tool_defs = self.loop.run_until_complete(server.mcp.list_tools())
        while True:
            text, cfg = self.tasks.get()
            self.stop_flag.clear()
            self.allow_all = not cfg["ask_before_input"]
            try:
                self._task(text, cfg)
            except Exception as e:
                self.emit("error", f"{type(e).__name__}: {e}")
            finally:
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
        b.add_user(text)
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

    def _run_tool(self, call_id, name, args):
        def result(text, images=(), error=False):
            return {"id": call_id, "text": text, "images": list(images), "error": error}

        if self.stop_flag.is_set():
            return result("Not run: the user stopped the task.", error=True)
        if not isinstance(args, dict):
            self.emit("tool_error", f"{name}: arguments were not valid JSON")
            return result("Invalid arguments: send a JSON object matching the tool's schema.", error=True)
        self.emit("tool", f"{name}({json.dumps(args, ensure_ascii=False)[:200]})")
        if args.get("window") not in (None, ""):
            try:
                if server._resolve(args["window"]) == self.app.hwnd:
                    return result("That is the WinPilot control window; it can't be automated.", error=True)
            except ValueError:
                pass  # the tool reports a bad window itself
        if name in NEEDS_APPROVAL and not self.allow_all:
            self.decided.clear()
            self.emit("approve", self._describe(name, args))
            self.decided.wait()
            if self.decision == "deny":
                self.emit("info", "Denied.")
                return result("The user denied this action. Don't retry it; adjust the plan or ask the user.",
                              error=True)
            self.allow_all = self.decision == "all"
        self.emit("status", f"Running {name}...")
        hidden = name in MOVES_FOCUS and self.cfg["hide_during_actions"] and self.app.hide_for_action()
        try:
            if name in KEYBOARD and args.get("window") is None:
                self.app.restore_target_focus()
            r = self.loop.run_until_complete(server.mcp.call_tool(name, args))
        except Exception as e:
            msg = str(e).removeprefix(f"Error executing tool {name}: ")
            self.emit("tool_error", msg)
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
        return f"{name} {json.dumps(a)}"

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


class App:
    def __init__(self, root, start_hidden):
        self.root, self.cfg, self.events = root, load_settings(), queue.Queue()
        self.running, self.live_on, self.hold_until = False, False, 0.0
        self.last_external, self._photo = 0, None
        root.title("WinPilot")
        scale = root.winfo_fpixels("1i") / 96  # the process is DPI-aware, so sizes are physical pixels
        root.geometry(f"{round(820 * scale)}x{round(560 * scale)}")
        root.minsize(round(560 * scale), round(380 * scale))
        self._build()
        root.update()
        self.hwnd = int(root.wm_frame(), 16)
        user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE)  # keep it out of screenshots
        root.protocol("WM_DELETE_WINDOW", self.hide)

        self.agent = Agent(self)
        self.agent.start()
        Hotkeys(self.events).start()
        LiveView(self).start()
        self.tray = pystray.Icon("WinPilot", tray_image(), "WinPilot", pystray.Menu(
            pystray.MenuItem("Show", lambda: self.events.put(("tray", "show")), default=True),
            pystray.MenuItem("Stop task", lambda: self.events.put(("tray", "stop"))),
            pystray.MenuItem("Quit", lambda: self.events.put(("tray", "quit")))))
        self.tray.run_detached()

        self._write("info", "Tell me what to do on your desktop, e.g. \"open Notepad and type a shopping list\".\n"
                            "Clicks and typing wait for your approval. Ctrl+Alt+Space shows or hides this window; "
                            "Ctrl+Alt+X or the top-left screen corner stops everything.")
        self._update_backend_label()
        self._pump()
        self._poll_foreground()
        if start_hidden:
            root.after(200, self.hide)

    def _build(self):
        r = self.root
        style = ttk.Style()
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

        top = ttk.Frame(r, padding=(8, 6))
        top.pack(fill="x")
        self.backend_label = ttk.Label(top, font=("Segoe UI", 10, "bold"))
        self.backend_label.pack(side="left")
        ttk.Button(top, text="Settings", command=self.open_settings).pack(side="right")
        ttk.Button(top, text="New chat", command=self.new_chat).pack(side="right", padx=4)
        self.live_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Live view", variable=self.live_var,
                        command=lambda: setattr(self, "live_on", self.live_var.get())).pack(side="right", padx=8)
        self.status = tk.StringVar(value="Idle")
        ttk.Label(top, textvariable=self.status, foreground="#555").pack(side="left", padx=12)

        # Packed last (after the input row) so it can't squeeze the input off the window.
        self.panes = panes = ttk.PanedWindow(r, orient="horizontal")
        feed_frame = ttk.Frame(panes)
        self.feed = tk.Text(feed_frame, wrap="word", state="disabled", font=("Segoe UI", 10), relief="flat",
                            padx=8, pady=6, background="#fbfbfb")
        sb = ttk.Scrollbar(feed_frame, command=self.feed.yview)
        self.feed.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.feed.pack(side="left", fill="both", expand=True)
        for tag, opts in {"user": {"foreground": "#1d4ed8", "font": ("Segoe UI", 10, "bold"), "spacing1": 8},
                          "assistant": {"foreground": "#111", "spacing1": 4},
                          "tool": {"foreground": "#7c3aed", "font": ("Consolas", 9)},
                          "result": {"foreground": "#666", "font": ("Consolas", 9), "lmargin1": 18, "lmargin2": 18},
                          "error": {"foreground": "#b91c1c"},
                          "info": {"foreground": "#666", "font": ("Segoe UI", 9, "italic")}}.items():
            self.feed.tag_configure(tag, **opts)
        panes.add(feed_frame, weight=3)

        view = ttk.Frame(panes)
        self.view_caption = ttk.Label(view, text="Agent's view", foreground="#555")
        self.view_caption.pack(anchor="w")
        self.view = tk.Label(view, background="#e5e7eb")
        self.view.pack(fill="both", expand=True)
        panes.add(view, weight=2)

        self.approval = tk.Frame(r, background="#fef3c7", padx=10, pady=8)
        self.approval_text = tk.Label(self.approval, background="#fef3c7", font=("Segoe UI", 10, "bold"),
                                      justify="left", anchor="w")
        self.approval_text.pack(side="left", fill="x", expand=True)
        ttk.Button(self.approval, text="Deny", command=lambda: self._decide("deny")).pack(side="right")
        ttk.Button(self.approval, text="Allow all this task",
                   command=lambda: self._decide("all")).pack(side="right", padx=4)
        ttk.Button(self.approval, text="Allow", style="Accent.TButton",
                   command=lambda: self._decide("allow")).pack(side="right", padx=4)

        bottom = ttk.Frame(r, padding=8)
        bottom.pack(fill="x", side="bottom")
        self.input = tk.Text(bottom, height=3, wrap="word", font=("Segoe UI", 11), relief="solid", borderwidth=1)
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Return>", self._on_enter)
        buttons = ttk.Frame(bottom)
        buttons.pack(side="right", padx=(8, 0))
        self.send_btn = ttk.Button(buttons, text="Send", style="Accent.TButton", command=self.send)
        self.send_btn.pack(fill="x")
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(fill="x", pady=(4, 0))
        self.foreground = tk.StringVar()
        ttk.Label(r, textvariable=self.foreground, foreground="#777", padding=(8, 0)).pack(
            fill="x", side="bottom", before=bottom)
        panes.pack(fill="both", expand=True, padx=8)
        self.input.focus_set()

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
        """Typing must not land in WinPilot itself: give focus back to the window the user was in."""
        fg = cb.root_of(user32.GetForegroundWindow())
        if fg in (0, self.hwnd) and self.last_external and user32.IsWindow(self.last_external):
            cb.activate(self.last_external)

    def _poll_foreground(self):
        fg = cb.root_of(user32.GetForegroundWindow())
        if fg and fg != self.hwnd:
            self.last_external = fg
        if self.last_external:
            self.foreground.set(f"Last active window: {cb.window_title(self.last_external)} "
                                f"[{cb.process_name(self.last_external)}]")
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
        self._write("user", f"You: {text}")
        self.running = True
        self.send_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.agent.tasks.put((text, dict(self.cfg)))

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
        self._write("info", "New chat.")

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
        self.view.configure(image=self._photo)

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
                elif kind == "image":
                    self.hold_until = time.time() + 6  # keep the agent's screenshot up for a moment
                    self._show_image(Image.open(io.BytesIO(base64.b64decode(data))), "Agent's view: last screenshot")
                elif kind == "live":
                    if time.time() > self.hold_until:
                        self._show_image(data, "Live desktop")
                elif kind == "approve":
                    self.approval_text.configure(text=f"Allow?  {data}", wraplength=max(300, self.root.winfo_width() - 380))
                    self.approval.pack(fill="x", side="bottom", padx=8, pady=(6, 0), before=self.panes)
                    self.status.set("Waiting for your approval")
                    if not user32.IsWindowVisible(self.hwnd) or user32.IsIconic(self.hwnd):
                        self.show()
                    self.root.bell()
                elif kind == "done":
                    self.running = False
                    self.approval.pack_forget()
                    self.send_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.status.set("Idle")
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
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _update_backend_label(self):
        c = self.cfg
        model = c["claude_model"] if c["backend"] == "claude" else c["lemonade_model"]
        self.backend_label.configure(text=f"{'Claude API' if c['backend'] == 'claude' else 'Lemonade'} · {model}")

    def open_settings(self):
        SettingsDialog(self)

    def apply_settings(self, cfg):
        self.cfg = cfg
        save_settings(cfg)
        self._update_backend_label()

    def quit(self):
        self.agent.stop()
        self.tray.stop()
        self.root.destroy()
        os._exit(0)


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app, c = app, app.cfg
        self.title("WinPilot settings")
        self.resizable(False, False)
        self.transient(app.root)
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both")
        self.vars = {k: (tk.BooleanVar if isinstance(v, bool) else tk.StringVar)(value=v) for k, v in c.items()}
        self.backend = tk.StringVar(value="Claude API" if c["backend"] == "claude" else "Lemonade (local)")
        row = 0

        def add(label, widget, note=None):
            nonlocal row
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
            widget.grid(row=row, column=1, sticky="we", pady=3)
            if note:
                row += 1
                ttk.Label(f, text=note, foreground="#777", font=("Segoe UI", 8)).grid(row=row, column=1, sticky="w")
            row += 1

        add("Model provider", ttk.Combobox(f, textvariable=self.backend, state="readonly", width=40,
                                           values=["Claude API", "Lemonade (local)"]))
        ttk.Separator(f).grid(row=row, column=0, columnspan=2, sticky="we", pady=6)
        row += 1
        add("Claude model", ttk.Combobox(f, textvariable=self.vars["claude_model"], values=CLAUDE_MODELS, width=40))
        add("Effort", ttk.Combobox(f, textvariable=self.vars["effort"], state="readonly",
                                   values=["low", "medium", "high"], width=40),
            "Higher is more careful but slower.")
        add("API key", ttk.Entry(f, textvariable=self.vars["api_key"], show="*", width=42),
            "Blank uses ANTHROPIC_API_KEY or your `ant auth login` profile. Stored encrypted for your Windows user.")
        ttk.Separator(f).grid(row=row, column=0, columnspan=2, sticky="we", pady=6)
        row += 1
        add("Lemonade URL", ttk.Entry(f, textvariable=self.vars["lemonade_url"], width=42))
        lm = ttk.Frame(f)
        self.lemonade_box = ttk.Combobox(lm, textvariable=self.vars["lemonade_model"], width=31)
        self.lemonade_box.pack(side="left", fill="x", expand=True)
        ttk.Button(lm, text="Refresh", command=self._refresh_models).pack(side="left", padx=(4, 0))
        add("Lemonade model", lm, "Use a vision model (e.g. Qwen3-VL) so screenshots work. Needs ctx_size >= 32768.")
        ttk.Separator(f).grid(row=row, column=0, columnspan=2, sticky="we", pady=6)
        row += 1
        add("Max steps per task", ttk.Spinbox(f, from_=5, to=200, textvariable=self.vars["max_steps"], width=8))
        add("", ttk.Checkbutton(f, text="Ask before clicking, typing or launching apps",
                                variable=self.vars["ask_before_input"]))
        add("", ttk.Checkbutton(f, text="Hide this window while the agent acts",
                                variable=self.vars["hide_during_actions"]))
        btns = ttk.Frame(f)
        btns.grid(row=row, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=self._save).pack(side="right", padx=6)
        self.grab_set()

    def _refresh_models(self):
        try:
            self.lemonade_box.configure(values=lemonade_models(self.vars["lemonade_url"].get()))
            self.lemonade_box.event_generate("<Down>")
        except Exception as e:
            messagebox.showerror("WinPilot", str(e), parent=self)

    def _save(self):
        cfg = {k: v.get() for k, v in self.vars.items()}
        cfg["backend"] = "claude" if self.backend.get() == "Claude API" else "lemonade"
        try:
            cfg["max_steps"] = max(1, int(cfg["max_steps"]))
        except ValueError:
            messagebox.showerror("WinPilot", "Max steps must be a number.", parent=self)
            return
        cfg = {k: v.strip() if isinstance(v, str) else v for k, v in cfg.items()}
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
