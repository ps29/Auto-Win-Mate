"""ClickBot - a dependency-free click/keyboard automation tool for Windows.

Run:  pythonw clickbot.py   (or double-click run_clickbot.bat)

Global hotkeys:
    F6  start / stop playback
    F7  start / stop recording your clicks
    F8  capture the mouse position (adds a click step, or sets the Quick-clicker point)
Failsafe: push the mouse into the top-left corner of the screen to stop playback.

Note: to automate a program that runs as Administrator, ClickBot must also run
as Administrator (Windows blocks input from lower-privilege processes).
"""
import ctypes
import ctypes.wintypes as wt
import json
import math
import queue
import random
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Must happen before any window exists so all coordinates are physical pixels.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

# ---------------------------------------------------------------- Win32 bindings

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
MAGIC = 0xC11C0B07  # tags our own injected input so the recorder ignores it


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)


def _sig(fn, restype, *argtypes):
    fn.restype, fn.argtypes = restype, argtypes


_sig(user32.SendInput, wt.UINT, wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_sig(user32.SetCursorPos, wt.BOOL, ctypes.c_int, ctypes.c_int)
_sig(user32.GetCursorPos, wt.BOOL, ctypes.POINTER(wt.POINT))
_sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
_sig(user32.EnumWindows, wt.BOOL, WNDENUMPROC, wt.LPARAM)
_sig(user32.IsWindowVisible, wt.BOOL, wt.HWND)
_sig(user32.IsWindow, wt.BOOL, wt.HWND)
_sig(user32.IsIconic, wt.BOOL, wt.HWND)
_sig(user32.GetWindowTextLengthW, ctypes.c_int, wt.HWND)
_sig(user32.GetWindowTextW, ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
_sig(user32.GetForegroundWindow, wt.HWND)
_sig(user32.SetForegroundWindow, wt.BOOL, wt.HWND)
_sig(user32.ShowWindow, wt.BOOL, wt.HWND, ctypes.c_int)
_sig(user32.ClientToScreen, wt.BOOL, wt.HWND, ctypes.POINTER(wt.POINT))
_sig(user32.ScreenToClient, wt.BOOL, wt.HWND, ctypes.POINTER(wt.POINT))
_sig(user32.WindowFromPoint, wt.HWND, wt.POINT)
_sig(user32.GetAncestor, wt.HWND, wt.HWND, wt.UINT)
_sig(user32.GetWindowThreadProcessId, wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD))
_sig(user32.MapVirtualKeyW, wt.UINT, wt.UINT, wt.UINT)
_sig(user32.VkKeyScanW, ctypes.c_short, wt.WCHAR)
_sig(user32.SetWindowsHookExW, wt.HANDLE, ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
_sig(user32.CallNextHookEx, LRESULT, wt.HANDLE, ctypes.c_int, wt.WPARAM, wt.LPARAM)
_sig(user32.UnhookWindowsHookEx, wt.BOOL, wt.HANDLE)
_sig(user32.RegisterHotKey, wt.BOOL, wt.HWND, ctypes.c_int, wt.UINT, wt.UINT)
_sig(user32.GetMessageW, wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
_sig(user32.PostThreadMessageW, wt.BOOL, wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
_sig(kernel32.GetModuleHandleW, wt.HMODULE, wt.LPCWSTR)
_sig(kernel32.GetCurrentThreadId, wt.DWORD)
_sig(kernel32.OpenProcess, wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
_sig(kernel32.QueryFullProcessImageNameW, wt.BOOL, wt.HANDLE, wt.DWORD, wt.LPWSTR,
     ctypes.POINTER(wt.DWORD))
_sig(kernel32.CloseHandle, wt.BOOL, wt.HANDLE)
_sig(dwmapi.DwmGetWindowAttribute, ctypes.c_long, wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD)

WM_QUIT, WM_HOTKEY = 0x0012, 0x0312
WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_MOUSEWHEEL = 0x201, 0x204, 0x207, 0x20A
REC_MSGS = {WM_LBUTTONDOWN: "left", WM_RBUTTONDOWN: "right",
            WM_MBUTTONDOWN: "middle", WM_MOUSEWHEEL: "wheel"}

# ---------------------------------------------------------------- window helpers


def cursor_pos():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def root_of(hwnd):
    return user32.GetAncestor(hwnd, 2) if hwnd else None  # GA_ROOT


def window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def process_name(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return "?"
    try:
        buf, size = ctypes.create_unicode_buffer(1024), wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return "?"
    finally:
        kernel32.CloseHandle(handle)


def _is_cloaked(hwnd):
    # Hidden UWP/background windows report as visible but are "cloaked" by DWM.
    cloaked = wt.DWORD()
    dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return cloaked.value != 0


def list_windows():
    """Visible top-level windows as (hwnd, title, exe)."""
    found = []

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not _is_cloaked(hwnd):
            title = window_title(hwnd)
            if title:
                found.append((hwnd, title, process_name(hwnd)))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def client_to_screen(hwnd, x, y):
    p = wt.POINT(x, y)
    user32.ClientToScreen(hwnd, ctypes.byref(p))
    return p.x, p.y


def screen_to_client(hwnd, x, y):
    p = wt.POINT(x, y)
    user32.ScreenToClient(hwnd, ctypes.byref(p))
    return p.x, p.y


def at_failsafe():
    pos = cursor_pos()
    return pos == (0, 0) or pos == (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77))

# ---------------------------------------------------------------- input injection


class InputBlocked(RuntimeError):
    pass


LAST_INJECTED = 0.0  # time.monotonic() of our latest synthetic input, so watchers can tell it from the user's


def _set_cursor(x, y):
    global LAST_INJECTED
    LAST_INJECTED = time.monotonic()
    user32.SetCursorPos(x, y)


def _send(*inputs):
    global LAST_INJECTED
    LAST_INJECTED = time.monotonic()
    arr = (INPUT * len(inputs))(*inputs)
    ctypes.set_last_error(0)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    # UIPI drops input silently, but leaves ERROR_ACCESS_DENIED behind.
    if sent != len(inputs) or ctypes.get_last_error() == 5:
        fg = root_of(user32.GetForegroundWindow())
        raise InputBlocked(
            f"Windows blocked the simulated input: the window in front ({process_name(fg) if fg else '?'}, "
            f"\"{window_title(fg) if fg else ''}\") belongs to a program running as administrator or as a "
            "system service. Bring the target window to the front, or run as administrator.")


def _mouse(flags, data=0):
    event = INPUT(type=0)
    event.u.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, MAGIC)
    return event


BUTTON_FLAGS = {"left": (0x02, 0x04), "right": (0x08, 0x10), "middle": (0x20, 0x40)}


# ---- input speed: instant (ClickBot's default) or human-like

# Human-like timing, scaled by "pace" (1 = an average person, higher = slower and more careful).
SPEEDS = {"instant": None, "human": {"pace": 1.0, "cps": (7.0, 12.0)}, "careful": {"pace": 1.7, "cps": (4.0, 7.0)}}
_speed = None


def set_input_speed(name):
    """'instant' (no delays, ClickBot's default), 'human' or 'careful'."""
    global _speed
    if name not in SPEEDS:
        raise ValueError(f"unknown input speed '{name}' (choose {', '.join(SPEEDS)})")
    _speed = SPEEDS[name]


def input_speed():
    return next(name for name, value in SPEEDS.items() if value is _speed)


class MovedByUser(RuntimeError):
    pass


def _pause(low, high):
    """Sleep a random human-ish while (seconds, scaled by pace); nothing in instant mode."""
    if _speed:
        time.sleep(random.uniform(low, high) * _speed["pace"])


def human_path(start, end, rng=random):
    """Points from start to end along a slightly curved, eased path, like a hand moving a mouse:
    a Bezier curve bowing to one side, with minimum-jerk easing (slow start, fast middle, slow end)."""
    (x0, y0), (x1, y1) = start, end
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 2:
        return [tuple(end)]
    bow = rng.uniform(-0.18, 0.18) * dist  # how far the curve bends sideways
    nx, ny = -dy / dist, dx / dist  # unit normal
    c1 = (x0 + dx * 0.3 + nx * bow, y0 + dy * 0.3 + ny * bow)
    c2 = (x0 + dx * 0.7 + nx * bow * 0.6, y0 + dy * 0.7 + ny * bow * 0.6)
    steps = max(8, min(90, int(dist / 9)))
    points = []
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * t * (10 - 15 * t + 6 * t * t)  # minimum-jerk easing
        u = 1 - t
        x = u ** 3 * x0 + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t ** 3 * x1
        y = u ** 3 * y0 + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t ** 3 * y1
        points.append((round(x), round(y)))
    points[-1] = (x1, y1)
    return points


def clamp_to_screen(x, y):
    """The nearest point on the virtual desktop (SetCursorPos clamps to it, so a path must too)."""
    left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    return min(max(x, left), left + width - 1), min(max(y, top), top + height - 1)


def _glide(pos, pace=1.0):
    """Move the pointer to pos: a jump in instant mode, a hand-like movement otherwise.
    Raises MovedByUser if someone moves the mouse while it glides."""
    if not _speed:
        _set_cursor(*pos)
        return
    start = cursor_pos()
    dist = math.hypot(pos[0] - start[0], pos[1] - start[1])
    # Fitts's law: far targets take longer, but not proportionally longer (about 0.15-0.6 s).
    duration = (0.12 + 0.11 * math.log2(1 + dist / 25)) * _speed["pace"] * pace * random.uniform(0.85, 1.15)
    path = [clamp_to_screen(*p) for p in human_path(start, pos)]
    gap = duration / len(path)
    last = start
    for point in path:
        here = cursor_pos()
        if abs(here[0] - last[0]) + abs(here[1] - last[1]) > 40:  # the user has the mouse
            raise MovedByUser("Stopped: the mouse was moved by hand while the agent was moving it.")
        _set_cursor(*point)
        last = point
        time.sleep(gap)


def click(pos, button="left", clicks=1):
    """Click at screen pos (x, y), or wherever the cursor is when pos is None."""
    if pos:
        _glide(pos)
        _pause(0.06, 0.16)  # aim before pressing
        time.sleep(0.02)  # let the app register the hover before the press
    down, up = BUTTON_FLAGS[button]
    for n in range(clicks):
        if _speed:
            _send(_mouse(down))
            time.sleep(random.uniform(0.05, 0.11))  # a press lasts a moment
            _send(_mouse(up))
        else:
            _send(_mouse(down), _mouse(up))
        if n < clicks - 1:
            time.sleep(random.uniform(0.08, 0.13) if _speed else 0.05)  # stays inside the double-click time


def _wheel(flag, pos, notches):
    if pos:
        _glide(pos)
        _pause(0.05, 0.12)
        time.sleep(0.02)
    if not _speed:
        _send(_mouse(flag, notches * 120))
        return
    step = 1 if notches > 0 else -1
    for _ in range(abs(notches)):  # one notch at a time, like a finger on a wheel
        _send(_mouse(flag, step * 120))
        time.sleep(random.uniform(0.03, 0.08) * _speed["pace"])


def scroll(pos, notches):
    _wheel(0x0800, pos, notches)


def hscroll(pos, notches):
    """Horizontal wheel: positive scrolls right."""
    _wheel(0x1000, pos, notches)


def move_to(pos):
    """Move the pointer to screen pos (x, y) without clicking, so the app sees a hover."""
    _glide(pos)
    _send(_mouse(0x0001))  # a zero-distance relative move makes apps process WM_MOUSEMOVE
    time.sleep(0.02)


def drag(start, end, button="left", steps=12):
    """Press at start, move to end, release. Human-like speeds move slower while the button is down."""
    down, up = BUTTON_FLAGS[button]
    move_to(start)
    _pause(0.08, 0.15)
    _send(_mouse(down))
    try:
        _pause(0.08, 0.16)  # people pause after grabbing something
        if _speed:
            for point in human_path(start, end):
                _set_cursor(*point)
                _send(_mouse(0x0001))
                time.sleep(random.uniform(0.012, 0.02) * _speed["pace"] * 1.3)
        else:
            for i in range(1, steps + 1):
                time.sleep(0.02)
                _set_cursor(round(start[0] + (end[0] - start[0]) * i / steps),
                            round(start[1] + (end[1] - start[1]) * i / steps))
                _send(_mouse(0x0001))
        time.sleep(0.05)
        _pause(0.06, 0.12)
    finally:
        _send(_mouse(up))


VK = {"enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
      "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24,
      "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26,
      "right": 0x27, "down": 0x28, "ctrl": 0x11, "control": 0x11, "shift": 0x10,
      "alt": 0x12, "win": 0x5B, "capslock": 0x14, "printscreen": 0x2C, "apps": 0x5D}
VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
VK.update({str(d): 0x30 + d for d in range(10)})
VK.update({f"f{n}": 0x6F + n for n in range(1, 25)})
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D}


def _key(vk, up=False):
    flags = (0x2 if up else 0) | (0x1 if vk in EXTENDED_VKS else 0)
    event = INPUT(type=1)
    event.u.ki = KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), flags, 0, MAGIC)
    return event


def _vk_for(name):
    if name in VK:
        return [VK[name]]
    if len(name) == 1:  # punctuation such as / . , - = [ ] on the current keyboard layout
        code = user32.VkKeyScanW(name)
        if code != -1:
            return ([VK["shift"]] if code & 0x100 else []) + [code & 0xFF]
    raise ValueError(f"unknown key '{name}' (examples: enter, ctrl+s, alt+f4)")


def parse_combo(combo):
    names = [p.strip().lower() for p in combo.replace("++", "+plus").split("+")]
    if not combo.strip():
        raise ValueError("empty key combo (examples: enter, ctrl+s, alt+f4)")
    vks = []
    for name in names:
        vks += [v for v in _vk_for("+" if name == "plus" else name) if v not in vks]
    return vks


def press_combo(combo):
    vks = parse_combo(combo)
    if not _speed:
        _send(*[_key(v) for v in vks], *[_key(v, up=True) for v in reversed(vks)])
        return
    for v in vks:  # modifiers first, one finger at a time
        _send(_key(v))
        time.sleep(random.uniform(0.03, 0.07) * _speed["pace"])
    time.sleep(random.uniform(0.03, 0.06))
    for v in reversed(vks):
        _send(_key(v, up=True))
        time.sleep(random.uniform(0.015, 0.04))


def keys_down(combo):
    """Press and hold a combo (released by keys_up); returns what to release."""
    vks = parse_combo(combo) if combo else []
    if vks:
        _send(*[_key(v) for v in vks])
    return vks


def keys_up(vks):
    if vks:
        _send(*[_key(v, up=True) for v in reversed(vks)])


def mouse_button(button, down):
    _send(_mouse(BUTTON_FLAGS[button][0 if down else 1]))


def type_text(text, delay=0.01):
    """Type text. Instant mode waits `delay` between characters; human-like modes type at a
    person's pace: an uneven rhythm, a little slower after spaces and punctuation, and an
    occasional pause to think."""
    for i, ch in enumerate(text):
        if ch == "\n":
            press_combo("enter")
        else:
            raw = ch.encode("utf-16-le")
            for j in range(0, len(raw), 2):  # surrogate pairs need two units
                code = int.from_bytes(raw[j:j + 2], "little")
                down, up = INPUT(type=1), INPUT(type=1)
                down.u.ki = KEYBDINPUT(0, code, 0x4, 0, MAGIC)
                up.u.ki = KEYBDINPUT(0, code, 0x4 | 0x2, 0, MAGIC)
                if _speed:
                    _send(down)
                    time.sleep(random.uniform(0.02, 0.06))  # how long the key stays down
                    _send(up)
                else:
                    _send(down, up)
        if not _speed:
            time.sleep(delay)
            continue
        gap = 1 / random.uniform(*_speed["cps"])
        if ch in " .,;:!?\n" and i + 1 < len(text):
            gap += random.uniform(0.04, 0.15)  # word and sentence boundaries
        if random.random() < 0.03:
            gap += random.uniform(0.2, 0.5)  # a short pause to think
        time.sleep(gap * _speed["pace"])


def activate(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if root_of(user32.GetForegroundWindow()) != hwnd:
        # A synthetic Alt tap lets a background process take the foreground.
        try:
            _send(_key(0x12), _key(0x12, up=True))
        except InputBlocked:
            pass
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
    if root_of(user32.GetForegroundWindow()) != hwnd:
        # Focus held by a process we can't take it from (e.g. a system service's
        # hidden window): minimizing and restoring still activates the window.
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        time.sleep(0.2)
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)

# ---------------------------------------------------------------- actions


def _where(a):
    if a.get("x") is None or a.get("y") is None:
        return "at cursor"
    return f"at ({a['x']}, {a['y']}) " + ("in target" if a.get("relative") else "on screen")


def describe(a):
    t = a["type"]
    if t == "click":
        n = a.get("clicks", 1)
        return f"{a.get('button', 'left').title()} click{f' x{n}' if n > 1 else ''} {_where(a)}"
    if t == "wait":
        return f"Wait {a['ms']} ms"
    if t == "type":
        text = a["text"].replace("\n", "\\n")
        return f'Type "{text[:40]}{"..." if len(text) > 40 else ""}"'
    if t == "key":
        return f"Press {a['key']}"
    if t == "scroll":
        return f"Scroll {'up' if a['amount'] > 0 else 'down'} {abs(a['amount'])} {_where(a)}"
    return str(a)


POINT_FIELDS = [("x", "X (blank = wherever the cursor is)", "optint"),
                ("y", "Y", "optint"),
                ("relative", "X/Y are relative to the target window", "bool")]
FIELDS = {
    "click": [("button", "Button", "choice:left,right,middle"),
              ("clicks", "Clicks (2 = double-click)", "int")] + POINT_FIELDS,
    "wait": [("ms", "Milliseconds", "int")],
    "type": [("text", "Text to type", "str")],
    "key": [("key", "Key or combo (enter, ctrl+s, alt+f4...)", "key")],
    "scroll": [("amount", "Notches (+ up, - down)", "int")] + POINT_FIELDS,
}
DEFAULTS = {
    "click": {"type": "click", "button": "left", "clicks": 1, "x": None, "y": None, "relative": False},
    "wait": {"type": "wait", "ms": 500},
    "type": {"type": "type", "text": ""},
    "key": {"type": "key", "key": "enter"},
    "scroll": {"type": "scroll", "amount": -3, "x": None, "y": None, "relative": False},
}


class Stopped(Exception):
    pass


class Player(threading.Thread):
    """Runs a list of actions on a background thread; reports via the event queue."""

    def __init__(self, actions, target, opts, events):
        super().__init__(daemon=True)
        self.actions, self.target, self.o, self.events = actions, target, opts, events
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def _status(self, text):
        self.events.put(("status", text))

    def run(self):
        msg = "Finished."
        try:
            total = "forever" if self.o["repeats"] == 0 else self.o["repeats"]
            loop = 0
            while self.o["repeats"] == 0 or loop < self.o["repeats"]:
                loop += 1
                for i, a in enumerate(self.actions):
                    self._checkpoint()
                    self._status(f"Running - loop {loop}/{total}, step {i + 1}/{len(self.actions)}: "
                                 f"{describe(a)}   (F6 to stop)")
                    self._do(a)
        except Stopped as e:
            msg = str(e)
        except Exception as e:
            msg = f"Error: {e}"
        self.events.put(("done", msg))

    def _checkpoint(self):
        if self._stop_evt.is_set():
            raise Stopped("Stopped.")
        if at_failsafe():
            raise Stopped("Stopped by failsafe (mouse in top-left corner).")
        if not self.target:
            return
        paused = False
        while True:
            if not user32.IsWindow(self.target):
                raise Stopped("Target window was closed.")
            if not self.o["require_fg"] or root_of(user32.GetForegroundWindow()) == self.target:
                return
            if not paused:
                self._status("Paused - waiting for the target window to be in the foreground...")
                paused = True
            if self._stop_evt.wait(0.2):
                raise Stopped("Stopped.")
            if at_failsafe():
                raise Stopped("Stopped by failsafe (mouse in top-left corner).")

    def _sleep(self, seconds):
        end = time.perf_counter() + seconds
        while (left := end - time.perf_counter()) > 0:
            if self._stop_evt.wait(min(left, 0.05)):
                raise Stopped("Stopped.")
            self._checkpoint()

    def _point(self, a):
        x, y = a.get("x"), a.get("y")
        if x is None or y is None:
            return None
        if j := self.o["jitter_px"]:
            x, y = x + random.randint(-j, j), y + random.randint(-j, j)
        if a.get("relative"):
            x, y = client_to_screen(self.target, x, y)
        return x, y

    def _do(self, a):
        t = a["type"]
        if t == "wait":
            ms = a["ms"]
            if jd := self.o["jitter_delay"]:
                ms *= 1 + random.uniform(-jd, jd) / 100
            self._sleep(max(0.0, ms / 1000 / self.o["speed"]))
        elif t == "click":
            click(self._point(a), a.get("button", "left"), a.get("clicks", 1))
        elif t == "scroll":
            scroll(self._point(a), a["amount"])
        elif t == "type":
            type_text(a["text"])
        elif t == "key":
            press_combo(a["key"])

# ---------------------------------------------------------------- background listeners


class MouseRecorder(threading.Thread):
    """Low-level mouse hook that reports real (non-injected) clicks and wheel turns."""

    def __init__(self, events, ignore_hwnd):
        super().__init__(daemon=True)
        self.events, self.ignore = events, ignore_hwnd
        self._ready = threading.Event()
        self._proc = HOOKPROC(self._hook)  # keep a reference so it isn't garbage-collected

    def run(self):
        self._tid = kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(14, self._proc, kernel32.GetModuleHandleW(None), 0)
        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)

    def _hook(self, code, wparam, lparam):
        if code == 0 and wparam in REC_MSGS:
            info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if info.dwExtraInfo != MAGIC:
                win = root_of(user32.WindowFromPoint(info.pt))
                if win != self.ignore:
                    delta = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
                    self.events.put(("rec", REC_MSGS[wparam], info.pt.x, info.pt.y, win,
                                     delta, time.perf_counter()))
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def stop(self):
        self._ready.wait(1)
        user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


