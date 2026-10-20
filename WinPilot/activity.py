"""Activity log for WinPilot: what happens on the desktop, by the agent, by you, and by Windows.

Recorded: agent actions, window switches, windows opening and closing, your clicks, scrolls,
shortcuts (ctrl+s) and named keys (enter, tab...). Typed text is never recorded: printable keys
are only counted, as "typed 12 characters".

Entries live in memory (newest 5000) and are appended to logs\\activity-YYYY-MM-DD.jsonl.
Import after `server` (or anything else that puts ..\\ClickBot on sys.path).
"""
import collections
import ctypes
import ctypes.wintypes as wt
import datetime
import json
import queue
import threading
import time

import clickbot as cb

user32, kernel32 = cb.user32, cb.kernel32

# ---------------------------------------------------------------- the log


class ActivityLog:
    def __init__(self, folder, maxlen=5000):
        self.folder = folder
        self.paused = False  # pauses your and system activity; agent actions are always kept
        self.listeners = []  # called with each new entry (from any thread)
        self._entries = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._next_id = 1

    @property
    def last_id(self):
        return self._next_id - 1

    def add(self, source, kind, detail, hwnd=None):
        if self.paused and source != "agent":
            return None
        now = datetime.datetime.now()
        entry = {"time": now.isoformat(timespec="seconds"), "source": source, "kind": kind, "detail": detail,
                 "window": cb.window_title(hwnd) if hwnd else "", "exe": cb.process_name(hwnd) if hwnd else ""}
        with self._lock:
            entry["id"] = self._next_id
            self._next_id += 1
            self._entries.append(entry)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self.folder / f"activity-{now:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        for listener in self.listeners:
            listener(entry)
        return entry

    def query(self, last=30, source="all", after_id=0):
        with self._lock:
            items = [e for e in self._entries
                     if e["id"] > after_id and (source == "all" or e["source"] == source)]
        return items[-last:] if last else items


def format_entry(e):
    where = f'  in "{e["window"]}" [{e["exe"]}]' if e["window"] else ""
    return f'{e["time"][11:]} {e["source"]:<6} {e["detail"]}{where}'

# ---------------------------------------------------------------- keyboard: count, never record


MODIFIERS = {0x10: "shift", 0xA0: "shift", 0xA1: "shift", 0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
             0x12: "alt", 0xA4: "alt", 0xA5: "alt", 0x5B: "win", 0x5C: "win"}
NAMED = {0x0D: "enter", 0x09: "tab", 0x1B: "esc", 0x2E: "delete", 0x2D: "insert", 0x21: "pageup",
         0x22: "pagedown", 0x23: "end", 0x24: "home", 0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
         0x2C: "printscreen", **{0x6F + n: f"f{n}" for n in range(1, 25)}}
KEY_NAMES = {**{vk: name for name, vk in cb.VK.items() if len(name) == 1}, **NAMED, 0x20: "space", 0x08: "backspace"}
MOD_ORDER = ("ctrl", "alt", "shift", "win")


class KeyAggregator:
    """Turns raw key events into log entries. Printable keys only bump a counter."""

    def __init__(self, log, idle=2.0):
        self.log, self.idle = log, idle
        self.held, self.count, self.hwnd, self.last = set(), 0, None, 0.0

    def key(self, vk, down, hwnd, t):
        if vk in MODIFIERS:
            (self.held.add if down else self.held.discard)(MODIFIERS[vk])
            return
        if not down:
            return
        if self.held & {"ctrl", "alt", "win"}:
            self.flush()
            combo = [m for m in MOD_ORDER if m in self.held] + [KEY_NAMES.get(vk, f"key{vk}")]
            self.log.add("you", "shortcut", f"pressed {'+'.join(combo)}", hwnd)
        elif vk in NAMED:
            self.flush()
            self.log.add("you", "key", f"pressed {NAMED[vk]}", hwnd)
        else:  # printable characters, space and backspace: typing
            if hwnd != self.hwnd:
                self.flush()
                self.hwnd = hwnd
            self.count += 1
            self.last = t

    def tick(self, t):
        if self.count and t - self.last >= self.idle:
            self.flush()

    def flush(self):
        if self.count:
            self.log.add("you", "typing", f"typed {self.count} character{'s' if self.count > 1 else ''}", self.hwnd)
            self.count = 0

# ---------------------------------------------------------------- user takeover


class TakeoverDetector:
    """Says when the user grabs the mouse: real (not injected) movement of more than
    `distance` pixels within `window` seconds. Our own synthetic input is ignored."""

    def __init__(self, distance=80, window=0.4, cooldown=2.0):
        self.distance, self.window, self.cooldown = distance, window, cooldown
        self.points, self.fired_at = [], -cooldown

    def feed(self, x, y, t):
        self.points = [p for p in self.points if t - p[2] <= self.window] + [(x, y, t)]
        x0, y0, _ = self.points[0]
        if abs(x - x0) + abs(y - y0) >= self.distance and t - self.fired_at >= self.cooldown:
            self.points, self.fired_at = [], t
            return True
        return False

# ---------------------------------------------------------------- hooks


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", cb.ULONG_PTR)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", cb.ULONG_PTR)]