class HotkeyListener(threading.Thread):
    """System-wide hotkeys; the key press is swallowed so the target app never sees it."""

    def __init__(self, keys, events):
        super().__init__(daemon=True)
        self.keys, self.events = keys, events

    def run(self):
        ids, failed = {}, []
        for i, (name, vk) in enumerate(self.keys.items(), 1):
            if user32.RegisterHotKey(None, i, 0x4000, vk):  # MOD_NOREPEAT
                ids[i] = name
            else:
                failed.append(name)
        if failed:
            self.events.put(("status", f"Could not register hotkey(s) {', '.join(failed)} - "
                                       "another program is using them."))
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in ids:
                self.events.put(("hotkey", ids[msg.wParam]))

# ---------------------------------------------------------------- GUI


class FormDialog(tk.Toplevel):
    def __init__(self, parent, title, fields, values):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result, self.fields, self.vars = None, fields, {}
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both")
        first = None
        for row, (key, label, kind) in enumerate(fields):
            value = values.get(key)
            if kind == "bool":
                var = tk.BooleanVar(value=bool(value))
                ttk.Checkbutton(body, text=label, variable=var).grid(
                    row=row, column=0, columnspan=2, sticky="w", pady=2)
            else:
                ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 8))
                var = tk.StringVar(value="" if value is None else str(value))
                if kind.startswith("choice:"):
                    widget = ttk.Combobox(body, textvariable=var, values=kind[7:].split(","),
                                          state="readonly", width=28)
                else:
                    widget = ttk.Entry(body, textvariable=var, width=32)
                widget.grid(row=row, column=1, sticky="ew", pady=2)
                first = first or widget
            self.vars[key] = var
        buttons = ttk.Frame(body)
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        if first:
            first.focus_set()
        self.grab_set()
        self.wait_window()

    def _ok(self):
        out = {}
        for key, label, kind in self.fields:
            raw = self.vars[key].get()
            try:
                if kind == "bool":
                    out[key] = bool(raw)
                elif kind == "int":
                    out[key] = int(raw)
                elif kind == "optint":
                    out[key] = int(raw) if raw.strip() else None
                elif kind == "key":
                    parse_combo(raw)
                    out[key] = raw.strip().lower()
                else:
                    out[key] = raw
            except ValueError as e:
                messagebox.showerror("Invalid value", f"{label}: {e}", parent=self)
                return
        self.result = out
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.actions, self.windows = [], []
        self.player = self.recorder = None
        self.q_point = None  # (x, y, relative) for the Quick clicker
        self._last_rec = None
        self._minimized = False

        root.title("ClickBot")
        root.geometry("680x640")
        root.minsize(600, 540)
        self._build()
        root.update_idletasks()
        self.own_hwnd = root_of(root.winfo_id())
        self.refresh_windows()

        HotkeyListener({"F6": 0x75, "F7": 0x76, "F8": 0x77}, self.events).start()
        root.protocol("WM_DELETE_WINDOW", self._quit)
        root.after(30, self._pump)

    # ---- layout

    def _build(self):
        root, pad = self.root, dict(padx=8, pady=4)

        self.status = tk.StringVar(value="Ready. Pick a target window, then build or record a script.")
        ttk.Label(root, textvariable=self.status, relief="sunken", anchor="w", padding=(6, 3)).pack(
            side="bottom", fill="x")
        self.pos_var = tk.StringVar()
        ttk.Label(root, textvariable=self.pos_var, anchor="w").pack(side="bottom", fill="x", padx=8)
        ttk.Label(root, foreground="#666", anchor="w",
                  text="Hotkeys: F6 start/stop  |  F7 record  |  F8 capture cursor  |  "
                       "Failsafe: push mouse into the top-left corner").pack(side="bottom", fill="x", padx=8)

        run = ttk.LabelFrame(root, text="Run", padding=6)
        run.pack(side="bottom", fill="x", **pad)
        self.repeats, self.speed = tk.StringVar(value="1"), tk.StringVar(value="1.0")
        self.jitter_px, self.jitter_delay = tk.StringVar(value="0"), tk.StringVar(value="0")
        for col, (label, var) in enumerate([("Repeat (0 = forever)", self.repeats),
                                            ("Speed x", self.speed),
                                            ("Random offset +/- px", self.jitter_px),
                                            ("Random delay +/- %", self.jitter_delay)]):
            ttk.Label(run, text=label).grid(row=0, column=col, sticky="w", padx=4)
            ttk.Entry(run, textvariable=var, width=10).grid(row=1, column=col, sticky="w", padx=4)
        self.minimize = tk.BooleanVar(value=True)
        ttk.Checkbutton(run, text="Minimize ClickBot while running / recording",
                        variable=self.minimize).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.human = tk.BooleanVar(value=False)
        ttk.Checkbutton(run, text="Human-like mouse movement and typing (slower)",
                        variable=self.human).grid(row=3, column=0, columnspan=3, sticky="w")
        self.start_btn = ttk.Button(run, text="Start (F6)", command=self.toggle_run)
        self.start_btn.grid(row=0, column=4, rowspan=3, sticky="nsew", padx=(12, 4))
        run.columnconfigure(4, weight=1)

        tf = ttk.LabelFrame(root, text="Target window", padding=6)
        tf.pack(side="top", fill="x", **pad)
        self.target_box = ttk.Combobox(tf, state="readonly")
        self.target_box.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(tf, text="Refresh", command=self.refresh_windows).grid(row=0, column=1, padx=2)
        ttk.Button(tf, text="Pick in 3 s", command=self.pick_target_delayed).grid(row=0, column=2, padx=2)
        self.require_fg = tk.BooleanVar(value=True)
        ttk.Checkbutton(tf, text="Only act while the target is in the foreground (pause otherwise)",
                        variable=self.require_fg).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.bring_front = tk.BooleanVar(value=True)
        ttk.Checkbutton(tf, text="Bring the target to the front when starting",
                        variable=self.bring_front).grid(row=2, column=0, columnspan=3, sticky="w")
        tf.columnconfigure(0, weight=1)

        self.nb = ttk.Notebook(root)
        self.nb.pack(side="top", fill="both", expand=True, **pad)

        sf = ttk.Frame(self.nb, padding=6)
        self.nb.add(sf, text="Script")
        self.tree = ttk.Treeview(sf, columns=("n", "action"), show="headings", selectmode="browse")
        self.tree.heading("n", text="#")
        self.tree.heading("action", text="Action")
        self.tree.column("n", width=40, stretch=False, anchor="e")
        sb = ttk.Scrollbar(sf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", lambda e: self.edit_selected())
        self.tree.bind("<Delete>", lambda e: self.delete_selected())
        sf.rowconfigure(0, weight=1)
        sf.columnconfigure(0, weight=1)

        btns = ttk.Frame(sf)
        btns.grid(row=0, column=2, sticky="n", padx=(8, 0))
        self.rec_btn = ttk.Button(btns, text="Record (F7)", command=self.toggle_record)
        self.rec_btn.pack(fill="x", pady=(0, 6))
        for item in [("Add click", lambda: self.add("click")), ("Add wait", lambda: self.add("wait")),
                     ("Add type text", lambda: self.add("type")), ("Add key press", lambda: self.add("key")),
                     ("Add scroll", lambda: self.add("scroll")), None,
                     ("Edit", self.edit_selected), ("Delete", self.delete_selected),
                     ("Move up", lambda: self.move(-1)), ("Move down", lambda: self.move(1)),
                     ("Clear all", self.clear), None,
                     ("Load...", self.load), ("Save...", self.save)]:
            if item is None:
                ttk.Separator(btns).pack(fill="x", pady=6)
            else:
                ttk.Button(btns, text=item[0], command=item[1]).pack(fill="x", pady=1)

        qf = ttk.Frame(self.nb, padding=12)
        self.nb.add(qf, text="Quick clicker")
        self.q_interval, self.q_button = tk.StringVar(value="100"), tk.StringVar(value="left")
        self.q_double, self.q_where = tk.BooleanVar(value=False), tk.StringVar(value="cursor")
        ttk.Label(qf, text="Click every (ms)").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(qf, textvariable=self.q_interval, width=10).grid(row=0, column=1, sticky="w")
        ttk.Label(qf, text="Button").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(qf, textvariable=self.q_button, values=("left", "right", "middle"),
                     state="readonly", width=8).grid(row=1, column=1, sticky="w")
        ttk.Checkbutton(qf, text="Double-click", variable=self.q_double).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Radiobutton(qf, text="Click wherever the mouse is", value="cursor",
                        variable=self.q_where).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Radiobutton(qf, text="Click a fixed point:", value="point",
                        variable=self.q_where).grid(row=4, column=0, sticky="w")
        self.q_point_lbl = ttk.Label(qf, text="(none yet - hover the spot and press F8)")
        self.q_point_lbl.grid(row=4, column=1, sticky="w")
        ttk.Label(qf, foreground="#666", wraplength=520, justify="left",
                  text="Uses the Repeat setting below (0 = forever). Press F6 to start and stop.").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(14, 0))

    # ---- target window

    def target_hwnd(self):
        idx = self.target_box.current()
        if idx > 0 and idx <= len(self.windows):
            hwnd = self.windows[idx - 1][0]
            return hwnd if user32.IsWindow(hwnd) else None
        return None

    def refresh_windows(self, select=None):
        select = select if select is not None else self.target_hwnd()
        self.windows = [w for w in list_windows() if w[0] != self.own_hwnd]
        if select and select not in [w[0] for w in self.windows] and user32.IsWindow(select):
            self.windows.insert(0, (select, window_title(select), process_name(select)))
        labels = ["(none - use screen coordinates)"]
        labels += [f"{t[:70]}  [{exe}]" for _, t, exe in self.windows]
        self.target_box["values"] = labels
        idx = next((i + 1 for i, w in enumerate(self.windows) if w[0] == select), 0)
        self.target_box.current(idx)

    def pick_target_delayed(self):
        self.status.set("Click on the window you want to automate - picking it in 3 seconds...")

        def pick():
            hwnd = root_of(user32.GetForegroundWindow())
            if not hwnd or hwnd == self.own_hwnd:
                self.status.set("ClickBot was still in front - nothing picked. Try again.")
                return
            self.refresh_windows(select=hwnd)
            self.status.set(f"Target: {window_title(hwnd)}  [{process_name(hwnd)}]")

        self.root.after(3000, pick)

    def _point_for(self, x, y, win=None):
        """Screen point -> (x, y, relative): relative to the target if it's over the target."""
        target = self.target_hwnd()
        win = win if win is not None else root_of(user32.WindowFromPoint(wt.POINT(x, y)))
        if target and win == target:
            return (*screen_to_client(target, x, y), True)
        return x, y, False

    # ---- event pump (all background threads talk to the GUI through self.events)

    def _pump(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "hotkey":
                    {"F6": self.toggle_run, "F7": self.toggle_record, "F8": self.capture}[ev[1]]()
                elif ev[0] == "rec":
                    self.on_recorded(*ev[1:])
                elif ev[0] == "status":
                    self.status.set(ev[1])
                elif ev[0] == "done":
                    self.on_done(ev[1])
        except queue.Empty:
            pass
        x, y = cursor_pos()
        text = f"Mouse: {x}, {y} on screen"
        if target := self.target_hwnd():
            cx, cy = screen_to_client(target, x, y)
            text += f"   |   {cx}, {cy} in target"
        self.pos_var.set(text)
        self.root.after(50, self._pump)

    def _hide(self):
        if self.minimize.get():
            self.root.iconify()
            self._minimized = True

    def _unhide(self):
        if self._minimized:
            self.root.deiconify()
            self._minimized = False

    # ---- script editing

    def refresh_tree(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, a in enumerate(self.actions):
            self.tree.insert("", "end", iid=str(i), values=(i + 1, describe(a)))
        if select is not None and 0 <= select < len(self.actions):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

    def selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _insert(self, action):
        i = self.selected_index()
        i = len(self.actions) if i is None else i + 1
        self.actions.insert(i, action)
        self.refresh_tree(i)

    def add(self, kind):
        dlg = FormDialog(self.root, f"Add {kind}", FIELDS[kind], DEFAULTS[kind])
        if dlg.result is not None:
            self._insert({**DEFAULTS[kind], **dlg.result})

    def edit_selected(self):
        i = self.selected_index()
        if i is None:
            return
        a = self.actions[i]
        dlg = FormDialog(self.root, f"Edit {a['type']}", FIELDS[a["type"]], a)
        if dlg.result is not None:
            a.update(dlg.result)
            self.refresh_tree(i)

    def delete_selected(self):
        i = self.selected_index()
        if i is not None:
            del self.actions[i]
            self.refresh_tree(min(i, len(self.actions) - 1))

    def move(self, delta):
        i = self.selected_index()
        if i is None or not 0 <= i + delta < len(self.actions):
            return
        self.actions[i], self.actions[i + delta] = self.actions[i + delta], self.actions[i]
        self.refresh_tree(i + delta)

    def clear(self):
        if self.actions and messagebox.askyesno("ClickBot", "Remove all steps?"):
            self.actions.clear()
            self.refresh_tree()

    def capture(self):
        x, y, rel = self._point_for(*cursor_pos())
        where = "in target" if rel else "on screen"
        if self.nb.index("current") == 1:
            self.q_point = (x, y, rel)
            self.q_point_lbl.config(text=f"({x}, {y}) {where}")
            self.q_where.set("point")
            self.status.set(f"Quick clicker point set to ({x}, {y}) {where}.")
        else:
            self._insert({"type": "click", "button": "left", "clicks": 1, "x": x, "y": y, "relative": rel})
            self.status.set(f"Added click at ({x}, {y}) {where}.")

    # ---- save / load

    def save(self):
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("ClickBot script", "*.json")])
        if not path:
            return
        target = self.target_hwnd()
        data = {"version": 1,
                "target": {"title": window_title(target), "exe": process_name(target)} if target else None,
                "actions": self.actions}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.status.set(f"Saved {len(self.actions)} steps to {path}")

    def load(self):
        path = filedialog.askopenfilename(filetypes=[("ClickBot script", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            actions = data["actions"]
            if not all(isinstance(a, dict) and a.get("type") in FIELDS for a in actions):
                raise ValueError("unknown action type in file")
        except (OSError, ValueError, KeyError, TypeError) as e:
            messagebox.showerror("ClickBot", f"Could not load script:\n{e}")
            return
        self.actions = actions
        self.refresh_tree()
        msg = f"Loaded {len(actions)} steps."
        if tgt := data.get("target"):
            self.refresh_windows()
            same_exe = [w for w in self.windows if w[2].lower() == tgt["exe"].lower()]
            match = next((w for w in same_exe if w[1] == tgt["title"]), same_exe[0] if same_exe else None)
            if match:
                self.refresh_windows(select=match[0])
                msg += f" Target: {match[1]}"
            else:
                msg += f" Target window '{tgt['title']}' ({tgt['exe']}) isn't open - open it and select it."
        self.status.set(msg)

    # ---- recording

    def toggle_record(self):
        if self.recorder:
            self.recorder.stop()
            self.recorder = None
            self.rec_btn.config(text="Record (F7)")
            self._unhide()
            self.status.set(f"Recording stopped. Script has {len(self.actions)} steps.")
            return
        if self.player:
            return
        self.nb.select(0)
        self._last_rec = None
        self.recorder = MouseRecorder(self.events, self.own_hwnd)
        self.recorder.start()
        self.rec_btn.config(text="Stop recording (F7)")
        self.status.set("Recording clicks and scrolls... press F7 to stop.")
        self._hide()

    def on_recorded(self, kind, x, y, win, delta, t):
        if not self.recorder:
            return
        px, py, rel = self._point_for(x, y, win)
        prev = self.actions[-1] if self.actions else None
        gap = t - self._last_rec if self._last_rec is not None else None
        near = (prev is not None and prev.get("x") is not None and prev.get("relative") == rel
                and abs(prev["x"] - px) <= 4 and abs(prev["y"] - py) <= 4)
        if kind == "wheel":
            notches = int(delta / 120) or (1 if delta > 0 else -1)
            if prev and prev["type"] == "scroll" and gap is not None and gap < 0.6 and near:
                prev["amount"] += notches
            else:
                self._append_recorded(gap, {"type": "scroll", "amount": notches,
                                            "x": px, "y": py, "relative": rel})
        elif (prev and prev["type"] == "click" and prev["button"] == kind and near
              and gap is not None and gap < 0.4 and prev["clicks"] < 3):
            prev["clicks"] += 1  # fold into a double/triple click
        else:
            self._append_recorded(gap, {"type": "click", "button": kind, "clicks": 1,
                                        "x": px, "y": py, "relative": rel})
        self._last_rec = t
        self.refresh_tree(len(self.actions) - 1)

    def _append_recorded(self, gap, action):
        if gap is not None and gap >= 0.03:
            self.actions.append({"type": "wait", "ms": int(gap * 1000)})
        self.actions.append(action)

    # ---- playback

    def _read_opts(self):
        try:
            o = {"repeats": int(self.repeats.get()), "speed": float(self.speed.get()),
                 "jitter_px": int(self.jitter_px.get()), "jitter_delay": float(self.jitter_delay.get()),
                 "require_fg": self.require_fg.get()}
        except ValueError:
            o = None
        if o is None or o["repeats"] < 0 or o["speed"] <= 0 or o["jitter_px"] < 0 \
                or not 0 <= o["jitter_delay"] < 100:
            messagebox.showerror("ClickBot", "Repeat must be >= 0, speed > 0, offset >= 0, "
                                             "and delay randomness between 0 and 99.")
            return None
        return o

    def _quick_actions(self):
        try:
            interval = int(self.q_interval.get())
            if interval < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("ClickBot", "Click interval must be a whole number of ms (1 or more).")
            return None
        if self.q_where.get() == "point":
            if not self.q_point:
                messagebox.showerror("ClickBot", "Hover over the spot and press F8 to set the point first.")
                return None
            x, y, rel = self.q_point
        else:
            x = y = None
            rel = False
        return [{"type": "click", "button": self.q_button.get(), "clicks": 2 if self.q_double.get() else 1,
                 "x": x, "y": y, "relative": rel},
                {"type": "wait", "ms": interval}]

    def toggle_run(self):
        if self.player:
            self.player.stop()
            return
        if self.recorder:
            self.toggle_record()
            return
        quick = self.nb.index("current") == 1
        actions = self._quick_actions() if quick else [dict(a) for a in self.actions]
        if not actions:
            if actions is not None:
                messagebox.showinfo("ClickBot", "The script is empty - add or record some steps first.")
            return
        opts = self._read_opts()
        if not opts:
            return
        set_input_speed("human" if self.human.get() else "instant")
        target = self.target_hwnd()
        if not target and any(a.get("relative") for a in actions):
            messagebox.showerror("ClickBot", "This script uses positions relative to a target window. "
                                             "Select the target window first.")
            return
        if target and self.bring_front.get():
            activate(target)
        self.player = Player(actions, target, opts, self.events)
        self.player.start()
        self.start_btn.config(text="Stop (F6)")
        self._hide()

    def on_done(self, msg):
        self.player = None
        self.start_btn.config(text="Start (F6)")
        self.status.set(msg)
        self._unhide()

    def _quit(self):
        if self.player:
            self.player.stop()
        if self.recorder:
            self.recorder.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