WINEVENTPROC = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)
cb._sig(user32.SetWinEventHook, wt.HANDLE, wt.DWORD, wt.DWORD, wt.HMODULE, WINEVENTPROC, wt.DWORD, wt.DWORD,
        wt.DWORD)
cb._sig(user32.UnhookWinEvent, wt.BOOL, wt.HANDLE)
WH_KEYBOARD_LL, LLKHF_INJECTED = 13, 0x10
WH_MOUSE_LL, WM_MOUSEMOVE, LLMHF_INJECTED = 14, 0x200, 0x1
EVENT_SYSTEM_FOREGROUND, WINEVENT_OUTOFCONTEXT = 0x0003, 0x0000
KEY_DOWN_MSGS = {0x100, 0x104}  # WM_KEYDOWN, WM_SYSKEYDOWN


class DesktopMonitor:
    """Feeds the ActivityLog from a keyboard hook, a mouse hook, foreground changes and a window list diff.

    The hook callbacks only enqueue; one worker thread does the (slower) window lookups and logging.
    `record_input()` is checked live, so your clicks and keys can be switched off without restarting.
    """

    def __init__(self, log, ignore_hwnd, record_input=lambda: True, watch_takeover=lambda: False,
                 on_takeover=lambda: None):
        self.log, self.ignore, self.record_input = log, ignore_hwnd, record_input
        self.watch_takeover, self.on_takeover = watch_takeover, on_takeover
        self.raw = queue.Queue()
        self.keys = KeyAggregator(log)
        self.takeover = TakeoverDetector()
        self._kb_proc = cb.HOOKPROC(self._on_key)  # keep references so they aren't garbage-collected
        self._mouse_proc = cb.HOOKPROC(self._on_mouse)
        self._fg_proc = WINEVENTPROC(self._on_foreground)

    def start(self):
        threading.Thread(target=self._hooks, daemon=True).start()
        cb.MouseRecorder(self.raw, self.ignore).start()  # reports real clicks/wheel only, never injected ones
        threading.Thread(target=self._worker, daemon=True).start()

    def _hooks(self):
        kb = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, kernel32.GetModuleHandleW(None), 0)
        mouse = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, kernel32.GetModuleHandleW(None), 0)
        fg = user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._fg_proc,
                                    0, 0, WINEVENT_OUTOFCONTEXT)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(kb)
        user32.UnhookWindowsHookEx(mouse)
        user32.UnhookWinEvent(fg)

    def _on_key(self, code, wparam, lparam):
        if code == 0:
            info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if not info.flags & LLKHF_INJECTED and info.dwExtraInfo != cb.MAGIC:
                self.raw.put(("key", info.vkCode, wparam in KEY_DOWN_MSGS, time.monotonic()))
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_mouse(self, code, wparam, lparam):
        # Runs for every mouse move on the system: bail out as early as possible.
        if code == 0 and wparam == WM_MOUSEMOVE and self.watch_takeover():
            info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            now = time.monotonic()
            if (not info.flags & LLMHF_INJECTED and info.dwExtraInfo != cb.MAGIC
                    and now - cb.LAST_INJECTED > 0.5
                    and self.takeover.feed(info.pt.x, info.pt.y, now)):
                self.raw.put(("takeover",))
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_foreground(self, hook, event, hwnd, id_object, id_child, thread, ms):
        self.raw.put(("fg", hwnd))

    def _worker(self):
        windows, last_fg, last_scan, last_wheel = {}, None, 0.0, {}
        while True:
            try:
                item = self.raw.get(timeout=0.5)
            except queue.Empty:
                item = None
            now = time.monotonic()
            try:
                if item and item[0] == "key":
                    _, vk, down, t = item
                    hwnd = cb.root_of(user32.GetForegroundWindow())
                    if hwnd != self.ignore and self.record_input():
                        self.keys.key(vk, down, hwnd, t)
                elif item and item[0] == "rec" and self.record_input():
                    _, kind, x, y, hwnd, delta, _t = item
                    if kind == "wheel":  # one entry per window per 2 s, not one per notch
                        if now - last_wheel.get(hwnd, 0) > 2:
                            self.log.add("you", "scroll", f"scrolled {'up' if delta > 0 else 'down'}", hwnd)
                        last_wheel[hwnd] = now
                    else:
                        self.keys.flush()
                        self.log.add("you", "click", f"{kind}-clicked at ({x}, {y})", hwnd)
                elif item and item[0] == "takeover":
                    self.log.add("you", "takeover", "took over the mouse")
                    self.on_takeover()
                elif item and item[0] == "fg":
                    hwnd = cb.root_of(item[1])
                    if hwnd and hwnd != last_fg and hwnd != self.ignore and cb.window_title(hwnd):
                        last_fg = hwnd
                        self.keys.flush()
                        self.log.add("system", "focus", "switched to window", hwnd)
                self.keys.tick(now)
                if now - last_scan >= 2:
                    last_scan = now
                    current = {h: (t, e) for h, t, e in cb.list_windows() if h != self.ignore}
                    if windows:
                        for h in current.keys() - windows.keys():
                            self.log.add("system", "opened", "window opened", h)
                        for h in windows.keys() - current.keys():
                            t, e = windows[h]
                            self.log.add("system", "closed", f'window closed: "{t}" [{e}]')
                    windows = current
            except Exception:
                pass  # a window can vanish mid-lookup; never let the monitor die
